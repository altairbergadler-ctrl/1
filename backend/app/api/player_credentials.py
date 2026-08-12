from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_auth, require_csrf
from app.config import settings
from app.db import get_db
from app.models import PlayerCredential, User
from app.schemas import (
    PlayerCredentialCreatedOut,
    PlayerCredentialCreateIn,
    PlayerCredentialListOut,
    PlayerCredentialOut,
)
from app.services.player_credentials import (
    PlayerCredentialError,
    create_player_credential,
    revoke_all_player_credentials,
    revoke_player_credential,
)

router = APIRouter()


def _out(record: PlayerCredential) -> PlayerCredentialOut:
    return PlayerCredentialOut(
        id=record.id,
        label=record.label,
        auth_scheme=record.auth_scheme,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        expires_at=record.expires_at,
        revoked_at=record.revoked_at,
    )


@router.get("", response_model=PlayerCredentialListOut)
def list_player_credentials(
    current_user: User = Depends(require_auth), db: Session = Depends(get_db)
):
    rows = db.scalars(
        select(PlayerCredential)
        .where(PlayerCredential.user_id == current_user.id)
        .order_by(PlayerCredential.created_at.desc(), PlayerCredential.id)
    ).all()
    return {"items": [_out(row) for row in rows]}


@router.post("", response_model=PlayerCredentialCreatedOut, status_code=201)
def create_player_key(
    payload: PlayerCredentialCreateIn,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    try:
        raw, record = create_player_credential(db, current_user.id, payload.label)
        db.commit()
        db.refresh(record)
    except PlayerCredentialError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail="Credential label is invalid") from exc
    return PlayerCredentialCreatedOut(
        **_out(record).model_dump(), server_url=settings.public_origin, api_key=raw
    )


@router.post("/{credential_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke_player_key(
    credential_id: str,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    if not revoke_player_credential(db, credential_id, current_user.id):
        db.rollback()
        raise HTTPException(status_code=404, detail="Credential not found")
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/revoke-all", status_code=status.HTTP_204_NO_CONTENT)
def revoke_all_player_keys(
    current_user: User = Depends(require_csrf), db: Session = Depends(get_db)
):
    revoke_all_player_credentials(db, current_user.id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
