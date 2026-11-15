# 单元测试(2026-09-29): 看板保存配置「先校验后写盘」+ 可操作提示
#
# 背景(真事故): `data/Dog Dryer-US-202608/config.yaml` 的 COOKIE 被粘成两行
#   L118  COOKIE: '<旧 cookie>'(完整)
#   L119  session-id=…'(裸值, 没有 `KEY:`)
# → PyYAML 报 `while scanning a simple key … could not find expected ':'`。
# 而当时的保存流程是"**先备份 → 先写盘 → 再校验**": 接口老老实实报"保存失败",
# 但**文件已经被写坏了**(只有备份是好的, 用户并不知道要回滚)。
#
# 本文件钉住三件事:
#   ① 内容不合法 → **一个字节都不写**(文件保持原样, 且不产生备份);
#   ② 错误提示必须**指到真正的病根行**(不是 YAML 报的那一行), 并给出"值里带了换行"的改法;
#   ③ 写盘后再校验失败(兜底分支) → 用刚做的备份**自动回滚**。
import os
from pathlib import Path

import pytest

from webui.server import yaml_save_guard


# ---------------------------------------------------------------- 纯函数: 校验与提示

def test_guard_accepts_normal_config():
    assert yaml_save_guard('CAT: "cat-a"\nLONGTAIL_PER_DIM: 7\n') is None


def test_guard_rejects_bare_scalar_and_points_at_real_culprit():
    """COOKIE 被粘成两行 → 必须指出**第 2 行**(裸值), 并说明改法。"""
    text = "CAT: \"cat-a\"\nCOOKIE: 'session-id=1; ubid-main=2'\nsession-id=134-abc; ubid-main=9'\n\nKEY: 1\n"
    msg = yaml_save_guard(text)
    assert msg is not None
    assert "第 3 行" in msg, f"应指到裸值那一行(第 3 行): {msg}"
    assert "合并成**一行**" in msg
    assert "session-id=134-abc" in msg, "应把出错行内容贴出来便于定位"
    assert "不是病根" in msg, "要说明 YAML 报的位置不是病根"


def test_guard_rejects_bare_scalar_followed_by_comment():
    """真实事故就是这个形状: 裸值行后面跟的是注释(YAML 的报错位置落在更后面)。"""
    text = "COOKIE: 'a=1; b=2'\nubid-main=9'\n\n# 可选: 启动预检查时校验 COOKIE 有效性\nCOOKIE_CHECK: true\n"
    msg = yaml_save_guard(text)
    assert msg is not None and "第 2 行" in msg


def test_guard_does_not_flag_block_scalar_content():
    """`KEY: |` 块标量的缩进内容没有冒号, **不能**被误判成裸值。"""
    text = "FILTER_REQUIREMENT: |\n  排除与本品无关的\n  第二条要求\nLONGTAIL_PER_DIM: 7\n"
    assert yaml_save_guard(text) is None


def test_guard_rejects_multiline_value_for_single_line_keys():
    """YAML 合法但语义坏: 双引号里的 `\\n` 是**真换行** → COOKIE/LIST_URLS 这类必须单行的键要拦。"""
    assert yaml_save_guard('COOKIE: "session-id=1\\nubid-main=2"\n') is not None
    assert yaml_save_guard("LIST_URLS:\n  - 'https://a\\n  https://b'\n") is None   # 列表项本身没换行
    msg = yaml_save_guard('LIST_URLS:\n  - "https://a\\nhttps://b"\n')
    assert msg is not None and "LIST_URLS" in msg


def test_guard_rejects_non_mapping_toplevel():
    msg = yaml_save_guard("- a\n- b\n")
    assert msg is not None and "顶层不是" in msg


# ---------------------------------------------------------------- 接口: 真保存流程

def _patch_roots(monkeypatch, tmp_dir):
    """把 webui/server 与 config_backup 项目根指到临时目录(否则会写真实 data/ 与 .webui/)。"""
    import core.config_backup as cb
    import webui.server as server
    monkeypatch.setattr(server, "PROJECT_ROOT", Path(tmp_dir))
    monkeypatch.setattr(cb, "PROJECT_ROOT", Path(tmp_dir))
    monkeypatch.setattr(cb, "BACKUP_DIR", Path(tmp_dir) / ".webui" / "config-backups")
    return server


def _mk(tmp_dir: str, body: str = 'CAT: "cat-a"\nDATA_ROOT: "data"\nDRY_RUN: false\nLONGTAIL_PER_DIM: 5\n') -> str:
    d = os.path.join(tmp_dir, "data", "cat-a")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "config.yaml")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return p


def _backups(tmp_dir: str) -> list:
    """备份可能落在两处: 类目自己的 `data/<CAT>/config-backups/`(2026-09 起) 或兜底 `.webui/config-backups/`。"""
    root = Path(tmp_dir)
    out = []
    for d in (root / "data" / "cat-a" / "config-backups", root / ".webui" / "config-backups"):
        if d.is_dir():
            out += sorted(d.glob("*.bak"))
    return out


def test_save_rejects_multiline_cookie_value_before_any_write(ws_tmp, monkeypatch):
    """入参层面拦截(最早、最准): COOKIE 值里带换行 → 拒绝, **文件一个字节不变**, 也不产生备份。"""
    server = _patch_roots(monkeypatch, ws_tmp)
    p = _mk(ws_tmp)
    before = open(p, encoding="utf-8").read()
    body = {"config_file": "data/cat-a/config.yaml",
            "values": {"CAT": "cat-a",
                       "COOKIE": "session-id=1; ubid-main=2\nsession-id=9; ubid-main=8'"}, "lists": {}}
    with pytest.raises(Exception) as ei:
        server.Handler._api_config_save(None, body)
    msg = str(ei.value)
    assert "配置未保存" in msg and "COOKIE" in msg and "换行" in msg
    assert "一行" in msg, "要给可操作的改法"
    assert open(p, encoding="utf-8").read() == before, "校验失败时绝不能写盘"
    assert _backups(ws_tmp) == [], "校验失败时不该产生备份(什么都没改)"


def test_save_rejects_broken_two_line_cookie_shape(ws_tmp, monkeypatch):
    """真实事故形状: 值里同时含 `"` 与 `'` → 序列化成**单引号跨行**, 生成的文本 YAML 解析不过。

    这类"粘两行"的值必须在写盘前被拦下(旧实现会先写坏文件再报错)。
    """
    server = _patch_roots(monkeypatch, ws_tmp)
    p = _mk(ws_tmp)
    before = open(p, encoding="utf-8").read()
    body = {"config_file": "data/cat-a/config.yaml",
            "values": {"CAT": "cat-a",
                       "COOKIE": 'session-id=1; x-main="abc@def"\nsession-id=9; ubid-main=8\''},
            "lists": {}}
    with pytest.raises(Exception) as ei:
        server.Handler._api_config_save(None, body)
    assert "配置未保存" in str(ei.value)
    assert open(p, encoding="utf-8").read() == before


def test_guard_rejects_broken_two_line_text_shape():
    """文本层面的兜底(手工编辑/整文件保存): 复刻真事故的形状 —— 第一行是**完整**的 `COOKIE: '…'`,
    第二行又多出一段裸值(没有 `KEY:`) → 必须拒绝, 且提示指到裸值行。"""
    text = ("CAT: \"cat-a\"\n"
            "COOKIE: 'session-id=134-abc; x-main=\"p@q\"; session-id-time=2082787201l'\n"
            "session-id=134-abc; ubid-main=135; session-token=xyz'\n"
            "\n"
            "# 可选: 启动预检查时校验 COOKIE 有效性\n"
            "COOKIE_CHECK: true\n")
    msg = yaml_save_guard(text)
    assert msg is not None
    assert "第 3 行" in msg and "一行" in msg
    assert "不是病根" in msg, "要说明 YAML 报的位置不是病根"


def test_save_writes_and_backs_up_when_valid(ws_tmp, monkeypatch):
    """正常改动: 写盘 + 生成备份(顺序为先校验后写盘, 但结果与旧行为一致)。"""
    server = _patch_roots(monkeypatch, ws_tmp)
    p = _mk(ws_tmp)
    out = server.Handler._api_config_save(
        None, {"config_file": "data/cat-a/config.yaml",
               "values": {"CAT": "cat-a", "LONGTAIL_PER_DIM": "9"}, "lists": {}})
    assert out["ok"] is True
    assert "LONGTAIL_PER_DIM: 9" in open(p, encoding="utf-8").read()
    assert len(_backups(ws_tmp)) == 1, "保存成功应留下备份"


def test_save_rolls_back_when_post_write_validation_fails(ws_tmp, monkeypatch):
    """兜底分支: 写盘后校验若失败(模拟落盘异常) → 用刚做的备份**回滚**, 不留坏文件。"""
    server = _patch_roots(monkeypatch, ws_tmp)
    p = _mk(ws_tmp)
    before = open(p, encoding="utf-8").read()
    real = server.load_yaml_file

    def flaky(path):
        # 只在"新内容已经落盘之后"的那次读取失败, 精确落进兜底分支
        try:
            if "LONGTAIL_PER_DIM: 9" in Path(path).read_text(encoding="utf-8-sig"):
                raise ValueError("模拟落盘后解析异常")
        except OSError:
            pass
        return real(path)

    monkeypatch.setattr(server, "load_yaml_file", flaky)
    with pytest.raises(Exception) as ei:
        server.Handler._api_config_save(
            None, {"config_file": "data/cat-a/config.yaml",
                   "values": {"CAT": "cat-a", "LONGTAIL_PER_DIM": "9"}, "lists": {}})
    assert "回滚" in str(ei.value)
    assert open(p, encoding="utf-8").read() == before, "回滚后应恢复为保存前内容"
    assert _backups(ws_tmp), "回滚依赖的那份备份应当存在"
