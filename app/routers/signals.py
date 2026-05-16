"""
신호등/교차로 위치 조회·적재 라우터.

- GET /signals/nearby?lat&lng&radius_m&limit  : 좌표 주변 신호등을 가까운 순으로 반환
- GET /signals/{id}                            : 단일 신호등 조회
- POST /signals/ingest/police                  : 경찰청 교차로 API 데이터 적재
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import IngestResult, SignalOut
from ..services.police_api import DataGoKrError
from ..services.signal_ingest import POLICE_SOURCE, ingest_police_crossroads

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/nearby", response_model=list[SignalOut])
async def nearby_signals(
    lat: float = Query(..., ge=-90, le=90, description="위도(WGS84)"),
    lng: float = Query(..., ge=-180, le=180, description="경도(WGS84)"),
    radius_m: int = Query(500, ge=10, le=5000, description="검색 반경(미터)"),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """현재 위치 주변의 신호등/교차로를 가까운 순으로 반환합니다."""
    sql = text(
        """
        SELECT id, source, source_id, name, region_cd,
               has_ped_signal, cycle_time,
               ST_Y(geom) AS lat,
               ST_X(geom) AS lng,
               ST_DistanceSphere(geom, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)) AS distance_m
        FROM signals
        WHERE ST_DWithin(
            geom::geography,
            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
            :radius_m
        )
        ORDER BY distance_m ASC
        LIMIT :limit
        """
    )
    rows = (
        await db.execute(
            sql, {"lat": lat, "lng": lng, "radius_m": radius_m, "limit": limit}
        )
    ).mappings().all()
    return [SignalOut(**dict(r)) for r in rows]


@router.get("/{signal_id}", response_model=SignalOut)
async def get_signal(signal_id: int, db: AsyncSession = Depends(get_db)):
    sql = text(
        """
        SELECT id, source, source_id, name, region_cd,
               has_ped_signal, cycle_time,
               ST_Y(geom) AS lat,
               ST_X(geom) AS lng
        FROM signals
        WHERE id = :id
        """
    )
    row = (await db.execute(sql, {"id": signal_id})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Signal not found")
    return SignalOut(**dict(row))


@router.post("/ingest/police", response_model=IngestResult)
async def ingest_police(
    region_cd: Optional[str] = Query(
        None, description="지역코드(미지정 시 전체). 서울만 제공되므로 보통 비워둡니다."
    ),
    limit: Optional[int] = Query(
        None, ge=1, le=20000, description="이번 적재에서 처리할 최대 레코드 수(테스트용)."
    ),
    db: AsyncSession = Depends(get_db),
):
    """경찰청 교차로기반정보서비스의 데이터를 적재한다."""
    try:
        stats = await ingest_police_crossroads(db, region_cd=region_cd, limit=limit)
    except DataGoKrError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return IngestResult(source=POLICE_SOURCE, **stats)
