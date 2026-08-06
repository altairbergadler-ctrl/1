from fastapi import FastAPI

from app.api import download, library, matching, playlists
from app.schemas import HealthOut

app = FastAPI(title="Music Service MVP", version="0.1.0")


@app.get("/api/health", response_model=HealthOut)
def health():
    return HealthOut()


app.include_router(playlists.router, prefix="/api/playlists", tags=["playlists"])
app.include_router(library.router, prefix="/api/library", tags=["library"])
app.include_router(matching.router, prefix="/api/matching", tags=["matching"])
app.include_router(download.router, prefix="/api/download", tags=["download"])
