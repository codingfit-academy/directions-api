"""
길찾기 라우터.

- GET  /directions/geocode?q=...      : 주소/장소명 → 좌표
- POST /directions/route              : 출발/도착(텍스트 또는 좌표) → 경로 + 주변 신호등
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import (
    GeocodeOut,
    RouteRequest,
    RouteResponse,
    SignalOut,
)
from ..services.naver_directions import (
    GeocodeResult,
    NaverDirectionsError,
    driving_route,
    geocode,
)

router = APIRouter(prefix="/directions", tags=["directions"])


async def _resolve(point, default_label: str) -> GeocodeResult:
    """RoutePoint를 좌표로 확정한다. lat/lng가 있으면 그대로 쓰고, 없으면 지오코딩."""
    if point.lat is not None and point.lng is not None:
        return GeocodeResult(
            lat=point.lat,
            lng=point.lng,
            address=(point.text or default_label),
            road_address=None,
        )
    try:
        return await geocode(point.text or "")
    except NaverDirectionsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/geocode", response_model=GeocodeOut)
async def geocode_endpoint(
    q: str = Query(..., min_length=1, description="검색어(주소/장소명)"),
):
    try:
        result = await geocode(q)
    except NaverDirectionsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return GeocodeOut(
        lat=result.lat,
        lng=result.lng,
        address=result.address,
        road_address=result.road_address,
    )


@router.post("/route", response_model=RouteResponse)
async def route_endpoint(
    body: RouteRequest, db: AsyncSession = Depends(get_db)
):
    origin = await _resolve(body.origin, "출발지")
    destination = await _resolve(body.destination, "도착지")

    try:
        route = await driving_route(
            origin.lat, origin.lng, destination.lat, destination.lng, option=body.option
        )
    except NaverDirectionsError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    path_pairs = route.path  # [(lat, lng), ...]

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

    return RouteResponse(
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
        duration_ms=route.duration_ms,
        distance_m=route.distance_m,
        path=[[lat, lng] for lat, lng in path_pairs],
        signals=signals,
        signal_delay_estimate_s=signal_delay,
    )
