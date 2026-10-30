import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core import config as config_module
from core.config import PluginConfig


@pytest.mark.parametrize(
    "proxy,emoji_proxy,parser_proxy,enabled,affected",
    [
        ("", True, True, True, ["emoji", "bilibili"]),
        ("  ", False, True, True, ["bilibili"]),
        ("", True, True, False, ["emoji"]),
        ("", False, False, True, []),
        ("http://proxy.example:8080", True, True, True, []),
    ],
)
def test_proxy_warning_on_config_load_only(
    monkeypatch, tmp_path, proxy, emoji_proxy, parser_proxy, enabled, affected
):
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    raw = {key: value["default"] for key, value in schema.items() if "default" in value}
    parser = {
        key: value["default"]
        for key, value in schema["parsers_template"]["templates"]["bilibili"]["items"].items()
        if "default" in value
    }
    parser.update(__template_key="bilibili", enable=enabled, use_proxy=parser_proxy)
    raw.update(
        proxy=proxy, parser_emoji_use_proxy=emoji_proxy,
        parsers_template=[parser],
    )
    warning = MagicMock()
    monkeypatch.setattr(config_module.logger, "warning", warning)
    monkeypatch.setattr(config_module.StarTools, "get_data_dir", lambda _: tmp_path)
    monkeypatch.setattr(config_module, "get_astrbot_plugin_path", lambda: str(tmp_path))
    context = MagicMock()
    context.get_config.return_value = {}
    config = PluginConfig(raw, context)
    notices = [
        call.args[0] for call in warning.call_args_list
        if "全局 proxy 地址为空" in call.args[0]
    ]
    assert len(notices) == bool(affected)
    if affected:
        assert notices[0].endswith("、".join(affected))
    for _ in range(3):
        assert config.proxy == (proxy.strip() or None)
    assert len(warning.call_args_list) == (1 if affected else 0)


def test_emoji_defaults_match_schema_for_existing_config(monkeypatch, tmp_path):
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    raw = {key: value["default"] for key, value in schema.items() if "default" in value}
    for key in ("parser_emoji_cdn", "parser_emoji_style", "parser_emoji_use_proxy"):
        raw.pop(key)
    raw["parsers_template"] = [{
        "__template_key": "bilibili",
        **{
            key: value["default"]
            for key, value in schema["parsers_template"]["templates"]["bilibili"]["items"].items()
            if "default" in value
        },
    }]
    monkeypatch.setattr(config_module.StarTools, "get_data_dir", lambda _: tmp_path)
    monkeypatch.setattr(config_module, "get_astrbot_plugin_path", lambda: str(tmp_path))
    context = MagicMock()
    context.get_config.return_value = {}
    config = PluginConfig(raw, context)
    for key in ("parser_emoji_cdn", "parser_emoji_style", "parser_emoji_use_proxy"):
        assert getattr(config, key) == schema[key]["default"]
