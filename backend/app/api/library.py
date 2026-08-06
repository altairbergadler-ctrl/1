from fastapi import APIRouter, Depends

from app.auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/scan")
def scan():
    """TODO (Этап 2): запустить scanner.py как Celery-задачу."""
    return {"todo": "Этап 2: Celery job сканирования библиотеки"}


@router.get("/stats")
def stats():
    """TODO (Этап 2): файлов/треков/альбомов, объём, форматы."""
    return {"files": 0, "tracks": 0, "albums": 0, "bytes": 0}
