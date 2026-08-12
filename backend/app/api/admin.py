"""Owner-only role and session administration."""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_owner, require_owner_csrf
from app.db import get_db
from app.models import User, UserRole, UserSession, UserState, utcnow
from app.schemas import UserAdminListOut, UserAdminOut, UserRoleUpdateIn
from app.services.authentication import (
    lock_identity_mutation,
    revoke_user_sessions,
)
from app.services.player_credentials import revoke_all_player_credentials

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
        "is_bootstrap_owner": user.is_bootstrap_owner,
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


@router.patch("/users/{user_id}/role", response_model=UserAdminOut)
def update_user_role(
    user_id: int,
    payload: UserRoleUpdateIn,
    response: Response,
    owner: User = Depends(require_owner_csrf),
    db: Session = Depends(get_db),
):
    _no_store(response)
    lock_identity_mutation(db)
    target = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    requested_role = UserRole(payload.role)
    if target.id == owner.id:
        raise HTTPException(status_code=409, detail="The current owner cannot change their role")
    if target.state != UserState.active:
        raise HTTPException(status_code=409, detail="Only active users can change role")
    if target.is_bootstrap_owner and requested_role != UserRole.owner:
        raise HTTPException(status_code=409, detail="The bootstrap owner cannot be demoted")
    if target.role == UserRole.owner and requested_role == UserRole.user:
        other_owners = db.scalar(
            select(func.count(User.id)).where(
                User.role == UserRole.owner,
                User.state == UserState.active,
                User.id != target.id,
            )
        )
        if not other_owners:
            raise HTTPException(status_code=409, detail="The last active owner cannot be demoted")
    if target.role != requested_role:
        target.role = requested_role
        # Force the next request to reload both the server-side role and the PWA
        # navigation instead of leaving a privilege change in an old session.
        revoke_user_sessions(db, target.id)
    db.commit()
    db.refresh(target)
    return _user_out(db, target)


@router.post("/users/{user_id}/disable", response_model=UserAdminOut)
def disable_user(
    user_id: int,
    response: Response,
    owner: User = Depends(require_owner_csrf),
    db: Session = Depends(get_db),
):
    _no_store(response)
    lock_identity_mutation(db)
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
    revoke_all_player_credentials(db, target.id)
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
