"""
Pydantic 응답/요청 스키마
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field, model_validator


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
    mode: str = Field(
        default="walking",
        description="이동 모드: 'walking'(T-map 보행자) 또는 'driving'(Naver Directions 5)",
    )
    option: str = Field(
        default="trafast",
        description="driving 모드 옵션: trafast(빠른길) / tracomfort(편한길) / traoptimal(추천)",
    )
    signal_buffer_m: int = Field(
        default=30, ge=0, le=200, description="경로 폴리라인에서 신호등을 수집할 버퍼(m)"
    )


class RealtimeTraffic(BaseModel):
    """서울 TOPIS 기반 실시간 도로 소통 요약."""
    sample_count: int
    avg_speed_kmh: float
    timestamp: Optional[str] = None
    congestion: str = Field(description="원활/서행/지정체/정체")
    eta_factor: float = Field(
        default=1.0,
        description="ETA에 적용된 실시간 보정 계수 (>1: ETA 증가, <1: 감소)",
    )


class RouteResponse(BaseModel):
    mode: str = Field(default="walking", description="실제 사용된 이동 모드")
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
    realtime_traffic: Optional[RealtimeTraffic] = Field(
        default=None, description="서울 실시간 도로 소통 요약 (Seoul OpenAPI 키 설정 시)"
    )


RouteResponse.model_rebuild()


# ── 인증 ───────────────────────────────────────────────────────
class UserCreate(BaseModel):
    email: EmailStr
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, description="8자 이상")


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    username: str
    created_at: datetime
    model_config = {"from_attributes": True}


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ── GPS 궤적 수집 ─────────────────────────────────────────────
class GpsTripCreate(BaseModel):
    label: Optional[str] = Field(
        default=None, max_length=100, description="기록 이름 (예: '퇴근길 산책'). 앱의 '기록하기' 화면에서 씀"
    )
    origin_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    origin_lng: Optional[float] = Field(default=None, ge=-180, le=180)
    dest_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    dest_lng: Optional[float] = Field(default=None, ge=-180, le=180)
    target_arrival_at: Optional[datetime] = Field(
        default=None, description="목표 도착 시각 (ETA/출발 추천 계산에 사용, 이후 단계에서 구현)"
    )


class GpsTripOut(BaseModel):
    id: int
    user_id: int
    label: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None
    target_arrival_at: Optional[datetime] = None
    status: str
    model_config = {"from_attributes": True}


class GpsPointIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    speed_mps: Optional[float] = Field(default=None, ge=0, description="기기가 제공하는 순간 속도(m/s), 없으면 null")
    accuracy_m: Optional[float] = Field(default=None, ge=0, description="위치 정확도 반경(m)")
    recorded_at: datetime = Field(description="기기에서 측정한 시각 (timezone 포함 권장)")


class GpsPointsUploadRequest(BaseModel):
    points: List[GpsPointIn] = Field(min_length=1, max_length=500)


class GpsPointsUploadResult(BaseModel):
    trip_id: int
    inserted: int


class GpsTripFinishResult(BaseModel):
    trip_id: int
    status: str
    point_count: int
    ended_at: datetime


class StopClusterOut(BaseModel):
    id: int
    trip_id: int
    lat: float
    lng: float
    started_at: datetime
    ended_at: datetime
    duration_s: int
    point_count: int
    matched_signal_id: Optional[int] = None
    matched_signal_distance_m: Optional[float] = None
    model_config = {"from_attributes": True}


class EtaTrainResult(BaseModel):
    trained: bool = Field(description="학습이 실제로 수행됐는지 (표본 부족 시 false)")
    sample_count: int = Field(description="학습에 사용된(또는 부족했던) 완료 trip 수")
    reason: Optional[str] = Field(default=None, description="trained=false일 때 사유")


class EtaPredictResult(BaseModel):
    predicted_duration_s: int = Field(description="예상 소요시간(중앙값, 초)")
    duration_p10_s: int = Field(description="낙관적 추정(하위 10%, 초)")
    duration_p90_s: int = Field(description="비관적 추정(상위 90%, 초)")
    on_time_probability: Optional[float] = Field(
        default=None, description="target_arrival_at을 지정했을 때만 채워짐 (0~1)"
    )
    model_source: str = Field(
        description="'model'(학습된 XGBoost 분위수 회귀) 또는 'heuristic'(데이터 부족 시 규칙 기반)"
    )
    sample_count: int = Field(description="예측에 쓰인 population 학습 표본 수 (모델 학습 시점 기준)")


class EtaLstmBenchmarkOut(BaseModel):
    trained: bool = Field(description="학습이 실제로 수행됐는지 (표본/시퀀스 부족 시 false)")
    sample_count: int = Field(description="유효한 GPS 시퀀스가 있는 완료 trip 수")
    train_count: int = Field(default=0, description="학습에 쓰인 수 (시간순 앞쪽)")
    holdout_count: int = Field(default=0, description="평가에 쓰인 수 (시간순 뒤쪽)")
    lstm_mae_s: Optional[float] = Field(default=None, description="LSTM 홀드아웃 평균 절대 오차(초)")
    xgb_mae_s: Optional[float] = Field(
        default=None, description="같은 홀드아웃에서 XGBoost 평균 절대 오차(초). 모델 없으면 null"
    )
    reason: Optional[str] = Field(default=None, description="trained=false일 때 사유")


class DepartureRecommendationOut(BaseModel):
    status: str = Field(
        description="'comfortable' / 'on_time' / 'urgent' / 'late' — 앱에서 분기 처리용"
    )
    message: str = Field(description="사람이 읽을 문구 (예: '여유 있는 출발')")
    on_time_probability: float = Field(description="목표 시각까지 도착할 확률 (0~1)")
    remaining_s: int = Field(description="목표 도착 시각까지 남은 시간(초). 음수면 이미 지남")
    predicted_duration_s: int
    duration_p10_s: int
    duration_p90_s: int
    model_source: str
    sample_count: int


class TripFeatureOut(BaseModel):
    trip_id: int
    distance_m: float = Field(description="노이즈 제거 후 GPS 궤적 누적 거리(m)")
    actual_duration_s: int = Field(description="trip 시작~종료 실측 소요시간(초)")
    moving_time_s: int
    stopped_time_s: int
    stop_count: int = Field(description="ST-DBSCAN으로 탐지된 정지 구간 수")
    signal_stop_count: int = Field(description="정지 구간 중 신호등과 매칭된 개수")
    avg_speed_mps: float = Field(description="이동 시간 기준 평균 속도(m/s)")
    hour_of_day: int = Field(description="출발 시각 (Asia/Seoul 기준, 0-23)")
    day_of_week: int = Field(description="출발 요일 (0=월 ... 6=일, Asia/Seoul 기준)")
    model_config = {"from_attributes": True}
