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
from ...data import Author, FileContent, ImageContent, MediaContent, Platform
from ...exception import ParseException, SizeLimitException
from .nsfw import create_blurred_cover, create_body_pdf, create_ugoira_gif
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
        self._pixiv_tasks: list[asyncio.Task] = []
        self._media_tasks: set[asyncio.Task[Any]] = set()
        self._ugoira_semaphore = asyncio.Semaphore(2)
        self._state_lock = asyncio.Lock()
        self._app_ready = asyncio.Event()
        self.pixiv_data_dir = Path(self.cfg.data_dir) / "pixiv"
        self.pixiv_data_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self.pixiv_data_dir / "state.json"
        self._state: dict[str, Any] = {"ranking_sent": {}, "works_sent": {}, "user_names": {}}
        try:
            if self._state_file.exists():
                loaded = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._state.update(loaded)
        except (OSError, ValueError, TypeError) as exc:
            logger.error("[pixiv] 状态缓存损坏，已降级为空缓存: %s", exc)
        self.app_client: PixivAppClient | None = None
        self.sub_map: dict[int, dict[str, list[str]]] = {}
        for entry in getattr(self.mycfg, "sub_uids_users", None) or []:
            parts = str(entry).split("-")
            if not parts or not parts[0].isdigit():
                continue
            uid = int(parts[0]); groups, users = self._targets(str(entry))
            self.sub_map[uid] = {"groups": groups, "users": users}
        refresh_token = getattr(self.mycfg, "refresh_token", None)
        app_features_enabled = (
            getattr(self.mycfg, "sub_enable", False)
            or getattr(self.mycfg, "ranking_list", False)
            or getattr(self.mycfg, "ranking_list_R18", False)
        )
        if app_features_enabled and not refresh_token:
            logger.warning("[pixiv] 已启用榜单或作者订阅，但未配置 refresh_token，相关任务不会启动")
        if refresh_token and app_features_enabled:
            self.app_client = PixivAppClient(str(refresh_token), self.proxy)
            self._pixiv_tasks.append(asyncio.create_task(self._app_login(), name="task_pixiv_app_login"))
            if getattr(self.mycfg, "ranking_list_R18", False):
                self._pixiv_tasks.append(asyncio.create_task(self._validate_r18_mode(), name="task_pixiv_validate_day_r18"))
            if getattr(self.mycfg, "sub_enable", False):
                self._pixiv_tasks.append(asyncio.create_task(self._subscription_loop(), name="task_pixiv_subscription_loop"))
            if getattr(self.mycfg, "ranking_list", False) or getattr(self.mycfg, "ranking_list_R18", False):
                self._pixiv_tasks.append(asyncio.create_task(self._ranking_loop(), name="task_pixiv_ranking_loop"))

    async def _app_login(self) -> None:
        try:
            new_refresh = await self.app_client.login()  # type: ignore[union-attr]
            async with self._state_lock:
                old_refresh = getattr(self.mycfg, "refresh_token", None)
                self.mycfg.refresh_token = new_refresh
                try:
                    self.cfg.save_config()
                except Exception:
                    self.mycfg.refresh_token = old_refresh
                    raise
        except Exception as exc:
            logger.warning("[pixiv] App API OAuth 刷新或凭据写回失败: %s", exc)
        finally:
            self._app_ready.set()

    async def _validate_r18_mode(self) -> None:
        try:
            await self._app_ready.wait()
            await self.app_client.illust_ranking("day_r18")  # type: ignore[union-attr]
            logger.info("[pixiv] day_r18 榜单能力验证成功")
        except Exception as exc:
            logger.warning("[pixiv] day_r18 榜单不可用，请检查账号权限：%s", exc)

    async def _save_state(self) -> None:
        async with self._state_lock:
            for field in ("ranking_sent", "works_sent"):
                values = self._state.get(field, {})
                if len(values) > 5000:
                    self._state[field] = dict(sorted(values.items(), key=lambda item: item[1])[-5000:])
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )
            tmp.replace(self._state_file)

    @staticmethod
    def _app_items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            values = payload.get("illusts") or payload.get("novels") or []
        else:
            values = getattr(payload, "illusts", None) or getattr(payload, "novels", None) or []
        result: list[dict[str, Any]] = []
        for value in values:
            if isinstance(value, dict):
                result.append(value)
            elif hasattr(value, "__dict__"):
                result.append(vars(value))
            else:
                fields = ("id", "title", "type", "user", "meta_single_page", "meta_pages", "caption", "description", "create_date")
                result.append({field: getattr(value, field) for field in fields if hasattr(value, field)})
        return result

    def _app_result(self, item: dict[str, Any]):
        pid = str(item.get("id") or item.get("illust_id") or item.get("novel_id") or "")
        user = item.get("user") or {}
        title = str(item.get("title") or f"Pixiv 作品 {pid}")
        user_id = user.get("id") or user.get("user_id") or item.get("user_id")
        user_name = str(user.get("name") or "未知作者")
        author_name = f"{user_name}（{user_id}）" if user_id else user_name
        profile_urls = user.get("profile_image_urls") or {}
        avatar_url = (
            profile_urls.get("medium")
            or profile_urls.get("original")
            or user.get("profile_image_url")
        )
        is_novel = item.get("type") == "novel" or item.get("novel_id")
        urls: list[str] = []
        single = (item.get("meta_single_page") or {}).get("original_image_url")
        if single:
            urls.append(str(single))
        for page in item.get("meta_pages") or []:
            original = (page.get("image_urls") or {}).get("original")
            if original:
                urls.append(str(original))
        limit = self._page_limit(getattr(self.mycfg, "max_manga_pages", 3))
        extra: dict[str, Any] = {
            "pixiv_id": pid,
            "author_name": str(user.get("name") or "未知作者"),
        }
        restricted = int(item.get("x_restrict") or item.get("xRestrict") or 0) > 0
        nsfw_mode = getattr(self.mycfg, "nsfw_mode", "ignore")
        # ``Downloader.download_img`` uses ``@auto_task`` and already returns
        # ``Task[Path]``; wrapping it in ``asyncio.create_task`` would reject
        # the task at type-check time (and needlessly double-schedule it).
        contents: list[MediaContent] = (
            []
            if restricted and nsfw_mode == "ignore"
            else self._create_pixiv_image_contents(urls[:limit], extra)
        )
        ugoira = item.get("_ugoira_meta")
        if isinstance(ugoira, dict) and getattr(self.mycfg, "nsfw_mode", "ignore") == "normal":
            zip_url = ugoira.get("zip_url") or ugoira.get("originalSrc") or ugoira.get("src")
            frames = ugoira.get("frames") or []
            if zip_url and frames:
                async def build_gif() -> Path:
                    try:
                        archive = await self.downloader.download_file(str(zip_url), headers=self.image_headers, proxy=self.proxy)
                    except SizeLimitException:
                        extra["warning"] = "图片超过资源大小限制已跳过下载，若需要请调整后台下载大小限制"
                        raise
                    async with self._ugoira_semaphore:
                        return await create_ugoira_gif(
                            archive, self.cfg.cache_dir / "pixiv_ugoira", frames, 5
                        )
                contents.append(FileContent(self._create_media_task(build_gif()), name="pixiv_ugoira.gif"))
        if restricted and nsfw_mode == "blur_cover_pdf":
            contents = self._create_blur_cover_pdf_contents(urls[:limit], extra)
            warning = None
        elif restricted and nsfw_mode == "ignore":
            contents = []
            warning = "R18作品因后台设置不予展示"
        else:
            warning = None
        return self.result(
            title=title,
            text=str(item.get("caption") or item.get("description") or "") or None,
            author=self.create_author(
                author_name,
                avatar_url=str(avatar_url) if avatar_url else None,
                headers=self.image_headers,
            ),
            timestamp=self._timestamp(item.get("create_date") or item.get("createDate")),
            url=(f"https://www.pixiv.net/novel/show.php?id={pid}" if is_novel else f"https://www.pixiv.net/artworks/{pid}"),
            contents=contents,
            extra={**extra, **({"warning": warning} if warning else {})},
        )

    def _create_pixiv_image_contents(
        self, image_urls: list[str], extra: dict[str, Any] | None = None
    ) -> list[MediaContent]:
        """创建带 Pixiv 大小限制提示的懒加载图片内容。"""
        contents: list[MediaContent] = []
        for url in image_urls:
            async def download(url: str = url) -> Path:
                try:
                    return await self.downloader.download_img(
                        url, headers=self.image_headers, proxy=self.proxy
                    )
                except SizeLimitException:
                    if extra is not None:
                        extra["warning"] = "图片超过资源大小限制已跳过下载，若需要请调整后台下载大小限制"
                    raise

            contents.append(ImageContent(self._create_media_task(download())))
        return contents

    @staticmethod
    def _targets(entry: str) -> tuple[list[str], list[str]]:
        parts = str(entry).split("-")
        return ([p[1:] for p in parts[1:] if p.startswith("g") and p[1:].isdigit()], [p[1:] for p in parts[1:] if p.startswith("u") and p[1:].isdigit()])

    async def _send_proactive(self, result, groups: list[str], users: list[str]) -> None:
        from ...render import Renderer
        from ...sender import MessageSender

        await MessageSender(self.cfg, Renderer(self.cfg)).send_proactive_msg(
            self.cfg.context, result, groups, users,
            getattr(self.mycfg, "platform_name", None) or ["default"],
            platform_botid=getattr(self.mycfg, "platform_botid", None),
            only_previewCard=bool(getattr(self.mycfg, "only_previewCard", False)),
        )

    def _create_media_task(self, coroutine) -> asyncio.Task[Any]:
        """创建并登记媒体任务，确保插件关闭时可以取消未完成任务。"""
        if not hasattr(self, "_media_tasks"):
            # 兼容未经过完整构造函数初始化的测试/嵌入场景。
            self._media_tasks = set()
        task = asyncio.create_task(coroutine)
        self._media_tasks.add(task)
        task.add_done_callback(self._media_tasks.discard)
        return task

    async def _send_ranking_with_retry(self, result, groups: list[str], users: list[str], key: str) -> None:
        for attempt in range(1, 4):
            try:
                await self._send_proactive(result, groups, users)
                self._state["ranking_sent"][key] = datetime.now().isoformat()
                await self._save_state()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt < 3:
                    await asyncio.sleep(120)
                    continue
                logger.warning("[pixiv] 榜单投递尝试 3 次后仍失败，已标记 sent：%s，原因：%s", key, exc)
                self._state["ranking_sent"][key] = datetime.now().isoformat()
                await self._save_state()

    @staticmethod
    def _app_image_urls(item: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        single = (item.get("meta_single_page") or {}).get("original_image_url")
        if single:
            urls.append(str(single))
        for page in item.get("meta_pages") or []:
            original = (page.get("image_urls") or {}).get("original")
            if original:
                urls.append(str(original))
        return urls

    async def _download_ranking_item(
        self,
        item: dict[str, Any],
        semaphore: asyncio.Semaphore,
    ) -> tuple[list[MediaContent], bool] | None:
        """按榜单作品粒度下载资源；失败作品由榜单聚合层跳过。"""
        restricted = int(item.get("x_restrict") or item.get("xRestrict") or 0) > 0
        if restricted and getattr(self.mycfg, "nsfw_mode", "ignore") == "ignore":
            return [], False
        urls = self._app_image_urls(item)
        if not urls:
            return [], False
        limit = self._page_limit(getattr(self.mycfg, "max_manga_pages", 3))
        async with semaphore:
            path: Path | None = None
            size_limited = False
            for url in urls[:limit]:
                for attempt in range(3):
                    try:
                        path = await self.downloader.download_img(
                            url, headers=self.image_headers, proxy=self.proxy
                        )
                        break
                    except asyncio.CancelledError:
                        raise
                    except SizeLimitException as exc:
                        logger.warning(
                            "[pixiv] 榜单资源超过 source_max_size，跳过当前图片并尝试下一张：%s，原因：%s",
                            url,
                            exc,
                        )
                        size_limited = True
                        break
                    except Exception as exc:
                        if attempt < 2:
                            logger.warning(
                                "[pixiv] 榜单资源下载失败，将在 60 秒后重试（第 %s/3 次）：%s，原因：%s",
                                attempt + 1,
                                url,
                                exc,
                            )
                            await asyncio.sleep(60)
                        else:
                            logger.warning("[pixiv] 榜单资源下载 3 次均失败，尝试该作品下一张图片：%s，原因：%s", url, exc)
                if path is not None:
                    break
            if path is None:
                logger.warning("[pixiv] 榜单作品所有图片均下载失败，跳过作品")
                return ([], size_limited) if size_limited else None
            if restricted and getattr(self.mycfg, "nsfw_mode", "ignore") == "blur_cover_pdf":
                return [
                    ImageContent(
                        await create_blurred_cover(
                            path,
                            self.cfg.cache_dir / "pixiv_nsfw",
                            self._blur_strength(getattr(self.mycfg, "nsfw_blur_strength", 70)),
                        )
                    )
                ], size_limited
            return [ImageContent(path)], size_limited

    async def _build_ranking_result(
        self,
        items: list[dict[str, Any]],
        mode: str,
        ranking_date: str | None,
        now: datetime,
    ):
        """下载榜单作品并构造单个榜单聚合结果。"""
        semaphore = asyncio.Semaphore(3)
        tasks = [
            asyncio.create_task(self._download_ranking_item(item, semaphore))
            for item in items
        ]
        downloaded = await asyncio.gather(*tasks, return_exceptions=True)
        contents: list[MediaContent] = []
        ranking_lines: list[str] = []
        for rank, (item, result) in enumerate(zip(items, downloaded), 1):
            title = str(item.get("title") or f"Pixiv 作品 {item.get('id', '')}")
            user = item.get("user") or {}
            username = str(user.get("name") or "未知作者")
            restricted = int(item.get("x_restrict") or item.get("xRestrict") or 0) > 0
            ignored = restricted and getattr(self.mycfg, "nsfw_mode", "ignore") == "ignore"
            if isinstance(result, BaseException):
                logger.warning("[pixiv] 榜单作品处理失败，跳过第 %s 名：%s", rank, result)
                if not ignored:
                    page_count = len(self._app_image_urls(item))
                    suffix = f" 共{page_count}张" if page_count > 1 else ""
                    ranking_lines.append(f"第{rank}名：{title} - {username}{suffix}")
                continue
            if result is None:
                if not ignored:
                    page_count = len(self._app_image_urls(item))
                    suffix = f" 共{page_count}张" if page_count > 1 else ""
                    ranking_lines.append(f"第{rank}名：{title} - {username}{suffix}")
                continue
            result_contents, size_limited = result
            if ignored:
                ranking_lines.append(f"第{rank}名：{title}")
            else:
                page_count = len(self._app_image_urls(item))
                suffix = f" 共{page_count}张" if page_count > 1 else ""
                limit_warning = " 图片资源大小超限制，有需要请调整后台配置" if size_limited else ""
                ranking_lines.append(f"第{rank}名：{title} - {username}{suffix}{limit_warning}")
            contents.extend(result_contents)
        title = f"{now:%m月%d日}{'R-18' if mode == 'day_r18' else ''}榜单"
        pixiv_logo = Path(__file__).resolve().parents[2] / "resources" / "logos" / "pixiv.png"
        return self.result(
            title=title,
            text="\n".join(ranking_lines) or None,
            author=Author(name="Pixiv榜单", avatar=pixiv_logo),
            timestamp=int(now.timestamp()),
            url=(
                "https://www.pixiv.net/ranking.php?mode=daily_r18"
                if mode == "day_r18"
                else "https://www.pixiv.net/ranking.php?mode=daily"
            ),
            contents=contents,
            extra={"ranking_date": ranking_date or str(now.date()), "ranking_mode": mode},
        )

    async def _subscription_loop(self) -> None:
        while True:
            try:
                if self.app_client:
                    for entry in getattr(self.mycfg, "sub_uids_users", None) or []:
                        parts = str(entry).split("-")
                        if not parts or not parts[0].isdigit():
                            continue
                        uid = int(parts[0]); groups, users = self._targets(str(entry)); items = []
                        for kind in ("illust", "manga"):
                            try:
                                items.extend(self._app_items(await self.app_client.user_illusts(uid, kind)))
                            except Exception as exc:
                                logger.warning("[pixiv] UID %s %s 列表请求失败: %s", uid, kind, exc)
                        try:
                            items.extend(self._app_items(await self.app_client.user_novels(uid)))
                        except Exception as exc:
                            logger.warning("[pixiv] UID %s 小说列表请求失败: %s", uid, exc)
                        items.sort(key=lambda x: str(x.get("create_date") or ""))
                        window = items[-10:]
                        for target in [*(f"group:{g}" for g in groups), *(f"user:{u}" for u in users)]:
                            prefix = f"{uid}:{target}:"
                            # 首次发现目标时建立最新窗口基线，不补发历史作品。
                            if not any(key.startswith(prefix) for key in self._state["works_sent"]):
                                for item in window:
                                    if item.get("id"):
                                        self._state["works_sent"][f"{prefix}{item.get('type', 'illust')}:{item['id']}"] = datetime.now().isoformat()
                                await self._save_state()
                                continue
                            for item in window:
                                if not item.get("id"):
                                    continue
                                key = f"{prefix}{item.get('type', 'illust')}:{item['id']}"
                                if key in self._state["works_sent"]:
                                    continue
                                if item.get("type") == "novel":
                                    try:
                                        detail = await self.app_client.novel_text(int(item["id"]))
                                        item["description"] = detail.get("novel_text") or detail.get("content") or item.get("description", "")
                                    except Exception as exc:
                                        logger.warning("[pixiv] 小说正文获取失败 %s: %s", item.get("id"), exc)
                                        continue
                                if int(item.get("type") or 0) == 2 or item.get("illust_type") == 2:
                                    try:
                                        item["_ugoira_meta"] = await self.app_client.ugoira_metadata(int(item["id"]))
                                    except Exception as exc:
                                        logger.warning("[pixiv] ugoira 元数据获取失败 %s: %s", item.get("id"), exc)
                                        continue
                                target_id = target.split(":", 1)[1]
                                await self._send_proactive(self._app_result(item), [target_id] if target.startswith("group:") else [], [target_id] if target.startswith("user:") else [])
                                self._state["works_sent"][key] = datetime.now().isoformat()
                                await self._save_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[pixiv] 作者订阅轮询失败: %s", exc)
            await asyncio.sleep(max(120, int(getattr(self.mycfg, "sub_interval", 10) or 10) * 60))

    async def _ranking_loop(self) -> None:
        await self._app_ready.wait()
        while True:
            try:
                now = datetime.now(self.cfg.timezone)
                current_time = now.strftime("%H:%M")
                configured_time = str(getattr(self.mycfg, "ranking_send_times", "18:00") or "").strip()
                targets = getattr(self.mycfg, "ranking_subscriptions", None) or []
                matched_targets = []
                for target in targets:
                    parts = str(target).split(":")
                    if len(parts) < 3:
                        continue
                    target_time = ":".join(parts[2:])
                    if (target_time == "default" and current_time == configured_time) or target_time == current_time:
                        matched_targets.append((target, parts))
                if matched_targets and self.app_client:
                    logger.info("[pixiv] 到达榜单发送时间 %s（当前时间 %s），开始获取榜单", current_time, current_time)
                    for mode, enabled in (("day", getattr(self.mycfg, "ranking_list", False)), ("day_r18", getattr(self.mycfg, "ranking_list_R18", False))):
                        if not enabled:
                            continue
                        pending_targets = []
                        for target, parts in matched_targets:
                            key = f"{now.date()}:{current_time}:{mode}:{target}"
                            if key not in self._state["ranking_sent"]:
                                pending_targets.append((target, parts, key))
                        if not pending_targets:
                            continue
                        try:
                            ranking_payload = await self.app_client.illust_ranking(mode)
                            items = self._app_items(ranking_payload)[:max(1, min(20, int(getattr(self.mycfg, "ranking_top_n", 5) or 5)))]
                            ranking_date = ranking_payload.get("date") if isinstance(ranking_payload, dict) else None
                            result = await self._build_ranking_result(items, mode, ranking_date, now)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning("[pixiv] 榜单 mode=%s 获取或聚合失败，本轮跳过：%s", mode, exc)
                            continue
                        for target, parts, key in pending_targets:
                            groups = [parts[1]] if parts[0] == "group" else []; users = [parts[1]] if parts[0] == "user" else []
                            await self._send_ranking_with_retry(result, groups, users, key)
                elif matched_targets and self.app_client is None:
                    logger.warning("[pixiv] 榜单时间 %s 已到，但 App API 未初始化，请检查 refresh_token", current_time)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[pixiv] 榜单发送失败: %s", exc)
            await asyncio.sleep(60)

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

    # ---------- 作品解析 ----------

    def _create_blur_cover_pdf_contents(
        self, image_urls: list[str], extra: dict[str, Any] | None = None
    ) -> list[MediaContent]:
        """为 R18 多页静态作品创建模糊封面及正文 PDF 下载任务。"""
        if not image_urls:
            return []
        output_dir = self.cfg.cache_dir / "pixiv_nsfw"

        async def blur_cover() -> Path:
            try:
                source = await self.downloader.download_img(
                    image_urls[0], headers=self.image_headers, proxy=self.proxy
                )
            except SizeLimitException:
                if extra is not None:
                    extra["warning"] = "图片超过资源大小限制已跳过下载，若需要请调整后台下载大小限制"
                raise
            return await create_blurred_cover(
                source,
                output_dir,
                self._blur_strength(getattr(self.mycfg, "nsfw_blur_strength", 70)),
            )

        contents: list[MediaContent] = [ImageContent(self._create_media_task(blur_cover()))]
        if len(image_urls) > 1:

            async def body_pdf() -> Path:
                try:
                    sources = await gather(
                        *[
                            self.downloader.download_img(
                                url, headers=self.image_headers, proxy=self.proxy
                            )
                            for url in image_urls[1:]
                        ]
                    )
                except SizeLimitException:
                    if extra is not None:
                        extra["warning"] = "图片超过资源大小限制已跳过下载，若需要请调整后台下载大小限制"
                    raise
                return await create_body_pdf(
                    sources, output_dir, self.cfg.source_max_size
                )

            contents.append(
                FileContent(
                    self._create_media_task(body_pdf()),
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

        result_extra: dict[str, Any] = {
            **extra,
            **({"info": stats} if (stats := self._stats_text(body)) else {}),
        }

        # list 在类型系统中不协变：将图片项扩展到 ParseResult 所需的媒体基类列表。
        # 不修改 BaseParser.create_image_contents 的返回约定，避免影响其他解析器。
        contents: list[MediaContent] = (
            self._create_blur_cover_pdf_contents(image_urls, result_extra)
            if nsfw and nsfw_mode == "blur_cover_pdf"
            else []
        )
        if not contents:
            contents.extend(self._create_pixiv_image_contents(image_urls, result_extra))

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
            extra=result_extra,
        )

    # ---------- 小说解析 ----------

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
        self,
        cover_url: Any,
        nsfw: bool,
        nsfw_mode: str,
        extra: dict[str, Any] | None = None,
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

            return [ImageContent(self._create_media_task(blur_cover()))]
        return self._create_pixiv_image_contents([url], extra)

    async def _fetch_novel_series_total(self, series_id: Any) -> int | None:
        """获取小说系列总话数；系列接口失败时降级为无系列总数。"""
        body = await self._fetch_novel_series_detail(series_id)
        if not body or body.get("total") is None:
            return None
        try:
            return int(str(body["total"]))
        except (TypeError, ValueError):
            logger.debug("Pixiv 小说系列总话数格式异常: %s", series_id)
            return None

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
        contents = await self._create_novel_cover_contents(
            body.get("coverUrl"), nsfw, nsfw_mode, extra
        )
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
        media_tasks = list(self._media_tasks)
        for task in media_tasks:
            task.cancel()
        if media_tasks:
            await asyncio.gather(*media_tasks, return_exceptions=True)
        self._media_tasks.clear()
        if self.app_client:
            await self.app_client.close()
        await super().close_session()
