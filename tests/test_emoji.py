import asyncio
import shutil
import time
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image, ImageChops, ImageDraw

from core import emoji as emoji_module
from core.data import ParseResult, Platform
from core.emoji import EmojiBudget, EmojiSource, iter_text_units
from core.render import FontSet, Renderer


@pytest.fixture
def png():
    buffer = BytesIO()
    Image.new("RGBA", (64, 64), "red").save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def config(tmp_path):
    return SimpleNamespace(
        cache_dir=tmp_path,
        parser_emoji_cdn=["https://first.example/", "https://second.example"],
        parser_emoji_style="apple",
        parser_emoji_use_proxy=False,
        proxy="http://proxy.example:8080",
    )


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    options = []

    def client(**kwargs):
        options.append(kwargs.copy())
        kwargs.pop("proxy", None)
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(emoji_module, "AsyncClient", client)
    return options


@pytest.mark.asyncio
async def test_source_and_style_fallback_with_real_cache(monkeypatch, config, png):
    requests = []

    def handler(request):
        requests.append((request.url.host, request.url.params["style"]))
        assert request.url.path == "/🏠"
        assert b"%F0%9F%8F%A0" in request.url.raw_path
        if requests[-1] == ("second.example", "google"):
            return httpx.Response(200, content=png)
        return httpx.Response(404)

    options = mock_http(monkeypatch, handler)
    source = EmojiSource(config)
    images = await source.fetch_images({"🏠"}, EmojiBudget())
    assert images["🏠"].getpixel((0, 0)) == (255, 0, 0, 255)
    assert requests == [
        ("first.example", "apple"), ("second.example", "apple"),
        ("first.example", "facebook"), ("second.example", "facebook"),
        ("first.example", "google"), ("second.example", "google"),
    ]
    assert options[0]["proxy"] is None
    assert options[0]["trust_env"] is False
    assert (config.cache_dir / "emojis/google/🏠.png").exists()
    assert not (config.cache_dir / "emojis/apple/🏠.png").exists()

    # 空列表只复用已有替代样式缓存；全局清理后可重新下载。
    source.urls = []
    assert (await source.fetch_images({"🏠"}, EmojiBudget()))["🏠"] is not None
    assert len(requests) == 6
    shutil.rmtree(config.cache_dir / "emojis")
    assert (await source.fetch_images({"🏠"}, EmojiBudget()))["🏠"] is None
    source.urls = ["https://second.example"]
    assert (await source.fetch_images({"🏠"}, EmojiBudget()))["🏠"] is not None


@pytest.mark.asyncio
async def test_failed_source_is_skipped_and_can_recover(monkeypatch, config, png):
    requests = []

    def handler(request):
        requests.append(request.url.host)
        if request.url.host == "first.example":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, content=png)

    config.parser_emoji_use_proxy = True
    options = mock_http(monkeypatch, handler)
    source = EmojiSource(config)
    images = await source.fetch_images({"🏠", "😀"}, EmojiBudget())
    assert all(images.values())
    assert requests.count("first.example") == 1
    assert options[0]["proxy"] == config.proxy
    source._unavailable_until["https://first.example"] = 0
    await source.fetch_images({"👍"}, EmojiBudget())
    assert requests.count("first.example") == 2


@pytest.mark.asyncio
async def test_corrupt_images_and_all_styles_missing(monkeypatch, config):
    requests = []

    def handler(request):
        requests.append((request.url.host, request.url.params["style"]))
        return httpx.Response(200, content=b"<html>unavailable</html>")

    mock_http(monkeypatch, handler)
    source = EmojiSource(config)
    path = config.cache_dir / "emojis/apple/🏠.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"broken cache")
    assert (await source.fetch_images({"🏠"}, EmojiBudget()))["🏠"] is None
    assert [style for host, style in requests if host == "first.example"] == [
        "apple", "facebook", "google", "twitter"
    ]
    assert not path.exists()


@pytest.mark.asyncio
async def test_concurrent_cards_download_once(monkeypatch, config, png):
    requests = []

    async def handler(request):
        requests.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, content=png)

    mock_http(monkeypatch, handler)
    source = EmojiSource(config)
    results = await asyncio.gather(
        source.fetch_images({"🏠"}, EmojiBudget()),
        source.fetch_images({"🏠"}, EmojiBudget()),
    )
    assert all(result["🏠"] is not None for result in results)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_render_budget_stops_pending_requests(monkeypatch, config):
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.Event().wait()

    mock_http(monkeypatch, handler)
    source = EmojiSource(config)
    budget = EmojiBudget(time.monotonic() + 0.05)
    assert (await source.fetch_images({"🏠"}, budget))["🏠"] is None
    assert started.is_set()
    # 同一卡片预算耗尽，后续文字区不能开启新请求。
    started.clear()
    assert (await source.fetch_images({"😀"}, budget))["😀"] is None
    assert not started.is_set()


@pytest.mark.parametrize("value", ["👍🏻", "👨‍👩‍👧‍👦", "🇨🇳", "1️⃣", "❤️"])
def test_sequences_wrap_as_one_unit(value):
    renderer = object.__new__(Renderer)
    font = FontSet.new(Renderer.DEFAULT_FONT_PATH).text_font
    text = "中" + value + "文"
    assert list(iter_text_units(value)) == [(value, True)]
    assert renderer._wrap_text(text, font.font.size, font) == ["中", value, "文"]
    assert font.get_text_width(value) == font.font.size
    assert "".join(renderer._wrap_text(text, font.font.size, font)) == text


def test_missing_emoji_keeps_text_position_without_tofu():
    font = FontSet.new(Renderer.DEFAULT_FONT_PATH).text_font
    units = [list(iter_text_units("🏠中"))]
    blank = Image.new("RGB", (100, 60), "white")
    failed = blank.copy()
    success = blank.copy()
    Renderer._draw_text_lines(failed, (0, 0), units, font, (0, 0, 0), {"🏠": None})
    Renderer._draw_text_lines(
        success, (0, 0), units, font, (0, 0, 0),
        {"🏠": Image.new("RGBA", (64, 64), "red")},
    )
    emoji_area = (0, 0, 24, 60)
    text_area = (24, 0, 100, 60)
    assert ImageChops.difference(
        failed.crop(emoji_area), blank.crop(emoji_area)
    ).getbbox() is None
    assert ImageChops.difference(
        failed.crop(text_area), success.crop(text_area)
    ).getbbox() is None
    assert ImageChops.difference(failed, blank).getbbox() is not None


def test_platform_emoticons_are_plain_text():
    text = "你好[b站_表情1]"
    assert all(not is_emoji for _, is_emoji in iter_text_units(text))


@pytest.mark.asyncio
async def test_legacy_cache_and_concurrent_cleanup(monkeypatch, config, png):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=png)

    mock_http(monkeypatch, handler)
    legacy = config.cache_dir / "emojis/APPLE/🏠.png"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(png)
    source = EmojiSource(config)
    assert (await source.fetch_images({"🏠"}, EmojiBudget()))["🏠"] is not None
    assert not requests

    # 模拟全局清理恰好删除了已写入、尚未原子替换的临时文件。
    replace = emoji_module.Path.replace

    def cleaned_replace(path, target):
        shutil.rmtree(config.cache_dir / "emojis")
        return replace(path, target)

    monkeypatch.setattr(emoji_module.Path, "replace", cleaned_replace)
    assert (await source.fetch_images({"😀"}, EmojiBudget()))["😀"] is not None


def test_plain_text_keeps_existing_font_drawing():
    font = FontSet.new(Renderer.DEFAULT_FONT_PATH).text_font
    text = "正常文字 Hello, AstrBot! [b站_表情1]"
    actual = Image.new("RGB", (750, 80), "white")
    expected = actual.copy()
    ImageDraw.Draw(expected).text((25, 10), text, font=font.font, fill=(51, 51, 51))
    Renderer._draw_text_lines(
        actual, (25, 10), [list(iter_text_units(text))], font, (51, 51, 51), {}
    )
    assert ImageChops.difference(actual, expected).getbbox() is None


def test_latin_text_does_not_accumulate_width_rounding_before_emoji():
    font = FontSet.new(Renderer.DEFAULT_FONT_PATH).text_font
    text = "Hello, AstrBot! " * 3
    assert font.get_text_width(text) == pytest.approx(font.font.getlength(text))
    image = Image.new("RGB", (750, 60), "white")
    Renderer._draw_text_lines(
        image, (0, 0), [list(iter_text_units(text + "🏠"))], font, (0, 0, 0),
        {"🏠": Image.new("RGBA", (64, 64), "red")},
    )
    red_pixels = [
        x for x in range(image.width)
        if image.getpixel((x, font.line_height // 2)) == (255, 0, 0)
    ]
    assert min(red_pixels) == round(font.font.getlength(text) + 1)


@pytest.mark.asyncio
async def test_card_and_repost_share_emoji_budget(monkeypatch, config, png):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=png)

    mock_http(monkeypatch, handler)
    renderer = Renderer(config)
    renderer.load_resources()
    result = ParseResult(
        platform=Platform(name="bilibili", display_name="B站"),
        title="标题🏠", text="正文1️⃣", repost=ParseResult(
            platform=Platform(name="bilibili", display_name="B站"), text="转发👍🏻",
        ),
    )
    budget = EmojiBudget()
    image = await renderer._create_card_image(result, emoji_budget=budget)
    assert image.width == renderer.DEFAULT_CARD_WIDTH
    assert budget.deadline is not None
    assert len(requests) == 3
