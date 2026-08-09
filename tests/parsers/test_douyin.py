"""抖音解析器测试模板。

网络交互均由 Mock 替代；补充真实抓包数据时可在此文件继续扩展用例。
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# 解决以插件包或仓库根目录运行 pytest 时的导入差异。
current_file = Path(__file__).resolve()
plugin_root = current_file.parent.parent.parent
plugins_dir = plugin_root.parent

if str(plugin_root) not in sys.path:
    sys.path.insert(0, str(plugin_root))
if str(plugins_dir) not in sys.path:
    sys.path.insert(0, str(plugins_dir))

try:
    from astrbot_plugin_parserURL.core.exception import ParseException
    from astrbot_plugin_parserURL.core.parsers.douyin import DouyinParser
except ModuleNotFoundError:
    from core.exception import ParseException
    from core.parsers.douyin import DouyinParser


@pytest.fixture
def mock_config(tmp_path):
    """提供不含真实 Cookie、代理或文件副作用的配置。"""
    config = MagicMock()
    config.data_dir = tmp_path

    douyin_cfg = MagicMock()
    douyin_cfg.cookies = "fake_douyin_cookie=123"
    douyin_cfg.use_proxy = False
    config.parser.douyin = douyin_cfg
    config.proxy = None
    config.common_timeout = 10
    config.download_retry_times = 3
    return config


@pytest.fixture
def douyin_parser(mock_config):
    """实例化解析器，并以 Mock Session 阻断所有真实 HTTP 请求。"""
    downloader = MagicMock()
    with patch(f"{DouyinParser.__module__}.CookieJar") as mock_cookie_jar:
        cookie_jar = MagicMock()
        cookie_jar.cookies_str = "fake_douyin_cookie=123"
        cookie_jar.get_cookie_header_for_url.return_value = "fake_douyin_cookie=123"
        cookie_jar.get.return_value = {"ttwid": "fake-ttwid"}
        mock_cookie_jar.return_value = cookie_jar
        parser = DouyinParser(config=mock_config, downloader=downloader)

    session = MagicMock()
    session.closed = False
    parser._session = session
    return parser


def test_routes_matching():
    """测试短链、视频、图文和直播链接能命中预期处理器。"""
    test_cases = [
        ("https://v.douyin.com/abcDEF12/", "v.douyin"),
        ("https://www.douyin.com/video/7521023890996514083", "douyin"),
        ("https://www.douyin.com/note/7469411074119322899", "douyin"),
        ("https://live.douyin.com/123456789", "live.douyin"),
        (
            "https://webcast.amemv.com/douyin/webcast/reflow/1234567890123456789",
            "webcast",
        ),
    ]

    for url, expected_keyword in test_cases:
        matched_keyword, _ = DouyinParser.search_url(url)
        assert matched_keyword == expected_keyword


def test_route_matching_fail():
    with pytest.raises(ParseException):
        DouyinParser.search_url("https://www.example.com/video/123")


def test_helper_functions(douyin_parser):
    """测试无网络依赖的 URL、请求头和文件大小处理逻辑。"""
    assert douyin_parser._build_iesdouyin_url("video", "123") == (
        "https://www.iesdouyin.com/share/video/123/"
    )
    assert douyin_parser._is_iesdouyin_url("https://www.iesdouyin.com/share/video/123/")
    assert not douyin_parser._is_iesdouyin_url("https://www.douyin.com/video/123")
    assert douyin_parser._extract_response_size({"Content-Range": "bytes 0-1/2048"}) == 2048
    assert douyin_parser._extract_response_size({"Content-Length": "512"}) == 512
    assert douyin_parser._extract_response_size({"Content-Length": "invalid"}) == 0


@pytest.mark.asyncio
async def test_parse_video_success(douyin_parser):
    """Mock 分享页中的 _ROUTER_DATA，验证视频信息和下载任务被正确构建。"""
    fake_url = "https://www.iesdouyin.com/share/video/7521023890996514083/"
    router_data = {
        "loaderData": {
            "video_(id)/page": {
                "videoInfoRes": {
                    "item_list": [
                        {
                            "create_time": 1_700_000_000,
                            "author": {
                                "nickname": "AstrBot_Douyin",
                                "avatar_thumb": {"url_list": ["https://example.com/avatar.jpg"]},
                            },
                            "desc": "抖音测试视频",
                            "video": {
                                "play_addr": {
                                    "uri": "play-token",
                                    "url_list": ["https://example.com/playwm/video.mp4"],
                                },
                                "cover": {"url_list": ["https://example.com/cover.jpg"]},
                                "duration": 12,
                            },
                        }
                    ]
                }
            }
        }
    }
    response = AsyncMock()
    response.status = 200
    response.text.return_value = (
        f"<script>window._ROUTER_DATA = {json.dumps(router_data)}</script>"
    )
    response.headers.getall.return_value = []
    douyin_parser._session.get.return_value.__aenter__.return_value = response

    with patch.object(douyin_parser, "ensure_ttwid", new_callable=AsyncMock), patch.object(
        douyin_parser,
        "probe_video_url",
        new_callable=AsyncMock,
        side_effect=ParseException("probe unavailable"),
    ):
        result = await douyin_parser.parse_video(fake_url)

    assert result.title == "抖音测试视频"
    assert result.author is not None
    assert result.author.name == "AstrBot_Douyin"
    assert result.timestamp == 1_700_000_000
    assert len(result.contents) == 1
    douyin_parser.downloader.download_video.assert_called_once()
    assert douyin_parser.downloader.download_video.call_args.args[0] == (
        "https://example.com/play/video.mp4"
    )
