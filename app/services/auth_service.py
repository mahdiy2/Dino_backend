from datetime import datetime, timedelta
import base64
import binascii
import re
import secrets
import time

from flask import current_app, has_request_context, request
from flask_jwt_extended import create_access_token, create_refresh_token
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.db import db
from app.extensions import redis_client as redis_backend
from app.repositories import pending_registration_repository, user_repository
from app.repositories import follow_repository, group_repository
from app.services import password_security


PENDING_REGISTRATION_TTL_SECONDS = 30 * 60
USERNAME_ALLOWED_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_LETTER_PATTERN = re.compile(r"[A-Za-z]")
_DIGIT_PATTERN = re.compile(r"\d")
_SYMBOL_PATTERN = re.compile(r"[^A-Za-z0-9]")
REFERRAL_CODE_ALLOWED_PATTERN = re.compile(r"^[A-Za-z0-9]{6,16}$")
REFERRAL_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REFERRAL_CODE_DEFAULT_LENGTH = 8
DEVICE_ID_ALLOWED_PATTERN = re.compile(r"^[A-Za-z0-9._-]{6,128}$")


class AuthError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _require_non_empty_string(value):
    return isinstance(value, str) and value.strip()


def _cleanup_expired_pending_registrations():
    # Cleanup is cheap and keeps "username already pending" scenarios predictable.
    pending_registration_repository.delete_expired()


def _validate_password_strength(password):
    min_length = int(current_app.config.get("AUTH_PASSWORD_MIN_LENGTH", 6))
    max_length = int(current_app.config.get("AUTH_PASSWORD_MAX_LENGTH", 128))
    require_symbol = bool(current_app.config.get("AUTH_PASSWORD_REQUIRE_SYMBOL", False))

    if not isinstance(password, str):
        raise AuthError("Password must be a string", status_code=400)

    if not password.strip():
        raise AuthError("Password must be a non-empty string", status_code=400)

    if len(password) < min_length:
        raise AuthError(
            f"Password must be at least {min_length} characters",
            status_code=400,
        )
    if len(password) > max_length:
        raise AuthError(
            f"Password must be at most {max_length} characters",
            status_code=400,
        )
    if not _LETTER_PATTERN.search(password):
        raise AuthError(
            "Password must include at least one letter",
            status_code=400,
        )
    if not _DIGIT_PATTERN.search(password):
        raise AuthError(
            "Password must include at least one number",
            status_code=400,
        )
    if require_symbol and not _SYMBOL_PATTERN.search(password):
        raise AuthError(
            "Password must include at least one symbol",
            status_code=400,
        )

    return password


def _decode_base64_variants(value: str):
    candidates = [value]
    if len(value) % 4:
        candidates.append(value + ("=" * (4 - (len(value) % 4))))

    for candidate in candidates:
        for altchars in (None, b"-_"):
            try:
                yield base64.b64decode(candidate, altchars=altchars, validate=False)
            except (binascii.Error, ValueError):
                continue


def _parse_rsa_public_key(public_key: str, depth: int = 0):
    if depth > 2:
        return None
    sanitized = (
        public_key.strip()
        .strip('"')
        .replace("\\\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "")
    )
    if "base64," in sanitized:
        sanitized = sanitized.split("base64,", 1)[1]

    pem_bytes = sanitized.encode("utf-8")
    try:
        parsed = serialization.load_pem_public_key(pem_bytes)
        return parsed if isinstance(parsed, rsa.RSAPublicKey) else None
    except (ValueError, TypeError, UnsupportedAlgorithm):
        pass

    try:
        parsed = serialization.load_ssh_public_key(pem_bytes)
        return parsed if isinstance(parsed, rsa.RSAPublicKey) else None
    except (ValueError, TypeError, UnsupportedAlgorithm):
        pass

    compact = "".join(sanitized.split())
    if not compact:
        return None

    for decoded in _decode_base64_variants(compact):
        try:
            parsed = serialization.load_der_public_key(decoded)
            if isinstance(parsed, rsa.RSAPublicKey):
                return parsed
        except (ValueError, TypeError, UnsupportedAlgorithm):
            pass

        try:
            nested = decoded.decode("utf-8")
        except UnicodeDecodeError:
            nested = ""
        if "BEGIN PUBLIC KEY" in nested or "BEGIN CERTIFICATE" in nested:
            nested_key = _parse_rsa_public_key(nested, depth + 1)
            if nested_key is not None:
                return nested_key

    return None


def _validate_public_key_format(public_key: str):
    if _parse_rsa_public_key(public_key) is None:
        raise AuthError(
            "Invalid public key format. Expected an RSA public key.",
            status_code=400,
        )


def _request_client_ip() -> str:
    if not has_request_context():
        return "internal"

    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        first_ip = forwarded_for.split(",", 1)[0].strip()
        if first_ip:
            return first_ip
    return (request.remote_addr or "unknown").strip() or "unknown"


def _rate_limit_key(username: str) -> str:
    return f"{username.lower()}|{_request_client_ip()}"


def _rate_limit_prefix() -> str:
    return str(current_app.config.get("AUTH_LOGIN_RATE_LIMIT_KEY_PREFIX", "auth:login_rl")).strip()


def _rate_limit_attempts_key(username: str) -> str:
    return f"{_rate_limit_prefix()}:attempts:{_rate_limit_key(username)}"


def _rate_limit_lock_key(username: str) -> str:
    return f"{_rate_limit_prefix()}:locked:{_rate_limit_key(username)}"


def _enforce_login_rate_limit(username: str):
    if not has_request_context():
        return

    max_attempts = int(current_app.config.get("AUTH_LOGIN_RATE_LIMIT_MAX_ATTEMPTS", 8))
    window_seconds = int(current_app.config.get("AUTH_LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300))
    lockout_seconds = int(current_app.config.get("AUTH_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS", 300))
    if max_attempts <= 0:
        return

    redis_client = redis_backend.redis_client
    now_ts = time.time()
    attempts_key = _rate_limit_attempts_key(username)
    lock_key = _rate_limit_lock_key(username)
    try:
        if redis_client.get(lock_key):
            raise AuthError("Too many login attempts. Try again later.", status_code=429)

        redis_client.zremrangebyscore(attempts_key, "-inf", now_ts - window_seconds)
        if int(redis_client.zcard(attempts_key) or 0) >= max_attempts:
            redis_client.setex(lock_key, lockout_seconds, "1")
            raise AuthError("Too many login attempts. Try again later.", status_code=429)
    except AuthError:
        raise
    except Exception:
        # Best-effort guard: auth flow remains available if Redis is unavailable.
        return


def _record_login_failure(username: str):
    if not has_request_context():
        return

    max_attempts = int(current_app.config.get("AUTH_LOGIN_RATE_LIMIT_MAX_ATTEMPTS", 8))
    window_seconds = int(current_app.config.get("AUTH_LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300))
    lockout_seconds = int(current_app.config.get("AUTH_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS", 300))
    if max_attempts <= 0:
        return

    redis_client = redis_backend.redis_client
    now_ts = time.time()
    attempts_key = _rate_limit_attempts_key(username)
    lock_key = _rate_limit_lock_key(username)
    ttl_seconds = max(window_seconds, lockout_seconds) + 60
    attempt_member = f"{now_ts}:{secrets.token_hex(4)}"
    try:
        pipe = redis_client.pipeline()
        pipe.zremrangebyscore(attempts_key, "-inf", now_ts - window_seconds)
        pipe.zadd(attempts_key, {attempt_member: now_ts})
        pipe.zcard(attempts_key)
        pipe.expire(attempts_key, ttl_seconds)
        results = pipe.execute()
        failures_count = int(results[2] or 0) if len(results) > 2 else 0
        if failures_count >= max_attempts:
            redis_client.setex(lock_key, lockout_seconds, "1")
    except Exception:
        return


def _clear_login_failures(username: str):
    if not has_request_context():
        return

    redis_client = redis_backend.redis_client
    attempts_key = _rate_limit_attempts_key(username)
    lock_key = _rate_limit_lock_key(username)
    try:
        redis_client.delete(attempts_key, lock_key)
    except Exception:
        return


def reset_login_rate_limit_state():
    redis_client = redis_backend.redis_client
    prefix = f"{_rate_limit_prefix()}:"
    try:
        keys = []
        scan_iter = getattr(redis_client, "scan_iter", None)
        if callable(scan_iter):
            keys = list(scan_iter(f"{prefix}*"))
        else:
            keys_fn = getattr(redis_client, "keys", None)
            if callable(keys_fn):
                keys = list(keys_fn(f"{prefix}*"))
            else:
                all_keys_fn = getattr(redis_client, "_all_keys", None)
                if callable(all_keys_fn):
                    keys = [key for key in all_keys_fn() if str(key).startswith(prefix)]
        if keys:
            redis_client.delete(*keys)
    except Exception:
        return


def hash_password_for_storage(password: str) -> str:
    validated_password = _validate_password_strength(password)
    return password_security.hash_password(validated_password)


def _normalize_referral_code(referral_code):
    if referral_code is None:
        return None
    if not isinstance(referral_code, str):
        raise AuthError("Referral code must be a string", status_code=400)

    normalized = referral_code.strip().upper()
    if not normalized:
        return None
    if not REFERRAL_CODE_ALLOWED_PATTERN.fullmatch(normalized):
        raise AuthError(
            "Referral code must contain 6 to 16 English letters or numbers",
            status_code=400,
        )
    return normalized


def _normalize_device_id(device_id):
    if device_id is None:
        return None
    if not isinstance(device_id, str):
        raise AuthError("device_id must be a string", status_code=400)

    normalized = device_id.strip()
    if not normalized:
        return None
    if len(normalized) > 128:
        raise AuthError("device_id is too long", status_code=400)
    if not DEVICE_ID_ALLOWED_PATTERN.fullmatch(normalized):
        raise AuthError("device_id has invalid format", status_code=400)
    return normalized


def _generate_unique_referral_code(reserved_codes: set[str] | None = None) -> str:
    reserved = reserved_codes or set()

    for _ in range(40):
        candidate = "".join(
            secrets.choice(REFERRAL_CODE_ALPHABET)
            for _ in range(REFERRAL_CODE_DEFAULT_LENGTH)
        )
        if candidate in reserved:
            continue
        if not user_repository.get_by_referral_code(candidate):
            return candidate

    for length in range(9, 17):
        for _ in range(20):
            candidate = "".join(
                secrets.choice(REFERRAL_CODE_ALPHABET)
                for _ in range(length)
            )
            if candidate in reserved:
                continue
            if not user_repository.get_by_referral_code(candidate):
                return candidate

    raise AuthError("Could not generate referral code", status_code=500)


def _resolve_referrer_id_for_new_user(
    username: str,
    referral_code: str | None,
) -> tuple[str | None, int | None]:
    normalized_referral_code = _normalize_referral_code(referral_code)
    if not normalized_referral_code:
        return None, None

    referrer = user_repository.get_by_referral_code(normalized_referral_code)
    if not referrer:
        raise AuthError("Referral code is invalid", status_code=400)
    if referrer.username.strip().lower() == username.strip().lower():
        raise AuthError("You cannot use your own referral code", status_code=400)

    return normalized_referral_code, int(referrer.id)


def ensure_user_referral_codes() -> int:
    from app.models.user_model import User

    users = User.query.order_by(User.id.asc()).all()
    reserved_codes: set[str] = set()
    updated_count = 0

    for user in users:
        normalized = (user.referral_code or "").strip().upper()
        is_valid = bool(
            normalized
            and REFERRAL_CODE_ALLOWED_PATTERN.fullmatch(normalized)
            and normalized not in reserved_codes
        )

        if is_valid:
            if user.referral_code != normalized:
                user.referral_code = normalized
                updated_count += 1
            reserved_codes.add(normalized)
            continue

        generated = _generate_unique_referral_code(reserved_codes=reserved_codes)
        user.referral_code = generated
        reserved_codes.add(generated)
        updated_count += 1

    if updated_count:
        db.session.commit()
    return updated_count


def _normalize_register_fields(username, password, public_key, name):
    if (
        not _require_non_empty_string(username)
        or not _require_non_empty_string(password)
        or not _require_non_empty_string(public_key)
    ):
        raise AuthError("Missing fields", status_code=400)

    username = username.strip()
    if not USERNAME_ALLOWED_PATTERN.fullmatch(username):
        raise AuthError(
            "Username can only contain English letters, numbers, '.', '-', and '_'",
            status_code=400,
        )

    public_key = public_key.strip()
    _validate_public_key_format(public_key)
    validated_password = _validate_password_strength(password)

    resolved_name = username
    if name is not None:
        if not _require_non_empty_string(name):
            raise AuthError("Name must be a non-empty string", status_code=400)
        resolved_name = name.strip()

    return username, validated_password, public_key, resolved_name


def register(
    username,
    password,
    public_key,
    name=None,
    referral_code=None,
    device_id=None,
):
    username, password, public_key, resolved_name = _normalize_register_fields(
        username,
        password,
        public_key,
        name,
    )

    if user_repository.get_by_username(username):
        # Keep legacy behavior for old clients that expect 400 from /register.
        raise AuthError("Username already exists", status_code=400)

    normalized_device_id = _normalize_device_id(device_id)
    normalized_referral_code, referred_by_user_id = _resolve_referrer_id_for_new_user(
        username=username,
        referral_code=referral_code,
    )

    password_hash = password_security.hash_password(password)
    user_repository.create_user(
        username=username,
        password_hash=password_hash,
        public_key=public_key,
        name=resolved_name,
        referral_code=_generate_unique_referral_code(),
        device_id=normalized_device_id,
        referred_by_user_id=referred_by_user_id,
    )


def start_registration(
    username,
    password,
    public_key,
    name=None,
    client_nonce=None,
    referral_code=None,
    device_id=None,
):
    username, password, public_key, resolved_name = _normalize_register_fields(
        username,
        password,
        public_key,
        name,
    )

    if client_nonce is not None:
        if not _require_non_empty_string(client_nonce):
            raise AuthError("client_nonce must be a non-empty string", status_code=400)
        client_nonce = client_nonce.strip()

    _cleanup_expired_pending_registrations()

    if user_repository.get_by_username(username):
        raise AuthError("Username already exists", status_code=409)

    normalized_device_id = _normalize_device_id(device_id)
    normalized_referral_code, _ = _resolve_referrer_id_for_new_user(
        username=username,
        referral_code=referral_code,
    )

    now = datetime.utcnow()
    expires_at = now + timedelta(seconds=PENDING_REGISTRATION_TTL_SECONDS)
    registration_id = secrets.token_urlsafe(24)
    password_hash = password_security.hash_password(password)

    if client_nonce:
        existing_same_nonce = pending_registration_repository.get_by_username_and_client_nonce(
            username, client_nonce
        )
        if existing_same_nonce and not existing_same_nonce.is_expired(now):
            return {
                "registration_id": existing_same_nonce.registration_id,
                "expires_in_seconds": max(
                    1, existing_same_nonce.seconds_until_expiry(now)
                ),
            }

    pending = pending_registration_repository.get_by_username(username)
    if pending:
        pending_registration_repository.update_pending_registration(
            pending=pending,
            registration_id=registration_id,
            password_hash=password_hash,
            public_key=public_key,
            name=resolved_name,
            referral_code=normalized_referral_code,
            device_id=normalized_device_id,
            expires_at=expires_at,
            client_nonce=client_nonce,
        )
    else:
        pending_registration_repository.create_pending_registration(
            registration_id=registration_id,
            username=username,
            password_hash=password_hash,
            public_key=public_key,
            name=resolved_name,
            referral_code=normalized_referral_code,
            device_id=normalized_device_id,
            expires_at=expires_at,
            client_nonce=client_nonce,
        )

    return {
        "registration_id": registration_id,
        "expires_in_seconds": PENDING_REGISTRATION_TTL_SECONDS,
    }


def confirm_registration(registration_id):
    if not _require_non_empty_string(registration_id):
        raise AuthError("Missing registration_id", status_code=400)

    registration_id = registration_id.strip()
    now = datetime.utcnow()
    pending = pending_registration_repository.get_by_registration_id(registration_id)

    if not pending:
        raise AuthError("Registration session not found", status_code=404)

    if pending.is_expired(now):
        pending_registration_repository.delete_pending_registration(pending)
        raise AuthError("Registration session expired", status_code=410)

    if user_repository.get_by_username(pending.username):
        pending_registration_repository.delete_pending_registration(pending)
        raise AuthError("Username already exists", status_code=409)

    referred_by_user_id = None
    normalized_pending_referral_code = (pending.referral_code or "").strip().upper()
    if normalized_pending_referral_code:
        referrer = user_repository.get_by_referral_code(normalized_pending_referral_code)
        if referrer and referrer.username.strip().lower() != pending.username.strip().lower():
            referred_by_user_id = int(referrer.id)

    try:
        user_repository.create_user(
            username=pending.username,
            password_hash=pending.password_hash,
            public_key=pending.public_key,
            name=pending.name,
            referral_code=_generate_unique_referral_code(),
            device_id=_normalize_device_id(pending.device_id),
            referred_by_user_id=referred_by_user_id,
            auto_commit=False,
        )
        pending_registration_repository.delete_pending_registration(pending, commit=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def authenticate_user_credentials(username, password):
    if not _require_non_empty_string(username) or not _require_non_empty_string(password):
        raise AuthError("Invalid username or password", status_code=401)

    username = username.strip()
    _enforce_login_rate_limit(username)

    _cleanup_expired_pending_registrations()

    user = user_repository.get_by_username(username)
    if not user:
        _record_login_failure(username)
        raise AuthError("Invalid username or password", status_code=401)

    if not password_security.verify_and_upgrade_user_password(user, password):
        _record_login_failure(username)
        raise AuthError("Invalid username or password", status_code=401)

    if getattr(user, "is_suspended", False):
        raise AuthError("Account suspended", status_code=403)

    _clear_login_failures(username)
    return user


def login(username, password):
    user = authenticate_user_credentials(username, password)
    return {
        "access_token": create_access_token(identity=user.username),
        "refresh_token": create_refresh_token(identity=user.username)
    }


def refresh_access_token(username):
    user = user_repository.get_by_username(username)
    if not user:
        raise AuthError("Unauthorized", status_code=401)
    if getattr(user, "is_suspended", False):
        raise AuthError("Account suspended", status_code=403)

    return {
        "access_token": create_access_token(identity=username)
    }

def rotate_public_key(username, public_key):
    if not _require_non_empty_string(username):
        raise AuthError("Unauthorized", status_code=401)
    if not _require_non_empty_string(public_key):
        raise AuthError("Missing fields", status_code=400)

    normalized_username = username.strip()
    normalized_public_key = public_key.strip()
    _validate_public_key_format(normalized_public_key)

    user = user_repository.get_by_username(normalized_username)
    if not user:
        raise AuthError("User not found", status_code=404)
    if getattr(user, "is_suspended", False):
        raise AuthError("Account suspended", status_code=403)

    if user.public_key != normalized_public_key:
        updated = user_repository.update_public_key(
            username=normalized_username,
            public_key=normalized_public_key,
        )
        if not updated:
            raise AuthError("User not found", status_code=404)

    recipients = set(follow_repository.get_follower_usernames(user.id))

    groups = group_repository.get_groups_for_user(user.id)
    group_ids = [group.id for group in groups]
    for group in groups:
        recipients.update(group_repository.get_group_member_usernames(group.id))

    recipients.discard(normalized_username)

    return {
        "username": normalized_username,
        "group_ids": group_ids,
        "notify_usernames": sorted(recipients),
    }


def get_key_status(username):
    if not _require_non_empty_string(username):
        raise AuthError("Unauthorized", status_code=401)

    normalized_username = username.strip()
    user = user_repository.get_by_username(normalized_username)
    if not user:
        raise AuthError("User not found", status_code=404)
    if getattr(user, "is_suspended", False):
        raise AuthError("Account suspended", status_code=403)

    return {
        "has_public_key": bool(getattr(user, "public_key", None)),
    }


def update_user_device_id(username, device_id):
    if not _require_non_empty_string(username):
        raise AuthError("Unauthorized", status_code=401)

    normalized_username = username.strip()
    normalized_device_id = _normalize_device_id(device_id)
    if not normalized_device_id:
        raise AuthError("device_id is required", status_code=400)

    user = user_repository.get_by_username(normalized_username)
    if not user:
        raise AuthError("User not found", status_code=404)
    if getattr(user, "is_suspended", False):
        raise AuthError("Account suspended", status_code=403)

    user_repository.update_device_id(
        username=normalized_username,
        device_id=normalized_device_id,
    )
    return {
        "message": "Device ID updated",
    }
