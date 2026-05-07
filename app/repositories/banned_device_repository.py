from app.db import db
from app.models.banned_device_model import BannedDevice
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


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
    normalized_device_id = (device_id or "").strip()
    if not normalized_device_id:
        raise ValueError("device_id is required")

    values = {
        "device_id": normalized_device_id,
        "blocked_user_id": blocked_user_id,
        "blocked_by_report_id": blocked_by_report_id,
        "reason": reason,
    }
    dialect_name = db.session.get_bind().dialect.name

    if dialect_name == "postgresql":
        insert_stmt = pg_insert(BannedDevice).values(**values)
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=[BannedDevice.device_id],
            set_={
                "blocked_user_id": func.coalesce(
                    BannedDevice.blocked_user_id,
                    insert_stmt.excluded.blocked_user_id,
                ),
                "blocked_by_report_id": func.coalesce(
                    BannedDevice.blocked_by_report_id,
                    insert_stmt.excluded.blocked_by_report_id,
                ),
                "reason": func.coalesce(
                    BannedDevice.reason,
                    insert_stmt.excluded.reason,
                ),
            },
        )
        db.session.execute(upsert_stmt)
    elif dialect_name == "sqlite":
        insert_stmt = sqlite_insert(BannedDevice).values(**values)
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=[BannedDevice.device_id],
            set_={
                "blocked_user_id": func.coalesce(
                    BannedDevice.blocked_user_id,
                    insert_stmt.excluded.blocked_user_id,
                ),
                "blocked_by_report_id": func.coalesce(
                    BannedDevice.blocked_by_report_id,
                    insert_stmt.excluded.blocked_by_report_id,
                ),
                "reason": func.coalesce(
                    BannedDevice.reason,
                    insert_stmt.excluded.reason,
                ),
            },
        )
        db.session.execute(upsert_stmt)
    else:
        blocked_device = get_by_device_id(normalized_device_id)
        if blocked_device:
            if blocked_user_id and not blocked_device.blocked_user_id:
                blocked_device.blocked_user_id = blocked_user_id
            if blocked_by_report_id and not blocked_device.blocked_by_report_id:
                blocked_device.blocked_by_report_id = blocked_by_report_id
            if reason and not blocked_device.reason:
                blocked_device.reason = reason
        else:
            blocked_device = BannedDevice(**values)
            db.session.add(blocked_device)

    if auto_commit:
        db.session.commit()
    else:
        db.session.flush()
    return get_by_device_id(normalized_device_id)
