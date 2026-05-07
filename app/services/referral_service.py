from app.constants.badges import DAILY_WINNER_BADGE
from app.db import db
from app.models.user_model import User

REFERRAL_VERIFIED_BADGE = "verified"
REFERRAL_VERIFIED_REQUIRED_INVITES = 5


def count_valid_referral_joins(user: User) -> int:
    inviter_device_id = (user.device_id or "").strip()
    joined_query = db.session.query(User.id).filter(
        User.referred_by_user_id == user.id,
        User.is_suspended.is_(False),
    )
    joined_query = joined_query.filter(
        User.device_id.isnot(None),
        User.device_id != "",
    )
    if inviter_device_id:
        joined_query = joined_query.filter(User.device_id != inviter_device_id)
    return joined_query.count()


def maybe_grant_verified_badge_for_referrals(
    referrer_user_id: int,
    *,
    precomputed_joined_count: int | None = None,
    auto_commit: bool = True,
) -> dict:
    user = db.session.get(User, referrer_user_id)
    if not user or getattr(user, "is_suspended", False):
        return {"assigned": False, "joined_count": 0, "is_verified": False}

    joined_count = (
        precomputed_joined_count
        if precomputed_joined_count is not None
        else count_valid_referral_joins(user)
    )
    current_badge = (user.badge or "").strip().lower()

    if current_badge == REFERRAL_VERIFIED_BADGE:
        return {"assigned": False, "joined_count": joined_count, "is_verified": True}

    if joined_count < REFERRAL_VERIFIED_REQUIRED_INVITES:
        return {"assigned": False, "joined_count": joined_count, "is_verified": False}

    # Preserve manually assigned staff/moderator badges while still allowing
    # verified to replace temporary "post_of_the_day".
    if current_badge and current_badge != DAILY_WINNER_BADGE:
        return {"assigned": False, "joined_count": joined_count, "is_verified": False}

    user.badge = REFERRAL_VERIFIED_BADGE
    if auto_commit:
        db.session.commit()

    return {"assigned": True, "joined_count": joined_count, "is_verified": True}
