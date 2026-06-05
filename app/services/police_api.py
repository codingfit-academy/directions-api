"""
경찰청 교차로기반정보서비스 API 클라이언트.

- 서비스 URL: http://apis.data.go.kr/1320000/CrossRoadInfoService/getCrossRoadInfoList
- 응답 좌표는 EPSG:5179 (Korea 2000 / Unified TM) 기준으로
  일반 회입되므로 PostGIS 적재 시 ST_Transform으로 WGS84로 변환한다.
- 서울 자료만 제공되므로 srchCTid는 서울 지역코드를 고정값으로 쓸 수 있다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx

from ..config import DATA_GO_KR_SERVICE_KEY

BASE_URL = "http://apis.data.go.kr/1320000/CrossRoadInfoService/getCrossRoadInfoList"


@dataclass
class CrossRoad:
    region_cd: str
    int_no: str
    int_nm: str
    x_coord: float  # EPSG:5179 X
    y_coord: float  # EPSG:5179 Y


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

            response = payload.get("response", {})
            header = response.get("header", {})
            result_code = header.get("resultCode")
            if result_code not in ("00", "0", None):
                raise DataGoKrError(
                    f"공공 API 오류: code={result_code} msg={header.get('resultMsg')}"
                )

            body = response.get("body") or {}
            items_wrap = body.get("items") or {}
            items = items_wrap.get("item") if isinstance(items_wrap, dict) else items_wrap
            if items is None:
                items = []
            if isinstance(items, dict):
                items = [items]

            for it in items:
                x = _to_float(it.get("X_COORD") or it.get("x_coord"))
                y = _to_float(it.get("Y_COORD") or it.get("y_coord"))
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

            total = int(body.get("totalCount") or 0)
            if page_no * page_size >= total or not items:
                break
            page_no += 1
