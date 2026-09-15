"""
여정 채팅 — Gemini Flash가 사용자의 자연어 "몇 시까지 도착해야 해요" 같은 말에서
목표 도착 시각만 뽑아낸다.

**ETA 예측(app/services/eta_model.py)은 여기서 부르지 않는다** — "AI 분석"은
새 기록이 쌓여서 사용자가 직접 "AI 분석 시작하기"를 눌렀을 때만(POST /eta/train)
실행되어야 하는 무거운 작업이고, 채팅에서 매 메시지마다 곁다리로 실행할 일이
아니다. 이 채팅이 하는 일은 자연어 → 구조화된 시각 추출, 그게 전부다. 알람
시각 계산이 필요하면 앱이 여기서 받은 target_arrival_at으로 기존
GET /eta/predict를 따로 호출하면 된다(record_alarm_page.dart가 이미 그렇게 한다).

directions-flutter의 mock_journey_detail_page.dart(대화형 UI 디자인 시안)가 이
엔드포인트로 교체되어야 한다 — 지금 Flutter 쪽은 제안 칩 3개로만 진행되는 고정
스크립트이고, 자유 입력 칸은 이 기능이 생기기 전까지 빼둔 상태다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from google.genai import types
from pydantic import BaseModel

from ..config import GEMINI_CHAT_MODEL
from .gemini_client import get_client

_KST = timezone(timedelta(hours=9))


class ChatTurn(BaseModel):
    """대화 기록 한 턴. 서버는 상태를 들고 있지 않으므로(무상태), 클라이언트가
    지금까지의 대화를 매 요청마다 함께 보낸다."""

    role: Literal["user", "model"]
    text: str


class _ExtractedIntent(BaseModel):
    """Gemini 구조화 출력 스키마 — 사용자 메시지에서 목표 도착 시각을 추출한다."""

    reply: str
    # 시각을 특정하지 못했으면 둘 다 비워둔다 (예: "언제까지인지 다시 알려주세요"만 답).
    target_arrival_hour: Optional[int] = None
    target_arrival_minute: Optional[int] = None


class ChatResult(BaseModel):
    reply: str
    target_arrival_at: Optional[datetime] = None


_SYSTEM_INSTRUCTION = (
    "당신은 도보 이동 알림 앱의 어시스턴트입니다. 사용자가 몇 시까지 도착해야 하는지 "
    "말하면 그 시각(24시간제, 한국 시각 기준)을 target_arrival_hour/minute로 정확히 "
    "추출하세요. '9시까지', '오전 9시', '저녁 7시 반'처럼 자연스러운 한국어 표현도 "
    "이해해야 합니다. 사용자가 아직 시각을 말하지 않았거나 모호하면 두 필드를 비워두고 "
    "reply에서 다시 물어보세요. 시각을 확인했으면 reply에서 그 시각을 그대로 되짚어 "
    "확인해주세요(구체적인 소요시간 예측이나 알람 값은 언급하지 마세요 — 그건 이 "
    "대화가 아니라 별도 화면에서 계산됩니다). reply는 한국어 존댓말로 한두 문장, "
    "이모지는 최대 1개만 사용하세요."
)


async def chat_about_journey(
    *,
    label: str,
    message: str,
    history: list[ChatTurn],
) -> ChatResult:
    """[label]은 대화에 맥락을 주기 위한 것일 뿐 — 예측 계산에는 쓰지 않는다."""
    client = get_client()  # 키가 없으면 GeminiUnavailableError — 라우터에서 503으로 변환.

    contents = [
        types.Content(role=turn.role, parts=[types.Part.from_text(text=turn.text)])
        for turn in history
    ]
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=message)]))

    response = await client.aio.models.generate_content(
        model=GEMINI_CHAT_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=f'{_SYSTEM_INSTRUCTION}\n\n지금 대화 중인 기록 이름: "{label}"',
            response_mime_type="application/json",
            response_schema=_ExtractedIntent,
            temperature=0.4,
        ),
    )

    intent = response.parsed
    if not isinstance(intent, _ExtractedIntent):
        # 구조화 출력 파싱에 실패하면(드묾) 원문 텍스트라도 그대로 돌려준다.
        return ChatResult(reply=response.text or "죄송해요, 다시 한 번 말씀해주시겠어요?")

    if intent.target_arrival_hour is None:
        return ChatResult(reply=intent.reply)

    now = datetime.now(_KST)
    hour = max(0, min(23, intent.target_arrival_hour))
    minute = max(0, min(59, intent.target_arrival_minute or 0))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)  # 이미 지난 시각이면 내일로 본다.

    return ChatResult(reply=intent.reply, target_arrival_at=target)
