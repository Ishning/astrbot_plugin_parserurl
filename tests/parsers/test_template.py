import pytest
import asyncio
import re
import sys
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# 该 test_template.py 是一个测试模板
# 用于为新的 Parser 提供一个清晰的测试框架
# 请根据注释中的指示替换相关部分，以适配你要测试的具体 Parser 和平台
# ==========================================
# 0. 环境初始化 (解决 ModuleNotFoundError)
# ==========================================
current_file = Path(__file__).resolve()
plugin_root = current_file.parent.parent.parent
plugins_dir = plugin_root.parent

if str(plugin_root) not in sys.path:
    sys.path.insert(0, str(plugin_root))
if str(plugins_dir) not in sys.path:
    sys.path.insert(0, str(plugins_dir))

# 1: 替换为你要测试的实际 Parser，type: ignore 可去除
try:
    from astrbot_plugin_parserurl.core.parsers.YOUR_PLATFORM import YourPlatformParser # type: ignore
    from astrbot_plugin_parserurl.core.parsers.base import ParseException # type: ignore
except ModuleNotFoundError:
    from core.parsers.YOUR_PLATFORM import YourPlatformParser # type: ignore
    from core.parsers.base import ParseException # type: ignore

# ==========================================
# 核心 Fixtures (提供极其干净的测试环境)
# ===========================================

@pytest.fixture
def mock_config(tmp_path):
    """
    提供干净配置和 tmp_path (自动清理的沙盒目录)
    暂不使用 MagicMock 路径，有可能遇到 aiohttp 崩溃问题，后续再改进
    """
    config = MagicMock()
    config.data_dir = tmp_path

    # 2: 模拟该平台的独立配置节点
    platform_cfg = MagicMock()
    platform_cfg.cookies = "fake_cookie_123"
    platform_cfg.use_proxy = False

    # 挂载配置
    # config.parser.your_platform = platform_cfg
    config.proxy = None
    config.common_timeout = 10
    config.download_retry_times = 3

    return config

@pytest.fixture
def target_parser(mock_config):
    """实例化要测试的 Parser，并进行防御性拦截"""
    mock_downloader = MagicMock()

    # 拦截后台可能存在的轮询任务和 CookieJar，防止产生真实 IO
    with patch('asyncio.create_task'), \
         patch('asyncio.all_tasks', return_value=set()), \
         patch('astrbot_plugin_parserurl.core.parsers.YOUR_PLATFORM.CookieJar') as MockCookieJar: # 3: 修改这里的拦截路径

        # 赋予假 Cookie 字符串
        mock_jar_instance = MagicMock()
        mock_jar_instance.cookies_str = "fake=123"
        MockCookieJar.return_value = mock_jar_instance

        # 4: 实例化真实 Parser
        parser = YourPlatformParser(config=mock_config, downloader=mock_downloader)

    # --- aiohttp 纯手写爬虫平台专属拦截 ---
    # 替换内部的 aiohttp.ClientSession 避免有实际网络请求
    mock_session = MagicMock()
    mock_session.closed = False  # 设 False，若 Ture 基类可能会使用重建的真实 Session
    parser._session = mock_session

    return parser

# ==========================================
# 2. 路由匹配测试 (Regex Route Tests)
# ==========================================

def test_routes_matching():
    """测试长短链接正则能否正确命中 (性价比最高的测试)"""
    
    # 5: 填入该平台各种相关的分享链接，以及你期望命中的 @handle 关键字
    test_cases = [
        ("https://short.url/abcd", "short.url"),
        ("https://www.long_domain.com/v/12345", "long_domain.com"),
    ]

    for url, expected_keyword in test_cases:
        # 6: 替换类名
        matched_keyword, _ = YourPlatformParser.search_url(url)
        assert matched_keyword == expected_keyword

# ==========================================
# 3. 纯逻辑工具测试 (Helper Logic Tests)
# ==========================================

def test_custom_helper_functions(target_parser):
    """测试该平台独有的工具函数，比如单独发送，时间转换，清理清晰等"""
    
    # 7: 编写纯逻辑函数的测试
    # 假设有个清理文本的方法
    # raw_text = "哇喔这么说你很勇哦 #话题 @用户"
    # clean_text = target_parser._clean_text(raw_text)
    # assert clean_text == "哇喔这么说你很勇哦"
    pass

# ==========================================
# 4. API Mock 测试 (Flow Mock Tests)
# ==========================================

@pytest.mark.asyncio
async def test_parse_core_success(target_parser):
    """
    测试核心抓取逻辑：
    1. 拦截网络请求，塞入伪造数据。
    2. 验证提取出的图文/视频链接和文案等是否正确。
    """

    fake_url = "https://www.example.com/v/12345"

    # 8: 将抓包得到的真实 JSON/HTML 粘贴在这里
    fake_api_response = {
        "data": {
            "title": "测试标题",
            "author": "测试作者",
            "video_url": "http://fake.com/video.mp4"
        }
    }

    # ================= 拦截区域 =================

    # A: 使用 aiohttp 自己发请求的：
    mock_resp = AsyncMock()
    # mock_resp.text.return_value = json.dumps(fake_api_response) # 如果是请求 HTML
    mock_resp.json.return_value = fake_api_response             # 如果是请求 API
    mock_resp.status = 200
    target_parser._session.get.return_value.__aenter__.return_value = mock_resp

    # B: 像 B 站那样用第三方库的：
    # with patch('xxx.第三方库.Client') as MockClient:
    #     MockClient.get_data.return_value = fake_api_response

    # ================= 执行与验证 =================

    # 9: 调用真实的解析入口函数
    # result = await target_parser.parse_video(fake_url)

    # 10: 编写断言 (Assert) 验证结果
    # assert result.title == "测试标题"
    # assert result.author.name == "测试作者"
    # 验证是否成功创建了视频对象
    # target_parser.downloader.download_video.assert_called_once_with("http://fake.com/video.mp4", ...)
    pass
