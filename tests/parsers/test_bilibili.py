import pytest
import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# 解决 ModuleNotFoundError
current_file = Path(__file__).resolve()
plugin_root = current_file.parent.parent.parent
plugins_dir = plugin_root.parent

if str(plugin_root) not in sys.path:
    sys.path.insert(0, str(plugin_root))
if str(plugins_dir) not in sys.path:
    sys.path.insert(0, str(plugins_dir))

try:
    from astrbot_plugin_parserURL.core.parsers.bilibili import BilibiliParser
    from astrbot_plugin_parserURL.core.parsers.base import ParseException
except ModuleNotFoundError:
    from core.parsers.bilibili import BilibiliParser
    from core.parsers.base import ParseException

@pytest.fixture
def mock_config(tmp_path):
    """fake配置，config.py"""
    config = MagicMock()

    # 使用 pytest 的 tmp_path 临时目录替代 MagicMock
    # 此时 __init__ 中的 mkdir, exists, open 全是安全的文件操作
    # 防止 MagicMock 可能被当成底层文件描述符(fd)导致 epoll 崩溃的
    config.data_dir = tmp_path

    # 模拟 parser 节点配置
    bili_cfg = MagicMock()
    bili_cfg.video_quality = "1080P"
    bili_cfg.video_codecs_list = ["HEV", "AV1", "AVC"]
    bili_cfg.sub_enable = False
    bili_cfg.sub_interval = 3
    bili_cfg.sub_delay = 5
    bili_cfg.platform_name = ["default"]
    bili_cfg.platform_botid = []
    bili_cfg.only_previewCard = False
    bili_cfg.ignore_lottery = False
    bili_cfg.sub_uids_users = ["114514-g666"] # 模拟一个订阅配置
    bili_cfg.use_proxy = False
    
    config.parser.bilibili = bili_cfg
    config.proxy = None
    config.common_timeout = 10
    config.download_retry_times = 3
    
    return config

@pytest.fixture
def bili_parser(mock_config):
    """实例化 BilibiliParser"""
    mock_downloader = MagicMock()
    
    # 拦截 create_task 和 all_tasks 避免初始化时触发 no running event loop
    with patch('asyncio.create_task'), \
         patch('asyncio.all_tasks', return_value=set()):
        parser = BilibiliParser(config=mock_config, downloader=mock_downloader)

    return parser

# 辅助函数逻辑测试 (Helper Logic Tests)
def test_codec_priority_list(bili_parser):
    """测试配置读取是否正确转换为大写"""
    assert bili_parser.codec_priority_list == ["HEV", "AV1", "AVC"]

@pytest.mark.asyncio
async def test_is_lottery_logic(bili_parser):
    """测试抽奖动态过滤逻辑"""
    assert await bili_parser._is_lottery("关注并转发，恭喜你中奖啦，快来私信领奖品") == True
    assert await bili_parser._is_lottery("先辈，给大家发个昏睡红茶座位小礼物") == False

@pytest.mark.asyncio
async def test_check_is_article(bili_parser):
    """测试专栏判断逻辑"""
    # 专栏
    article_data = {"item": {"basic": {"comment_type": 12}}}
    # 动态
    dynamic_data = {"item": {"basic": {"comment_type": 11}}}
    # other
    bad_data = {}

    assert await bili_parser._check_is_article(article_data) == True
    assert await bili_parser._check_is_article(dynamic_data) == False
    assert await bili_parser._check_is_article(bad_data) == False

# 路由匹配测试 (Regex Route Tests)
def test_routes_matching():
    """测试相关链接是否匹配到相关的 handler 关键字"""
    
    test_cases = [
        ("https://b23.tv/abcd123", "b23.tv"),
        ("https://bili2233.cn/xyz", "bili2233"),
        ("https://www.bilibili.com/video/BV1xx411c7mD?p=2", "/BV"),
        ("BV1xx411c7mD 2", "BV"),
        ("bmBV1xx411c7mD 2", "bm"),
        ("https://www.bilibili.com/video/av12345678", "/av"),
        ("av12345678", "av"),
        ("https://t.bilibili.com/114514", "t.bili"),
        ("https://www.bilibili.com/dynamic/114514", "/dynamic/"),
        ("https://live.bilibili.com/2233", "live.bili"),
        ("https://www.bilibili.com/read/cv123456", "/read/"),
        ("https://www.bilibili.com/opus/123456", "/opus/"),
        ("parserurl_test_push 88888", "parserurl_test_push"),
        ("find_uid_str_roomid 12345", "find_uid_str_roomid"),
    ]

    for url, expected_keyword in test_cases:
        matched_keyword, _ = BilibiliParser.search_url(url)
        assert matched_keyword == expected_keyword

def test_route_matching_fail():
    """非法其它 URL 应抛出 ParseException"""
    with pytest.raises(ParseException):
        BilibiliParser.search_url("https://acfun.com/12345")

# API Mock 测试 (API Mock Tests)
@pytest.mark.asyncio
async def test_extract_download_urls_success(bili_parser):
    """测试多编码降级抓取核心逻辑 (拦截 bilibili_api 返回数据)"""

    mock_dash_data = {
        "dash": {
            "video": [
                {"id": 80, "codecid": 7, "codecs": "avc1.640032", "base_url": "http://avc.mp4"},
                {"id": 80, "codecid": 12, "codecs": "hev1.1.6.L120.90", "base_url": "http://hev.mp4"},
                {"id": 64, "codecid": 12, "codecs": "hev1.1.6.L120.90", "base_url": "http://hev_720.mp4"},
            ],
            "audio": [
                {"id": 30280, "base_url": "http://audio_192k.mp3"}
            ]
        }
    }

    # 使用 patch 拦截掉 bilibili_api_video 的实例化和 get_download_url
    with patch('astrbot_plugin_parserURL.core.parsers.bilibili.Video') as MockVideo:
        mock_video_instance = AsyncMock()
        mock_video_instance.get_download_url.return_value = mock_dash_data
        MockVideo.return_value = mock_video_instance

        # 执行测试 (假设目标画质设为 1080P/60)
        # 不能直接改 Enum 的 value 属性，直接赋予一个带 value=60 属性的 MagicMock 对象即可
        bili_parser.video_quality = MagicMock(value=60)
        best_v, best_a = await bili_parser.extract_download_urls(bvid="BV1xx411c7mD")

        # 根据优先列表 ["HEV", "AV1", "AVC"]，应该选出 1080P(60) 里的 HEV 流
        assert best_v == "http://hev.mp4"
        assert best_a == "http://audio_192k.mp3"

@pytest.mark.asyncio
async def test_test_push_manual_no_target(bili_parser):
    """测试主动推送：无推送目标时应中断借宿"""
    bili_parser.sub_map = {} # 清空订阅配置
    match_obj = re.match(r"parserurl_test_push\s+(?P<dyid>\d+)", "parserurl_test_push 12345")

    result = await bili_parser._test_push_manual(match_obj)
    assert result.title == "测试中断"
    assert "未在配置中检测到任何有效的推送目标" in result.text