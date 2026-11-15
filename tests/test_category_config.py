# 单元测试: core/category_config.py(类目配置 data/<CAT>/config.yaml 模型)
import shutil
from pathlib import Path

from core.category_config import (
    category_config_path,
    ensure_category_config,
    is_placeholder_cat,
    list_category_configs,
    sync_category_config,
)

_TMP = Path(__file__).resolve().parent / "_cc_tmp"
_DROOT = _TMP / "data"


def _setup():
    shutil.rmtree(_TMP, ignore_errors=True)
    (_DROOT / "Sim Cat A-US-202608").mkdir(parents=True)
    (_DROOT / "Sim Cat A-US-202608" / "config.yaml").write_text("CAT: Sim Cat A-US-202608\n", encoding="utf-8")
    (_DROOT / "archive").mkdir(parents=True)
    (_DROOT / "archive" / "Sim Cat A-US-202608-ts").mkdir(parents=True)
    (_DROOT / "archive" / "Sim Cat A-US-202608-ts" / "config.yaml").write_text("x", encoding="utf-8")
    (_DROOT / ".hidden").mkdir(parents=True)


def _seed():
    p = _TMP / "seed.yaml"
    p.write_text('CAT: "Sim Cat B-US-202609"\nTRAFFIC_MONTH: "2026-09"\n', encoding="utf-8")
    return p


def test_placeholder():
    assert is_placeholder_cat("") or is_placeholder_cat("your-category-US-YYYYMM")
    assert is_placeholder_cat("xxx-202607")
    assert not is_placeholder_cat("salt and pepper mill sets-US-202608")
    assert not is_placeholder_cat("smoke-test")


def test_ensure_creates_once_and_keeps():
    _setup()
    seed = _seed()
    t1 = ensure_category_config(seed, _DROOT, "Sim Cat B-US-202609")
    assert t1.is_file() and "Sim Cat B-US-202609" in t1.read_text(encoding="utf-8")
    # 已存在不覆盖(用户编辑为真源)
    t1.write_text("CAT: Sim Cat B-US-202609\n# 用户手工改过\n", encoding="utf-8")
    ensure_category_config(seed, _DROOT, "Sim Cat B-US-202609")
    assert "# 用户手工改过" in t1.read_text(encoding="utf-8")


def test_list_excludes_archive_hidden_and_placeholder():
    _setup()
    # 放一个占位类目 config 不应被列出
    (_DROOT / "your-category-US-YYYYMM").mkdir(parents=True)
    (_DROOT / "your-category-US-YYYYMM" / "config.yaml").write_text("CAT: your-category-US-YYYYMM\n", encoding="utf-8")
    listed = [str(p.relative_to(_DROOT)) for p in list_category_configs(_DROOT)]
    assert any("Sim Cat A-US-202608" in x for x in listed)
    assert not any("archive" in x for x in listed)
    assert not any("your-category" in x for x in listed)
    assert not any(".hidden" in x for x in listed)


def test_sync_overwrites_and_creates_dirs():
    _setup()
    seed = _seed()
    t = sync_category_config(seed, _DROOT, "New Cat-US-202610")
    assert t.is_file()
    assert "Sim Cat B-US-202609" in t.read_text(encoding="utf-8")  # 来自种子内容
    assert category_config_path(_DROOT, "New Cat-US-202610") == t
