"""番剧/电影的元数据解析，不请求播放地址或创建视频下载任务"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html import unescape
from time import time
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from bilibili_api.bangumi import Bangumi
from bilibili_api.user import User

from ...data import Author, ParseResult, SendGroup
from ..base import ParseException

if TYPE_CHECKING:
    from . import BilibiliParser

_BILIBILI_TIMEZONE = timezone(timedelta(hours=8))
_FALLBACK_AVATAR_UID = 8047632

_SEASON_URL = "https://www.bilibili.com/bangumi/play/ss{season_id}"
_EPISODE_URL = "https://www.bilibili.com/bangumi/play/ep{episode_id}"


def _first(data: Any, *paths: str) -> Any:
    """Return the first non-empty value from dotted mapping paths."""

    for path in paths:
        value = data
        for key in path.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if value not in (None, ""):
            return value
    return None


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        value = value.get("name") or value.get("title") or value.get("value")
    if isinstance(value, (list, tuple)):
        values = [_text(item) for item in value]
        value = "、".join(item for item in values if item)
    if not isinstance(value, (str, int, float)):
        return None
    return unescape(re.sub(r"<[^>]+>", "", str(value))).strip() or None


def _timestamp(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return int(number) if number > 0 else None
    except (TypeError, ValueError, OverflowError):
        pass

    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=_BILIBILI_TIMEZONE)
        return int(date.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _rating(meta: dict[str, Any]) -> str:
    score = _first(
        meta,
        "rating.score",
        "rating.score_float",
        "media.rating.score",
        "media.rating.score_float",
    )
    count = _first(
        meta,
        "rating.count",
        "rating.num",
        "rating.rating_count",
        "media.rating.count",
        "media.rating.num",
    )
    if score in (None, "", 0) or count in (None, "", 0):
        return "暂无评分"

    score_text = _text(score) or "暂无评分"
    try:
        count_value = int(count) if count not in (None, "") else None
    except (TypeError, ValueError):
        count_value = None
    if count_value is not None and count_value > 0:
        return f"{score_text}（{count_value} 人评价）"
    return "暂无评分"


def _episode_items(episode_list: Any) -> list[dict[str, Any]]:
    if not isinstance(episode_list, dict):
        return []
    main_section = episode_list.get("main_section")
    if isinstance(main_section, dict):
        episodes = main_section.get("episodes")
        if isinstance(episodes, list):
            return [item for item in episodes if isinstance(item, dict)]
    episodes = episode_list.get("episodes")
    if isinstance(episodes, list):
        return [item for item in episodes if isinstance(item, dict)]
    return []


def _cover(meta: dict[str, Any], episode: dict[str, Any] | None = None) -> str | None:
    episode = episode or {}
    cover = _text(
        _first(
            episode,
            "cover",
            "cover_url",
            "thumb",
        )
        or _first(meta, "media.cover", "cover", "cover_url", "media.horizontal_cover")
    )
    if cover and cover.startswith("//"):
        cover = f"https:{cover}"
    return cover if cover and cover.startswith(("https://", "http://")) else None


def _description(meta: dict[str, Any]) -> str | None:
    text = _text(
        _first(
            meta,
            "media.evaluate",
            "media.description",
            "media.desc",
            "evaluate",
            "description",
            "desc",
        )
    )
    return text[:1000] if text else None


def _title(meta: dict[str, Any]) -> str:
    return _text(_first(meta, "media.title", "title", "media.name", "name")) or "B站番剧/电影"


def _common_info(meta: dict[str, Any], episode_count: int | None = None) -> list[str]:
    rating = _rating(meta)
    values = [f"评分：{rating}"]
    pub_time = _timestamp(
        _first(
            meta,
            "media.pubtime",
            "media.pub_time",
            "media.publish.pub_time",
            "publish.pub_time",
            "publish.pub_time_ts",
            "pubtime",
            "pub_time",
            "release_date",
        )
    )
    if pub_time is not None:
        date = datetime.fromtimestamp(pub_time, _BILIBILI_TIMEZONE).strftime("%Y-%m-%d")
        values.append(f"发布时间：{date}")
    area = _text(_first(meta, "media.areas", "areas", "media.area", "area"))
    styles = _text(_first(meta, "media.styles", "styles", "media.style", "style"))
    season_type = _text(meta.get("season_type_name"))
    if area:
        values.append(f"地区：{area}")
    if styles:
        values.append(f"类型：{styles}")
    elif season_type:
        values.append(f"类型：{season_type}")
    if episode_count is not None:
        values.append(f"集数：{episode_count}")
    return values


async def _create_author(
    parser: BilibiliParser, up_info: dict[str, Any], credential: Any
) -> Author:
    """缺失上传方信息时使用指定账号头像，头像失败不影响卡片头部的显示"""
    name = _text(up_info.get("uname")) or "番剧/电影"
    avatar = _text(up_info.get("avatar"))
    if not avatar:
        try:
            user = User(uid=_FALLBACK_AVATAR_UID, credential=credential)
            profile = await user.get_user_info()
            avatar = _text(profile.get("face"))
        except Exception as exc:
            logger.warning(f"[bili_解析] 番剧兜底头像信息获取失败: {type(exc).__name__}")
    if avatar and avatar.startswith("//"):
        avatar = f"https:{avatar}"

    author = parser.create_author(name)
    if avatar and avatar.startswith(("https://", "http://")):
        try:
            author.avatar = await parser.downloader.download_img(
                avatar, headers=parser.headers, proxy=parser.proxy
            )
        except Exception as exc:
            logger.warning(f"[bili_解析] 番剧头像下载失败: {type(exc).__name__}")
    return author


def _make_result(
    parser: BilibiliParser,
    *,
    title: str,
    text: str | None,
    cover: str | None,
    url: str,
    info: list[str],
    timestamp: int | None,
    author: Author,
) -> ParseResult:
    contents = []
    if cover:
        contents.append(parser.create_graphics_content(cover, text=""))
    return parser.result(
        title=title,
        author=author,
        text=text,
        timestamp=timestamp,
        url=url,
        contents=contents,
        # 封面只参与预览卡片，不再作为独立图片发送
        send_groups=[SendGroup(render_card=True)],
        extra={"info": "\n".join(info), "layout": "bangumi"},
    )


async def _parse_bangumi(
    parser: BilibiliParser,
    *,
    season_id: int | None = None,
    episode_id: int | None = None,
) -> ParseResult:
    """只调用番剧元数据接口；Episode 的部分方法会间接请求播放地址"""
    target_id = episode_id if episode_id is not None else season_id
    if target_id is None or target_id <= 0:
        raise ParseException("番剧/单集 ID 无效")
    parsed_at = int(time())

    credential = await parser.login.credential
    bangumi = (
        Bangumi(epid=episode_id, credential=credential)
        if episode_id is not None
        else Bangumi(ssid=target_id, credential=credential)
    )
    try:
        overview = await bangumi.get_overview()
    except Exception as exc:
        raise ParseException("番剧/电影元数据获取失败，请稍后重试") from exc
    if not isinstance(overview, dict) or not overview.get("title"):
        raise ParseException("番剧/电影不存在或暂无可用元数据")

    # 评分等补充信息不可用时，仍用概况生成卡片
    try:
        meta = await bangumi.get_meta()
    except Exception as exc:
        logger.warning(f"[bili_解析] 番剧评分信息获取失败: {type(exc).__name__}")
        meta = {}
    data = dict(overview)
    if isinstance(meta, dict) and isinstance(meta.get("media"), dict):
        data["media"] = meta["media"]

    episodes = _episode_items(overview)
    episode = (
        next(
            (item for item in episodes if str(item.get("id")) == str(episode_id)),
            {},
        )
        if episode_id is not None
        else {}
    )
    # 其它内容等可能位于独立 section；只匹配用户发送的单集 ID
    if episode_id is not None and not episode:
        for section in overview.get("section") or []:
            episode = next(
                (
                    item for item in _episode_items(section)
                    if str(item.get("id")) == str(episode_id)
                ),
                {},
            )
            if episode:
                break

    season_title = _title(data)
    episode_title = _text(_first(episode, "long_title", "longtitle", "title"))
    title = f"{season_title} - {episode_title}" if episode_title else season_title
    total = overview.get("total")
    episode_count = total if isinstance(total, int) and total > 0 else None
    info = _common_info(data, episode_count)
    if episode:
        index_title = _text(episode.get("title"))
        if index_title and index_title != episode_title:
            info.insert(0, f"分集：{index_title}")
        badge = _text(episode.get("badge"))
        if badge:
            info.append(badge)
    url = (
        _EPISODE_URL.format(episode_id=episode_id)
        if episode_id is not None
        else _SEASON_URL.format(season_id=season_id)
    )
    return _make_result(
        parser,
        title=title,
        text=_description(data),
        cover=_cover(data, episode),
        url=url,
        info=info,
        timestamp=parsed_at,
        author=await _create_author(parser, overview.get("up_info") or {}, credential),
    )


async def parse_bangumi_season(parser: BilibiliParser, season_id: int) -> ParseResult:
    """解析番剧/电影概况并生成预览结果"""
    return await _parse_bangumi(parser, season_id=season_id)


async def parse_bangumi_episode(parser: BilibiliParser, episode_id: int) -> ParseResult:
    """解析指定剧集及所属作品元数据，不请求播放地址"""
    return await _parse_bangumi(parser, episode_id=episode_id)
