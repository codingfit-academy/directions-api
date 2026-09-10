"""
외부 API에서 받은 데이터를 signals 테이블에 적재한다.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import SessionLocal

from ..config import SIGNAL_LOOKUP_BUFFER_M
from .police_api import (
    fetch_crossroads,
    find_nearest_crossroad,
    get_cached_signal_cycle,
    is_in_seoul,
    load_signal_cycles,
)

POLICE_SOURCE = "police_crossroad"


async def ingest_police_crossroads(
    db: AsyncSession,
    region_cd: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict[str, int]:
    """
    경찰청 교차로기반정보서비스의 결과를 signals 테이블에 upsert.

    police_api에서 이미 WGS84 도(°) 단위로 변환되어 들어오므로
    SRID 4326으로 그대로 저장한다.
    """
    fetched = inserted = updated = 0

    # 신호 주기(교차로계획정보서비스)를 한 번 훑어 교차로별 대표값으로 준비한다.
    # 활용신청이 안 됐거나 실패하면 빈 dict가 되고, cycle_time은 NULL로 남는다.
    try:
        cycles = await load_signal_cycles()
    except Exception:
        cycles = {}

    upsert_sql = text(
        """
        INSERT INTO signals (source, source_id, name, region_cd, cycle_time, geom, updated_at)
        VALUES (
            :source,
            :source_id,
            :name,
            :region_cd,
            :cycle_time,
            ST_SetSRID(ST_MakePoint(:x, :y), 4326),
            NOW()
        )
        ON CONFLICT (source, source_id) DO UPDATE
        SET name = EXCLUDED.name,
            region_cd = EXCLUDED.region_cd,
            geom = EXCLUDED.geom,
            cycle_time = COALESCE(EXCLUDED.cycle_time, signals.cycle_time),
            updated_at = NOW()
        RETURNING (xmax = 0) AS inserted
        """
    )

    async for cr in fetch_crossroads(region_cd=region_cd):
        fetched += 1
        result = await db.execute(
            upsert_sql,
            {
                "source": POLICE_SOURCE,
                "source_id": cr.int_no,
                "name": cr.int_nm or None,
                "region_cd": cr.region_cd or None,
                "cycle_time": cycles.get(cr.int_no),
                "x": cr.x_coord,
                "y": cr.y_coord,
            },
        )
        row = result.first()
        if row and row[0]:
            inserted += 1
        else:
            updated += 1

        if limit is not None and fetched >= limit:
            break

    await db.commit()
    return {"fetched": fetched, "inserted": inserted, "updated": updated}


async def ensure_signal_near(
    db: AsyncSession,
    lat: float,
    lng: float,
    radius_m: Optional[float] = None,
) -> Optional[tuple[int, float]]:
    """
    좌표 주변의 신호등(교차로)을 확보해 (signals.id, 거리m)를 반환한다.

    사용자가 정지 구간을 "신호등"이라고 확인해준 시점에 호출한다. 전국 벌크 적재
    없이 그때그때 필요한 교차로만 가져와 저장하는 방식이라(= 요청할 때 가져오기),
    signals 테이블이 비어 있어도 바로 동작한다.

    1) 이미 DB에 있으면 그걸 쓰고,
    2) 없으면 경찰청 API에서 가장 가까운 교차로를 찾아 upsert한 뒤 쓴다.

    경찰청 신호 데이터는 서울만 제공되므로 서울 밖 좌표는 None을 반환한다.
    """
    buffer_m = float(radius_m if radius_m is not None else SIGNAL_LOOKUP_BUFFER_M)

    if not is_in_seoul(lat, lng):
        return None

    existing = (
        await db.execute(
            text(
                """
                SELECT id, ST_DistanceSphere(
                           geom, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)
                       ) AS distance_m
                FROM signals
                WHERE ST_DWithin(
                    geom::geography,
                    ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
                    :buffer_m
                )
                ORDER BY distance_m ASC
                LIMIT 1
                """
            ),
            {"lat": lat, "lng": lng, "buffer_m": buffer_m},
        )
    ).mappings().first()
    if existing:
        return int(existing["id"]), float(existing["distance_m"])

    found = await find_nearest_crossroad(lat, lng, buffer_m)
    if not found:
        return None
    crossroad, distance_m = found

    # 주기는 이미 메모리에 올라와 있을 때만 붙인다. 전체 스캔은 외부 호출이 수백 번이라
    # 사용자 요청을 붙잡아 둘 수 없다 — 비어 있으면 NULL로 두고, 신호등 적재
    # (POST /signals/ingest/police)가 나중에 채운다.
    cycle_time = get_cached_signal_cycle(crossroad.int_no)

    row = (
        await db.execute(
            text(
                """
                INSERT INTO signals (source, source_id, name, region_cd, cycle_time, geom, updated_at)
                VALUES (
                    :source, :source_id, :name, :region_cd, :cycle_time,
                    ST_SetSRID(ST_MakePoint(:x, :y), 4326), NOW()
                )
                ON CONFLICT (source, source_id) DO UPDATE
                SET name = EXCLUDED.name,
                    region_cd = EXCLUDED.region_cd,
                    geom = EXCLUDED.geom,
                    cycle_time = COALESCE(EXCLUDED.cycle_time, signals.cycle_time),
                    updated_at = NOW()
                RETURNING id
                """
            ),
            {
                "source": POLICE_SOURCE,
                "source_id": crossroad.int_no,
                "name": crossroad.int_nm or None,
                "region_cd": crossroad.region_cd or None,
                "cycle_time": cycle_time,
                "x": crossroad.x_coord,
                "y": crossroad.y_coord,
            },
        )
    ).mappings().first()
    return int(row["id"]), float(distance_m)


async def refresh_signal_cycles() -> int:
    """
    주기(cycle_time)가 비어 있는 신호등을 공공 API 값으로 채운다. 채운 개수를 반환.

    백그라운드 작업으로 돌리는 것을 전제로 한다 (요청을 붙잡지 않는다). 교차로계획
    정보서비스는 특정 교차로만 조회할 수 없어서 전체를 한 번 훑어야 하는데,
    load_signal_cycles()가 결과를 캐시(12시간)하므로 두 번째 호출부터는 외부 호출이
    없고 DB 갱신만 남는다.

    관리자 적재(POST /signals/ingest/police)를 한 번도 안 돌려도, 사용자가 정지
    구간을 신호등으로 확인해줄 때 이 작업이 뒤따라 실행돼 스스로 채워진다.
    """
    try:
        cycles = await load_signal_cycles()
    except Exception:
        return 0
    if not cycles:
        return 0

    update_sql = text(
        """
        UPDATE signals
        SET cycle_time = :cycle_time, updated_at = NOW()
        WHERE source = :source AND source_id = :source_id AND cycle_time IS NULL
        """
    )
    filled = 0
    try:
        async with SessionLocal() as db:
            for source_id, cycle_time in cycles.items():
                result = await db.execute(
                    update_sql,
                    {"source": POLICE_SOURCE, "source_id": source_id, "cycle_time": cycle_time},
                )
                filled += result.rowcount or 0
            await db.commit()
    except Exception:
        # 최선 노력(best-effort) 보강 작업이라, 실패해도 사용자 요청에는 영향이 없다.
        return filled
    return filled
