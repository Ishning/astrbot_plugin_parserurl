import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

import msgspec
from aiohttp import ClientError

from astrbot.api import logger

from ...config import PluginConfig
from ...cookie import CookieJar
from ..base import (
    BaseParser,
    Downloader,
    ParseException,
    Platform,
    handle,
)
from .signing import ABogusSigner, generate_ms_token, generate_web_id

if TYPE_CHECKING:
    from ...data import ParseResult


@dataclass(slots=True)
class ProbedVideo:
    url: str
    size: int
    headers: dict[str, str]


class DouyinParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="douyin", display_name="抖音")
    PLAY_RATIOS: ClassVar[tuple[str, ...]] = ("1080p", "720p", "540p", "360p")
    TTWID_REGISTER_URL: ClassVar[str] = (
        "https://ttwid.bytedance.com/ttwid/union/register/"
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.douyin
        self.cookiejar = CookieJar(config, self.mycfg, domain="douyin.com")
        self._set_cookies()

        # 订阅配置。抖音用户页使用 sec_user_id（而不是数字 uid）作为稳定标识。
        self.sub_enable = getattr(self.mycfg, "sub_enable", None) or False
        self.sub_interval = getattr(self.mycfg, "sub_interval", None) or 3
        self.sub_delay = getattr(self.mycfg, "sub_delay", None) or 5
        self.platforms = getattr(self.mycfg, "platform_name", None) or ["default"]
        self.platform_botid = getattr(self.mycfg, "platform_botid", None) or []
        self.only_previewCard = getattr(self.mycfg, "only_previewCard", None) or False
        self.sub_uids_users = getattr(self.mycfg, "sub_uids_users", None) or []
        self.sub_map: dict[str, dict[str, list[str]]] = {}
        self._pending_subscription_targets: list[tuple[str, dict[str, list[str]]]] = []

        self.douyin_data_dir = config.data_dir / "douyin"
        self.douyin_data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.douyin_data_dir / "douyin_sub_cache.json"
        self.profile_cache_file = self.douyin_data_dir / "douyin_user_profile_cache.json"
        self._last_aweme_cache: dict[str, list[str]] = {}
        self._user_profile_cache: dict[str, dict[str, str]] = {}
        self._load_subscription_cache()
        self._load_user_profile_cache()
        self._load_subscription_targets()
        self._polling_task: asyncio.Task | None = None
        # 数字号解析请求的简单限流：最多两个并发，首批之后每次至少间隔 10 秒。
        self._numeric_resolve_semaphore = asyncio.Semaphore(2)
        self._numeric_resolve_gate = asyncio.Lock()
        self._numeric_resolve_burst = 2
        self._numeric_resolve_last_start = 0.0
        self._abogus_signer = ABogusSigner(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        self._config_resolve_task: asyncio.Task | None = None
        if self._pending_subscription_targets:
            try:
                self._config_resolve_task = asyncio.create_task(
                    self._resolve_pending_subscriptions(),
                    name="task_douyin_resolve_config_users",
                )
            except RuntimeError:
                logger.warning("[douyin_订阅] 当前没有运行中的事件循环，后台配置中的数字号将在下次加载时解析")

        # 热重载时避免留下旧轮询任务。
        try:
            tasks = asyncio.all_tasks()
        except RuntimeError:
            tasks = set()
        for task in tasks:
            if task.get_name() == "task_douyin_subscription_loop":
                task.cancel()
        self.ensure_subscription_task()

    async def _resolve_pending_subscriptions(self) -> None:
        """将后台配置中的抖音号解析为内部使用的 sec_user_id。"""
        pending = self._pending_subscription_targets
        self._pending_subscription_targets = []
        for unique_id, targets in pending:
            try:
                sec_user_id = await self.resolve_sec_user_id(unique_id)
                current = self.sub_map.setdefault(sec_user_id, {"groups": [], "users": []})
                current["groups"] = list(set(current["groups"] + targets["groups"]))
                current["users"] = list(set(current["users"] + targets["users"]))
            except (ValueError, ParseException) as exc:
                logger.warning(f"[douyin_订阅] 配置中的抖音号 {unique_id} 解析失败: {exc}")
        self.ensure_subscription_task()

    def _load_subscription_targets(self) -> None:
        """读取 ``unique_id-g群号-u用户号`` 格式的订阅配置，兼容旧 sec_user_id。"""
        for item in self.sub_uids_users:
            # sec_uid 自身可能含有连字符，只把后面的 -g123/-u456 当作目标分隔符。
            parts = [part.strip() for part in re.split(r"-(?=[gu]\d+(?:-|$))", str(item).strip(), flags=re.IGNORECASE)]
            sec_user_id = parts[0] if parts else ""
            if not sec_user_id or len(parts) < 2:
                logger.error(f"[douyin_订阅] 配置错误: '{item}'")
                continue

            groups, users = [], []
            for target in parts[1:]:
                if len(target) < 2 or target[0].lower() not in {"g", "u"} or not target[1:].isdigit():
                    logger.error(f"[douyin_订阅] 配置错误: '{item}' 中的目标 '{target}' 无效")
                    continue
                (groups if target[0].lower() == "g" else users).append(str(int(target[1:])))

            if groups or users:
                target_map = {"groups": groups, "users": users}
                resolved_sec = next(
                    (key for key, profile in self._user_profile_cache.items()
                     if profile.get("unique_id") == sec_user_id),
                    None,
                )
                if resolved_sec:
                    sec_user_id = resolved_sec
                elif not sec_user_id.startswith("MS4w"):
                    self._pending_subscription_targets.append((sec_user_id, target_map))
                    continue
                targets = self.sub_map.setdefault(sec_user_id, {"groups": [], "users": []})
                targets["groups"] = list(set(targets["groups"] + groups))
                targets["users"] = list(set(targets["users"] + users))

    def _load_subscription_cache(self) -> None:
        if not self.cache_file.exists():
            return
        try:
            with open(self.cache_file, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                self._last_aweme_cache = {
                    str(sec_user_id): [str(aweme_id) for aweme_id in aweme_ids]
                    for sec_user_id, aweme_ids in data.items()
                    if isinstance(aweme_ids, list)
                }
        except Exception as exc:
            logger.warning(f"[douyin_订阅] 加载状态缓存失败: {exc}")

    async def _save_subscription_cache(self) -> None:
        try:
            with open(self.cache_file, "w", encoding="utf-8") as file:
                json.dump(self._last_aweme_cache, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning(f"[douyin_订阅] 保存状态缓存失败: {exc}")

    def _load_user_profile_cache(self) -> None:
        if not self.profile_cache_file.exists():
            return
        try:
            with open(self.profile_cache_file, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                self._user_profile_cache = {
                    str(sec_user_id): {
                        key: str(value)
                        for key, value in profile.items()
                        if key in {"nickname", "unique_id"} and value
                    }
                    for sec_user_id, profile in data.items()
                    if isinstance(profile, dict)
                }
        except Exception as exc:
            logger.warning(f"[douyin_订阅] 加载用户资料缓存失败: {exc}")

    async def _save_user_profile_cache(self) -> None:
        try:
            with open(self.profile_cache_file, "w", encoding="utf-8") as file:
                json.dump(self._user_profile_cache, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning(f"[douyin_订阅] 保存用户资料缓存失败: {exc}")

    async def _update_user_profile(self, sec_user_id: str, profile: dict[str, Any]) -> None:
        nickname = profile.get("nickname")
        unique_id = profile.get("unique_id")
        if nickname or unique_id:
            cached = self._user_profile_cache.setdefault(sec_user_id, {})
            if nickname:
                cached["nickname"] = str(nickname)
            if unique_id:
                cached["unique_id"] = str(unique_id)
            await self._save_user_profile_cache()

    def ensure_subscription_task(self) -> None:
        """在有订阅目标时启动轮询；供管理命令在运行时新增订阅后调用。"""
        if not self.sub_enable or not self.sub_map:
            return
        if self._polling_task is None or self._polling_task.done():
            self._polling_task = asyncio.create_task(
                self._subscription_loop(), name="task_douyin_subscription_loop"
            )
            logger.info(f"[douyin_订阅] 已启动轮询，加载 {len(self.sub_map)} 个用户")

    async def close_session(self) -> None:
        """停止订阅轮询后关闭 HTTP 会话。"""
        if self._config_resolve_task and not self._config_resolve_task.done():
            self._config_resolve_task.cancel()
            await asyncio.gather(self._config_resolve_task, return_exceptions=True)
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            await asyncio.gather(self._polling_task, return_exceptions=True)
        await super().close_session()

    @staticmethod
    def normalize_sec_user_id(value: str) -> str:
        """从 sec_user_id 或用户主页 URL 提取订阅标识。"""
        value = value.strip()
        matched = re.search(r"(?:douyin\.com/user/|sec_user_id=)([^?/#\s]+)", value)
        sec_user_id = matched.group(1) if matched else value
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", sec_user_id):
            raise ValueError("请输入有效的抖音 sec_user_id 或用户主页链接")
        return sec_user_id

    async def resolve_sec_user_id(self, value: str) -> str:
        """将用户输入统一解析为 sec_user_id，支持抖音数字号。"""
        value = value.strip()
        if not value:
            raise ValueError("请输入抖音 sec_user_id、数字号或用户主页链接")
        if not value.isdigit():
            sec_user_id = self.normalize_sec_user_id(value)
            if sec_user_id.startswith("MS4w"):
                await self.refresh_user_profile(sec_user_id)
            return sec_user_id

        async with self._numeric_resolve_semaphore:
            await self._wait_numeric_resolve_slot()
            sec_user_id = await self._resolve_sec_user_id_by_numeric_id(value)
        if sec_user_id:
            return sec_user_id
        raise ValueError("未能通过该抖音数字号找到用户，请改用用户主页链接或 sec_user_id")

    async def _wait_numeric_resolve_slot(self) -> None:
        """控制数字号解析的启动频率，避免多个管理员命令触发风控。"""
        async with self._numeric_resolve_gate:
            if self._numeric_resolve_burst:
                self._numeric_resolve_burst -= 1
            else:
                wait_seconds = max(
                    0.0,
                    self._numeric_resolve_last_start + 10.0 - time.monotonic(),
                )
                if wait_seconds:
                    logger.info(f"[douyin_订阅] 数字号解析请求排队，等待 {wait_seconds:.1f} 秒")
                    await asyncio.sleep(wait_seconds)
            self._numeric_resolve_last_start = time.monotonic()

    async def _resolve_sec_user_id_by_numeric_id(self, numeric_id: str) -> str | None:
        """查询数字号对应的 sec_user_id，并以主页重定向作为兼容兜底。"""
        await self.ensure_ttwid()
        api_url = "https://www.iesdouyin.com/web/api/v2/user/info/"
        params = {"uid": numeric_id, "unique_id": numeric_id, "aid": "1128"}
        headers = self._sync_headers_for_url(api_url)
        headers["Referer"] = "https://www.iesdouyin.com/"

        profile = await self._request_user_profile(api_url, params, headers)
        if profile and profile.get("sec_user_id"):
            await self._update_user_profile(profile["sec_user_id"], profile)
            return profile["sec_user_id"]

        # 部分账号无法通过旧用户信息接口查询，尝试从主页最终地址或页面数据提取。
        profile_url = f"https://www.douyin.com/user/{numeric_id}"
        try:
            headers = self._sync_headers_for_url(profile_url)
            async with self.session.get(profile_url, headers=headers, allow_redirects=True) as response:
                sec_user_id = self._extract_sec_user_id(str(response.url))
                if sec_user_id:
                    return sec_user_id
                if response.status == 200:
                    return self._extract_sec_user_id(await response.text())
        except (ClientError, TimeoutError, ValueError) as exc:
            logger.info(f"[douyin_订阅] 数字号主页查询失败: {exc}")
        return None

    async def _request_user_profile(
        self, api_url: str, params: dict[str, str], headers: dict[str, str]
    ) -> dict[str, str] | None:
        try:
            async with self.session.get(api_url, params=params, headers=headers) as response:
                if response.status != 200:
                    return None
                return self._extract_user_profile(await response.json(content_type=None))
        except (ClientError, TimeoutError, ValueError) as exc:
            logger.debug(f"[douyin_订阅] 用户资料请求失败: {exc}")
            return None

    async def refresh_user_profile(self, sec_user_id: str) -> dict[str, str] | None:
        """按已知 sec_user_id 查询并缓存用户资料。"""
        await self.ensure_ttwid()
        api_url = "https://www.iesdouyin.com/web/api/v2/user/info/"
        params = {"sec_uid": sec_user_id, "aid": "1128"}
        headers = self._sync_headers_for_url(api_url)
        headers["Referer"] = f"https://www.douyin.com/user/{sec_user_id}"
        profile = await self._request_user_profile(api_url, params, headers)
        if profile:
            await self._update_user_profile(sec_user_id, profile)
        return profile

    @staticmethod
    def _extract_user_profile(data: Any) -> dict[str, str] | None:
        if not isinstance(data, dict) or not isinstance(data.get("user_info"), dict):
            return None
        user_info = data["user_info"]
        sec_user_id = user_info.get("sec_uid") or user_info.get("sec_user_id")
        if not isinstance(sec_user_id, str):
            return None
        profile = {"sec_user_id": sec_user_id}
        for key in ("nickname", "unique_id"):
            if user_info.get(key):
                profile[key] = str(user_info[key])
        return profile

    @staticmethod
    def _extract_sec_user_id(data: Any) -> str | None:
        """从接口响应、主页 URL 或页面文本中提取 sec_user_id。"""
        if isinstance(data, dict):
            profile = DouyinParser._extract_user_profile(data)
            return profile.get("sec_user_id") if profile else None

        matched = re.search(
            r"(?:douyin\.com/user/|[\"'](?:sec_uid|sec_user_id)[\"']\s*[:=]\s*[\"'])([A-Za-z0-9_.-]+)",
            str(data),
        )
        return matched.group(1) if matched else None

    async def _get_user_awemes(self, sec_user_id: str) -> list[dict[str, Any]]:
        """优先使用移动端作品接口，失败时从 SSR 用户页提取作品列表。"""
        await self.ensure_ttwid()
        try:
            aweme_list = await self._get_user_awemes_web(sec_user_id)
        except ParseException as exc:
            logger.info(f"[douyin_订阅] Web 作品列表请求失败，尝试 SSR 兜底: {exc}")
            aweme_list = await self._get_user_awemes_ssr(sec_user_id)

        for aweme in aweme_list:
            author = aweme.get("author", {})
            if isinstance(author, dict):
                await self._update_user_profile(sec_user_id, author)
                if author.get("nickname"):
                    break
        return aweme_list

    async def _get_user_awemes_web(self, sec_user_id: str) -> list[dict[str, Any]]:
        """按 DouYin_Spider 的参数格式请求 PC Web 作品列表接口。"""
        url = "https://www.douyin.com/aweme/v1/web/aweme/post/"
        cookies = self.cookiejar.get(domain="douyin.com")
        web_id = cookies.get("MONITOR_WEB_ID") or cookies.get("s_v_web_id")
        ms_token = cookies.get("msToken") or generate_ms_token()
        headers = self._sync_headers_for_url(url)
        headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Referer": f"https://www.douyin.com/user/{sec_user_id}",
        })
        params = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "sec_user_id": sec_user_id,
            "max_cursor": "0",
            "locate_query": "false",
            "show_live_replay_strategy": "1",
            "need_time_list": "1",
            "time_list_query": "0",
            "whale_cut_token": "",
            "cut_version": "1",
            "count": "18",
            "publish_video_strategy_type": "2",
            "update_version_code": "170400",
            "pc_client_type": "1",
            "version_code": "290100",
            "version_name": "29.1.0",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "131.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "131.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": "8",
            "device_memory": "8",
            "platform": "PC",
            "downlink": "10",
            "effective_type": "4g",
            "round_trip_time": "100",
            "msToken": ms_token,
        }
        web_id = web_id or generate_web_id()
        params["webid"] = web_id
        params["verifyFp"] = cookies.get("s_v_web_id", web_id)
        params["fp"] = cookies.get("s_v_web_id", web_id)
        try:
            params["a_bogus"] = self._abogus_signer.sign_params(params)
        except (ValueError, RuntimeError) as exc:
            raise ParseException(f"a_bogus 签名不可用: {exc}") from exc
        try:
            async with self.session.get(url, params=params, headers=headers) as response:
                if response.status != 200:
                    raise ParseException(f"状态码: {response.status}")
                data = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as exc:
            raise ParseException(f"Web 接口请求异常: {exc}") from exc

        if not isinstance(data, dict):
            raise ParseException("响应格式异常")
        aweme_list = data.get("aweme_list", [])
        if not isinstance(aweme_list, list):
            raise ParseException(data.get("status_msg") or "响应中没有 aweme_list")
        return [aweme for aweme in aweme_list if isinstance(aweme, dict)]

    async def _get_user_awemes_ssr(self, sec_user_id: str) -> list[dict[str, Any]]:
        """从用户主页 SSR 数据提取作品，避免依赖 PC Web 签名参数。"""
        url = f"https://www.douyin.com/user/{sec_user_id}"
        headers = self._sync_headers_for_url(url)
        async with self.session.get(url, headers=headers) as response:
            if response.status != 200:
                raise ParseException(f"SSR 用户页状态码: {response.status}")
            html = await response.text()

        matched = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", html, re.DOTALL)
        if not matched:
            raise ParseException("SSR 用户页未包含 _ROUTER_DATA")
        try:
            router_data = json.loads(matched.group(1).strip().rstrip(";"))
        except json.JSONDecodeError as exc:
            raise ParseException("SSR 用户页数据解析失败") from exc

        awemes: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        stack: list[Any] = [router_data]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                aweme_id = current.get("aweme_id")
                if aweme_id and str(aweme_id) not in seen_ids:
                    seen_ids.add(str(aweme_id))
                    awemes.append(current)
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
        if not awemes:
            raise ParseException("SSR 用户页未找到作品列表")
        return awemes

    async def _parse_subscription_aweme(self, aweme_id: str):
        """复用现有作品解析逻辑生成主动推送所需的 ParseResult。"""
        share_url = self._build_iesdouyin_url("video", aweme_id)
        try:
            result = await self.parse_video(share_url)
        except ParseException:
            # 图文作品在部分地区会以 slides 端点提供。
            result = await self.parse_slides(aweme_id)
        result.url = share_url
        return result

    async def _subscription_loop(self) -> None:
        """轮询已订阅用户；首次看到用户时仅建立基线，避免补发历史作品。"""
        await asyncio.sleep(5)
        while True:
            try:
                for sec_user_id, targets in list(self.sub_map.items()):
                    try:
                        awemes = await self._get_user_awemes(sec_user_id)
                        current_ids = [str(item["aweme_id"]) for item in awemes if item.get("aweme_id")]
                        if sec_user_id not in self._last_aweme_cache:
                            self._last_aweme_cache[sec_user_id] = current_ids[:50]
                            await self._save_subscription_cache()
                            logger.info(f"[douyin_订阅] 用户 {sec_user_id} 已建立作品基线")
                            await asyncio.sleep(float(self.sub_delay))
                            continue

                        known_ids = set(self._last_aweme_cache[sec_user_id])
                        new_awemes = [item for item in awemes if str(item.get("aweme_id", "")) not in known_ids]
                        # 接口按新到旧返回，反转后按发布时间顺序推送。
                        for aweme in reversed(new_awemes):
                            aweme_id = str(aweme.get("aweme_id", ""))
                            if not aweme_id:
                                continue
                            result = await self._parse_subscription_aweme(aweme_id)
                            from ...render import Renderer
                            from ...sender import MessageSender
                            sender = MessageSender(self.cfg, Renderer(self.cfg))
                            await sender.send_proactive_msg(
                                context=self.cfg.context,
                                result=result,
                                sub_groups=list(targets.get("groups", [])),
                                sub_users=list(targets.get("users", [])),
                                platforms=self.platforms,
                                dynamic_id=aweme_id,
                                platform_botid=self.platform_botid,
                                only_previewCard=self.only_previewCard,
                            )
                            known_ids.add(aweme_id)

                        self._last_aweme_cache[sec_user_id] = (current_ids + list(known_ids - set(current_ids)))[:50]
                        await self._save_subscription_cache()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(f"[douyin_订阅] 检查用户 {sec_user_id} 失败: {exc}")
                    await asyncio.sleep(float(self.sub_delay))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"[douyin_订阅] 订阅循环异常: {exc}")
            await asyncio.sleep(float(self.sub_interval) * 60)

    def _set_cookies(self, cookies_str: str = ""):
        """设置cookie到请求头"""
        cookies_str = cookies_str or self.cookiejar.cookies_str
        if cookies_str:
            self.ios_headers["Cookie"] = cookies_str
            self.android_headers["Cookie"] = cookies_str

    def _sync_headers_for_url(self, url: str) -> dict[str, str]:
        headers = self.ios_headers.copy()
        headers.pop("Cookie", None)
        if cookies_str := self.cookiejar.get_cookie_header_for_url(url):
            headers["Cookie"] = cookies_str
        elif self._is_iesdouyin_url(url):
            if cookies_str := self.cookiejar.get_cookie_header(domain="iesdouyin.com"):
                headers["Cookie"] = cookies_str
        return headers

    @staticmethod
    def _is_iesdouyin_url(url: str) -> bool:
        hostname = urlparse(url).hostname or ""
        return hostname == "iesdouyin.com" or hostname.endswith(".iesdouyin.com")

    def _has_ttwid(self) -> bool:
        cookies = self.cookiejar.get(domain="iesdouyin.com") or {}
        return bool(cookies.get("ttwid"))

    # https://v.douyin.com/_2ljF4AmKL8
    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+/?")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+/?")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        result = await self.parse_with_redirect(url)
        if result:
            result.url = url
        return result

    # https://www.douyin.com/video/7521023890996514083
    # https://www.douyin.com/note/7469411074119322899
    @handle("", r"(?<!\d)(?P<vid>\d{18,20})(?!\d)")
    @handle("aweme_id", r"aweme_id[=:/\s]+(?P<vid>\d{10,})")
    @handle("aweme", r"aweme/(?P<vid>\d{10,})")
    @handle("douyin", r"douyin\.com/(?P<ty>slides|video|note|live)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note|live)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note|live)/(?P<vid>\d+)")
    # https://jingxuan.douyin.com/m/video/7574300896016862490?app=yumme&utm_source=copy_link
    @handle(
        "jingxuan.douyin",
        r"jingxuan\.douyin.com/m/(?P<ty>slides|video|note|live)/(?P<vid>\d+)",
    )
    async def _parse_douyin(self, searched: re.Match[str]):
        ty = searched.groupdict().get("ty") or "video"
        vid = searched.group("vid")
        logger.info(f"[抖音] 解析类型: {ty}, ID: {vid}")
        if ty == "slides":
            return await self.parse_slides(vid)
        if ty == "live":
            return await self.parse_live(vid, original_url=searched.string)

        await self.ensure_ttwid()
        share_url = self._build_iesdouyin_url(ty, vid)
        logger.info(f"[抖音] 使用 canonical share 页解析: {share_url}")

        try:
            return await self.parse_video(share_url)
        except ParseException as e:
            logger.warning(f"[抖音] canonical share 页解析失败 {share_url}, 错误: {e}")
            raise ParseException("分享已删除或资源直链提取失败, 请稍后再试") from e

    #解析直播
    @handle("live.douyin", r"live\.douyin\.com/(?P<web_rid>[a-zA-Z0-9_]+)")
    async def _parse_douyin_web_live(self, searched: re.Match[str]):
        web_rid = searched.group("web_rid")
        logger.info(f"[抖音] 解析类型: live (网页), Web_RID: {web_rid}")

        # 将网页的 web_rid 转换为底层可用的 19位 room_id
        room_id = await self._get_room_id_from_web_rid(web_rid)
        if not room_id:
            logger.warning(f"[抖音] 无法将 web_rid: {web_rid} 转换为 room_id，尝试直接解析")
            room_id = web_rid

        return await self.parse_live(room_id, original_url=searched.string)

    @handle("webcast", r"webcast\.amemv\.com/(?:douyin/)?webcast/reflow/(?P<room_id>\d+)")
    async def _parse_douyin_reflow_live(self, searched: re.Match[str]):
        room_id = searched.group("room_id")
        logger.info(f"[抖音] 解析类型: live (底层跳转), room_id: {room_id}")
        return await self.parse_live(room_id, original_url=searched.string)

    @staticmethod
    def _build_iesdouyin_url(ty: str, vid: str) -> str:
        return f"https://www.iesdouyin.com/share/{ty}/{vid}/"

    @staticmethod
    def _build_m_douyin_url(ty: str, vid: str) -> str:
        return f"https://m.douyin.com/share/{ty}/{vid}/"

    async def ensure_ttwid(self) -> None:
        if self._has_ttwid():
            return

        logger.debug("[抖音] 当前缺少匿名 ttwid，尝试注册")
        headers = self.ios_headers.copy()
        headers.update(
            {
                "Content-Type": "application/json",
                "Referer": "https://www.iesdouyin.com/",
            }
        )
        payload = {
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.iesdouyin.com",
            "union": True,
            "fid": "",
        }
        try:
            async with self.session.post(
                self.TTWID_REGISTER_URL,
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"ttwid register status: {resp.status}")
                set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                self.cookiejar.update_from_response(set_cookie_headers)
                self._set_cookies()
                body = await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as e:
            raise ParseException("ttwid register failed") from e

        if not isinstance(body, dict):
            raise ParseException("ttwid register returned invalid body")

        if callback_url := body.get("redirect_url"):
            callback_headers = self._sync_headers_for_url(callback_url)
            callback_headers["Referer"] = "https://www.iesdouyin.com/"
            try:
                async with self.session.get(
                    callback_url,
                    headers=callback_headers,
                    allow_redirects=False,
                ) as resp:
                    if resp.status >= 400:
                        raise ParseException(f"ttwid callback status: {resp.status}")
                    set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                    self.cookiejar.update_from_response(set_cookie_headers)
                    self._set_cookies()
            except (ClientError, TimeoutError) as e:
                raise ParseException("ttwid callback failed") from e

        if not self._has_ttwid():
            raise ParseException("ttwid register returned no cookie")

    async def parse_with_redirect(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> "ParseResult":
        """先重定向再解析，并更新 cookies"""
        logger.debug(f"[抖音] 短链重定向请求: {url}")

        # 兼容基类的参数
        request_headers = headers or self.ios_headers

        async with self.session.get(
            url, headers=request_headers, allow_redirects=False, ssl=False
        ) as resp:
            logger.debug(f"[抖音] 短链重定向响应状态码: {resp.status}")
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookie_headers)
            self._set_cookies()

            # 只有在状态码是重定向状态码时才获取 Location
            redirect_url = url
            if resp.status in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("Location", url)
                logger.debug(f"[抖音] 重定向到: {redirect_url}")

        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")

        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    async def parse_video(self, url: str):
        await self.ensure_ttwid()
        share_headers = self._sync_headers_for_url(url)
        async with self.session.get(
            url, headers=share_headers, allow_redirects=False
        ) as resp:
            if resp.status != 200:
                raise ParseException(f"status: {resp.status}")
            text = await resp.text()
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookie_headers)
            self._set_cookies()

        pattern = re.compile(
            pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
            flags=re.DOTALL,
        )
        matched = pattern.search(text)

        if not matched or not matched.group(1):
            logger.debug("[抖音] 未在HTML中找到 window._ROUTER_DATA")
            raise ParseException("can't find _ROUTER_DATA in html")

        logger.debug("[抖音] 成功提取 window._ROUTER_DATA")

        from .video import RouterData

        video_data = msgspec.json.decode(
            matched.group(1).strip(), type=RouterData
        ).video_data
        logger.debug(
            f"[抖音] 解析成功 - 作者: {video_data.author.nickname}, 描述: {video_data.desc[:50]}..."
        )
        # 使用新的简洁构建方式
        contents = []

        # 添加图片内容
        if image_urls := video_data.image_urls:
            logger.debug(f"[抖音] 检测到图文内容，图片数量: {len(image_urls)}")
            contents.extend(
                self.create_image_contents(image_urls, headers=self.ios_headers)
            )

        # 添加视频内容
        elif video_data.video:
            cover_url = video_data.cover_url
            duration = video_data.video.duration if video_data.video else 0
            logger.debug(f"[抖音] 检测到视频内容，时长: {duration}秒")
            video_headers = self._build_media_headers(url)
            video_url = None
            if play_token := video_data.play_token:
                try:
                    probed = await self.probe_video_url(play_token, url)
                    video_url = probed.url
                    video_headers = probed.headers
                    logger.debug(
                        f"[抖音] play 端点探测成功，文件大小: {probed.size} 字节"
                    )
                except ParseException as e:
                    logger.warning(f"[抖音] play 端点探测失败，回退 play_addr: {e}")
            video_url = video_url or video_data.video_url
            if video_url:
                contents.append(
                    self.create_video_content(
                        video_url, cover_url, duration, headers=video_headers
                    )
                )

        # 构建作者
        author = self.create_author(
            video_data.author.nickname, video_data.avatar_url, headers=self.ios_headers
        )

        return self.result(
            title=video_data.desc,
            author=author,
            contents=contents,
            timestamp=video_data.create_time,
        )

    @staticmethod
    def _build_play_url(video_id: str, ratio: str) -> str:
        return (
            "https://aweme.snssdk.com/aweme/v1/play/"
            f"?video_id={video_id}&ratio={ratio}"
        )

    def _build_media_headers(self, referer: str) -> dict[str, str]:
        headers = self.ios_headers.copy()
        headers.pop("Cookie", None)
        headers["Referer"] = referer
        return headers

    async def probe_video_url(self, video_id: str, referer: str) -> ProbedVideo:
        probed_by_size: dict[int, ProbedVideo] = {}

        for ratio in self.PLAY_RATIOS:
            play_url = self._build_play_url(video_id, ratio)
            headers = self._build_media_headers(referer)
            headers["Range"] = "bytes=0-1"
            try:
                async with self.session.get(
                    play_url,
                    headers=headers,
                    allow_redirects=True,
                ) as resp:
                    if resp.status >= 400:
                        logger.debug(
                            f"[抖音] ratio={ratio} 探测失败，状态码: {resp.status}"
                        )
                        continue
                    size = self._extract_response_size(resp.headers)
                    if size <= 0:
                        logger.debug(f"[抖音] ratio={ratio} 未拿到有效文件大小")
                        continue
                    final_url = str(resp.url)
            except (ClientError, TimeoutError) as e:
                logger.debug(f"[抖音] ratio={ratio} 探测请求失败: {e}")
                continue

            probed_by_size.setdefault(
                size, ProbedVideo(final_url, size, self._build_media_headers(referer))
            )

        if not probed_by_size:
            raise ParseException("can't probe play endpoint")

        return max(probed_by_size.values(), key=lambda item: item.size)

    @staticmethod
    def _extract_response_size(headers) -> int:
        if content_range := headers.get("Content-Range"):
            if matched := re.search(r"/(\d+)\s*$", content_range):
                return int(matched.group(1))
        if content_length := headers.get("Content-Length"):
            try:
                return int(content_length)
            except ValueError:
                return 0
        return 0

    async def parse_slides(self, video_id: str):
        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {
            "aweme_ids": f"[{video_id}]",
            "request_source": "200",
        }
        logger.debug(f"[抖音] 请求参数: {params}")
        async with self.session.get(
            url, params=params, headers=self.android_headers
        ) as resp:
            logger.debug(f"[抖音] 幻灯片API响应状态码: {resp.status}")
            resp.raise_for_status()
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookie_headers)
            self._set_cookies()

            from .slides import SlidesInfo

            response_text = await resp.read()
            logger.debug(f"[抖音] 幻灯片API响应体大小: {len(response_text)} 字节")
            slides_data = msgspec.json.decode(
                response_text, type=SlidesInfo
            ).aweme_details[0]
        logger.debug(
            f"[抖音] 幻灯片解析成功 - 作者: {slides_data.name}, 描述: {slides_data.desc[:50]}..."
        )
        contents = []

        # 添加图片内容
        if image_urls := slides_data.image_urls:
            logger.debug(f"[抖音] 检测到幻灯片图片，数量: {len(image_urls)}")
            contents.extend(
                self.create_image_contents(image_urls, headers=self.android_headers)
            )

        # 添加动态内容
        if dynamic_urls := slides_data.dynamic_urls:
            logger.debug(f"[抖音] 检测到幻灯片动态效果，数量: {len(dynamic_urls)}")
            contents.extend(
                self.create_dynamic_contents(dynamic_urls, headers=self.android_headers)
            )

        # 构建作者
        author = self.create_author(
            slides_data.name, slides_data.avatar_url, headers=self.android_headers
        )

        return self.result(
            title=slides_data.desc,
            author=author,
            contents=contents,
            timestamp=slides_data.create_time,
        )

    async def _get_room_id_from_web_rid(self, web_rid: str) -> str | None:
        """将网页的 web_rid 转换为底层访问可用的19 位 room_id"""
        await self.ensure_ttwid()

        pc_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Cookie": self.cookiejar.cookies_str or "",
        }

        # 基于 PC 端解析 room_id
        try:
            page_url = f"https://live.douyin.com/{web_rid}"
            async with self.session.get(page_url, headers=pc_headers, ssl=False) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    match = re.search(r'\\?"room\\?"\s*:\s*\{[^\}]*?\\?"id_str\\?"\s*:\s*\\?"(\d{18,20})\\?"', html)
                    if match:
                        real_room_id = match.group(1)
                        logger.info(f"[抖音] PC 端解析：web_rid: {web_rid} -> room_id: {real_room_id} 成功")
                        return real_room_id

                    # 兜底正则：直接在 roomStore 暴力寻找 id_str
                    fallback_match = re.search(r'\\?"roomStore\\?".*?\\?"id_str\\?"\s*:\s*\\?"(\d{18,20})\\?"', html)
                    if fallback_match:
                        real_room_id = fallback_match.group(1)
                        logger.info(f"[抖音]  PC 端解析：web_rid: {web_rid} -> room_id: {real_room_id} 成功 (兜底正则)")
                        return real_room_id
        except Exception as e:
            logger.info(f"[抖音] PC 源码解析 room_id 失败: {e}")

        # 备用：移动端方式去访问
        mobile_headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Mobile/15E148 Safari/604.1",
        }
        try:
            page_url = f"https://live.douyin.com/{web_rid}"
            logger.info(f"[抖音] 尝试移动端 UA 重定向探测: {page_url}")
            async with self.session.get(page_url, headers=mobile_headers, allow_redirects=False, ssl=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    match = re.search(r'/reflow/(\d{18,20})', location)
                    if not match:
                        match = re.search(r'(?:room_id|roomId)=(\d{18,20})', location)
                    if match:
                        real_room_id = match.group(1)
                        logger.info(f"[抖音] 302 移动端方式重定向：web_rid: {web_rid} -> room_id: {real_room_id} 成功")
                        return real_room_id
        except Exception as e:
            logger.info(f"[抖音] 移动端重定向探测失败: {e}")

        logger.warning(f"[抖音] 所有 web_rid 获取方式均失败，主播可能未开播，或风控已升级")
        return None

    async def parse_live(self, room_id: str, original_url: str | None = None):
        """
            解析抖音直播间信息(需要 19位 room_id)
        """
        await self.ensure_ttwid()

        url = "https://webcast.amemv.com/webcast/room/reflow/info/"
        params = {
            "type_id": "0",
            "live_id": "1",
            "room_id": room_id,
            "app_id": "1128"
        }
        logger.info(f"[抖音] 请求底层直播间信息 API: Room ID={room_id}")

        try:
            async with self.session.get(
                url, params=params, headers=self.ios_headers, ssl=False
            ) as resp:
                if resp.status != 200:
                    raise ParseException(f"直播 API 状态码异常: {resp.status}")
                data = await resp.json()
        except Exception as e:
            raise ParseException(f"直播信息请求失败: {e}")

        room_info = data.get("data", {}).get("room", {})
        if not room_info:
            raise ParseException("未获取到直播间信息，可能已下播或受风控拦截")

        title = room_info.get("title", "抖音直播间")
        status = room_info.get("status")
        status_text = "正在直播" if status == 2 else "直播已结束"

        owner = room_info.get("owner", {})
        nickname = owner.get("nickname", "未知主播")

        avatar_list = owner.get("avatar_thumb", {}).get("url_list", [])
        avatar_url = avatar_list[0] if avatar_list else None

        cover_list = room_info.get("cover", {}).get("url_list", [])
        cover_url = cover_list[0] if cover_list else None

        contents = []
        if cover_url:
            contents.extend(self.create_image_contents([cover_url], headers=self.ios_headers))

        author = self.create_author(
            nickname, avatar_url, headers=self.ios_headers
        )

        desc = f"状态: {status_text}"

        if original_url and "live.douyin.com" in original_url:
            # 网页链接直接返回
            final_url = original_url
        else:
            #若没有原始链接，使用底层的 webcast 返回作为最终链接
            final_url = f"https://webcast.amemv.com/douyin/webcast/reflow/{room_id}"

        return self.result(
            title=title,
            text=desc,
            author=author,
            contents=contents,
            url=final_url
        )
