import threading
from datetime import datetime, timedelta, timezone

from app.extensions.redis_client import redis_client

PUBLIC_ROOM_MESSAGE_TTL_SECONDS = 30 * 60
_EXPIRY_LOCK_PREFIX = "public_room:expiry:lock:"
_scheduled_timers_lock = threading.Lock()
_scheduled_timers = {}


def _parse_iso_datetime(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_iso_datetime(value):
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    normalized = normalized.astimezone(timezone.utc)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.") + f"{normalized.microsecond:06d}Z"


def _cancel_timer(message_id):
    with _scheduled_timers_lock:
        timer = _scheduled_timers.pop(message_id, None)
    if timer is not None:
        timer.cancel()


def _claim_expiry_execution(message_id):
    lock_key = f"{_EXPIRY_LOCK_PREFIX}{message_id}"
    try:
        return bool(redis_client.set(lock_key, "1", nx=True, ex=120))
    except Exception:
        # If the distributed lock is unavailable, still attempt local execution.
        return True


def schedule_public_room_message_expiration(
    *,
    app,
    group_id: int,
    message_id: str,
    expires_at: datetime,
):
    if app is None or not message_id:
        return

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    expires_at = expires_at.astimezone(timezone.utc)

    _cancel_timer(message_id)

    delay_seconds = max(
        0.0,
        (expires_at - datetime.now(timezone.utc)).total_seconds(),
    )

    def _callback():
        try:
            with app.app_context():
                expire_public_room_message(
                    group_id=group_id,
                    message_id=message_id,
                )
        finally:
            with _scheduled_timers_lock:
                _scheduled_timers.pop(message_id, None)

    timer = threading.Timer(delay_seconds, _callback)
    timer.daemon = True
    with _scheduled_timers_lock:
        _scheduled_timers[message_id] = timer
    timer.start()


def schedule_public_room_message_expiration_from_payload(app, *, group_id: int, payload: dict):
    if not isinstance(payload, dict):
        return

    message_id = (payload.get("message_id") or "").strip()
    if not message_id:
        return

    expires_at = _parse_iso_datetime(payload.get("expires_at"))
    if expires_at is None:
        timestamp = _parse_iso_datetime(payload.get("timestamp"))
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        expires_at = timestamp + timedelta(seconds=PUBLIC_ROOM_MESSAGE_TTL_SECONDS)
        payload["expires_at"] = _format_iso_datetime(expires_at)

    schedule_public_room_message_expiration(
        app=app,
        group_id=int(group_id),
        message_id=message_id,
        expires_at=expires_at,
    )


def expire_public_room_message(*, group_id: int, message_id: str):
    if not message_id:
        return False
    if not _claim_expiry_execution(message_id):
        return False

    from app.db import db
    from app.extensions.extensions import socketio
    from app.models.chat_message_model import GroupMessage
    from app.repositories import group_repository
    from app.services import message_service

    try:
        normalized_group_id = int(group_id)
    except (TypeError, ValueError):
        return False
    if normalized_group_id <= 0:
        return False
    if not group_repository.is_public_room(normalized_group_id):
        return False

    row = (
        GroupMessage.query.filter_by(
            group_id=normalized_group_id,
            message_id=message_id,
        )
        .first()
    )
    if row is None:
        return False
    if row.deleted_for_everyone:
        return False

    now_utc = datetime.now(timezone.utc)
    expires_at = row.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > now_utc:
            return False

    row.deleted_for_everyone = True
    row.deleted_at = now_utc.replace(tzinfo=None)
    db.session.commit()

    payload = {
        "group_id": normalized_group_id,
        "message_id": message_id,
        "deleted_by": "system",
        "deleted_at": _format_iso_datetime(now_utc),
        "reason": "expired",
    }
    socketio.emit("group_message_deleted", payload, room=f"group_{normalized_group_id}")

    member_usernames = group_repository.get_group_member_usernames(normalized_group_id)
    for member_username in member_usernames:
        message_service.mark_group_message_deleted(
            username=member_username,
            group_id=normalized_group_id,
            message_id=message_id,
        )
        message_service.queue_message_deletion_event(
            member_username,
            "group_message_deleted",
            payload,
        )

    message_service.delete_message_metadata(message_id)
    return True


def bootstrap_public_room_message_expirations(app):
    from app.models.chat_message_model import GroupMessage
    from app.models.group_model import Group

    public_group_ids = [
        row[0]
        for row in (
            Group.query.with_entities(Group.id)
            .filter(Group.is_public_room.is_(True))
            .all()
        )
    ]
    if not public_group_ids:
        return

    rows = (
        GroupMessage.query
        .with_entities(
            GroupMessage.group_id,
            GroupMessage.message_id,
            GroupMessage.timestamp,
            GroupMessage.expires_at,
        )
        .filter(
            GroupMessage.group_id.in_(public_group_ids),
            GroupMessage.deleted_for_everyone.is_(False),
        )
        .all()
    )
    now_utc = datetime.now(timezone.utc)
    for row in rows:
        expires_at = row.expires_at
        if expires_at is None:
            ts = row.timestamp
            if ts is None:
                ts = now_utc.replace(tzinfo=None)
            if ts.tzinfo is None:
                expires_at = ts.replace(tzinfo=timezone.utc) + timedelta(
                    seconds=PUBLIC_ROOM_MESSAGE_TTL_SECONDS,
                )
            else:
                expires_at = ts.astimezone(timezone.utc) + timedelta(
                    seconds=PUBLIC_ROOM_MESSAGE_TTL_SECONDS,
                )
        else:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at <= now_utc:
            expire_public_room_message(
                group_id=row.group_id,
                message_id=row.message_id,
            )
            continue

        schedule_public_room_message_expiration(
            app=app,
            group_id=row.group_id,
            message_id=row.message_id,
            expires_at=expires_at,
        )
