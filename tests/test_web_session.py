import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from app.db import db
from app.routes.auth_routes import auth_bp
from app.routes.web_session import COOKIE_NAME, WebSession


class WebSessionTest(unittest.TestCase):
    origin = "https://secmessage.example"
    path = "/api/auth/web-session"

    def setUp(self):
        app = Flask(__name__)
        app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://")
        db.init_app(app)
        app.register_blueprint(auth_bp, url_prefix="/api/auth")
        self.context = app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        db.create_all()
        self.client = app.test_client()
        env = patch.dict(os.environ, {"WEB_SESSION_ORIGIN": self.origin})
        env.start()
        self.addCleanup(env.stop)

    def sign_in(self, origin=None):
        return self.client.post(
            self.path, base_url=self.origin, headers={"Origin": origin or self.origin},
            json={"username": "mina", "password": "fixture"},
        )

    def test_cookie_is_opaque_and_logout_revokes_session(self):
        user = SimpleNamespace(username="mina", is_suspended=False)
        with patch("app.routes.web_session.auth_service.authenticate_user_credentials", return_value=user), \
                patch("app.routes.web_session.user_repository.get_by_username", return_value=user):
            response = self.sign_in()
            self.assertEqual(response.json, {"username": "mina"})
            attributes = response.headers["Set-Cookie"]
            for attribute in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
                self.assertIn(attribute, attributes)
            self.assertNotIn("Domain=", attributes)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            cookie = self.client.get_cookie(COOKIE_NAME, domain="secmessage.example")
            self.assertNotEqual(WebSession.query.one().digest, cookie.value)
            self.assertEqual(self.client.get(self.path, base_url=self.origin).json["username"], "mina")
            self.client.delete(self.path, base_url=self.origin, headers={"Origin": self.origin})
            self.assertEqual(WebSession.query.count(), 0)

    def test_rejects_cross_origin_and_invalid_credentials(self):
        self.assertEqual(self.sign_in("https://other.example").status_code, 403)
        with patch("app.routes.web_session.auth_service.authenticate_user_credentials", side_effect=ValueError("Invalid credentials")):
            self.assertEqual(self.sign_in().status_code, 401)

    def test_expired_session_does_not_restore(self):
        with patch("app.routes.web_session.auth_service.authenticate_user_credentials", return_value=SimpleNamespace(username="mina")):
            self.assertEqual(self.sign_in().status_code, 200)
        WebSession.query.one().expires_at = int(time.time()) - 1
        db.session.commit()
        self.assertIsNone(self.client.get(self.path, base_url=self.origin).json["username"])


    def test_account_keys_require_session_origin_and_password(self):
        path = self.path + "/keys"
        unauthenticated = self.client.get(path, base_url=self.origin)
        self.assertEqual(unauthenticated.status_code, 401)
        user = SimpleNamespace(username="mina", is_suspended=False)
        with patch("app.routes.web_session.auth_service.authenticate_user_credentials", return_value=user):
            self.assertEqual(self.sign_in().status_code, 200)
        with patch("app.routes.web_session.user_repository.get_by_username", return_value=user), \
                patch("app.routes.auth_routes.auth_service.get_key_status", return_value={"username": "mina", "public_key": "current"}), \
                patch("app.routes.auth_routes.auth_service.rotate_public_key", return_value={"username": "mina", "group_ids": [], "notify_usernames": []}) as rotate, \
                patch("app.routes.auth_routes.auth_service.authenticate_user_credentials") as authenticate:
            read = self.client.get(path, base_url=self.origin)
            self.assertEqual(read.json["public_key"], "current")
            self.assertEqual(read.headers["Cache-Control"], "no-store")
            rejected = self.client.post(path, base_url=self.origin, headers={"Origin": "https://other.example"}, json={})
            self.assertEqual(rejected.status_code, 403)
            authenticate.side_effect = ValueError("Invalid credentials")
            rejected = self.client.post(path, base_url=self.origin, headers={"Origin": self.origin}, json={"password": "wrong"})
            self.assertEqual(rejected.status_code, 401)
            rotate.assert_not_called()
            authenticate.side_effect = None
            accepted = self.client.post(path, base_url=self.origin, headers={"Origin": self.origin}, json={"password": "fixture", "public_key": "replacement"})
            self.assertEqual(accepted.status_code, 200)
            rotate.assert_called_once_with(username="mina", public_key="replacement")
