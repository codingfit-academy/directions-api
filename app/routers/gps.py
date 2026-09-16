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
    RenameLabelIn,
    StopClusterLabelIn,
    StopClusterOut,
    TripFeatureOut,
)
from ..services.gemini_classify import classify_and_cache
from ..services.gps_processing import process_trip, refresh_outlier_flags, refresh_signal_stop_count
from ..services.signal_ingest import ensure_signal_near, refresh_signal_cycles

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
    background_tasks: BackgroundTasks,
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

    # 새 기록 이름이면 카드 썸네일용 분류를 백그라운드로 시도한다(응답 지연 없이).
    # classify_and_cache가 이미 분류된 이름이면 조용히 아무 것도 안 하므로 매번
    # 예약해도 안전하다.
    if body.label:
        background_tasks.add_task(classify_and_cache, current_user.id, body.label)

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


@router.patch("/trips/rename-label", status_code=204)
async def rename_label(
    body: RenameLabelIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    기록 이름(여정)을 바꾼다 — 개별 trip 하나가 아니라, 이 이름으로 쌓인 이
    사용자의 모든 trip이 한 번에 새 이름으로 바뀐다. 이미 존재하는 다른 이름으로
    바꾸면 그 여정과 합쳐지는 효과가 난다(둘 다 같은 label을 공유하게 되므로) —
    의도적인 동작이다.
    """
    old = body.old_label.strip()
    new = body.new_label.strip()
    if not new:
        raise HTTPException(status_code=400, detail="새 이름을 입력하세요.")

    await db.execute(
        text(
            "UPDATE gps_trips SET label = :new_label "
            "WHERE user_id = :user_id AND label IS NOT DISTINCT FROM :old_label"
        ),
        {"new_label": new, "user_id": current_user.id, "old_label": old},
    )
    await db.commit()

    # 이름이 합쳐졌을 수도 있으니(다른 여정과 같은 이름이 됐을 수 있음) 새 이름
    # 그룹 전체의 이상치 플래그를 다시 매긴다.
    latest = (
        await db.execute(
            text(
                "SELECT id FROM gps_trips WHERE user_id = :user_id "
                "AND label IS NOT DISTINCT FROM :new_label AND status = 'completed' "
                "ORDER BY started_at DESC LIMIT 1"
            ),
            {"user_id": current_user.id, "new_label": new},
        )
    ).first()
    if latest:
        await refresh_outlier_flags(db, latest[0])
        await db.commit()


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

    # is_noise를 명시해야 한다 — 모델의 default=False는 SQLAlchemy ORM insert()에서만
    # 채워지는 "파이썬 쪽" 기본값이라, 여기처럼 순수 text() SQL을 쓸 땐 전혀 적용되지
    # 않는다. DB 컬럼 자체엔 DEFAULT가 없어(NOT NULL만 있음) 이걸 빼면 매번
    # NotNullViolation으로 500이 났다 — 그래서 지금까지 좌표가 한 개도 안 쌓이고 있었다.
    insert_sql = text(
        """
        INSERT INTO gps_points (trip_id, geom, speed_mps, accuracy_m, recorded_at, is_noise)
        VALUES (
            :trip_id,
            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
            :speed_mps, :accuracy_m, :recorded_at, false
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


async def _delete_trip_cascade(db: AsyncSession, trip_id: int) -> None:
    """
    trip 하나와 거기 딸린 데이터를 전부 지운다. gps_points/stop_clusters/
    trip_segment_features → gps_trips.id에 FK가 있지만 ON DELETE CASCADE가
    없어서(스키마를 그대로 두고 싶어서) 여기서 순서대로 직접 지운다.
    호출부(취소/명시적 삭제)에서 커밋한다.
    """
    for table in ("trip_segment_features", "stop_clusters", "gps_points"):
        await db.execute(text(f"DELETE FROM {table} WHERE trip_id = :id"), {"id": trip_id})
    await db.execute(text("DELETE FROM gps_trips WHERE id = :id"), {"id": trip_id})


@router.post("/trips/{trip_id}/cancel", status_code=204)
async def cancel_trip(
    trip_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    기록 중간에 취소한다 — 완료 처리하지 않고 trip과 그동안 업로드된 데이터를
    통째로 지운다(과거엔 'discarded' 상태로만 남겼었는데, 어차피 쓸모없는 반쪽
    기록을 DB에 남겨둘 이유가 없어서 실제 삭제로 바꿨다).
    """
    trip = await _get_own_trip_or_404(db, trip_id, current_user.id)
    if trip["status"] != "active":
        raise HTTPException(
            status_code=409, detail=f"Trip is not active (status={trip['status']})"
        )

    await _delete_trip_cascade(db, trip_id)
    await db.commit()


@router.delete("/trips/{trip_id}", status_code=204)
async def delete_trip(
    trip_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    완료된(또는 어떤 상태든) 기록 하나를 사용자가 직접 지운다 — 예: GPS가 튀어서
    비정상적으로 남은 기록. 같은 (user, label) 그룹에 다른 기록이 남아 있으면
    이상치 재계산이 필요할 수 있어 refresh_outlier_flags를 한 번 더 돌린다
    (이 trip 자체는 지워지지만, 그 그룹의 "정상 범위" 자체가 이 trip 때문에
    쏠려 있었을 수 있기 때문).
    """
    trip = await _get_own_trip_or_404(db, trip_id, current_user.id)
    user_id, label = trip["user_id"], trip["label"]

    await _delete_trip_cascade(db, trip_id)
    await db.commit()

    if label:
        remaining = (
            await db.execute(
                text(
                    "SELECT id FROM gps_trips WHERE user_id = :user_id "
                    "AND label IS NOT DISTINCT FROM :label AND status = 'completed' LIMIT 1"
                ),
                {"user_id": user_id, "label": label},
            )
        ).first()
        if remaining:
            await refresh_outlier_flags(db, remaining[0])
            await db.commit()


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
    background_tasks: BackgroundTasks,
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

    # 신호등으로 연결됐는데 주기를 아직 모르면, 응답을 보낸 뒤 백그라운드로 주기를
    # 채운다. 관리자 적재를 따로 돌리지 않아도 서버가 스스로 보강하도록 하는 장치다
    # (전체 스캔은 12시간 캐시되므로 반복 호출해도 외부 API를 다시 때리지 않는다).
    if row["user_label"] == "traffic_light" and row["matched_signal_id"] is not None:
        signal_cycle = (
            await db.execute(
                text("SELECT cycle_time FROM signals WHERE id = :id"),
                {"id": row["matched_signal_id"]},
            )
        ).scalar_one_or_none()
        if signal_cycle is None:
            background_tasks.add_task(refresh_signal_cycles)

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
                       hour_of_day, day_of_week, is_outlier
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
