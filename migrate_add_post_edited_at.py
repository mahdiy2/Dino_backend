"""
One-time migration: add edited_at timestamp to posts.

Run from project root:
    source venv/bin/activate
    python migrate_add_post_edited_at.py
"""

from sqlalchemy import inspect, text

from app import create_app
from app.db import db


def migrate():
    app = create_app()
    with app.app_context():
        inspector = inspect(db.engine)
        if not inspector.has_table("posts"):
            print("[!] posts table not found. Nothing to migrate.")
            return

        columns = {column["name"] for column in inspector.get_columns("posts")}
        changed = False

        if "edited_at" not in columns:
            db.session.execute(
                text(
                    "ALTER TABLE posts "
                    "ADD COLUMN edited_at TIMESTAMP NULL"
                )
            )
            changed = True

        db.session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_posts_edited_at "
                "ON posts (edited_at)"
            )
        )
        db.session.commit()

        if changed:
            print("[✓] Added posts.edited_at column and index.")
        else:
            print("[✓] edited_at column already exists. Index ensured.")


if __name__ == "__main__":
    migrate()
