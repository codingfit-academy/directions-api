"""
SQLAlchemy 모델
─────────────────────────────────────────────────────────────
앱 시작 시 main.py의 lifespan에서 테이블이 자동 생성됩니다.
"""
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Item(Base):
    """예시 모델 — 필요에 맞게 수정하거나 삭제하세요."""
    __tablename__ = "items"

    id: Mapped[int]          = mapped_column(Integer, primary_key=True)
    title: Mapped[str]       = mapped_column(String(100), nullable=False)
    content: Mapped[str]     = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Signal(Base):
    """
    신호등/교차로 통합 위치 정보.

    출처별로 source(예: 'police_crossroad', 'standard_data')와 source_id(원본 키)를
    저장하여 중복 적재를 방지합니다. 좌표는 항상 EPSG:4326(WGS84)으로 저장합니다.
    """
    __tablename__ = "signals"

    id: Mapped[int]            = mapped_column(Integer, primary_key=True)
    source: Mapped[str]        = mapped_column(String(32), nullable=False)
    source_id: Mapped[str]     = mapped_column(String(64), nullable=False)
    name: Mapped[str]          = mapped_column(String(200), nullable=True)
    region_cd: Mapped[str]     = mapped_column(String(16), nullable=True)
    has_ped_signal: Mapped[bool] = mapped_column(Boolean, nullable=True)
    cycle_time: Mapped[int]    = mapped_column(Integer, nullable=True)
    geom: Mapped[object]       = mapped_column(
        Geometry(geometry_type="POINT", srid=4326), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_signal_source"),
        Index("ix_signals_geom", "geom", postgresql_using="gist"),
    )


class User(Base):
    """이메일/비밀번호 기반 사용자 계정."""
    __tablename__ = "users"

    id: Mapped[int]              = mapped_column(Integer, primary_key=True)
    email: Mapped[str]           = mapped_column(String(255), nullable=False, unique=True, index=True)
    username: Mapped[str]        = mapped_column(String(64), nullable=False, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GpsTrip(Base):
    """
    앱이 하나의 이동(출발~도착)을 시작~종료할 때 생성하는 단위.

    trip 종료 시 app/services/gps_processing.py가 노이즈 제거·ST-DBSCAN 정지
    클러스터링을 수행해 StopCluster를 생성한다. 속도/거리 계산·ETA 피처 저장은
    이후 단계에서 추가된다.
    """
    __tablename__ = "gps_trips"

    id: Mapped[int]         = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int]    = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    label: Mapped[str]      = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), nullable=True)
    origin_lat: Mapped[float]    = mapped_column(Float, nullable=True)
    origin_lng: Mapped[float]    = mapped_column(Float, nullable=True)
    dest_lat: Mapped[float]      = mapped_column(Float, nullable=True)
    dest_lng: Mapped[float]      = mapped_column(Float, nullable=True)
    target_arrival_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str]     = mapped_column(String(16), nullable=False, default="active")
    # status: active(수집 중) / completed(종료) / discarded(폐기)


class GpsPoint(Base):
    """원시 GPS 포인트 (노이즈 제거 전). 좌표는 EPSG:4326(WGS84)으로 저장."""
    __tablename__ = "gps_points"

    id: Mapped[int]         = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int]    = mapped_column(Integer, ForeignKey("gps_trips.id"), nullable=False, index=True)
    geom: Mapped[object]    = mapped_column(
        Geometry(geometry_type="POINT", srid=4326), nullable=False
    )
    speed_mps: Mapped[float]   = mapped_column(Float, nullable=True)
    accuracy_m: Mapped[float]  = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_noise: Mapped[bool]  = mapped_column(Boolean, nullable=False, default=False)


class StopCluster(Base):
    """
    ST-DBSCAN으로 탐지된 정지 구간 (trip 종료 시 app/services/gps_processing.py가 생성).

    matched_signal_id로 signals(신호등) 테이블과 연결해, 이 정지가 실제 신호
    대기였는지 판별할 수 있게 한다.
    """
    __tablename__ = "stop_clusters"

    id: Mapped[int]         = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int]    = mapped_column(Integer, ForeignKey("gps_trips.id"), nullable=False, index=True)
    center_geom: Mapped[object] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), nullable=False)
    duration_s: Mapped[int]      = mapped_column(Integer, nullable=False)
    point_count: Mapped[int]     = mapped_column(Integer, nullable=False)
    matched_signal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("signals.id"), nullable=True
    )
    matched_signal_distance_m: Mapped[float] = mapped_column(Float, nullable=True)
    # 앱에서 사용자가 직접 확인해준 정지 사유. 'traffic_light' / 'elevator' / 'other'.
    # 서버가 자동 매칭한 matched_signal_id와 달리 사람이 검증한 값이라, 향후 신호등
    # DB 보정과 ETA 피처(대기 유형별 평균 대기시간)에 쓸 수 있다.
    user_label: Mapped[str] = mapped_column(String(32), nullable=True)
    # user_label == 'other'일 때 사용자가 직접 입력한 설명 (예: 육교, 계단).
    user_label_text: Mapped[str] = mapped_column(String(100), nullable=True)


class TripSegmentFeature(Base):
    """
    ETA 모델 학습/추론용 trip 단위 피처 (trip마다 1행). trip 종료 시 정지
    클러스터링과 함께 app/services/gps_processing.py가 계산해 upsert한다.

    actual_duration_s가 향후 ETA 회귀 모델의 학습 라벨이 되고, 나머지는 입력
    피처가 된다.
    """
    __tablename__ = "trip_segment_features"

    id: Mapped[int]         = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int]    = mapped_column(
        Integer, ForeignKey("gps_trips.id"), nullable=False, unique=True
    )
    distance_m: Mapped[float]        = mapped_column(Float, nullable=False)
    actual_duration_s: Mapped[int]   = mapped_column(Integer, nullable=False)
    moving_time_s: Mapped[int]       = mapped_column(Integer, nullable=False)
    stopped_time_s: Mapped[int]      = mapped_column(Integer, nullable=False)
    stop_count: Mapped[int]          = mapped_column(Integer, nullable=False)
    signal_stop_count: Mapped[int]   = mapped_column(Integer, nullable=False)
    avg_speed_mps: Mapped[float]     = mapped_column(Float, nullable=False)
    hour_of_day: Mapped[int]         = mapped_column(Integer, nullable=False)
    day_of_week: Mapped[int]         = mapped_column(Integer, nullable=False)
    # day_of_week: Python datetime.weekday() 기준 (0=월 ... 6=일), Asia/Seoul 기준.
    computed_at: Mapped[datetime]    = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ThumbnailCategory(Base):
    """
    관리자가 미리 만들어 둔 "분류 → 대표 이미지" 매핑 (directions-flutter 관리자
    페이지에서 생성/삭제한다). 여정 이름을 Gemini가 이 분류들 중 하나로 매칭하면
    (JourneyThumbnail), 그 분류의 이미지가 카드 썸네일로 쓰인다.
    """
    __tablename__ = "thumbnail_categories"

    id: Mapped[int]           = mapped_column(Integer, primary_key=True)
    name: Mapped[str]         = mapped_column(String(50), nullable=False, unique=True)
    image_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    mime_type: Mapped[str]    = mapped_column(String(32), nullable=False, default="image/png")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class JourneyThumbnail(Base):
    """
    사용자가 어떤 기록 이름(여정)으로 처음 기록을 시작하면(POST /gps/trips), 그
    이름이 어느 ThumbnailCategory와 가장 비슷한지 Gemini가 분류해 여기 캐싱해둔다
    (app/services/gemini_classify.py, 백그라운드로 1회만 실행).

    category_id가 NULL이면 "분류는 해봤지만 어울리는 분류가 없었다"는 뜻이고,
    행 자체가 없으면 "아직 분류를 안(못) 해봤다"는 뜻이다 — 두 상태를 구분해야
    똑같은 이름을 매번 다시 분류 시도하지 않는다.
    """
    __tablename__ = "journey_thumbnails"

    id: Mapped[int]      = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    label: Mapped[str]   = mapped_column(String(100), nullable=False)
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("thumbnail_categories.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "label", name="uq_journey_thumbnail_user_label"),
    )
