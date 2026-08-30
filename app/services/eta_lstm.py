"""
ETA(예상 소요시간) 예측 — LSTM 시퀀스 모델 오프라인 벤치마크 (2차 확장, 계획서 5절).

XGBoost baseline(app/services/eta_model.py)은 trip 하나를 "평균 속도·정지
횟수" 같은 요약 피처로 뭉개서 쓴다. 이 모델은 대신 trip 내부 GPS 포인트
시퀀스(구간별 속도·정지 패턴)를 그대로 학습해, 요약 피처로는 못 잡는
"사람마다 다른 이동 습관"을 잡아낼 수 있는지 확인하려는 목적이다.

**주의 — 아직 /eta/predict 라이브 예측에는 연결돼 있지 않다.** LSTM은 trip
"자기 자신의" GPS 시퀀스를 입력으로 duration을 맞추도록 학습되는데,
출발 전 예측(/eta/predict)은 정의상 그 trip의 시퀀스가 아직 없는 시점에
이루어진다. 그래서 지금은:

  - POST /eta/train-lstm 으로 완료된 trip들을 시간순으로 학습/홀드아웃 분할해
    학습하고, 같은 홀드아웃에서 XGBoost와 정확도(MAE)를 비교하는 용도로만 쓴다.
  - 이 비교에서 LSTM이 확실히 더 낫고 표본도 충분히 쌓이면, 그다음 단계로
    "사용자+기록이름별 과거 시퀀스의 평균 임베딩"을 XGBoost 피처에 추가하는
    식으로 라이브 예측에 연결하는 걸 고려한다 (아직 미구현).

torch(선택 의존성, requirements-optional.txt)가 없으면 이 모듈의 함수들은
LstmUnavailableError를 던진다 — 기본 API(XGBoost 기반)는 영향받지 않는다.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import xgboost as xgb
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import (
    ETA_LSTM_EPOCHS,
    ETA_LSTM_HIDDEN_SIZE,
    ETA_LSTM_HOLDOUT_RATIO,
    ETA_LSTM_LEARNING_RATE,
    ETA_LSTM_MAX_SEQ_LEN,
    ETA_LSTM_MIN_TRAINING_SAMPLES,
    ETA_LSTM_MODEL_PATH,
)
from . import eta_model

# numpy/xgboost는 eta_model.py도 쓰는 필수 의존성이라 항상 있다고 가정해도 된다.
# torch만 선택 의존성(requirements-optional.txt)이라 여기서만 guard한다.
try:
    import torch
    from torch import nn

    _TORCH_AVAILABLE = True
except ImportError:  # torch 미설치 — 선택 기능이라 정상적인 경로
    _TORCH_AVAILABLE = False


class LstmUnavailableError(Exception):
    """torch가 설치되어 있지 않을 때 (requirements-optional.txt 참고)."""


def _require_torch() -> None:
    if not _TORCH_AVAILABLE:
        raise LstmUnavailableError(
            "torch가 설치되어 있지 않습니다 (선택 기능). "
            "pip install -r requirements-optional.txt 로 설치할 수 있습니다."
        )


_QUANTILES = [0.1, 0.5, 0.9]


@dataclass
class LstmBenchmarkResult:
    trained: bool
    sample_count: int
    train_count: int = 0
    holdout_count: int = 0
    lstm_mae_s: Optional[float] = None
    xgb_mae_s: Optional[float] = None
    reason: Optional[str] = None


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 WGS84 좌표 간 직선거리(m)."""
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


async def _fetch_sequence(db: AsyncSession, trip_id: int) -> "np.ndarray":
    """
    trip의 노이즈 제거 후 GPS 포인트를 [delta_t_s, delta_dist_m, is_stop] 스텝
    시퀀스로 변환한다. is_stop은 그 스텝의 도착 시점이 ST-DBSCAN이 찾아낸 정지
    구간(stop_clusters) 안에 들어가는지로 표시한다.
    """
    points = (
        await db.execute(
            text(
                """
                SELECT ST_Y(geom) AS lat, ST_X(geom) AS lng, recorded_at
                FROM gps_points
                WHERE trip_id = :trip_id AND is_noise = false
                ORDER BY recorded_at ASC
                """
            ),
            {"trip_id": trip_id},
        )
    ).mappings().all()

    stops = (
        await db.execute(
            text("SELECT started_at, ended_at FROM stop_clusters WHERE trip_id = :trip_id"),
            {"trip_id": trip_id},
        )
    ).mappings().all()

    steps: list[list[float]] = []
    for prev, curr in zip(points, points[1:]):
        dt = (curr["recorded_at"] - prev["recorded_at"]).total_seconds()
        dist = _haversine_m(prev["lat"], prev["lng"], curr["lat"], curr["lng"])
        is_stop = any(s["started_at"] <= curr["recorded_at"] <= s["ended_at"] for s in stops)
        steps.append([float(dt), float(dist), 1.0 if is_stop else 0.0])

    if not steps:
        return np.zeros((0, 3), dtype=np.float32)
    return np.array(steps, dtype=np.float32)


def _pad_batch(sequences: list["np.ndarray"], max_len: int) -> tuple["np.ndarray", "np.ndarray"]:
    """가변 길이 시퀀스를 0으로 패딩해 (batch, T, 3) 배열과 실제 길이 배열로 만든다."""
    longest = max((len(s) for s in sequences), default=1)
    seq_len = max(1, min(longest, max_len))

    batch = np.zeros((len(sequences), seq_len, 3), dtype=np.float32)
    lengths = np.ones(len(sequences), dtype=np.int64)
    for i, seq in enumerate(sequences):
        n = min(len(seq), seq_len)
        if n == 0:
            continue  # 빈 시퀀스는 길이 1짜리 0벡터로 둔다 (pack_padded_sequence는 길이 0 불가)
        batch[i, :n] = seq[:n]
        lengths[i] = n
    return batch, lengths


if _TORCH_AVAILABLE:

    class _EtaLstmNet(nn.Module):
        def __init__(self, input_size: int = 3, hidden_size: int = 32, num_quantiles: int = 3):
            super().__init__()
            self.lstm = nn.LSTM(input_size, hidden_size, batch_first=True)
            self.head = nn.Linear(hidden_size, num_quantiles)

        def forward(self, x: "torch.Tensor", lengths: "torch.Tensor") -> "torch.Tensor":
            packed = nn.utils.rnn.pack_padded_sequence(
                x, lengths.cpu(), batch_first=True, enforce_sorted=False
            )
            _, (h_n, _) = self.lstm(packed)
            return self.head(h_n[-1])

    def _pinball_loss(
        preds: "torch.Tensor", target: "torch.Tensor", quantiles: list[float]
    ) -> "torch.Tensor":
        losses = []
        for i, q in enumerate(quantiles):
            errors = target - preds[:, i]
            losses.append(torch.maximum((q - 1) * errors, q * errors))
        return torch.stack(losses, dim=1).mean()


async def _evaluate_xgboost_on_holdout(holdout_rows: list[dict]) -> Optional[float]:
    """같은 홀드아웃 trip들에 대해, 이미 학습된 XGBoost 모델(있다면)의 MAE를 구한다."""
    booster = eta_model._get_cached_model()
    if booster is None or not holdout_rows:
        return None

    X = np.array([eta_model._row_to_features(r).to_row() for r in holdout_rows], dtype=float)
    y = np.array([r["actual_duration_s"] for r in holdout_rows], dtype=float)
    dmatrix = xgb.DMatrix(X, feature_names=eta_model._FEATURE_NAMES)
    preds = booster.predict(dmatrix)
    medians = np.sort(preds, axis=1)[:, 1]
    return float(np.mean(np.abs(medians - y)))


async def train_and_evaluate(db: AsyncSession) -> LstmBenchmarkResult:
    """
    완료된 trip을 시간순으로 학습/홀드아웃 분할해 LSTM을 학습하고, 같은
    홀드아웃에서 XGBoost와 MAE(평균 절대 오차, 초)를 비교한다.
    """
    _require_torch()

    rows = await eta_model.fetch_training_rows(db)
    if len(rows) < ETA_LSTM_MIN_TRAINING_SAMPLES:
        return LstmBenchmarkResult(
            trained=False,
            sample_count=len(rows),
            reason=(
                f"학습 데이터가 부족합니다 ({len(rows)}건, 최소 "
                f"{ETA_LSTM_MIN_TRAINING_SAMPLES}건 필요). XGBoost보다 표본이 더 필요합니다."
            ),
        )

    sequences: list["np.ndarray"] = []
    kept_rows: list[dict] = []
    for row in rows:
        seq = await _fetch_sequence(db, row["trip_id"])
        if len(seq) == 0:
            continue
        sequences.append(seq)
        kept_rows.append(row)

    if len(sequences) < ETA_LSTM_MIN_TRAINING_SAMPLES:
        return LstmBenchmarkResult(
            trained=False,
            sample_count=len(sequences),
            reason="유효한 GPS 시퀀스가 있는 trip이 부족합니다 (포인트가 너무 적은 trip이 많음).",
        )

    n = len(sequences)
    holdout_n = max(1, int(n * ETA_LSTM_HOLDOUT_RATIO))
    train_n = n - holdout_n

    train_seqs, holdout_seqs = sequences[:train_n], sequences[train_n:]
    train_rows, holdout_rows = kept_rows[:train_n], kept_rows[train_n:]

    X_train, len_train = _pad_batch(train_seqs, ETA_LSTM_MAX_SEQ_LEN)
    y_train = np.array([r["actual_duration_s"] for r in train_rows], dtype=np.float32)

    model = _EtaLstmNet(hidden_size=ETA_LSTM_HIDDEN_SIZE)
    optimizer = torch.optim.Adam(model.parameters(), lr=ETA_LSTM_LEARNING_RATE)

    x_tensor = torch.from_numpy(X_train)
    len_tensor = torch.from_numpy(len_train)
    y_tensor = torch.from_numpy(y_train)

    model.train()
    for _ in range(ETA_LSTM_EPOCHS):
        optimizer.zero_grad()
        preds = model(x_tensor, len_tensor)
        loss = _pinball_loss(preds, y_tensor, _QUANTILES)
        loss.backward()
        optimizer.step()

    os.makedirs(os.path.dirname(ETA_LSTM_MODEL_PATH) or ".", exist_ok=True)
    torch.save(model.state_dict(), ETA_LSTM_MODEL_PATH)

    model.eval()
    X_hold, len_hold = _pad_batch(holdout_seqs, ETA_LSTM_MAX_SEQ_LEN)
    with torch.no_grad():
        hold_preds = model(torch.from_numpy(X_hold), torch.from_numpy(len_hold)).numpy()
    lstm_medians = np.sort(hold_preds, axis=1)[:, 1]
    y_hold = np.array([r["actual_duration_s"] for r in holdout_rows], dtype=float)
    lstm_mae = float(np.mean(np.abs(lstm_medians - y_hold)))

    xgb_mae = await _evaluate_xgboost_on_holdout(holdout_rows)

    return LstmBenchmarkResult(
        trained=True,
        sample_count=n,
        train_count=train_n,
        holdout_count=holdout_n,
        lstm_mae_s=round(lstm_mae, 1),
        xgb_mae_s=round(xgb_mae, 1) if xgb_mae is not None else None,
    )
