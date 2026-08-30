"""
회원가입/로그인 라우터.

- POST /auth/register : 이메일/비밀번호로 회원가입 → 액세스 토큰 발급
- POST /auth/login     : 로그인 → 액세스 토큰 발급
- GET  /auth/me        : 현재 로그인된 사용자 정보
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import create_access_token, get_current_user, hash_password, verify_password
from ..database import get_db
from ..models import User
from ..schemas import TokenOut, UserCreate, UserLogin, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenOut, status_code=201)
async def register(body: UserCreate, db: AsyncSession = Depends(get_db)):
    existing_email = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if existing_email:
        raise HTTPException(status_code=409, detail="이미 가입된 이메일입니다.")

    existing_username = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if existing_username:
        raise HTTPException(status_code=409, detail="이미 사용 중인 아이디입니다.")

    user = User(
        email=body.email,
        username=body.username,
        hashed_password=hash_password(body.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(user.id)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenOut)
async def login(body: UserLogin, db: AsyncSession = Depends(get_db)):
    user = (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이메일 또는 비밀번호가 올바르지 않습니다.",
        )

    token = create_access_token(user.id)
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return UserOut.model_validate(current_user)
