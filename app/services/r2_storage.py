"""
Cloudflare R2(S3 호환 오브젝트 스토리지) 클라이언트.

여정 분류 대표 이미지(app/routers/admin.py)를 여기 올리고/받아온다. API가
이미지 바이트를 직접 중계하는 방식이라(=클라이언트는 R2 주소를 몰라도 됨),
기존 `/admin/categories/{id}/image`, `/journeys/{label}/thumbnail` 엔드포인트의
응답 형태(이미지 바이트 그대로)는 하나도 안 바뀐다 — DB에서 읽던 걸 R2에서
읽도록 내부만 바뀐다.

R2는 S3 API와 호환되므로 boto3의 "s3" 클라이언트를 그대로 쓴다(전용 SDK 불필요).
boto3는 동기(sync) 라이브러리라, FastAPI의 이벤트 루프를 막지 않도록 매 호출을
`asyncio.to_thread`로 스레드풀에서 실행한다.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from ..config import (
    R2_ACCESS_KEY_ID,
    R2_ACCOUNT_ID,
    R2_BUCKET_NAME,
    R2_ENDPOINT_URL,
    R2_SECRET_ACCESS_KEY,
)


class R2UnavailableError(RuntimeError):
    """R2 관련 환경변수가 비어 있을 때. 라우터에서 503으로 변환한다."""


class R2ObjectNotFoundError(RuntimeError):
    """버킷에 해당 key의 오브젝트가 없을 때. 라우터에서 404로 변환한다."""


_client: Optional[BaseClient] = None


def _get_client() -> BaseClient:
    global _client
    if not (R2_ACCOUNT_ID or R2_ENDPOINT_URL) or not R2_ACCESS_KEY_ID or not R2_SECRET_ACCESS_KEY or not R2_BUCKET_NAME:
        raise R2UnavailableError(
            "R2 설정이 비어 있습니다. Cloudflare 대시보드 → R2 → Manage R2 API "
            "Tokens에서 발급받아 .env의 R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/"
            "R2_SECRET_ACCESS_KEY/R2_BUCKET_NAME을 채우세요."
        )
    if _client is None:
        endpoint = R2_ENDPOINT_URL or f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
        _client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            region_name="auto",
        )
    return _client


def _put_object_sync(key: str, data: bytes, content_type: str) -> None:
    client = _get_client()
    client.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=data, ContentType=content_type)


def _get_object_sync(key: str) -> bytes:
    client = _get_client()
    try:
        response = client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404"):
            raise R2ObjectNotFoundError(key) from exc
        raise
    return response["Body"].read()


def _delete_object_sync(key: str) -> None:
    client = _get_client()
    client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)


async def upload_image(key: str, data: bytes, content_type: str) -> None:
    await asyncio.to_thread(_put_object_sync, key, data, content_type)


async def download_image(key: str) -> bytes:
    return await asyncio.to_thread(_get_object_sync, key)


async def delete_image(key: str) -> None:
    """
    실패해도 조용히 넘어간다(오브젝트가 이미 없거나, R2 설정이 비어 있는 경우 등) —
    분류를 지우는 목적은 DB 행을 없애는 것이고, R2 오브젝트 정리는 덤이라
    여기서 막혀서 DB 삭제 자체를 못 하게 되면 안 된다.
    """
    try:
        await asyncio.to_thread(_delete_object_sync, key)
    except (ClientError, R2UnavailableError):
        pass
