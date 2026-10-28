from typing import Any

from msgspec import Struct, convert


class AuthorInfo(Struct):
    """作者信息"""

    name: str
    face: str
    mid: int
    pub_time: str
    pub_ts: int | str  # 兼容 B 站返回字符串格式的时间戳
    # jump_url: str
    # following: bool = False
    # official_verify: dict[str, Any] | None = None
    # vip: dict[str, Any] | None = None
    # pendant: dict[str, Any] | None = None


class VideoArchive(Struct):
    """视频信息"""

    aid: str
    bvid: str
    title: str
    desc: str
    cover: str
    # duration_text: str
    # jump_url: str
    # stat: dict[str, str]
    # badge: dict[str, Any] | None = None


class OpusImage(Struct):
    """图文动态图片信息"""

    url: str
    # width: int
    # height: int
    # size: float
    # aigc: dict[str, Any] | None = None
    # live_url: str | None = None


class OpusSummary(Struct):
    """图文动态摘要"""

    text: str
    # rich_text_nodes: list[dict[str, Any]]


class OpusContent(Struct):
    """图文动态内容"""

    jump_url: str
    pics: list[OpusImage]
    summary: OpusSummary
    title: str | None = None
    # fold_action: list[str] | None = None


class DynamicMajor(Struct):
    """动态主要内容"""

    type: str
    archive: VideoArchive | None = None
    opus: OpusContent | None = None

    @property
    def title(self) -> str | None:
        """获取标题"""
        if self.type == "MAJOR_TYPE_ARCHIVE" and self.archive:
            return self.archive.title
        return None

    @property
    def text(self) -> str | None:
        """获取文本内容"""
        if self.type == "MAJOR_TYPE_ARCHIVE" and self.archive:
            return self.archive.desc
        elif self.type == "MAJOR_TYPE_OPUS" and self.opus:
            return self.opus.summary.text
        return None

    @property
    def image_urls(self) -> list[str]:
        """获取图片URL列表"""
        if self.type == "MAJOR_TYPE_OPUS" and self.opus:
            return [pic.url for pic in self.opus.pics]
        elif self.type == "MAJOR_TYPE_ARCHIVE" and self.archive and self.archive.cover:
            return [self.archive.cover]
        return []

    @property
    def cover_url(self) -> str | None:
        """获取封面URL"""
        if self.type == "MAJOR_TYPE_ARCHIVE" and self.archive:
            return self.archive.cover
        return None


class DynamicModule(Struct):
    """动态模块"""

    module_author: AuthorInfo
    module_dynamic: dict[str, Any] | None = None
    module_stat: dict[str, Any] | None = None

    @property
    def author_name(self) -> str:
        """获取作者名称"""
        return self.module_author.name

    @property
    def author_face(self) -> str:
        """获取作者头像URL"""
        return self.module_author.face

    @property
    def pub_ts(self) -> int | None:
        """获取发布时间戳"""
        try:
            return int(self.module_author.pub_ts)
        except (ValueError, TypeError):
            return None

    @property
    def major_info(self) -> dict[str, Any] | None:
        """获取主要内容信息"""
        if self.module_dynamic:
            return self.module_dynamic.get("major")
        return None


class DynamicInfo(Struct):
    """动态信息"""

    id_str: str
    type: str
    visible: bool
    modules: DynamicModule
    basic: dict[str, Any] | None = None
    orig: "DynamicInfo | None" = None
    desc: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        """获取作者名称"""
        return self.modules.author_name

    @property
    def avatar(self) -> str:
        """获取作者头像URL"""
        return self.modules.author_face

    @property
    def timestamp(self) -> int | None:
        """获取发布时间戳"""
        return self.modules.pub_ts

    @property
    def title(self) -> str | None:
        """获取标题"""
        major_info = self.modules.major_info
        if major_info:
            major = convert(major_info, DynamicMajor)
            return major.title
        return None

    @property
    def text(self) -> str | None:
        """获取文本内容"""
        major_info = self.modules.major_info
        if major_info:
            major = convert(major_info, DynamicMajor)
            return major.text

        #增加一个转发获取转发发送的正文
        modules_dynamic = self.modules.module_dynamic
        if modules_dynamic and modules_dynamic.get("desc"):
            desc_info = convert(modules_dynamic["desc"], DynamicDesc)
            return desc_info.text

        return None

    @property
    def image_urls(self) -> list[str]:
        """获取图片URL列表"""
        major_info = self.modules.major_info
        if major_info:
            major = convert(major_info, DynamicMajor)
            return major.image_urls
        return []

    @property
    def cover_url(self) -> str | None:
        """获取封面URL"""
        major_info = self.modules.major_info
        if major_info:
            major = convert(major_info, DynamicMajor)
            return major.cover_url
        return None

class DynamicDesc(Struct):
    """动态描述内容"""
    text: str

class DynamicData(Struct):
    """动态项目"""

    item: DynamicInfo
