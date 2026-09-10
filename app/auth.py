"""
JWT 기반 인증 유틸리티.

- 비밀번호 해시/검증: bcrypt
- 액세스 토큰 발급/검증: PyJWT (HS256)
- get_current_user: `Authorization: Bearer <token>` 헤더로 현재 사용자를 조회하는
  FastAPI 의존성. 로그인이 필요한 라우터에서 Depends(get_current_user)로 사용한다.
- require_admin: `X-Admin-Token` 헤더를 검사하는 의존성. 데이터 적재처럼 비용이
  큰 관리자 작업을 보호한다.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import (
    ADMIN_API_TOKEN,
    JWT_ALGORITHM,
    JWT_EXPIRE_MINUTES,
    JWT_SECRET_KEY,
)
from .database import get_db
from .models import User

# tokenUrl은 Swagger UI(/docs)의 Authorize 버튼용 안내일 뿐, 실제 검증은 아래
# get_current_user에서 토큰 자체를 디코딩해서 수행한다.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))


def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="인증 정보가 유효하지 않습니다.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_error

    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise credentials_error

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise credentials_error
    return user


def require_admin(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """
    관리자 토큰을 검사하는 의존성. 통과하지 못하면 요청을 그 자리에서 끊는다.

    - ADMIN_API_TOKEN이 설정돼 있지 않으면 503으로 막는다(fail-closed). 토큰을
      잊고 배포했을 때 엔드포인트가 열려 있는 편이 더 위험하기 때문이다.
    - 비교는 secrets.compare_digest로 해서 타이밍 공격으로 토큰을 한 글자씩
      알아내지 못하게 한다.
    """
    if not ADMIN_API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="관리자 작업이 비활성화되어 있습니다 (ADMIN_API_TOKEN 미설정).",
        )
    if not x_admin_token or not secrets.compare_digest(x_admin_token, ADMIN_API_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자 권한이 필요합니다.",
        )
