"""Owner-only user invitation and session administration."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_owner, require_owner_csrf
from app.db import get_db
from app.models import User, UserRole, UserSession, UserState, utcnow
from app.schemas import UserAdminListOut, UserAdminOut, UserInviteIn
from app.services.authentication import (
    AuthenticationError,
    lock_identity_mutation,
    normalize_email,
    revoke_user_sessions,
)

router = APIRouter()


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _user_out(db: Session, user: User) -> dict:
    # Keep identity internals (google_sub and session hashes) out of the admin
    # response; operators only need lifecycle state and an aggregate count.
    active_sessions = db.scalar(
        select(func.count(UserSession.id)).where(
            UserSession.user_id == user.id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > utcnow(),
        )
    )
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role.value,
        "state": user.state.value,
        "created_at": user.created_at,
        "activated_at": user.activated_at,
        "last_login_at": user.last_login_at,
        "active_sessions": int(active_sessions or 0),
    }


@router.get("/users", response_model=UserAdminListOut)
def list_users(
    response: Response,
    _: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    _no_store(response)
    users = db.scalars(select(User).order_by(User.created_at, User.id)).all()
    return {"items": [_user_out(db, user) for user in users]}


@router.post("/users", response_model=UserAdminOut, status_code=status.HTTP_201_CREATED)
def invite_user(
    payload: UserInviteIn,
    response: Response,
    _: User = Depends(require_owner_csrf),
    db: Session = Depends(get_db),
):
    _no_store(response)
    try:
        email, email_key = normalize_email(payload.email)
    except AuthenticationError as exc:
        raise HTTPException(status_code=422, detail="Email address is invalid") from exc
    lock_identity_mutation(db)
    if db.scalar(select(User.id).where(User.email_key == email_key)) is not None:
        raise HTTPException(status_code=409, detail="Invitation already exists")
    user = User(
        email=email,
        email_key=email_key,
        role=UserRole(payload.role),
        state=UserState.pending,
        is_bootstrap_owner=False,
        created_at=utcnow(),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Invitation already exists") from exc
    db.refresh(user)
    return _user_out(db, user)


@router.post("/users/{user_id}/disable", response_model=UserAdminOut)
def disable_user(
    user_id: int,
    response: Response,
    owner: User = Depends(require_owner_csrf),
    db: Session = Depends(get_db),
):
    _no_store(response)
    target = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == owner.id:
        raise HTTPException(status_code=409, detail="The current owner cannot be disabled")
    # Preserve at least one active owner so a routine admin action cannot remove
    # the only account capable of recovering or administering the service.
    if target.role == UserRole.owner and target.state == UserState.active:
        other_owners = db.scalar(
            select(func.count(User.id)).where(
                User.role == UserRole.owner,
                User.state == UserState.active,
                User.id != target.id,
            )
        )
        if not other_owners:
            raise HTTPException(status_code=409, detail="The last active owner cannot be disabled")
    target.state = UserState.disabled
    revoke_user_sessions(db, target.id)
    db.commit()
    db.refresh(target)
    return _user_out(db, target)


@router.post("/users/{user_id}/sessions/revoke", response_model=UserAdminOut)
def revoke_sessions(
    user_id: int,
    response: Response,
    _: User = Depends(require_owner_csrf),
    db: Session = Depends(get_db),
):
    _no_store(response)
    target = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    revoke_user_sessions(db, target.id)
    db.commit()
    db.refresh(target)
    return _user_out(db, target)
