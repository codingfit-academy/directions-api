"""
Naver Cloud Platform Maps API 클라이언트.

- Geocoding API: 주소/장소명 → 좌표
  https://naveropenapi.apigw.ntruss.com/map-geocode/v2/geocode
- Directions 5 API: 출발/도착 좌표 → 자동차 경로
  https://naveropenapi.apigw.ntruss.com/map-direction/v1/driving

서버측에서만 호출되며, 자격 증명은 환경변수에서 읽습니다.
  NAVER_NCP_API_KEY_ID  (X-NCP-APIGW-API-KEY-ID)
  NAVER_NCP_API_KEY     (X-NCP-APIGW-API-KEY)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import httpx

GEOCODE_URL = "https://naveropenapi.apigw.ntruss.com/map-geocode/v2/geocode"
DRIVING_URL = "https://naveropenapi.apigw.ntruss.com/map-direction/v1/driving"


class NaverDirectionsError(RuntimeError):
    pass


@dataclass
class GeocodeResult:
    lat: float
    lng: float
    address: str
    road_address: Optional[str] = None


@dataclass
class RouteResult:
    duration_ms: int
    distance_m: int
    path: list[tuple[float, float]]  # [(lat, lng), ...]
    summary: dict


def _auth_headers() -> dict[str, str]:
    key_id = os.getenv("NAVER_NCP_API_KEY_ID", "").strip()
    key = os.getenv("NAVER_NCP_API_KEY", "").strip()
    if not key_id or not key:
        raise NaverDirectionsError(
            "NAVER_NCP_API_KEY_ID / NAVER_NCP_API_KEY 환경변수가 설정되어 있지 않습니다."
        )
    return {
        "X-NCP-APIGW-API-KEY-ID": key_id,
        "X-NCP-APIGW-API-KEY": key,
    }


async def geocode(query: str, timeout: float = 10.0) -> GeocodeResult:
    """주소/장소명을 WGS84 좌표로 변환한다."""
    if not query or not query.strip():
        raise NaverDirectionsError("검색어가 비어 있습니다.")

    headers = _auth_headers()
    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.get(GEOCODE_URL, headers=headers, params={"query": query})

    if res.status_code != 200:
        raise NaverDirectionsError(
            f"Geocode 실패: status={res.status_code} body={res.text[:200]}"
        )

    payload = res.json()
    addresses = payload.get("addresses") or []
    if not addresses:
        raise NaverDirectionsError(f"'{query}'에 해당하는 주소를 찾지 못했습니다.")

    top = addresses[0]
    try:
        lng = float(top["x"])
        lat = float(top["y"])
    except (KeyError, TypeError, ValueError) as exc:
        raise NaverDirectionsError(f"좌표 파싱 실패: {top}") from exc

    return GeocodeResult(
        lat=lat,
        lng=lng,
        address=top.get("jibunAddress") or top.get("roadAddress") or query,
        road_address=top.get("roadAddress"),
    )


async def driving_route(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    option: str = "trafast",
    timeout: float = 15.0,
) -> RouteResult:
    """Naver Directions 5(자동차) API로 경로를 조회한다."""
    headers = _auth_headers()
    params = {
        "start": f"{origin_lng},{origin_lat}",
        "goal": f"{dest_lng},{dest_lat}",
        "option": option,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.get(DRIVING_URL, headers=headers, params=params)

    if res.status_code != 200:
        raise NaverDirectionsError(
            f"Directions 실패: status={res.status_code} body={res.text[:200]}"
        )

    payload = res.json()
    code = payload.get("code")
    if code != 0:
        raise NaverDirectionsError(
            f"Directions API 오류: code={code} message={payload.get('message')}"
        )

    routes = (payload.get("route") or {}).get(option) or []
    if not routes:
        raise NaverDirectionsError("경로 결과가 비어 있습니다.")

    route = routes[0]
    summary = route.get("summary") or {}
    path_lnglat: list[list[float]] = route.get("path") or []
    path = [(float(p[1]), float(p[0])) for p in path_lnglat if len(p) >= 2]

    return RouteResult(
        duration_ms=int(summary.get("duration") or 0),
        distance_m=int(summary.get("distance") or 0),
        path=path,
        summary=summary,
    )
