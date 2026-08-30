"""
ETA(예상 소요시간) 예측 라우터.

- POST /eta/train                    : trip_segment_features 데이터로 XGBoost 분위수 회귀 모델을 재학습
- GET  /eta/predict                  : 예상 소요시간 + (목표 도착시각을 주면) 정시 도착 확률 예측
- GET  /eta/departure-recommendation : 정시 도착 확률을 출발 상태 문구로 매핑
- POST /eta/train-lstm               : (2차 확장, 오프라인 벤치마크) LSTM 학습 + XGBoost와 정확도 비교.
                                        torch가 설치되어 있지 않으면 501.

로그인만 하면 누구나 호출 가능하다 (population 모델이라 특정 사용자 전용 데이터가
아님 — 학원 프로젝트 규모에서 별도 관리자 권한 체계는 두지 않았다).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import get_db
from ..models import User
from ..schemas import (
    DepartureRecommendationOut,
    EtaLstmBenchmarkOut,
    EtaPredictResult,
    EtaTrainResult,
)
from ..services.eta_lstm import LstmUnavailableError
from ..services.eta_lstm import train_and_evaluate as train_lstm_model
from ..services.eta_model import EtaInputError
from ..services.eta_model import predict as predict_eta
from ..services.eta_model import recommend_departure, train_model

router = APIRouter(prefix="/eta", tags=["eta"])


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 WGS84 좌표 간 직선거리(m)."""
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _distance_from_coords(
    distance_m: float | None,
    origin_lat: float | None,
    origin_lng: float | None,
    dest_lat: float | None,
    dest_lng: float | None,
) -> float | None:
    """
    distance_m을 직접 줬으면 그대로, origin/dest 좌표를 모두 줬으면 직선거리로
    계산해 반환한다. 아무 것도 없으면 None을 반환하고, 이후 eta_model.predict()가
    label 기준 과거 평균 거리로 폴백한다 (앱이 좌표 없이 기록 이름만 넘기는
    경우 대비 — trip_tracking_page.dart 참고).
    """
    if distance_m is not None:
        return distance_m
    if None in (origin_lat, origin_lng, dest_lat, dest_lng):
        return None
    return _haversine_m(origin_lat, origin_lng, dest_lat, dest_lng)


@router.post("/train", response_model=EtaTrainResult)
async def train(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """전체 사용자의 완료된 trip 데이터로 population 모델을 (재)학습한다."""
    result = await train_model(db)
    return EtaTrainResult(
        trained=result.trained, sample_count=result.sample_count, reason=result.reason
    )


@router.get("/predict", response_model=EtaPredictResult)
async def predict(
    label: str | None = Query(
        default=None, description="과거 기록 이름 (있으면 개인 이력을 우선 사용)"
    ),
    origin_lat: float | None = Query(default=None, ge=-90, le=90),
    origin_lng: float | None = Query(default=None, ge=-180, le=180),
    dest_lat: float | None = Query(default=None, ge=-90, le=90),
    dest_lng: float | None = Query(default=None, ge=-180, le=180),
    distance_m: float | None = Query(
        default=None, ge=0, description="직접 넘기면 origin/dest 좌표 대신 이 값을 사용"
    ),
    target_arrival_at: datetime | None = Query(
        default=None, description="목표 도착 시각 — 지정하면 정시 도착 확률도 계산"
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    지금 출발한다고 가정했을 때의 예상 소요시간(분위수 3점)과, 목표 도착시각을
    줬다면 그 시각까지 도착할 확률을 반환한다.

    거리(distance_m)는 직접 줄 수도 있고, origin/dest 좌표를 주면 직선거리로
    계산한다 (실제 도보 경로 거리가 아니라 근사치임에 유의 — 더 정확한 거리가
    필요하면 /directions/route로 먼저 경로를 구한 뒤 그 distance_m을 넘기면 된다).
    셋 다 안 주면 같은 이름(label)의 과거 trip 평균 거리로 대신한다.
    """
    resolved_distance = _distance_from_coords(distance_m, origin_lat, origin_lng, dest_lat, dest_lng)

    try:
        result = await predict_eta(
            db,
            user_id=current_user.id,
            label=label,
            distance_m=resolved_distance,
            departure_at=datetime.now(timezone.utc),
            target_arrival_at=target_arrival_at,
        )
    except EtaInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return EtaPredictResult(
        predicted_duration_s=result.predicted_duration_s,
        duration_p10_s=result.duration_p10_s,
        duration_p90_s=result.duration_p90_s,
        on_time_probability=result.on_time_probability,
        model_source=result.model_source,
        sample_count=result.sample_count,
    )


@router.get("/departure-recommendation", response_model=DepartureRecommendationOut)
async def departure_recommendation(
    target_arrival_at: datetime = Query(..., description="목표 도착 시각 (필수)"),
    label: str | None = Query(
        default=None, description="과거 기록 이름 (있으면 개인 이력을 우선 사용)"
    ),
    origin_lat: float | None = Query(default=None, ge=-90, le=90),
    origin_lng: float | None = Query(default=None, ge=-180, le=180),
    dest_lat: float | None = Query(default=None, ge=-90, le=90),
    dest_lng: float | None = Query(default=None, ge=-180, le=180),
    distance_m: float | None = Query(
        default=None, ge=0, description="직접 넘기면 origin/dest 좌표 대신 이 값을 사용"
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    "지금 출발하면 목표 시각까지 도착할 확률"을 계산해 그 확률을 출발 상태
    문구("여유 있는 출발"/"정시 출발"/"늦어도 지금은 출발"/"이미 늦음")로 매핑한다.
    임계값은 아직 실측 데이터로 보정 전인 가정치다 (app/config.py의
    DEPARTURE_REC_THRESHOLD_* 참고).
    """
    resolved_distance = _distance_from_coords(distance_m, origin_lat, origin_lng, dest_lat, dest_lng)
    now = datetime.now(timezone.utc)

    try:
        result = await predict_eta(
            db,
            user_id=current_user.id,
            label=label,
            distance_m=resolved_distance,
            departure_at=now,
            target_arrival_at=target_arrival_at,
        )
    except EtaInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # target_arrival_at을 항상 넘기므로 on_time_probability는 반드시 채워진다.
    assert result.on_time_probability is not None
    recommendation = recommend_departure(result.on_time_probability)

    return DepartureRecommendationOut(
        status=recommendation.status,
        message=recommendation.message,
        on_time_probability=result.on_time_probability,
        remaining_s=int((target_arrival_at - now).total_seconds()),
        predicted_duration_s=result.predicted_duration_s,
        duration_p10_s=result.duration_p10_s,
        duration_p90_s=result.duration_p90_s,
        model_source=result.model_source,
        sample_count=result.sample_count,
    )


@router.post("/train-lstm", response_model=EtaLstmBenchmarkOut)
async def train_lstm(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    (2차 확장, 오프라인 벤치마크 전용) 완료된 trip의 GPS 시퀀스로 LSTM을
    시간순 학습/홀드아웃 분할해 학습하고, 같은 홀드아웃에서 XGBoost와 정확도
    (MAE, 초)를 비교한다.

    **이 모델은 아직 /eta/predict 라이브 예측에는 쓰이지 않는다** — 출발 전
    예측 시점에는 그 trip의 GPS 시퀀스가 아직 존재하지 않기 때문이다. 완료된
    과거 trip들만으로 "시퀀스를 통째로 학습하면 XGBoost의 요약 피처보다 더
    정확한지" 가늠해보는 용도다. 자세한 내용은 app/services/eta_lstm.py 참고.

    torch(선택 의존성)가 설치되어 있지 않으면 501을 반환한다.
    """
    try:
        result = await train_lstm_model(db)
    except LstmUnavailableError as exc:
        raise HTTPException(status_code=501, detail=str(exc))

    return EtaLstmBenchmarkOut(
        trained=result.trained,
        sample_count=result.sample_count,
        train_count=result.train_count,
        holdout_count=result.holdout_count,
        lstm_mae_s=result.lstm_mae_s,
        xgb_mae_s=result.xgb_mae_s,
        reason=result.reason,
    )
