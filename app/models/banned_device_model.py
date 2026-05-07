from datetime import datetime

from app.db import db


class BannedDevice(db.Model):
    __tablename__ = "banned_devices"

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(128), nullable=False, unique=True, index=True)
    blocked_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    blocked_user_id = db.Column(db.Integer, nullable=True, index=True)
    blocked_by_report_id = db.Column(db.Integer, nullable=True, index=True)
    reason = db.Column(db.String(64), nullable=True)
