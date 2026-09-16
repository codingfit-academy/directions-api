"""
Academy FastAPI 스타터 템플릿
─────────────────────────────────────────────────────────────
DB 접근:
  환경변수(DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASS)는
  서버의 provision 과정에서 자동으로 .env에 기록됩니다.
  로컬 개발 시에는 프로젝트 루트에 .env 파일을 만들어 사용하세요.

    DB_HOST=localhost
    DB_PORT=5432
    DB_NAME=mydb
    DB_USER=myuser
    DB_PASS=mypassword

엔드포인트 추가 방법:
  app/routers/ 폴더를 만들어 라우터 파일을 분리하고
  아래 include_router 예시처럼 등록하세요.
─────────────────────────────────────────────────────────────
"""
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import KAKAO_MAPS_APP_KEY, NAVER_MAPS_CLIENT_ID
from .database import Base, enable_postgis, engine, get_db
from .models import Item
from .routers import admin as admin_router
from .routers import auth as auth_router
from .routers import directions as directions_router
from .routers import eta as eta_router
from .routers import gps as gps_router
from .routers import journeys as journeys_router
from .routers import signals as signals_router


# 이미 만들어진 테이블에 나중에 추가된 컬럼들. create_all은 "없는 테이블"만 만들고
# 기존 테이블에 컬럼을 추가해주지는 않아서, 여기서 idempotent하게 보강한다.
# (알렘빅을 도입할 규모는 아니라 ADD COLUMN IF NOT EXISTS로 처리)
_COLUMN_MIGRATIONS = (
    "ALTER TABLE stop_clusters ADD COLUMN IF NOT EXISTS user_label VARCHAR(32)",
    "ALTER TABLE stop_clusters ADD COLUMN IF NOT EXISTS user_label_text VARCHAR(100)",
    # 걷는 속도 평균(남/녀) 비교용 — 기존 가입자는 NULL(선택 안 함)로 남는다.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS gender VARCHAR(16)",
    # GPS가 튄 trip을 ETA 학습/예측에서 자동으로 제외하기 위한 플래그.
    "ALTER TABLE trip_segment_features ADD COLUMN IF NOT EXISTS is_outlier BOOLEAN NOT NULL DEFAULT FALSE",
    # gps_points.is_noise엔 컬럼 레벨 기본값이 없어서(모델의 default=는 ORM
    # insert()에만 적용됨) raw SQL INSERT(app/routers/gps.py의 upload_points)가
    # 이 컬럼을 안 채우면 NOT NULL 위반으로 매번 500이 났다 — 그 INSERT문은
    # is_noise를 명시하도록 고쳤지만, DB 컬럼에도 진짜 기본값을 심어 재발을 막는다.
    "ALTER TABLE gps_points ALTER COLUMN is_noise SET DEFAULT false",
    # 분류 대표 이미지를 DB(BYTEA)가 아니라 Cloudflare R2에 저장하도록 바꿈
    # (app/services/r2_storage.py). 기존 배포엔 image_data(NOT NULL) 컬럼이 이미
    # 있어서 새 행 insert가 막히므로 nullable로 풀어준다 — 반대로 새로 만드는
    # 테이블은 최신 모델대로 image_data 자체가 없으니, DO 블록으로 컬럼이 있을
    # 때만 ALTER해서 양쪽 다 안전하게 만든다.
    "ALTER TABLE thumbnail_categories ADD COLUMN IF NOT EXISTS r2_key VARCHAR(255)",
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'thumbnail_categories' AND column_name = 'image_data'
        ) THEN
            ALTER TABLE thumbnail_categories ALTER COLUMN image_data DROP NOT NULL;
        END IF;
    END $$;
    """,
)


# ── 앱 시작 시 PostGIS 확장 활성화 + 테이블 자동 생성 ───────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await enable_postgis()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for statement in _COLUMN_MIGRATIONS:
            await conn.execute(text(statement))
    yield


app = FastAPI(title="Directions API", lifespan=lifespan)

# allow_origins=["*"] 대신 정규식을 쓴다: 배포 환경(Cloudflare 프록시)에서 실제
# 브라우저(Flutter Web) preflight 요청 시 Access-Control-Allow-Origin 헤더가
# 통째로 빠지는 문제를 확인했다 — Origin 헤더가 전혀 없는 요청에도
# Access-Control-Allow-Credentials: true가 붙어 있는 걸로 보아, 프록시가
# "와일드카드(*) + Credentials" 조합(스펙상 금지된 조합)을 감지해 응답에서
# Allow-Origin 자체를 지워버리는 것으로 보인다(원인 추정 — 인프라 쪽이라 앱
# 코드만으로 100% 확인은 불가능). 고정 문자열 "*" 대신 매칭된 origin을 그대로
# 되돌려주면(echo) 이 조합이 안 생기므로 회피된다.
#
# 허용 origin: codingfit.kr 서브도메인 전체(배포된 프론트) + 로컬 개발
# (Flutter Web `flutter run -d chrome`는 매번 랜덤 포트를 쓰므로 포트 전체 허용).
_ALLOWED_ORIGIN_REGEX = (
    r"https://([a-zA-Z0-9-]+\.)*codingfit\.kr"
    r"|http://localhost(:\d+)?"
    r"|http://127\.0\.0\.1(:\d+)?"
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_ALLOWED_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response 스키마 ──────────────────────────────────
class ItemCreate(BaseModel):
    title: str
    content: Optional[str] = None


class ItemOut(BaseModel):
    id: int
    title: str
    content: Optional[str]
    model_config = {"from_attributes": True}


# ── 헬스체크 (필수 — 배포 시 health check가 이 엔드포인트를 호출합니다) ──
@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB 연결 실패: {e}")

@app.get("/")
async def root():
    return {"message": "Hello from Academy API!"}


# ── 예시 CRUD (items 테이블) ───────────────────────────────────
@app.get("/items", response_model=list[ItemOut])
async def list_items(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Item).order_by(Item.id))
    return result.scalars().all()


@app.post("/items", response_model=ItemOut, status_code=201)
async def create_item(body: ItemCreate, db: AsyncSession = Depends(get_db)):
    item = Item(title=body.title, content=body.content)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@app.get("/items/{item_id}", response_model=ItemOut)
async def get_item(item_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Item).where(Item.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@app.delete("/items/{item_id}", status_code=204)
async def delete_item(item_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Item).where(Item.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    await db.delete(item)
    await db.commit()


# ── 라우터 등록 ────────────────────────────────────────────────
app.include_router(auth_router.router)
app.include_router(signals_router.router)
app.include_router(directions_router.router)
app.include_router(gps_router.router)
app.include_router(eta_router.router)
app.include_router(journeys_router.router)
app.include_router(admin_router.router)

# ── 프론트용 공개 설정 (지도 API 키 등 — 브라우저에 노출되는 값만) ──
@app.get("/config")
async def public_config():
    return {
        "naverMapsClientId": NAVER_MAPS_CLIENT_ID,
        "kakaoMapsAppKey":   KAKAO_MAPS_APP_KEY,
    }
