import hashlib
import os
import secrets
import time
from urllib.parse import urlsplit

from flask import jsonify, request

from app.db import db
from app.repositories import user_repository
from app.services import auth_service

COOKIE_NAME = "__Host-secmessage-session"
SESSION_SECONDS = 24 * 60 * 60


class WebSession(db.Model):
    __tablename__ = "web_sessions"

    digest = db.Column(db.String(64), primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    expires_at = db.Column(db.BigInteger, nullable=False, index=True)


def _settings():
    origin = os.environ.get("WEB_SESSION_ORIGIN", "")
    parsed = urlsplit(origin)
    valid = (
        parsed.hostname
        and parsed.netloc == parsed.netloc.rsplit("@", 1)[-1]
        and not (parsed.path or parsed.query or parsed.fragment)
    )
    if valid and parsed.scheme == "https":
        return origin, COOKIE_NAME, True
    if (
        valid
        and parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        and os.environ.get("WEB_SESSION_LOCAL_HTTP") == "1"
    ):
        return origin, "secmessage-dev-session", False
    return None


def _digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _reply(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def browser_identity(require_origin=False):
    settings = _settings()
    if settings is None:
        return None, _reply({"error": "Browser sessions are unavailable"}, 503)
    origin, cookie_name, _ = settings
    if require_origin and request.headers.get("Origin") != origin:
        return None, _reply({"error": "Request origin rejected"}, 403)
    token = request.cookies.get(cookie_name)
    session = db.session.get(WebSession, _digest(token)) if token else None
    if session and session.expires_at > int(time.time()):
        user = user_repository.get_by_username(session.username)
        if user and not getattr(user, "is_suspended", False):
            return user.username, None
    return None, _reply({"error": "Sign-in required"}, 401)


def handle_web_session():
    settings = _settings()
    if settings is None:
        return _reply({"error": "Browser sessions are unavailable"}, 503)
    origin, cookie_name, secure = settings
    token = request.cookies.get(cookie_name)
    existing = db.session.get(WebSession, _digest(token)) if token else None

    if request.method == "GET":
        if existing and existing.expires_at > int(time.time()):
            user = user_repository.get_by_username(existing.username)
            if user and not getattr(user, "is_suspended", False):
                return _reply({"username": user.username})
        return _reply({"username": None})

    if request.headers.get("Origin") != origin:
        return _reply({"error": "Request origin rejected"}, 403)

    if request.method == "DELETE":
        if existing:
            db.session.delete(existing)
            db.session.commit()
        response = _reply({"username": None})
        response.delete_cookie(cookie_name, path="/", secure=secure, httponly=True, samesite="Lax")
        return response

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _reply({"error": "Invalid JSON body"}, 400)
    try:
        user = auth_service.authenticate_user_credentials(data.get("username"), data.get("password"))
    except ValueError as error:
        return _reply({"error": str(error)}, getattr(error, "status_code", 401))

    now = int(time.time())
    if existing:
        db.session.delete(existing)
    db.session.query(WebSession).filter(WebSession.expires_at <= now).delete()
    identifier = secrets.token_urlsafe(32)
    db.session.add(WebSession(digest=_digest(identifier), username=user.username, expires_at=now + SESSION_SECONDS))
    db.session.commit()
    response = _reply({"username": user.username})
    response.set_cookie(
        cookie_name, identifier, max_age=SESSION_SECONDS,
        path="/", secure=secure, httponly=True, samesite="Lax",
    )
    return response
