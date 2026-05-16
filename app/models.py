"""
SQLAlchemy 모델
─────────────────────────────────────────────────────────────
앱 시작 시 main.py의 lifespan에서 테이블이 자동 생성됩니다.
"""
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, func
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
