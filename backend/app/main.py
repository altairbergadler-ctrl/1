from fastapi import FastAPI, Request

from app.api import auth as auth_api
from app.api import (
    download,
    jobs,
    library,
    matching,
    playlists,
    providers,
    qobuz,
    sources,
    storage,
    yandex_download,
)
from app.schemas import HealthOut

app = FastAPI(title="Music Service MVP", version="0.4.0")


@app.middleware("http")
async def provider_api_no_store(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/api/providers", "/api/storage")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/health", response_model=HealthOut)
def health():
    return HealthOut()


app.include_router(playlists.router, prefix="/api/playlists", tags=["playlists"])
app.include_router(auth_api.router, prefix="/api/auth", tags=["auth"])
app.include_router(sources.router, prefix="/api/sources", tags=["sources"])
app.include_router(library.router, prefix="/api/library", tags=["library"])
app.include_router(matching.router, prefix="/api/matching", tags=["matching"])
app.include_router(download.router, prefix="/api/download", tags=["download"])
app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
app.include_router(providers.router, prefix="/api/providers", tags=["providers"])
app.include_router(storage.router, prefix="/api/storage", tags=["storage"])
# Интеграция Qobuz (RESTRICT, docs/qobuz-dl-assessment.md): докачка
# MISSING-треков и скачивание по ссылкам. Роутер подключается последним —
# опциональная надстройка над базовым контуром сервиса.
app.include_router(qobuz.router, prefix="/api/qobuz", tags=["qobuz"])
app.include_router(
    yandex_download.router,
    prefix="/api/yandex-download",
    tags=["yandex-download"],
)
