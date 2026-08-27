from __future__ import annotations

import html
import json
import re
import asyncio
from asyncio import TimeoutError, create_task, gather
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from aiohttp import ClientError

from astrbot.api import logger

from ..base import BaseParser, handle
from ...data import FileContent, ImageContent, MediaContent, Platform
from ...exception import ParseException
from .nsfw import create_blurred_cover, create_body_pdf
from .app_client import PixivAppClient


class PixivParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="pixiv", display_name="Pixiv")

    _artwork_url = "https://www.pixiv.net/artworks/{}"

    def __init__(self, config, downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.pixiv
        self.web_headers = self.headers.copy()
        self.web_headers.update(
            {
                "Accept": "application/json",
                "Referer": "https://www.pixiv.net/",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        if self.mycfg.cookies:
            self.web_headers["Cookie"] = self.mycfg.cookies
        self.image_headers = self.headers.copy()
        self.image_headers["Referer"] = "https://www.pixiv.net/"

    @staticmethod
    def _clean_comment(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).replace("\r\n", "\n").replace("\r", "\n")
        # Pixiv 简介是 HTML。先保留显式换行与块级元素的分段，再移除样式标签。
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(
            r"</?(?:p|div|li|h[1-6]|blockquote)[^>]*>",
            "\n",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text or None

    @staticmethod
    def _page_limit(value: Any) -> int:
        try:
            return max(1, min(20, int(value)))
        except (TypeError, ValueError):
            return 3

    @staticmethod
    def _image_urls(body: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        for page in body.get("metaPages") or []:
            image_urls = page.get("image_urls") or {}
            if image_urls.get("original"):
                urls.append(image_urls["original"])
        if not urls:
            original = (body.get("urls") or {}).get("original")
            if original:
                urls.append(original)
        return urls

    @staticmethod
    def _stats_text(body: dict[str, Any]) -> str | None:
        """将 Pixiv 作品详情中的互动数据转换为卡片底部说明。"""
        fields = (
            ("viewCount", "浏览"),
            ("likeCount", "点赞"),
            ("bookmarkCount", "收藏"),
            ("commentCount", "评论"),
        )
        stats: list[str] = []
        for key, label in fields:
            value = body.get(key)
            if value is None:
                continue
            try:
                stats.append(f"{label} {int(value):,}")
            except (TypeError, ValueError):
                logger.debug("Pixiv 作品互动数据异常 %s=%r", key, value)
        return " · ".join(stats) or None

    @staticmethod
    def _tags(body: dict[str, Any]) -> list[str]:
        """按作品解析的规则提取 Pixiv 标签，优先使用翻译名。"""
        raw_tags = body.get("tags") or {}
        tag_items = raw_tags.get("tags", []) if isinstance(raw_tags, dict) else raw_tags
        tags = [
            str(tag.get("translated_name") or tag.get("tag") or "").strip()
            for tag in tag_items
            if isinstance(tag, dict)
        ]
        return [tag for tag in tags if tag]

    async def _fetch_user_avatar(self, user_id: Any) -> str | None:
        """通过作者接口获取头像；作品详情未携带头像时使用此回退。"""
        if not user_id:
            return None
        try:
            async with self.session.get(
                f"https://www.pixiv.net/ajax/user/{user_id}",
                headers=self.web_headers,
                proxy=self.proxy,
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
            body = payload.get("body") if isinstance(payload, dict) else None
            if isinstance(body, dict):
                avatar = body.get("imageBig") or body.get("image") or body.get("profileImageUrl")
                return str(avatar) if avatar else None
        except (ClientError, TimeoutError, ValueError, TypeError):
            logger.warning("Pixiv 作者头像获取失败: %s", user_id)
        return None

    async def _fetch_novel_series_total(self, series_id: Any) -> int | None:
        """获取小说系列总话数；系列接口失败时降级为无系列总数。"""
        if not series_id:
            return None
        try:
            async with self.session.get(
                f"https://www.pixiv.net/ajax/novel/series/{series_id}",
                headers=self.web_headers,
                proxy=self.proxy,
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
            if not isinstance(payload, dict) or payload.get("error"):
                return None
            body = payload.get("body")
            if not isinstance(body, dict) or body.get("total") is None:
                return None
            return int(body["total"])
        except (ClientError, TimeoutError, ValueError, TypeError):
            logger.warning("Pixiv 小说系列信息获取失败: %s", series_id)
            return None

    async def _novel_series_text(self, body: dict[str, Any], user_id: Any) -> str | None:
        series = body.get("seriesNavData")
        if not isinstance(series, dict) or not series.get("seriesId"):
            return None
        series_id = series.get("seriesId")
        order = series.get("order")
        series_title = str(series.get("title") or "未命名系列")
        total = await self._fetch_novel_series_total(series_id)
        progress = f"当前第 {order} 话" if order is not None else None
        if total is not None:
            progress = f"{progress}，共 {total} 话" if progress else f"共 {total} 话"
        series_url = (
            f"https://www.pixiv.net/user/{user_id}/series/{series_id}"
            if user_id
            else f"https://www.pixiv.net/novel/series/{series_id}"
        )
        details = " · ".join(part for part in (progress, series_url) if part)
        return f"小说系列：{series_title}" + (f"\n{details}" if details else "")

    def _create_blur_cover_pdf_contents(self, image_urls: list[str]) -> list[MediaContent]:
        """为 R18 多页静态作品创建模糊封面及正文 PDF 下载任务。"""
        if not image_urls:
            return []
        output_dir = self.cfg.cache_dir / "pixiv_nsfw"

        async def blur_cover() -> Path:
            source = await self.downloader.download_img(
                image_urls[0], headers=self.image_headers, proxy=self.proxy
            )
            return await create_blurred_cover(
                source,
                output_dir,
                self._blur_strength(getattr(self.mycfg, "nsfw_blur_strength", 70)),
            )

        contents: list[MediaContent] = [ImageContent(create_task(blur_cover()))]
        if len(image_urls) > 1:

            async def body_pdf() -> Path:
                sources = await gather(
                    *[
                        self.downloader.download_img(
                            url, headers=self.image_headers, proxy=self.proxy
                        )
                        for url in image_urls[1:]
                    ]
                )
                return await create_body_pdf(
                    sources, output_dir, self.cfg.source_max_size
                )

            contents.append(
                FileContent(
                    create_task(body_pdf()),
                    name="pixiv_r18_pages.pdf",
                )
            )
        return contents

    @staticmethod
    def _blur_strength(value: Any) -> int:
        try:
            return max(30, min(100, int(value)))
        except (TypeError, ValueError):
            return 70

    @handle(
        "pixiv.net/artworks/",
        r"https?://(?:www\.)?pixiv\.net/artworks/(?P<pid>\d+)(?:[/?#][^\s]*)?",
    )
    async def _parse_artwork(self, searched: re.Match[str]):
        pid = searched.group("pid")
        artwork_url = self._artwork_url.format(pid)
        endpoint = f"https://www.pixiv.net/ajax/illust/{pid}"
        try:
            async with self.session.get(
                endpoint,
                headers=self.web_headers,
                proxy=self.proxy,
            ) as response:
                if response.status in (401, 403):
                    raise ParseException("Pixiv 作品认证失败，请检查 cookies 配置")
                if response.status == 429:
                    raise ParseException("Pixiv 请求过于频繁，请稍后再试")
                if response.status >= 400:
                    raise ParseException(f"Pixiv 作品请求失败（HTTP {response.status}）")
                payload = await response.json(content_type=None)
        except ParseException:
            raise
        except (ClientError, TimeoutError) as exc:
            logger.warning("Pixiv 作品请求失败 %s: %s", pid, exc)
            raise ParseException("Pixiv 作品请求失败，请稍后再试") from exc
        except (ValueError, TypeError) as exc:
            raise ParseException("Pixiv 返回数据格式异常") from exc

        if payload.get("error"):
            message = payload.get("message") or "作品不存在、已删除或无权访问"
            raise ParseException(f"Pixiv 作品解析失败：{message}")
        body = payload.get("body")
        if not isinstance(body, dict):
            raise ParseException("Pixiv 作品不存在、已删除或无权访问")

        title = str(body.get("illustTitle") or body.get("title") or f"Pixiv 作品 {pid}")
        user_name = str(body.get("userName") or body.get("userAccount") or "未知作者")
        user_id = body.get("userId")
        author_name = f"{user_name}（{user_id}）" if user_id else user_name
        avatar_url = await self._fetch_user_avatar(user_id)
        text = self._clean_comment(body.get("illustComment") or body.get("description"))
        # Web Ajax 实际响应为 {"authorId": ..., "tags": [{"tag": ...}]}；
        # 同时兼容可能出现的直接列表形式。
        tags = self._tags(body)
        if tags:
            tag_text = " ".join(f"#{tag}" for tag in tags)
            text = f"{text}\n{tag_text}" if text else tag_text
        body_text = text
        image_urls = self._image_urls(body)
        limit = self._page_limit(getattr(self.mycfg, "max_manga_pages", 3))
        truncated = len(image_urls) > limit
        image_urls = image_urls[:limit]

        nsfw = int(body.get("xRestrict") or body.get("x_restrict") or 0) > 0
        nsfw_mode = getattr(self.mycfg, "nsfw_mode", "ignore")
        extra: dict[str, str] = {}
        if nsfw and nsfw_mode == "ignore":
            # 标题由卡片标题区渲染；限制提示使用独立的大字号警告区，
            # 正文/标签保留在其后，形成“标题-空行-提示-空行-正文”的结构。
            extra["warning"] = "R18作品因后台设置不予展示"
            if not getattr(self.mycfg, "cookies", None):
                extra["warning"] += "\n未配置 Pixiv 网页登录 Cookie，可能无法获取完整作品内容"
            text = body_text
            image_urls = []

        if not image_urls and not text:
            raise ParseException("Pixiv 作品没有可用的图片或文本内容")
        if truncated:
            suffix = f"图片数量过多，超过设置限制；剩余详情请查看链接：{artwork_url}"
            text = f"{text}\n{suffix}" if text else suffix

        # list 在类型系统中不协变：将图片项扩展到 ParseResult 所需的媒体基类列表。
        # 不修改 BaseParser.create_image_contents 的返回约定，避免影响其他解析器。
        contents: list[MediaContent] = (
            self._create_blur_cover_pdf_contents(image_urls)
            if nsfw and nsfw_mode == "blur_cover_pdf"
            else []
        )
        if not contents:
            contents.extend(
                self.create_image_contents(image_urls, headers=self.image_headers)
            )

        return self.result(
            title=title,
            text=text,
            author=self.create_author(
                author_name,
                avatar_url=str(avatar_url) if avatar_url else None,
                headers=self.image_headers,
            ),
            contents=contents,
            timestamp=self._timestamp(body.get("createDate")),
            url=artwork_url,
            extra={**extra, **({"info": stats} if (stats := self._stats_text(body)) else {})},
        )

    @staticmethod
    def _clean_novel_content(value: Any) -> str | None:
        """清理 Pixiv 小说正文标记，同时保留正文换行。"""
        text = PixivParser._clean_comment(value)
        if not text:
            return None
        text = text.replace("[newpage]", "\n\n---\n\n")
        text = re.sub(r"\[\[jumpuri:\s*([^>]+?)\s*>[^\]]*\]\]", r"\1", text)
        text = re.sub(r"\[\[rb:\s*([^>]+?)\s*>[^\]]*\]\]", r"\1", text)
        text = re.sub(r"\[\[jump:[^\]]*\]\]", "", text)
        return text.strip() or None

    async def _create_novel_cover_contents(
        self, cover_url: Any, nsfw: bool, nsfw_mode: str
    ) -> list[MediaContent]:
        if not cover_url or (nsfw and nsfw_mode == "ignore"):
            return []
        url = str(cover_url)
        if nsfw and nsfw_mode == "blur_cover_pdf":
            output_dir = self.cfg.cache_dir / "pixiv_nsfw"

            async def blur_cover() -> Path:
                source = await self.downloader.download_img(
                    url, headers=self.image_headers, proxy=self.proxy
                )
                return await create_blurred_cover(
                    source,
                    output_dir,
                    self._blur_strength(getattr(self.mycfg, "nsfw_blur_strength", 70)),
                )

            return [ImageContent(create_task(blur_cover()))]
        return list(self.create_image_contents([url], headers=self.image_headers))

    async def _fetch_novel_series_detail(self, series_id: Any) -> dict[str, Any] | None:
        """获取小说系列完整资料，供系列预览使用。"""
        if not series_id:
            return None
        try:
            async with self.session.get(
                f"https://www.pixiv.net/ajax/novel/series/{series_id}",
                headers=self.web_headers,
                proxy=self.proxy,
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
            body = payload.get("body") if isinstance(payload, dict) else None
            return body if isinstance(body, dict) and not payload.get("error") else None
        except (ClientError, TimeoutError, ValueError, TypeError):
            logger.warning("Pixiv 小说系列详情获取失败: %s", series_id)
            return None

    async def _novel_series_text(self, body: dict[str, Any], user_id: Any) -> str | None:
        series = body.get("seriesNavData")
        if not isinstance(series, dict) or not series.get("seriesId"):
            return None
        series_id = series.get("seriesId")
        order = series.get("order")
        series_title = str(series.get("title") or "未命名系列")
        total = await self._fetch_novel_series_total(series_id)
        progress = f"当前第 {order} 话" if order is not None else None
        if total is not None:
            progress = f"{progress}，共 {total} 话" if progress else f"共 {total} 话"
        series_url = (
            f"https://www.pixiv.net/user/{user_id}/series/{series_id}"
            if user_id
            else f"https://www.pixiv.net/novel/series/{series_id}"
        )
        details = " · ".join(part for part in (progress, series_url) if part)
        return f"小说系列：{series_title}" + (f"\n{details}" if details else "")

    @handle(
        "pixiv.net/novel/show",
        r"https?://(?:www\.)?pixiv\.net/novel/show\.php\?(?:[^\s#]*&)?id=(?P<nid>\d+)(?:[&#][^\s]*)?",
    )
    @handle(
        "pixiv.net/novel/",
        r"https?://(?:www\.)?pixiv\.net/novel/(?P<nid>\d+)(?:[/?#][^\s]*)?",
    )
    async def _parse_novel(self, searched: re.Match[str]):
        nid = searched.group("nid")
        novel_url = f"https://www.pixiv.net/novel/show.php?id={nid}"
        endpoint = f"https://www.pixiv.net/ajax/novel/{nid}"
        try:
            async with self.session.get(endpoint, headers=self.web_headers, proxy=self.proxy) as response:
                if response.status in (401, 403):
                    raise ParseException("Pixiv 小说认证失败，请检查 cookies 配置")
                if response.status == 429:
                    raise ParseException("Pixiv 请求过于频繁，请稍后再试")
                if response.status >= 400:
                    raise ParseException(f"Pixiv 小说请求失败（HTTP {response.status}）")
                payload = await response.json(content_type=None)
        except ParseException:
            raise
        except (ClientError, TimeoutError) as exc:
            logger.warning("Pixiv 小说请求失败 %s: %s", nid, exc)
            raise ParseException("Pixiv 小说请求失败，请稍后再试") from exc
        except (ValueError, TypeError) as exc:
            raise ParseException("Pixiv 返回数据格式异常") from exc

        if not isinstance(payload, dict) or payload.get("error"):
            message = payload.get("message") if isinstance(payload, dict) else None
            raise ParseException(f"Pixiv 小说解析失败：{message or '小说不存在、已删除或无权访问'}")
        body = payload.get("body")
        if not isinstance(body, dict):
            raise ParseException("Pixiv 小说不存在、已删除或无权访问")

        title = str(body.get("title") or body.get("novelTitle") or f"Pixiv 小说 {nid}")
        user_name = str(body.get("userName") or body.get("userAccount") or "未知作者")
        user_id = body.get("userId")
        author_name = f"{user_name}（{user_id}）" if user_id else user_name
        avatar_url = await self._fetch_user_avatar(user_id)
        intro = self._clean_comment(body.get("description") or body.get("novelComment"))
        content = self._clean_novel_content(body.get("content"))
        tags = self._tags(body)
        series_text = await self._novel_series_text(body, user_id)
        metadata = "\n".join(
            part
            for part in (intro, " ".join(f"#{tag}" for tag in tags), series_text)
            if part
        )
        text_parts = [part for part in (metadata, content) if part]
        # 小说简介/标签与正文之间明确分隔，避免预览卡片中各段落粘连。
        body_text = "\n\n---\n\n".join(text_parts)
        nsfw = int(body.get("xRestrict") or body.get("x_restrict") or 0) > 0
        nsfw_mode = getattr(self.mycfg, "nsfw_mode", "ignore")
        extra: dict[str, str] = {}
        if nsfw and nsfw_mode == "ignore":
            extra["warning"] = "R18作品因后台设置不予展示"
            if not getattr(self.mycfg, "cookies", None):
                extra["warning"] += "\n未配置 Pixiv 网页登录 Cookie，可能无法获取完整作品内容"
        if not body_text:
            raise ParseException("Pixiv 小说正文为空")
        if count := body.get("characterCount"):
            try:
                extra["info"] = f"字数 {int(count):,}"
            except (TypeError, ValueError):
                pass
        contents = await self._create_novel_cover_contents(body.get("coverUrl"), nsfw, nsfw_mode)
        if nsfw and nsfw_mode == "ignore":
            contents = []
        if contents:
            # 渲染器按“标题—图片—正文”顺序绘制，文字区首行增加分隔线。
            body_text = f"---\n\n{body_text}"
        return self.result(
            title=title,
            text=body_text,
            author=self.create_author(author_name, avatar_url=str(avatar_url) if avatar_url else None, headers=self.image_headers),
            contents=contents,
            timestamp=self._timestamp(body.get("createDate")),
            url=novel_url,
            extra=extra,
        )

    @handle(
        "pixiv.net/novel/series/",
        r"https?://(?:www\.)?pixiv\.net/novel/series/(?P<series_id>\d+)(?:[/?#][^\s]*)?",
    )
    async def _parse_novel_series(self, searched: re.Match[str]):
        """解析小说系列入口，展示系列总话数。"""
        series_id = searched.group("series_id")
        detail = await self._fetch_novel_series_detail(series_id)
        if detail is None:
            raise ParseException("Pixiv 小说系列解析失败、已删除或无权访问")
        raw_total = detail.get("total")
        total: int | None = None
        if raw_total is not None:
            try:
                total = int(str(raw_total))
            except (TypeError, ValueError):
                total = None
        series_url = f"https://www.pixiv.net/novel/series/{series_id}"
        title = str(detail.get("title") or detail.get("seriesTitle") or f"Pixiv 小说系列 {series_id}")
        intro = self._clean_comment(detail.get("description") or detail.get("seriesComment"))
        tags = self._tags(detail)
        chapters = detail.get("novels") or detail.get("items") or detail.get("contents") or []
        chapter_lines: list[str] = []
        if isinstance(chapters, list):
            for index, chapter in enumerate(chapters, 1):
                if not isinstance(chapter, dict):
                    continue
                nid = chapter.get("id") or chapter.get("novelId") or chapter.get("novel_id")
                chapter_title = chapter.get("title") or chapter.get("novelTitle")
                if not chapter_title and not nid:
                    continue
                label = str(chapter_title or f"小说 {nid}")
                if nid:
                    label += f" (https://www.pixiv.net/novel/show.php?id={nid})"
                chapter_lines.append(f"{index}. {label}")
        sections = [part for part in (intro, " ".join(f"#{tag}" for tag in tags)) if part]
        if total is not None:
            sections.append(f"总共 {total} 话")
        if chapter_lines:
            sections.append("章节：\n" + "\n".join(chapter_lines))
        text = "\n\n---\n\n".join(sections) if sections else f"小说系列：{series_id}"
        return self.result(
            title=title,
            text=text,
            url=series_url,
        )

    @staticmethod
    def _timestamp(value: Any) -> int | None:
        if not value:
            return None
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError, OverflowError):
            return None

    async def close_session(self) -> None:
        for task in self._pixiv_tasks:
            task.cancel()
        if self._pixiv_tasks:
            await asyncio.gather(*self._pixiv_tasks, return_exceptions=True)
        if self.app_client:
            await self.app_client.close()
        await super().close_session()
