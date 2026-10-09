"""Minimal Supabase Storage client (REST API, service-role key, server side only)."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from app.core.config import Settings
from app.core.errors import DependencyUnavailable


class SupabaseStorage:
    def __init__(self, settings: Settings) -> None:
        key = settings.supabase_service_role_key.get_secret_value()
        self._client = httpx.AsyncClient(
            base_url=f"{settings.supabase_url.rstrip('/')}/storage/v1",
            headers={"Authorization": f"Bearer {key}", "apikey": key},
            timeout=httpx.Timeout(30.0, connect=5.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _object_path(bucket: str, path: str) -> str:
        return f"/object/{quote(bucket)}/{quote(path.lstrip('/'))}"

    async def upload(
        self, bucket: str, path: str, data: bytes, *, content_type: str, upsert: bool = True
    ) -> None:
        response = await self._client.post(
            self._object_path(bucket, path),
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "true" if upsert else "false"},
        )
        self._raise_for_status(response, "upload")

    async def download(self, bucket: str, path: str) -> bytes:
        response = await self._client.get(self._object_path(bucket, path))
        self._raise_for_status(response, "download")
        return response.content

    async def delete(self, bucket: str, path: str) -> None:
        response = await self._client.delete(self._object_path(bucket, path))
        if response.status_code != 404:
            self._raise_for_status(response, "delete")

    async def ping(self) -> None:
        response = await self._client.get("/bucket")
        self._raise_for_status(response, "ping")

    @staticmethod
    def _raise_for_status(response: httpx.Response, action: str) -> None:
        if response.is_success:
            return
        # The response body may echo the object path but never the key; keep it short regardless.
        raise DependencyUnavailable(
            f"Supabase Storage {action} failed with HTTP {response.status_code}.",
            details={"body": response.text[:200]},
        )
