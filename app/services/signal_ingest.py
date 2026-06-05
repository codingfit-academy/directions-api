"""
외부 API에서 받은 데이터를 signals 테이블에 적재한다.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .police_api import fetch_crossroads

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

    upsert_sql = text(
        """
        INSERT INTO signals (source, source_id, name, region_cd, geom, updated_at)
        VALUES (
            :source,
            :source_id,
            :name,
            :region_cd,
            ST_SetSRID(ST_MakePoint(:x, :y), 4326),
            NOW()
        )
        ON CONFLICT (source, source_id) DO UPDATE
        SET name = EXCLUDED.name,
            region_cd = EXCLUDED.region_cd,
            geom = EXCLUDED.geom,
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
