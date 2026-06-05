"""
서울 TOPIS 실시간 도로 소통 정보 (Seoul Open API).

데이터셋: TrafficInfo (실시간 도로 소통 / 5분 주기 갱신)
- 호출 형식: http://openapi.seoul.go.kr:8088/{KEY}/xml/TrafficInfo/{START}/{END}/{LINK_ID}
- ⚠️ JSON 미지원, XML 응답만 가능
- LINK_ID 인자 필수 — 도로 링크 ID당 한 번씩 호출

응답 예:
  <TrafficInfo>
    <list_total_count>1</list_total_count>
    <RESULT><CODE>INFO-000</CODE><MESSAGE>정상 처리되었습니다</MESSAGE></RESULT>
    <row>
      <link_id>1220003800</link_id>
      <prcs_spd>14</prcs_spd>           ← km/h
      <prcs_trv_time>771</prcs_trv_time> ← 통행시간(초)
    </row>
  </TrafficInfo>

이 모듈은 config 에 정의된 LINK_ID 목록을 모두 조회해 평균 속도를 계산합니다.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional
from xml.etree import ElementTree as ET

import httpx

from ..config import SEOUL_OPENAPI_DATASET, SEOUL_OPENAPI_KEY, SEOUL_TOPIS_LINK_IDS

BASE_URL = "http://openapi.seoul.go.kr:8088"


class SeoulTopisError(RuntimeError):
    pass


@dataclass
class LinkSpeedSample:
    link_id: str
    speed_kmh: float
    travel_time_s: int


@dataclass
class TrafficSummary:
    sample_count: int
    avg_speed_kmh: float
    timestamp: Optional[str]
    congestion: str  # "원활" / "서행" / "지정체" / "정체"


def _congestion_label(speed_kmh: float) -> str:
    if speed_kmh >= 30:
        return "원활"
    if speed_kmh >= 20:
        return "서행"
    if speed_kmh >= 10:
        return "지정체"
    return "정체"


def _parse_link_ids() -> list[str]:
    return [s.strip() for s in SEOUL_TOPIS_LINK_IDS.split(",") if s.strip()]


def _parse_xml_row(xml_text: str) -> Optional[LinkSpeedSample]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SeoulTopisError(f"XML 파싱 실패: {exc}") from exc

    # 오류 응답은 최상위 <response> 또는 <RESULT> 아래 CODE
    result = root.find("RESULT")
    if result is not None:
        code_el = result.find("CODE")
        code = (code_el.text or "").strip() if code_el is not None else ""
        if code and code != "INFO-000":
            msg_el = result.find("MESSAGE")
            msg = msg_el.text if msg_el is not None else ""
            raise SeoulTopisError(f"Seoul API 오류: {code} {msg}")

    row = root.find("row")
    if row is None:
        return None

    def _txt(tag: str) -> Optional[str]:
        el = row.find(tag)
        return el.text if el is not None and el.text is not None else None

    link_id = _txt("link_id") or ""
    spd_s = _txt("prcs_spd")
    tt_s = _txt("prcs_trv_time")
    if not link_id or not spd_s:
        return None
    try:
        spd = float(spd_s)
        tt = int(float(tt_s)) if tt_s else 0
    except ValueError:
        return None
    if spd <= 0:
        return None
    return LinkSpeedSample(link_id=link_id, speed_kmh=spd, travel_time_s=tt)


async def _fetch_link(
    client: httpx.AsyncClient, link_id: str
) -> Optional[LinkSpeedSample]:
    if not SEOUL_OPENAPI_KEY:
        raise SeoulTopisError(
            "app/config.py 의 SEOUL_OPENAPI_KEY 가 비어 있습니다."
        )
    url = (
        f"{BASE_URL}/{SEOUL_OPENAPI_KEY}/xml/{SEOUL_OPENAPI_DATASET}"
        f"/1/1/{link_id}"
    )
    res = await client.get(url)
    if res.status_code != 200:
        raise SeoulTopisError(f"HTTP {res.status_code}: {res.text[:200]}")
    return _parse_xml_row(res.text)


async def fetch_realtime_summary(
    link_ids: Optional[list[str]] = None, timeout: float = 10.0
) -> TrafficSummary:
    """설정된 LINK_ID 목록을 병렬 조회해 평균 속도 요약을 반환한다."""
    ids = link_ids or _parse_link_ids()
    if not ids:
        return TrafficSummary(0, 0.0, None, "원활")

    async with httpx.AsyncClient(timeout=timeout) as client:
        results = await asyncio.gather(
            *(_fetch_link(client, lid) for lid in ids),
            return_exceptions=True,
        )

    samples: list[LinkSpeedSample] = []
    for r in results:
        if isinstance(r, LinkSpeedSample):
            samples.append(r)
        # 일부 링크가 에러여도 나머지로 평균 계산

    if not samples:
        return TrafficSummary(0, 0.0, None, "원활")

    avg = sum(s.speed_kmh for s in samples) / len(samples)
    return TrafficSummary(
        sample_count=len(samples),
        avg_speed_kmh=round(avg, 1),
        timestamp=None,  # TrafficInfo 응답에 시간 필드 없음
        congestion=_congestion_label(avg),
    )
