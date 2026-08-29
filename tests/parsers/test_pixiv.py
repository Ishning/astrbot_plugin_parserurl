from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.parsers.pixiv import PixivParser
from core.parsers.pixiv.nsfw import _blur_radius, create_blurred_cover, create_body_pdf
from core.data import FileContent, ImageContent


@pytest.mark.asyncio
async def test_ranking_retry_marks_sent_after_success(monkeypatch):
    parser = object.__new__(PixivParser)
    parser._state = {"ranking_sent": {}, "works_sent": {}}
    parser._save_state = MagicMock(side_effect=lambda: asyncio.sleep(0))
    calls = 0

    async def send(*args):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError("network")

    parser._send_proactive = send
    async def no_sleep(*_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    await parser._send_ranking_with_retry(object(), [], [], "k")
    assert calls == 3
    assert "k" in parser._state["ranking_sent"]


@pytest.mark.asyncio
async def test_ranking_retry_marks_sent_after_three_failures(monkeypatch):
    parser = object.__new__(PixivParser)
    parser._state = {"ranking_sent": {}, "works_sent": {}}
    parser._save_state = MagicMock(side_effect=lambda: asyncio.sleep(0))

    async def send(*args):
        raise OSError("network")

    parser._send_proactive = send
    async def no_sleep(*_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    await parser._send_ranking_with_retry(object(), [], [], "failed")
    assert "failed" in parser._state["ranking_sent"]


@pytest.mark.asyncio
async def test_ugoira_gif_size_limit(tmp_path):
    import zipfile
    from PIL import Image
    from core.parsers.pixiv.nsfw import create_ugoira_gif

    source_dir = tmp_path / "frames"
    source_dir.mkdir()
    for index, color in enumerate(((255, 0, 0), (0, 255, 0))):
        Image.new("RGB", (8, 8), color).save(source_dir / f"frame{index}.jpg")
    archive = tmp_path / "ugoira.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.write(source_dir / "frame0.jpg", "frame0.jpg")
        zipped.write(source_dir / "frame1.jpg", "frame1.jpg")
    output = await create_ugoira_gif(archive, tmp_path / "out", [{"file": "frame0.jpg", "delay": 100}, {"file": "frame1.jpg", "delay": 100}])
    assert output.exists()
    assert output.suffix == ".gif"


def test_pixiv_artwork_routes():
    for url in (
        "https://www.pixiv.net/artworks/12345",
        "https://www.pixiv.net/artworks/12345?utm_source=share",
        "https://pixiv.net/artworks/12345/",
    ):
        keyword, matched = PixivParser.search_url(url)
        assert keyword == "pixiv.net/artworks/"
        assert matched.group("pid") == "12345"

    with pytest.raises(Exception):
        PixivParser.search_url("https://www.pixiv.net/users/12345")

    for url in (
        "https://www.pixiv.net/novel/show.php?id=67890",
        "https://www.pixiv.net/novel/show.php?foo=1&id=67890",
        "https://www.pixiv.net/novel/67890",
    ):
        keyword, matched = PixivParser.search_url(url)
        assert keyword in {"pixiv.net/novel/show", "pixiv.net/novel/"}
        assert matched.group("nid") == "67890"

    keyword, matched = PixivParser.search_url("https://www.pixiv.net/novel/series/16281413")
    assert keyword == "pixiv.net/novel/series/"
    assert matched.group("series_id") == "16281413"


def test_image_urls_and_page_limit():
    body = {
        "urls": {"original": "https://i.pximg.net/one.jpg"},
        "metaPages": [
            {"image_urls": {"original": "https://i.pximg.net/one.jpg"}},
            {"image_urls": {"original": "https://i.pximg.net/two.jpg"}},
        ],
    }
    assert PixivParser._image_urls(body) == [
        "https://i.pximg.net/one.jpg",
        "https://i.pximg.net/two.jpg",
    ]
    assert PixivParser._page_limit(99) == 20
    assert PixivParser._page_limit(0) == 1
    assert PixivParser._blur_strength(0) == 30
    assert PixivParser._blur_strength(101) == 100


def test_clean_comment_preserves_html_line_breaks():
    comment = "<p>第一段<br />第二行</p><div>第二段 &amp; 链接</div>"
    assert PixivParser._clean_comment(comment) == "第一段\n第二行\n\n第二段 & 链接"


def test_novel_tags_follow_artwork_tag_priority():
    assert PixivParser._tags(
        {"tags": {"tags": [{"tag": "原标签", "translated_name": "翻译标签"}, {"tag": "仅原标签"}]}}
    ) == ["翻译标签", "仅原标签"]


@pytest.mark.asyncio
async def test_fetch_user_avatar_uses_user_endpoint():
    parser = object.__new__(PixivParser)
    parser.web_headers = {}
    parser._session = MagicMock(closed=False)

    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def json(self, **kwargs):
            return {"error": False, "body": {"imageBig": "https://i.pximg.net/user.png"}}

    parser._session.get.return_value = Response()
    parser.cfg = SimpleNamespace(proxy=None)
    assert await parser._fetch_user_avatar("21862577") == "https://i.pximg.net/user.png"


@pytest.mark.asyncio
async def test_novel_series_text_includes_total_and_link():
    parser = object.__new__(PixivParser)
    parser.web_headers = {}
    parser.cfg = SimpleNamespace(proxy=None)
    parser._session = MagicMock(closed=False)

    class Response:
        status = 200

        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None

        async def json(self, **kwargs):
            return {"error": False, "body": {"total": 12}}

    parser._session.get.return_value = Response()
    text = await parser._novel_series_text(
        {"seriesNavData": {"seriesId": "99", "order": 3, "title": "系列名"}},
        "7",
    )
    assert text == "小说系列：系列名\n当前第 3 话，共 12 话 · https://www.pixiv.net/user/7/series/99"


@pytest.mark.asyncio
async def test_parse_novel_series_builds_result():
    parser = object.__new__(PixivParser)
    parser._fetch_novel_series_detail = lambda series_id: __import__("asyncio").sleep(
        0,
        result={
            "title": "系列标题",
            "description": "系列说明",
            "tags": {"tags": [{"tag": "标签"}]},
            "total": 12,
            "novels": [{"id": "9", "title": "第一章"}],
        },
    )
    result = await parser._parse_novel_series(
        PixivParser.search_url("https://www.pixiv.net/novel/series/16281413")[1]
    )
    assert result.title == "系列标题"
    assert "系列说明" in (result.text or "")
    assert "#标签" in (result.text or "")
    assert "总共 12 话" in (result.text or "")
    assert "第一章" in (result.text or "")
    assert result.url == "https://www.pixiv.net/novel/series/16281413"


@pytest.mark.asyncio
async def test_parse_artwork_builds_result_with_truncation():
    cfg = MagicMock()
    cfg.data_dir = "."
    cfg.proxy = None
    cfg.common_timeout = 10
    cfg.download_retry_times = 1
    cfg.parser.pixiv = SimpleNamespace(
        cookies="cookie=1", use_proxy=False, max_manga_pages=1, nsfw_mode="normal"
    )
    downloader = MagicMock()
    downloader.download_img.side_effect = lambda url, **kwargs: url
    parser = PixivParser(cfg, downloader)

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self, **kwargs):
            return {
                "error": False,
                "body": {
                    "illustTitle": "测试作品",
                    "illustComment": "简介",
                    "userName": "作者",
                    "userId": "7",
                    "profileImageUrl": "https://i.pximg.net/avatar.jpg",
                    "tags": {"authorId": "7", "tags": [{"tag": "tag1"}]},
                    "viewCount": 1234,
                    "likeCount": 56,
                    "bookmarkCount": 78,
                    "commentCount": 9,
                    "metaPages": [
                        {"image_urls": {"original": "https://i.pximg.net/1.jpg"}},
                        {"image_urls": {"original": "https://i.pximg.net/2.jpg"}},
                    ],
                },
            }

    class UserResponse(Response):
        async def json(self, **kwargs):
            return {"error": False, "body": {"imageBig": "https://i.pximg.net/avatar.jpg"}}

    parser._session = MagicMock(closed=False)
    parser._session.get.side_effect = [Response(), UserResponse()]
    result = await parser.parse(*PixivParser.search_url("https://www.pixiv.net/artworks/8"))
    assert result.title == "测试作品"
    assert result.url == "https://www.pixiv.net/artworks/8"
    assert "#tag1" in (result.text or "")
    assert "图片数量过多" in (result.text or "")
    assert result.extra["info"] == "浏览 1,234 · 点赞 56 · 收藏 78 · 评论 9"
    assert len(result.contents) == 1
    assert isinstance(result.contents[0], ImageContent)
    assert result.author is not None
    assert result.author.avatar is not None
    downloader.download_img.assert_any_call(
        "https://i.pximg.net/avatar.jpg",
        headers=parser.image_headers,
        proxy=parser.proxy,
    )


@pytest.mark.asyncio
async def test_parse_novel_builds_text_result_and_cover():
    cfg = MagicMock()
    cfg.data_dir = "."
    cfg.cache_dir = __import__("pathlib").Path("/tmp")
    cfg.proxy = None
    cfg.common_timeout = 10
    cfg.parser.pixiv = SimpleNamespace(
        cookies="cookie=1", use_proxy=False, nsfw_mode="normal", nsfw_blur_strength=70
    )
    downloader = MagicMock()
    parser = PixivParser(cfg, downloader)

    class Response:
        status = 200

        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None

        async def json(self, **kwargs):
            return {
                "error": False,
                "body": {
                    "title": "小说标题",
                    "content": "第一段[newpage]第二段 [[jumpuri:链接>https://example.com]]",
                    "description": "简介",
                    "tags": {"tags": [{"tag": "tag1"}]},
                    "userName": "作者",
                    "userId": "7",
                    "coverUrl": "https://i.pximg.net/cover.jpg",
                    "characterCount": 1234,
                    "createDate": "2026-01-01T00:00:00+00:00",
                },
            }

    class UserResponse(Response):
        async def json(self, **kwargs):
            return {"error": False, "body": {"imageBig": "https://i.pximg.net/avatar.jpg"}}

    parser._session = MagicMock(closed=False)
    parser._session.get.side_effect = [Response(), UserResponse()]
    result = await parser.parse(*PixivParser.search_url("https://www.pixiv.net/novel/show.php?id=9"))
    assert result.title == "小说标题"
    assert result.url == "https://www.pixiv.net/novel/show.php?id=9"
    assert "简介" in (result.text or "")
    assert "#tag1" in (result.text or "")
    assert (result.text or "").startswith("---\n\n")
    assert "#tag1\n\n---\n\n第一段" in (result.text or "")
    assert "---" in (result.text or "")
    assert "链接" in (result.text or "")
    assert result.extra["info"] == "字数 1,234"
    assert len(result.contents) == 1
    assert isinstance(result.contents[0], ImageContent)


@pytest.mark.asyncio
async def test_nsfw_blur_cover_and_pdf(tmp_path):
    from PIL import Image

    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (32, 32), "red").save(first)
    Image.new("RGB", (32, 32), "blue").save(second)

    cover = await create_blurred_cover(first, tmp_path / "output")
    pdf = await create_body_pdf([second], tmp_path / "output", max_size_mb=1)

    assert cover.suffix == ".jpg" and cover.is_file()
    assert pdf.suffix == ".pdf" and pdf.is_file()
    assert _blur_radius(30) < _blur_radius(100)


@pytest.mark.asyncio
async def test_blur_cover_pdf_mode_creates_blurred_cover_and_file(tmp_path):
    parser = object.__new__(PixivParser)
    parser.cfg = SimpleNamespace(
        cache_dir=tmp_path,
        source_max_size=1,
        proxy=None,
        parser=SimpleNamespace(pixiv=SimpleNamespace(use_proxy=False)),
    )
    parser.image_headers = {}

    async def pending_download(*args, **kwargs):
        await __import__("asyncio").Event().wait()

    parser.downloader = MagicMock()
    parser.downloader.download_img.side_effect = pending_download
    contents = parser._create_blur_cover_pdf_contents(
        ["https://i.pximg.net/cover.jpg", "https://i.pximg.net/page.jpg"]
    )

    assert isinstance(contents[0], ImageContent)
    assert isinstance(contents[1], FileContent)
    for content in contents:
        content.path_task.cancel()
