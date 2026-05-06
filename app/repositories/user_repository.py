from app.models.user_model import User
from app.db import db
from app.repositories.profile_repository import create_profile_for_user

def get_by_username(username: str):
    return User.query.filter_by(username=username).first()


def get_by_referral_code(referral_code: str):
    return User.query.filter_by(referral_code=referral_code).first()


def update_public_key(username: str, public_key: str) -> bool:
    user = get_by_username(username)
    if not user:
        return False

    user.public_key = public_key
    db.session.commit()
    return True


def create_user(
    username,
    password_hash,
    public_key,
    name=None,
    referral_code=None,
    device_id=None,
    referred_by_user_id=None,
    *,
    auto_commit=True,
):
    user = User(
        username=username,
        password_hash=password_hash,
        public_key=public_key,
        referral_code=referral_code,
        device_id=device_id,
        referred_by_user_id=referred_by_user_id,
    )
    db.session.add(user)
    db.session.flush()

    create_profile_for_user(
        user_id=user.id,
        name=(name or username).strip(),
    )

    if auto_commit:
        db.session.commit()
    return user


def update_device_id(username: str, device_id: str | None) -> bool:
    user = get_by_username(username)
    if not user:
        return False

    user.device_id = device_id
    db.session.commit()
    return True
