# main.py

import asyncio
import re
from datetime import datetime
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
from .core.parsers import BaseParser, BilibiliParser, PixivParser
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

    async def _pixiv_target(self, event: AstrMessageEvent) -> tuple[str, str]:
        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            target_id = str(raw.get("group_id", "")) if isinstance(raw, dict) else str(getattr(event.message_obj, "group_id", ""))
            return "group", target_id
        return "user", str(event.get_sender_id())

    async def _save_pixiv_config(self, parser: PixivParser) -> None:
        async with self._plugin_config_lock:
            values: list[str] = []
            for uid, targets in parser.sub_map.items():
                values.append("-".join([str(uid), *(f"g{x}" for x in targets.get("groups", [])), *(f"u{x}" for x in targets.get("users", []))]))
            ranking: list[str] = list(getattr(parser.mycfg, "ranking_subscriptions", None) or [])
            node: dict[str, Any] | None = next((item for item in self.cfg.parsers_template if item.get("__template_key") == "pixiv"), None)
            if node is None:
                node = {"__template_key": "pixiv"}; self.cfg.parsers_template.append(node)
            node["sub_uids_users"] = values
            node["ranking_subscriptions"] = ranking
            self.cfg.save_config()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("订阅pixiv用户")
    async def subscribe_pixiv_user(self, event: AstrMessageEvent, uid_str: str = ""):
        if not uid_str.isdigit():
            yield event.plain_result("Pixiv ID 不能为空且为数字")
            return
        parser: PixivParser | None = None
        old_sub_map: dict[int, dict[str, list[str]]] = {}
        old_state: dict[str, Any] = {}
        try:
            parser = self._get_parser_by_type(PixivParser)  # type: ignore
            assert parser is not None
            old_sub_map = {key: {kind: list(values) for kind, values in item.items()} for key, item in parser.sub_map.items()}
            old_state = dict(parser._state.get("works_sent", {}))
            if parser.app_client is None:
                yield event.plain_result("Pixiv App API 未启用或未配置 refresh_token")
                return
            target_type, target_id = await self._pixiv_target(event)
            uid = int(uid_str)
            payload = await parser.app_client.user_detail(uid)
            user = payload.get("user") if isinstance(payload, dict) else getattr(payload, "user", None)
            username = str((user or {}).get("name", uid) if isinstance(user, dict) else getattr(user, "name", uid))
            existing = parser.sub_map.get(uid)
            target_bucket = existing[f"{target_type}s"] if existing else []
            targets = target_bucket
            if target_id in targets:
                yield event.plain_result(f"已经订阅 Pixiv 用户：{username}，ID：{uid}")
                return
            kinds = ("illust", "manga")
            items = []
            for kind in kinds:
                items.extend(parser._app_items(await parser.app_client.user_illusts(uid, kind)))
            items.extend(parser._app_items(await parser.app_client.user_novels(uid)))
            # 所有远程请求成功后才提交内存状态，避免失败留下半条订阅。
            parser.sub_map.setdefault(uid, {"groups": [], "users": []})
            parser.sub_map[uid][f"{target_type}s"].append(target_id)
            prefix = f"{uid}:{target_type}:{target_id}:"
            for item in sorted(items, key=lambda x: str(x.get("create_date") or ""))[-10:]:
                if item.get("id"):
                    parser._state["works_sent"][f"{prefix}{item.get('type', 'illust')}:{item['id']}"] = datetime.now().isoformat()
            await parser._save_state()
            parser._state["user_names"][str(uid)] = {"name": username, "updated_at": datetime.now().isoformat()}
            await parser._save_state()
            await self._save_pixiv_config(parser)
            yield event.plain_result(f"已经订阅 Pixiv 用户：{username}，ID：{uid}，地址：https://www.pixiv.net/users/{uid}")
        except ValueError as exc:
            yield event.plain_result(f"Pixiv 用户查询失败：{exc}")
        except Exception as exc:
            if parser is not None:
                parser.sub_map = old_sub_map
                parser._state["works_sent"] = old_state
                await parser._save_state()
            logger.exception("[pixiv_订阅] 添加失败: %s", exc)
            yield event.plain_result("Pixiv 用户订阅失败，请稍后再试")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消订阅pixiv用户")
    async def unsubscribe_pixiv_user(self, event: AstrMessageEvent, uid_str: str = ""):
        if not uid_str.isdigit():
            yield event.plain_result("Pixiv ID 不能为空且为数字")
            return
        try:
            parser: PixivParser = self._get_parser_by_type(PixivParser)  # type: ignore
            target_type, target_id = await self._pixiv_target(event); uid = int(uid_str)
            targets = parser.sub_map.get(uid, {}).get(f"{target_type}s", [])
            if target_id not in targets:
                yield event.plain_result(f"当前会话未订阅 Pixiv 用户：{uid}")
                return
            targets.remove(target_id)
            if not parser.sub_map[uid]["groups"] and not parser.sub_map[uid]["users"]:
                parser.sub_map.pop(uid)
                parser._state.get("user_names", {}).pop(str(uid), None)
                for key in list(parser._state.get("works_sent", {})):
                    if key.startswith(f"{uid}:"):
                        parser._state["works_sent"].pop(key, None)
                await parser._save_state()
            await self._save_pixiv_config(parser)
            yield event.plain_result(f"已取消订阅 Pixiv 用户：ID：{uid}")
        except Exception:
            yield event.plain_result("取消 Pixiv 用户订阅失败，请稍后再试")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查询已订阅pixiv用户", alias={"查询已订阅pixix用户"})
    async def check_pixiv_users(self, event: AstrMessageEvent):
        try:
            parser: PixivParser = self._get_parser_by_type(PixivParser)  # type: ignore
            if not parser.sub_map:
                yield event.plain_result("当前没有任何 Pixiv 用户订阅记录")
                return
            lines = ["Pixiv 用户订阅列表"]
            for uid in parser.sub_map:
                cached = parser._state.get("user_names", {}).get(str(uid), {})
                name = cached.get("name") if isinstance(cached, dict) else None
                lines.append(f"用户名：{name or '未知'}，ID：{uid}，地址：https://www.pixiv.net/users/{uid}")
            yield event.plain_result("\n".join(lines))
        except ValueError:
            yield event.plain_result("Pixiv 相关功能未开启，请检查后台配置")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("订阅pixiv每日榜单")
    async def subscribe_pixiv_ranking(self, event: AstrMessageEvent, args: str = ""):
        tokens = str(args).split()
        if len(tokens) > 1 or (tokens and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", tokens[0])):
            yield event.plain_result("格式错误，请填写一个 HH:MM 时间")
            return
        parser: PixivParser = self._get_parser_by_type(PixivParser)  # type: ignore
        target_type, target_id = await self._pixiv_target(event)
        when = tokens[0] if tokens else "default"
        prefix = f"{target_type}:{target_id}:"
        records = list(getattr(parser.mycfg, "ranking_subscriptions", None) or [])
        if any(str(x).startswith(prefix) for x in records):
            yield event.plain_result("当前会话已经订阅 Pixiv 每日榜单")
            return
        records.append(f"{prefix}{when}")
        parser.mycfg.ranking_subscriptions = records
        await self._save_pixiv_config(parser)
        logger.info(
            "[pixiv] %s %s 订阅每日榜单，订阅时间：%s，系统当前时间：%s",
            target_type,
            target_id,
            when,
            datetime.now(self.cfg.timezone).isoformat(),
        )
        yield event.plain_result(f"已订阅 Pixiv 每日榜单，时间：{'跟随后台默认时间' if when == 'default' else when}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消订阅pixiv每日榜单")
    async def unsubscribe_pixiv_ranking(self, event: AstrMessageEvent, args: str = ""):
        tokens = str(args).split()
        target_type, target_id = await self._pixiv_target(event)
        parser: PixivParser = self._get_parser_by_type(PixivParser)  # type: ignore
        records = list(getattr(parser.mycfg, "ranking_subscriptions", None) or [])
        prefix = f"{target_type}:{target_id}:"; wanted = tokens[0] if tokens else None
        matched = [x for x in records if str(x).startswith(prefix) and (wanted is None or str(x).rsplit(":", 1)[-1] == wanted)]
        if not matched:
            yield event.plain_result("当前会话未订阅该 Pixiv 每日榜单时间")
            return
        parser.mycfg.ranking_subscriptions = [x for x in records if x not in matched]
        await self._save_pixiv_config(parser)
        yield event.plain_result("已取消 Pixiv 每日榜单订阅")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查询pixiv每日榜单订阅")
    async def check_pixiv_ranking(self, event: AstrMessageEvent):
        parser: PixivParser = self._get_parser_by_type(PixivParser)  # type: ignore
        records = getattr(parser.mycfg, "ranking_subscriptions", None) or []
        default_time = getattr(parser.mycfg, "ranking_send_times", "18:00")
        if not records:
            yield event.plain_result("当前没有 Pixiv 每日榜单订阅记录")
            return
        lines = ["Pixiv 每日榜单订阅列表"]
        for record in records:
            parts = str(record).split(":")
            when = ":".join(parts[2:])
            lines.append(f"{parts[0]} {parts[1]}：{'跟随默认时间 ' + str(default_time) if when == 'default' else when}")
        yield event.plain_result("\n".join(lines))
