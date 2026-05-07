from app.db import db
from app.models.banned_device_model import BannedDevice


def get_by_device_id(device_id: str):
    return BannedDevice.query.filter_by(device_id=device_id).first()


def block_device(
    device_id: str,
    *,
    blocked_user_id: int | None = None,
    blocked_by_report_id: int | None = None,
    reason: str | None = None,
    auto_commit: bool = True,
):
    blocked_device = get_by_device_id(device_id)
    if blocked_device:
        if blocked_user_id and not blocked_device.blocked_user_id:
            blocked_device.blocked_user_id = blocked_user_id
        if blocked_by_report_id and not blocked_device.blocked_by_report_id:
            blocked_device.blocked_by_report_id = blocked_by_report_id
        if reason and not blocked_device.reason:
            blocked_device.reason = reason
    else:
        blocked_device = BannedDevice(
            device_id=device_id,
            blocked_user_id=blocked_user_id,
            blocked_by_report_id=blocked_by_report_id,
            reason=reason,
        )
        db.session.add(blocked_device)

    if auto_commit:
        db.session.commit()
    return blocked_device
