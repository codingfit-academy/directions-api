"""
GPS 궤적 수집 라우터. 모든 엔드포인트는 로그인이 필요하다.

- POST /gps/trips             : 이동(trip) 시작
- GET  /gps/trips/{id}        : trip 상태 조회
- POST /gps/trips/{id}/points : GPS 포인트 배치 업로드 (앱이 주기적으로 호출)
- POST /gps/trips/{id}/finish : 이동 종료 처리

노이즈 제거 · ST-DBSCAN 정지 클러스터링 · ETA 피처 계산은 다음 단계에서
trip 종료 후 비동기로 실행되는 배치 작업으로 추가된다 (현재는 원시 포인트
적재와 개수 집계까지만 수행).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import get_db
from ..models import User
from ..schemas import (
    GpsPointsUploadRequest,
    GpsPointsUploadResult,
    GpsTripCreate,
    GpsTripFinishResult,
    GpsTripOut,
)

_TRIP_COLUMNS = (
    "id, user_id, label, started_at, ended_at, origin_lat, origin_lng,"
    " dest_lat, dest_lng, target_arrival_at, status"
)

router = APIRouter(prefix="/gps", tags=["gps"])


async def _get_own_trip_or_404(db: AsyncSession, trip_id: int, user_id: int) -> dict:
    row = (
        await db.execute(
            text("SELECT * FROM gps_trips WHERE id = :id"), {"id": trip_id}
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Trip not found")
    if row["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="본인의 trip이 아닙니다.")
    return dict(row)


@router.post("/trips", response_model=GpsTripOut, status_code=201)
async def create_trip(
    body: GpsTripCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sql = text(
        f"""
        INSERT INTO gps_trips
            (user_id, label, started_at, origin_lat, origin_lng, dest_lat, dest_lng,
             target_arrival_at, status)
        VALUES
            (:user_id, :label, NOW(), :origin_lat, :origin_lng, :dest_lat, :dest_lng,
             :target_arrival_at, 'active')
        RETURNING {_TRIP_COLUMNS}
        """
    )
    row = (
        await db.execute(
            sql,
            {
                "user_id": current_user.id,
                "label": body.label,
                "origin_lat": body.origin_lat,
                "origin_lng": body.origin_lng,
                "dest_lat": body.dest_lat,
                "dest_lng": body.dest_lng,
                "target_arrival_at": body.target_arrival_at,
            },
        )
    ).mappings().first()
    await db.commit()
    return GpsTripOut(**dict(row))


@router.get("/trips", response_model=list[GpsTripOut])
async def list_trips(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """현재 로그인한 사용자의 trip 목록을 최신순으로 반환한다 (기록 화면의 '이전 기록' 목록용)."""
    rows = (
        await db.execute(
            text(
                f"SELECT {_TRIP_COLUMNS} FROM gps_trips "
                "WHERE user_id = :user_id ORDER BY started_at DESC"
            ),
            {"user_id": current_user.id},
        )
    ).mappings().all()
    return [GpsTripOut(**dict(r)) for r in rows]


@router.get("/trips/{trip_id}", response_model=GpsTripOut)
async def get_trip(
    trip_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = await _get_own_trip_or_404(db, trip_id, current_user.id)
    return GpsTripOut(**row)


@router.post("/trips/{trip_id}/points", response_model=GpsPointsUploadResult)
async def upload_points(
    trip_id: int,
    body: GpsPointsUploadRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    trip = await _get_own_trip_or_404(db, trip_id, current_user.id)
    if trip["status"] != "active":
        raise HTTPException(
            status_code=409, detail=f"Trip is not active (status={trip['status']})"
        )

    insert_sql = text(
        """
        INSERT INTO gps_points (trip_id, geom, speed_mps, accuracy_m, recorded_at)
        VALUES (
            :trip_id,
            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
            :speed_mps, :accuracy_m, :recorded_at
        )
        """
    )
    for p in body.points:
        await db.execute(
            insert_sql,
            {
                "trip_id": trip_id,
                "lat": p.lat,
                "lng": p.lng,
                "speed_mps": p.speed_mps,
                "accuracy_m": p.accuracy_m,
                "recorded_at": p.recorded_at,
            },
        )
    await db.commit()
    return GpsPointsUploadResult(trip_id=trip_id, inserted=len(body.points))


@router.post("/trips/{trip_id}/finish", response_model=GpsTripFinishResult)
async def finish_trip(
    trip_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    trip = await _get_own_trip_or_404(db, trip_id, current_user.id)
    if trip["status"] != "active":
        raise HTTPException(
            status_code=409, detail=f"Trip is not active (status={trip['status']})"
        )

    count_row = (
        await db.execute(
            text("SELECT COUNT(*) AS c FROM gps_points WHERE trip_id = :id"),
            {"id": trip_id},
        )
    ).mappings().first()

    updated = (
        await db.execute(
            text(
                """
                UPDATE gps_trips SET ended_at = NOW(), status = 'completed'
                WHERE id = :id
                RETURNING ended_at
                """
            ),
            {"id": trip_id},
        )
    ).mappings().first()
    await db.commit()

    return GpsTripFinishResult(
        trip_id=trip_id,
        status="completed",
        point_count=count_row["c"],
        ended_at=updated["ended_at"],
    )
