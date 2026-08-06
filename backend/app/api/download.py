from fastapi import APIRouter, Depends

from app.auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/track/{item_id}")
def download_track(item_id: int):
    """TODO (Этап 4): отдать файл bit-perfect с поддержкой Range."""
    return {"todo": f"Этап 4: отдача файла для playlist_item {item_id}"}
