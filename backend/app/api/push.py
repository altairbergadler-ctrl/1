"""User-scoped standards-based Web Push subscription API."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_auth, require_csrf
from app.db import get_db
from app.models import PushSubscription, User
from app.schemas import (
    WebPushStatusOut,
    WebPushSubscriptionIn,
    WebPushUnsubscribeIn,
    WebPushSubscriptionOut,
)
from app.services.web_push import (
    WebPushError,
    delete_subscription,
    endpoint_hash,
    save_subscription,
    vapid_public_key,
    web_push_configured,
)

router = APIRouter(dependencies=[Depends(require_auth)])


def _serialize(subscription: PushSubscription) -> dict:
    return {
        "id": subscription.id,
        "created_at": subscription.created_at,
        "last_success_at": subscription.last_success_at,
        "failure_count": int(subscription.failure_count or 0),
    }


@router.get("", response_model=WebPushStatusOut)
def status_payload(
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    configured = web_push_configured()
    subscriptions = list(
        db.scalars(
            select(PushSubscription)
            .where(
                PushSubscription.user_id == current_user.id,
                PushSubscription.revoked_at.is_(None),
            )
            .order_by(PushSubscription.created_at, PushSubscription.id)
        )
    )
    return {
        "enabled": configured,
        "configured": configured,
        "public_key": vapid_public_key() if configured else None,
        "subscriptions": [_serialize(item) for item in subscriptions],
    }


@router.post(
    "/subscriptions",
    response_model=WebPushSubscriptionOut,
    status_code=status.HTTP_201_CREATED,
)
def subscribe(
    payload: WebPushSubscriptionIn,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    if not web_push_configured():
        raise HTTPException(status_code=503, detail="Web Push is unavailable")
    try:
        subscription = save_subscription(db, current_user, payload)
        db.commit()
        db.refresh(subscription)
        return _serialize(subscription)
    except WebPushError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Web Push subscription rejected") from exc


@router.post("/subscriptions/unsubscribe", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe_current_device(
    payload: WebPushUnsubscribeIn,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    try:
        digest = endpoint_hash(payload.endpoint)
    except WebPushError as exc:
        raise HTTPException(status_code=400, detail="Invalid Web Push endpoint") from exc
    subscription = db.scalar(
        select(PushSubscription).where(
            PushSubscription.endpoint_hash == digest,
            PushSubscription.user_id == current_user.id,
        )
    )
    if subscription is not None:
        delete_subscription(db, subscription)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/subscriptions/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe(
    subscription_id: str,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    subscription = db.scalar(
        select(PushSubscription).where(
            PushSubscription.id == subscription_id,
            PushSubscription.user_id == current_user.id,
        )
    )
    if subscription is None:
        raise HTTPException(status_code=404, detail="Web Push subscription not found")
    delete_subscription(db, subscription)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
