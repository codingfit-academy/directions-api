"""
길찾기 라우터.

- GET  /directions/geocode?q=...      : 주소/장소명 → 좌표
- POST /directions/route              : 출발/도착(텍스트 또는 좌표) → 경로 + 주변 신호등
"""
from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import MAX_ROUTE_DISTANCE_M
from ..database import get_db
from ..schemas import (
    GeocodeOut,
    RealtimeTraffic,
    RouteRequest,
    RouteResponse,
    SignalOut,
)
from ..services.kakao_local import KakaoLocalError
from ..services.kakao_local import geocode as kakao_geocode
from ..services.naver_directions import (
    GeocodeResult,
    NaverDirectionsError,
    driving_route,
    geocode,
)
from ..services.seoul_topis import SeoulTopisError, fetch_realtime_summary
from ..services.tmap_directions import TmapError, pedestrian_route
from ..services.vworld_geocode import VWorldError
from ..services.vworld_geocode import geocode as vworld_geocode
from ..services.vworld_geocode import search_candidates as vworld_search

router = APIRouter(prefix="/directions", tags=["directions"])


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 WGS84 좌표 간 직선거리(m)."""
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


async def _geocode_with_fallback(query: str) -> GeocodeResult:
    """폴백 체인: NCP Geocoding → VWorld → Kakao Local."""
    errors: list[str] = []

    # 1) NCP Geocoding (정밀 주소)
    try:
        return await geocode(query)
    except NaverDirectionsError as exc:
        errors.append(f"NCP: {exc}")

    # 2) VWorld (주소 + POI 검색)
    try:
        v = await vworld_geocode(query)
        return GeocodeResult(
            lat=v.lat, lng=v.lng, address=v.address, road_address=None
        )
    except VWorldError as exc:
        errors.append(f"VWorld: {exc}")

    # 3) Kakao Local (활성화된 경우)
    try:
        k = await kakao_geocode(query)
        return GeocodeResult(
            lat=k.lat, lng=k.lng, address=k.address, road_address=None
        )
    except KakaoLocalError as exc:
        errors.append(f"Kakao: {exc}")

    raise HTTPException(status_code=400, detail=" / ".join(errors))


async def _resolve(point, default_label: str) -> GeocodeResult:
    """RoutePoint를 좌표로 확정한다. lat/lng가 있으면 그대로 쓰고, 없으면 지오코딩."""
    if point.lat is not None and point.lng is not None:
        return GeocodeResult(
            lat=point.lat,
            lng=point.lng,
            address=(point.text or default_label),
            road_address=None,
        )
    return await _geocode_with_fallback(point.text or "")


@router.get("/geocode", response_model=GeocodeOut)
async def geocode_endpoint(
    q: str = Query(..., min_length=1, description="검색어(주소/장소명)"),
):
    result = await _geocode_with_fallback(q)
    return GeocodeOut(
        lat=result.lat,
        lng=result.lng,
        address=result.address,
        road_address=result.road_address,
    )


@router.get("/geocode/search", response_model=list[GeocodeOut])
async def geocode_search_endpoint(
    q: str = Query(..., min_length=1, description="검색어(주소/장소명)"),
    limit: int = Query(5, ge=1, le=10, description="반환할 후보 개수"),
):
    """후보 목록을 N개 반환. 사용자가 정확한 위치를 직접 선택하도록 한다."""
    try:
        results = await vworld_search(q, limit=limit)
    except VWorldError as exc:
        # VWorld 실패 시 NCP 단일 결과 + Kakao 단일 결과로 보조
        fallback: list[GeocodeOut] = []
        try:
            r = await _geocode_with_fallback(q)
            fallback.append(
                GeocodeOut(
                    lat=r.lat, lng=r.lng,
                    address=r.address, road_address=r.road_address,
                )
            )
        except HTTPException:
            pass
        if not fallback:
            raise HTTPException(status_code=400, detail=str(exc))
        return fallback

    return [
        GeocodeOut(lat=r.lat, lng=r.lng, address=r.address, road_address=None)
        for r in results
    ]


@router.post("/route", response_model=RouteResponse)
async def route_endpoint(
    body: RouteRequest, db: AsyncSession = Depends(get_db)
):
    origin = await _resolve(body.origin, "출발지")
    destination = await _resolve(body.destination, "도착지")

    # 근거리 보행 전용이므로 직선거리 상한 검증
    straight_m = _haversine_m(origin.lat, origin.lng, destination.lat, destination.lng)
    if straight_m > MAX_ROUTE_DISTANCE_M:
        raise HTTPException(
            status_code=400,
            detail=(
                f"출발지·도착지 직선거리 {straight_m / 1000:.2f}km 는 허용 범위 "
                f"{MAX_ROUTE_DISTANCE_M / 1000:.1f}km 를 초과합니다. "
                f"이 앱은 근거리 보행 경로 전용입니다."
            ),
        )

    # mode 별 라우팅. T-map 키 없으면 driving 으로 폴백.
    used_mode = body.mode
    duration_ms = 0
    distance_m = 0
    path_pairs: list[tuple[float, float]] = []

    if body.mode == "walking":
        try:
            walk = await pedestrian_route(
                origin.lat, origin.lng, destination.lat, destination.lng,
                origin_name=origin.address, dest_name=destination.address,
            )
            duration_ms = walk.duration_s * 1000
            distance_m = walk.distance_m
            path_pairs = walk.path
        except TmapError as exc:
            # 키 없거나 외부 장애 → driving 폴백
            used_mode = "driving"
            try:
                drive = await driving_route(
                    origin.lat, origin.lng, destination.lat, destination.lng,
                    option=body.option,
                )
                duration_ms = drive.duration_ms
                distance_m = drive.distance_m
                path_pairs = drive.path
            except NaverDirectionsError as drive_exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"T-map: {exc} / Naver: {drive_exc}",
                )
    else:
        try:
            drive = await driving_route(
                origin.lat, origin.lng, destination.lat, destination.lng,
                option=body.option,
            )
            duration_ms = drive.duration_ms
            distance_m = drive.distance_m
            path_pairs = drive.path
        except NaverDirectionsError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    signals: list[SignalOut] = []
    signal_delay = 0
    if len(path_pairs) >= 2 and body.signal_buffer_m > 0:
        # PostGIS LINESTRING WKT (경위도 순)
        line_wkt = "LINESTRING(" + ", ".join(
            f"{lng} {lat}" for lat, lng in path_pairs
        ) + ")"
        sql = text(
            """
            SELECT id, source, source_id, name, region_cd,
                   has_ped_signal, cycle_time,
                   ST_Y(geom) AS lat,
                   ST_X(geom) AS lng,
                   ST_Distance(
                       geom::geography,
                       ST_GeomFromText(:line, 4326)::geography
                   ) AS distance_m
            FROM signals
            WHERE ST_DWithin(
                geom::geography,
                ST_GeomFromText(:line, 4326)::geography,
                :buf
            )
            ORDER BY ST_LineLocatePoint(
                ST_GeomFromText(:line, 4326), geom
            ) ASC
            """
        )
        rows = (
            await db.execute(sql, {"line": line_wkt, "buf": body.signal_buffer_m})
        ).mappings().all()
        signals = [SignalOut(**dict(r)) for r in rows]
        # 신호등 1개당 cycle의 절반을 평균 대기시간으로 가정
        signal_delay = sum(
            int((s.cycle_time or 0) / 2) for s in signals if s.cycle_time
        )

    # 서울 TOPIS 실시간 도로 소통 보정 — driving 모드일 때만 의미 있음
    realtime: RealtimeTraffic | None = None
    adjusted_duration_ms = duration_ms
    if used_mode == "driving":
        try:
            summary = await fetch_realtime_summary()
            if summary.sample_count > 0:
                free_speed = 35.0
                factor = max(0.6, min(2.0, free_speed / summary.avg_speed_kmh))
                adjusted_duration_ms = int(duration_ms * factor)
                realtime = RealtimeTraffic(
                    sample_count=summary.sample_count,
                    avg_speed_kmh=summary.avg_speed_kmh,
                    timestamp=summary.timestamp,
                    congestion=summary.congestion,
                    eta_factor=round(factor, 2),
                )
        except (SeoulTopisError, Exception):
            realtime = None

    return RouteResponse(
        mode=used_mode,
        origin=GeocodeOut(
            lat=origin.lat,
            lng=origin.lng,
            address=origin.address,
            road_address=origin.road_address,
        ),
        destination=GeocodeOut(
            lat=destination.lat,
            lng=destination.lng,
            address=destination.address,
            road_address=destination.road_address,
        ),
        duration_ms=adjusted_duration_ms,
        distance_m=distance_m,
        path=[[lat, lng] for lat, lng in path_pairs],
        signals=signals,
        signal_delay_estimate_s=signal_delay,
        realtime_traffic=realtime,
    )
