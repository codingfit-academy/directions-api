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

import asyncio
import math
import time
from statistics import median
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx

from ..config import (
    DATA_GO_KR_PLAN_SERVICE_KEY,
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
    page_size: int = 100,
    timeout: float = 30.0,
) -> AsyncIterator[CrossRoad]:
    """페이지네이션을 돌며 교차로 목록을 yield 한다."""
    service_key = _service_key()
    page_no = 1
    fetched = 0

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

            # 서버가 numOfRows를 100으로 깎아서 주므로, 요청한 page_size가 아니라
            # 실제로 받은 건수를 누적해서 종료를 판단해야 한다 (이걸 요청값으로
            # 계산하면 1페이지만 읽고 끝나 388건 중 100건만 적재된다).
            fetched += len(items)
            total = int(meta.get("totalCount") or 0)
            if not items or (total and fetched >= total):
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


# ── 신호 주기(교차로계획정보서비스) ─────────────────────────────
# 실제 응답 확인 결과(2026-09-10, 활용신청 승인 후):
#   INT_OPER_CYCLE_VAL : 신호 주기(초). 0은 "해당 계획에 주기 없음"이라 버린다.
#   OPER_PLAN_HH/MI    : 이 계획이 적용되는 시각 — 같은 교차로도 시간대별로 주기가 다르다.
#   INT_NO / INT_NM    : 교차로기반정보서비스(위치)의 INT_NO와 그대로 맞물린다
#                        (실측: 위치 398개 중 351개가 주기 데이터를 가짐).
#   A_RING_n / B_RING_n_PHASE_VAL : 링별 현시 시간 (지금은 사용하지 않음)
#
# srchCRNo/srchCRNm 같은 검색 조건은 서버가 무시하고 항상 1페이지를 돌려준다.
# 그래서 특정 교차로만 콕 집어 조회할 수 없고, 전체(약 62,000행 / 621페이지)를
# 한 번 훑어 교차로별 대표 주기로 집계해 두는 방식을 쓴다.
_PLAN_PAGE_SIZE = 100
_PLAN_MAX_PAGES = 700  # 실측 621페이지 + 여유
_plan_cache: dict[str, int] = {}
_plan_cache_at: float = 0.0


async def _fetch_plan_page(client: httpx.AsyncClient, page_no: int) -> list[dict]:
    params = {
        "serviceKey": DATA_GO_KR_PLAN_SERVICE_KEY,
        "pageNo": page_no,
        "numOfRows": _PLAN_PAGE_SIZE,
        "type": "json",
    }
    res = await client.get(PLAN_BASE_URL, params=params)
    res.raise_for_status()
    payload = res.json()
    # 미등록 키 등 오류는 dict(OpenAPI_ServiceResponse)로 온다.
    if not isinstance(payload, list) or not payload:
        raise DataGoKrError(
            "교차로계획정보서비스 응답이 올바르지 않습니다 "
            "(활용신청 승인 여부/인증키를 확인하세요): "
            f"{str(payload)[:200]}"
        )
    return [it for it in payload[1:] if isinstance(it, dict)]


async def fetch_signal_cycles(concurrency: int = 8) -> dict[str, int]:
    """
    서울 전체 교차로의 대표 신호 주기를 {INT_NO: 주기초}로 반환한다.

    한 교차로에 시간대별 계획이 여러 개 있어서, 0을 제외한 주기들의 중앙값을
    대표값으로 쓴다 (첨두시/비첨두시 편차를 한 값으로 요약).
    """
    cycles: dict[str, list[int]] = {}

    async with httpx.AsyncClient(timeout=40.0) as client:
        first = await _fetch_plan_page(client, 1)
        semaphore = asyncio.Semaphore(concurrency)

        def collect(items: list[dict]) -> None:
            for it in items:
                int_no = str(it.get("INT_NO") or "").strip()
                if not int_no:
                    continue
                cycle = _to_float(it.get("INT_OPER_CYCLE_VAL"))
                if cycle and cycle > 0:
                    cycles.setdefault(int_no, []).append(int(cycle))

        collect(first)

        async def worker(page_no: int) -> list[dict]:
            async with semaphore:
                try:
                    return await _fetch_plan_page(client, page_no)
                except (httpx.HTTPError, ValueError, DataGoKrError):
                    return []

        page_no = 2
        while page_no <= _PLAN_MAX_PAGES:
            batch = list(range(page_no, min(page_no + concurrency * 4, _PLAN_MAX_PAGES + 1)))
            results = await asyncio.gather(*(worker(p) for p in batch))
            for items in results:
                collect(items)
            # 빈 페이지가 나오면 끝까지 읽은 것으로 본다.
            if any(len(items) == 0 for items in results):
                break
            page_no = batch[-1] + 1

    return {no: int(median(vals)) for no, vals in cycles.items() if vals}


async def load_signal_cycles(force: bool = False) -> dict[str, int]:
    """교차로별 대표 주기를 캐시와 함께 반환한다 (전체 스캔이라 비용이 크다)."""
    global _plan_cache, _plan_cache_at
    if not force and _plan_cache and (time.time() - _plan_cache_at) < _CACHE_TTL_S:
        return _plan_cache

    cycles = await fetch_signal_cycles()
    if cycles:
        _plan_cache = cycles
        _plan_cache_at = time.time()
    return cycles


def get_cached_signal_cycle(int_no: str) -> Optional[int]:
    """
    이미 메모리에 올라와 있는 주기만 조회한다 (없으면 None).

    전체 스캔은 수백 번의 외부 호출이라 사용자 요청 중에 하면 안 된다. 그래서
    라벨링 같은 실시간 경로에서는 캐시 히트만 노리고, 캐시는 신호등 적재
    (POST /signals/ingest/police)나 첫 워밍업 때 채운다.
    """
    return _plan_cache.get(int_no)
