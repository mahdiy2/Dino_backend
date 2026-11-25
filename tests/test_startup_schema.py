import os
import tempfile
import unittest

from flask import Flask
from sqlalchemy import text


class TestStartupSchema(unittest.TestCase):
    def _make_sqlite_app(self, db_path):
        from app.db import db

        app = Flask(__name__)
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
        app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        db.init_app(app)
        return app, db

    def test_post_edit_schema_adds_edited_at_to_legacy_sqlite_table(self):
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)

        try:
            from app import _ensure_post_edit_schema

            app, db = self._make_sqlite_app(db_path)

            with app.app_context():
                db.session.execute(
                    text(
                        "CREATE TABLE posts ("
                        "id INTEGER PRIMARY KEY, "
                        "author_id INTEGER NOT NULL, "
                        "text TEXT NOT NULL, "
                        "created_at TIMESTAMP"
                        ")"
                    )
                )
                db.session.commit()

                _ensure_post_edit_schema()

                columns = {
                    row[1]
                    for row in db.session.execute(
                        text("PRAGMA table_info(posts)")
                    )
                }
                indexes = {
                    row[1]
                    for row in db.session.execute(
                        text("PRAGMA index_list(posts)")
                    )
                }
                self.assertIn("edited_at", columns)
                self.assertIn("ix_posts_edited_at", indexes)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_crash_schema_creates_event_ids_for_legacy_sqlite_table(self):
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)

        try:
            from app import _ensure_crash_log_schema

            app, db = self._make_sqlite_app(db_path)

            with app.app_context():
                db.session.execute(
                    text(
                        "CREATE TABLE crash_logs ("
                        "id INTEGER PRIMARY KEY, "
                        "event_id VARCHAR(64) NOT NULL UNIQUE, "
                        "platform VARCHAR(16) NOT NULL, "
                        "app_version VARCHAR(64) NOT NULL, "
                        "exception_type VARCHAR(255) NOT NULL, "
                        "stack_trace TEXT NOT NULL, "
                        "occurred_at TIMESTAMP NOT NULL, "
                        "received_at TIMESTAMP NOT NULL"
                        ")"
                    )
                )
                db.session.commit()

                _ensure_crash_log_schema()

                table_names = {
                    row[0]
                    for row in db.session.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type = 'table'"
                        )
                    )
                }
                event_columns = {
                    row[1]
                    for row in db.session.execute(
                        text("PRAGMA table_info(crash_event_ids)")
                    )
                }
                event_indexes = {
                    row[1]
                    for row in db.session.execute(
                        text("PRAGMA index_list(crash_event_ids)")
                    )
                }
                self.assertIn("crash_event_ids", table_names)
                self.assertIn("event_id", event_columns)
                self.assertIn("crash_log_id", event_columns)
                self.assertIn("ix_crash_event_ids_event_id", event_indexes)
                self.assertIn("ix_crash_event_ids_crash_log_id", event_indexes)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)


if __name__ == "__main__":
    unittest.main()
