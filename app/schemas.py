"""
Pydantic 응답/요청 스키마
"""
from typing import Optional

from pydantic import BaseModel, Field


class SignalOut(BaseModel):
    id: int
    source: str
    source_id: str
    name: Optional[str] = None
    region_cd: Optional[str] = None
    has_ped_signal: Optional[bool] = None
    cycle_time: Optional[int] = None
    lat: float
    lng: float
    distance_m: Optional[float] = Field(
        default=None, description="요청 좌표로부터의 거리(m). nearby 응답에서만 채워짐."
    )


class IngestResult(BaseModel):
    source: str
    fetched: int = Field(description="외부 API에서 받은 레코드 수")
    inserted: int
    updated: int
    skipped: int = 0
