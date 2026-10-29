from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from core.data import GraphicsContent, ParseResult, Platform
from core.render import (
    ExtraSectionData,
    FontSet,
    GraphicsSectionData,
    HeaderSectionData,
    Renderer,
    TextSectionData,
    TitleSectionData,
)


@pytest.fixture
def renderer():
    renderer = object.__new__(Renderer)
    renderer.fontset = FontSet.new(renderer.DEFAULT_FONT_PATH)
    renderer._calculate_header_section = AsyncMock(
        return_value=HeaderSectionData(
            height=80, avatar=None, name_lines=["番剧/电影"],
            time_lines=["2026-10-06"], text_height=60,
        )
    )
    renderer._load_and_resize_cover = AsyncMock(return_value=None)
    renderer._calculate_graphics_section = AsyncMock(
        return_value=GraphicsSectionData(
            height=100, text_lines=[], image=Image.new("RGB", (100, 100)),
        )
    )
    return renderer


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", [None, "bangumi", "unknown"])
@pytest.mark.parametrize("has_cover", [False, True])
async def test_card_layout_and_title_font(renderer, tmp_path, layout, has_cover):
    result = ParseResult(
        platform=Platform(name="bilibili", display_name="B站"),
        title="长标题需要正确换行" * 10,
        text="测试简介",
        contents=[GraphicsContent(tmp_path / "cover.png")] if has_cover else [],
        extra={"info": "评分：9.2"},
    )
    if layout is not None:
        result.extra["layout"] = layout

    sections = await renderer._calculate_sections(result, 750)
    expected = [HeaderSectionData]
    if has_cover and layout == "bangumi":
        expected.append(GraphicsSectionData)
    expected.append(TitleSectionData)
    if has_cover and layout != "bangumi":
        expected.append(GraphicsSectionData)
    expected.extend([TextSectionData, ExtraSectionData])
    assert [type(section) for section in sections] == expected

    title = next(section for section in sections if isinstance(section, TitleSectionData))
    expected_font = (
        renderer.fontset.large_title_font if layout == "bangumi"
        else renderer.fontset.title_font
    )
    assert title.font is expected_font
    assert title.font.font.size == (36 if layout == "bangumi" else 30)
    assert len(title.lines) > 1
    assert title.height == len(title.lines) * expected_font.line_height

    # 实际绘制使用与测量相同的字体，避免放大标题后重叠或截断
    renderer.text = AsyncMock(return_value=title.height)
    ctx = SimpleNamespace(y_pos=100)
    await renderer._draw_title(ctx, title.lines, title.font)
    assert renderer.text.await_args.args[3] is expected_font
    assert ctx.y_pos == 100 + title.height + renderer.SECTION_SPACING
