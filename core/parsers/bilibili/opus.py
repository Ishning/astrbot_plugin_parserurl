from collections.abc import Generator
from typing import Any

from msgspec import Struct


class TextNode(Struct, tag="TextNode"):
    """图文动态文本节点"""

    text: str
    """文本内容"""


class ImageNode(Struct, tag="ImageNode"):
    """图文动态图片节点"""

    url: str
    """图片链接"""
    alt: str | None = None
    """图片描述"""


class Author(Struct):
    """图文动态作者信息"""

    name: str
    face: str
    mid: int
    pub_time: str
    pub_ts: int | str  # 修复: B站返回字符串格式的时间戳问题


class Image(Struct):
    """图文动态图片信息"""

    url: str
    # width: int
    # height: int
    # size: float


class Album(Struct):
    """图文动态顶部图片"""

    pics: list[Image]


class TopDisplay(Struct):
    """图文动态顶部展示内容"""

    album: Album | None = None


class Top(Struct):
    """图文动态顶部模块"""

    display: TopDisplay | None = None


class Pic(Struct):
    """图文动态图片组"""

    pics: list[Image]
    style: int


class Text(Struct):
    """图文动态文本"""

    nodes: list[dict[str, Any]]


class Paragraph(Struct):
    """图文动态段落"""

    para_type: int
    text: Text | None = None
    pic: Pic | None = None
    # align: int = 0
    # format: dict[str, Any] | None = None


class Content(Struct):
    """图文动态内容"""

    paragraphs: list[Paragraph]


class Stat(Struct):
    """图文动态统计"""

    like: dict[str, Any] | None = None
    comment: dict[str, Any] | None = None
    forward: dict[str, Any] | None = None
    favorite: dict[str, Any] | None = None
    coin: dict[str, Any] | None = None


class Module(Struct):
    """图文动态模块"""

    module_type: str
    module_author: Author | None = None
    module_content: Content | None = None
    module_top: Top | None = None
    # module_stat: OpusStat | None = None


class Basic(Struct):
    """图文动态基本信息"""

    title: str


class Info(Struct):
    """图文动态信息"""

    id_str: str
    type: int
    modules: list[Module]
    basic: Basic | None = None


class OpusItem(Struct):
    """图文动态项目"""

    item: Info

    @property
    def title(self) -> str | None:
        return self.item.basic.title if self.item.basic else None

    @property
    def name_avatar(self) -> tuple[str, str]:
        author_module = next(module.module_author for module in self.item.modules if module.module_author)
        return author_module.name, author_module.face

    @property
    def timestamp(self) -> int | None:
        """获取发布时间戳"""
        for module in self.item.modules:
            if module.module_type == "MODULE_TYPE_AUTHOR" and module.module_author:
                ts = module.module_author.pub_ts
                # 防止 fromtimestamp 崩溃
                try:
                    return int(ts)
                except (ValueError, TypeError):
                    return None
        return None

    def gen_text_img(self) -> Generator[TextNode | ImageNode, None, None]:
        """生成图文节点（保持顺序）"""
        for module in self.item.modules:
            if module.module_type == "MODULE_TYPE_TOP" and module.module_top:
                display = module.module_top.display
                if display and display.album:
                    for pic in display.album.pics:
                        yield ImageNode(url=pic.url)
            elif module.module_type == "MODULE_TYPE_CONTENT" and module.module_content:
                for paragraph in module.module_content.paragraphs:
                    # 处理文本段落
                    if paragraph.text and paragraph.text.nodes:
                        text_content = self._extract_text_from_nodes(paragraph.text.nodes)
                        text_content = text_content.strip()
                        if text_content:
                            yield TextNode(text="\n\n" + text_content)

                    # 处理图片段落
                    if paragraph.pic and paragraph.pic.pics:
                        for pic in paragraph.pic.pics:
                            yield ImageNode(url=pic.url)

    def _extract_text_from_nodes(self, nodes: list[dict[str, Any]]) -> str:
        """从节点列表中提取文本内容"""
        #解决 b站富文本问题
        text_content = ""
        for node in nodes:
            # 确保 node 确实是一个字典，嗯，应该是吧
            if not node or not isinstance(node, dict):
                continue

            # 提取普通文字 (type TEXT_NODE_TYPE_WORD，word 字段)
            word_dict = node.get("word")
            if isinstance(word_dict, dict) and word_dict.get("words"):
                text_content += str(word_dict["words"])
                continue
                
            # fix：修复专栏里富文本发送出去缺失的问题 (打 log发现b站放在 type TEXT_NODE_TYPE_RICH，在 rich字段内s)
            # 包含：#话题#、@用户、emoji 等
            rich_dict = node.get("rich")
            if isinstance(rich_dict, dict):
                # 直接拿 orig_text（带有 # 和 [] 的原文本），如果没有就拿 text 兜底
                rich_text = rich_dict.get("orig_text") or rich_dict.get("text")
                if rich_text:
                    text_content += str(rich_text)
                continue

            # 兜底格式直接存放在根节点的属性，并且兼容老格式，免得漏掉一些文本内容，鬼知道 b站后面会不会又改个新格式
            if node.get("orig_text"):
                text_content += str(node["orig_text"])
                continue
            if node.get("text"):
                text_content += str(node["text"])
                continue

        return text_content
