# 单元测试: 配置加载与解析
import pytest

from core.config import Config
from core.errors import ConfigError


def test_config_defaults():
    cfg = Config({})
    assert cfg.cat == ""
    assert cfg.dry_run is False
    assert cfg.market == "amazon.com"


def test_attr_keys_and_desc_are_gone():
    """★ `ATTR_KEYS`/`ATTR_DESC` **已真删**（2026-10-12 方案 C）：配置里有也不能再读出来。

    它们只是"整份重写 spec"的耐久补丁；现在 `step16` 以**上一份 spec** 为基底合并
    （只增不减、人工改过的不覆盖）→ spec 是唯一真值，补丁与它那个脆弱格式（值里带
    `；` 而 `；` 是分隔符）一起消失。
    """
    cfg = Config({"ATTR_KEYS": "甲;乙", "ATTR_DESC": "丙=直接说明"})
    assert not hasattr(cfg, "attr_keys"), "attr_keys 属性应已删除"
    assert not hasattr(cfg, "attr_desc"), "attr_desc 属性应已删除"


def test_config_list_urls_semicolon():
    cfg = Config({"LIST_URLS": "https://a.com; https://b.com ;https://c.com"})
    assert cfg.list_urls == ["https://a.com", "https://b.com", "https://c.com"]


def test_config_load_missing_file():
    with pytest.raises(ConfigError):
        Config.load("no-such-file.yaml")


# ------------------------------------------------ 7 个默认值升级（2026-10-12 用户口径 B）
UPGRADED = {
    "FETCH_WORKERS": 1,
    "REQUEST_MIN_INTERVAL": 2,
    "REQUEST_MAX_INTERVAL": 4,
    "LLM_TIMEOUT": 300,
    "SS_PROFILE": "data/profiles/ss-profile",
    "AMZ_PROFILE": "data/profiles/amz-profile",
    "VISION_MODEL": "deepseek-v4-flash-vision-exp",
}


def test_upgraded_defaults():
    """★ 这 7 个键的默认值已升级为「15 个类目统一在用的值」。

    为什么升级：原来每个类目都要把这 7 行抄一遍（且抄的值都**不等于**旧默认）——
    与其 15 份各写一次，不如变成默认（新类目 / `copy_from` 自动拿到对的）。
    """
    for k, want in UPGRADED.items():
        assert Config.DEFAULTS.get(k) == want, "%s 默认值应为 %r" % (k, want)


def test_empty_config_gets_upgraded_values():
    """空配置（新类目 / 工具脚本）也拿到升级后的值 —— 不依赖文件里写没写。"""
    c = Config({"CAT": "X"})
    assert c.get("FETCH_WORKERS") == 1
    assert c.get("LLM_TIMEOUT") == 300
    assert str(c.amz_profile_dir) == UPGRADED["AMZ_PROFILE"]
    assert str(c.ss_profile_dir) == UPGRADED["SS_PROFILE"]


def test_upgraded_keys_are_kept_in_every_category_config():
    """★ 用户口径 **B**（2026-10-12）：这 7 行**保留在 config.yaml 里**。

    为什么保留：表单只渲染「文件里有的键」—— 删了行 = **表单里再也改不了它们**。
    用户要的是"新人在表单上看得懂、需要时也改得到"，所以**默认值升级、配置行保留**；
    且写回的值 == 新默认值 → 无论将来删还是留，**生效值都一样**（少一处漂移源）。
    """
    import pathlib

    import yaml
    root = pathlib.Path(__file__).resolve().parent.parent
    files = sorted((root / "data").glob("*/config.yaml"))
    assert files, "没找到任何类目配置？"
    for f in files:
        raw = yaml.safe_load(f.read_text(encoding="utf-8-sig")) or {}
        for k, want in UPGRADED.items():
            assert k in raw, "%s 少了 %s（口径 B 要求保留，否则表单上改不了）" % (f.parent.name, k)
            assert str(raw[k]) == str(want), \
                "%s 的 %s=%r 与新默认 %r 不一致（会造成「文件 vs 默认」两套值）" % (
                    f.parent.name, k, raw[k], want)
