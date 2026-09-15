"""
Gemini API(Flash, 가장 저렴한 모델) 공용 클라이언트.

여정 채팅(app/services/gemini_chat.py)과 여정 대표 이미지 생성
(app/services/gemini_image.py) 둘 다 이 모듈에서 클라이언트를 받아 쓴다.

SDK: google-genai (구글 공식 통합 SDK, 옛 google-generativeai의 후속).
"""
from __future__ import annotations

from google import genai

from ..config import GEMINI_API_KEY


class GeminiUnavailableError(RuntimeError):
    """GEMINI_API_KEY가 비어 있을 때. 라우터에서 503으로 변환한다."""


_client: genai.Client | None = None


def get_client() -> genai.Client:
    """설정된 API 키로 초기화된 Gemini 클라이언트를 반환한다.

    클라이언트 생성 자체는 네트워크 호출이 아니라 가벼운 설정 객체라, 앱 전체에서
    하나만 만들어 재사용한다. 키가 없으면 여기서 바로 [GeminiUnavailableError]를
    던진다 — 호출부(라우터)가 이걸 잡아 503으로 응답하면 된다.
    """
    global _client
    if not GEMINI_API_KEY:
        raise GeminiUnavailableError(
            "GEMINI_API_KEY가 설정되어 있지 않습니다. "
            "https://aistudio.google.com/app/apikey 에서 발급받아 .env의 "
            "GEMINI_API_KEY에 추가하세요."
        )
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client
