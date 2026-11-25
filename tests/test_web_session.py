import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from app.db import db
from app.routes.web_session import COOKIE_NAME, WebSession, handle_web_session


class WebSessionTest(unittest.TestCase):
    origin = "https://secmessage.example"
    path = "/api/auth/web-session"

    def setUp(self):
        app = Flask(__name__)
        app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://")
        db.init_app(app)
        app.add_url_rule(self.path, view_func=handle_web_session, methods=["GET", "POST", "DELETE"])
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
