# main.py

import asyncio
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Json
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import ArbiterContext, EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.config import PluginConfig
from .core.debounce import Debouncer
from .core.download import Downloader
from .core.parsers import BaseParser, BilibiliParser, DouyinParser
from .core.render import Renderer
from .core.sender import MessageSender
from .core.utils import extract_json_url


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context=context)
        # 渲染器
        self.renderer = Renderer(self.cfg)
        # 下载器
        self.downloader = Downloader(self.cfg)
        # 防抖器
        self.debouncer = Debouncer(self.cfg)
        # 仲裁器
        self.arbiter = EmojiLikeArbiter()
        # 消息发送器
        self.sender = MessageSender(self.cfg, self.renderer)
        # 缓存清理器
        self.cleaner = CacheCleaner(self.cfg)
        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}
        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []
        # config目录的下的配置json读写锁
        self._plugin_config_lock = asyncio.Lock()


    async def initialize(self):
        """加载、重载插件时触发"""
        # 加载渲染器资源
        await asyncio.to_thread(Renderer.load_resources)
        # 注册解析器
        self._register_parser()

    async def terminate(self):
        """插件卸载时触发"""
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话 (去重后的实例)
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        # 关缓存清理器
        await self.cleaner.stop()

    def _register_parser(self):
        """注册解析器（以 parser.enable 为唯一启用来源）"""
        # 所有 Parser 子类
        all_subclass = BaseParser.get_all_subclass()
        enabled_platforms = set(self.cfg.parser.enabled_platforms())

        enabled_classes: list[type[BaseParser]] = []
        enabled_names: list[str] = []
        for cls in all_subclass:
            platform_name = cls.platform.name

            if platform_name not in enabled_platforms:
                logger.info(f"[parser] 平台未启用或未配置: {platform_name}")
                continue

            enabled_classes.append(cls)
            enabled_names.append(platform_name)

            # 一个平台一个 parser 实例
            parser = cls(self.cfg, self.downloader)

            # 关键词 → parser
            for keyword, _ in cls._key_patterns:
                self.parser_map[keyword] = parser

        logger.info(f"启用平台: {'、'.join(enabled_names) if enabled_names else '无'}")

        # -------- 关键词-正则表（统一生成） --------
        patterns: list[tuple[str, re.Pattern[str]]] = []

        for cls in enabled_classes:
            for kw, pat in cls._key_patterns:
                patterns.append((kw, re.compile(pat) if isinstance(pat, str) else pat))

        # 长关键词优先，避免短词抢匹配
        patterns.sort(key=lambda x: -len(x[0]))

        self.key_pattern_list = patterns

        logger.debug(f"[parser] 关键词-正则对已生成: {[kw for kw, _ in patterns]}")

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        umo = event.unified_msg_origin
        logger.debug(f"DEBUG: 当前消息的 session 字符串是: {event.unified_msg_origin}")
        # 白名单
        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            return

        # 黑名单
        if self.cfg.blacklist and umo in self.cfg.blacklist:
            return

        # 消息链
        chain = event.get_messages()
        if not chain:
            return

        seg1 = chain[0]
        text = event.message_str

        # 卡片解析：解析Json组件，提取URL
        if isinstance(seg1, Json):
            text = extract_json_url(seg1.data)
            logger.debug(f"解析Json组件: {text}")

        if not text:
            return

        self_id = event.get_self_id()

        # 指定机制：专门@其他bot的消息不解析
        if isinstance(seg1, At) and str(seg1.qq) != self_id:
            return

        # 核心匹配逻辑 ：关键词 + 正则双重判定，汇集了所有解析器的正则对。
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            return
        logger.debug(f"匹配结果: {keyword}, {searched}")

        # 仲裁机制
        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                logger.warning(f"Unexpected raw_message type: {type(raw)}")
                return

            try:
                msg_id = int(raw["message_id"])
                msg_time = int(raw["time"])
                bot_self_id = int(raw["self_id"])
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"获取仲裁所需字段失败。错误信息: {e}, raw_message: {raw}")
                return

            is_win = await self.arbiter.compete(
                bot=event.bot,
                ctx=ArbiterContext(
                    message_id=msg_id,
                    msg_time=msg_time,
                    self_id=bot_self_id,
                ),
            )
            if not is_win:
                logger.debug("Bot在仲裁中输了, 跳过解析")
                return
            logger.debug("Bot在仲裁中胜出, 准备解析...")

        # 基于link防抖
        link = searched.group(0)
        if self.debouncer.hit_link(umo, link):
            logger.warning(f"[链接防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        # 解析
        parse_res = await self.parser_map[keyword].parse(keyword, searched)

        # 基于资源ID防抖
        resource_id = parse_res.get_resource_id()
        if self.debouncer.hit_resource(umo, resource_id):
            logger.warning(f"[资源防抖] 资源 {resource_id} 在防抖时间内，跳过发送")
            return

        # 发送
        await self.sender.send_parse_result(event, parse_res)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.remove_blacklist(umo)
        yield event.plain_result("当前会话的解析已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.add_blacklist(umo)
        yield event.plain_result("当前会话的解析已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站"""
        try:
            parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore
            qrcode = await parser.login.login_with_qrcode()
            yield event.chain_result([Image.fromBytes(qrcode)])
            async for msg in parser.login.check_qr_state():
                yield event.plain_result(msg)

        except ValueError as e:
            if "BilibiliParser" in str(e):
                yield event.plain_result("B站相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"错误: {e}")

        except Exception as e:
            logger.exception(f"[bili_登录] 扫码登录发生异常: {e}")
            yield event.plain_result(f"登录过程中发生错误，请稍后再试: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("订阅up")
    async def subscribe_bili_up(self, event: AstrMessageEvent, uid_str: str = ""):
        """添加B站up主动态订阅"""
        try:
            if not uid_str or not uid_str.isdigit():
                yield event.plain_result("UID 不能为空且为数字\n示例：订阅up 114514")
                return

            uid = int(uid_str)
            parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore

            is_group = False
            target_id = ""

            if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
                is_group = True
                raw = event.message_obj.raw_message

                if isinstance(raw, dict):
                    target_id = str(raw.get("group_id", ""))
                else:
                    target_id = str(getattr(event.message_obj, "group_id", ""))

                if not target_id:
                    yield event.plain_result("获取群号失败")
                    return
            else:
                target_id = str(event.get_sender_id())

            target_type = "groups" if is_group else "users"

            up_name = await parser.get_up_info(uid=uid)

            # 更新内存 sub_map
            if uid not in parser.sub_map:
                parser.sub_map[uid] = {"groups": [], "users": []}

            if target_id in parser.sub_map[uid][target_type]:
                yield event.plain_result(f"当前通过{'群' if is_group else '私聊'}订阅的UP主：{up_name}，uid：{uid} 已被订阅")
                return

            parser.sub_map[uid][target_type].append(target_id)

            await self.save_to_plugin_config()
            yield event.plain_result(f"成功订阅 UP主：{up_name}，uid：{uid}")

        except ValueError as e:
            if "BilibiliParser" in str(e):
                yield event.plain_result("B站相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"错误: {e}")

        except Exception as e:
            logger.exception(f"[bili_订阅] 添加订阅失败: {e}")
            yield event.plain_result(f"错误: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消订阅up")
    async def unsubscribe_bili_up(self, event: AstrMessageEvent, uid_str: str = ""):
        """取消B站UP主订阅动态推送"""
        try:
            if not uid_str or not uid_str.isdigit():
                yield event.plain_result("UID 不能为空且为数字\n示例：取消订阅up 114514")
                return

            uid = int(uid_str)
            parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)   # type: ignore

            is_group = False
            target_id = ""

            if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
                is_group = True
                raw = event.message_obj.raw_message
                if isinstance(raw, dict):
                    target_id = str(raw.get("group_id", ""))
                else:
                    target_id = str(getattr(event.message_obj, "group_id", ""))
            else:
                target_id = str(event.get_sender_id())

            target_type = "groups" if is_group else "users"

            up_name = await parser.get_up_info(uid=uid)

            #检测是否存在
            if uid not in parser.sub_map or target_id not in parser.sub_map[uid][target_type]:
                yield event.plain_result(f"当前{'群' if is_group else '私聊'}并没有订阅 UP主：{up_name}，uid：{uid}")
                return

            #仅移除针对会话发起的群或个人私聊
            parser.sub_map[uid][target_type].remove(target_id)

            if not parser.sub_map[uid]["groups"] and not parser.sub_map[uid]["users"]:
                parser.sub_map.pop(uid, None)
                logger.info(f"[bili_订阅] UID {uid} 无任何订阅者从内存移除")

            await self.save_to_plugin_config()
            yield event.plain_result(f"成功取消订阅 UP主：{up_name}，uid：{uid}")

        except ValueError as e:
            if "BilibiliParser" in str(e):
                yield event.plain_result("B站相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"错误: {e}")

        except Exception as e:
            logger.exception(f"[bili_订阅] 取消订阅失败: {e}")
            yield event.plain_result(f"错误: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查询订阅up列表")
    async def check_subscribe_bili_up(self, event: AstrMessageEvent):
        """查询订阅列表，仅基础信息"""
        try:
            parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)   # type: ignore

            if not parser.sub_map:
                yield event.plain_result("当前没有任何B站 up订阅记录")
                return

            msg_lines = ["B站up订阅列表"]
            for uid in parser.sub_map.keys():
                up_name = parser.uid_name_cache.get(uid, f"[查询量过快可能触发风控，稍后再查询]")
                msg_lines.append(f"up名：{up_name}，uid：{uid}，地址：https://space.bilibili.com/{uid}")

            yield event.plain_result("\n".join(msg_lines))

        except ValueError as e:
            if "BilibiliParser" in str(e):
                yield event.plain_result("B站相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"错误: {e}")

        except Exception as e:
            logger.exception(f"[bili_订阅] 查询订阅失败: {e}")
            yield event.plain_result(f"查询错误: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查询订阅up列表详细")
    async def check_subscribe_bili_up_all(self, event: AstrMessageEvent):
        """查询订阅列表，详细信息带上了发送至哪些人和群"""
        try:
            parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)   # type: ignore

            if not parser.sub_map:
                yield event.plain_result("当前没有任何B站 up订阅记录")
                return

            msg_lines = ["B站up订阅详细列表"]
            for uid, targets in parser.sub_map.items():
                groups = targets.get("groups", [])
                users = targets.get("users", [])

                up_name = parser.uid_name_cache.get(uid, f"[查询量过快可能触发风控，稍后再查询]")

                group_str = "、".join(groups) if groups else "无"
                user_str = "、".join(users) if users else "无"

                msg_lines.append(
                    f"up名：{up_name}，uid：{uid}，地址：https://space.bilibili.com/{uid}， 发送至 群：{group_str}，个人：{user_str}"
                )

            yield event.plain_result("\n".join(msg_lines))

        except ValueError as e:
            if "BilibiliParser" in str(e):
                yield event.plain_result("B站相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"错误: {e}")

        except Exception as e:
            logger.exception(f"[bili_订阅] 查询详细订阅失败: {e}")
            yield event.plain_result(f"查询错误: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("订阅抖音用户")
    async def subscribe_douyin_user(self, event: AstrMessageEvent, user_value: str = ""):
        """订阅抖音用户的最新作品推送。"""
        try:
            parser: DouyinParser = self._get_parser_by_type(DouyinParser)  # type: ignore
            sec_user_id = await parser.resolve_sec_user_id(user_value)
            profile = parser._user_profile_cache.get(sec_user_id, {})
            user_name = profile.get("nickname", "未知用户")
            target_type, target_id = self._get_subscription_target(event)

            targets = parser.sub_map.setdefault(sec_user_id, {"groups": [], "users": []})
            if target_id in targets[target_type]:
                yield event.plain_result(f"当前{'群' if target_type == 'groups' else '私聊'}已订阅抖音用户：{user_name}")
                return

            targets[target_type].append(target_id)
            await self.save_douyin_subscription_to_plugin_config()
            parser.ensure_subscription_task()
            yield event.plain_result(f"成功订阅抖音用户：{user_name}")
        except ValueError as exc:
            if "DouyinParser" in str(exc):
                yield event.plain_result("抖音相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"参数错误: {exc}\n示例：订阅抖音用户 123456789、MS4wLjABAAAA... 或用户主页链接")
        except Exception as exc:
            logger.exception(f"[douyin_订阅] 添加订阅失败: {exc}")
            yield event.plain_result(f"订阅失败: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消订阅抖音用户")
    async def unsubscribe_douyin_user(self, event: AstrMessageEvent, user_value: str = ""):
        """取消当前会话对抖音用户的订阅。"""
        try:
            parser: DouyinParser = self._get_parser_by_type(DouyinParser)  # type: ignore
            sec_user_id = await parser.resolve_sec_user_id(user_value)
            profile = parser._user_profile_cache.get(sec_user_id, {})
            user_name = profile.get("nickname", "未知用户")
            target_type, target_id = self._get_subscription_target(event)
            targets = parser.sub_map.get(sec_user_id)
            if not targets or target_id not in targets[target_type]:
                yield event.plain_result(f"当前{'群' if target_type == 'groups' else '私聊'}没有订阅抖音用户：{user_name}")
                return

            targets[target_type].remove(target_id)
            if not targets["groups"] and not targets["users"]:
                parser.sub_map.pop(sec_user_id)
                parser._last_aweme_cache.pop(sec_user_id, None)
                await parser._save_subscription_cache()
            await self.save_douyin_subscription_to_plugin_config()
            yield event.plain_result(f"已取消订阅抖音用户：{user_name}")
        except ValueError as exc:
            if "DouyinParser" in str(exc):
                yield event.plain_result("抖音相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"参数错误: {exc}")
        except Exception as exc:
            logger.exception(f"[douyin_订阅] 取消订阅失败: {exc}")
            yield event.plain_result(f"取消订阅失败: {exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查询已订阅抖音用户")
    async def check_subscribe_douyin_users(self, event: AstrMessageEvent):
        """查询全部抖音订阅用户及其投递目标。"""
        try:
            parser: DouyinParser = self._get_parser_by_type(DouyinParser)  # type: ignore
            if not parser.sub_map:
                yield event.plain_result("当前没有任何抖音用户订阅记录")
                return

            lines = ["抖音用户订阅列表"]
            for index, (sec_user_id, targets) in enumerate(parser.sub_map.items()):
                profile = parser._user_profile_cache.get(sec_user_id, {})
                if not profile.get("nickname") or not profile.get("unique_id"):
                    if index:
                        await asyncio.sleep(1)
                    await parser.refresh_user_profile(sec_user_id)
                    profile = parser._user_profile_cache.get(sec_user_id, {})
                name = profile.get("nickname", "未知用户")
                unique_id = profile.get("unique_id", "未知")
                groups = "、".join(targets.get("groups", [])) or "无"
                users = "、".join(targets.get("users", [])) or "无"
                lines.append(
                    f"用户：{name}\n抖音号：{unique_id}\nsec_user_id：{sec_user_id}\n"
                    f"发送至 群：{groups}，个人：{users}\n"
                    f"主页：https://www.douyin.com/user/{sec_user_id}"
                )
            yield event.plain_result("\n".join(lines))
        except ValueError as exc:
            if "DouyinParser" in str(exc):
                yield event.plain_result("抖音相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"查询错误: {exc}")
        except Exception as exc:
            logger.exception(f"[douyin_订阅] 查询订阅失败: {exc}")
            yield event.plain_result(f"查询错误: {exc}")

    @staticmethod
    def _get_subscription_target(event: AstrMessageEvent) -> tuple[str, str]:
        """返回当前命令应投递到的群或私聊目标。"""
        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            group_id = raw.get("group_id", "") if isinstance(raw, dict) else getattr(event.message_obj, "group_id", "")
            if not group_id:
                raise ValueError("获取群号失败")
            return "groups", str(group_id)
        return "users", str(event.get_sender_id())

    async def save_douyin_subscription_to_plugin_config(self):
        """将抖音订阅映射写回 parsers_template。"""
        async with self._plugin_config_lock:
            parser: DouyinParser = self._get_parser_by_type(DouyinParser)  # type: ignore
            formatted_list: list[str] = []
            for sec_user_id, targets in parser.sub_map.items():
                # 配置对用户展示和维护使用抖音号；sec_user_id 仅保留在内存及资料缓存中。
                display_id = parser._user_profile_cache.get(sec_user_id, {}).get("unique_id") or sec_user_id
                parts = [display_id]
                parts.extend(f"g{group_id}" for group_id in targets.get("groups", []))
                parts.extend(f"u{user_id}" for user_id in targets.get("users", []))
                formatted_list.append("-".join(parts))

            target_node: dict[str, Any] | None = next(
                (item for item in self.cfg.parsers_template if item.get("__template_key") == "douyin"),
                None,
            )
            if target_node is None:
                target_node = {"__template_key": "douyin"}
                self.cfg.parsers_template.append(target_node)
            target_node["sub_uids_users"] = formatted_list
            self.cfg.save_config()
            logger.info(f"[douyin_订阅] 配置写入成功，现有 {len(formatted_list)} 条订阅记录")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查询up直播状态")
    async def check_subscribe_bili_up_live_status(self, event: AstrMessageEvent):
        """查询已订阅UP主的直播状态"""
        import time

        try:
            parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)   # type: ignore

            if not parser.sub_map:
                yield event.plain_result("当前没有任何B站 up订阅记录")
                return

            from bilibili_api.utils.network import Api

            poll_uids = [int(str(uid).strip()) for uid in parser.sub_map.keys()]

            if not poll_uids:
                yield event.plain_result("当前没有任何B站 up订阅记录")
                return

            #两个独立的列表来分别存放状态，直播和未直播
            live_lines = []
            offline_lines = []
            live_count = 0
            offline_count = 0

            # 分批查询，避免一次查询过多导致接口报错
            batch_size = 10
            for i in range(0, len(poll_uids), batch_size):
                batch_uids = poll_uids[i:i + batch_size]
                live_params = {"uids[]": batch_uids}

                try:
                    LIVE_API_CONFIG = {
                        "url": "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids",
                        "method": "GET",
                        "verify": False,
                        "params": {"uids[]": "list<int>: up uid"},
                        "comment": "通过up uid列表获取直播间状态",
                    }
                    resp = await Api(**LIVE_API_CONFIG, no_csrf=True).update_params(**live_params).result

                    if isinstance(resp, dict):
                        for uid_str, room_info in resp.items():
                            uid = int(uid_str)
                            up_name = parser.uid_name_cache.get(uid, f"UID{uid}")
                            live_status = room_info.get("live_status", 0)
                            live_start_ts = room_info.get("live_time", 0)
                            room_id = room_info.get("room_id", "")
                            title = room_info.get("title", "无标题")
                            area_name = room_info.get("area_name", "未分区")
                            game_name = room_info.get("area_v2_name", "未知游戏")

                            if live_status == 1:
                                if live_start_ts > 0:
                                    duration_sec = int(time.time()) - live_start_ts
                                    hours = duration_sec // 3600
                                    minutes = (duration_sec % 3600) // 60
                                    seconds = duration_sec % 60
                                    time_str = f"{hours}小时{minutes}分{seconds}秒" if hours > 0 else f"{minutes}分钟{seconds}秒"
                                else:
                                    time_str = "未知"

                                room_url = f"https://live.bilibili.com/{room_id}"

                                live_lines.append(
                                    f"🔴 {up_name} (uid:{uid})\n"
                                    f"标题: {title}\n"
                                    f"分区: {area_name} | 游戏: {game_name}\n"
                                    f"当前已直播时长: {time_str}\n"
                                    f"链接: {room_url}"
                                )
                                live_count += 1
                            else:
                                offline_lines.append(f"- {up_name} (uid:{uid})")
                                offline_count += 1

                except Exception as e:
                    logger.warning(f"[bili_订阅] 查询直播状态批次 {i//batch_size + 1} 发生异常: {e}")
                    yield event.plain_result(f"查询部分UP直播状态失败: {e}")
                    return

                if i + batch_size < len(poll_uids):
                    await asyncio.sleep(0.5)

            final_msg = ["B站UP主直播状态查询", "=" * 20, "直播中:"]

            # 正在直播的列表
            if live_lines:
                final_msg.extend(live_lines)
            else:
                final_msg.append("当前暂无UP主在直播")

            # 未直播的列表
            if offline_lines:
                final_msg.append("")
                final_msg.append("未直播:")
                final_msg.extend(offline_lines)

            final_msg.append("=" * 20)
            final_msg.append(f"统计: 直播中 {live_count} 个 | 未播 {offline_count} 个")
            yield event.plain_result("\n".join(final_msg))

        except ValueError as e:
            if "BilibiliParser" in str(e):
                yield event.plain_result("B站相关功能未开启，请检查后台配置是否开启")
            else:
                yield event.plain_result(f"错误: {e}")

        except Exception as e:
            logger.exception(f"[bili_订阅] 查询直播状态失败: {e}")
            yield event.plain_result(f"查询错误: {e}")

    async def save_to_plugin_config(self):
        """将 sub_map 同步到框架的内存配置并调用框架原生保存方法"""
        # 增加一层 asyncio.Lock 异步锁保护
        # 调整为直接操作 self.cfg 的内存对象，然后通知 self.cfg.save_config() 保存安全的写进去
        async with self._plugin_config_lock:
            try:
                parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)   # type: ignore

                formatted_list = []
                for uid, targets in parser.sub_map.items():
                    parts = [str(uid)]
                    parts.extend([f"g{g_id}" for g_id in targets.get("groups", [])])
                    parts.extend([f"u{u_id}" for u_id in targets.get("users", [])])
                    formatted_list.append("-".join(parts))

                target_node = next((t for t in self.cfg.parsers_template if t.get("__template_key") == "bilibili"), None)

                if target_node:
                    target_node["sub_uids_users"] = formatted_list
                else:
                    self.cfg.parsers_template.append({
                        "__template_key": "bilibili",
                        "sub_uids_users": formatted_list
                    })

                self.cfg.save_config()

                logger.info(f"[bili_订阅] 配置写入成功，先有 {len(formatted_list)} 条订阅记录。")

            except ValueError as e:
                if "BilibiliParser" in str(e):
                    logger.error("B站相关功能未开启，请检查后台配置是否开启")
                else:
                    logger.error(f"错误: {e}")

            except Exception as e:
                logger.error(f"[bili_订阅] 写入该插件的配置文件失败: {e}")
                raise e
