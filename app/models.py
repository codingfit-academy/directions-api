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
