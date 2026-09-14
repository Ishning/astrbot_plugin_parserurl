import asyncio
import json
from re import Match
from typing import ClassVar

from bilibili_api import request_settings, select_client
from bilibili_api.opus import Opus
from bilibili_api.video import Video, VideoCodecs, VideoQuality
from msgspec import convert

from astrbot.api import logger

from ...config import PluginConfig
from ...data import ImageContent, MediaContent, Platform
from ...exception import DownloadException, DurationLimitException
from ..base import (
    BaseParser,
    Downloader,
    ParseException,
    handle,
)
from .login import BilibiliLogin

from .dynamic import DynamicInfo

# 选择客户端
select_client("curl_cffi")
# 模拟浏览器，第二参数数值参考 curl_cffi 文档
# https://curl-cffi.readthedocs.io/en/latest/impersonate.html
request_settings.set("impersonate", "chrome131")


class BilibiliParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="bilibili", display_name="B站")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.bilibili
        self.headers.update(
            {
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
            }
        )

        self.video_quality = getattr(
            VideoQuality, str(self.mycfg.video_quality).upper(), VideoQuality._720P
        )
        # 修改为读取配置列表，并且做一些兼容性处理
        raw_codecs = getattr(self.mycfg, "video_codecs_list", ["AVC", "AV1", "HEV"])
        if not raw_codecs:
            raw_codecs = ["AVC", "AV1", "HEV"]
        elif isinstance(raw_codecs, str):
            raw_codecs = [c.strip() for c in raw_codecs.split(",")]

        self.codec_priority_list = [str(c).upper() for c in raw_codecs]

        self.login = BilibiliLogin(config)

        #订阅处理
        self.sub_enable = getattr(self.mycfg, "sub_enable", False)
        if self.sub_enable is None:
            self.sub_enable = False

        self.sub_interval = getattr(self.mycfg, "sub_interval", None) or 3
        self.sub_delay = getattr(self.mycfg, "sub_delay", None) or 5
        self.platforms = getattr(self.mycfg, "platform_name", ["default"]) or ["default"]
        self.platform_botid = getattr(self.mycfg, "platform_botid", None) or []
        self.only_previewCard = getattr(self.mycfg, "only_previewCard", None) or False
        self.ignore_lottery = getattr(self.mycfg, "ignore_lottery", None) or False
        self.ignore_lottery_content = getattr(self.mycfg, "ignore_lottery_content", None) or ["恭喜", "中奖", "私信", "奖品", "抽奖"]

        self.uid_name_cache = {}

        #获取订阅配置
        self.sub_uids_users = getattr(self.mycfg, "sub_uids_users", None) or []
        self.sub_map = {}
        for item in self.sub_uids_users:
            item_str = str(item).strip()
            if not item_str: continue

            parts = item_str.split('-')
            uid_str = parts[0]

            try:
                uid = int(uid_str)
            except ValueError:
                logger.error(f" [bili订阅] 配置错误: '{item_str}' 的 UID 部分不是有效的数字")
                continue

            if len(parts) == 1:
                logger.error(f" [bili订阅] 配置错误: UID {uid} 下该 {parts} 未指定任何群(g)或个人(u)")
                continue

            target_groups = []
            target_users = []
            has_error = False

            for target in parts[1:]:
                target = target.strip()
                if not target: continue
                
                prefix = target[0].lower()
                id_part = target[1:]
                
                if prefix == 'g':
                    try:
                        target_groups.append(str(int(id_part))) #验证是否为数字以防填写错误，然后再转回来
                    except ValueError:
                        logger.error(f" [bili订阅] UID {uid} 的群号格式错误: '{target}'")
                        has_error = True
                elif prefix == 'u':
                    try:
                        target_users.append(str(int(id_part)))
                    except ValueError:
                        logger.error(f" [bili订阅] UID {uid} 的个人号格式错误: '{target}'")
                        has_error = True
                else:
                    logger.error(f" [bili订阅] 配置非法: UID {uid} 中的 '{target}' 未以群(g)或个人(u)开头")
                    has_error = True

            if not has_error and (target_groups or target_users):
                if uid not in self.sub_map:
                    self.sub_map[uid] = {
                        "groups": [],
                        "users": []
                    }
                # 追加同时去重，乙方出现填写重复
                self.sub_map[uid]["groups"].extend(target_groups)
                self.sub_map[uid]["groups"] = list(set(self.sub_map[uid]["groups"]))

                self.sub_map[uid]["users"].extend(target_users)
                self.sub_map[uid]["users"] = list(set(self.sub_map[uid]["users"]))

        #初始化状态缓存
        self._last_dynamic_cache = {}

        #用于存放订阅一些信息
        self.bili_data_dir = config.data_dir / "bilibili"
        self.bili_data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.bili_data_dir / "bilibili_sub_cache.json"
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self._last_dynamic_cache = json.load(f)
                logger.info(f"[bili_订阅] 已加载状态缓存，目前记录数为: {len(self._last_dynamic_cache)}")
            except Exception as e:
                logger.error(f"[bili_订阅] 加载状态缓存失败: {e}")

        #用于处理直播的状态换成
        self._first_live_check_done = set()
        self.live_cache_file = self.bili_data_dir / "bili_live_cache.json"
        self._live_status_cache = {}
        if self.live_cache_file.exists():
            try:
                with open(self.live_cache_file, "r", encoding="utf-8") as f:
                    self._live_status_cache = json.load(f)
            except Exception as e:
                logger.error(f"[bili_订阅] 加载直播缓存失败: {e}")

        #解决热重载导致的僵尸任务
        f_old_task=False
        for task in asyncio.all_tasks():
            if task.get_name()=="task_bili_subscription_loop":
                task.cancel()
                f_old_task = True
        if f_old_task:
            logger.info("[bili_订阅] 热重载，已清理旧 task_bili_subscription_loop 任务")

        #如果开启了订阅且有配置 UID，启动后台轮询任务
        if self.sub_enable and self.sub_map:
            logger.info(
                f"启动 B 站动态订阅，加载 {len(self.sub_map)}个"
            )
            self._polling_task = asyncio.create_task(
                self._subscription_loop(),
                name="task_bili_subscription_loop")

        #任务 _warm_up_cache_loop
        f_old_warmup = False
        for task in asyncio.all_tasks():
            if task.get_name() == "task_bili_warm_up_cache_loop":
                task.cancel()
                f_old_warmup = True
        if f_old_warmup:
            logger.info("[bili_订阅] 热重载，已清理旧 task_bili_warm_up_cache_loop 任务")

        if self.sub_enable:
            self._warmup_task = asyncio.create_task(
                self._warm_up_cache_loop(),
                name="task_bili_warm_up_cache_loop"
            )

        #任务 _subscription_loop_live
        f_old_live = False
        for task in asyncio.all_tasks():
            if task.get_name() == "task_subscription_loop_live":
                task.cancel()
                f_old_live = True
        if f_old_live:
            logger.info("[bili_订阅] 热重载，已清理旧 task_subscription_loop_live 任务")

        if self.sub_enable:
            self._warmup_task = asyncio.create_task(
                self._subscription_loop_live(),
                name="task_subscription_loop_live"
            )

    @handle("b23.tv", r"b23\.tv/[A-Za-z\d\._?%&+\-=/#]+")
    @handle("bili2233", r"bili2233\.cn/[A-Za-z\d\._?%&+\-=/#]+")
    async def _parse_short_link(self, searched: Match[str]):
        """解析短链"""
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("BV", r"^(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle(
        "/BV",
        r"bilibili\.com(?:/video)?/(?P<bvid>BV[0-9a-zA-Z]{10})(?:\?p=(?P<page_num>\d{1,3}))?",
    )
    async def _parse_bv(self, searched: Match[str]):
        """解析视频信息"""
        bvid = str(searched.group("bvid"))
        page_num = int(searched.group("page_num") or 1)

        return await self.parse_video(bvid=bvid, page_num=page_num)

    @handle("bm", r"^bm(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s(?P<page_num>\d{1,3}))?$")
    async def _parse_bv_bm(self, searched: Match[str]):
        bvid = searched.group("bvid")
        page = int(searched.group("page_num") or 1)
        _, a_url = await self.extract_download_urls(bvid=bvid, page_index=page - 1)
        if not a_url:
            raise ParseException("未找到音频链接")
        audio = self.create_audio_content(a_url)
        return self.result(
            title=f"BiliBili_audio_{bvid}",
            contents=[audio],
            url=a_url,
        )

    @handle("av", r"^av(?P<avid>\d{6,})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle(
        "/av",
        r"bilibili\.com(?:/video)?/av(?P<avid>\d{6,})(?:\?p=(?P<page_num>\d{1,3}))?",
    )
    async def _parse_av(self, searched: Match[str]):
        """解析视频信息"""
        avid = int(searched.group("avid"))
        page_num = int(searched.group("page_num") or 1)

        return await self.parse_video(avid=avid, page_num=page_num)

    @handle("/dynamic/", r"bilibili\.com/dynamic/(?P<dynamic_id>\d+)")
    @handle("t.bili", r"t\.bilibili\.com/(?P<dynamic_id>\d+)")
    async def _parse_dynamic(self, searched: Match[str]):
        """解析动态信息"""
        dynamic_id = int(searched.group("dynamic_id"))
        return await self.parse_dynamic(dynamic_id)

    @handle("live.bili", r"live\.bilibili\.com/(?P<room_id>\d+)")
    async def _parse_live(self, searched: Match[str]):
        """解析直播信息"""
        room_id = int(searched.group("room_id"))
        return await self.parse_live(room_id)

    @handle("/favlist", r"favlist\?fid=(?P<fav_id>\d+)")
    async def _parse_favlist(self, searched: Match[str]):
        """解析收藏夹信息"""
        fav_id = int(searched.group("fav_id"))
        return await self.parse_favlist(fav_id)

    @handle("/read/", r"bilibili\.com/read/cv(?P<read_id>\d+)")
    async def _parse_read(self, searched: Match[str]):
        """解析专栏信息"""
        # read_id = int(searched.group("read_id"))
        # return await self.parse_read_with_opus(read_id)
        # 调整为走 _parse_opus_obj
        from bilibili_api.article import Article

        read_id = int(searched.group("read_id"))
        article = Article(read_id)
        opus = await article.turn_to_opus()
        return await self._parse_opus_obj(opus)

    @handle("/opus/", r"bilibili\.com/opus/(?P<opus_id>\d+)")
    async def _parse_opus(self, searched: Match[str]):
        """解析图文动态信息"""
        opus_id = int(searched.group("opus_id"))
        return await self.parse_opus(opus_id)

    @handle("find_uid_str_roomid", r"^(?:/find_uid_str_roomid|find_uid_str_roomid)\s+(?P<uids>\d+)$")
    async def _find_uid_str_roomid(self, searched: Match[str]):
        from .live import RoomData
        from bilibili_api.utils.network import Api
        from typing import Dict

        uids = int(searched.group("uids"))

        API_CONFIG = {
        "url": "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids",
        "method": "GET",
        "verify": False,
        "params": {"uids[]": "list<int>: uid"},
        "comment": "查直播，批量",
        }
        params: Dict[str, list[int]] = {"uids[]": [uids]}
        resp = await Api(**API_CONFIG, no_csrf=True).update_params(**params).result
        if not isinstance(resp, dict) or not resp:
            return self.result()
        live_room = next(iter(resp.values()))

        API_CONFIG_1 = {
        "url" : "https://api.live.bilibili.com/room/v2/Room/room_id_by_uid",
        "method": "GET",
        "verify": False,
        "params": {"uid[]": "list<int>: uid"},
        "comment": "通过uid 查 room id",
        }
        params_1 : Dict[str, int]={"uid": 67141}
        resp_1 = await Api(**API_CONFIG_1, no_csrf=True).update_params(**params_1).result
        if not isinstance(resp_1, dict) or not resp_1:
            return self.result()
        live_room1 = next(iter(resp_1.values()))

        # room_info =  LiveRoom(room_display_id=live_room, credential=await self.login.credential)
        # request_info = await room_info.get_room_info()
        # print(request_info)

        return self.result(text=str(live_room)+"\n"+str(live_room1))

    @handle("parserurl_test_push", r"^(?:/parserurl_test_push|parserurl_test_push)\s+(?P<dyid>\d+)$")
    async def _test_push_manual(self, searched: Match[str]):
        """手动触发订阅推送测试 (用于测试)"""
        dynamic_id = int(searched.group("dyid"))
        
        from ...render import Renderer
        from ...sender import MessageSender
        from bilibili_api.opus import Opus
        import traceback

        try:
            # opus_obj = Opus(int(dynamic_id), await self.login.credential)

            test_groups = set()
            test_users = set()

            for targets in self.sub_map.values():
                test_groups.update(targets["groups"])
                test_users.update(targets["users"])

            if not test_groups and not test_users:
                return self.result(
                    title="测试中断",
                    text="未在配置中检测到任何有效的推送目标（群或个人）。请先在设置中配置 sub_uids_users。",
                    contents=[]
                )

            parsed_result = await self.parse_dynamic(dynamic_id)

            if parsed_result:
                renderer = Renderer(self.cfg)
                sender = MessageSender(self.cfg, renderer)
                
                await sender.send_proactive_msg(
                    context=self.cfg.context,
                    result=parsed_result,
                    sub_groups=list(test_groups),
                    sub_users=list(test_users),
                    platforms=self.platforms,
                    dynamic_id=str(dynamic_id),
                    platform_botid=self.platform_botid,
                    only_previewCard=self.only_previewCard,
                )

                return self.result(
                    title="主动推送测试已触发",
                    text=f"动态 {dynamic_id} 解析成功，已调用推送接口发送至目标群/人，请检查是否收到。\n（若没收到请检查日志，以及相关配置是否配置好）",
                    contents=[]
                )
            else:
                return self.result(
                    title="测试失败",
                    text="动态解析为空，请检查该动态是否存在",
                    contents=[]
                )

        except Exception as e:
            logger.error(f"手动推送测试发生异常: {traceback.format_exc()}")
            return self.result(
                title="测试执行异常",
                text=str(e),
                contents=[]
            )

    async def _warm_up_cache_loop(self):
        """用于每30min执行一次 warm_up_cache 函数来检查是否有新uid进来，有的话进行处理"""

        if self.sub_map:
            logger.info(f"[bili_订阅] 当前已加载 {len(self.sub_map)} 个 up uid，开始预加载 up 信息")
        else:
            logger.info("[bili_订阅] 当前暂未配置任何 up uid")

        while True:
            try:
                await self.warm_up_cache()
            except Exception as e:
                logger.error(f"[bili_订阅] 预加载循环发生异常: {e}")
            #30min
            await asyncio.sleep(1800)

    async def warm_up_cache(self):
        """用于预加载 get_up_info 函数进行的内容"""
        if not self.sub_map:
            return

        uids_to_fetch = [uid for uid in self.sub_map.keys() if uid not in self.uid_name_cache]

        if not uids_to_fetch:
            logger.debug(f"[bili_订阅] 目前所有 {len(self.sub_map)} up 状态信息已是最新")
            return

        logger.info(f"[bili_订阅] 发现 {len(uids_to_fetch)} 个新 up uid，开始预加载 up 信息")

        for uid in uids_to_fetch:
            try:
                await self.get_up_info(uid)

            except Exception as e:
                err_msg = str(e)
                #处理风控
                if "-352" in err_msg or "风控" in err_msg:
                    logger.warning(f"[bili_订阅] 触发错误 -352，风控校验失败。1分钟后再尝试")
                    await asyncio.sleep(60)
                else:
                    logger.warning(f"[bili_订阅] 预加载 UID {uid} 失败: {err_msg}")
                continue

        logger.info("[bili_订阅] 预载加载 up 信息状态完成")

    async def get_up_info(self, uid: int) -> str:
        """用户解析获取uid的名字等信息"""
        from bilibili_api import user
        import asyncio

        if uid in self.uid_name_cache:
            return self.uid_name_cache[uid]

        try:
            await asyncio.sleep(1)

            u = user.User(uid=uid, credential=await self.login.credential)
            user_info = await u.get_user_info()

            name = user_info.get("name", f"该{uid}未查询到up主名字")

            self.uid_name_cache[uid] = name
            return name
        except Exception as e:
            err_msg = str(e)
            logger.warning(f"获取 UID {uid} up主名字失败: {err_msg}")
            #处理风控向上走到 warm_up_cache 处理
            if "-352" in err_msg or "风控" in err_msg:
                raise e

            return f"未知UP主"

    async def parse_video(
        self,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_num: int = 1,
    ):
        """解析视频信息

        Args:
            bvid (str | None): bvid
            avid (int | None): avid
            page_num (int): 页码
        """

        from .video import AIConclusion, VideoInfo

        video = await self._get_video(bvid=bvid, avid=avid)
        # 转换为 msgspec struct
        video_info = convert(await video.get_info(), VideoInfo)
        # 获取简介
        text = f"简介: {video_info.desc}" if video_info.desc else None
        # up
        author = self.create_author(video_info.owner.name, video_info.owner.face)
        # 处理分 p
        page_info = video_info.extract_info_with_page(page_num)

        # 获取 AI 总结
        ai_summary = ""
        if self.login._credential:
            try:
                cid = await video.get_cid(page_info.index)
                ai_conclusion = await video.get_ai_conclusion(cid)
                ai_conclusion = convert(ai_conclusion, AIConclusion)
                ai_summary = ai_conclusion.summary
            except Exception as e:
                logger.warning(f"获取视频 AI 总结失败 (BVID: {video_info.bvid}): {e}")
                ai_summary = "暂无 AI 总结或获取失败，可能哔哩哔哩 cookie 未配置或失效"
        else:
            ai_summary = "哔哩哔哩 cookie 未配置或失效, 无法使用 AI 总结"

        url = f"https://bilibili.com/{video_info.bvid}"
        url += f"?p={page_info.index + 1}" if page_info.index > 0 else ""

        # 视频下载 task
        async def download_video():
            output_path = self.cfg.cache_dir / f"{video_info.bvid}-{page_num}.mp4"
            if output_path.exists():
                return output_path
            v_url, a_url = await self.extract_download_urls(
                video=video, page_index=page_info.index
            )
            if page_info.duration > self.cfg.max_duration:
                raise DurationLimitException
            if a_url is not None:
                return await self.downloader.download_av_and_merge(
                    v_url,
                    a_url,
                    output_path=output_path,
                    headers=self.headers,
                    proxy=self.proxy,
                )
            else:
                return await self.downloader.streamd(
                    v_url,
                    file_name=output_path.name,
                    headers=self.headers,
                    proxy=self.proxy,
                )

        video_task = asyncio.create_task(download_video())
        video_content = self.create_video_content(
            video_task,
            page_info.cover,
            page_info.duration,
        )

        return self.result(
            url=url,
            title=page_info.title,
            timestamp=page_info.timestamp,
            text=text,
            author=author,
            contents=[video_content],
            extra={"info": ai_summary},
        )

    async def parse_dynamic(self, dynamic_id: int):
        """解析动态信息，含专栏等

        Args:
            dynamic_id (int): 动态 id
        """
        from bilibili_api.dynamic import Dynamic

        from .dynamic import DynamicData

        dynamic_ = Dynamic(dynamic_id, await self.login.credential)

        #动态为专栏时候的
        if await dynamic_.is_article():
            return await self.parse_read_with_opus(dynamic_id)
        
        dynamic_info = convert(await dynamic_.get_info(), DynamicData).item

        return await self._parse_dynamic_info(dynamic_info)

    async def _parse_dynamic_info(self, dynamic_info: DynamicInfo):
        """解析动态信息

        Args:
            dynamic_info (DynamicInfo)
        """

        #增加堆转发内容判断
        repost = None
        if dynamic_info.type == "DYNAMIC_TYPE_FORWARD" and dynamic_info.orig is not None:
            repost = await self._parse_dynamic_info(dynamic_info.orig)

        # 媒体内容
        author = self.create_author(dynamic_info.name, dynamic_info.avatar)
        contents: list[MediaContent] = []

        for image_url in dynamic_info.image_urls:
            img_task = self.downloader.download_img(
                image_url, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(img_task))

        dynamic_url=f'https://t.bilibili.com/{dynamic_info.id_str}'

        return self.result(
            title=dynamic_info.title,
            text=dynamic_info.text,
            timestamp=dynamic_info.timestamp,
            author=author,
            contents=contents,
            repost=repost,
            url=dynamic_url
        )

    async def parse_opus(self, opus_id: int):
        """解析图文动态信息

        Args:
            opus_id (int): 图文动态 id
        """
        opus = Opus(opus_id, await self.login.credential)
        return await self._parse_opus_obj(opus)

    async def parse_read_with_opus(self, read_id: int):
        """解析动态和图文, 使用 Opus 接口
        Args:
            read_id (int): 专栏 id
        """
        # from bilibili_api.article import Article

        from bilibili_api.dynamic import Dynamic

        # article = Article(read_id)
        # return await self._parse_opus_obj(await article.turn_to_opus())

        dynamic = Dynamic(read_id, await self.login.credential)
        return await self._parse_opus_obj(dynamic.turn_to_opus())

    async def _parse_opus_obj(self, bili_opus: Opus):
        """解析图文动态信息
        Args:
            opus_id (int): 图文动态 id
        Returns:
            ParseResult: 解析结果
        """
        import re
        from .opus import ImageNode, OpusItem

        opus_info = await bili_opus.get_info()

        if not isinstance(opus_info, dict):
            raise ParseException("获取图文动态信息失败")

        # 结构体
        raw_title = None
        try:
            def _find_title(data, found):
                if isinstance(data, dict):
                    for t in ["opus", "draw", "article"]:
                        if t in data and isinstance(data[t], dict) and data[t].get("title"):
                            found["major_title"] = str(data[t]["title"])
                    for v in data.values():
                        if "major_title" in found: return
                        _find_title(v, found)
                elif isinstance(data, list):
                    for item in data:
                        if "major_title" in found: return
                        _find_title(item, found)

            found_titles = {}
            _find_title(opus_info, found_titles)
            raw_title = found_titles.get("major_title")

        except Exception as e:
            logger.warning(f"扫描标题失败: {e}")

        #判断给的opus链接是专栏还是动态
        is_article = await self._check_is_article(opus_info)

        #先试试能否在接口返回的 json里找到封面,若没有只能从网页拉取，目前是否有其它相关接口提供了封面还不清楚，没去具体查找
        raw_cover_url = None
        if is_article:
            raw_cover_url = await self.__cover_from_json(opus_info)
            if not raw_cover_url:
                raw_cover_url = await self._get_web_cover(bili_opus)

        #加上大小限制免得拿到原图过大
        if raw_cover_url:
            if raw_cover_url.startswith("//"):
                raw_cover_url = f"https:{raw_cover_url}"
            if "@" not in raw_cover_url:
                raw_cover_url += "@700w.webp"
                logger.info(f"获取封面url: {raw_cover_url}")

        # 转换为结构体
        opus_data = convert(opus_info, OpusItem)
        author = self.create_author(*opus_data.name_avatar)

        # 准备contests和文本
        contents: list[MediaContent] = []
        current_text = ""
        full_text = ""

        # 封面
        if raw_cover_url:
            contents.insert(0, self.create_graphics_content(raw_cover_url, text=""))
            # contents.append(
            #     self.create_graphics_content(raw_cover_url, text="")
            # )

        #处理正文
        for node in opus_data.gen_text_img():
            if isinstance(node, ImageNode):
                img_url = node.url
                if img_url.startswith("//"):
                    img_url = f"https:{img_url}"
                contents.append(
                    self.create_graphics_content(
                        img_url, current_text.strip(), node.alt
                    )
                )
                current_text = ""
            elif hasattr(node, "text"):
                text_part = str(node.text)
                current_text += text_part
                full_text += text_part

        # 提取标题，大概类似 #www#这样 -> www,要是多个就凭借起来
        topic_matches = re.findall(r"#([^#]+)#", full_text)
        topics_title = " ".join(topic_matches) if topic_matches else None

        # 排列标题优先级,专栏，动态，最后都没有就默认 b站的标题去掉了小尾巴，去不去都行
        final_title = raw_title
        if not final_title:
            final_title = topics_title
        if not final_title:
            final_title = opus_data.title
            if final_title and final_title.endswith(" - 哔哩哔哩"):
                final_title = final_title.replace(" - 哔哩哔哩", "")

        opus_url = f"https://www.bilibili.com/opus/{opus_data.item.id_str}"

        return self.result(
            title=final_title,
            author=author,
            timestamp=opus_data.timestamp,
            cover=None,
            contents=contents,
            text=current_text.strip(),
            url=opus_url
        )
    
    async def _check_is_article(self, opus_info: dict) -> bool:
        """检查是否为专栏文章

        Args:
            opus_info (dict): Opus 信息字典

        Returns:
            bool: 是否为专栏文章，专栏 comment_type 为固定的12值
        """
        try:
            item_basic = opus_info.get("item", {}).get("basic", {})
            if item_basic.get("comment_type") == 12:
                return True
        except Exception as e:
            logger.warning(f"判断文章类型失败: {e}")
            return False
        return False

    async def __cover_from_json(self, opus_info: dict) -> str | None:
        """尝试从 Opus JSON 中提取封面 URL

        Args:
            opus_info (dict): Opus 信息字典

        Returns:
            str | None: 封面 URL,如果未找到则返回 None
        """
        try:
            modules_data = opus_info.get("item", {}).get("modules", [])
            mod_list = modules_data if isinstance(modules_data, list) else list(modules_data.values()) if isinstance(modules_data, dict) else []
            for mod in mod_list:
                if not isinstance(mod, dict): continue
                major = mod.get("module_dynamic", {}).get("major", {})
                for t in ["opus", "article", "common"]:
                    target = major.get(t, {})
                    if isinstance(target, dict):
                        cov = target.get("cover") or target.get("summary", {}).get("cover")
                        if isinstance(cov, str) and cov:
                            return cov
                        elif isinstance(target.get("covers"), list) and target["covers"]:
                            return str(target["covers"][0])
        except Exception as e:
            logger.warning(f"从接口Json中提取封面失败: {e}")
        return None

    async def _get_web_cover(self, bili_opus: Opus) -> str | None:
        """从网页源码中提取封面 URL，从接口中没找到封面相关的字段，
           就采用这种网页爬虫的方式来提取封面的url链接，若有找到接口以后可以取消掉这个方法
           毕竟不太好，可能会被反爬针对，暂时先这么写着后面花时间再找找

        Args:
            bili_opus (Opus): Opus 对象

        Returns:
            str | None: 封面 URL,如果未找到则返回 None
        """
        import re
        import json
        import aiohttp
        try:
            if not hasattr(bili_opus, "get_opus_id"):
                return None
                
            opus_url = f"https://www.bilibili.com/opus/{bili_opus.get_opus_id()}"
            logger.info(f"使用网页获取封面: {opus_url}")
            
            async with aiohttp.ClientSession() as session:
                proxy_url = getattr(self, "proxy", None)
                req_headers = getattr(self, "headers", {})
                
                async with session.get(opus_url, headers=req_headers, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                    html_text = await resp.text()
                    
                    # 找寻 dom window.__INITIAL_STATE__
                    state_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html_text)
                    if state_match:
                        try:
                            state_json = json.loads(state_match.group(1))
                            cov = state_json.get("detail", {}).get("basic", {}).get("cover") or \
                                  state_json.get("detail", {}).get("modules", [{}])[0].get("module_dynamic", {}).get("major", {}).get("opus", {}).get("summary", {}).get("cover")
                            if cov: return cov
                        except: pass

                    # 匹配 b-img__inner 字段
                    # img_match = re.search(r'b-img__inner.*?src="([^"]+?hdslb\.com/bfs/new_dyn/[^"]+)"', html_text, re.S)
                    # if img_match: return img_match.group(1)

                    # 上面要是都找不到直接正则匹配图片域名,比较的暴力匹配╰(*°▽°*)╯
                    brute_match = re.search(r'//i[0-9]\.hdslb\.com/bfs/new_dyn/[a-zA-Z0-9_\.\-]+', html_text)
                    if brute_match: return brute_match.group(0)
                    
        except Exception as e:
            logger.warning(f"网页获取失败: {e}")
        return None

    async def parse_live(self, room_id: int):
        """解析直播信息

        Args:
            room_id (int): 直播 id

        Returns:
            ParseResult: 解析结果
        """
        from bilibili_api.live import LiveRoom

        from .live import RoomData

        room = LiveRoom(room_display_id=room_id, credential=await self.login.credential)
        info_dict = await room.get_room_info()

        room_data = convert(info_dict, RoomData)
        contents: list[MediaContent] = []
        # 下载封面
        if cover := room_data.cover:
            cover_task = self.downloader.download_img(
                cover, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(cover_task))

        # 下载关键帧
        if keyframe := room_data.keyframe:
            keyframe_task = self.downloader.download_img(
                keyframe, headers=self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(keyframe_task))

        author = self.create_author(room_data.name, room_data.avatar)

        #修改 url直播链接
        # url = f"https://www.bilibili.com/blackboard/live/live-activity-player.html?enterTheRoom=0&cid={room_id}"
        url = f"https://live.bilibili.com/{room_id}"
        return self.result(
            url=url,
            title=room_data.title,
            text=room_data.detail,
            contents=contents,
            author=author,
        )

    async def parse_favlist(self, fav_id: int):
        """解析收藏夹信息

        Args:
            fav_id (int): 收藏夹 id

        Returns:
            list[GraphicsContent]: 图文内容列表
        """
        from bilibili_api.favorite_list import get_video_favorite_list_content

        from .favlist import FavData

        # 只会取一页，20 个
        fav_dict = await get_video_favorite_list_content(fav_id)

        if fav_dict["medias"] is None:
            raise ParseException("收藏夹内容为空, 或被风控")

        favdata = convert(fav_dict, FavData)

        return self.result(
            title=favdata.title,
            timestamp=favdata.timestamp,
            author=self.create_author(favdata.info.upper.name, favdata.info.upper.face),
            contents=[
                self.create_graphics_content(fav.cover, fav.desc)
                for fav in favdata.medias
            ],
        )

    async def _get_video(
        self, *, bvid: str | None = None, avid: int | None = None
    ) -> Video:
        """解析视频信息

        Args:
            bvid (str | None): bvid
            avid (int | None): avid
        """
        if avid:
            return Video(aid=avid, credential=await self.login.credential)
        elif bvid:
            return Video(bvid=bvid, credential=await self.login.credential)
        else:
            raise ParseException("avid 和 bvid 至少指定一项")

    async def extract_download_urls(
        self,
        video: Video | None = None,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_index: int = 0,
    ) -> tuple[str, str | None]:
        """解析视频下载链接
           增加多编码回退与优先级排序机制

        Args:
            bvid (str | None): bvid
            avid (int | None): avid
            page_index (int): 页索引 = 页码 - 1
        """

        # from bilibili_api.video import (
        #     AudioStreamDownloadURL,
        #     VideoDownloadURLDataDetecter,
        #     VideoStreamDownloadURL,
        # )

        from bilibili_api.video import VideoCodecs, VideoQuality, AudioQuality

        if video is None:
            video = await self._get_video(bvid=bvid, avid=avid)

        download_url_data = await video.get_download_url(page_index=page_index)
        target_quality_id = getattr(self.video_quality, "value", 64)

        # 获取bili api库的画质和音质的反查字典
        all_quality_map = {e.value: e.name.lstrip("_") for e in VideoQuality}
        all_audio_map = {e.value: e.name.lstrip("_") for e in AudioQuality}

        codec_scores = {}
        score = len(self.codec_priority_list)
        for codec_name in self.codec_priority_list:
            codec_scores[codec_name.upper()] = score
            score -= 1  # 列表越靠前，分数越高

        def get_codec_info(v_data: dict) -> tuple[str, int]:
            """
            使用视频流字典并返回匹配到的编码名称和该编码的得分
            """
            codec_str = v_data.get("codecs", "")
            for val in VideoCodecs:
                if val.value in codec_str:
                    return val.name, codec_scores.get(val.name, 0)
            return "未知编码", 0

        dash = download_url_data.get("dash", {})
        if dash:
            videos = dash.get("video", [])
            audios = dash.get("audio", [])
            if not videos:
                raise DownloadException("获取到的 DASH 视频流为空")

            valid_videos = [
                v for v in videos
                # if v.get("id", 0) <= target_quality_id and v.get("id", 0) not in (125, 126) # 去掉超过设定最高画质的流，以及杜比视界(126)/HDR(125)
                if v.get("id", 0) <= target_quality_id
            ]
            if not valid_videos:
                valid_videos = videos

            # 排序：先选画质，再编码的分数
            valid_videos.sort(
                key=lambda x: (
                    x.get("id", 0),
                    get_codec_info(x)[1]
                ),
                reverse=True
            )

            best_video = valid_videos[0]
            best_video_url = best_video.get("base_url") or best_video.get("url")

            # 获取音频信息
            best_audio_url = None
            best_audio_id = 0
            if audios:
                audios.sort(key=lambda x: x.get("id", 0), reverse=True)
                best_audio = audios[0]
                best_audio_url = best_audio.get("base_url") or best_audio.get("url")
                best_audio_id = best_audio.get("id", 0)

            if not best_video_url:
                raise DownloadException("从 DASH 中提取底层视频 URL 失败")

            # 获取名称
            best_quality_id = best_video.get("id", 0)
            quality_display_name = all_quality_map.get(best_quality_id, "未知画质")
            codec_display_name, codec_final_score = get_codec_info(best_video)

            # 判断是否有音频流
            if best_audio_id > 0:
                audio_display_name = all_audio_map.get(best_audio_id, "未知音质")
            else:
                audio_display_name = "无音频流"

            logger.info(
                f"选择视频流 -> 画质: {quality_display_name} (ID: {best_quality_id}), "
                f"视频编码: {codec_display_name} (详细: {best_video.get('codecs', '无')}), "
                f"音质: {audio_display_name} (ID: {best_audio_id}), "
                f"最终编码得分: {codec_final_score}"
            )
            return best_video_url, best_audio_url

        durls = download_url_data.get("durl", [])
        if durls:
            return durls[0].get("url"), None

        raise DownloadException("解析视频下载链接失败，API 未返回有效的 DASH 或 DURL 数据")

    async def _parse_live_info(self, uid: int, room_info: dict, is_end: bool = False):
        """用于解析直播间信息然后构建 result

        Args:
            uid int: up uid
            room_inf dict: 查询接口返回的字典信息
            is_end bool: 判断是否下播
        """
        import time

        room_id = room_info.get("room_id")
        title = room_info.get("title", "无标题")
        uname = room_info.get("uname", str(uid))
        face = room_info.get("face", "")
        cover_url = room_info.get("cover_from_user", "")
        area_name = room_info.get("area_name", "未分区")
        game_name = room_info.get("area_v2_name", "未知游戏")
        room_url = f"https://live.bilibili.com/{room_id}"

        live_start_ts = room_info.get("live_time", 0)

        author = self.create_author(uname, face)
        contents = []
        #计算直播时间，结束时候
        if is_end:
            if live_start_ts > 0:
                duration_sec = int(time.time()) - live_start_ts
                hours = duration_sec // 3600
                minutes = (duration_sec % 3600) // 60
                seconds = duration_sec % 60
                duration_str = f"{hours}小时{minutes}分钟{seconds}秒" if hours > 0 else f"{minutes}分钟{seconds}秒"
            else:
                duration_str = "未知"

            msg_title = f"直播已结束：{title}"
            msg_text = f"分区：{area_name}\n游戏：{game_name}\n本次直播时长：{duration_str}"
        else:
            msg_title = f"直播已开始：{title}"
            msg_text = f"分区：{area_name}\n游戏：{game_name}"
            if cover_url:
                img_task = self.downloader.download_img(cover_url, headers=self.headers, proxy=self.proxy)
                contents.append(ImageContent(img_task))

        return self.result(
            title=msg_title,
            text=msg_text,
            timestamp=int(time.time()),
            author=author,
            contents=contents,
            url=room_url
        )

    async def _subscription_loop_live(self):
        """进行直播间轮询订阅
           因为 User.get_live_info 接口频繁报错导致使用效果不佳，需要等待上游 api修复
           现在改用自己通过接口访问轮询
        """
        import traceback
        from bilibili_api.utils.network import Api

        await asyncio.sleep(5.0)

        while True:
            try:
                poll_uids = [int(str(uid).strip()) for uid in self.sub_map.keys()]
                if not poll_uids:
                    await asyncio.sleep(60.0)
                    continue

                LIVE_API_CONFIG = {
                    "url": "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids",
                    "method": "GET",
                    "verify": False,
                    "params": {"uids[]": "list<int>: up uid"},
                    "comment": "通过up uid列表获取直播间状态，包含知否在直播、房间号、直播时长信息等",
                }

                # 将总列表才分查询，避免一次查询过多导致接口报错
                batch_size = 10
                for i in range(0, len(poll_uids), batch_size):
                    batch_uids = poll_uids[i:i + batch_size]
                    live_params = {"uids[]": batch_uids}

                    try:
                        resp = await Api(**LIVE_API_CONFIG, no_csrf=True).update_params(**live_params).result

                        if isinstance(resp, dict):
                            for uid_str, room_info in resp.items():
                                uid = int(uid_str)
                                current_status = room_info.get("live_status") #0未播，1直播
                                cache_key = str(uid)

                                last_cache = self._live_status_cache.get(cache_key, {})
                                if isinstance(last_cache, int):
                                    last_status = last_cache
                                    last_live_time = 0
                                else:
                                    last_status = last_cache.get("status", 0)
                                    last_live_time = last_cache.get("live_time", 0)

                                is_startup = uid not in self._first_live_check_done
                                action = None

                                #设置状态
                                if is_startup:
                                    self._first_live_check_done.add(uid)
                                    if current_status == 1:
                                        logger.info(f"[bili_订阅] 发现 up uid {uid} 正在直播")
                                        action = "start"
                                else:
                                    if last_status != 1 and current_status == 1:
                                        logger.info(f"[bili_订阅] 检测到 up uid {uid} 开播")
                                        action = "start"
                                    elif last_status == 1 and current_status != 1:
                                        logger.info(f"[bili_订阅] 检测到 up uid {uid} 下播")
                                        action = "end"

                                if action:
                                    targets = self.sub_map.get(uid) or self.sub_map.get(str(uid)) or {}
                                    target_groups = targets.get("groups", [])
                                    target_users = targets.get("users", [])

                                    if target_groups or target_users:
                                        from ...render import Renderer
                                        from ...sender import MessageSender

                                        is_end = (action == "end")

                                        #下播，因为可能 live_time 没有了，利用缓存机制计算
                                        if is_end and last_live_time > 0:
                                            room_info["live_time"] = last_live_time

                                        live_result = await self._parse_live_info(uid, room_info, is_end=is_end)

                                        renderer = Renderer(self.cfg)
                                        sender = MessageSender(self.cfg, renderer)

                                        try:
                                            await sender.send_proactive_msg(
                                                context=self.cfg.context,
                                                result=live_result,
                                                sub_groups=list(target_groups),
                                                sub_users=list(target_users),
                                                platforms=self.platforms,
                                                dynamic_id=None,
                                                platform_botid=self.platform_botid,
                                                only_previewCard=self.only_previewCard
                                            )
                                        except Exception as e:
                                            logger.error(f"[bili_订阅] 发送 UP主 {uid} 的推送失败: {e}")

                                self._live_status_cache[cache_key] = {
                                    "status": current_status,
                                    "live_time": room_info.get("live_time", 0) if current_status == 1 else 0
                                }

                    except Exception as e:
                        logger.warning(f"[bili_订阅] 直播轮询批次 {i//batch_size + 1} 发生异常: {e}")

                    if i + batch_size < len(poll_uids):
                        await asyncio.sleep(1.0)

                await self._save_live_cache()

            except Exception as e:
                logger.error(f"[bili_订阅] 直播订阅循环发生异常: {traceback.format_exc()}")

            await asyncio.sleep(float(self.sub_interval) * 60.0)

    async def _subscription_loop(self):
        """进行订阅轮询

        Args:
            

        Returns:
            
        """
        import traceback
        from bilibili_api import user

        await asyncio.sleep(5.0) # 用float

        while True:
            try:
                poll_queue=[]
                for uid, targets in self.sub_map.items():
                    poll_queue.append((str(uid), targets["groups"], targets["users"]))

                #遍历，请求。rule -> 根据 targets 发送到具体的群或人
                for uid_str, target_groups, target_users in poll_queue:
                    if not uid_str: continue

                    uid = int(str(uid_str).strip())
                    cache_key = str(uid)

                    newest_item = None

                    u = user.User(uid=uid, credential=await self.login.credential)
                    try:
                        resp = await u.get_dynamics_new()
                    except Exception as e:
                        logger.warning(f"[bili_订阅] 获取 UP主 {uid}, 动态失败: {e}")
                        await asyncio.sleep(float(self.sub_delay))
                        continue

                    items = resp.get("items", [])
                    if not items:
                        await asyncio.sleep(float(self.sub_delay))
                        continue

                    #重新调整逻辑，改用时间戳
                    cache_entry = self._last_dynamic_cache.get(cache_key)
                    is_first_init = False
                    if not cache_entry:
                        self._last_dynamic_cache[cache_key] = {
                            "is_first_init": 1,
                            "dynamics": {}
                        }
                        is_first_init = True
                    else:
                        # 兼容旧版本缓存数据结构
                        if isinstance(cache_entry, str):
                            self._last_dynamic_cache[cache_key] = {
                                "is_first_init": 0,
                                "dynamics": {cache_entry: {"timestamp": 0, "status": "sent"}}
                            }
                        elif "is_first_init" not in cache_entry:
                            new_dyn_dict = {}
                            for k, v in cache_entry.items():
                                new_dyn_dict[str(k)] = {"timestamp": 0, "status": v}
                            self._last_dynamic_cache[cache_key] = {
                                "is_first_init": 0,
                                "dynamics": new_dyn_dict
                            }

                        # 判断当前是否为第一次记录
                        is_first_init = self._last_dynamic_cache[cache_key].get("is_first_init") == 1

                    uid_cache_full = self._last_dynamic_cache[cache_key]
                    uid_cache_dyn = uid_cache_full["dynamics"]

                    #记录当前置顶动态，用于缓存清理时保护置顶动态
                    pinned_dynamic_ids = set()
                    recent_items = []
                    normal_item_count = 0
                    for item in items:
                        try:
                            is_pinned = item["modules"]["module_tag"]["text"] == "置顶"
                        except:
                            is_pinned = False

                        #同时去除type 为直播 DYNAMIC_TYPE_LIVE_RCMD 类型的动态
                        dyn_type = item.get("type", "")
                        if dyn_type != "DYNAMIC_TYPE_LIVE_RCMD" and (is_pinned or normal_item_count < 5):
                            recent_items.append(item)
                            if is_pinned:
                                pinned_dynamic_ids.add(str(item["id_str"]))
                            else:
                                normal_item_count += 1

                    #让旧的在前面倒叙发送
                    recent_items.reverse()

                    #滑动窗口动态处理基于时间错非原来的动态id
                    for newest_item in recent_items:
                        dynamic_id = str(newest_item["id_str"])
                        is_pinned = dynamic_id in pinned_dynamic_ids
                        try:
                            pub_ts = int(newest_item["modules"]["module_author"]["pub_ts"])
                        except Exception:
                            pub_ts = 0

                        # 获取当前动态的状态
                        dyn_info = uid_cache_dyn.get(dynamic_id, {})
                        status = dyn_info.get("status", "new")

                        # 发送过或已跳过的忽略
                        if status == "sent" or status == "skip":
                            continue

                        # 第一次使用默认都当发送过
                        if is_first_init:
                            uid_cache_dyn[dynamic_id] = {"timestamp": pub_ts, "status": "sent"}
                            uid_cache_full["is_first_init"] = 0  # 标记后续不再是首次
                            await self._save_cache()
                            continue

                        if not is_first_init and uid_cache_dyn and not is_pinned:
                            try:
                                max_cached_ts = max(
                                    [v.get("timestamp", 0) for v in uid_cache_dyn.values() if isinstance(v, dict)],
                                    default=0
                                )

                                # 如果当前动态时间戳比缓存里最新的时间戳要旧，则判定为历史删减暴露出的老动态
                                if pub_ts > 0 and max_cached_ts > 0 and pub_ts < max_cached_ts:
                                    logger.info(f"[bili_订阅] 发现因删动态获取到老动态id：{dynamic_id} (发布时间落后于历史记录)，已自动跳过不发送")
                                    uid_cache_dyn[dynamic_id] = {"timestamp": pub_ts, "status": "skip"}
                                    continue
                            except Exception as e:
                                logger.warning(f"[bili_订阅] 记录最大动态时间戳发生了异常: {e}")

                        try:
                            int_dynamic_id = int(dynamic_id)
                            parsed_result = await self.parse_dynamic(int_dynamic_id)

                            if parsed_result:
                                # 抽奖过滤
                                if getattr(self, "ignore_lottery", False):
                                    _result_text = f"{parsed_result.header or ''} {parsed_result.text or ''}"
                                    if await self._is_lottery(_result_text):
                                        logger.info(f"[bili订阅] 动态 {dynamic_id} 确认为抽奖动态将过滤不推送")
                                        uid_cache_dyn[dynamic_id] = {"timestamp": pub_ts, "status": "skip"}
                                        await self._save_cache()
                                        continue

                                from ...render import Renderer
                                from ...sender import MessageSender
                                renderer = Renderer(self.cfg)
                                sender = MessageSender(self.cfg, renderer)

                                if target_groups or target_users:
                                    await sender.send_proactive_msg(
                                        context=self.cfg.context,
                                        result=parsed_result,
                                        sub_groups=list(target_groups),
                                        sub_users=list(target_users),
                                        platforms=self.platforms,
                                        dynamic_id=dynamic_id,
                                        platform_botid=self.platform_botid,
                                        only_previewCard=self.only_previewCard
                                    )
                                # 成功发送
                                uid_cache_dyn[dynamic_id] = {"timestamp": pub_ts, "status": "sent"}

                        except Exception as e:
                            # 失败重试
                            max_retries = 3

                            if status == "new":
                                current_fail_count = 0
                            elif status.startswith("fail_"):
                                try:
                                    current_fail_count = int(status.split("_")[1])
                                except (ValueError, IndexError):
                                    current_fail_count = 0
                            else:
                                current_fail_count = 0

                            current_fail_count += 1

                            if current_fail_count >= max_retries:
                                uid_cache_dyn[dynamic_id] = {"timestamp": pub_ts, "status": "sent"}
                                logger.error(f"[bili_订阅] 动态 {dynamic_id} 连续失败 {max_retries} 次，放弃发送")
                            else:
                                uid_cache_dyn[dynamic_id] = {"timestamp": pub_ts, "status": f"fail_{current_fail_count}"}
                                logger.warning(f"[bili_订阅] 动态 {dynamic_id} 第 {current_fail_count} 次处理失败: {e}")

                        await self._save_cache()
                        await asyncio.sleep(1.0)

                    #清理缓存，uid保留最多10条非置顶动态id的记录
                    keys = [k for k in uid_cache_dyn.keys() if k not in pinned_dynamic_ids]
                    if len(keys) > 10:
                        for k in keys[:-10]:
                            uid_cache_dyn.pop(k, None)
                        await self._save_cache()

                    await asyncio.sleep(float(self.sub_delay))

            except Exception as e:
                logger.error(f"[bili_订阅] 动态订阅循环发生异常: {traceback.format_exc()}")

            await asyncio.sleep(float(self.sub_interval) * 60.0)

    async def _save_cache(self):
        """将缓存保存到对于data本地数据目录下"""
        try:
            max_entries = 100
            while len(self._last_dynamic_cache) > max_entries:
                oldest_key = next(iter(self._last_dynamic_cache))
                self._last_dynamic_cache.pop(oldest_key)
                logger.warning(f"数量达到最大 {max_entries}，移除旧纪录: {oldest_key}")

            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self._last_dynamic_cache, f, ensure_ascii=False, indent=4)

        except Exception as e:
            logger.error(f"保存失败: {e}")

    async def _save_live_cache(self):
        """将直播状态缓存保存到data本地目录下"""
        try:
            with open(self.live_cache_file, "w", encoding="utf-8") as f:
                json.dump(self._live_status_cache, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"[bili_订阅] 保存直播缓存失败: {e}")

    async def _is_lottery(self, text: str) -> bool:
        """判断文本是否包含了抽奖相关动态"""
        if not text:
            return False

        # keywords = ["恭喜", "中奖", "私信", "奖品", "抽奖"]
        keywords = self.ignore_lottery_content
        hits = sum(1 for k in keywords if k in text)

        if hits >= 3:
            return True

        return False
