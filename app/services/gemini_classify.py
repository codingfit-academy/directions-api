"""
여정 이름 분류 — Gemini(가장 저렴한 텍스트 모델)가 새로 들어온 기록 이름(예:
"출근길", "헬스장 가는 길")을 관리자가 미리 만들어 둔 분류(ThumbnailCategory)
중 가장 의미가 비슷한 것으로 매칭한다. 매칭되면 그 분류의 이미지가 카드
썸네일로 쓰인다.

트리거: 사용자가 "기록하기"로 새 이름의 기록을 시작할 때(POST /gps/trips) —
gps.py가 이 모듈의 [classify_and_cache]를 백그라운드 작업으로 예약한다. 응답을
막지 않으려고 백그라운드로 돌리고(ST-DBSCAN 정지 구간 처리와 같은 패턴), 이미
분류된 이름이면 그냥 조용히 끝낸다(재시도 비용 없음).
"""
from __future__ import annotations

from typing import Optional

from google.genai import types
from pydantic import BaseModel
from sqlalchemy import select

from ..config import GEMINI_CHAT_MODEL
from ..database import SessionLocal
from ..models import JourneyThumbnail, ThumbnailCategory
from .gemini_client import GeminiUnavailableError, get_client


class _ClassificationResult(BaseModel):
    # 분류 목록 중 정확히 일치하는 이름, 또는 어울리는 게 없으면 비워둔다.
    category_name: Optional[str] = None


_SYSTEM_INSTRUCTION = (
    "사용자가 기록한 이동 경로의 이름을 보고, 주어진 분류 목록 중 의미가 가장 "
    "비슷한 것을 정확히 하나만 고르세요. 목록에 있는 이름을 토씨 하나 틀리지 "
    "않고 그대로 반환해야 합니다. 어울리는 분류가 전혀 없으면 category_name을 "
    "비워두세요(억지로 아무거나 고르지 마세요)."
)


async def classify_journey_label(label: str, category_names: list[str]) -> Optional[str]:
    """[category_names] 중 정확히 일치하는 이름 하나 또는 None을 반환한다."""
    if not category_names:
        return None

    client = get_client()  # 키 없으면 GeminiUnavailableError.
    prompt = f'기록 이름: "{label}"\n분류 목록: {", ".join(category_names)}'

    response = await client.aio.models.generate_content(
        model=GEMINI_CHAT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=_ClassificationResult,
            temperature=0.1,
        ),
    )

    result = response.parsed
    if not isinstance(result, _ClassificationResult) or not result.category_name:
        return None
    # Gemini가 철자를 살짝 바꿔 돌려줄 수 있으니, 목록에 정확히 있는 것만 신뢰한다.
    return result.category_name if result.category_name in category_names else None


async def classify_and_cache(user_id: int, label: str) -> None:
    """
    이미 분류돼 있으면 아무 것도 하지 않는다. 아니면 Gemini로 분류해 결과를
    [JourneyThumbnail]에 저장한다 — 어울리는 분류가 없거나 Gemini를 못 쓰는
    상황(키 없음/일시 오류)이어도 최소한 "시도는 해봤다"는 행은 남기지 않는다
    (GEMINI_API_KEY를 나중에 채워 넣으면 다음 기록 때 다시 시도되도록).
    """
    async with SessionLocal() as db:
        existing = (
            await db.execute(
                select(JourneyThumbnail).where(
                    JourneyThumbnail.user_id == user_id,
                    JourneyThumbnail.label == label,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return

        categories = (await db.execute(select(ThumbnailCategory))).scalars().all()
        if not categories:
            return  # 관리자가 아직 분류를 하나도 안 만들었으면 시도할 이유가 없다.

        try:
            matched_name = await classify_journey_label(label, [c.name for c in categories])
        except GeminiUnavailableError:
            return

        matched = next((c for c in categories if c.name == matched_name), None)
        db.add(
            JourneyThumbnail(
                user_id=user_id,
                label=label,
                category_id=matched.id if matched else None,
            )
        )
        await db.commit()
