"""
프로젝트 설정 — 학원 환경용 직접 주입 설정.

이 파일에 키를 직접 박아 두면 .env 없이 동작합니다.
운영 환경에서는 환경변수가 있으면 그 값으로 덮어씌워집니다.

────────────────────────────────────────────────────────────────
NCP Maps 키:
  console.ncloud.com → AI·NAVER API → Maps → Application 등록
  같은 Application 안에서 사용할 서비스를 모두 추가해 주세요:
    □ Maps Geocoding   (서버측 주소→좌표)
    □ Maps Direction 5 (서버측 자동차 길찾기)
    □ Web Dynamic Map  (프론트 JS SDK)
  세 가지 모두 같은 ncpKeyId / Secret을 공유합니다.

  ※ 401 "A subscription to the API is required" 가 뜨면
    위 서비스 중 일부가 Application에 추가되지 않은 상태입니다.

Kakao Local 키 (NCP Geocoding 대체용):
  developers.kakao.com → 내 애플리케이션 → REST API 키
  무료 한도 안에서 주소·키워드 검색이 가능합니다.
  KAKAO_REST_API_KEY 만 채우면 NCP Geocoding 실패 시 자동 폴백됩니다.
────────────────────────────────────────────────────────────────
"""
import os


def _val(env_key: str, default: str) -> str:
    """환경변수가 있으면 사용, 없으면 직접 주입된 default 값을 사용."""
    return (os.getenv(env_key) or default).strip()


# ── Naver Cloud Platform (서버측 Geocode / Directions) ────────
NAVER_NCP_API_KEY_ID = _val("NAVER_NCP_API_KEY_ID", "fondkjpfpo")
NAVER_NCP_API_KEY = _val(
    "NAVER_NCP_API_KEY", "5ZW3anzjNV95UhlEwUehOVifRLvnf8Mpl9qv4s0T"
)

# ── 프론트엔드 JS SDK용 (브라우저 노출 OK) ─────────────────────
NAVER_MAPS_CLIENT_ID = _val("NAVER_MAPS_CLIENT_ID", "fondkjpfpo")
KAKAO_MAPS_APP_KEY = _val("KAKAO_MAPS_APP_KEY", "9b2b8dabcf2224eef0db36992670fb49")

# ── Kakao Local API (선택 — Geocoding 폴백용) ─────────────────
# 비워두면 NCP만 사용. 채워두면 NCP가 401/실패일 때 자동으로 폴백.
# ※ Kakao 콘솔에서 OPEN_MAP_AND_LOCAL 활성화가 막힌 경우 VWorld 사용.
KAKAO_REST_API_KEY = _val("KAKAO_REST_API_KEY", "d95e04ad3654a2ebad057b45f62ecf38")

# ── VWorld (국토교통부 공간정보 오픈플랫폼 - Kakao 대체) ────────
# 발급: https://www.vworld.kr → 회원가입 → 인증키 신청
# 무료, 학생 가능, 일 30,000회 (Geocoder + Search 공용).
# Geocoder: 주소 → 좌표 / Search: 키워드(장소명) → 좌표
VWORLD_API_KEY = _val("VWORLD_API_KEY", "AE2D3A36-9F19-3B08-8394-EC64E8266DDC")

# ── T-map 보행자 경로 (SK Telecom Open API) ────────────────────
# 발급: https://openapi.sk.com → 가입 → 앱 등록 → "T-map 보행자 경로안내" 추가
# 무료, 월 50,000 transactions, 학생 가능.
# 비워두면 보행 모드 요청 시 Naver Directions 5(자동차)로 자동 폴백.
TMAP_API_KEY = _val("TMAP_API_KEY", "njw4yBAyB83Ym0MN9fCrP4wnecYGCbs15zggefg3")

# ── 공공데이터포털 (경찰청 교차로 API) ─────────────────────────
DATA_GO_KR_SERVICE_KEY = _val(
    "DATA_GO_KR_SERVICE_KEY",
    "37ebc5d0f8167cd620d223440c1a660f62677e3a1c90d2601da7470e2924740f",
)

# ── 서울 열린데이터광장 (data.seoul.go.kr) ────────────────────
# 발급: data.seoul.go.kr → 회원가입 → 인증키 신청
# 무료, 학생 프로젝트 가능, 일 1,000회 한도(기본).
# 비워두면 실시간 도로소통 보정 기능이 비활성화됩니다.
SEOUL_OPENAPI_KEY = _val("SEOUL_OPENAPI_KEY", "705645795870616a38325171696343")

# 사용할 데이터셋의 정확한 OpenAPI 서비스명 (서울 열린데이터광장의 데이터셋 페이지
# "OpenAPI 호출 예제" 에 표시됨). 예: 'TrafficInfo' = 실시간 도로 소통 정보.
# 이 데이터셋은 XML만 지원하며 LINK_ID 인자 필요.
SEOUL_OPENAPI_DATASET = _val("SEOUL_OPENAPI_DATASET", "TrafficInfo")

# 실시간 통행속도를 가져올 도로 링크 ID 목록 (CSV).
# 표준노드링크 LINK_ID — 데이터셋 가이드 / its.go.kr 에서 확인 가능.
# 기본값은 사용자 제공한 샘플 LINK_ID 하나. 더 다양한 도로를 평균에 포함시키려면
# 쉼표로 추가하세요. (예: "1220003800,1220004100,1220004300")
SEOUL_TOPIS_LINK_IDS = _val("SEOUL_TOPIS_LINK_IDS", "1220003800")

# ── 길찾기 제한 ───────────────────────────────────────────────
# 본 앱은 '근거리 신호등 정보를 통한 최단시간 보행'이 목적이라
# 출발지~도착지 직선거리 상한을 둔다. (Haversine 미터)
MAX_ROUTE_DISTANCE_M = int(_val("MAX_ROUTE_DISTANCE_M", "2000"))

# ── 인증 (JWT) ──────────────────────────────────────────────
# 학원 환경 기본값 — 운영 배포 시에는 반드시 환경변수로 교체하세요.
JWT_SECRET_KEY = _val("JWT_SECRET_KEY", "change-this-secret-in-production-9f8a3d7c1b")
JWT_ALGORITHM = _val("JWT_ALGORITHM", "HS256")
# 모바일 앱 특성상 재로그인 부담을 줄이기 위해 기본 30일로 설정 (refresh token 없음).
JWT_EXPIRE_MINUTES = int(_val("JWT_EXPIRE_MINUTES", str(60 * 24 * 30)))

# ── GPS 궤적 전처리 (노이즈 제거 · ST-DBSCAN 정지 클러스터링) ────
# trip 종료(POST /gps/trips/{id}/finish) 시 백그라운드로 실행된다.
# GPS 포인트 정확도 반경이 이보다 나쁘면(미터) 노이즈로 버린다.
GPS_MAX_ACCURACY_M = float(_val("GPS_MAX_ACCURACY_M", "50"))
# 직전 유효 포인트 대비 이동속도가 이보다 크면(m/s) 튀는 값으로 보고 버린다.
# 15m/s ≒ 54km/h — 도보 앱이지만 GPS 튐/차량 탑승 구간을 감안해 여유를 둠.
GPS_MAX_SPEED_MPS = float(_val("GPS_MAX_SPEED_MPS", "15"))
# ST-DBSCAN 공간 반경(미터) — 이 안에 모여 있어야 "같은 자리"로 본다.
ST_DBSCAN_EPS_SPACE_M = float(_val("ST_DBSCAN_EPS_SPACE_M", "15"))
# ST-DBSCAN 시간 반경(초) — 공간 조건과 동시에 이 안에서 측정됐어야 이웃으로 본다.
ST_DBSCAN_EPS_TIME_S = float(_val("ST_DBSCAN_EPS_TIME_S", "60"))
# 정지로 인정하기 위한 최소 이웃 포인트 수 (자기 자신 포함).
ST_DBSCAN_MIN_PTS = int(_val("ST_DBSCAN_MIN_PTS", "3"))
# 클러스터의 체류시간이 이보다 짧으면(초) 정지로 인정하지 않는다 (신호 대기 등과
# 구분하기 위한 최소 기준 — 너무 짧으면 그냥 서행/GPS 튐일 가능성이 높음).
MIN_STOP_DURATION_S = int(_val("MIN_STOP_DURATION_S", "10"))
# 정지 클러스터를 signals 테이블의 신호등과 매칭할 때 쓰는 반경(미터).
STOP_SIGNAL_MATCH_BUFFER_M = int(_val("STOP_SIGNAL_MATCH_BUFFER_M", "30"))

# ── ETA 예측 (XGBoost baseline) ──────────────────────────────
# 학습된 모델 파일 저장 경로. POST /eta/train으로 재생성되는 산출물이라
# git에는 커밋하지 않는다 (.gitignore 참고).
ETA_MODEL_PATH = _val("ETA_MODEL_PATH", "app/ml_models/eta_xgb.json")
# 이보다 학습 표본(완료된 trip)이 적으면 학습을 거부하고 휴리스틱으로만 예측한다.
# 데이터가 거의 없는 초기 단계에서는 이 값을 낮춰 테스트할 수 있다.
ETA_MIN_TRAINING_SAMPLES = int(_val("ETA_MIN_TRAINING_SAMPLES", "20"))
# 개인/population 이력이 전혀 없을 때(콜드스타트) 쓰는 기본 도보 속도(m/s).
ETA_DEFAULT_SPEED_MPS = float(_val("ETA_DEFAULT_SPEED_MPS", "1.2"))
# 휴리스틱 예측에서 정지 1회당 더할 예상 대기시간(초) — 모델이 없을 때만 사용.
ETA_DEFAULT_WAIT_PER_STOP_S = int(_val("ETA_DEFAULT_WAIT_PER_STOP_S", "20"))

# ── 출발 추천 (정시 도착 확률 → 상태 문구) ────────────────────
# 임계값은 가정치 — 실측 데이터가 쌓이면 실제 지각/여유 비율을 보고 보정 필요.
# P >= COMFORTABLE           → "여유 있는 출발"
# COMFORTABLE > P >= ON_TIME → "정시 출발"
# ON_TIME > P >= URGENT      → "늦어도 지금은 출발"
# P < URGENT                 → "이미 늦음"
DEPARTURE_REC_THRESHOLD_COMFORTABLE = float(_val("DEPARTURE_REC_THRESHOLD_COMFORTABLE", "0.9"))
DEPARTURE_REC_THRESHOLD_ON_TIME = float(_val("DEPARTURE_REC_THRESHOLD_ON_TIME", "0.6"))
DEPARTURE_REC_THRESHOLD_URGENT = float(_val("DEPARTURE_REC_THRESHOLD_URGENT", "0.3"))

# ── ETA 예측 (LSTM, 2차 확장 — 오프라인 벤치마크 전용) ────────────
# torch가 설치돼 있을 때만 동작한다 (requirements-optional.txt 참고).
# GPS 시퀀스를 직접 학습하는 만큼 XGBoost보다 더 많은 표본을 요구한다.
ETA_LSTM_MIN_TRAINING_SAMPLES = int(_val("ETA_LSTM_MIN_TRAINING_SAMPLES", "50"))
# 시퀀스 최대 길이(스텝 수) — 이보다 긴 trip은 앞부분만 잘라 쓴다.
ETA_LSTM_MAX_SEQ_LEN = int(_val("ETA_LSTM_MAX_SEQ_LEN", "300"))
ETA_LSTM_HIDDEN_SIZE = int(_val("ETA_LSTM_HIDDEN_SIZE", "32"))
ETA_LSTM_EPOCHS = int(_val("ETA_LSTM_EPOCHS", "200"))
# 합성 데이터로 실험한 결과 0.01은 너무 느리게 수렴해 기본값을 0.05로 올렸다 —
# 실제 데이터로 학습해보면서 다시 튜닝이 필요할 수 있다.
ETA_LSTM_LEARNING_RATE = float(_val("ETA_LSTM_LEARNING_RATE", "0.05"))
ETA_LSTM_MODEL_PATH = _val("ETA_LSTM_MODEL_PATH", "app/ml_models/eta_lstm.pt")
# 학습/평가 분할 비율 — 시간순 뒤쪽 일부를 홀드아웃으로 뗀다 (미래 데이터로
# 평가하는 게 실제 배포 시나리오와 더 비슷해서 랜덤 셔플 대신 이렇게 한다).
ETA_LSTM_HOLDOUT_RATIO = float(_val("ETA_LSTM_HOLDOUT_RATIO", "0.2"))
