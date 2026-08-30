"""
GPS 궤적 후처리: 노이즈 제거 + ST-DBSCAN 정지 지점 클러스터링 + 속도/거리 피처 계산.

trip 종료(POST /gps/trips/{id}/finish) 시 백그라운드 태스크로 실행된다
(app/routers/gps.py의 finish_trip 참고). 흐름:

    1) 노이즈 제거 — 정확도(accuracy_m)가 나쁘거나, 직전 유효 포인트 대비
       속도가 비정상적으로 큰 포인트를 걸러낸다 (gps_points.is_noise = true 표시,
       삭제하지는 않는다).
    2) ST-DBSCAN — 남은 포인트에서 "공간적으로 가깝고 + 시간적으로도 가까운"
       군집을 찾아 정지 구간으로 본다 (Birant & Kut, 2007).
    3) 각 정지 구간을 signals(신호등) 테이블과 매칭해 stop_clusters에 저장한다.
    4) 누적 거리·이동/정지 시간·평균 속도·출발 시각(요일/시간대) 등을 계산해
       trip_segment_features에 upsert한다 — 이후 ETA 모델의 학습 피처/라벨이 된다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import (
    GPS_MAX_ACCURACY_M,
    GPS_MAX_SPEED_MPS,
    MIN_STOP_DURATION_S,
    ST_DBSCAN_EPS_SPACE_M,
    ST_DBSCAN_EPS_TIME_S,
    ST_DBSCAN_MIN_PTS,
    STOP_SIGNAL_MATCH_BUFFER_M,
)
from ..database import SessionLocal

_KST = ZoneInfo("Asia/Seoul")


@dataclass
class _Point:
    id: int
    lat: float
    lng: float
    accuracy_m: float | None
    recorded_at: datetime


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 WGS84 좌표 간 직선거리(m)."""
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def remove_noise(points: list[_Point]) -> tuple[list[_Point], list[int]]:
    """
    정확도가 나쁜 포인트와, 직전 유효 포인트 대비 순간속도가 비정상적인
    포인트를 걸러낸다. (kept_points, noise_point_ids) 를 반환한다.

    포인트는 이미 recorded_at 오름차순으로 정렬돼 들어온다고 가정한다.
    """
    kept: list[_Point] = []
    noise_ids: list[int] = []
    last_kept: _Point | None = None

    for p in points:
        if p.accuracy_m is not None and p.accuracy_m > GPS_MAX_ACCURACY_M:
            noise_ids.append(p.id)
            continue

        if last_kept is not None:
            dt = (p.recorded_at - last_kept.recorded_at).total_seconds()
            if dt > 0:
                dist = _haversine_m(last_kept.lat, last_kept.lng, p.lat, p.lng)
                speed = dist / dt
                if speed > GPS_MAX_SPEED_MPS:
                    noise_ids.append(p.id)
                    continue

        kept.append(p)
        last_kept = p

    return kept, noise_ids


def st_dbscan(
    points: list[_Point],
    eps_space_m: float = ST_DBSCAN_EPS_SPACE_M,
    eps_time_s: float = ST_DBSCAN_EPS_TIME_S,
    min_pts: int = ST_DBSCAN_MIN_PTS,
) -> list[int]:
    """
    ST-DBSCAN (Birant & Kut, 2007). 일반 DBSCAN과 달리 두 포인트가 이웃이려면
    공간 거리 <= eps_space_m *그리고* 시간 거리 <= eps_time_s 를 모두 만족해야
    한다. points와 같은 길이의 라벨 리스트를 반환한다 (-1 = 정지 아님/노이즈,
    0 이상 = 클러스터 번호).

    trip 하나의 포인트 수가 많지 않다는 전제(도보 20~30분 기준 수백 개)로
    O(n^2) 전수 비교를 쓴다 — 배치 작업이라 응답 지연에 영향 없음.
    """
    n = len(points)
    labels: list[int | None] = [None] * n

    def region_query(i: int) -> list[int]:
        neighbors = []
        for j in range(n):
            if j == i:
                continue
            spatial_d = _haversine_m(points[i].lat, points[i].lng, points[j].lat, points[j].lng)
            if spatial_d > eps_space_m:
                continue
            temporal_d = abs((points[i].recorded_at - points[j].recorded_at).total_seconds())
            if temporal_d > eps_time_s:
                continue
            neighbors.append(j)
        return neighbors

    cluster_id = -1
    for i in range(n):
        if labels[i] is not None:
            continue

        neighbors = region_query(i)
        if len(neighbors) + 1 < min_pts:
            labels[i] = -1
            continue

        cluster_id += 1
        labels[i] = cluster_id
        seeds = list(neighbors)
        k = 0
        while k < len(seeds):
            j = seeds[k]
            k += 1
            if labels[j] == -1:
                labels[j] = cluster_id
            if labels[j] is not None:
                continue
            labels[j] = cluster_id
            j_neighbors = region_query(j)
            if len(j_neighbors) + 1 >= min_pts:
                for nb in j_neighbors:
                    if nb not in seeds:
                        seeds.append(nb)

    return [label if label is not None else -1 for label in labels]


@dataclass
class StopClusterResult:
    center_lat: float
    center_lng: float
    started_at: datetime
    ended_at: datetime
    duration_s: int
    point_count: int


def extract_stop_clusters(points: list[_Point], labels: list[int]) -> list[StopClusterResult]:
    """클러스터 라벨을 (중심좌표, 시작/종료시각, 체류시간) 목록으로 정리한다.

    최소 체류시간(MIN_STOP_DURATION_S)보다 짧은 클러스터는 버린다 — 신호
    대기라기엔 너무 짧아 GPS 튐/서행일 가능성이 높다.
    """
    clusters: dict[int, list[_Point]] = {}
    for point, label in zip(points, labels):
        if label < 0:
            continue
        clusters.setdefault(label, []).append(point)

    results: list[StopClusterResult] = []
    for members in clusters.values():
        started_at = min(p.recorded_at for p in members)
        ended_at = max(p.recorded_at for p in members)
        duration_s = int((ended_at - started_at).total_seconds())
        if duration_s < MIN_STOP_DURATION_S:
            continue
        results.append(
            StopClusterResult(
                center_lat=sum(p.lat for p in members) / len(members),
                center_lng=sum(p.lng for p in members) / len(members),
                started_at=started_at,
                ended_at=ended_at,
                duration_s=duration_s,
                point_count=len(members),
            )
        )
    return results


def _total_distance_m(points: list[_Point]) -> float:
    """노이즈 제거 후 남은 포인트를 시간순으로 이어 누적 거리를 구한다."""
    return sum(
        _haversine_m(a.lat, a.lng, b.lat, b.lng) for a, b in zip(points, points[1:])
    )


async def _compute_and_store_feature(
    db: AsyncSession, trip_id: int, kept_points: list[_Point]
) -> None:
    """
    속도/거리 + ETA 학습용 피처를 계산해 trip_segment_features에 upsert한다.
    stop_clusters는 이미 DB에 적재된 상태여야 한다 (stopped_time_s 등을 거기서 집계).
    """
    trip_row = (
        await db.execute(
            text("SELECT started_at, ended_at FROM gps_trips WHERE id = :id"),
            {"id": trip_id},
        )
    ).mappings().first()
    if trip_row is None or trip_row["ended_at"] is None:
        return  # 아직 종료되지 않은 trip이면 계산할 수 없다.

    started_at: datetime = trip_row["started_at"]
    ended_at: datetime = trip_row["ended_at"]
    actual_duration_s = max(0, int((ended_at - started_at).total_seconds()))

    stop_row = (
        await db.execute(
            text(
                """
                SELECT COUNT(*) AS stop_count,
                       COUNT(matched_signal_id) AS signal_stop_count,
                       COALESCE(SUM(duration_s), 0) AS stopped_time_s
                FROM stop_clusters
                WHERE trip_id = :trip_id
                """
            ),
            {"trip_id": trip_id},
        )
    ).mappings().first()

    stopped_time_s = int(stop_row["stopped_time_s"] or 0)
    moving_time_s = max(0, actual_duration_s - stopped_time_s)
    distance_m = _total_distance_m(kept_points)
    if moving_time_s > 0:
        avg_speed_mps = distance_m / moving_time_s
    elif actual_duration_s > 0:
        avg_speed_mps = distance_m / actual_duration_s
    else:
        avg_speed_mps = 0.0

    local_started = started_at.astimezone(_KST)

    await db.execute(
        text(
            """
            INSERT INTO trip_segment_features
                (trip_id, distance_m, actual_duration_s, moving_time_s, stopped_time_s,
                 stop_count, signal_stop_count, avg_speed_mps, hour_of_day, day_of_week)
            VALUES
                (:trip_id, :distance_m, :actual_duration_s, :moving_time_s, :stopped_time_s,
                 :stop_count, :signal_stop_count, :avg_speed_mps, :hour_of_day, :day_of_week)
            ON CONFLICT (trip_id) DO UPDATE SET
                distance_m = EXCLUDED.distance_m,
                actual_duration_s = EXCLUDED.actual_duration_s,
                moving_time_s = EXCLUDED.moving_time_s,
                stopped_time_s = EXCLUDED.stopped_time_s,
                stop_count = EXCLUDED.stop_count,
                signal_stop_count = EXCLUDED.signal_stop_count,
                avg_speed_mps = EXCLUDED.avg_speed_mps,
                hour_of_day = EXCLUDED.hour_of_day,
                day_of_week = EXCLUDED.day_of_week,
                computed_at = NOW()
            """
        ),
        {
            "trip_id": trip_id,
            "distance_m": distance_m,
            "actual_duration_s": actual_duration_s,
            "moving_time_s": moving_time_s,
            "stopped_time_s": stopped_time_s,
            "stop_count": int(stop_row["stop_count"] or 0),
            "signal_stop_count": int(stop_row["signal_stop_count"] or 0),
            "avg_speed_mps": avg_speed_mps,
            "hour_of_day": local_started.hour,
            "day_of_week": local_started.weekday(),
        },
    )


async def process_trip(trip_id: int) -> None:
    """trip 종료 후 백그라운드로 실행되는 전체 파이프라인 (finish_trip에서 트리거)."""
    async with SessionLocal() as db:
        await _process_trip(db, trip_id)


async def _process_trip(db: AsyncSession, trip_id: int) -> None:
    rows = (
        await db.execute(
            text(
                """
                SELECT id, ST_Y(geom) AS lat, ST_X(geom) AS lng, accuracy_m, recorded_at
                FROM gps_points
                WHERE trip_id = :trip_id
                ORDER BY recorded_at ASC
                """
            ),
            {"trip_id": trip_id},
        )
    ).mappings().all()

    points = [
        _Point(
            id=r["id"], lat=r["lat"], lng=r["lng"],
            accuracy_m=r["accuracy_m"], recorded_at=r["recorded_at"],
        )
        for r in rows
    ]
    if not points:
        return

    kept, noise_ids = remove_noise(points)

    if noise_ids:
        await db.execute(
            text("UPDATE gps_points SET is_noise = true WHERE id = ANY(:ids)"),
            {"ids": noise_ids},
        )

    labels = st_dbscan(kept)
    stops = extract_stop_clusters(kept, labels)

    for stop in stops:
        await _insert_stop_cluster(db, trip_id, stop)

    await _compute_and_store_feature(db, trip_id, kept)

    await db.commit()


async def _insert_stop_cluster(db: AsyncSession, trip_id: int, stop: StopClusterResult) -> None:
    # 정지 중심점 주변 신호등을 가장 가까운 순으로 찾아 매칭한다 (signals 라우터의
    # /signals/nearby 와 같은 ST_DWithin 패턴).
    signal_row = (
        await db.execute(
            text(
                """
                SELECT id, ST_Distance(
                    geom::geography,
                    ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography
                ) AS distance_m
                FROM signals
                WHERE ST_DWithin(
                    geom::geography,
                    ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
                    :buffer_m
                )
                ORDER BY distance_m ASC
                LIMIT 1
                """
            ),
            {"lat": stop.center_lat, "lng": stop.center_lng, "buffer_m": STOP_SIGNAL_MATCH_BUFFER_M},
        )
    ).mappings().first()

    await db.execute(
        text(
            """
            INSERT INTO stop_clusters
                (trip_id, center_geom, started_at, ended_at, duration_s, point_count,
                 matched_signal_id, matched_signal_distance_m)
            VALUES (
                :trip_id,
                ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
                :started_at, :ended_at, :duration_s, :point_count,
                :matched_signal_id, :matched_signal_distance_m
            )
            """
        ),
        {
            "trip_id": trip_id,
            "lat": stop.center_lat,
            "lng": stop.center_lng,
            "started_at": stop.started_at,
            "ended_at": stop.ended_at,
            "duration_s": stop.duration_s,
            "point_count": stop.point_count,
            "matched_signal_id": signal_row["id"] if signal_row else None,
            "matched_signal_distance_m": signal_row["distance_m"] if signal_row else None,
        },
    )
