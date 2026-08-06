from celery import Celery

from app.config import settings

celery = Celery("music_service", broker=settings.redis_url,
                backend=settings.redis_url)
celery.conf.update(task_track_started=True, timezone="UTC")
