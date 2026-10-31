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


@pytest.fixture
def mock_bangumi(bili_parser):
    """固定的模拟元数据；不包含账号、网页或播放地址。"""
    bangumi_module = f"{BilibiliParser.__module__}.bangumi"
    credential = MagicMock()

    async def get_credential():
        return credential

    with patch.object(
        type(bili_parser.login), "credential", new_callable=PropertyMock
    ) as login, patch(f"{bangumi_module}.Bangumi") as factory, patch(
        f"{bangumi_module}.User"
    ) as user_factory, patch(f"{bangumi_module}.time", return_value=1800000000):
        user_factory.return_value.get_user_info = AsyncMock(return_value={})
        login.side_effect = get_credential
        bangumi = AsyncMock()
        factory.return_value = bangumi
        bangumi.get_overview.return_value = {
            "season_id": 1588,
            "title": "测试番剧",
            "cover": "https://example.com/season.jpg",
            "evaluate": "<p>测试概况 &amp; 简介</p>",
            "publish": {"pub_time": "2020-01-02 12:00:00"},
            "total": 12,
            "areas": [{"name": "日本"}],
            "styles": ["奇幻", "冒险"],
            "episodes": [{
                "id": 6513061,
                "title": "1",
                "long_title": "测试单集",
                "cover": "//example.com/episode.jpg",
                "pub_time": 1577937600000,
                "badge": "会员",
            }],
        }
        bangumi.get_meta.return_value = {
            "media": {"title": "测试番剧", "rating": {"score": 9.2, "count": 123}}
        }
        yield bangumi, factory, credential


@pytest.mark.parametrize("kind", ["ss", "ep"])
@pytest.mark.parametrize("suffix", ["", "/", "?from=share", "#detail", " 这部不错", "）"])
def test_bangumi_route(kind, suffix):
    keyword, match = BilibiliParser.search_url(
        f"看看 https://www.bilibili.com/bangumi/play/{kind}1588{suffix}"
    )
    assert keyword == f"/bangumi/play/{kind}"
    assert match.group("season_id" if kind == "ss" else "episode_id") == "1588"


@pytest.mark.parametrize("url", [
    "https://evilbilibili.com/bangumi/play/ss1588",
    "https://other.bilibili.com/bangumi/play/ss1588",
    "https://www.bilibili.com/bangumi/play/ep123abc",
    "https://www.bilibili.com/bangumi/play/ss123.4",
])
def test_bangumi_invalid_route(url):
    with pytest.raises(ParseException):
        BilibiliParser.search_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, target_id", [("ss", 1588), ("ep", 6513061)])
async def test_bangumi_metadata_preview(bili_parser, mock_bangumi, kind, target_id):
    bangumi, factory, credential = mock_bangumi
    keyword, match = bili_parser.search_url(
        f"https://www.bilibili.com/bangumi/play/{kind}{target_id}?from=share"
    )
    result = await bili_parser.parse(keyword, match)

    factory.assert_called_once_with(
        **{"ssid" if kind == "ss" else "epid": target_id}, credential=credential
    )
    assert result.title == ("测试番剧" if kind == "ss" else "测试番剧 - 测试单集")
    assert result.url == f"https://www.bilibili.com/bangumi/play/{kind}{target_id}"
    assert result.extra["layout"] == "bangumi"
    assert result.text == "测试概况 & 简介"
    assert result.timestamp == 1800000000
    assert "评分：9.2（123 人评价）" in result.extra_info
    assert "2020-01-02" in result.extra_info
    assert "地区：日本" in result.extra_info
    assert "类型：奇幻、冒险" in result.extra_info
    assert "集数：12" in result.extra_info
    if kind == "ep":
        assert "分集：1" in result.extra_info
        assert "会员" in result.extra_info
    cover = "season.jpg" if kind == "ss" else "episode.jpg"
    bili_parser.downloader.download_img.assert_called_once_with(
        f"https://example.com/{cover}", headers=bili_parser.headers, proxy=None
    )
    assert not result.video_contents
    assert len(result.graphics_contents) == 1
    assert len(result.send_groups) == 1
    assert not result.send_groups[0].contents
    assert result.send_groups[0].render_card is True
    bangumi.get_download_url.assert_not_called()
    bangumi.get_episodes.assert_not_called()
    bili_parser.downloader.download_video.assert_not_called()
    bili_parser.downloader.download_av_and_merge.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("rating", [None, {}, {"score": 9.2}, {"count": 123}])
async def test_bangumi_missing_rating(bili_parser, mock_bangumi, rating):
    bangumi, _, _ = mock_bangumi
    bangumi.get_meta.return_value = {"media": {"rating": rating}}
    keyword, match = bili_parser.search_url("https://bilibili.com/bangumi/play/ss1588")
    result = await bili_parser.parse(keyword, match)
    assert "评分：暂无评分" in result.extra_info


@pytest.mark.asyncio
async def test_bangumi_optional_meta_failure(bili_parser, mock_bangumi):
    bangumi, _, _ = mock_bangumi
    bangumi.get_meta.side_effect = ResponseCodeException(-404, "无评分信息")
    keyword, match = bili_parser.search_url("https://bilibili.com/bangumi/play/ep6513061")
    result = await bili_parser.parse(keyword, match)
    assert result.title == "测试番剧 - 测试单集"
    assert "暂无评分" in result.extra_info


@pytest.mark.asyncio
async def test_bangumi_minimal_public_metadata(bili_parser, mock_bangumi):
    bangumi, factory, _ = mock_bangumi
    bangumi.get_overview.return_value = {"title": "测试电影"}
    bangumi.get_meta.return_value = {}

    async def no_credential():
        return None

    with patch.object(type(bili_parser.login), "credential", new_callable=PropertyMock) as login:
        login.side_effect = no_credential
        keyword, match = bili_parser.search_url("https://bilibili.com/bangumi/play/ep6513061")
        result = await bili_parser.parse(keyword, match)
    factory.assert_called_once_with(epid=6513061, credential=None)
    assert result.title == "测试电影"
    assert result.text is None
    assert result.timestamp == 1800000000
    assert result.contents == []
    assert result.extra_info == "评分：暂无评分"


@pytest.mark.asyncio
@pytest.mark.parametrize("overview", [None, {}, {"episodes": []}])
async def test_bangumi_missing_overview(bili_parser, mock_bangumi, overview):
    bangumi, _, _ = mock_bangumi
    bangumi.get_overview.return_value = overview
    keyword, match = bili_parser.search_url("https://bilibili.com/bangumi/play/ss1588")
    with pytest.raises(ParseException, match="不存在或暂无可用元数据"):
        await bili_parser.parse(keyword, match)


@pytest.mark.asyncio
async def test_bangumi_overview_error(bili_parser, mock_bangumi):
    bangumi, _, _ = mock_bangumi
    error = ResponseCodeException(-352, "风控")
    bangumi.get_overview.side_effect = error
    keyword, match = bili_parser.search_url("https://bilibili.com/bangumi/play/ss1588")
    with pytest.raises(ParseException, match="元数据获取失败") as exc_info:
        await bili_parser.parse(keyword, match)
    assert exc_info.value.__cause__ is error
    bangumi.get_meta.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, target_id", [("ss", 1588), ("ep", 6513061)])
async def test_bangumi_upstream_uses_only_metadata_api(
    bili_parser, mock_bangumi, kind, target_id
):
    """保留真实上游 Bangumi 调用链，拦截底层 API 以防间接请求播放地址。"""
    from bilibili_api import bangumi as upstream

    bangumi, factory, _ = mock_bangumi
    overview = dict(bangumi.get_overview.return_value, media_id=1001588)
    meta = bangumi.get_meta.return_value
    factory.side_effect = upstream.Bangumi
    calls = []

    class MetadataApi:
        def __init__(self, *, url, **kwargs):
            assert url in {
                "https://api.bilibili.com/pgc/view/web/simple/season",
                "https://api.bilibili.com/pgc/review/user",
            }
            self.url = url

        def update_params(self, **params):
            calls.append((self.url, params))
            return self

        @property
        async def result(self):
            return meta if self.url.endswith("/review/user") else overview

    with patch.object(upstream, "Api", MetadataApi), patch.object(
        upstream.Episode, "get_download_url", new_callable=AsyncMock
    ) as playurl, patch.object(
        bili_parser, "extract_download_urls", new_callable=AsyncMock
    ) as extract:
        keyword, match = bili_parser.search_url(
            f"https://bilibili.com/bangumi/play/{kind}{target_id}"
        )
        result = await bili_parser.parse(keyword, match)
    assert "9.2" in result.extra_info
    assert calls
    playurl.assert_not_called()
    extract.assert_not_called()


@pytest.mark.asyncio
async def test_bangumi_sends_preview_and_link_only(bili_parser, mock_bangumi, tmp_path):
    import importlib

    sender_module = importlib.import_module(
        f"{BilibiliParser.__module__.rsplit('.parsers.', 1)[0]}.sender"
    )
    keyword, match = bili_parser.search_url("https://bilibili.com/bangumi/play/ss1588")
    result = await bili_parser.parse(keyword, match)
    renderer = MagicMock()
    renderer.render_card = AsyncMock(return_value=tmp_path / "preview.png")
    bili_parser.cfg.forward_threshold = 5
    bili_parser.cfg.parser.bilibili.show_preview_detail = False
    sender = sender_module.MessageSender(bili_parser.cfg, renderer)
    event = MagicMock()
    event.chain_result.side_effect = lambda segments: segments
    event.send = AsyncMock()

    completed = await sender.send_parse_result(event, result)

    assert completed is True
    event.send.assert_awaited_once()
    segments = event.send.await_args.args[0]
    assert len(segments) == 2
    assert segments[0].__class__.__name__ == "Image"
    assert segments[1].text == "链接: https://www.bilibili.com/bangumi/play/ss1588"
    renderer.render_card.assert_awaited_once_with(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, target_id", [("ss", 1588), ("ep", 6513061)])
async def test_bangumi_uploader_header(bili_parser, mock_bangumi, kind, target_id, tmp_path):
    bangumi, _, _ = mock_bangumi
    bangumi.get_overview.return_value["up_info"] = {
        "uname": "测试上传方",
        "avatar": "//example.com/avatar.jpg",
    }
    avatar_path = tmp_path / "avatar.jpg"
    avatar_task = asyncio.get_running_loop().create_future()
    avatar_task.set_result(avatar_path)
    bili_parser.downloader.download_img.return_value = avatar_task
    keyword, match = bili_parser.search_url(
        f"https://bilibili.com/bangumi/play/{kind}{target_id}"
    )
    result = await bili_parser.parse(keyword, match)
    assert result.author.name == "测试上传方"
    assert result.author.avatar is not None
    assert result.timestamp == 1800000000
    bili_parser.downloader.download_img.assert_any_call(
        "https://example.com/avatar.jpg", headers=bili_parser.headers, proxy=None
    )

    # 通过现有头部计算验证名字、时间进入与视频/动态相同的渲染路径。
    import importlib

    render_module = importlib.import_module(
        f"{BilibiliParser.__module__.rsplit('.parsers.', 1)[0]}.render"
    )
    renderer = object.__new__(render_module.Renderer)
    renderer.fontset = render_module.FontSet.new(renderer.DEFAULT_FONT_PATH)
    renderer._load_and_process_avatar = AsyncMock(return_value=None)
    with patch.object(type(result.author), "get_avatar_path", new_callable=AsyncMock):
        header = await renderer._calculate_header_section(result, 750)
    assert header is not None
    assert header.name_lines == ["测试上传方"]
    assert header.time_lines == [result.formatted_datetime()]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "profile", "download"])
async def test_bangumi_fallback_author(bili_parser, mock_bangumi, tmp_path, failure):
    _, _, credential = mock_bangumi
    module = f"{BilibiliParser.__module__}.bangumi"
    avatar_path = tmp_path / "fallback.jpg"
    avatar_task = asyncio.get_running_loop().create_future()
    if failure == "download":
        avatar_task.set_exception(OSError("模拟头像下载失败"))
    else:
        avatar_task.set_result(avatar_path)

    # 封面仍是同步返回的下载任务，只有头像在解析时等待并兜底。
    bili_parser.downloader.download_img.side_effect = (
        lambda url, **kwargs: avatar_task if "fallback" in url else MagicMock()
    )
    with patch(f"{module}.User") as factory:
        factory.return_value.get_user_info = AsyncMock(
            return_value={"name": "不使用账号名字", "face": "//example.com/fallback.jpg"}
        )
        if failure == "profile":
            factory.return_value.get_user_info.side_effect = TimeoutError()
        keyword, match = bili_parser.search_url("https://bilibili.com/bangumi/play/ss1588")
        result = await bili_parser.parse(keyword, match)

    factory.assert_called_once_with(uid=8047632, credential=credential)
    assert result.author is not None
    assert result.author.name == "番剧/电影"
    assert result.author.avatar == (avatar_path if failure is None else None)
    assert result.timestamp == 1800000000
    assert result.platform.name == "bilibili"
    assert "评分：9.2" in result.extra_info
