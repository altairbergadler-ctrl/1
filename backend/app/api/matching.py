from fastapi import APIRouter, Depends

from app.auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/run")
def run_matching():
    """TODO (Этап 4): запустить matcher.py (каскад ISRC -> exact -> fuzzy)."""
    return {"todo": "Этап 4: Celery job матчинга"}


@router.get("/review")
def review_queue():
    """TODO (Этап 4): элементы NEEDS_REVIEW с кандидатами."""
    return {"items": []}
