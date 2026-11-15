"""display_cat: 类目名 → LLM 展示名(去掉末尾 -v2/-202609/-US 等后缀)。"""
from core.catname import display_cat


def test_strip_version_suffix():
    assert display_cat("salt-pepper-shaker-sets-v2") == "salt pepper shaker sets"
    assert display_cat("salt-pepper-shaker-sets-v3") == "salt pepper shaker sets"


def test_strip_date_and_region_suffix():
    assert display_cat("cutting-board-202609") == "cutting board"
    assert display_cat("salt-pepper-shaker-sets-US") == "salt pepper shaker sets"
    assert display_cat("kitchen-tools-usa") == "kitchen tools"


def test_legacy_no_suffix_kept():
    # 无版本/日期/地区后缀的旧命名不截断
    assert display_cat("salt-pepper-shaker-sets") == "salt pepper shaker sets"
    assert display_cat("cutting-boards") == "cutting boards"


def test_plain_name_unchanged():
    assert display_cat("saltpepper") == "saltpepper"
    assert display_cat("") == ""
    assert display_cat("my-cat") == "my cat"


def test_multi_suffix_all_stripped():
    # 组合后缀: 版本+地区+日期 等, 全部去掉
    assert display_cat("cutting-board-v2-us-202607") == "cutting board"
    assert display_cat("cutting-board-v4-us") == "cutting board"
    assert display_cat("cutting-board-v5-202608") == "cutting board"
    assert display_cat("kitchen-v2-US-202607") == "kitchen"


def test_spaced_base_with_suffix():
    # 基底名用空格连接 + 末尾 -vN/日期 后缀(词间不用 '-' 的新命名)
    assert display_cat("cutting board-v5-202608") == "cutting board"
    assert display_cat("salt pepper shakers-v2") == "salt pepper shakers"
    assert display_cat("cutting board") == "cutting board"
