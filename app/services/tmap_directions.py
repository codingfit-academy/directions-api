"""
T-map 보행자 경로안내 API (SK Telecom Open API).

엔드포인트: POST https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1
헤더: appKey: {TMAP_API_KEY}
바디 (JSON):
  startX (lng), startY (lat), endX (lng), endY (lat),
  startName, endName, reqCoordType=WGS84GEO, resCoordType=WGS84GEO

응답: GeoJSON FeatureCollection
  - 각 Feature 의 properties.totalDistance / totalTime 이 첫 Point Feature 에 들어 있음
  - LineString Feature 의 coordinates 가 폴리라인 (lng, lat 순)

발급: https://openapi.sk.com → 가입 → 앱 등록 → "T-map 보행자 경로안내" API 추가
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import TMAP_API_KEY

PEDESTRIAN_URL = "https://apis.openapi.sk.com/tmap/routes/pedestrian"


class TmapError(RuntimeError):
    pass


@dataclass
class PedestrianRoute:
    duration_s: int
    distance_m: int
    path: list[tuple[float, float]]  # [(lat, lng), ...]


async def pedestrian_route(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    origin_name: str = "출발지",
    dest_name: str = "도착지",
    timeout: float = 15.0,
) -> PedestrianRoute:
    if not TMAP_API_KEY:
        raise TmapError(
            "app/config.py 의 TMAP_API_KEY 가 비어 있습니다. "
            "https://openapi.sk.com 에서 발급받아 채워주세요."
        )

    headers = {"appKey": TMAP_API_KEY, "Content-Type": "application/json"}
    body = {
        "startX": str(origin_lng),
        "startY": str(origin_lat),
        "endX": str(dest_lng),
        "endY": str(dest_lat),
        "startName": origin_name,
        "endName": dest_name,
        "reqCoordType": "WGS84GEO",
        "resCoordType": "WGS84GEO",
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.post(
            PEDESTRIAN_URL,
            headers=headers,
            params={"version": "1", "format": "json"},
            json=body,
        )

    if res.status_code == 401 or res.status_code == 403:
        raise TmapError(
            f"T-map 인증 실패 ({res.status_code}) — "
            f"appKey 또는 'T-map 보행자 경로안내' API 활성화 여부 확인."
        )
    if res.status_code != 200:
        raise TmapError(f"T-map 실패: HTTP {res.status_code} {res.text[:200]}")

    try:
        payload = res.json()
    except ValueError as exc:
        raise TmapError(f"T-map 응답 파싱 실패: {res.text[:200]}") from exc

    features = payload.get("features") or []
    if not features:
        raise TmapError("T-map 응답에 features가 없습니다.")

    # 첫 Point feature 에 totalDistance / totalTime 들어있음
    total_distance = 0
    total_time = 0
    for f in features:
        props = f.get("properties") or {}
        if "totalDistance" in props:
            total_distance = int(props.get("totalDistance") or 0)
        if "totalTime" in props:
            total_time = int(props.get("totalTime") or 0)
        if total_distance and total_time:
            break

    # LineString 좌표들을 차례로 모아 폴리라인 구성
    path: list[tuple[float, float]] = []
    for f in features:
        geom = f.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        coords = geom.get("coordinates") or []
        for c in coords:
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                lng, lat = float(c[0]), float(c[1])
                path.append((lat, lng))

    if not path:
        raise TmapError("T-map 응답에 경로 좌표가 없습니다.")

    return PedestrianRoute(
        duration_s=total_time, distance_m=total_distance, path=path
    )
