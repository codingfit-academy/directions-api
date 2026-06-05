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
KAKAO_REST_API_KEY = _val("KAKAO_REST_API_KEY", "")

# ── 공공데이터포털 (경찰청 교차로 API) ─────────────────────────
DATA_GO_KR_SERVICE_KEY = _val(
    "DATA_GO_KR_SERVICE_KEY",
    "37ebc5d0f8167cd620d223440c1a660f62677e3a1c90d2601da7470e2924740f",
)
