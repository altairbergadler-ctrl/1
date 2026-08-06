from pydantic import BaseModel


class HealthOut(BaseModel):
    status: str = "ok"


class LoginIn(BaseModel):
    token: str


class JobOut(BaseModel):
    id: int
    type: str
    status: str
    error: str | None = None

    class Config:
        from_attributes = True


class PlaylistOut(BaseModel):
    id: int
    name: str
    track_count: int

    class Config:
        from_attributes = True
