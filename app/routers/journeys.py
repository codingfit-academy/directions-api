"""
여정(기록 이름) 단위 Gemini 기능 라우터.

- POST /journeys/chat              : 자연어에서 목표 도착 시각만 뽑아 답한다.
- GET  /journeys/{label}/thumbnail : 그 여정이 분류된 카테고리의 대표 이미지.

이미지 자체는 관리자가 미리 올려두고(app/routers/admin.py), Gemini는 새 기록
이름이 그 중 어디에 가장 가까운지 "분류"만 한다(app/services/gemini_classify.py) —
이미지를 직접 생성하지 않는다. Gemini 호출은 API 키가 클라이언트에 노출되면
안 되므로 항상 서버(여기)에서만 한다.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import get_db
from ..models import JourneyThumbnail, ThumbnailCategory, User
from ..schemas import JourneyChatIn, JourneyChatOut
from ..services.gemini_chat import ChatTurn, chat_about_journey
from ..services.gemini_client import GeminiUnavailableError
from ..services.r2_storage import R2ObjectNotFoundError, R2UnavailableError, download_image

router = APIRouter(prefix="/journeys", tags=["journeys"])


@router.post("/chat", response_model=JourneyChatOut)
async def chat(
    body: JourneyChatIn,
    # 로그인 게이트로만 쓴다(호출당 비용이 드는 외부 API라 아무나 못 부르게) —
    # current_user 값 자체는 안 쓴다.
    current_user: User = Depends(get_current_user),
):
    """
    사용자가 "9시까지 도착해야 해요" 처럼 자연어로 말하면 Gemini가 그 시각만 뽑아
    되돌려준다. **여기서 ETA를 예측하거나 알람을 계산하지 않는다** — "AI 분석"은
    새 기록이 쌓여 사용자가 직접 실행할 때만(POST /eta/train) 도는 무거운 작업이라,
    매 채팅 메시지마다 곁다리로 돌릴 일이 아니다. 시각이 정해지면 앱이 그
    target_arrival_at으로 기존 GET /eta/predict를 따로 호출해 알람을 계산한다
    (record_alarm_page.dart가 이미 그렇게 한다).
    """
    try:
        result = await chat_about_journey(
            label=body.label,
            message=body.message,
            history=[ChatTurn(role=t.role, text=t.text) for t in body.history],
        )
    except GeminiUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return JourneyChatOut(**result.model_dump())


@router.get("/{label}/thumbnail")
async def get_thumbnail(
    label: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    이 사용자의 이 기록 이름이 분류된 카테고리의 대표 이미지를 반환한다.

    분류는 기록을 처음 시작할 때(POST /gps/trips) 백그라운드로 이미 끝나 있어야
    한다 — 여기서는 그 결과를 읽기만 한다(추가 Gemini 호출 없음). 아직 분류가
    안 끝났거나(막 기록을 시작한 직후), 어울리는 분류가 없으면 404를 반환한다 —
    앱은 이때 기본 이미지/아이콘을 대신 보여주면 된다.
    """
    row = (
        await db.execute(
            select(ThumbnailCategory)
            .join(JourneyThumbnail, JourneyThumbnail.category_id == ThumbnailCategory.id)
            .where(
                JourneyThumbnail.user_id == current_user.id,
                JourneyThumbnail.label == label,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="아직 분류되지 않았거나 어울리는 분류가 없는 기록입니다.",
        )
    try:
        data = await download_image(row.r2_key)
    except R2UnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except R2ObjectNotFoundError:
        raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다.")
    return Response(content=data, media_type=row.mime_type)
