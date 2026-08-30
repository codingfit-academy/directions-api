"""
ETA(예상 소요시간) 예측 — XGBoost 분위수 회귀 baseline.

trip_segment_features(+gps_trips)에 쌓인 실측 데이터로 학습한다 (POST /eta/train).
데이터가 아직 거의 없는 초기 단계를 감안해:

  - 학습 표본이 ETA_MIN_TRAINING_SAMPLES 미만이면 학습을 거부한다 (heuristic만 사용).
  - 예측 시 학습된 모델이 없으면 "거리 / 이력 평균속도 + 정지당 예상 대기시간"
    규칙 기반 계산으로 대체한다 — 완전히 콜드스타트인 상태에서도 API가 항상
    응답을 줄 수 있게 하기 위함.

피처: 거리, 출발 시각(시간대·요일, Asia/Seoul 기준), 이력 평균속도(개인+기록이름 →
population → 기본값 순으로 폴백), 이력 평균 정지 횟수. "이력"은 항상 해당 시점
*이전에* 완료된 trip만으로 계산해 학습 시점 데이터 누수를 피한다.

정시 도착 확률은 분위수 3점(p10/p50/p90)을 CDF의 앵커로 보고 선형 보간/외삽해
근사한다 — 표본이 적을 때는 부정확할 수 있으나, 데이터가 쌓일수록 분위수 추정이
정교해지며 함께 개선된다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import xgboost as xgb
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import (
    DEPARTURE_REC_THRESHOLD_COMFORTABLE,
    DEPARTURE_REC_THRESHOLD_ON_TIME,
    DEPARTURE_REC_THRESHOLD_URGENT,
    ETA_DEFAULT_SPEED_MPS,
    ETA_DEFAULT_WAIT_PER_STOP_S,
    ETA_MIN_TRAINING_SAMPLES,
    ETA_MODEL_PATH,
)

_KST = ZoneInfo("Asia/Seoul")
_QUANTILES = [0.1, 0.5, 0.9]
_FEATURE_NAMES = [
    "distance_m",
    "hour_of_day",
    "day_of_week",
    "historical_avg_speed_mps",
    "historical_source",  # 0=기본값 / 1=population / 2=개인+기록이름
    "historical_avg_stop_count",
]

# 프로세스 안에 캐시해 둔 모델. 재학습(POST /eta/train)하면 갱신된다.
_model: Optional[xgb.Booster] = None
_model_checked_disk = False


class EtaInputError(Exception):
    """예측에 필요한 최소 정보(거리 등)가 없을 때. 라우터에서 400으로 변환한다."""


@dataclass
class EtaFeatures:
    distance_m: float
    hour_of_day: int
    day_of_week: int
    historical_avg_speed_mps: float
    historical_source: int
    historical_avg_stop_count: float

    def to_row(self) -> list[float]:
        return [
            self.distance_m,
            float(self.hour_of_day),
            float(self.day_of_week),
            self.historical_avg_speed_mps,
            float(self.historical_source),
            self.historical_avg_stop_count,
        ]


@dataclass
class TrainResult:
    trained: bool
    sample_count: int
    reason: Optional[str] = None


@dataclass
class PredictResult:
    predicted_duration_s: int
    duration_p10_s: int
    duration_p90_s: int
    on_time_probability: Optional[float]
    model_source: str  # "model" | "heuristic"
    sample_count: int


def _get_cached_model() -> Optional[xgb.Booster]:
    """프로세스 캐시 → 디스크 파일 순으로 모델을 찾는다. 없으면 None."""
    global _model, _model_checked_disk
    if _model is not None:
        return _model
    if not _model_checked_disk and os.path.exists(ETA_MODEL_PATH):
        booster = xgb.Booster()
        booster.load_model(ETA_MODEL_PATH)
        _model = booster
    _model_checked_disk = True
    return _model


# 이력 평균(속도·정지횟수)을 계산할 때, 대상 trip 시작 시각 *이전*의 완료된
# trip만 집계한다 — 학습 시 미래 데이터를 몰래 참조하는 leakage를 막기 위함.
# label은 NULL끼리도 같은 값으로 취급해야 해서 IS NOT DISTINCT FROM을 쓴다.
_TRAINING_ROWS_SQL = """
    SELECT
        t.id AS trip_id,
        f.distance_m, f.actual_duration_s, f.stop_count,
        f.hour_of_day, f.day_of_week,
        (
            SELECT AVG(f2.avg_speed_mps)
            FROM trip_segment_features f2
            JOIN gps_trips t2 ON t2.id = f2.trip_id
            WHERE t2.user_id = t.user_id
              AND t2.label IS NOT DISTINCT FROM t.label
              AND t2.started_at < t.started_at
        ) AS personal_avg_speed_mps,
        (
            SELECT AVG(f3.avg_speed_mps)
            FROM trip_segment_features f3
            JOIN gps_trips t3 ON t3.id = f3.trip_id
            WHERE t3.started_at < t.started_at
        ) AS population_avg_speed_mps,
        (
            SELECT AVG(f4.stop_count)
            FROM trip_segment_features f4
            JOIN gps_trips t4 ON t4.id = f4.trip_id
            WHERE t4.started_at < t.started_at
        ) AS population_avg_stop_count
    FROM trip_segment_features f
    JOIN gps_trips t ON t.id = f.trip_id
    WHERE t.status = 'completed'
    ORDER BY t.started_at ASC
"""


def _resolve_speed_and_source(
    personal: Optional[float], population: Optional[float]
) -> tuple[float, int]:
    if personal is not None:
        return float(personal), 2
    if population is not None:
        return float(population), 1
    return ETA_DEFAULT_SPEED_MPS, 0


def _row_to_features(row: dict) -> EtaFeatures:
    speed, source = _resolve_speed_and_source(
        row.get("personal_avg_speed_mps"), row.get("population_avg_speed_mps")
    )
    stop_count = (
        float(row["population_avg_stop_count"])
        if row.get("population_avg_stop_count") is not None
        else 0.0
    )
    return EtaFeatures(
        distance_m=float(row["distance_m"]),
        hour_of_day=int(row["hour_of_day"]),
        day_of_week=int(row["day_of_week"]),
        historical_avg_speed_mps=speed,
        historical_source=source,
        historical_avg_stop_count=stop_count,
    )


async def fetch_training_rows(db: AsyncSession) -> list[dict]:
    """XGBoost 학습에 쓰는 trip 단위 피처 행. eta_lstm.py의 벤치마크에서도 재사용한다."""
    return [dict(r) for r in (await db.execute(text(_TRAINING_ROWS_SQL))).mappings().all()]


async def train_model(db: AsyncSession) -> TrainResult:
    rows = await fetch_training_rows(db)

    if len(rows) < ETA_MIN_TRAINING_SAMPLES:
        return TrainResult(
            trained=False,
            sample_count=len(rows),
            reason=(
                f"학습 데이터가 부족합니다 ({len(rows)}건, 최소 {ETA_MIN_TRAINING_SAMPLES}건 필요). "
                "데이터가 더 쌓인 뒤 다시 시도하세요."
            ),
        )

    X = np.array([_row_to_features(r).to_row() for r in rows], dtype=float)
    y = np.array([r["actual_duration_s"] for r in rows], dtype=float)

    dtrain = xgb.DMatrix(X, label=y, feature_names=_FEATURE_NAMES)
    booster = xgb.train(
        params={
            "objective": "reg:quantileerror",
            "quantile_alpha": _QUANTILES,
            "tree_method": "hist",
            "max_depth": 4,
            "eta": 0.1,
        },
        dtrain=dtrain,
        num_boost_round=100,
    )

    os.makedirs(os.path.dirname(ETA_MODEL_PATH) or ".", exist_ok=True)
    booster.save_model(ETA_MODEL_PATH)

    global _model, _model_checked_disk
    _model = booster
    _model_checked_disk = True

    return TrainResult(trained=True, sample_count=len(rows))


async def _resolve_history(
    db: AsyncSession, user_id: int, label: Optional[str]
) -> tuple[float, int, float, int]:
    """지금 이 순간 기준으로(= 모든 완료 trip 포함) 이력 평균 속도/정지횟수를 구한다.

    반환: (historical_avg_speed_mps, historical_source, historical_avg_stop_count, sample_count)
    """
    personal_row = (
        await db.execute(
            text(
                """
                SELECT AVG(f.avg_speed_mps) AS avg_speed
                FROM trip_segment_features f
                JOIN gps_trips t ON t.id = f.trip_id
                WHERE t.user_id = :user_id AND t.label IS NOT DISTINCT FROM :label
                  AND t.status = 'completed'
                """
            ),
            {"user_id": user_id, "label": label},
        )
    ).mappings().first()

    population_row = (
        await db.execute(
            text(
                """
                SELECT AVG(f.avg_speed_mps) AS avg_speed,
                       AVG(f.stop_count) AS avg_stop_count,
                       COUNT(*) AS sample_count
                FROM trip_segment_features f
                JOIN gps_trips t ON t.id = f.trip_id
                WHERE t.status = 'completed'
                """
            )
        )
    ).mappings().first()

    speed, source = _resolve_speed_and_source(
        personal_row["avg_speed"] if personal_row else None,
        population_row["avg_speed"] if population_row else None,
    )
    stop_count = (
        float(population_row["avg_stop_count"])
        if population_row and population_row["avg_stop_count"] is not None
        else 0.0
    )
    sample_count = int(population_row["sample_count"]) if population_row else 0
    return speed, source, stop_count, sample_count


async def _resolve_distance_m(
    db: AsyncSession, user_id: int, label: Optional[str], distance_m: Optional[float]
) -> float:
    """
    거리(distance_m)를 직접 안 줬을 때, 같은 이름(label)으로 기록된 과거 trip들의
    평균 거리로 대신한다 — 앱의 '기록하기'/알람 화면은 좌표가 아니라 기록 이름만
    갖고 있어서, 이 폴백이 없으면 매번 좌표를 새로 물어봐야 한다.

    개인(같은 user + label) 이력 → population(전체 완료 trip) 평균 순으로 폴백하고,
    그마저도 없으면 예측할 방법이 없으므로 EtaInputError를 던진다.
    """
    if distance_m is not None:
        return distance_m

    personal_row = (
        await db.execute(
            text(
                """
                SELECT AVG(f.distance_m) AS avg_distance
                FROM trip_segment_features f
                JOIN gps_trips t ON t.id = f.trip_id
                WHERE t.user_id = :user_id AND t.label IS NOT DISTINCT FROM :label
                  AND t.status = 'completed'
                """
            ),
            {"user_id": user_id, "label": label},
        )
    ).mappings().first()
    if personal_row and personal_row["avg_distance"] is not None:
        return float(personal_row["avg_distance"])

    population_row = (
        await db.execute(
            text(
                """
                SELECT AVG(f.distance_m) AS avg_distance
                FROM trip_segment_features f
                JOIN gps_trips t ON t.id = f.trip_id
                WHERE t.status = 'completed'
                """
            )
        )
    ).mappings().first()
    if population_row and population_row["avg_distance"] is not None:
        return float(population_row["avg_distance"])

    raise EtaInputError(
        "distance_m 또는 origin/dest 좌표를 지정하세요 (참고할 과거 이동 기록이 아직 없습니다)."
    )


def _cdf_from_quantiles(x: float, p10: float, p50: float, p90: float) -> float:
    """분위수 3점을 CDF 앵커로 보고 선형 보간/외삽해 P(실제값 <= x)를 근사한다."""
    points = sorted({p10: 0.1, p50: 0.5, p90: 0.9}.items())
    xs = [p for p, _ in points]
    ys = [q for _, q in points]

    if x <= xs[0]:
        i = 0
    elif x >= xs[-1]:
        i = len(xs) - 2
    else:
        i = next(k for k in range(len(xs) - 1) if xs[k] <= x <= xs[k + 1])

    if xs[i + 1] == xs[i]:
        return ys[i]
    t = (x - xs[i]) / (xs[i + 1] - xs[i])
    return max(0.0, min(1.0, ys[i] + t * (ys[i + 1] - ys[i])))


def _heuristic_predict(
    distance_m: float, historical_avg_speed_mps: float, historical_avg_stop_count: float
) -> tuple[int, int, int]:
    """모델이 없을 때 쓰는 규칙 기반 추정 — 거리/속도 + 정지당 대기시간."""
    base_s = distance_m / historical_avg_speed_mps + historical_avg_stop_count * ETA_DEFAULT_WAIT_PER_STOP_S
    base_s = max(1.0, base_s)
    return int(round(base_s)), int(round(base_s * 0.75)), int(round(base_s * 1.3))


async def predict(
    db: AsyncSession,
    *,
    user_id: int,
    label: Optional[str],
    distance_m: Optional[float],
    departure_at: datetime,
    target_arrival_at: Optional[datetime],
) -> PredictResult:
    distance_m = await _resolve_distance_m(db, user_id, label, distance_m)
    speed, source, stop_count, sample_count = await _resolve_history(db, user_id, label)
    local_departure = departure_at.astimezone(_KST)

    booster = _get_cached_model()
    if booster is not None:
        features = EtaFeatures(
            distance_m=distance_m,
            hour_of_day=local_departure.hour,
            day_of_week=local_departure.weekday(),
            historical_avg_speed_mps=speed,
            historical_source=source,
            historical_avg_stop_count=stop_count,
        )
        dmatrix = xgb.DMatrix(
            np.array([features.to_row()], dtype=float), feature_names=_FEATURE_NAMES
        )
        raw = booster.predict(dmatrix)[0]  # quantile_alpha 순서([0.1,0.5,0.9])대로 나오지만,
        # 데이터가 적을 때는 분위수 교차(quantile crossing, 예: p10 > p50)가 실제로 발생할 수
        # 있어 정렬로 단조성을 강제한다 — 개별 분위수의 미세한 정확도보다 순서 보장이 우선.
        p10_s, p50_s, p90_s = sorted(max(1.0, float(v)) for v in raw)
        model_source = "model"
    else:
        p50_s, p10_s, p90_s = _heuristic_predict(distance_m, speed, stop_count)
        model_source = "heuristic"

    on_time_probability = None
    if target_arrival_at is not None:
        remaining_s = (target_arrival_at - departure_at).total_seconds()
        on_time_probability = round(_cdf_from_quantiles(remaining_s, p10_s, p50_s, p90_s), 3)

    return PredictResult(
        predicted_duration_s=int(round(p50_s)),
        duration_p10_s=int(round(p10_s)),
        duration_p90_s=int(round(p90_s)),
        on_time_probability=on_time_probability,
        model_source=model_source,
        sample_count=sample_count,
    )


@dataclass
class DepartureRecommendation:
    status: str
    message: str


# 임계값(config.py)은 실측 지각/여유 비율을 보고 나중에 보정해야 하는 가정치.
_STATUS_MESSAGES = {
    "comfortable": "여유 있는 출발",
    "on_time": "정시 출발",
    "urgent": "늦어도 지금은 출발",
    "late": "이미 늦음",
}


def recommend_departure(on_time_probability: float) -> DepartureRecommendation:
    """정시 도착 확률을 출발 상태 문구로 매핑한다."""
    if on_time_probability >= DEPARTURE_REC_THRESHOLD_COMFORTABLE:
        status = "comfortable"
    elif on_time_probability >= DEPARTURE_REC_THRESHOLD_ON_TIME:
        status = "on_time"
    elif on_time_probability >= DEPARTURE_REC_THRESHOLD_URGENT:
        status = "urgent"
    else:
        status = "late"
    return DepartureRecommendation(status=status, message=_STATUS_MESSAGES[status])
