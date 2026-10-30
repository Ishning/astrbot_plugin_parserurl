import asyncio
import time
import uuid
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import aiofiles
import emoji
from apilmoji import EmojiCDNSource
from apilmoji.source import HEADERS
from httpx import AsyncClient, HTTPError, InvalidURL, Timeout
from PIL import Image

from astrbot.api import logger

from .config import PluginConfig

EMOJI_STYLES = ("facebook", "apple", "google", "twitter")


@dataclass
class EmojiBudget:
    """同一卡片及其转发内容共用预算，从第一次 emoji 请求开始计时"""

    deadline: float | None = None


def iter_text_units(text: str) -> Iterator[tuple[str, bool]]:
    """按普通字符或完整 emoji 序列遍历，保留变体选择符"""
    position = 0
    for item in emoji.emoji_list(text):
        for char in text[position:item["match_start"]]:
            yield char, False
        yield item["emoji"], True
        position = item["match_end"]
    for char in text[position:]:
        yield char, False


class EmojiSource(EmojiCDNSource):
    """复用 Apilmoji 的缓存目录约定，补充多源、代理及样式降级"""

    # 限制 emoji 对卡片延迟的影响；后续可根据实际渲染耗时调整。
    RENDER_TIMEOUT = 8.0
    REQUEST_TIMEOUT = 2.0
    MAX_CONCURRENT = 8
    SOURCE_COOLDOWN = 30.0

    def __init__(self, config: PluginConfig):
        super().__init__(
            style=config.parser_emoji_style,
            cache_dir=config.cache_dir / "emojis",
            max_concurrent=self.MAX_CONCURRENT,
        )
        self.urls = list(dict.fromkeys(
            url.strip().rstrip("/")
            for url in config.parser_emoji_cdn if url.strip()
        ))
        self.styles = (self.style,) + tuple(
            style for style in EMOJI_STYLES if style != self.style
        )
        self.proxy = config.proxy if config.parser_emoji_use_proxy else None
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._unavailable_until: dict[str, float] = {}

    async def fetch_images(
        self, emojis: set[str], budget: EmojiBudget
    ) -> dict[str, Image.Image | None]:
        """在当前卡片的等待预算内获取图片；失败返回 None，不回退普通字体"""
        if not emojis:
            return {}
        if budget.deadline is None:
            budget.deadline = time.monotonic() + self.RENDER_TIMEOUT
        remaining = budget.deadline - time.monotonic()
        if remaining <= 0:
            # 预算耗尽后仍允许读取已有缓存，不发起网络请求。
            return {
                value: await self._cached_image(value, self.styles)
                for value in emojis
            }

        images: dict[str, Image.Image | None] = {}
        async with AsyncClient(
            proxy=self.proxy,
            headers=HEADERS,
            trust_env=False,
            timeout=Timeout(self.REQUEST_TIMEOUT),
            follow_redirects=True,
        ) as client:
            async def fetch(value: str) -> None:
                async with self._locks[value]:
                    images[value] = await self._fetch_image(client, value)

            try:
                await asyncio.wait_for(
                    asyncio.gather(*(fetch(value) for value in emojis)), remaining
                )
            except asyncio.TimeoutError:
                logger.info("Emoji 图片获取已达到当前卡片的等待预算")
        return {value: images.get(value) for value in emojis}

    async def _cached_image(
        self, value: str, styles: tuple[str, ...]
    ) -> Image.Image | None:
        for style in styles:
            # 旧配置使用大写 FACEBOOK 等目录名，已有有效图片可继续复用。
            for directory in (style, style.upper()):
                path = self._cache_dir / directory / f"{value}.png"
                if path.exists():
                    image = await asyncio.to_thread(self._read_image, path)
                    if image is not None:
                        return image
        return None

    @staticmethod
    def _read_image(path: Path) -> Image.Image | None:
        try:
            with Image.open(path) as image:
                return image.convert("RGBA")
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            return None

    async def _fetch_image(
        self, client: AsyncClient, value: str
    ) -> Image.Image | None:
        for style in self.styles:
            if image := await self._cached_image(value, (style,)):
                return image
            for url in self.urls:
                if self._unavailable_until.get(url, 0) > time.monotonic():
                    continue
                async with self._semaphore:
                    # 等待并发额度期间，另一个请求可能已将此源标为不可用。
                    if self._unavailable_until.get(url, 0) > time.monotonic():
                        continue
                    try:
                        response = await client.get(
                            f"{url}/{value}", params={"style": style}
                        )
                        if response.status_code != 200:
                            if response.status_code >= 500 or response.status_code == 429:
                                self._unavailable_until[url] = (
                                    time.monotonic() + self.SOURCE_COOLDOWN
                                )
                            continue
                        image = await asyncio.to_thread(
                            self._decode_image, response.content
                        )
                        if image is None:
                            continue
                    except (HTTPError, InvalidURL, ValueError):
                        self._unavailable_until[url] = (
                            time.monotonic() + self.SOURCE_COOLDOWN
                        )
                        logger.info("Emoji 图片源请求失败，暂时跳过该源")
                        continue
                    await self._save_image(value, style, response.content)
                    return image
        return None

    @staticmethod
    def _decode_image(content: bytes) -> Image.Image | None:
        try:
            with Image.open(BytesIO(content)) as image:
                return image.convert("RGBA")
        except (OSError, ValueError):
            return None

    async def _save_image(self, value: str, style: str, content: bytes) -> None:
        path = self._cache_dir / style / f"{value}.png"
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(temporary, "wb") as file:
                await file.write(content)
            temporary.replace(path)
        except OSError:
            # 全局清理可能同时删除目录；当前已解码的图片仍可正常绘制。
            logger.info("Emoji 图片缓存写入失败，本次继续使用已下载图片")
        finally:
            temporary.unlink(missing_ok=True)
