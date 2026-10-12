import pytest
import asyncio
import re
import sys
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

current_file = Path(__file__).resolve()
plugin_root = current_file.parent.parent.parent
plugins_dir = plugin_root.parent

if str(plugin_root) not in sys.path:
    sys.path.insert(0, str(plugin_root))
if str(plugins_dir) not in sys.path:
    sys.path.insert(0, str(plugins_dir))

try:
    from astrbot_plugin_parserurl.core.parsers.xhs import XHSParser, Video, Media, Stream
    from astrbot_plugin_parserurl.core.parsers.base import ParseException
except ModuleNotFoundError:
    from core.parsers.xhs import XHSParser, Video, Media, Stream
    from core.parsers.base import ParseException

@pytest.fixture
def mock_config(tmp_path):
    """伪造 PluginConfig"""
    config = MagicMock()
    config.data_dir = tmp_path
    
    # 模拟 xhs 配置
    xhs_cfg = MagicMock()
    xhs_cfg.cookies = "fake_xhs_cookie=123;"
    xhs_cfg.use_proxy = False
    
    config.parser.xhs = xhs_cfg
    config.proxy = None
    config.common_timeout = 10
    config.download_retry_times = 3
    
    return config

@pytest.fixture
def xhs_parser(mock_config):
    """实例化XHSParser"""
    mock_downloader = MagicMock()
    # 避免 CookieJar 初始化报错或进行 IO
    with patch('astrbot_plugin_parserurl.core.parsers.xhs.CookieJar') as MockCookieJar:
        mock_jar_instance = MagicMock()
        mock_jar_instance.cookies_str = "fake_cookie=123"
        MockCookieJar.return_value = mock_jar_instance
        
        parser = XHSParser(config=mock_config, downloader=mock_downloader)
    
    # 替换掉 parser.session，避免实际网路请求
    mock_session = MagicMock()
    # 因为MagicMock 的布尔值默认是 True，会导致去新建一个真实的 aiohttp Session
    mock_session.closed = False
    parser._session = mock_session
    return parser

# 逻辑工具测试
def test_extract_initial_state_json_success(xhs_parser):
    """测试从 HTML 中提取 window.__INITIAL_STATE__ 的 JSON"""
    fake_json = {"note": "test_data"}
    fake_html = f"<html><body><script>window.__INITIAL_STATE__={json.dumps(fake_json)}</script></body></html>"
    
    result = xhs_parser._extract_initial_state_json(fake_html)
    assert result == fake_json

def test_extract_initial_state_json_fail(xhs_parser):
    """测试提取失败时是否抛出异常"""
    bad_html = "<html><body><script>var a = 1;</script></body></html>"
    with pytest.raises(ParseException, match="小红书分享链接失效或内容已删除"):
        xhs_parser._extract_initial_state_json(bad_html)

def test_video_stream_priority():
    """测试视频流的降级选取逻辑 (h265 > h264 > av1 > h266)"""
    # 模拟一个同时拥有 h264 和 h265 情况
    stream = Stream(
        h264=[{"masterUrl": "http://xhs.com/video_h264.mp4"}],
        h265=[{"masterUrl": "http://xhs.com/video_h265.mp4"}]
    )
    video = Video(media=Media(stream=stream))
    # 预期：优先选取 h265 无水印视频
    assert video.video_url == "http://xhs.com/video_h265.mp4"

# 匹配测试
def test_routes_matching():
    """测试小红书相关链接正则能否正确命中"""
    test_cases = [
        ("http://xhslink.com/A1b2C3d", "xhslink.com"),
        ("https://xhslink.com/A1b2C3d", "xhslink.com"),
        ("https://www.xiaohongshu.com/explore/64a1b2c30000000027011111?xsec_token=123", "xiaohongshu.com"),
        ("https://www.xiaohongshu.com/discovery/item/64a1b2c30000000027011111?app_platform=and", "xiaohongshu.com"),
    ]

    for url, expected_keyword in test_cases:
        matched_keyword, _ = XHSParser.search_url(url)
        assert matched_keyword == expected_keyword

# 核心 API Mock 测试
@pytest.mark.asyncio
async def test_parse_explore_video_success(xhs_parser):
    """测试解析 explore 视频帖子的核心逻辑"""

    xhs_id = "test_note_123"
    fake_url = f"https://www.xiaohongshu.com/explore/{xhs_id}"
    
    # 伪造小红书 __INITIAL_STATE__ 返回的数据结构
    fake_state = {
        "note": {
            "noteDetailMap": {
                xhs_id: {
                    "note": {
                        "type": "video",
                        "title": "小红书标题",
                        "desc": "测试文案",
                        "user": {"nickname": "AstrBot_XHS", "avatar": "http://avatar.jpg"},
                        "imageList": [{"urlDefault": "http://cover.jpg"}],
                        "video": {
                            "media": {
                                "stream": {
                                    "h265": [{"masterUrl": "http://video_h265.mp4"}]
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    # 构造假的 HTML 返回
    fake_html = f"<script>window.__INITIAL_STATE__={json.dumps(fake_state)}</script>"

    # 设置 mock_session.get 的返回值
    mock_resp = AsyncMock()
    mock_resp.text.return_value = fake_html
    mock_resp.url = fake_url
    mock_resp.status = 200
    xhs_parser._session.get.return_value.__aenter__.return_value = mock_resp

    # 执行解析
    result = await xhs_parser.parse_explore(fake_url, xhs_id)

    # 验证
    assert result.title == "小红书标题"
    assert result.text == "测试文案"
    assert result.author.name == "AstrBot_XHS"

    # 验证是否成功创建了 VideoContent，内有 1 元素
    assert len(result.contents) == 1
    xhs_parser.downloader.download_video.assert_called_once_with(
        "http://video_h265.mp4",
        headers=xhs_parser.headers,
        proxy=None
    )
