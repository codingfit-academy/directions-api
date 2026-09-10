"""
GPS 궤적 수집 라우터. 모든 엔드포인트는 로그인이 필요하다.

- POST /gps/trips             : 이동(trip) 시작
- GET  /gps/trips             : trip 목록 조회
- GET  /gps/trips/{id}        : trip 상태 조회
- POST /gps/trips/{id}/points : GPS 포인트 배치 업로드 (앱이 주기적으로 호출)
- POST /gps/trips/{id}/finish   : 이동 종료 처리 → 노이즈 제거 + ST-DBSCAN 정지
                                   클러스터링 + 피처 계산을 백그라운드로 트리거
- GET  /gps/trips/{id}/points   : 기록한 GPS 경로 좌표 조회 (앱 지도에 경로 다시 그리기)
- GET  /gps/trips/{id}/stops    : 위 클러스터링 결과 조회
- PATCH /gps/stops/{id}/label   : 정지 구간이 신호등/엘리베이터/기타 중 무엇이었는지 사용자가 확인
- GET  /gps/trips/{id}/features : 속도/거리/정지 등 ETA 학습용 피처 조회

ETA 예측 모델과 출발 추천 로직은 다음 단계에서 추가된다.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import get_db
from ..models import User
from ..schemas import (
    GpsPointOut,
    GpsPointsUploadRequest,
    GpsPointsUploadResult,
    GpsTripCreate,
    GpsTripFinishResult,
    GpsTripOut,
    StopClusterLabelIn,
    StopClusterOut,
    TripFeatureOut,
)
from ..services.gps_processing import process_trip, refresh_signal_stop_count
from ..services.signal_ingest import ensure_signal_near

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
    background_tasks: BackgroundTasks,
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

    # 노이즈 제거 + ST-DBSCAN 정지 클러스터링은 응답 지연 없이 백그라운드로 실행.
    background_tasks.add_task(process_trip, trip_id)

    return GpsTripFinishResult(
        trip_id=trip_id,
        status="completed",
        point_count=count_row["c"],
        ended_at=updated["ended_at"],
    )


@router.get("/trips/{trip_id}/stops", response_model=list[StopClusterOut])
async def list_stop_clusters(
    trip_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """finish 이후 백그라운드 처리가 끝나면 채워지는 정지 구간 목록 (디버깅/확인용)."""
    await _get_own_trip_or_404(db, trip_id, current_user.id)

    rows = (
        await db.execute(
            text(
                """
                SELECT id, trip_id,
                       ST_Y(center_geom) AS lat, ST_X(center_geom) AS lng,
                       started_at, ended_at, duration_s, point_count,
                       matched_signal_id, matched_signal_distance_m,
                       user_label, user_label_text
                FROM stop_clusters
                WHERE trip_id = :trip_id
                ORDER BY started_at ASC
                """
            ),
            {"trip_id": trip_id},
        )
    ).mappings().all()
    return [StopClusterOut(**dict(r)) for r in rows]


@router.get("/trips/{trip_id}/points", response_model=list[GpsPointOut])
async def list_trip_points(
    trip_id: int,
    include_noise: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    기록한 이동 경로의 GPS 좌표를 시간순으로 반환한다 (앱의 '기록 지도 보기' 화면용).

    기본적으로 전처리에서 노이즈로 판정된 점은 제외한다 — 그대로 그리면 경로가
    튀어 보이기 때문. 원본 그대로 필요하면 include_noise=true를 준다.
    """
    await _get_own_trip_or_404(db, trip_id, current_user.id)

    where_noise = "" if include_noise else " AND is_noise = FALSE"
    rows = (
        await db.execute(
            text(
                f"""
                SELECT ST_Y(geom) AS lat, ST_X(geom) AS lng,
                       speed_mps, accuracy_m, recorded_at, is_noise
                FROM gps_points
                WHERE trip_id = :trip_id{where_noise}
                ORDER BY recorded_at ASC
                """
            ),
            {"trip_id": trip_id},
        )
    ).mappings().all()
    return [GpsPointOut(**dict(r)) for r in rows]


@router.patch("/stops/{stop_id}/label", response_model=StopClusterOut)
async def label_stop_cluster(
    stop_id: int,
    body: StopClusterLabelIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    정지 구간이 실제로 무엇이었는지(신호등/엘리베이터/기타) 사용자가 알려준 값을 저장한다.

    서버가 signals 테이블로 자동 매칭한 matched_signal_id와 달리 사람이 검증한
    값이라, 신호등 DB 보정과 ETA 학습(대기 유형별 평균 대기시간)의 근거가 된다.
    """
    owner = (
        await db.execute(
            text(
                """
                SELECT t.user_id, s.matched_signal_id,
                       ST_Y(s.center_geom) AS lat, ST_X(s.center_geom) AS lng
                FROM stop_clusters s
                JOIN gps_trips t ON t.id = s.trip_id
                WHERE s.id = :stop_id
                """
            ),
            {"stop_id": stop_id},
        )
    ).mappings().first()
    if not owner:
        raise HTTPException(status_code=404, detail="Stop cluster not found")
    if owner["user_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="본인의 기록이 아닙니다.")

    # 신호등이라고 확인해줬는데 자동 매칭이 안 된 지점이면, 지금 이 요청에서 바로
    # 경찰청 교차로 데이터를 조회해 연결한다. 신호 데이터가 서울만 제공되므로
    # 서울 밖이면 조용히 건너뛴다(라벨 자체는 그대로 저장된다).
    matched_signal_id = owner["matched_signal_id"]
    matched_signal_distance_m = None
    if body.label == "traffic_light" and matched_signal_id is None:
        found = await ensure_signal_near(db, owner["lat"], owner["lng"])
        if found:
            matched_signal_id, matched_signal_distance_m = found

    row = (
        await db.execute(
            text(
                """
                UPDATE stop_clusters
                SET user_label = :label,
                    user_label_text = :text,
                    matched_signal_id = COALESCE(:matched_signal_id, matched_signal_id),
                    matched_signal_distance_m = COALESCE(
                        :matched_signal_distance_m, matched_signal_distance_m
                    )
                WHERE id = :stop_id
                RETURNING id, trip_id,
                          ST_Y(center_geom) AS lat, ST_X(center_geom) AS lng,
                          started_at, ended_at, duration_s, point_count,
                          matched_signal_id, matched_signal_distance_m,
                          user_label, user_label_text
                """
            ),
            {
                "stop_id": stop_id,
                "label": body.label,
                "text": body.text,
                "matched_signal_id": matched_signal_id,
                "matched_signal_distance_m": matched_signal_distance_m,
            },
        )
    ).mappings().first()

    # 사용자가 "신호등"이라고 확인해준 정지는 ETA 학습 피처(signal_stop_count)에도
    # 반영해야 다음 예측이 실제로 나아진다 — 앱이 사용자에게 약속하는 부분이다.
    await refresh_signal_stop_count(db, row["trip_id"])

    await db.commit()
    return StopClusterOut(**dict(row))


@router.get("/trips/{trip_id}/features", response_model=TripFeatureOut)
async def get_trip_feature(
    trip_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """finish 이후 백그라운드 처리가 끝나면 채워지는 ETA 학습용 피처 (디버깅/확인용)."""
    await _get_own_trip_or_404(db, trip_id, current_user.id)

    row = (
        await db.execute(
            text(
                """
                SELECT trip_id, distance_m, actual_duration_s, moving_time_s,
                       stopped_time_s, stop_count, signal_stop_count, avg_speed_mps,
                       hour_of_day, day_of_week
                FROM trip_segment_features
                WHERE trip_id = :trip_id
                """
            ),
            {"trip_id": trip_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(
            status_code=404, detail="아직 처리되지 않았거나 GPS 데이터가 없습니다."
        )
    return TripFeatureOut(**dict(row))
