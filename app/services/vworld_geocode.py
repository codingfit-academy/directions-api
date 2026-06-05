"""
VWorld 지오코딩 / 검색 API — 국토교통부 공간정보 오픈플랫폼.

학원·학생 프로젝트에서 가장 안정적인 한글 주소·장소명 검색 옵션.

엔드포인트:
  - Geocoder (주소 → 좌표): https://api.vworld.kr/req/address
  - Search   (키워드 → POI): https://api.vworld.kr/req/search

사용 흐름:
  1) Geocoder 로 도로명 주소 시도(type=ROAD)
  2) 실패 시 Geocoder 로 지번 주소 시도(type=PARCEL)
  3) 그래도 실패하면 Search API 로 장소명(POI) 검색

발급: https://www.vworld.kr → 회원가입(개인) → 인증키 발급
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import VWORLD_API_KEY

GEOCODER_URL = "https://api.vworld.kr/req/address"
SEARCH_URL = "https://api.vworld.kr/req/search"


class VWorldError(RuntimeError):
    pass


@dataclass
class VWorldGeocodeResult:
    lat: float
    lng: float
    address: str


async def _geocoder(
    client: httpx.AsyncClient, query: str, addr_type: str
) -> VWorldGeocodeResult | None:
    """VWorld Geocoder. addr_type: 'ROAD'(도로명) 또는 'PARCEL'(지번)."""
    params = {
        "service": "address",
        "request": "getcoord",
        "version": "2.0",
        "crs": "epsg:4326",
        "address": query,
        "format": "json",
        "type": addr_type,
        "key": VWORLD_API_KEY,
    }
    res = await client.get(GEOCODER_URL, params=params)
    if res.status_code != 200:
        return None
    try:
        payload = res.json()
    except ValueError:
        return None

    response = payload.get("response") or {}
    status = response.get("status")
    if status != "OK":
        return None
    result = response.get("result") or {}
    point = result.get("point") or {}
    try:
        lng = float(point.get("x"))
        lat = float(point.get("y"))
    except (TypeError, ValueError):
        return None
    return VWorldGeocodeResult(
        lat=lat, lng=lng, address=result.get("text") or query
    )


async def _search(
    client: httpx.AsyncClient, query: str
) -> VWorldGeocodeResult | None:
    """VWorld Search (POI). 장소명/키워드 검색."""
    params = {
        "service": "search",
        "request": "search",
        "version": "2.0",
        "crs": "EPSG:4326",
        "query": query,
        "type": "PLACE",
        "size": "1",
        "page": "1",
        "format": "json",
        "errorformat": "json",
        "key": VWORLD_API_KEY,
    }
    res = await client.get(SEARCH_URL, params=params)
    if res.status_code != 200:
        return None
    try:
        payload = res.json()
    except ValueError:
        return None

    response = payload.get("response") or {}
    status = response.get("status")
    if status != "OK":
        return None
    items = (response.get("result") or {}).get("items") or []
    if not items:
        return None
    top = items[0]
    point = top.get("point") or {}
    try:
        lng = float(point.get("x"))
        lat = float(point.get("y"))
    except (TypeError, ValueError):
        return None
    return VWorldGeocodeResult(
        lat=lat,
        lng=lng,
        address=top.get("address", {}).get("road")
        or top.get("address", {}).get("parcel")
        or top.get("title")
        or query,
    )


async def geocode(query: str, timeout: float = 10.0) -> VWorldGeocodeResult:
    """주소 + 장소명 통합 지오코딩. ROAD → PARCEL → POI Search 순으로 시도."""
    if not query or not query.strip():
        raise VWorldError("검색어가 비어 있습니다.")
    if not VWORLD_API_KEY:
        raise VWorldError(
            "app/config.py 의 VWORLD_API_KEY 가 비어 있습니다. "
            "vworld.kr 에서 인증키를 발급받아 채워주세요."
        )

    async with httpx.AsyncClient(timeout=timeout) as client:
        for addr_type in ("ROAD", "PARCEL"):
            r = await _geocoder(client, query, addr_type)
            if r:
                return r
        r = await _search(client, query)
        if r:
            return r

    raise VWorldError(f"VWorld에서 '{query}' 위치를 찾지 못했습니다.")


async def _search_multi(
    client: httpx.AsyncClient, query: str, size: int
) -> list[VWorldGeocodeResult]:
    """Search API 다중 결과."""
    params = {
        "service": "search",
        "request": "search",
        "version": "2.0",
        "crs": "EPSG:4326",
        "query": query,
        "type": "PLACE",
        "size": str(size),
        "page": "1",
        "format": "json",
        "errorformat": "json",
        "key": VWORLD_API_KEY,
    }
    res = await client.get(SEARCH_URL, params=params)
    if res.status_code != 200:
        return []
    try:
        payload = res.json()
    except ValueError:
        return []

    response = payload.get("response") or {}
    if response.get("status") != "OK":
        return []
    items = (response.get("result") or {}).get("items") or []
    out: list[VWorldGeocodeResult] = []
    for it in items:
        point = it.get("point") or {}
        try:
            lng = float(point.get("x"))
            lat = float(point.get("y"))
        except (TypeError, ValueError):
            continue
        addr = it.get("address") or {}
        title = (it.get("title") or "").strip()
        addr_text = (addr.get("road") or addr.get("parcel") or "").strip()
        if title and addr_text:
            label = f"{title} ({addr_text})"
        else:
            label = title or addr_text or "(이름 없음)"
        out.append(VWorldGeocodeResult(lat=lat, lng=lng, address=label))
    return out


async def search_candidates(
    query: str, limit: int = 5, timeout: float = 10.0
) -> list[VWorldGeocodeResult]:
    """주소 1건 + POI N건 형태로 후보 목록을 반환한다."""
    if not query or not query.strip():
        raise VWorldError("검색어가 비어 있습니다.")
    if not VWORLD_API_KEY:
        raise VWorldError("VWORLD_API_KEY 가 비어 있습니다.")

    candidates: list[VWorldGeocodeResult] = []
    seen_keys: set[tuple[float, float]] = set()

    def _add(r: VWorldGeocodeResult) -> None:
        key = (round(r.lat, 5), round(r.lng, 5))
        if key in seen_keys:
            return
        seen_keys.add(key)
        candidates.append(r)

    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1) 정확한 주소 매칭 시도 (도로명 → 지번)
        for addr_type in ("ROAD", "PARCEL"):
            r = await _geocoder(client, query, addr_type)
            if r:
                _add(r)
        # 2) POI 다중 결과 추가
        pois = await _search_multi(client, query, max(1, limit))
        for p in pois:
            if len(candidates) >= limit:
                break
            _add(p)

    return candidates[:limit]
