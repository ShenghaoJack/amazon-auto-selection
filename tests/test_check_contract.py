# 单元测试: 跨步骤数据契约检查器(scripts/check_contract.py)
# ---------------------------------------------------------------------------
# 背景(2026-09-29): 该检查器原先**按文件名**取步骤号, 而 `steps/step17_backup_monitor.py`
# 的类声明是 `number = 20`(步骤20 备份+体检) —— 于是它拿 FR-17 的契约去检查步骤19 的代码,
# 恒报两条"契约产物 … 未在代码中出现"的假问题; 另外步骤18 继承步骤17(共享 STD_XLSX_TPL
# 等常量), 文件名字符串只写在步骤17 里, 也被报成"未在代码中出现"。
# 下面把这两条修复与"仓库当前必须干净"都钉住。
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_contract_mod",
                                                  ROOT / "scripts" / "check_contract.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def checker(monkeypatch):
    monkeypatch.chdir(ROOT)             # 检查器用相对路径 glob("steps/step*.py")
    return _load_checker()


def test_contract_checker_passes_on_repo(checker):
    """当前仓库必须零问题(这条就是"契约漂移"的看门狗)。"""
    assert checker.check() == []
    assert checker.main() == 0


def test_step_number_comes_from_class_declaration(checker):
    """步骤号取类声明 number=N, 不是文件名 —— step17_backup_monitor 必须是 20。"""
    backup = checker.read_text("steps/step17_backup_monitor.py")
    attr = checker.read_text("steps/step17_attr_annotation.py")
    assert checker.step_number("steps/step17_backup_monitor.py", backup) == "20"
    assert checker.step_number("steps/step17_attr_annotation.py", attr) == "17"
    # 没有 number 声明时回落文件名
    assert checker.step_number("steps/step09_search_collect.py", "x = 1") == "09"


def test_filenames_from_imported_parent_module(checker):
    """步骤18 的契约常量定义在步骤17(父类)里 —— 跟着 import 一起看才不误报。"""
    src = checker.read_text("steps/step18_standardization.py")
    extra = checker.imported_module_texts(src)
    assert extra, "步骤18 应当能从 steps.* 里找到被引用的模块源码"
    only_self = checker.extract_filenames(src)
    with_parent = checker.extract_filenames(src, extra)
    target = "CAT-属性标注-标准化交付.xlsx"
    assert target not in only_self, "该常量确实不在步骤18 自己的源码里(所以才需要跟 import)"
    assert target in with_parent


def test_comment_lines_are_ignored(checker):
    """注释行里出现的文件名不算数(否则文档里的示例会掩盖真实漂移)。"""
    found = checker.extract_filenames('# 参考: <CAT>-keywords.txt\n')
    assert "<CAT>-keywords.txt" not in found
    assert not found


def test_upstream_without_producer_is_reported(checker, monkeypatch):
    """上游引用没有生产者时必须报出来(检查器的核心价值)。"""
    monkeypatch.setitem(checker.CONTRACT, "16", [])
    monkeypatch.setitem(checker.UPSTREAM, "16", ["<CAT>-不存在的产物.json"])
    issues = checker.check()
    assert any("无对应生产者" in i and "不存在的产物" in i for i in issues)
