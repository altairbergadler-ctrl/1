from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

bearer = HTTPBearer(auto_error=False)


def require_auth(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    """MVP-заглушка: один статический токен из .env (APP_AUTH_TOKEN).
    Релиз 2: заменить на JWT с истечением."""
    if not creds or creds.credentials != settings.app_auth_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid token")
    return True
