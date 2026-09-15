"""
관리자 전용 — 여정 썸네일 분류(ThumbnailCategory) CRUD.

directions-front의 관리자 페이지가 이 라우터로 분류(이름 + 대표 이미지)를 만들고
지운다. 새 기록 이름은 이 분류 목록 중 하나로 Gemini가 자동 분류된다
(app/services/gemini_classify.py). 전부 X-Admin-Token/Authorization 헤더로
보호한다 — 로그인한 일반 사용자도 호출할 수 없다(app/auth.py의 require_admin,
신호등 적재와 같은 패턴).

이미지 자체는 DB가 아니라 Cloudflare R2에 저장한다(app/services/r2_storage.py) —
여기 라우터는 R2에서 읽은 바이트를 그대로 중계할 뿐이라, directions-front/
directions-flutter 쪽 API 응답 형태는 그대로다.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_admin
from ..database import get_db
from ..models import ThumbnailCategory
from ..schemas import ThumbnailCategoryOut
from ..services.r2_storage import (
    R2ObjectNotFoundError,
    R2UnavailableError,
    delete_image,
    download_image,
    upload_image,
)

router = APIRouter(
    prefix="/admin/categories",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

# content-type → 확장자. R2 오브젝트 키에 붙이는 용도일 뿐(응답 Content-Type은
# DB의 mime_type 컬럼을 그대로 쓰므로) 모르는 타입이면 확장자 없이 저장해도 무방하다.
_EXTENSION_BY_CONTENT_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


@router.get("", response_model=list[ThumbnailCategoryOut])
async def list_categories(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(select(ThumbnailCategory).order_by(ThumbnailCategory.name))
    ).scalars().all()
    return [ThumbnailCategoryOut.model_validate(r) for r in rows]


@router.get("/{category_id}/image")
async def get_category_image(category_id: int, db: AsyncSession = Depends(get_db)):
    row = (
        await db.execute(select(ThumbnailCategory).where(ThumbnailCategory.id == category_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="분류를 찾을 수 없습니다.")
    try:
        data = await download_image(row.r2_key)
    except R2UnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except R2ObjectNotFoundError:
        raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다.")
    return Response(content=data, media_type=row.mime_type)


@router.post("", response_model=ThumbnailCategoryOut, status_code=201)
async def create_category(
    name: str = Form(..., min_length=1, max_length=50),
    image: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    clean_name = name.strip()
    existing = (
        await db.execute(select(ThumbnailCategory).where(ThumbnailCategory.name == clean_name))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="이미 같은 이름의 분류가 있습니다.")

    image_data = await image.read()
    if not image_data:
        raise HTTPException(status_code=400, detail="이미지 파일이 비어 있습니다.")

    content_type = image.content_type or "image/png"
    extension = _EXTENSION_BY_CONTENT_TYPE.get(content_type, "")
    key = f"thumbnail-categories/{uuid.uuid4().hex}" + (f".{extension}" if extension else "")

    try:
        await upload_image(key, image_data, content_type)
    except R2UnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    row = ThumbnailCategory(name=clean_name, r2_key=key, mime_type=content_type)
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        await delete_image(key)  # 방금 올린 이미지가 고아로 남지 않게 정리
        raise HTTPException(status_code=409, detail="이미 같은 이름의 분류가 있습니다.")
    await db.refresh(row)
    return ThumbnailCategoryOut.model_validate(row)


@router.delete("/{category_id}", status_code=204)
async def delete_category(category_id: int, db: AsyncSession = Depends(get_db)):
    row = (
        await db.execute(select(ThumbnailCategory).where(ThumbnailCategory.id == category_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="분류를 찾을 수 없습니다.")
    await delete_image(row.r2_key)
    await db.delete(row)
    await db.commit()
