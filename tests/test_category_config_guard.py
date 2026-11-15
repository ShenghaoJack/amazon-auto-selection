# 单元测试: 类目配置防呆(2026-09-13) —— "CAT 必须等于所在目录名"
#
# 背景(实战事故, 证据见 data/archive/Air Purifiers for Home Pets-<ts>/遗留配置归档/):
#   2026-09-09 有人在 webui 里把 data/Air Purifiers for Home Pets/config.yaml 的 CAT 改成
#   "Air Purifiers for Home Pets-US-202608" 后保存; webui 只写回原路径(不搬文件) → 该文件留在
#   老目录、里面的 CAT 已是新名字; 之后运行按 CAT 新建 data/<新名>/, 老目录成为"孤儿";
#   而 webui 下拉标签取的就是 CAT, 两个条目**同名**, 误选孤儿跑就会把孤儿内容覆盖到活跃类目配置上。
# 现策略: 保存/运行前校验, 不一致**阻断**(保存可用 allow_move=true 自动搬到 data/<新CAT>/)。
import os

from core.category_config import (
    CAT_DIR_MISMATCH_TOKEN,
    check_cat_dir_consistency,
    find_data_cat_dir,
    list_category_configs,
    orphan_category_configs,
)


def _mk(tmp_dir: str, dir_name: str, cat: str, data_root: str = "data") -> str:
    d = os.path.join(tmp_dir, data_root, dir_name)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "config.yaml")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f'CAT: "{cat}"\nDATA_ROOT: "{data_root}"\nDRY_RUN: false\n')
    return p


def _root(tmp_dir: str, data_root: str = "data") -> str:
    return os.path.join(tmp_dir, data_root)


# ---------- find_data_cat_dir: 只认"类目配置" ----------

def test_find_data_cat_dir_recognizes_category_config(ws_tmp):
    p = _mk(ws_tmp, "cat-a", "cat-a")
    found = find_data_cat_dir(p, _root(ws_tmp))
    assert found is not None
    assert found[1] == "cat-a"


def test_find_data_cat_dir_ignores_non_category_files(ws_tmp):
    # 根目录模板 / configs/*.yaml 不做校验
    root_cfg = os.path.join(ws_tmp, "config.yaml")
    with open(root_cfg, "w", encoding="utf-8") as fh:
        fh.write("CAT: anything\n")
    assert find_data_cat_dir(root_cfg, _root(ws_tmp)) is None
    cfgdir = os.path.join(ws_tmp, "configs")
    os.makedirs(cfgdir, exist_ok=True)
    other = os.path.join(cfgdir, "m.yaml")
    with open(other, "w", encoding="utf-8") as fh:
        fh.write("CAT: cat-a\n")
    assert find_data_cat_dir(other, _root(ws_tmp)) is None


def test_find_data_cat_dir_handles_relative_path(ws_tmp, monkeypatch):
    """CLI 可能传相对路径(--config data/A/config.yaml) + DATA_ROOT="data" —— 两侧都需绝对化。"""
    monkeypatch.chdir(ws_tmp)
    _mk(ws_tmp, "cat-a", "cat-a")
    found = find_data_cat_dir(os.path.join("data", "cat-a", "config.yaml"), "data")
    assert found is not None and found[1] == "cat-a"


# ---------- 一致性校验 ----------

def test_check_passes_when_cat_matches_dir(ws_tmp):
    p = _mk(ws_tmp, "cat-a", "cat-a")
    assert check_cat_dir_consistency(p, "cat-a", _root(ws_tmp)) is None


def test_check_blocks_when_cat_differs_from_dir(ws_tmp):
    p = _mk(ws_tmp, "cat-a", "cat-b")          # 文件在 data/cat-a/ 里却写着 CAT=cat-b
    err = check_cat_dir_consistency(p, "cat-b", _root(ws_tmp))
    assert err and err.startswith(CAT_DIR_MISMATCH_TOKEN)
    assert "cat-a" in err and "cat-b" in err
    assert "data/cat-b/config.yaml" in err


def test_check_marks_target_existing_or_not(ws_tmp):
    p = _mk(ws_tmp, "cat-a", "cat-b")
    assert "尚不存在" in check_cat_dir_consistency(p, "cat-b", _root(ws_tmp))
    _mk(ws_tmp, "cat-b", "cat-b")
    p2 = _mk(ws_tmp, "cat-a", "cat-b")
    assert "已存在" in check_cat_dir_consistency(p2, "cat-b", _root(ws_tmp))


def test_check_reads_cat_from_file_when_not_passed(ws_tmp):
    p = _mk(ws_tmp, "cat-a", "cat-b")
    err = check_cat_dir_consistency(p, None, _root(ws_tmp))
    assert err and "cat-b" in err


def test_check_skips_placeholder_and_root_templates(ws_tmp):
    p = _mk(ws_tmp, "cat-a", "your-category-US-YYYYMM")
    assert check_cat_dir_consistency(p, None, _root(ws_tmp)) is None
    root_cfg = os.path.join(ws_tmp, "config.yaml")
    with open(root_cfg, "w", encoding="utf-8") as fh:
        fh.write("CAT: whatever\n")
    assert check_cat_dir_consistency(root_cfg, "whatever", _root(ws_tmp)) is None


def test_check_respects_custom_data_root(ws_tmp):
    p = _mk(ws_tmp, "cat-a", "cat-b", data_root="mydata")
    mydata = _root(ws_tmp, "mydata")
    assert check_cat_dir_consistency(p, "cat-b", mydata) is not None
    # 用错的 data_root(默认 data) -> 不认作类目配置, 不误报
    assert check_cat_dir_consistency(p, "cat-b", _root(ws_tmp)) is None


# ---------- 扫描: 孤儿识别 + 正常配置不受影响 ----------

def test_orphan_category_configs_finds_mismatch(ws_tmp):
    _mk(ws_tmp, "cat-a", "cat-a")
    _mk(ws_tmp, "cat-b", "cat-a")              # 孤儿: CAT 指向 cat-a
    orphans = orphan_category_configs(_root(ws_tmp))
    assert [o["dir"] for o in orphans] == ["cat-b"]
    assert orphans[0]["cat"] == "cat-a"
    assert orphans[0]["target_exists"] is True          # data/cat-a/config.yaml 存在 -> 误选会覆盖它


def test_list_category_configs_lists_both_entries(ws_tmp):
    """错位配置仍会被列出(webui 需标 ⚠ 而不是静默隐藏), 数量与目录一致。"""
    _mk(ws_tmp, "cat-a", "cat-a")
    _mk(ws_tmp, "cat-b", "cat-a")
    got = list_category_configs(_root(ws_tmp))
    assert sorted(c.parent.name for c in got) == ["cat-a", "cat-b"]


# ---------- webui 保存: 阻断 + allow_move 自动迁移 ----------

def _patch_roots(monkeypatch, tmp_dir):
    """把 webui/server 与 config_backup 的项目根指到临时目录(否则会写真实 data/ 与 .webui/)。"""
    from pathlib import Path

    import core.config_backup as cb
    import webui.server as server
    monkeypatch.setattr(server, "PROJECT_ROOT", Path(tmp_dir))
    monkeypatch.setattr(cb, "PROJECT_ROOT", Path(tmp_dir))
    monkeypatch.setattr(cb, "BACKUP_DIR", Path(tmp_dir) / ".webui" / "config-backups")
    return server


def test_webui_save_blocks_cat_dir_mismatch(ws_tmp, monkeypatch):
    server = _patch_roots(monkeypatch, ws_tmp)
    p = _mk(ws_tmp, "cat-a", "cat-b")
    # 注意: 必须**带一个真正变化的字段** —— 只改 CAT 而文件里已是该值时会先命中"无实际改动, 不写盘"的早返回
    body = {"config_file": "data/cat-a/config.yaml",
            "values": {"CAT": "cat-b", "LONGTAIL_PER_DIM": "7"}, "lists": {}}
    try:
        server.Handler._api_config_save(None, body)
        raise AssertionError("CAT 与目录不一致时应阻断保存")
    except ValueError as exc:
        assert CAT_DIR_MISMATCH_TOKEN in str(exc)
    # 文件未被改写(仍是原内容), 也没有凭空造出目标类目
    assert "LONGTAIL_PER_DIM" not in open(p, encoding="utf-8").read()
    assert not os.path.exists(os.path.join(ws_tmp, "data", "cat-b", "config.yaml"))


def test_webui_save_no_change_returns_unchanged_without_write(ws_tmp, monkeypatch):
    """内容无实际改动 -> 早返回 unchanged, 不写盘不备份(错位配置在这种请求下也不会被改写)。"""
    server = _patch_roots(monkeypatch, ws_tmp)
    p = _mk(ws_tmp, "cat-a", "cat-b")
    out = server.Handler._api_config_save(
        None, {"config_file": "data/cat-a/config.yaml", "values": {"CAT": "cat-b"}, "lists": {}})
    assert out.get("unchanged") is True
    assert '"cat-b"' in open(p, encoding="utf-8").read()


def test_webui_save_allow_move_relocates_config(ws_tmp, monkeypatch):
    server = _patch_roots(monkeypatch, ws_tmp)
    _mk(ws_tmp, "cat-a", "cat-b")
    body = {"config_file": "data/cat-a/config.yaml",
            "values": {"CAT": "cat-b", "LONGTAIL_PER_DIM": "7"}, "lists": {},
            "allow_move": True}
    out = server.Handler._api_config_save(None, body)
    assert out["ok"] is True
    assert out["moved_to"] == "data/cat-b/config.yaml"
    new_path = os.path.join(ws_tmp, "data", "cat-b", "config.yaml")
    assert os.path.isfile(new_path)
    assert not os.path.exists(os.path.join(ws_tmp, "data", "cat-a", "config.yaml"))
    assert "LONGTAIL_PER_DIM: 7" in open(new_path, encoding="utf-8").read()
    # 迁移后备份写在**新类目**目录下(版本化备份随类目走)
    baks = os.listdir(os.path.join(ws_tmp, "data", "cat-b", "config-backups"))
    assert baks and all(b.endswith(".bak") for b in baks)


def test_webui_save_allow_move_refuses_existing_target(ws_tmp, monkeypatch):
    server = _patch_roots(monkeypatch, ws_tmp)
    _mk(ws_tmp, "cat-a", "cat-b")
    _mk(ws_tmp, "cat-b", "cat-b")             # 目标已存在 -> 不能自动迁移(避免覆盖)
    body = {"config_file": "data/cat-a/config.yaml",
            "values": {"CAT": "cat-b", "LONGTAIL_PER_DIM": "7"}, "lists": {},
            "allow_move": True}
    try:
        server.Handler._api_config_save(None, body)
        raise AssertionError("目标配置已存在时应拒绝自动迁移")
    except ValueError as exc:
        assert "目标配置已存在" in str(exc)


def test_webui_save_normal_config_unaffected(ws_tmp, monkeypatch):
    server = _patch_roots(monkeypatch, ws_tmp)
    _mk(ws_tmp, "cat-a", "cat-a")
    body = {"config_file": "data/cat-a/config.yaml", "values": {"LONGTAIL_PER_DIM": "3"}, "lists": {}}
    out = server.Handler._api_config_save(None, body)
    assert out["ok"] is True and out["changed"] == ["LONGTAIL_PER_DIM"]
    assert "moved_to" not in out
    text = open(os.path.join(ws_tmp, "data", "cat-a", "config.yaml"), encoding="utf-8").read()
    assert "LONGTAIL_PER_DIM: 3" in text
