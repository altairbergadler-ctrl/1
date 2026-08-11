from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_db
from app.models import Job, JobScope, User, UserRole
from app.schemas import JobOut

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/{job_id}", response_model=JobOut)
def get_job(
    job_id: int,
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    access = [Job.user_id == current_user.id]
    if current_user.role == UserRole.owner:
        access.append(and_(Job.scope == JobScope.system, Job.user_id.is_(None)))
    job = db.scalar(
        select(Job).where(Job.id == job_id, or_(*access))
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )
    return job
