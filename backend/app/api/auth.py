from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.auth import (
    SESSION_COOKIE_NAME,
    app_token_is_valid,
    require_auth,
    session_cookie_value,
)
from app.config import settings
from app.schemas import LoginIn, LoginOut

router = APIRouter()


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn, response: Response):
    if not app_token_is_valid(payload.token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_cookie_value(),
        max_age=settings.auth_cookie_max_age_seconds,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )
    return LoginOut()


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_auth)],
)
def logout(response: Response):
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="strict",
    )
