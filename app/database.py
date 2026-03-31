"""
DB 연결 설정
─────────────────────────────────────────────────────────────
환경변수(docker-compose → .env)에서 자동으로 DB 정보를 읽습니다.
개발 환경에서는 프로젝트 루트의 .env 파일을 사용하세요.

  DB_HOST  = postgres          (서버에서는 Docker 서비스 이름)
  DB_PORT  = 5432
  DB_NAME  = {username}_db     (provision 시 자동 생성)
  DB_USER  = {username}        (provision 시 자동 생성)
  DB_PASS  = ****              (provision 시 자동 생성)
"""
import os

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = (
    "postgresql+asyncpg://"
    f"{os.getenv('DB_USER')}:{os.getenv('DB_PASS')}"
    f"@{os.getenv('DB_HOST', 'postgres')}:{os.getenv('DB_PORT', '5432')}"
    f"/{os.getenv('DB_NAME')}"
)

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with SessionLocal() as session:
        yield session
