"""
Pydantic 응답/요청 스키마
"""
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


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


class GeocodeOut(BaseModel):
    lat: float
    lng: float
    address: str
    road_address: Optional[str] = None


class RoutePoint(BaseModel):
    """길찾기 입력 한 끝(출발지/도착지). text 또는 lat+lng 중 하나는 반드시 있어야 한다."""
    text: Optional[str] = None
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def _check_one(self) -> "RoutePoint":
        has_coords = self.lat is not None and self.lng is not None
        has_text = bool(self.text and self.text.strip())
        if not has_coords and not has_text:
            raise ValueError("text 또는 lat/lng 중 하나는 필수입니다.")
        return self


class RouteRequest(BaseModel):
    origin: RoutePoint
    destination: RoutePoint
    option: str = Field(
        default="trafast",
        description="Naver Directions 5 옵션: trafast(빠른길) / tracomfort(편한길) / traoptimal(추천)",
    )
    signal_buffer_m: int = Field(
        default=30, ge=0, le=200, description="경로 폴리라인에서 신호등을 수집할 버퍼(m)"
    )


class RouteResponse(BaseModel):
    origin: GeocodeOut
    destination: GeocodeOut
    duration_ms: int
    distance_m: int
    path: List[List[float]] = Field(
        description="경로 폴리라인 좌표 [[lat, lng], ...] (WGS84)"
    )
    signals: List["SignalOut"] = Field(
        default_factory=list, description="경로 주변(버퍼 내) 신호등 목록"
    )
    signal_delay_estimate_s: int = Field(
        default=0, description="경로 상 신호등 cycle_time의 절반을 합산한 예상 대기 시간(초)"
    )


RouteResponse.model_rebuild()
