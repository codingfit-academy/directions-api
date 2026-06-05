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
