from fastapi import APIRouter, Depends

from app.auth import require_auth
from app.db import get_db

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("")
def list_playlists(db=Depends(get_db)):
    """TODO (Этап 3): вернуть плейлисты со сводкой статусов матчинга."""
    return {"items": [], "todo": "Этап 3: импорт + сводка статусов"}


@router.post("/import")
def import_playlists():
    """TODO (Этап 3): запустить Celery-задачу импорта из источника."""
    return {"todo": "Этап 3: Celery job импорта плейлистов"}
