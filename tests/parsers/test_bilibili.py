import pytest
from msgspec import convert
import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from bilibili_api.exceptions import ArgsException, ResponseCodeException

# 解决 ModuleNotFoundError
current_file = Path(__file__).resolve()
plugin_root = current_file.parent.parent.parent
plugins_dir = plugin_root.parent

if str(plugin_root) not in sys.path:
    sys.path.insert(0, str(plugin_root))
if str(plugins_dir) not in sys.path:
    sys.path.insert(0, str(plugins_dir))

try:
    from astrbot_plugin_parserurl.core.parsers.bilibili import BilibiliParser
    from astrbot_plugin_parserurl.core.parsers.base import ParseException
    from astrbot_plugin_parserurl.core.parsers.bilibili.dynamic import DynamicData
    from astrbot_plugin_parserurl.core.parsers.bilibili.opus import ImageNode, OpusItem, TextNode
except ModuleNotFoundError:
    from core.parsers.bilibili import BilibiliParser
    from core.parsers.base import ParseException
    from core.parsers.bilibili.dynamic import DynamicData
    from core.parsers.bilibili.opus import ImageNode, OpusItem, TextNode

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
    bili_cfg.ignore_lottery_content = ["恭喜", "中奖", "私信", "奖品", "抽奖"]
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


@pytest.fixture
def mock_dynamic_request(bili_parser):
    """隔离动态请求和登录凭证，避免回归测试访问网络"""
    credential = MagicMock()

    async def get_credential():
        return credential

    with patch.object(
        type(bili_parser.login), "credential", new_callable=PropertyMock
    ) as mock_credential, patch("bilibili_api.dynamic.Dynamic") as mock_dynamic, patch(
        f"{BilibiliParser.__module__}.Opus"
    ) as mock_opus:
        mock_credential.side_effect = get_credential
        dynamic = AsyncMock()
        dynamic.is_article.return_value = False
        mock_dynamic.return_value = dynamic
        yield dynamic, mock_dynamic, mock_opus, credential, mock_credential


@pytest.mark.asyncio
@pytest.mark.parametrize("is_article", [False, True])
async def test_parse_dynamic_normal_flow(bili_parser, mock_dynamic_request, is_article):
    """正常动态和专栏保留原有解析分支，不触发降级"""
    dynamic, _, mock_opus, _, _ = mock_dynamic_request
    dynamic.is_article.return_value = is_article
    dynamic.get_info.return_value = {"item": {}}
    expected = MagicMock()
    dynamic_info = MagicMock()

    with patch.object(
        bili_parser, "parse_read_with_opus", new_callable=AsyncMock, return_value=expected
    ) as parse_article, patch.object(
        bili_parser, "_parse_dynamic_info", new_callable=AsyncMock, return_value=expected
    ) as parse_info, patch(
        f"{BilibiliParser.__module__}.convert", return_value=dynamic_info
    ):
        result = await bili_parser.parse_dynamic(1258550832983441431)

    assert result is expected
    mock_opus.assert_not_called()
    if is_article:
        parse_article.assert_awaited_once_with(1258550832983441431)
        dynamic.get_info.assert_not_awaited()
        parse_info.assert_not_awaited()
    else:
        parse_article.assert_not_awaited()
        dynamic.get_info.assert_awaited_once()
        parse_info.assert_awaited_once_with(dynamic_info.item)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_method", ["is_article", "get_info"])
async def test_parse_dynamic_352_opus_fallback(
    bili_parser, mock_dynamic_request, opus_top_info, failed_method
):
    """-352 仅尝试一次 Opus，复用凭证并实际解析图文内容。"""
    dynamic, mock_dynamic, mock_opus, credential, mock_credential = mock_dynamic_request
    getattr(dynamic, failed_method).side_effect = ResponseCodeException(-352, "-352")
    opus = AsyncMock()
    opus.get_info.return_value = opus_top_info
    mock_opus.return_value = opus

    keyword, searched = bili_parser.search_url("https://t.bilibili.com/1258550832983441431")
    result = await bili_parser.parse(keyword, searched)

    assert len(result.contents) == 1
    assert "有人要换一下吗" in result.text
    mock_dynamic.assert_called_once_with(1258550832983441431, credential)
    mock_opus.assert_called_once_with(1258550832983441431, credential)
    mock_credential.assert_called_once()
    opus.get_info.assert_awaited_once()
    getattr(dynamic, failed_method).assert_awaited_once()
    if failed_method == "is_article":
        dynamic.get_info.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_method", ["is_article", "get_info"])
async def test_parse_dynamic_other_error_no_fallback(
    bili_parser, mock_dynamic_request, failed_method
):
    """其他接口错误原样抛出，不使用 Opus 掩盖错误"""
    dynamic, _, mock_opus, _, _ = mock_dynamic_request
    error = ResponseCodeException(-404, "动态不存在")
    getattr(dynamic, failed_method).side_effect = error

    with pytest.raises(ResponseCodeException) as exc_info:
        await bili_parser.parse_dynamic(1258550832983441431)

    assert exc_info.value is error
    mock_opus.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("opus_error", [
    ResponseCodeException(-352, "-352"),
    ArgsException("传入的 opus_id 不正确"),
    TimeoutError("请求超时"),
])
async def test_parse_dynamic_opus_fallback_failure(
    bili_parser, mock_dynamic_request, opus_error
):
    """Opus 风控、不支持或超时均终止降级，保留异常原因。"""
    dynamic, _, mock_opus, credential, _ = mock_dynamic_request
    dynamic.is_article.side_effect = ResponseCodeException(-352, "-352")
    opus = AsyncMock()
    opus.get_info.side_effect = opus_error
    mock_opus.return_value = opus

    with pytest.raises(ParseException, match="Opus 降级解析失败") as exc_info:
        await bili_parser.parse_dynamic(1258550832983441431)

    assert exc_info.value.__cause__ is opus_error
    mock_opus.assert_called_once_with(1258550832983441431, credential)
    opus.get_info.assert_awaited_once()
    dynamic.is_article.assert_awaited_once()
    dynamic.get_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_parse_dynamic_article_error_no_fallback(bili_parser, mock_dynamic_request):
    """专栏已走 Opus 分支时失败，不再重复降级"""
    dynamic, _, mock_opus, _, _ = mock_dynamic_request
    dynamic.is_article.return_value = True
    error = ResponseCodeException(-352, "-352")
    with patch.object(
        bili_parser, "parse_read_with_opus", new_callable=AsyncMock, side_effect=error
    ) as parse_article:
        with pytest.raises(ResponseCodeException) as exc_info:
            await bili_parser.parse_dynamic(1258550832983441431)

    assert exc_info.value is error
    parse_article.assert_awaited_once_with(1258550832983441431)
    mock_opus.assert_not_called()

# 辅助函数逻辑测试 (Helper Logic Tests)
def test_codec_priority_list(bili_parser):
    """测试配置读取是否正确转换为大写"""
    assert bili_parser.codec_priority_list == ["HEV", "AV1", "AVC"]

@pytest.mark.asyncio
async def test_is_lottery_logic(bili_parser):
    """测试抽奖动态过滤逻辑"""
    # _is_lottery 使用解析器从用户配置读取的关键词，这里直接覆盖测试逻辑
    bili_parser.ignore_lottery_content = ["关注", "转发", "中奖"]

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


@pytest.fixture
def opus_top_info():
    """动态 1254290227529252902 的顶部相册及正文结构"""
    return {
        "item": {
            "id_str": "1254290227529252902",
            "type": 1,
            "basic": {"title": "毋氓WuMeng的动态 - 哔哩哔哩", "comment_type": 11},
            "modules": [
                {
                    "module_type": "MODULE_TYPE_TOP",
                    "module_top": {
                        "display": {
                            "type": 1,
                            "album": {
                                "pics": [{
                                    "url": "http://i0.hdslb.com/bfs/new_dyn/0be9c9e162e4786d3ca4fa782f24fb1e1670429928.png",
                                    "width": 1080,
                                    "height": 1440,
                                }]
                            },
                            "video": None,
                        }
                    },
                    "module_content": None,
                },
                {
                    "module_type": "MODULE_TYPE_AUTHOR",
                    "module_author": {
                        "name": "毋氓WuMeng",
                        "face": "https://example.com/avatar.jpg",
                        "mid": 1670429928,
                        "pub_time": "",
                        "pub_ts": "1700000000",
                    },
                },
                {
                    "module_type": "MODULE_TYPE_CONTENT",
                    "module_content": {
                        "paragraphs": [{
                            "para_type": 1,
                            "text": {"nodes": [{
                                "type": "TEXT_NODE_TYPE_WORD",
                                "word": {"words": "有人要换一下吗，多出来了迷途兽景元元车厘子和球棒狸"},
                            }]},
                            "pic": None,
                        }]
                    },
                },
            ],
        }
    }


@pytest.mark.parametrize("pub_ts, expected", [
    (1700000000, 1700000000),
    ("1700000000", 1700000000),
    ("", None),
    ("invalid", None),
])
def test_dynamic_pub_ts_is_converted(pub_ts, expected):
    """普通动态应兼容两种时间戳格式，并忽略无效时间戳。"""
    dynamic_info = {
        "item": {
            "id_str": "1234567",
            "type": "DYNAMIC_TYPE_WORD",
            "visible": True,
            "modules": {
                "module_author": {
                    "name": "测试用户",
                    "face": "https://example.com/avatar.jpg",
                    "mid": 123,
                    "pub_time": "刚刚",
                    "pub_ts": pub_ts,
                },
            },
        },
    }

    parsed = convert(dynamic_info, DynamicData).item

    assert parsed.timestamp == expected


def test_opus_top_album_and_body_order(opus_top_info):
    """顶部多图和正文图文应按原顺序生成。"""
    modules = opus_top_info["item"]["modules"]
    pics = modules[0]["module_top"]["display"]["album"]["pics"]
    pics.append({"url": "https://example.com/top2.jpg"})
    modules[2]["module_content"]["paragraphs"].append({
        "para_type": 2,
        "pic": {"pics": [{"url": "https://example.com/body.jpg"}], "style": 1},
    })

    nodes = list(convert(opus_top_info, OpusItem).gen_text_img())

    assert nodes == [
        ImageNode(url=pics[0]["url"]),
        ImageNode(url=pics[1]["url"]),
        TextNode(text="\n\n有人要换一下吗，多出来了迷途兽景元元车厘子和球棒狸"),
        ImageNode(url="https://example.com/body.jpg"),
    ]


@pytest.mark.parametrize("top", [
    None,
    {},
    {"display": None},
    {"display": {}},
    {"display": {"album": None, "video": {"aid": 1}}},
    {"display": {"album": {"pics": []}}},
])
def test_opus_top_without_album_keeps_body(opus_top_info, top):
    """空顶部及非相册顶部不影响正文解析。"""
    opus_top_info["item"]["modules"][0]["module_top"] = top
    nodes = list(convert(opus_top_info, OpusItem).gen_text_img())
    assert nodes == [TextNode(text="\n\n有人要换一下吗，多出来了迷途兽景元元车厘子和球棒狸")]


def test_opus_without_top_keeps_body_images(opus_top_info):
    """没有顶部模块的原有正文图片仍正常提取。"""
    modules = opus_top_info["item"]["modules"]
    modules.pop(0)
    modules[1]["module_content"]["paragraphs"].insert(0, {
        "para_type": 2,
        "pic": {"pics": [{"url": "https://example.com/body.jpg"}], "style": 1},
    })
    nodes = list(convert(opus_top_info, OpusItem).gen_text_img())
    assert nodes == [
        ImageNode(url="https://example.com/body.jpg"),
        TextNode(text="\n\n有人要换一下吗，多出来了迷途兽景元元车厘子和球棒狸"),
    ]


@pytest.mark.asyncio
async def test_parse_opus_top_image(bili_parser, opus_top_info):
    """实际 case 的顶部图片应进入解析结果并触发下载。"""
    opus = AsyncMock()
    opus.get_info.return_value = opus_top_info

    result = await bili_parser._parse_opus_obj(opus)

    assert len(result.contents) == 1
    assert result.text == "有人要换一下吗，多出来了迷途兽景元元车厘子和球棒狸"
    assert result.url == "https://www.bilibili.com/opus/1254290227529252902"
    bili_parser.downloader.download_img.assert_any_call(
        opus_top_info["item"]["modules"][0]["module_top"]["display"]["album"]["pics"][0]["url"],
        headers=bili_parser.headers,
        proxy=bili_parser.proxy,
    )

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

    # 拦截视频接口和凭证初始化，避免测试触发真实网络请求
    with patch('astrbot_plugin_parserurl.core.parsers.bilibili.Video') as MockVideo, \
         patch.object(bili_parser.login, '_init_credential', new_callable=AsyncMock):
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
