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
