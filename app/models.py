"""
SQLAlchemy 모델 예시
─────────────────────────────────────────────────────────────
여기에 테이블 모델을 추가하세요.
앱 시작 시 main.py의 lifespan에서 테이블이 자동 생성됩니다.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
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
