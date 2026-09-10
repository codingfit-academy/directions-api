"""
경찰청 교차로기반정보서비스 API 클라이언트.

- 서비스 URL: http://apis.data.go.kr/1320000/CrossRoadInfoService/getCrossRoadInfoList
- 응답은 최상위 배열 형태: [{메타데이터}, {item1}, {item2}, ...]
  메타데이터에 resultCode/resultMsg/totalCount/pageNo/totPage/numOfRows 가 들어있다.
- X_COORD / Y_COORD 는 WGS84 좌표를 정수로(×10^7) 표현한다.
  예: Y_COORD=374915430 → 37.4915430°N, X_COORD=1270306860 → 127.0306860°E
- 서울 자료만 제공되므로 srchCTid는 서울 지역코드를 고정값으로 쓸 수 있다.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx

from ..config import (
    DATA_GO_KR_SERVICE_KEY,
    SEOUL_BBOX_MAX_LAT,
    SEOUL_BBOX_MAX_LNG,
    SEOUL_BBOX_MIN_LAT,
    SEOUL_BBOX_MIN_LNG,
)

BASE_URL = "http://apis.data.go.kr/1320000/CrossRoadInfoService/getCrossRoadInfoList"

# 신호 주기(요일별/시간대별 신호계획)를 주는 별도 서비스. 같은 경찰청 제공이지만
# 공공데이터포털에서 **따로 활용신청**을 해야 하고, 승인 전에는 같은 인증키로도
# SERVICE_KEY_IS_NOT_REGISTERED_ERROR가 난다.
PLAN_BASE_URL = "https://apis.data.go.kr/1320000/PlanCrossRoadInfoService/getPlanCROPInfo"


@dataclass
class CrossRoad:
    region_cd: str
    int_no: str
    int_nm: str
    x_coord: float  # WGS84 경도(°)
    y_coord: float  # WGS84 위도(°)


class DataGoKrError(RuntimeError):
    pass


def _service_key() -> str:
    if not DATA_GO_KR_SERVICE_KEY:
        raise DataGoKrError(
            "app/config.py 의 DATA_GO_KR_SERVICE_KEY 가 비어 있습니다. "
            "공공데이터포털에서 발급받은 일반 인증키(Decoding)를 설정하세요."
        )
    return DATA_GO_KR_SERVICE_KEY


def _to_float(v: object) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coord_int_to_deg(v: object) -> Optional[float]:
    """degrees × 10^7 정수 문자열을 도(°) 단위 float로 변환."""
    f = _to_float(v)
    if f is None:
        return None
    return f / 1e7


async def fetch_crossroads(
    region_cd: Optional[str] = None,
    page_size: int = 1000,
    timeout: float = 30.0,
) -> AsyncIterator[CrossRoad]:
    """페이지네이션을 돌며 교차로 목록을 yield 한다."""
    service_key = _service_key()
    page_no = 1

    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            params: dict[str, str | int] = {
                "serviceKey": service_key,
                "pageNo": page_no,
                "numOfRows": page_size,
                "type": "json",
            }
            if region_cd:
                params["srchCTid"] = region_cd

            res = await client.get(BASE_URL, params=params)
            res.raise_for_status()

            try:
                payload = res.json()
            except ValueError as exc:
                raise DataGoKrError(
                    f"응답을 JSON으로 파싱할 수 없습니다 (인증키 오류일 수 있음): {res.text[:200]}"
                ) from exc

            # 새 응답 형식: [{메타데이터}, {item1}, {item2}, ...]
            if not isinstance(payload, list) or not payload:
                raise DataGoKrError(
                    f"예상치 못한 응답 형식입니다: {str(payload)[:200]}"
                )

            meta = payload[0] if isinstance(payload[0], dict) else {}
            result_code = str(meta.get("resultCode") or "").strip()
            if result_code not in ("00", "0", ""):
                raise DataGoKrError(
                    f"공공 API 오류: code={result_code} msg={meta.get('resultMsg')}"
                )

            items = [it for it in payload[1:] if isinstance(it, dict)]

            for it in items:
                x = _coord_int_to_deg(it.get("X_COORD") or it.get("x_coord"))
                y = _coord_int_to_deg(it.get("Y_COORD") or it.get("y_coord"))
                int_no = str(it.get("INT_NO") or it.get("int_no") or "").strip()
                if x is None or y is None or not int_no:
                    continue
                yield CrossRoad(
                    region_cd=str(it.get("REGION_CD") or it.get("region_cd") or "").strip(),
                    int_no=int_no,
                    int_nm=str(it.get("INT_NM") or it.get("int_nm") or "").strip(),
                    x_coord=x,
                    y_coord=y,
                )

            total = int(meta.get("totalCount") or 0)
            if page_no * page_size >= total or not items:
                break
            page_no += 1


# ── 서울 교차로 목록 메모리 캐시 ────────────────────────────────
# 전체가 388건(2026-09 기준)이라 통째로 들고 있어도 부담이 없다. 정지 구간을
# 신호등으로 확인해줄 때마다 공공 API를 다시 부르지 않기 위한 캐시.
_CACHE_TTL_S = 60 * 60 * 12
_cache: list["CrossRoad"] = []
_cache_at: float = 0.0


def is_in_seoul(lat: float, lng: float) -> bool:
    """경찰청 신호 데이터가 서울만 제공되므로, 그 밖이면 조회 자체를 건너뛴다."""
    return (
        SEOUL_BBOX_MIN_LAT <= lat <= SEOUL_BBOX_MAX_LAT
        and SEOUL_BBOX_MIN_LNG <= lng <= SEOUL_BBOX_MAX_LNG
    )


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


async def load_seoul_crossroads(force: bool = False) -> list[CrossRoad]:
    """서울 교차로 전체 목록을 캐시와 함께 반환한다."""
    global _cache, _cache_at
    if not force and _cache and (time.time() - _cache_at) < _CACHE_TTL_S:
        return _cache

    items = [cr async for cr in fetch_crossroads()]
    if items:
        _cache = items
        _cache_at = time.time()
    return items


async def find_nearest_crossroad(
    lat: float, lng: float, radius_m: float
) -> Optional[tuple[CrossRoad, float]]:
    """
    좌표에서 radius_m 안의 가장 가까운 교차로를 (교차로, 거리m)로 반환한다.
    서울 밖이거나 반경 안에 없으면 None.
    """
    if not is_in_seoul(lat, lng):
        return None

    crossroads = await load_seoul_crossroads()
    best: Optional[tuple[CrossRoad, float]] = None
    for cr in crossroads:
        d = _haversine_m(lat, lng, cr.y_coord, cr.x_coord)
        if d <= radius_m and (best is None or d < best[1]):
            best = (cr, d)
    return best


async def fetch_signal_cycle_time(
    int_no: str, int_nm: str = "", timeout: float = 15.0
) -> Optional[int]:
    """
    교차로계획정보서비스에서 해당 교차로의 신호 주기(초)를 가져온다.

    이 서비스는 별도 활용신청이 필요해서 승인 전에는 인증키가 등록되지 않았다는
    응답이 온다. 주기를 모르는 것 자체는 치명적이지 않으므로(대기시간은 기본값
    ETA_DEFAULT_WAIT_PER_STOP_S로 폴백) 실패하면 조용히 None을 반환한다.

    TODO: 활용신청 승인 후 실제 응답으로 필드명을 확인할 것 — 지금은 주기로 보이는
    키(CYCLE 계열)를 관대하게 훑는 방식이라, 승인 뒤 한 번 검증이 필요하다.
    """
    if not DATA_GO_KR_SERVICE_KEY:
        return None

    # 이 서비스의 검색 조건은 교차로 "이름"(srchCRNm)이다 — 교차로번호로는 못 거른다.
    # 그래서 이름으로 좁힌 뒤, 응답 안에서 INT_NO가 일치하는 항목을 우선 고른다.
    params = {
        "serviceKey": DATA_GO_KR_SERVICE_KEY,
        "pageNo": 1,
        "numOfRows": 50,
        "type": "json",
    }
    if int_nm:
        params["srchCRNm"] = int_nm
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.get(PLAN_BASE_URL, params=params)
            res.raise_for_status()
            payload = res.json()
    except (httpx.HTTPError, ValueError):
        return None

    # 미등록 키 등 오류 응답은 dict(OpenAPI_ServiceResponse) 형태로 온다.
    if not isinstance(payload, list):
        return None

    items = [it for it in payload[1:] if isinstance(it, dict)]
    # 같은 이름의 교차로가 여러 개일 수 있어 INT_NO가 맞는 것을 우선한다.
    exact = [it for it in items if str(it.get("INT_NO") or "").strip() == int_no]

    for item in exact or items:
        for key, value in item.items():
            if "CYCLE" not in key.upper():
                continue
            cycle = _to_float(value)
            # 신호 주기는 보통 60~300초 범위. 벗어나면 다른 뜻의 필드로 보고 무시한다.
            if cycle is not None and 30 <= cycle <= 400:
                return int(cycle)
    return None
