"""
Kakao Local API — NCP Geocoding 대체용.

주소 검색이 안 되면 키워드(장소명) 검색으로 한 번 더 시도합니다.
KAKAO_REST_API_KEY 가 비어 있으면 KakaoLocalError 가 발생하므로
호출자가 적절히 폴백할 수 있도록 설계되어 있습니다.

  https://developers.kakao.com/docs/latest/ko/local/dev-guide
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import KAKAO_REST_API_KEY

SEARCH_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"
SEARCH_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"


class KakaoLocalError(RuntimeError):
    pass


@dataclass
class KakaoGeocodeResult:
    lat: float
    lng: float
    address: str


async def geocode(query: str, timeout: float = 10.0) -> KakaoGeocodeResult:
    if not query or not query.strip():
        raise KakaoLocalError("검색어가 비어 있습니다.")
    if not KAKAO_REST_API_KEY:
        raise KakaoLocalError(
            "app/config.py 의 KAKAO_REST_API_KEY 가 비어 있습니다."
        )

    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}

    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1) 주소 검색
        addr_res = await client.get(
            SEARCH_ADDRESS_URL, headers=headers, params={"query": query}
        )
        if addr_res.status_code == 401:
            raise KakaoLocalError("Kakao 401 — REST API 키가 잘못되었거나 권한이 없습니다.")
        if addr_res.status_code == 200:
            docs = addr_res.json().get("documents") or []
            if docs:
                doc = docs[0]
                return KakaoGeocodeResult(
                    lat=float(doc["y"]),
                    lng=float(doc["x"]),
                    address=doc.get("address_name", query),
                )

        # 2) 키워드(장소명) 검색
        kw_res = await client.get(
            SEARCH_KEYWORD_URL, headers=headers, params={"query": query}
        )
        if kw_res.status_code == 200:
            docs = kw_res.json().get("documents") or []
            if docs:
                doc = docs[0]
                return KakaoGeocodeResult(
                    lat=float(doc["y"]),
                    lng=float(doc["x"]),
                    address=doc.get("address_name") or doc.get("place_name", query),
                )

    raise KakaoLocalError(f"Kakao Local에서 '{query}' 위치를 찾지 못했습니다.")
