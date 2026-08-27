"""Pixiv App API client used by ranking and author subscriptions."""

from __future__ import annotations

from typing import Any
import asyncio

import msgspec
from pixivpy_async import AppPixivAPI


class PixivAppClient:
    """Small compatibility wrapper around PixivPy-Async 1.2.x."""

    def __init__(self, refresh_token: str, proxy: str | None = None):
        self.refresh_token = refresh_token
        self.api = AppPixivAPI(proxy=proxy) if proxy else AppPixivAPI()
        self.access_token: str | None = None

    async def login(self) -> str:
        """Refresh the OAuth token and return the new refresh token."""
        token = await self.api.login(refresh_token=self.refresh_token)
        self.access_token = getattr(self.api, "access_token", None)
        self.refresh_token = getattr(self.api, "refresh_token", self.refresh_token)
        return self.refresh_token

    async def _call(self, method, *args, **kwargs):
        last: Exception | None = None
        for attempt in range(3):
            try:
                return await method(*args, **kwargs)
            except Exception as exc:
                last = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        raise last  # type: ignore[misc]

    async def user_detail(self, uid: int) -> dict[str, Any]:
        return msgspec.to_builtins(await self._call(self.api.user_detail, uid))

    async def user_illusts(self, uid: int, kind: str) -> dict[str, Any]:
        return msgspec.to_builtins(await self._call(self.api.user_illusts, uid, type=kind))

    async def user_novels(self, uid: int) -> dict[str, Any]:
        return msgspec.to_builtins(await self._call(self.api.user_novels, uid))

    async def illust_ranking(self, mode: str, date: str | None = None) -> dict[str, Any]:
        if date is None:
            return msgspec.to_builtins(await self._call(self.api.illust_ranking, mode=mode))
        return msgspec.to_builtins(await self._call(self.api.illust_ranking, mode=mode, date=date))

    async def illust_detail(self, pid: int) -> dict[str, Any]:
        return msgspec.to_builtins(await self.api.illust_detail(pid))

    async def ugoira_metadata(self, pid: int) -> dict[str, Any]:
        return msgspec.to_builtins(await self.api.ugoira_metadata(pid))

    async def novel_text(self, nid: int) -> dict[str, Any]:
        return msgspec.to_builtins(await self.api.novel_text(nid))

    async def close(self) -> None:
        session = getattr(self.api, "session", None)
        if session is not None and not session.closed:
            await session.close()
