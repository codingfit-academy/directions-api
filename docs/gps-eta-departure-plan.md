# GPS 기반 ETA 예측 · 출발 추천 기능 계획서

작성일: 2026-08-23 (2026-08-23 갱신: 1단계 구현 완료 + 실제 Flutter 앱과 연동)
대상 저장소: `directions-api` (백엔드) + `directions-flutter` (앱)

> ⚠️ **경로 주의**: 이 저장소(`directions-api`)는 `C:\Users\codin\Documents\길 찾기\01_프로젝트\`와
> `C:\Users\codin\Documents\roadfinder\01_project\`에 **동일한 git 저장소가 두 곳에 체크아웃**되어
> 있다 (경로에 한글이 섞이면 Flutter/Gradle 툴체인이 깨져서, ASCII 전용 경로인 roadfinder
> 쪽이 실제 개발용으로 쓰이는 것으로 보인다). 이 문서와 백엔드 코드 변경은 `길 찾기` 경로
> 클론에서 이뤄졌다 — **커밋해서 양쪽에 반영하거나, 최소한 백엔드는 항상 이 클론에서
> 실행해야** 아래 API가 실제로 존재한다.
>
> 앱 쪽은 처음에 `길 찾기\01_프로젝트\directions-flutter`에 빈 프로젝트만 있는 줄 알고
> 새로 스캐폴딩했었으나, **진짜 앱은 `roadfinder\01_project\directions-flutter`에 이미
> Riverpod + Freezed + go_router + Dio 기반으로 상당히 진행되어 있었다** (로그인/회원가입/
> 기록하기/실시간 추적/AI분석 화면까지 UI가 존재, data/domain 레이어만 비어있었음).
> 착오로 만든 스캐폴딩은 삭제했고, 실제 앱(roadfinder 경로)에 인증 + GPS 수집 기능을
> 연결했다. **다만 이 `directions-flutter` 프로젝트는 git 저장소가 아니다(.git 없음)** —
> 지금까지의 모든 작업(이번 세션 이전 것 포함)이 버전 관리 없이 디스크에만 존재한다.

> **진행 상황**: 1단계(인증 + GPS 수집 파이프라인 뼈대)를 구현 완료.
> `users`/`gps_trips`/`gps_points` 테이블, `/auth/*`·`/gps/*` API를 만들고, 실제 앱
> (roadfinder 경로)의 로그인/회원가입/기록하기/실시간 추적 화면을 여기에 연결했다.
> 상세는 8절 "구현 현황" 참고. 이후 절의 "초안" 내용은 실제 구현에 맞춰 갱신되었다.

## 1. 배경 및 목표

현재 `directions-api`는 출발지/도착지를 받아 T-map(보행)·Naver Directions(차량) 경로를
조회하고, 경로 주변 신호등(`signals` 테이블)의 예상 대기시간을 더해 ETA를 보정하는
정적(static) 길찾기 서비스다 (`app/routers/directions.py`). 이동 거리는 2km 이내 근거리
보행으로 제한되어 있다 (`MAX_ROUTE_DISTANCE_M`).

이번 확장의 목표는, 앱이 실제 이동 중 수집한 **GPS 궤적**을 서버로 전송하면 이를 분석해
- 사용자가 실제로 멈췄던 지점(신호 대기, 정차 등)을 탐지하고
- 구간별 실제 이동 속도·거리를 계산하고
- 이 실측 데이터를 학습 데이터로 삼아 **개인화된 ETA(도착 예정 시각)** 를 예측하고
- 목표 도착 시각 대비 **정시 도착 확률**을 계산해
- "여유 있는 출발 / 정시 출발 / 늦어도 지금은 출발" 같은 **출발 타이밍 추천**을 제공하는 것.

즉 기존 기능이 "정적 지도 API 기반 예상 소요시간"이라면, 신규 기능은 "실측 이동 이력 기반
개인화 예측"으로, 두 값을 함께 써서 추천 정확도를 높인다.

## 2. 전체 파이프라인

```
[Flutter 앱]
  위치 권한 + 백그라운드 트래킹
  → 로컬 버퍼링 (5~10초 간격) → 배치 업로드 (30초~1분마다 또는 이동 종료 시)
        │  POST /gps/trips, POST /gps/trips/{id}/points
        ▼
[FastAPI: 수집 계층]
  gps_points 원시 테이블 적재 (trip_id, user_id, lat/lng, speed, accuracy, ts)
        │
        ▼
[전처리 파이프라인] (trip 종료 시 또는 배치 잡으로 비동기 실행)
  1) 노이즈 제거
     - accuracy(정확도 반경) 임계값 초과 포인트 제거
     - 물리적으로 불가능한 순간속도(예: 도보 > 20km/h) 제거
     - 중복/정지 지터(jitter) 포인트 스무딩 (이동평균 또는 칼만 필터)
  2) ST-DBSCAN 정지 지점 클러스터링
     - 공간 반경 eps_space, 시간 반경 eps_time, minPts 로 "일정 반경 내에서
       일정 시간 이상 머문" 포인트 군집을 정지 클러스터로 탐지
     - 각 클러스터 = 정지 구간(체류 시작/종료 시각, 중심 좌표, 체류시간)
  3) 속도/거리 계산
     - 정지 클러스터 사이 구간을 "이동 구간"으로 분리
     - 구간별 이동거리(Haversine 누적), 이동시간, 평균/최대 속도 계산
     - 총 이동시간 대비 정지시간 비율 산출
        │
        ▼
[Feature 저장 / trip_segments, stop_clusters 테이블]
        │
        ▼
[ETA 예측 모델]
  - Baseline: XGBoost (정형 피처: 거리, 시간대, 요일, 과거 평균속도,
    signals 테이블 기반 신호등 개수/예상 대기시간, 최근 N회 동일 경로 실적 등)
  - 확장: LSTM (raw/segment 시계열 기반, 궤적 패턴 학습 — 데이터 축적 후 도입)
  - 출력: 예상 도착 소요시간의 분포(또는 quantile) → 목표 시각 대비
    정시 도착 확률 P(도착 ≤ 목표시각)
        │
        ▼
[출발 추천 로직]
  P(정시 도착) 구간별로 상태 매핑
    - P ≥ 0.9           → "여유 있는 출발"
    - 0.9 > P ≥ 0.6      → "정시 출발"
    - 0.6 > P ≥ 0.3      → "늦어도 지금은 출발"
    - P < 0.3           → "이미 늦음 / 다른 수단 고려"
  (임계값은 실측 데이터 축적 후 보정 필요 — 1차는 가정치)
        │
        ▼
[Flutter 앱에 응답] → 알림/위젯으로 표시
```

## 3. 데이터베이스 스키마

기존 `Base`(SQLAlchemy declarative, `app/database.py`)와 `signals` 테이블의 PostGIS
패턴을 그대로 따른다. **`User`/`GpsTrip`/`GpsPoint`는 1단계에서 구현 완료**
(`app/models.py`). `user_id`는 애초 계획한 기기 UUID 대신, 신규로 추가한 이메일/비밀번호
+ JWT 인증 체계의 `users.id`(FK)를 사용한다 — 인증 없이 클라이언트가 스스로 신원을
주장하게 두면 다른 사용자의 trip에 데이터를 주입할 수 있는 문제가 있어, 서버가 토큰으로
검증한 사용자만 자신의 trip에 쓰도록 했다. `StopCluster`/`TripSegmentFeature`는 2~3단계
(전처리/피처 계산)에서 아직 구현되지 않은 초안이다.

```python
# app/models.py — 구현 완료

class User(Base):
    """이메일/비밀번호 기반 사용자 계정."""
    __tablename__ = "users"

    id: Mapped[int]              = mapped_column(Integer, primary_key=True)
    email: Mapped[str]           = mapped_column(String(255), nullable=False, unique=True, index=True)
    username: Mapped[str]        = mapped_column(String(64), nullable=False, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GpsTrip(Base):
    """앱이 하나의 이동(출발~도착)을 시작~종료할 때 생성하는 단위."""
    __tablename__ = "gps_trips"

    id: Mapped[int]         = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int]    = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    label: Mapped[str]      = mapped_column(String(100), nullable=True)  # 앱 "기록하기" 화면의 기록 이름
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), nullable=True)
    origin_lat: Mapped[float]    = mapped_column(Float, nullable=True)
    origin_lng: Mapped[float]    = mapped_column(Float, nullable=True)
    dest_lat: Mapped[float]      = mapped_column(Float, nullable=True)
    dest_lng: Mapped[float]      = mapped_column(Float, nullable=True)
    target_arrival_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str]     = mapped_column(String(16), nullable=False, default="active")
    # status: active(수집 중) / completed(종료) / discarded(폐기)


class GpsPoint(Base):
    """원시 GPS 포인트 (노이즈 제거 전). 좌표는 EPSG:4326(WGS84)."""
    __tablename__ = "gps_points"

    id: Mapped[int]         = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int]    = mapped_column(Integer, ForeignKey("gps_trips.id"), nullable=False, index=True)
    geom: Mapped[object]    = mapped_column(Geometry(geometry_type="POINT", srid=4326), nullable=False)
    speed_mps: Mapped[float]   = mapped_column(Float, nullable=True)
    accuracy_m: Mapped[float]  = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_noise: Mapped[bool]  = mapped_column(Boolean, nullable=False, default=False)
```

> 원래 초안에서는 `GpsTrip.origin_geom`/`dest_geom`을 PostGIS `Geometry` 컬럼으로
> 뒀지만, 현재 trip 출발/도착지에 대한 공간 쿼리(예: 반경 검색)가 필요 없어 단순
> `Float` 위경도 쌍으로 구현을 단순화했다. 추후 "과거 동일 경로 검색" 같은 기능이
> 필요해지면 그때 PostGIS 컬럼으로 옮기면 된다.

```python
# app/models.py — 구현 완료 (2~3단계)

class StopCluster(Base):
    """ST-DBSCAN으로 탐지된 정지 구간 (trip 종료 시 gps_processing.py가 생성)."""
    __tablename__ = "stop_clusters"

    id: Mapped[int]         = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int]    = mapped_column(Integer, ForeignKey("gps_trips.id"), nullable=False, index=True)
    center_geom: Mapped[object] = mapped_column(Geometry(geometry_type="POINT", srid=4326), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), nullable=False)
    duration_s: Mapped[int]      = mapped_column(Integer, nullable=False)
    point_count: Mapped[int]     = mapped_column(Integer, nullable=False)
    matched_signal_id: Mapped[int] = mapped_column(Integer, ForeignKey("signals.id"), nullable=True)
    matched_signal_distance_m: Mapped[float] = mapped_column(Float, nullable=True)


class TripSegmentFeature(Base):
    """ETA 모델 학습/추론용 trip 단위 피처 (trip마다 1행, upsert)."""
    __tablename__ = "trip_segment_features"

    id: Mapped[int]         = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int]    = mapped_column(Integer, ForeignKey("gps_trips.id"), nullable=False, unique=True)
    distance_m: Mapped[float]        = mapped_column(Float, nullable=False)
    actual_duration_s: Mapped[int]   = mapped_column(Integer, nullable=False)  # 학습 라벨
    moving_time_s: Mapped[int]       = mapped_column(Integer, nullable=False)
    stopped_time_s: Mapped[int]      = mapped_column(Integer, nullable=False)
    stop_count: Mapped[int]          = mapped_column(Integer, nullable=False)
    signal_stop_count: Mapped[int]   = mapped_column(Integer, nullable=False)
    avg_speed_mps: Mapped[float]     = mapped_column(Float, nullable=False)
    hour_of_day: Mapped[int]         = mapped_column(Integer, nullable=False)   # Asia/Seoul 기준
    day_of_week: Mapped[int]         = mapped_column(Integer, nullable=False)   # 0=월...6=일
    computed_at: Mapped[datetime]    = mapped_column(DateTime(timezone=True), server_default=func.now())
```

`StopCluster.matched_signal_id`로 기존 `signals`(신호등) 테이블과 연결해, "이 정지가
실제 신호등 대기였는지" 판별한다 — 기존 `ST_DWithin` 매칭 로직을 재사용.
`TripSegmentFeature.signal_stop_count`는 원래 초안의 `signal_count_on_route`(경로
전체의 신호등 개수) 대신, 실제로 감지된 정지 구간 중 신호등과 매칭된 개수로
구현했다 — 별도 route 정보 없이 이미 계산된 stop_clusters에서 바로 집계할 수
있어서다.

## 4. API 엔드포인트

`app/routers/auth.py`, `app/routers/gps.py`가 구현 완료. `/eta/*`는 3~4단계에서
피처 계산·모델이 준비된 뒤 추가할 예정.

| Method | Path | 설명 | 상태 |
|---|---|---|---|
| POST | `/auth/register` | 이메일/비밀번호 회원가입 → 액세스 토큰 발급 | 완료 |
| POST | `/auth/login` | 로그인 → 액세스 토큰 발급 | 완료 |
| GET | `/auth/me` | 현재 로그인 사용자 정보 | 완료 |
| POST | `/gps/trips` | trip 시작 (label, origin, target_arrival_at 등록) → trip_id 반환. 로그인 필요 | 완료 |
| GET | `/gps/trips` | 로그인한 사용자의 trip 목록 (최신순). 앱의 "이전 기록 불러오기"용 | 완료 |
| GET | `/gps/trips/{id}` | trip 상태 조회 (본인 소유만) | 완료 |
| POST | `/gps/trips/{id}/points` | GPS 포인트 배치 업로드 (앱이 주기적으로 호출) | 완료 |
| POST | `/gps/trips/{id}/finish` | trip 종료 → 노이즈 제거·ST-DBSCAN·피처 계산을 백그라운드로 트리거 | 완료 |
| GET | `/gps/trips/{id}/stops` | 탐지된 정지 구간 목록 (디버깅/확인용) | 완료 |
| GET | `/gps/trips/{id}/features` | 속도/거리/정지 등 ETA 학습용 피처 (디버깅/확인용) | 완료 |
| POST | `/eta/train` | trip_segment_features로 XGBoost 분위수 회귀 population 모델 재학습 | 완료 |
| GET | `/eta/predict` | 현재 위치 + 목적지 + 목표 도착시각 → 예상 소요시간(p10/p50/p90), 정시 도착 확률 | 완료 |
| GET | `/eta/departure-recommendation` | 위 확률 기반 출발 상태 문구 반환 | 완료 |

`/gps/*` 엔드포인트는 모두 `Authorization: Bearer <token>` 필요. trip 소유자가
아니면 403을 반환한다 (`app/routers/gps.py`의 `_get_own_trip_or_404`).

## 5. ML 파이프라인 설계

**1차 (baseline, XGBoost) — 구현 완료 (`app/services/eta_model.py`)**
- 입력 피처(실제 구현): `distance_m`, `hour_of_day`/`day_of_week`(Asia/Seoul 기준),
  이력 평균속도(`historical_avg_speed_mps` — 개인+기록이름 → population → 기본값
  1.2m/s 순으로 폴백, 어느 단계였는지는 `historical_source` 0/1/2로 별도 피처화),
  이력 평균 정지 횟수(`historical_avg_stop_count`, population 기준).
  원래 초안의 "신호등 개수/실시간 교통 보정"은 이번 baseline에서는 빠졌다 — route
  정보 없이 이미 계산된 `trip_segment_features`만으로 구성했다.
- 라벨: 실측 `actual_duration_s`.
- 학습: `POST /eta/train`이 `trip_segment_features` + `gps_trips`를 조인해
  전체 사용자 데이터로 population 모델 하나를 재학습한다 (사용자별 별도 모델 아님 —
  개인화는 "이력 평균속도" 피처를 통해서만 반영됨). 표본이 `ETA_MIN_TRAINING_SAMPLES`
  (기본 20건) 미만이면 학습을 거부한다.
- **데이터 누수 방지**: 각 학습 행의 "이력 평균속도/정지횟수"는 해당 trip의
  `started_at` *이전에* 완료된 trip만으로 계산한다 (SQL 상관 서브쿼리). 이걸
  안 했으면 모델이 답을 몰래 참조하는 꼴이라 실제로는 못 쓰는 모델이 나옴.
- **콜드스타트(모델 자체가 없을 때)**: `GET /eta/predict`는 학습된 모델 파일이
  없으면 "거리 ÷ 이력평균속도 + 정지횟수 × 기본 대기시간(20초)" 규칙 기반으로
  대체한다 — 데이터가 하나도 없어도 항상 응답은 준다.
- **분위수 교차(quantile crossing) 처리**: `reg:quantileerror`로 p10/p50/p90을
  한 번에 학습하는데, 표본이 적으면 순서가 뒤집히는 경우가 실제로 관찰됐다
  (검증 스크립트로 확인). 예측값을 정렬해서 단조성을 강제하는 방식으로 대응했다 —
  개별 분위수의 미세한 정확도보다 순서 보장을 우선함.

**2차 (확장, LSTM) — 미착수**
- 입력: 정규화된 GPS 궤적 시퀀스(구간별 속도·정지 패턴) — trip 내부 시계열 처리
- 목적: XGBoost가 못 잡는 "사람마다 다른 이동 습관(예: 신호 무시하고 빨리 건너는지,
  중간에 매번 들르는 곳이 있는지)" 같은 시퀀스 패턴 학습.
- 데이터가 충분히 쌓이기 전(사용자당 최소 수십 회 이동 기록)에는 XGBoost 단독 사용,
  이후 앙상블 또는 대체.

**정시 도착 확률 산출 — 구현 완료**
- 분위수 3점(p10/p50/p90)을 CDF의 앵커점으로 보고 선형 보간/외삽해
  `P(실제 소요시간 ≤ 남은시간)`을 근사한다 (`_cdf_from_quantiles`). 표본이 적을
  때는 부정확할 수 있으나 데이터가 쌓일수록 분위수 추정과 함께 개선된다.

**콜드스타트 대응 (개인화) — 구현 완료, 초안과 다른 방식**
- 원래 초안은 "베이지안 shrinkage/가중평균으로 개인 모델과 population 모델을
  섞는" 방식을 가정했지만, 실제로는 **모델을 하나만 두고 개인 이력을 피처로
  주입**하는 더 단순한 방식으로 구현했다 (개인 이력 있으면 그 값, 없으면 population
  평균, 그것도 없으면 기본값 — `historical_source`로 어느 단계인지 모델에 알려줌).
  사용자당 수십 건이 쌓이기 전까지는 이 방식이 학습/운영 모두 더 안정적이라 판단.

## 6. Flutter 앱 요구사항

`directions-flutter`는 `flutter create`로 새로 스캐폴딩하고 최소 트래킹 기능을
구현 완료 (아래 "구현 현황" 참고). 남은 항목:

- [x] 위치 권한: 포그라운드(`geolocator`) — 완료
- [ ] 백그라운드 트래킹(Android `ACCESS_BACKGROUND_LOCATION`, iOS `Always` 권한,
  `flutter_background_service` 등) — 미구현. 앱이 화면에 떠 있을 때만 수집된다.
- [x] 배치 업로드 (20개 모이거나 30초마다) — 완료, `LocationTracker` 참고
- [ ] 로컬 큐잉/재시도: 지금은 업로드 실패 시 메모리 버퍼에만 되돌리므로 앱이
  종료되면 유실된다. SQLite/Hive 영속 큐는 미구현.
- 배터리 최적화: `distanceFilter: 5m`로 불필요한 업데이트는 억제했지만, 정지 감지 시
  간격을 더 늘리는 적응형 로직은 아직 없음.
- [x] **정지 중 포인트 부족 문제 수정**: `distanceFilter: 5m`이면 실제로 멈춰
  있을 때(신호 대기 등) 위치 스트림이 새 값을 안 보내, 서버의 ST-DBSCAN이
  요구하는 최소 포인트 수(min_pts)를 못 채울 수 있었다. `TripRecordingController`에
  15초마다 현재 위치를 강제로 한 번씩 찍는 하트비트 타이머를 추가해 해결.
- [ ] 목표 도착 시각 입력 UI, ETA/추천 결과 표시 화면, 알림 — `/eta/*` API가
  없어서 아직 미구현.

## 7. 단계별 로드맵

1. ✅ **데이터 수집 파이프라인** — `User`/`GpsTrip`/`GpsPoint` 테이블, 인증(JWT) +
   업로드 API, Flutter 로그인/추적 화면까지 구현 완료. 실 데이터 축적을 시작할 수
   있는 상태. (백그라운드 트래킹·로컬 큐잉은 남음 — 6절 참고)
2. ✅ **전처리 + ST-DBSCAN** — 노이즈 제거, 정지 클러스터링, `StopCluster` 저장,
   기존 `signals` 테이블과 매칭까지 구현 완료 (`app/services/gps_processing.py`).
   trip 종료(`POST /gps/trips/{id}/finish`) 시 백그라운드로 자동 실행되고,
   결과는 `GET /gps/trips/{id}/stops`로 확인 가능. 순수 함수(노이즈 제거,
   ST-DBSCAN)는 합성 시나리오로 동작 검증 완료 — 다만 실제 DB(PostGIS)에 연결한
   end-to-end 테스트는 이 환경에 Docker/로컬 PostGIS가 없어 못 했음 (1단계와 동일한
   제약, 10절 참고).
3. ✅ **속도/거리 계산 + 피처 저장** — `TripSegmentFeature` 생성 로직 구현 완료
   (`app/services/gps_processing.py`의 `_compute_and_store_feature`). trip
   종료 시 정지 클러스터링 직후 같은 백그라운드 작업에서 실행되어
   `trip_segment_features`에 upsert되고, `GET /gps/trips/{id}/features`로
   확인 가능. 계산 항목: 누적 거리(노이즈 제거 후 GPS 궤적), 실측
   소요시간·이동시간·정지시간, 정지 횟수·신호등 매칭 정지 횟수, 평균 속도,
   출발 시각의 시간대·요일(Asia/Seoul 기준). `_total_distance_m` 순수 함수는
   단위 검증 완료.
4. ✅ **ETA baseline (XGBoost)** — `POST /eta/train`(재학습), `GET /eta/predict`
   (p10/p50/p90 + 정시 도착 확률) 구현 완료. **아직 실제 학습 데이터가 없어
   검증은 못 했음** — 순수 함수(이력 폴백, CDF 근사, 휴리스틱)와 XGBoost 분위수
   회귀 자체의 동작(합성 데이터)만 확인. 데이터가 쌓이면 `POST /eta/train` 호출
   후 `GET /eta/predict`로 실제 정확도를 봐야 함. 앱(Flutter)에는 아직 연결 안 됨.
5. ✅ **출발 추천 로직** — `recommend_departure()`가 정시 도착 확률을
   `comfortable`/`on_time`/`urgent`/`late` 4단계(문구: "여유 있는 출발"/"정시
   출발"/"늦어도 지금은 출발"/"이미 늦음")로 매핑, `GET /eta/departure-recommendation`
   구현 완료. 임계값 경계값(0.9/0.6/0.3) 양쪽 다 검증 완료. 임계값은 실측
   데이터 없이 정한 가정치라 데이터가 쌓이면 보정 필요.
   **앱 연동도 완료**: `lib/features/eta/`(domain/data/presentation) 신규 추가,
   `trip_tracking_page.dart`의 "출발" 시 이 API를 30초마다 호출해 카드로 표시
   (`label`만 넘기고 좌표는 안 줌 — 서버가 기록 이름 기준 과거 평균 거리로
   대신 계산하도록 `eta_model.py`에 `_resolve_distance_m` 폴백을 추가했다).
   기존 로컬 타이머 기반 경고는 그대로 두고, AI 추천은 별도 카드로 나란히
   보여준다. 표본이 적거나(<20건) heuristic일 때는 "아직 기록이 적어 정확하지
   않을 수 있어요" 안내를 함께 띄운다. `dart analyze`/`flutter test` 통과 —
   단, 이 환경의 Android Studio JBR(java.exe)이 손상돼 있어 APK 빌드까지는
   확인 못 했다 (Dart/Flutter 코드 자체 문제 아님, gradle의 JDK 탐색 문제).
6. ✅ **LSTM 고도화 — 오프라인 벤치마크 파이프라인까지 구현 완료, 라이브 연결은 보류**
   `app/services/eta_lstm.py`: trip GPS 시퀀스(`[delta_t_s, delta_dist_m, is_stop]`)를
   패딩해 LSTM(분위수 회귀, `reg:quantileerror`와 같은 pinball loss)으로 학습하고,
   `POST /eta/train-lstm`이 시간순 홀드아웃에서 XGBoost와 MAE를 비교해 돌려준다.

   **구현 중 발견한 중요한 설계 문제**: 원래 계획대로 "LSTM이 trip 시퀀스를
   학습해 duration을 맞춘다"는 건, 출발 *전* 예측(`/eta/predict`)에는 그 trip의
   시퀀스가 아직 존재하지 않아 그대로 쓸 수 없다. 그래서 지금은 **완료된 과거
   trip들로 XGBoost 대비 정확도를 재보는 오프라인 벤치마크 용도로만** 구현했고,
   `/eta/predict`/`/eta/departure-recommendation` 라이브 예측에는 연결하지 않았다.
   벤치마크에서 LSTM이 확실히 더 낫고 표본도 충분해지면, 그다음 단계로 "사용자+
   기록이름별 과거 시퀀스의 평균 임베딩"을 XGBoost 피처에 추가하는 식으로
   라이브 연결을 고려할 수 있다 (아직 미구현 — 데이터도, 벤치마크 결과도 없어
   지금 미리 만드는 건 시기상조라고 판단).

   **또 하나 발견한 문제 — 배포 환경 호환성**: `torch`(그리고 이미 4단계에서
   넣은 `xgboost`)는 Alpine(musl) wheel을 배포하지 않는다. 기존 `Dockerfile`이
   Alpine 기반이라 그대로였으면 실제 배포에서 설치가 안 됐을 것 — 이번에
   `python:3.11-slim-bookworm`(Debian, glibc)으로 전환했다 (Docker가 이 환경에
   없어 실제 빌드 검증은 못 했음, 직접 확인 필요). `torch`는 여전히 무겁고
   지금은 오프라인 벤치마크에만 쓰이므로 `requirements.txt`(Docker 이미지에
   포함)가 아니라 `requirements-optional.txt`로 분리했다 — 설치 안 해도 기본
   API는 정상 동작하고, `/eta/train-lstm`만 501을 반환한다 (torch를 가짜로
   차단한 상태에서 앱 전체 임포트 + HTTP 501 응답까지 실제로 검증함).

   학습 루프 자체(패딩, LSTM forward, pinball loss, 정렬 기반 분위수 교차 방지)는
   합성 데이터로 검증 완료. 학습률 0.01은 너무 느리게 수렴해 0.05로,
   epoch도 60→200으로 올렸다 (그래도 실제 데이터로는 다시 튜닝 필요).

데이터가 없으면 3~6단계 모델이 무의미하므로, 1~2단계를 먼저 끝내고 최소 몇 주간
실사용 데이터를 모으는 기간을 계획에 넣는 것을 권장한다.

## 8. 기술 스택 추가 항목

- `requirements.txt` 추가 후보: `scikit-learn`(전처리/거리 계산 보조),
  `xgboost`, `numpy`, `pandas`. ST-DBSCAN은 표준 라이브러리가 마땅치 않아
  `scikit-learn`의 `DBSCAN`을 3차원(위경도+정규화된 시간축) 입력으로 응용하거나
  직접 구현.
- LSTM 단계 도입 시 `torch` 또는 `tensorflow` 추가 (모델 서빙 방식 별도 검토 —
  FastAPI 프로세스 내 추론 vs 별도 추론 서버).
- 모델 학습은 API 프로세스와 분리(배치 스크립트/Celery/cron)해서 API 응답 지연을
  일으키지 않도록 한다.

## 9. 확인이 필요한 사항

- ~~개인화 범위~~ → **결정됨**: 익명 기기 UUID 대신 이메일/비밀번호 + JWT
  인증을 먼저 구현하고, `GpsTrip.user_id`는 인증된 `users.id`를 사용하기로 함.
- **목표 도착 시각 입력 주체**: 사용자가 직접 입력("9시까지 도착")하는 방식으로
  가정. 캘린더 연동 등은 범위 밖으로 둠. (`GpsTripCreate.target_arrival_at`으로
  받는 필드는 이미 구현했지만, 앱 UI에서 입력받는 화면은 아직 없음)
- **실시간 연속 트래킹 vs 단발성 조회**: "이동 중 계속 추적하며 추천 갱신"까지
  포함할지, 아니면 "출발 전 한 번 조회"만 할지에 따라 앱 백그라운드 트래킹
  복잡도가 크게 달라짐. 위 계획은 이동 전체를 추적하는 것을 전제로 하되, 현재
  구현은 포그라운드 추적까지만 됨.
- **위치 데이터 보관/프라이버시**: 원시 GPS는 개인 이동 패턴을 드러내는
  민감 정보이므로 보관 기간, 익명화, 사용자 삭제 요청 처리 방침을 정해야 함
  (학원 프로젝트 범위에서는 최소한 "탈퇴 시 삭제" 정도는 명시 권장). 아직 미구현
  — 회원 탈퇴 API와 함께 처리 필요.

## 10. 1단계 구현 현황 및 실행 방법

**백엔드** (`directions-api`)
- 추가 파일: `app/auth.py`(비밀번호 해시·JWT), `app/routers/auth.py`,
  `app/routers/gps.py`, `app/models.py`/`app/schemas.py`에 관련 모델·스키마 추가.
- `requirements.txt`에 `bcrypt`, `PyJWT`, `email-validator` 추가.
- 검증: 로컬 venv에 의존성 설치 후 앱 임포트·라우트 등록·OpenAPI 스키마 생성 확인,
  `hash_password`/`verify_password`/`create_access_token`의 JWT 왕복까지 단위
  테스트 완료. **단, 이 환경에는 Docker와 로컬 PostGIS가 없어 실제 DB에 연결한
  end-to-end 테스트(회원가입→trip 생성→포인트 업로드)는 못 했다.** 아래 명령으로
  직접 확인 권장:
  ```
  docker compose -f docker-compose.dev.yml up
  # 이후 http://localhost:8000/docs 에서 /auth/register → /gps/trips → /gps/trips/{id}/points 순서로 테스트
  ```

**앱** (`directions-flutter`)
- `flutter create`로 신규 스캐폴딩 (Android/iOS). `geolocator`, `http`,
  `shared_preferences` 의존성 추가.
- 화면: `LoginScreen`(회원가입/로그인) → `TrackingScreen`(이동 시작/종료, 실시간
  수집 개수 표시).
- `dart analyze` 통과, 위젯 테스트 1개 통과, **`flutter build apk --debug` 로 실제
  APK 빌드까지 성공 확인**.
- 주의: 프로젝트 경로에 한글/공백(`01_프로젝트`)이 포함돼 있어 Gradle이 기본적으로
  빌드를 거부한다. `android/gradle.properties`에 `android.overridePathCheck=true`를
  추가해 우회했다 — 가능하면 추후 ASCII 전용 경로로 옮기는 것이 더 안전하다.
- API 주소는 `lib/api_config.dart`의 `kApiBaseUrl`(기본값 `http://10.0.2.2:8000`,
  Android 에뮬레이터에서 PC의 localhost를 가리킴)이며,
  `flutter run --dart-define=API_BASE_URL=http://<PC-IP>:8000`으로 실제 기기에서
  바꿔 실행 가능.

## 11. 다음 액션

1. 위 명령으로 백엔드 DB 연동을 직접 확인 (Docker 실행 후 `/docs`에서 수동 테스트).
2. 실기기/에뮬레이터에서 `flutter run`으로 로그인→이동 시작→GPS 수집→이동 종료
   흐름을 눈으로 확인.
3. 문제 없으면 2단계(노이즈 제거 + ST-DBSCAN 정지 클러스터링) 착수.
