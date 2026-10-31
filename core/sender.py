from collections.abc import Awaitable, Callable
from itertools import chain
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from astrbot.api.all import MessageChain

import asyncio

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from astrbot.core.star.context import Context

class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 控制是否渲染卡片、是否强制合并转发
    - 将不同类型的内容转换为 AstrBot 消息组件并发送

    重要原则：
    - 不在此处做解析
    - 不在此处决定“内容是什么”
    - 只负责“怎么发”
    """

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    def _to_file_uri(self, path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        return path.as_uri()

    @staticmethod
    def _iter_contents(result: ParseResult):
        return chain(result.contents, result.repost.contents if result.repost else ())

    def _build_send_plan(
        self,
        result: ParseResult,
        contents: list | tuple | None = None,
        *,
        force_merge_override: bool | None = None,
        render_card_override: bool | None = None,
    ) -> dict:
        """
        根据解析结果生成发送计划（plan）

        plan 只做“策略决策”，不做任何 IO 或发送动作。
        后续发送流程严格按 plan 执行，避免逻辑分散。
        """
        light, heavy = [], []

        # 合并主内容 + 转发内容，统一参与发送策略计算
        iterable = contents if contents is not None else self._iter_contents(result)
        for cont in iterable:
            match cont:
                case ImageContent() | GraphicsContent() | TextContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)
        
        render_card = True 
        
        if render_card_override is not None:
            render_card = render_card_override

        # 预览卡片不计入转发阈值，仅按解析结果内容数量判断
        seg_count = len(light) + len(heavy)

        force_merge = seg_count >= self.cfg.forward_threshold
        if force_merge_override is not None:
            force_merge = force_merge_override

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            # 预览卡片：仅在“渲染卡片 + 不合并”时独立发送
            # "preview_card": render_card and not force_merge,
            "force_merge": force_merge,
        }

    async def _build_segments(
        self,
        result: ParseResult,
        plan: dict,
    ) -> list[BaseMessageComponent]:
        """
        根据发送计划构建消息段列表

        这里负责：
        - 下载媒体
        - 转换为 AstrBot 消息组件
        """
        segs: list[BaseMessageComponent] = []

        # 合并转发时，卡片以内联形式作为一个消息段参与合并
        # 取消原分开的，只要有渲染卡片就统一加入前面消息列表中
        if plan["render_card"]:
            if image_path := await self.renderer.render_card(result):
                segs.append(Image.fromFileSystem(str(image_path)))

        # 轻媒体处理
        for cont in plan["light"]:
            if isinstance(cont, TextContent):
                if cont.text:
                    segs.append(Plain(cont.text))
                continue

            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败\n"))
                continue

            match cont:
                case ImageContent():
                    segs.append(Image.fromFileSystem(str(path)))
                case GraphicsContent() as g:
                    segs.append(Image.fromFileSystem(str(path)))
                    # GraphicsContent 允许携带补充文本
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体处理
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except SizeLimitException:
                logger.warning(f"此媒体超过设置的大小限制，已跳过发送。若需要发送请在后台配置修改‘整资源最大大小’配置")
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败\n"))
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    segs.append(Video.fromFileSystem(str(path)))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=self._to_file_uri(path))
                        if self.cfg.audio_to_file
                        else Record.fromFileSystem(str(path))
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=self._to_file_uri(path)))

        return segs

    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
    ) -> list[BaseMessageComponent]:
        """
        根据策略决定是否将消息段合并为转发节点

        合并后的消息结构：
        - 每个原始消息段成为一个 Node
        - 统一使用机器人自身身份
        """
        if not force_merge or not segs:
            return segs

        nodes = Nodes([])
        self_id = event.get_self_id()

        for seg in segs:
            nodes.nodes.append(Node(uin=self_id, name="解析器", content=[seg]))

        return [nodes]

    @staticmethod
    def _build_text_fallback(result: ParseResult) -> list[BaseMessageComponent]:
        lines: list[str] = []
        if result.header:
            lines.append(result.header)
        if result.text:
            lines.append(result.text)
        if result.display_url:
            lines.append(result.display_url)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))

        text = "\n".join(line for line in lines if line).strip()
        return [Plain(text)] if text else []

    @staticmethod
    def _build_text_fallback_for_url(result: ParseResult) -> list[BaseMessageComponent]:
        lines: list[str]=[]
        if result.display_url:
            lines.append(result.display_url)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))

        text = "\n".join(line for line in lines if line).strip()
        return [Plain(text)] if text else []


    def _resolve_groups(self, result: ParseResult) -> list[SendGroup]:
        if result.send_groups:
            return result.send_groups
        return [SendGroup(contents=list(MessageSender._iter_contents(result)))]

    async def _send_group(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        group: SendGroup,
        text_segs: list[BaseMessageComponent] | None = None, #文本接收
        on_completion: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[bool, bool]:
        plan = self._build_send_plan(
            result,
            group.contents,
            force_merge_override=group.force_merge,
            render_card_override=group.render_card,
        )

        # 预览卡片和链接文本独立
        preview_segs = []
        if plan["render_card"]:
            if image_path := await self.renderer.render_card(result):
                preview_segs.append(Image.fromFileSystem(str(image_path)))
        
        if text_segs:
            preview_segs.extend(text_segs)

        sent = False
        completion_sent = False
        content_segs: list[BaseMessageComponent] = []

        # 预览卡片或文字兜底先行发送，完成表情不等待媒体下载
        if preview_segs:
            try:
                await event.send(event.chain_result(preview_segs))
                sent = True
                completion_sent = True
                if on_completion:
                    await on_completion()
            except Exception as e:
                seg_meta = self._collect_seg_meta(preview_segs)
                logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")

        try:
            # 生成正文，设置 plan 里的卡片标识防止重复生成
            plan["render_card"] = False
            content_segs = await self._build_segments(result, plan)
            
            # 策略
            if plan["force_merge"]:
                # 剩下都合并
                if content_segs:
                    merged_nodes = self._merge_segments_if_needed(event, content_segs, True)
                    await event.send(event.chain_result(merged_nodes))
                    sent = True
                    
                return sent, completion_sent

            # 4. 未触发以上的策略
            if not content_segs:
                return sent, completion_sent
                
            normal_segs = []
            heavy_segs = []
            for seg in content_segs:
                if isinstance(seg, (Video, File, Record)):
                    heavy_segs.append(seg)
                else:
                    normal_segs.append(seg)

            if normal_segs:
                await event.send(event.chain_result(normal_segs))
                sent = True
            
            if heavy_segs:
                for heavy_seg in heavy_segs:
                    await event.send(event.chain_result([heavy_seg]))
                    sent = True

            return sent, completion_sent

        except Exception as e:
            seg_meta = self._collect_seg_meta(preview_segs + content_segs)
            logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return sent, completion_sent

    @staticmethod
    def _collect_seg_meta(segs: list[BaseMessageComponent]) -> list[dict[str, str]]:
        """提取消息段元信息，用于失败日志定位。"""
        meta: list[dict[str, str]] = []

        for seg in segs:
            item = {"type": seg.__class__.__name__}
            for attr in ("file", "path", "url"):
                value = getattr(seg, attr, None)
                if value:
                    item["media"] = str(value)
                    break
            meta.append(item)

        return meta

    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        on_completion: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """
        发送解析结果的统一入口

        执行顺序固定：
        1. 构建发送计划
        2. 发送预览卡片（如有）
        3. 构建消息段
        4. 必要时合并转发
        5. 最终发送
        """
        groups = self._resolve_groups(result)

        platform_id = result.platform.name
        parser_cfg = getattr(self.cfg.parser, platform_id, None)
        show_detail = getattr(parser_cfg, "show_preview_detail", False) if parser_cfg else False

        if show_detail:
            segs = self._build_text_fallback(result)
        else:
            segs = self._build_text_fallback_for_url(result)

        sent = False
        completion_sent = False
        completion_notified = False

        async def notify_completion():
            nonlocal completion_notified
            if completion_notified:
                return
            completion_notified = True
            if on_completion:
                await on_completion()

        for i, group in enumerate(groups):
            # 防止如果有多个 group 时重复发送文本，这里仅在最后一个 group 附带文字
            current_text_segs = segs if i == len(groups) - 1 else None
            group_sent, group_completion_sent = await self._send_group(
                event, result, group, current_text_segs, notify_completion
            )
            sent = group_sent or sent
            completion_sent = group_completion_sent or completion_sent

        platform_name = result.platform.display_name

        if not sent:
            if not segs:
                logger.warning(f"[{platform_name}] 平台发送结果为空，不执行发送")
                return False
            try:
                await event.send(event.chain_result(segs))
                completion_sent = True
                await notify_completion()
            except Exception as e:
                seg_meta = self._collect_seg_meta(segs)
                logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
                return False

        return completion_sent

    async def send_proactive_msg(
        self,
        context: "Context",  # AstrBot 的Context
        result: ParseResult,
        sub_groups: list[str],
        sub_users: list[str],
        platforms: list[str],
        dynamic_id: str | None = None,
        platform_botid: list[str] | None = None,
        only_previewCard: bool = False,
    ):
        """
        主动消息发送入口

        """
        # import astrbot.api.message_components as Comp
        # from astrbot.api.all import MessageChain
        # import asyncio

        plan = self._build_send_plan(result, force_merge_override=False)
        preview_segs = []

        if plan["render_card"]:
            if image_path := await self.renderer.render_card(result):
                preview_segs.append(Image.fromFileSystem(str(image_path)))

        text_segs = self._build_text_fallback_for_url(result)
        if text_segs:
            preview_segs.extend(text_segs)

        if only_previewCard:
            content_segs = []
            logger.debug("已开启 only_previewCard，跳过媒体内容的解析与发送。")
        else:
            plan["render_card"] = False
            content_segs = await self._build_segments(result, plan)

        info_segs = list(preview_segs)
        if dynamic_id:
            dynamic_url = f"https://t.bilibili.com/{dynamic_id}"
            #添加 link判断，若是 result 里已有链接则跳过，没有再添加。getattr 获取 text 属性，若没有再转换 str判断
            already_has_link = False
            for seg in info_segs:
                seg_text = getattr(seg, "text", str(seg))
                if dynamic_id in seg_text:
                    already_has_link = True
                    break

            if not already_has_link:
                info_segs.append(Plain(f"链接：{dynamic_url}"))

        info_chain = MessageChain(info_segs) if info_segs else None

        if not info_chain and not content_segs:
            logger.warning("推送内容为空，不发送")
            return

        # chain = MessageChain(all_segs)

        #获取消息发送机器人
        # platform_name=getattr(self.cfg.parsers_template, "platform_name", ["default"])
        if not platforms:
            platforms = ["default"]

        for platform in platforms:
            platform = str(platform).strip()
            if not platform: continue

            if platform_botid and isinstance(platform_botid, list) and str(platform_botid[0]).strip():
                bot_self_id = str(platform_botid[0]).strip()
            else:
                bot_self_id = None
            bot_display_name = "B站动态推送"

            media_chain = None
            if content_segs:
                forward_threshold = getattr(self.cfg, "forward_threshold", 2)
                
                if len(content_segs) >= forward_threshold:
                    # 参考 _merge_segments_if_needed 方法创建一个空的 Nodes 容器然后添加相关富媒体
                    nodes_container = Nodes([])

                    for seg in content_segs:
                        nod = Node(
                            uin=bot_self_id,
                            name=bot_display_name,
                            content=[seg]
                        )
                        nodes_container.nodes.append(nod)

                    # single = Node(
                    #     uin=bot_self_id,
                    #     name=bot_display_name,
                    #     content=content_segs
                    # )
                    # nodes_container.nodes.append(single)

                    media_chain = MessageChain([nodes_container])
                else:
                    media_chain = MessageChain(content_segs)

            # 群
            for g in sub_groups:
                if not g: continue
                try:
                    session = f"{platform}:GroupMessage:{g}"

                    # await astrbotContext.send_message(context, session, chain)
                    # await context.send_message(session, chain)
                    if info_chain:
                        await context.send_message(session, info_chain)
                    if info_chain and media_chain:
                        await asyncio.sleep(0.5)

                    if media_chain:
                        await context.send_message(session, media_chain)
                    logger.info(f"[订阅发送成功] 通过机器人{platform} 发送到群: {g}")
                except Exception as e:
                    logger.error(f"[订阅发送失败] 通过机器人{platform} 发送群 {g} 发送失败: {e}")

            # 个人
            for u in sub_users:
                if not u: continue
                try:
                    session = f"{platform}:FriendMessage:{u}"

                    # await astrbotContext.send_message(context, session, chain)
                    # await context.send_message(session, chain)
                    if info_chain:
                        await context.send_message(session, info_chain)

                    if info_chain and media_chain:
                        await asyncio.sleep(0.5)

                    if media_chain:
                        await context.send_message(session, media_chain)
                    logger.info(f"[订阅发送成功] 通过机器人{platform} 发送到个人: {u}")
                except Exception as e:
                    logger.error(f"[订阅发送失败] 通过机器人{platform} 发送个人 {u} 发送失败: {e}")
