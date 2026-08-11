from __future__ import annotations

from sqlalchemy import select

from app.models import User, UserRole, UserState, utcnow


def ensure_user(
    db,
    *,
    email: str = "owner@example.test",
    role: UserRole = UserRole.owner,
    bootstrap: bool = True,
) -> User:
    email_key = email.casefold()
    user = db.scalar(select(User).where(User.email_key == email_key))
    if user is None:
        now = utcnow()
        user = User(
            email=email,
            email_key=email_key,
            role=role,
            state=UserState.active,
            is_bootstrap_owner=bootstrap,
            created_at=now,
            activated_at=now,
        )
        db.add(user)
        db.flush()
    return user
