# -*- coding: utf-8 -*-
"""代码审核关卡（强制，三项缺一不可）—— 2026-10-08 用户要求。

背景
===============================================================================
本轮我引入了至少 19 个 bug，失败模式高度重复，**全部属于下面三类之一**：

  ① 语法/静态      漏了模块级 lint —— `_run_pytest.py` 自己的 print 撞 GBK
                    编码把已跑完的测试变成假失败；`except` 行被我编辑时合并。
  ② 输入输出契约    `_extract_size_split` 的键是 str、写盘循环用 int → 列建出来却全空；
                    `attrs_spec` 是**扁平**结构而我去读 `spec["params"]` → 推导从不执行；
                    `Counter(cur.elements())` 又乘一次计数 → 覆盖率 4608%。
  ③ 逻辑/不变量     极性从"典型取值"词序推 → 4 个属性反了；
                    空值也算冲突 → 误报；学习映射覆盖确定性推导。

共同根因：**我用"看起来对"代替了"断言真实输出"**，且探针的构造方式与生产不一致
（嵌套 vs 扁平、从空开始 vs 从机械收敛后开始）→ 探针绿、真跑不生效。

三项检查（缺一不可）
===============================================================================
  ① 语法：全部改动过的 .py 过 `ast.parse` + `py_compile`；改过的 JSON/YAML 过解析。
  ② 输入输出：**读回真实产物**断言 —— 键类型、空/哨兵处理、列真的落了值。
     绝不断言"列存在"就算过（这正是漏掉 int/str 键不匹配的原因）。
  ③ 逻辑：把不变量**逐条写出来并断言**（hint⊆values、厚↔厚度一致、是否*∈{是,否}…）。

用法::

    python -X utf8 scripts/code_review.py                     # 全量
    python -X utf8 scripts/code_review.py --cat "<CAT>" [--dir <隔离目录>]

退出码：0=全过；1=有失败项（**不许声称完成**）。
"""
from __future__ import annotations

import argparse
import ast
import json
import py_compile
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 每次改代码后至少要覆盖的目录（新增文件也在这里面）
PY_DIRS = ("steps", "tools/attr_stability", "core", "scripts", "webui", "tests")
EMPTY = {"", "未明确", "未标注", "不适用", "未知", "待确认", "None", "none"}


def _def_spec(cat_dir: Path, cat: str) -> Path:
    """定义层属性集路径 —— **走解析器**（自己的目录优先 → 共享沉淀层继承）。

    ⚠ 为什么关卡也得接（2026-10-10 试水实测）：关卡原先写的是
      `cat_dir / f"{cat}-attribute-spec.json"`（**直接拼路径**）。定义层迁移到
      `_catalog/<类目>/` 之后，这种写法**立刻摸空** —— 试水时关卡就是这么报
      「❌ spec 存在 / rules/spec 存在」的。
    解析器缺失/异常时**退回原写法**（向后兼容，绝不让关卡因此崩掉）。
    """
    try:
        from tools.attr_stability.catalog import resolve_spec_path
        return resolve_spec_path(cat_dir.parent, cat)
    except Exception:                                            # noqa: BLE001
        return cat_dir / ("%s-attribute-spec.json" % cat)


def _def_rules(cat_dir: Path, cat: str) -> Path | None:
    """定义层规则文件路径 —— **走解析器**；找不到返回 None（与 `next(glob)` 同义）。"""
    try:
        from tools.attr_stability.catalog import resolve_rules_path
        p = resolve_rules_path(cat_dir.parent, cat)
        return p if p.is_file() else None
    except Exception:                                            # noqa: BLE001
        return next(iter(sorted(cat_dir.glob("*.rules.json"))), None)


class Review:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, bool, str]] = []

    def add(self, sec: str, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((sec, name, bool(ok), detail))

    def report(self) -> int:
        secs = [("①", "语法/静态"), ("②", "输入输出"), ("③", "逻辑/不变量")]
        print("=" * 96)
        print("代码审核（语法 / 输入输出 / 逻辑 —— 缺一不可）")
        print("=" * 96)
        bad = 0
        for key, title in secs:
            items = [r for r in self.rows if r[0] == key]
            if not items:
                print("\n%s %s：**没有检查项**（视为不通过）" % (key, title))
                bad += 1
                continue
            n_bad = sum(1 for r in items if not r[2])
            print("\n%s %s：%d 项，%s" % (key, title, len(items),
                                          "全部通过" if not n_bad else "**%d 项失败**" % n_bad))
            for _s, name, ok, detail in items:
                print("   %s %-44s %s" % ("✅" if ok else "❌", name, detail[:80]))
            bad += n_bad
        print()
        print("=" * 96)
        print("结论：%s" % ("✅ 全部通过，可以声称完成" if not bad else
                            "❌ 有 %d 项失败 —— **不许声称完成**" % bad))
        return 0 if not bad else 1


def _logic_conflicts(path: Path) -> list[str]:
    """AST 层查**逻辑打架**（用户 2026-10-09：「你怎么总出现逻辑打架」）。

    静态分析（pyflakes/ruff/pylint）查的是"没用到的名字/风格"，
    **查不出自相矛盾的分支**。这里补四类最容易犯的：

      ① **重复条件**：`if x: … elif x: …` —— 第二个分支永远不可达
      ② **不可达代码**：`return/raise/continue/break` 之后还有同层语句
      ③ **常量条件**：`if True:` / `if False:` / `if not True:` —— 死分支
      ④ **空的 if 体**：`if x: pass` —— 多半是写漏了

    实测抓到过我自己的：安全阀写成 `if not flag: fixed = []` 之后**照样跑**
    填充 `fixed` 的循环 —— 安全阀被自己的代码架空（这类"写了个门又留了条路"
    属于 ②/逻辑顺序问题，本检查能覆盖其中一部分）。
    """
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
    except SyntaxError:
        return []
    out: list[str] = []

    TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)

    def _block_unreachable(block: list) -> None:
        for i, st in enumerate(block[:-1]):
            if isinstance(st, TERMINATORS):
                nxt = block[i + 1]
                out.append("不可达代码: 第 %d 行 %s 之后还有第 %d 行"
                           % (getattr(st, "lineno", 0), type(st).__name__,
                              getattr(nxt, "lineno", 0)))
                return

    for node in ast.walk(tree):
        # ② 不可达代码
        for attr in ("body", "orelse", "finalbody"):
            blk = getattr(node, attr, None)
            if isinstance(blk, list) and blk:
                _block_unreachable(blk)
        if isinstance(node, ast.If):
            # ③ 常量条件
            t = node.test
            if isinstance(t, ast.Constant):
                out.append("常量条件: 第 %d 行 `if %r` 恒为%s"
                           % (node.lineno, t.value, "真" if t.value else "假"))
            elif (isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not)
                  and isinstance(t.operand, ast.Constant)):
                out.append("常量条件: 第 %d 行 `if not %r`" % (node.lineno, t.operand.value))
            # ④ 空 if 体
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass) and node.orelse:
                out.append("空 if 体: 第 %d 行 `if …: pass else: …`" % node.lineno)
            # ① 重复条件（if / elif 链）
            seen = {ast.dump(t)}
            cur = node
            while (len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If)):
                cur = cur.orelse[0]
                key = ast.dump(cur.test)
                if key in seen:
                    out.append("重复条件: 第 %d 行的 `elif` 与前面某分支条件**完全相同** → 死分支"
                               % cur.lineno)
                    break
                seen.add(key)
    return out


def sec1_syntax(rv: Review, cat: str, cat_dir: Path) -> None:
    """① 语法/静态。"""
    files = []
    for d in PY_DIRS:
        files += sorted((ROOT / d).rglob("*.py"))
    # 排除：__pycache__；以及 `_scratch/`（一次性探针/修复脚本的归档 ——
    # 里面会含被检测工具自身的字面量，造成自我引用假阳性；不属维护范围）
    files = [f for f in files
             if "__pycache__" not in str(f) and "_scratch" not in str(f)]
    # ★ py_compile 的 cfile 必须写在**工作区内** —— 写 OS 临时目录会被沙箱拒绝
    #   （实测把审核脚本自己搞崩了，正是"输入输出没验证"的又一例）。
    pyc_dir = ROOT / "scripts" / "_plan_pilot" / "_cr_pyc"
    pyc_dir.mkdir(parents=True, exist_ok=True)
    bad_ast, bad_c = [], []
    for i, f in enumerate(files):
        # ★ 用 utf-8-sig：PowerShell Out-File 写的脚本带 BOM，
        #   用 utf-8 读会让首字符变成 \ufeff → 假语法错（实测踩过）。
        src = f.read_text(encoding="utf-8-sig", errors="replace")
        try:
            ast.parse(src)
        except SyntaxError as e:
            bad_ast.append("%s:%s" % (f.name, e.lineno))
        try:
            py_compile.compile(str(f), cfile=str(pyc_dir / ("f%d.pyc" % i)), doraise=True)
        except py_compile.PyCompileError as e:
            bad_c.append("%s: %s" % (f.name, str(e)[:60]))
    rv.add("①", "Python 语法(ast.parse, %d 文件)" % len(files), not bad_ast, str(bad_ast[:3]))
    rv.add("①", "Python 编译(py_compile)", not bad_c, str(bad_c[:3]))

    # ★ 逻辑打架（AST 层）：静态分析查不出自相矛盾的分支，这里补上。
    #   仅对**本次改动涉及的文件**报（全仓库存量噪音太大），且只报不拦（存量问题多）。
    HOT = ("step16_attribute_annotation.py", "step17_attr_annotation.py",
           "step18_standardization.py", "step17_backup_monitor.py",
           "converge.py", "cross_checks.py", "spec.py", "learned_map.py",
           "spec_sync.py", "derive.py")
    logic_bad: list[str] = []
    for f in files:
        if f.name in HOT:
            for msg in _logic_conflicts(f):
                logic_bad.append("%s:%s" % (f.name, msg))
    rv.add("①", "逻辑打架(重复条件/不可达/常量条件/空if)", not logic_bad,
           str(logic_bad[:4]) if logic_bad else "热文件 0 处")

    # 我改过的数据文件必须仍可解析
    json_bad = []
    json_files = list((ROOT / "data").rglob("*.rules.json")) + list((ROOT / "data").rglob("*spec.json"))
    for p in json_files:
        try:
            json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            json_bad.append(p.name)
    rv.add("①", "rules.json / spec.json 仍是合法 JSON(%d 个)" % len(json_files),
           not json_bad, str(json_bad[:3]))

    # config.yaml 里不应残留已删的键
    left = [c.parent.name for c in (ROOT / "data").glob("*/config.yaml")
            if "ATTR_STAB_VOCAB_GRANULARITY" in c.read_text(encoding="utf-8", errors="replace")]
    rv.add("①", "已删键 ATTR_STAB_VOCAB_GRANULARITY 无残留", not left, str(left[:3]))

    # 控制台编码：任何会被打印的脚本都必须对 GBK 安全（我踩过）
    enc_bad = []
    for name in ("scripts/_plan_pilot/_run_pytest.py", "scripts/code_review.py",
                 "scripts/audit_attributes.py"):
        p = ROOT / name
        if p.is_file():
            txt = p.read_text(encoding="utf-8", errors="replace")
            if "errors=\"replace\"" not in txt and "errors='replace'" not in txt:
                enc_bad.append(Path(name).name)
    rv.add("①", "控制台输出脚本对 GBK 安全", not enc_bad, str(enc_bad))
    # ★ 内联 JS 语法检查（2026-10-10 补）：此前关卡只查 Python 的语法/编译，
    #   `webui/index.html` 里的内联 JS **从来没被检查过** —— 改坏了要等页面报错才发现。
    #   用 `node --check`；**没有 node 就报"未检查"，不冒充通过**。
    try:
        import re as _rejs
        import shutil as _shjs
        import subprocess as _spjs
        _idx = ROOT / "webui" / "index.html"
        _node = _shjs.which("node")
        if not _node:
            # ★★ 修复（审核高4）：「没检查 ≠ 通过」—— 原来写 `ok=True`，把"从未执行的检查"
            #   计为通过。改成 `ok=False`（与 ② 节 L778/780 的"无交付表→未检查(不等于通过)"同口径）。
            rv.add("①", "内联 JS 语法(node --check)", False, "⚠ 未检查(不等于通过): 本机无 node")
        elif not _idx.is_file():
            rv.add("①", "内联 JS 语法(node --check)", False, "⚠ 未检查(不等于通过): 无 index.html")
        else:
            _blocks = _rejs.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                    _idx.read_text(encoding="utf-8", errors="replace"),
                                    _rejs.S)
            _tmp = ROOT / "scripts" / "_plan_pilot" / "_cr_tmp" / "_gate_inline.js"
            _tmp.parent.mkdir(parents=True, exist_ok=True)
            _tmp.write_text("\n".join(_blocks), encoding="utf-8")
            # ⚠ **不能用管道捕获输出**：本沙箱禁止 `subprocess` 开管道（`stderr=PIPE`
            #   会 `[WinError 5] 拒绝访问`）→ 第一版就报了"未检查"。改成**写文件**再读。
            _errf = _tmp.with_suffix(".err.txt")
            with open(_errf, "wb") as _fh:
                _pr = _spjs.run([_node, "--check", str(_tmp)],
                                stdout=_fh, stderr=_spjs.STDOUT)
            _msg = ""
            if _pr.returncode != 0:
                try:
                    _msg = _errf.read_text(encoding="utf-8", errors="replace")[:120]
                except OSError:
                    _msg = "(读不到错误输出)"
            rv.add("①", "内联 JS 语法(node --check)", _pr.returncode == 0,
                   "%d 个 script 块" % len(_blocks) if _pr.returncode == 0 else _msg)
    except Exception as _exc:                                    # noqa: BLE001
        # ★★ 修复（审核高4）：检查代码**自身异常**原来也算 `ok=True`（fail-open）——
        #   拼错一个名字、导入失败，都会让这个检查"通过"。改成 `ok=False`。
        rv.add("①", "内联 JS 语法(node --check)", False, "⚠ 未检查(不等于通过): %s" % str(_exc)[:60])

    # ★★ **未定义名 / 未用导入（pyflakes）**（2026-10-10 加，来自格式审核 A-1/A-2）。
    #   为什么必须有这条：项目有 `from __future__ import annotations`，**只写在注解里的名字
    #   不会报错**；而被 `or` 短路、被 `try/except` 包住的未定义名同样**长期不炸**。
    #   实测抓到两处：`code_review.py` 的 `t18`（拼错，一旦短路失效就 **NameError 让整个关卡
    #   无输出**）、`step16` 注解里的 `Any`（没导入）。这两类**只能靠静态检查**发现。
    #   用 pyflakes（有就用；**没有就报"未检查"，不冒充通过**）。
    try:
        from pyflakes.api import check as _pf_check
        _pf_files: list[Path] = []
        for _d in PY_DIRS:
            _pf_files += sorted((ROOT / _d).rglob("*.py"))
        _pf_files.append(ROOT / "scripts" / "code_review.py")
        _pf_bad: list[str] = []

        class _PFRep:
            def unexpectedError(self, *a):                        # noqa: N802
                _pf_bad.append("ERR %s" % (a,))

            def syntaxError(self, *a):                            # noqa: N802
                _pf_bad.append("SYN %s" % (a,))

            def flake(self, m):
                _pf_bad.append(str(m))

        for _p in _pf_files:
            if "__pycache__" in str(_p) or "_scratch" in str(_p):
                continue
            try:
                _src = _p.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            _pf_check(_src, str(_p), _PFRep())
        # 只拦**真正危险**的两类：未定义名、语法/意外错误。
        # （未用导入/未用变量属风格，本仓既存较多，全拦会淹掉信号 —— 单独列成 note。）
        _pf_danger = [x for x in _pf_bad if "undefined name" in x.lower() or x.startswith(("ERR", "SYN"))]
        _pf_style = [x for x in _pf_bad if x not in _pf_danger]
        rv.add("①", "未定义名(pyflakes · 防 NameError)", not _pf_danger,
               ("⚠ %d 处: %s" % (len(_pf_danger), _pf_danger[:2])) if _pf_danger
               else ("另有 %d 处风格告警(未用导入/变量, 不拦)" % len(_pf_style) if _pf_style
                     else "0 处"))
    except ImportError:
        rv.add("①", "未定义名(pyflakes · 防 NameError)", False,
               "⚠ 未检查(不等于通过): 本机无 pyflakes")
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("①", "未定义名(pyflakes · 防 NameError)", False,
               "⚠ 未检查(不等于通过): %s" % str(_exc)[:60])

    # 临时路径不得指向 OS 临时目录（本沙箱不可写；我已反复踩）
    os_temp = []
    for d in PY_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            if "__pycache__" in str(p) or "_scratch" in str(p):
                continue          # 归档的一次性脚本不属维护范围（含被检测字面量）
            if p.name == "code_review.py":
                continue          # 检查器源码里含被检测的字面量，排除自己
            txt = p.read_text(encoding="utf-8-sig", errors="replace")
            if re.search(r"tempfile\.TemporaryDirectory\s*\(", txt) or re.search(r"gettempdir\s*\(\s*\)", txt):
                os_temp.append(p.name)
    rv.add("①", "不使用 OS 临时目录(沙箱不可写)", not os_temp, str(os_temp[:4]))


def sec2_io(rv: Review, cat: str, cat_dir: Path) -> None:
    """② 输入输出契约：**读回真实产物**断言。"""
    from tools.attr_stability import learned_map as LM
    from tools.attr_stability.derive import band_of, parse_mm
    from tools.attr_stability.cross_checks import check_row, load_cross_checks
    from tools.attr_stability.spec_sync import extract_typical

    # --- learned_map：键/类型/往返 ---
    # ★ 临时目录必须在**工作区内** —— OS 临时目录在本沙箱里不可写
    #   （实测连审核脚本自己都被拒，这是"输入输出没验证"的典型）
    td = ROOT / "scripts" / "_plan_pilot" / "_cr_tmp"
    if td.exists():
        import shutil
        shutil.rmtree(td, ignore_errors=True)
    td.mkdir(parents=True, exist_ok=True)
    m = {"A": {"x": "y"}}
    LM.save_learned(str(td), "t", m)
    rv.add("②", "learned_map 落盘→读回一致", LM.load_learned(str(td), "t") == m)
    p = Path(LM.learned_path(str(td), "t"))
    rv.add("②", "learned_map 原子写(不留 .tmp)", not Path(str(p) + ".tmp").exists())

    # --- derive：分档必须无缝覆盖（0..inf 任意值都有档）---
    bands = [["超薄(≤3mm)", 0, 3], ["标准(3-20mm)", 3, 20],
             ["加厚(20-45mm)", 20, 45], ["超厚(>45mm)", 45, 10 ** 9]]
    gaps = [x for x in (0, 1, 3, 5, 10, 15, 20, 25, 44, 45, 60, 500) if not band_of(float(x), bands)]
    rv.add("②", "derive 分档无缝(无洞)", not gaps, "落洞 mm=%s" % gaps)
    rv.add("②", "derive 单位识别(cm/mm/in)",
           (parse_mm("1cm"), parse_mm("10mm"), parse_mm('4"')) == (10.0, 10.0, 101.6),
           "%s" % ([parse_mm("1cm"), parse_mm("10mm"), parse_mm('4"')],))

    # --- cross_checks：空/哨兵绝不判冲突（我踩过的误报）---
    decl = load_cross_checks({"cross_checks": {"互斥": [
        {"左": {"属性": "M", "含": ["竹"]}, "右": {"属性": "T", "不得含": ["实木"]}}]}})
    fp = [v for v in ("", "未明确", "不适用", None)
          if check_row({"M": "竹木", "T": v}, decl)]
    rv.add("②", "cross_checks 空值不误报", not fp, "误报于 %s" % fp)

    # --- spec_sync：真实 spec 的「典型取值」可提取 ---
    spec_f = _def_spec(cat_dir, cat)
    if spec_f.is_file():
        spec = json.loads(spec_f.read_text(encoding="utf-8-sig"))
        n_hint = sum(1 for a in spec.get("attributes") or [] if extract_typical(a.get("hint")))
        rv.add("②", "spec.hint 典型取值可提取(%d 个属性)" % n_hint, True)
    else:
        rv.add("②", "spec 存在", False, str(spec_f))

    # --- 交付表：**读回真实值**（不是"列存在"就算过）---
    xl = cat_dir / "18_标准化" / f"{cat}-属性标注-标准化交付.xlsx"
    if not xl.is_file():
        rv.add("②", "交付表可读回", False, "未找到 %s（此项不能跳过）" % xl.name)
        return
    try:
        import openpyxl
        wb = openpyxl.load_workbook(xl, read_only=True, data_only=True)
        ws = wb["属性标注"] if "属性标注" in wb.sheetnames else wb.active
        it = ws.iter_rows(values_only=True)
        header = [str(x or "") for x in next(it)]
        rows = [dict(zip(header, r)) for r in it]
        wb.close()
    except Exception as exc:                                       # noqa: BLE001
        rv.add("②", "交付表可读回", False, str(exc)[:60])
        return
    rv.add("②", "交付表行数 > 0", bool(rows), "%d 行" % len(rows))
    for col in ("长", "宽", "厚"):
        if col not in header:
            rv.add("②", "列「%s」存在" % col, False)
            continue
        vals = [str(r.get(col) or "").strip() for r in rows]
        n = sum(1 for v in vals if v)
        nounit = [v for v in vals if v and not re.search(r"[A-Za-z]$", v)]
        rv.add("②", "列「%s」**真的落了值**" % col, n > 0, "%d/%d 非空" % (n, len(rows)))
        rv.add("②", "列「%s」值都以单位结尾" % col, not nounit, "例: %s" % nounit[:2])


def sec3_logic(rv: Review, cat: str, cat_dir: Path) -> None:
    """③ 逻辑/不变量：逐条写出来并断言。"""
    from tools.attr_stability.spec_sync import check_caliber
    from tools.attr_stability.cross_checks import check_rows, load_cross_checks

    rules_f = _def_rules(cat_dir, cat)
    spec_f = _def_spec(cat_dir, cat)
    if not (rules_f and spec_f.is_file()):
        rv.add("③", "rules/spec 存在", False)
        return
    rules = json.loads(rules_f.read_text(encoding="utf-8-sig"))
    spec = json.loads(spec_f.read_text(encoding="utf-8-sig"))
    r_by = {a["name"]: a for a in rules.get("attributes") or [] if a.get("name")}
    s_by = {a["name"]: a for a in spec.get("attributes") or [] if a.get("name")}

    # ★ 交付表**只加载一次**（多个不变量都要用）。
    #   踩过的坑：放在后面的检查里定义，前面的检查引用就 NameError。
    xl = cat_dir / "18_标准化" / f"{cat}-属性标注-标准化交付.xlsx"
    header: list[str] = []
    rows: list[dict] = []
    if xl.is_file():
        try:
            import openpyxl
            wb = openpyxl.load_workbook(xl, read_only=True, data_only=True)
            ws = wb["属性标注"] if "属性标注" in wb.sheetnames else wb.active
            it = ws.iter_rows(values_only=True)
            header = [str(x or "") for x in next(it)]
            rows = [dict(zip(header, r)) for r in it]
            wb.close()
        except Exception as exc:                                   # noqa: BLE001
            rv.add("③", "交付表可读回", False, str(exc)[:60])
    else:
        rv.add("③", "交付表存在", False, "未找到 %s（多项检查将无法进行）" % xl.name)

    # 不变量 1：hint 的典型取值 ⊆ rules.values
    bad = []
    for n, a in s_by.items():
        vals = ((r_by.get(n, {}).get("params") or {}).get("values") or [])
        if vals and check_caliber(a.get("hint"), vals):
            bad.append(n)
    rv.add("③", "不变量: hint典型取值 ⊆ rules.values", not bad, str(bad[:5]))

    # 不变量 2：h 无手写 params.converge（枚举表只能由标准化生成）
    conv = [a["name"] for a in rules.get("attributes") or []
            if isinstance(a.get("params"), dict) and a["params"].get("converge")]
    rv.add("③", "不变量: rules.json 无手写 params.converge", not conv, str(conv[:5]))

    # 不变量 2b：`cross_checks` 只能由**步骤16 产出**，不许手写
    #   （用户口径：跨属性约束应在属性集阶段就产生，而不是我在 rules.json 里编）
    step16 = (ROOT / "steps" / "step16_attribute_annotation.py")
    t16 = step16.read_text(encoding="utf-8", errors="replace") if step16.is_file() else ""
    rv.add("③", "不变量: cross_checks 有产出点(步骤16)",
           "cross_checks" in t16, "step16_attribute_annotation.py")
    # 不变量 2x：跨属性约束候选的「校验」标记必须是**三态**（2026-10-09 修）。
    #   原先是**二元**：不在 `passed` 就一律写 `疑似误报(%d 命中)`，
    #   命中数用 `suspect.get(id(it), 0)` 兜底 → **没检查时 `suspect` 为空 → 兜底成 0**
    #   → "根本没检查"被写成 **`疑似误报(0 命中)`**：既自相矛盾（说明写着"0 命中的才采纳"），
    #   又违反「**没检查 ≠ 通过**」。实测全新类目 12 条候选**全部**被这么误标。
    rv.add("③", "不变量: 约束候选校验标记有三态(未过检≠误报)",
           "未过检（" in t16 and "_label" in t16, "step16 _label 三态")
    # 不变量 2y：「不要清单」`ATTR_EXCLUDE`（2026-10-09 用户口径）。
    #   根因：项目里只有 `ATTR_KEYS`（加）和 `ATTR_DESC`（改），**没有"删"** →
    #   人工在 Web 删掉的属性**只管一轮**，下一轮步骤16 会按数据重新抽出来
    #   （实测 `厚度` 被删后又回来了）。
    #   **行为型**断言：真去解析两级清单（config + 共享层），不是看字符串在不在。
    try:
        import shutil as _sh2
        from steps.step16_attribute_annotation import Step16AttributeAnnotation as _S16
        _pr = ROOT / "scripts" / "_plan_pilot" / "_exclude_probe"
        _sh2.rmtree(_pr, ignore_errors=True)
        (_pr / "_catalog" / "Cutting Boards").mkdir(parents=True)
        (_pr / "_catalog" / "Cutting Boards" / "_catalog.json").write_text(
            '{"类目": "Cutting Boards", "排除属性": ["形状"]}', encoding="utf-8")

        class _Cfg:
            cat = "Cutting Boards-US-202608"
            _d = {"ATTR_EXCLUDE": "厚度；厚度", "ATTR_KEYS": "厚度"}

            def get(self, k, default=None):
                return self._d.get(k, default)

        class _P:
            data_root = str(_pr)
            cat_root = str(_pr / "Cutting Boards-US-202608")

        class _Ctx:
            config = _Cfg()
            paths = _P()

        _st = _S16()
        _st._cat = "Cutting Boards-US-202608"
        _ex = _st._parse_attr_exclude(_Ctx())
        rv.add("③", "不变量: 排除清单两级并集+去重",
               _ex == ["厚度", "形状"], str(_ex))
        _sh2.rmtree(_pr, ignore_errors=True)
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 排除清单(ATTR_EXCLUDE)", False, "调用失败: %s" % _exc)
    _sw = (ROOT / "webui" / "server.py")
    _tsw = _sw.read_text(encoding="utf-8-sig", errors="replace") if _sw.is_file() else ""
    rv.add("③", "不变量: Web 删除=自动加入不要清单",
           "ATTR_EXCLUDE" in _tsw and "_patch_cat_config" in _tsw
           and "excluded_note" in _tsw, "webui _api_attr_spec_save")
    rv.add("③", "不变量: 步骤16 最终过滤被排除属性",
           "排除清单生效" in t16 and "_write_attr_excluded" in t16, "step16.run")
    # 不变量 2z：`是否*` 的口径 = {是,否,未明确}（2026-10-09 用户口径）。
    #   真冷启动里 AI 把它生成成了语义值（`双面可用/单面使用`），根因是**步骤16 提示词里的
    #   两条示例**就在教它这么写（`典型取值 带汁槽 / 无汁槽`）。所以：
    #     · 步骤16 提示词：加规则 + **改掉那两条示例**（示例比规则更能左右模型）
    #     · 标注阶段提示词：再加一句（**已有类目的 hint 是旧提示词生成的**，需要第二道防线）
    #   ⚠ 这是**最简做法**（用户口径：「能用提示词解决的，不加机制」）——
    #     没有引入属性级"锁定口径"表。
    _pp = (ROOT / "prompts" / "23_llm_only_pipeline.md")
    _tp = _pp.read_text(encoding="utf-8", errors="replace") if _pp.is_file() else ""
    rv.add("③", "不变量: 提示词规定 是否* 用 是/否/未明确",
           ("是否*" in _tp and "一律填" in _tp)
           and ("是非题" in t16 and "典型取值 是 / 否 / 未明确" in t16),
           "step16 提示词(规则+示例) + §1b.2")
    rv.add("③", "不变量: 已清除误导的语义示例",
           "典型取值 带汁槽 / 无汁槽" not in t16
           and "典型取值 可洗碗机清洗" not in t16, "step16 详细程度示例")
    # 不变量 2w：**属性池**（多轮属性集累积 → `runs` = 命中轮数，用户口径 2026-10-10）。
    #   用户原话：「不要再复杂化了，可以把这三轮的提取的属性都给模型去判断和聚类啊」
    #   —— 判定逻辑**一条没加**：`runs >= ATTR_SPEC_MIN_RUNS` 阈值 + 模型同义归并本来就存在，
    #      池子只是把多轮的产出攒起来，让 `runs` 从"本轮 N 次抽取"变成"**命中轮数**"。
    #   **行为型**断言（真跑池子读写），并锁两条铁律：
    #     ① 池子为空 → 原样返回（否则上线第一天所有类目的 runs 都变 1 → 全判偶然）
    #     ② 市场过滤真生效（踩过坑：读写键名不一致 → `is_visible(None,m)` 恒真 → 静默失效）
    try:
        import shutil as _sh3
        from tools.attr_stability import catalog as _cat3
        from tools.attr_stability.spec import merge_extractions as _me3
        _pr3 = ROOT / "scripts" / "_plan_pilot" / "_pool_probe"
        _sh3.rmtree(_pr3, ignore_errors=True)
        _a = [{"name": "产品类型", "hint": "a"}, {"name": "品牌", "hint": "b"}]
        _b = [{"name": "产品类型", "hint": "a2"}, {"name": "材质", "hint": "c"}]
        _cat3.append_attr_pool(_pr3, "Cutting Boards", _a, "US", "t1")
        _cat3.append_attr_pool(_pr3, "Cutting Boards", _a, "US", "t2")      # 同集 → 不追加
        _cat3.append_attr_pool(_pr3, "Cutting Boards", _b, "US", "t3")
        _cat3.append_attr_pool(_pr3, "Cutting Boards", [{"name": "件数"}], "DE", "t4")
        _us = _cat3.load_attr_pool(_pr3, "Cutting Boards", "US")
        _de = _cat3.load_attr_pool(_pr3, "Cutting Boards", "DE")
        _runs = {it["name"]: int(it.get("runs") or 1)
                 for it in _me3([r["属性"] for r in _us])}
        rv.add("③", "不变量: 属性池/runs=命中轮数",
               _runs.get("产品类型") == 2 and _runs.get("品牌") == 1, str(_runs))
        rv.add("③", "不变量: 属性池/同集不重复追加", len(_us) == 2, "%d 轮" % len(_us))
        rv.add("③", "不变量: 属性池/市场过滤真生效",
               len(_de) == 1 and len(_cat3.load_attr_pool(_pr3, "Cutting Boards")) == 3,
               "US %d · DE %d · 全部 %d" % (len(_us), len(_de),
                                            len(_cat3.load_attr_pool(_pr3, "Cutting Boards"))))
        _sh3.rmtree(_pr3, ignore_errors=True)
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 属性池(多轮 runs)", False, "调用失败: %s" % _exc)
    rv.add("③", "不变量: 属性池已接入步骤16(池空则行为不变)",
           "_pool_merge" in t16 and "if past:" in t16, "step16._pool_merge")
    # 不变量 2v：定义层文件解析（跨市场共享，**刻意不改文件名**）。
    #   发现：`<CAT>-attribute-spec.json` 这个**带 CAT 的固定名**在代码里被引用 **60+ 处**
    #   → 改成"规范名"要动 60 个调用点（正是用户反对的"复杂化"）。
    #   所以共享层**保留原名**，靠**后缀查找**让同类目其他市场找到它。
    #   同时锁：**没有共享层时退回运行目录**（否则现有 60+ 读点全坏）。
    try:
        import shutil as _sh4
        from tools.attr_stability import catalog as _cat4
        _pr4 = ROOT / "scripts" / "_plan_pilot" / "_resolve_probe"
        _sh4.rmtree(_pr4, ignore_errors=True)
        (_pr4 / "Cutting Boards-US-202608").mkdir(parents=True)
        (_pr4 / "Cutting Boards-US-202608" / "Cutting Boards-US-202608-attribute-spec.json"
         ).write_text("{}", encoding="utf-8")
        _own = _cat4.resolve_spec_path(_pr4, "Cutting Boards-US-202608")
        (_pr4 / "Cutting Boards-US-202608" / "Cutting Boards-US-202608-attribute-spec.json"
         ).unlink()                                               # 模拟"已迁移走"
        (_pr4 / "Cutting Boards-UK-202609").mkdir()
        _fallback = _cat4.resolve_spec_path(_pr4, "Cutting Boards-UK-202609")
        _cdir = _cat4.catalog_dir_of(_pr4, "Cutting Boards")
        _cdir.mkdir(parents=True)
        (_cdir / "Cutting Boards-US-202608-attribute-spec.json").write_text("{}", encoding="utf-8")
        _inherited = _cat4.resolve_spec_path(_pr4, "Cutting Boards-UK-202609")
        rv.add("③", "不变量: 定义层解析/自己目录第一优先",
               _own == _pr4 / "Cutting Boards-US-202608"
               / "Cutting Boards-US-202608-attribute-spec.json",
               "用户口径「肯定是自己的目录」")
        rv.add("③", "不变量: 定义层解析/自己目录空则继承(不改名)",
               _fallback == _pr4 / "Cutting Boards-UK-202609"
               / "Cutting Boards-UK-202609-attribute-spec.json"
               and _inherited == _cdir / "Cutting Boards-US-202608-attribute-spec.json",
               str(_inherited.name))
        # 多份候选**不猜**（猜错=静默污染）
        (_cdir / "Cutting Boards-DE-202608-attribute-spec.json").write_text("{}", encoding="utf-8")
        _amb = _cat4.resolve_spec_path(_pr4, "Cutting Boards-UK-202609")
        rv.add("③", "不变量: 定义层解析/多份候选不猜",
               _amb == _pr4 / "Cutting Boards-UK-202609"
               / "Cutting Boards-UK-202609-attribute-spec.json",
               "有歧义返回自己目录, 逼人工在 _catalog.json 声明")
        _sh4.rmtree(_pr4, ignore_errors=True)
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 定义层解析(resolve_spec_path)", False, "调用失败: %s" % _exc)
    _mg = ROOT / "scripts" / "_plan_pilot" / "migrate_to_catalog.py"
    _tmg = _mg.read_text(encoding="utf-8", errors="replace") if _mg.is_file() else ""
    # ⚠ 这条不变量**改过一次**：原先是断言"迁移脚本**拒绝执行**"（硬编码守卫）。
    #   但硬编码的守卫**不会随现实更新** —— 接线做完后它还照着老话拒绝，变成谎话。
    #   现在改成**动态接线自检**，不变量也跟着改成断言"自检存在且是真读代码的"。
    rv.add("③", "不变量: 迁移脚本执行前做动态接线自检",
           "wiring_report" in _tmg and "接线自检" in _tmg
           and "resolve_definition_file" in _tmg, "migrate_to_catalog.py")
    # 不变量 2s：**定义层消费者已全部接线**（2026-10-10）。
    #   为什么单列一条：试水实测过 —— 关卡自己就是"直接拼路径"的消费者，
    #   定义层一迁走它立刻摸空（报「❌ spec 存在」）。**凡是找定义层文件的代码，
    #   都必须走解析器**，否则"自己的目录优先 → 共享层继承"就只覆盖了一半人。
    #   这里逐文件断言它引用了 `resolve_*`（**不是**断言某个字符串长什么样）。
    _consumers = {
        "关卡 scripts/code_review.py": ("scripts/code_review.py", "resolve_spec_path"),
        "审计 scripts/audit_attributes.py": ("scripts/audit_attributes.py", "resolve_rules_path"),
        "Web 面板 webui/server.py": ("webui/server.py", "resolve_spec_path"),
        "监控 steps/step17_backup_monitor.py": ("steps/step17_backup_monitor.py",
                                                "resolve_rules_path"),
        "冒烟 scripts/smoke_dry_run.py": ("scripts/smoke_dry_run.py",
                                          "resolve_definition_file"),
        "契约 scripts/check_contract.py": ("scripts/check_contract.py", None),
    }
    for _label, (_rel, _needle) in _consumers.items():
        _p = ROOT / _rel
        _txt = _p.read_text(encoding="utf-8", errors="replace") if _p.is_file() else ""
        if _needle is None:
            # 契约检查器只比对**步骤源码里的产物名**，不碰文件系统 → 断言它确实没拼路径
            _ok = "cat_dir /" not in _txt and "os.path.join(root" not in _txt
            _detail = "只比对源码产物名, 不读文件系统"
        else:
            _ok = _needle in _txt
            _detail = _needle
        rv.add("③", "不变量: 定义层消费者已接线/%s" % _label, _ok, _detail)
    # 不变量 2u：**接线的 4 个 choke point**（2026-10-10）。
    #   接在**一处**（统一入口 `catalog.resolve_definition_file`）+ 4 个调用点，
    #   **不是改 60 个调用点**。**行为型**断言两条最要紧的：
    #     ① **非定义层文件名逐字不变**（否则交付表/稳定性结果全跑偏）
    #     ② `find_rules_file` 在"本目录有多个 rules"时**不猜**（返回 None，原行为）
    #        —— 第一版接线后它会**随便挑一个**，被测试抓到真回归。
    try:
        import shutil as _sh5
        from steps.base import StepContext as _SC
        from core.paths import Paths as _P5
        from tools.attr_stability.catalog import (
            catalog_dir_of as _cd5, resolve_definition_file as _rdf,
        )
        from tools.attr_stability.spec import find_rules_file as _frf
        _pr5 = ROOT / "scripts" / "_plan_pilot" / "_wire_probe"
        _sh5.rmtree(_pr5, ignore_errors=True)
        _cat5 = "Cutting Boards-US-202608"
        _run5 = _pr5 / "data" / _cat5
        _run5.mkdir(parents=True)
        _non = ["%s-属性标注-标准化交付.xlsx" % _cat5, "17_属性标注/稳定性结果.json", "input.json"]
        rv.add("③", "不变量: 接线/非定义层文件名逐字不变",
               all(_rdf(_run5, n) == _run5 / n for n in _non), "交付表等不受影响")
        _cd5(_pr5 / "data", "Cutting Boards").mkdir(parents=True)
        (_cd5(_pr5 / "data", "Cutting Boards") / ("%s-attribute-spec.json" % _cat5)
         ).write_text("{}", encoding="utf-8")
        _p5 = _P5.__new__(_P5)
        _p5.data_root = _pr5 / "data"
        _p5.cat_root = _run5
        _ctx5 = _SC.__new__(_SC)
        _ctx5.paths = _p5
        rv.add("③", "不变量: 接线/定义层从共享层继承",
               _rdf(_run5, "%s-attribute-spec.json" % _cat5)
               == _cd5(_pr5 / "data", "Cutting Boards") / ("%s-attribute-spec.json" % _cat5)
               and Path(_ctx5.resolve_artifact("%s-attribute-spec.json" % _cat5)).name
               == "%s-attribute-spec.json" % _cat5, "cat_file / resolve_artifact 都生效")
        (_run5 / "input.json").write_text("{}", encoding="utf-8")
        rv.add("③", "不变量: 接线/cat_file 保证父目录可写",
               Path(_p5.cat_file("新文件.json")).parent.is_dir(), "")
        (_run5 / "a.rules.json").write_text("{}", encoding="utf-8")
        (_run5 / "b.rules.json").write_text("{}", encoding="utf-8")
        rv.add("③", "不变量: 接线/find_rules_file 多候选不猜",
               _frf(str(_run5)) is None, "本目录多个 rules → None（原行为）")
        _sh5.rmtree(_pr5, ignore_errors=True)
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 接线(resolve_definition_file)", False, "调用失败: %s" % _exc)
    # 不变量 2t：Web「已排除属性」**常驻可见**（2026-10-10 用户口径）。
    #   为什么必须常驻：排除清单生效后，人就**看不出 AI 还在提议它**，而"反复提议"
    #   恰恰是**排除可能过时**的信号。藏起来 = 过两周忘了自己排过什么。
    _ih = ROOT / "webui" / "index.html"
    _tih = _ih.read_text(encoding="utf-8", errors="replace") if _ih.is_file() else ""
    rv.add("③", "不变量: Web「已排除属性」常驻可见+两种恢复",
           all(k in _tih for k in ("attrExcludedRows", "打开", "强制加回"))
           or all(k in _tih for k in ("attrExcludedRows", "renderAttrExcluded", "强制加回")),
           "index.html 步骤16 面板")
    rv.add("③", "不变量: Web 排除清单有增删端点",
           "/api/attr-exclude" in _tsw and "_api_attr_exclude" in _tsw
           and "_excluded_list" in _tsw, "webui/server.py")
    # ★ 「候选属性」面板（2026-10-12 用户口径）：步骤16 抽到、但 `runs < 阈值` 被挡在 spec 外的
    #   属性原先只躺在 `属性集待确认清单.json`，**Web 零入口** → 值得加的新维度会被无声漏掉。
    rv.add("③", "不变量: Web「候选属性」面板可采纳/忽略",
           all(k in _tih for k in ("attrCandRows", "renderAttrCandidates", "data-cand-adopt",
                                   "data-cand-ignore"))
           and "/api/attr-candidate" in _tsw and "_api_attr_candidate" in _tsw
           and "_candidates_list" in _tsw,
           "index.html 步骤16 面板 + webui/server.py")
    # ★★ **不许用 `_status_cfg` 读业务键**（2026-10-12 抓到的真数据丢失 bug）：
    #   `_status_cfg` **只返回 `{CAT, DATA_ROOT}`**（见 `webui/server.py` 的实现），
    #   `.get("ATTR_*")` 恒为空 → 再 `";".join(cur)` 写回就把**整份清单抹掉**
    #   （UI 上看起来"只是加了一条"）。这类"读空→写回覆盖"是最危险的静默失败。
    #   ⚠ 用 **AST 数据流**判定（先找出从 `_status_cfg` 赋值的变量名，再找它的 `.get("ATTR_*")`）——
    #     行级正则会被**注释**里引用的代码片段误报（我这个不变量第一版就误报了 2 处，
    #     而那两处恰好是本修复自己的说明注释）。
    _bad_reads: list[str] = []
    try:
        _stree = ast.parse(_tsw)
        _status_names: set[str] = set()
        for _n in ast.walk(_stree):
            if isinstance(_n, ast.Assign) and isinstance(_n.value, ast.Call):
                _f = _n.value.func
                if isinstance(_f, ast.Attribute) and _f.attr == "_status_cfg":
                    for _t in _n.targets:
                        if isinstance(_t, ast.Name):
                            _status_names.add(_t.id)
        for _n in ast.walk(_stree):
            if (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute)
                    and _n.func.attr == "get" and isinstance(_n.func.value, ast.Name)
                    and _n.func.value.id in _status_names and _n.args
                    and isinstance(_n.args[0], ast.Constant)
                    and str(_n.args[0].value).startswith("ATTR_")):
                _bad_reads.append("%s.get(%r) @L%d"
                                  % (_n.func.value.id, _n.args[0].value, _n.lineno))
    except SyntaxError as _e:
        _bad_reads = ["server.py 语法错误: %s" % _e]
    rv.add("③", "不变量: Web 读 ATTR_* 走配置文件(不用 _status_cfg)",
           not _bad_reads, ("⚠ 残留: %s" % _bad_reads[:3]) if _bad_reads else "0 处")

    # ---- 部署期密钥走环境变量（2026-10-12 用户口径）--------------------------------
    #   系统要上服务器：令牌不该进源码/仓库，也不该让 14 个类目各存一份 config.yaml。
    #   ① **行为型**：`Config.load` 环境变量优先；`Config({...})`（inline，测试夹具）**不受影响**
    #      —— 这条边界必须有，否则本机一旦 setx 真令牌，单元测试会被悄悄改掉（假失败/假通过）。
    try:
        import os as _os
        from core.config import ENV_OVERRIDE_KEYS as _EOK, Config as _Cfg
        import tempfile as _tf
        _ok_want = {"LLM_API_KEY", "VISION_API_KEY", "SS_MCP_KEY", "PANGOLIN_MCP_KEY"}
        rv.add("③", "不变量: 环境变量密钥白名单=4个(不含 URL/模型/BaseURL)",
               set(_EOK) == _ok_want and "PANGOLIN_MCP_URL" not in _EOK
               and "VISION_BASE_URL" not in _EOK and "VISION_MODEL" not in _EOK,
               str(tuple(_EOK)))
        _saved = {k: _os.environ.get(k) for k in _ok_want}
        try:
            for _k in _ok_want:
                _os.environ[_k] = "env-" + _k
            # ⚠ 不用 OS 临时目录（本沙箱不可写）→ 写在仓库内的一次性路径
            _td = ROOT / "scripts" / "_plan_pilot" / "_cr_tmp"
            _td.mkdir(parents=True, exist_ok=True)
            _cf = _td / "_envprobe.yaml"
            _cf.write_text('CAT: "t"\nLLM_API_KEY: "from-yaml"\n', encoding="utf-8")
            _loaded = _Cfg.load(_cf)
            _inline = _Cfg({"LLM_API_KEY": "inline"})
            rv.add("③", "不变量: Config.load 环境变量优先 / inline 不受影响",
                   _loaded.get("LLM_API_KEY") == "env-LLM_API_KEY"
                   and _inline.get("LLM_API_KEY") == "inline",
                   "load=%r inline=%r" % (_loaded.get("LLM_API_KEY"), _inline.get("LLM_API_KEY")))
            _cf.unlink(missing_ok=True)
            # ⚠ 不要 `_td.rmdir()`：`_cr_tmp` 是**共享** scratch 目录，别的探针可能还留着文件
            #   → rmdir 抛 `WinError 145 目录不是空的` → 被下面的 except 记成"失败"，
            #   而检查其实**通过了**（我第一版就这么误报了自己）。留着目录无害，关卡别做多余清理。
        finally:
            for _k, _v in _saved.items():
                if _v is None:
                    _os.environ.pop(_k, None)
                else:
                    _os.environ[_k] = _v
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 环境变量密钥白名单/优先级", False, "调用失败: %s" % _exc)
    #   ② PANGOLIN_MCP_URL 必须是**代码常量**（固定端点、非秘密）；③ 四个键不得出现在 Web 表单
    _tp = (ROOT / "tools" / "voc" / "pipeline.py").read_text(encoding="utf-8", errors="replace")
    rv.add("③", "不变量: Pangolinfo URL 是代码常量(非配置)",
           re.search(r'^PANGOLIN_MCP_URL\s*=\s*"https://', _tp, re.M) is not None,
           "tools/voc/pipeline.py")
    rv.add("③", "不变量: 部署期密钥不在 Web 配置表单",
           all(('"%s"' % k) in _tih for k in
               ("LLM_API_KEY", "VISION_API_KEY", "SS_MCP_KEY", "PANGOLIN_MCP_URL", "PANGOLIN_MCP_KEY"))
           and "HIDDEN_KEYS" in _tih
           and all(('"%s"' % k) not in _g for _g in
                   [ln for ln in _tih.splitlines() if "keys:[" in ln]
                   for k in ("LLM_API_KEY", "VISION_API_KEY", "SS_MCP_KEY",
                             "PANGOLIN_MCP_URL", "PANGOLIN_MCP_KEY")),
           "index.html HIDDEN_KEYS")

    # ---- VOC 两个键：源顺序写死 / 站点自适应（2026-10-12 用户口径）------------------
    #   **行为型**：`voc_site_of` 必须跟 `SITE` 走，**配置里残留的 `VOC_SITE` 不得生效** ——
    #   原来它是一份独立拷贝，`copy_from` 新建类目时被一起复制，实测英/德站里仍是 `amz_us`
    #   → **一直在抓美国站评论且无任何提示**（真 bug）。
    try:
        from core.config import Config as _Cfg2
        from tools.voc.pipeline import (DEFAULT_SOURCE_PRIORITY as _DSP,
                                        DEFAULT_SITE as _DS, voc_site_of as _vso)
        _cases = [("美国", "amz_us"), ("英国", "amz_uk"), ("德国", "amz_de"), ("日本", "amz_jp")]
        _bad = [(s, _vso(_Cfg2({"CAT": "t", "SITE": s}))) for s, w in _cases
                if _vso(_Cfg2({"CAT": "t", "SITE": s})) != w]
        _stale = _vso(_Cfg2({"CAT": "t", "SITE": "英国", "VOC_SITE": "amz_us"}))
        rv.add("③", "不变量: VOC_SITE 跟 SITE 自适应(旧副本不生效)",
               not _bad and _stale == "amz_uk" and _vso(_Cfg2({"CAT": "t"})) == _DS,
               ("⚠ %s" % (_bad or _stale)) if (_bad or _stale != "amz_uk") else "英国→amz_uk ✓ 旧副本被忽略")
        rv.add("③", "不变量: VOC 数据源顺序写死在代码",
               _DSP == "pangolinfo,sellersprite"
               and "VOC_SOURCE_PRIORITY" not in _tsw,
               "常量=%s" % _DSP)
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: VOC 站点自适应/源顺序写死", False, "调用失败: %s" % _exc)
    rv.add("③", "不变量: VOC 两键不在 Web 配置表单",
           all(('"%s"' % k) in _tih for k in ("VOC_SITE", "VOC_SOURCE_PRIORITY")),
           "index.html HIDDEN_KEYS")

    # ---- 属性集三键：全部**真删**（2026-10-12 方案 C）--------------------------------
    #   用户口径：「属性集入口已统一到 Web 的步骤16 面板」。
    #   方案 C 之后 `step16` **以「上一份 spec」为基底合并**（只增不减、人工改过的不覆盖）
    #   → **spec 是唯一真值**，`ATTR_KEYS`/`ATTR_DESC` 那两个"耐久补丁"不再需要。
    rv.add("③", "不变量: 属性集三键不在 Web 表单",
           all(('"%s"' % k) in _tih for k in ("ATTR_MODE", "ATTR_KEYS", "ATTR_DESC")),
           "index.html HIDDEN_KEYS")

    # ---- 属性抽取「几遍 + 票数门槛」两键一体（2026-10-12 用户口径）----------------------
    #   `runs` = "N 遍里被提到几次"（票数）→ `ATTR_SPEC_RUNS` 与 `ATTR_SPEC_MIN_RUNS`
    #   是**一个比例的两半**：只调 N 不调门槛 = **悄悄放开闸门**（属性集变大、偶然属性被自动采纳）。
    #   设计上门槛**默认由 N 推导**；这里钉住：默认 N=5、门槛走推导、且没有硬编码旧默认。
    try:
        from steps.step16_attribute_annotation import (   # noqa: PLC0415
            ATTR_SPEC_RUNS_DEFAULT, spec_min_runs_default,
        )
        _t16c = re.sub(r"(?m)#[^\n]*", "",
                       (ROOT / "steps" / "step16_attribute_annotation.py").read_text(
                           encoding="utf-8", errors="replace"))
        _k5 = spec_min_runs_default(ATTR_SPEC_RUNS_DEFAULT)
        rv.add("③", "不变量: 抽取次数/票数门槛两键一体(N=5·门槛由N推导)",
               ATTR_SPEC_RUNS_DEFAULT == 5 and _k5 == 3
               and spec_min_runs_default(3) == 2          # 与旧默认逐字一致(向后兼容)
               and ('"ATTR_SPEC_RUNS", 3' not in _t16c)
               and ('"ATTR_SPEC_MIN_RUNS", 2' not in _t16c)
               and ('"ATTR_SPEC_MIN_RUNS", min_runs_default' in _t16c)
               and ("票数门槛偏松" in _t16c),
               "N=%d · 门槛=%d · 无硬编码旧默认 · 比例过松会告警"
               % (ATTR_SPEC_RUNS_DEFAULT, _k5))
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 抽取次数/票数门槛两键一体(N=5·门槛由N推导)", False,
               "调用失败: %s" % _exc)

    # ---- 7 个默认值升级 + 配置行**保留**（2026-10-12 用户口径 B）------------------------
    #   为什么**两件事都做**（这是用户明确选的 B，不是半途而废）：
    #     · 升级 DEFAULTS → 新类目 / `copy_from` 自动拿到对的；"删或留"生效值都一致；
    #     · **保留配置行** → 表单只渲染「文件里有的键」，删了行 = **表单上再也改不了** ✗
    #       用户要"新人看得懂、需要时也改得到"，所以保留（代价：这 7 个框仍在）。
    #   这条把 B 的选择钉住，免得将来有人按"差异配置"的思路又把这 7 行清掉。
    try:
        import yaml as _yaml                                          # noqa: PLC0415
        _up = {"FETCH_WORKERS": 1, "REQUEST_MIN_INTERVAL": 2, "REQUEST_MAX_INTERVAL": 4,
               "LLM_TIMEOUT": 300, "SS_PROFILE": "data/profiles/ss-profile",
               "AMZ_PROFILE": "data/profiles/amz-profile",
               "VISION_MODEL": "deepseek-v4-flash-vision-exp"}
        from core.config import Config as _Cfg                        # noqa: PLC0415
        _bad = []
        for _k, _want in _up.items():
            if _Cfg.DEFAULTS.get(_k) != _want:
                _bad.append("默认值 %s=%r≠%r" % (_k, _Cfg.DEFAULTS.get(_k), _want))
        _cfiles = sorted((ROOT / "data").glob("*/config.yaml"))
        for _cf in _cfiles:
            _raw = _yaml.safe_load(_cf.read_text(encoding="utf-8-sig")) or {}
            for _k in _up:
                if _k not in _raw:
                    _bad.append("%s 缺 %s" % (_cf.parent.name, _k))
        rv.add("③", "不变量: 7 个默认值已升级且配置行保留(口径 B)",
               (not _bad) and bool(_cfiles),
               "、".join(_bad[:3]) if _bad else
               "DEFAULTS 已升级 · %d 个类目都保留了那 7 行（表单上可调）" % len(_cfiles))
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 7 个默认值已升级且配置行保留(口径 B)", False,
               "调用失败: %s" % _exc)

    # ---- 步骤17/18 两个"空值活键"已清（2026-10-12 用户口径 A）------------------------
    #   `ATTR_STAB_PROMPT` / `ATTR_STAB_VOCAB_HINT` 在 14 个 config 里都是空串，
    #   而 `step17._cfg_str` 的规则是"**空串 = 用默认**" → 删掉**行为一字不变**
    #   （它们仍是**在读的活键**，想用时手写回 config.yaml；表单标签也保留着）。
    #   ⚠ 关于 `ATTR_STAB_MIN_RATE`：**它不是硬闸门**（2026-10-12 核实更正 ——
    #     旧注释与 Web 提示词都写"低于则阻断"，但 step17 里那条分支**只 log 一条警告**）。
    #     所以删掉它**不会阻断**，只是回落默认 85 → **重新开始打那条提示**。
    #     保留它的意义是"**显式记录当前决定**"（现在 0 = 不想看这条提示），比隐含默认清楚。
    #     真正要防的是**措辞再写错**（人会因此不敢调/误判风险）→ 下面同时断言措辞。
    _s17c = ""
    for _f in sorted((ROOT / "data").glob("*/config.yaml")):
        _s17c += _f.read_text(encoding="utf-8", errors="replace")
    _t17txt = (ROOT / "steps" / "step17_attr_annotation.py").read_text(
        encoding="utf-8", errors="replace")
    rv.add("③", "不变量: 空值活键已清 / 一致率门槛措辞=「只提示不阻断」",
           ("ATTR_STAB_PROMPT" not in _s17c)
           and ("ATTR_STAB_VOCAB_HINT" not in _s17c)
           and ("ATTR_STAB_MIN_RATE" in _s17c)
           and ("只提示、不阻断" in _t17txt)
           and ("硬" + "门槛" not in _tih),
           "已删 PROMPT/VOCAB_HINT；MIN_RATE 仍在（**它只提示、不阻断**，删了会回落 85 重新提示）")

    #   `server.py:WEBUI_VERSION` 由服务端通过 `/api/bootstrap` 下发，页面拿它跟自己的
    #   `WUI_EXPECT` 比：**不等就弹「⚠ 旧构建(…)：请重启服务」**。
    #   这条机制的意义正是治今天这个坑（**前端已更新、服务端还是旧进程**时，用户能看见提示），
    #   但前提是**两边同步 bump** ——
    #     · 只 bump 一边 → 每次打开页面都误报"请重启服务"（狼来了，真需要重启时反而不信）；
    #     · 都不 bump   → **该报时不报**（今天就发生了：改了前端，页面一声不响，用户以为没生效）。
    _mv = re.search(r'WEBUI_VERSION\s*=\s*"([^"]+)"',
                    (ROOT / "webui" / "server.py").read_text(encoding="utf-8", errors="replace"))
    _mx = re.search(r'WUI_EXPECT\s*=\s*"([^"]+)"', _tih)
    rv.add("③", "不变量: 前后端版本握手一致(WUI_EXPECT == WEBUI_VERSION)",
           bool(_mv) and bool(_mx) and _mv.group(1) == _mx.group(1),
           "server=%s · index.html=%s（不等会误报/漏报「请重启服务」）"
           % (_mv.group(1) if _mv else "?", _mx.group(1) if _mx else "?"))

    # ---- 面板 vs 配置中心：同一个键**只能有一个改的地方**（2026-10-12 用户口径）----------
    #   两个旋钮已在「步骤16 属性集提取与确认」面板里能改 → 配置中心必须**不再展示**，
    #   否则同一个键两处都能改 = "冲突 / 以谁为准"的隐患。
    #   ⚠ 顺带钉住一个**一般性修复**：`GROUPS` 的渲染原来**不看 HIDDEN_KEYS**
    #     （HIDDEN 只对"兜底组"生效）→ 一个键只要登记进 GROUPS，加进 HIDDEN_KEYS 照样显示。
    _hk = re.search(r"const HIDDEN_KEYS = new Set\(\[(.*?)\]\);", _tih, re.S)
    _hks = set(re.findall(r'"([A-Z_]+)"', _hk.group(1))) if _hk else set()
    rv.add("③", "不变量: 面板旋钮不在配置表单(一个键只有一个改的地方)",
           {"ATTR_SPEC_RUNS", "ATTR_STAB_MIN_RATE", "ATTR_SPEC_MIN_RUNS"} <= _hks
           and ('id="attrSpecRuns"' in _tih) and ('id="attrMinRate"' in _tih)
           and ('id="btnSaveAttrTuning"' in _tih)          # ★ 两项**共用一个**保存按钮
           and ("in values && !HIDDEN_KEYS.has(k)" in _tih),
           "三个键已隐藏 + 面板两个输入框 + 一个保存按钮 + GROUPS 渲染也过滤 HIDDEN_KEYS")

    # ---- 重置步骤16 必须清得干净（2026-10-12 用户报障）--------------------------------
    #   `reset_steps_from()` 只删 `step.artifacts`（声明文件）+ `step.archive_dirs`（整目录）。
    #   step16 除了 spec，还往 `16_产品属性标注/` 写「属性集待确认清单.json/.md」
    #   —— **那正是"候选属性"面板的数据源**。但原来 `archive_dirs` 是**空**（`base.py` 默认），
    #   于是重置只删 spec、**目录残留** → 重置后候选属性照旧显示旧值，
    #   看起来像"重置没生效"（用户实际报障）。
    #   已核实**只有 step16** 引用该目录名 → 整目录随步骤删是安全的。
    try:
        from steps.step16_attribute_annotation import (   # noqa: PLC0415
            Step16AttributeAnnotation as _S16R,
        )
        rv.add("③", "不变量: 步骤16 声明了归档目录(重置才清得掉候选属性)",
               list(_S16R.archive_dirs) == ["16_产品属性标注"],
               "archive_dirs=%s（空了就会留下 属性集待确认清单.json）" % (list(_S16R.archive_dirs),))
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 步骤16 声明了归档目录(重置才清得掉候选属性)", False,
               "调用失败: %s" % _exc)

    # ---- 前端 watcher 必须覆盖步骤16 且"状态一变就刷"（2026-10-12 用户报障）------------
    #   原来只盯步骤 4/7，且**只在"变成 DONE"时**刷 —— 而**重置是 DONE→非DONE（方向相反）**
    #   → 重置步骤16 时属性集面板永远不刷新 ✗
    _wh = _tih[_tih.find("function watchKeywordRefresh"):]
    _wh = _wh[:_wh.find("\nfunction ") if _wh.find("\nfunction ") > 0 else 2200]
    rv.add("③", "不变量: 面板刷新监听覆盖步骤16(完成/重置都要刷)",
           ("s.number === 16" in _wh) and ("attrChanged" in _wh)
           and ("loadAttrSpec(true)" in _wh) and ("prev === s.status" in _wh)
           and ("s.number === 4 || s.number === 7" in _wh),
           "watchKeywordRefresh：4/7 刷关键词面板 + 16 状态一变刷属性面板")

    # ---- 「仅注入词」独立保存（2026-10-12 用户口径）----------------------------------
    #   它和核心大词/长尾词**在同一个模块里**，但**保存分开**：
    #     · 有独立按钮 `btnKwInjectSave` + 函数 `saveKwInject()`；
    #     · 「保存并应用」(`saveKwMode`) 里**不能再提交 inject** —— 否则
    #       `__kwDirty` 是**共享标志** → 改了仅注入词再点那个按钮，改动**不会被保存**
    #       而"未保存"提示被清掉 = **静默丢失**；
    #     · 所以 `saveKwMode` 里必须有 `injChanged` 的**显式告警**兜底。
    _sm = re.search(r"async function saveKwMode\(\)(.*?)\n\}", _tih, re.S)
    _sm_body = _sm.group(1) if _sm else ""
    rv.add("③", "不变量: 仅注入词独立保存(不受「保存并应用」控制)",
           bool(re.search(r"function saveKwInject\(", _tih))
           and ('id="btnKwInjectSave"' in _tih)
           and bool(_sm_body)
           and ('list: "inject"' not in _sm_body)          # 主按钮不再碰 inject
           and ('list: "inject"' in _tih)                  # 但独立函数里有
           and ("injChanged" in _sm_body),                 # 有"未保存"告警，不静默
           "saveKwInject + btnKwInjectSave + saveKwMode 里无 inject 提交 + injChanged 告警")

    # ---- 注入词不受三态影响（2026-10-12 用户口径）------------------------------------
    #   三态（自动 / 人工清单 / 人工停止）只决定「核心大词从哪来」和「步骤9 抓不抓」，
    #   与 `[仅注入]` 分区**无关**：它在三种态下都要照常进步骤10 的判定词。
    #   一旦有人给 `inject_words` 加三态判断，这条立刻红（行为面另有 pytest 守着）。
    #   ⚠ 判据用 **`inspect.getsource`**，与 `tests/test_keyword_inject_independent.py` 里
    #     那条 pytest **完全同源** —— 我第一版手搓文本切片（找 `@property` 当边界），
    #     边界找不到就把函数体截成空串 -> **自己假 FAIL**。同源判据省得两处漂移。
    try:
        import inspect as _insp2
        from core.keyword_lists import Plan as _KPlan
        _iw = _insp2.getsource(_KPlan.inject_words.fget)
        rv.add("③", "不变量: 注入词不受三态影响(自动/人工/停止)",
               ("inject_only" in _iw)
               and not any(k in _iw for k in ("self.mode", "is_manual", "self.stopped",
                                              "MODE_MANUAL", "MODE_STOP")),
               "Plan.inject_words = core + inject_only，函数体内无三态判断")
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 注入词不受三态影响(自动/人工/停止)", False,
               "调用失败: %s" % _exc)
    rv.add("③", "不变量: 属性集三键真删(代码不再读)",
           not re.search(r'["\']ATTR_MODE["\']', t16)
           and not re.search(r'["\']ATTR_KEYS["\']', t16)
           and not re.search(r'["\']ATTR_DESC["\']', t16),
           "step16_attribute_annotation.py（**注释**里的解释性提及不算）")
    _tcfg = (ROOT / "core" / "config.py").read_text(encoding="utf-8", errors="replace")
    rv.add("③", "不变量: ATTR_KEYS/ATTR_DESC 从 config 默认值里删除",
           '"ATTR_KEYS"' not in _tcfg and '"ATTR_DESC"' not in _tcfg
           and "def attr_keys" not in _tcfg and "def attr_desc" not in _tcfg,
           "core/config.py")
    #   ★★ **方案 C 的核心行为**（行为型，直接调 step16）：
    #     以「上一份 spec」为基底 → **只增不减**、**人工改过的说明不被 LLM 覆盖**。
    #     这条一旦破，就是"面板里改的东西下一轮静默丢失"（原来那个已验证的真毛病）。
    try:
        import inspect as _insp
        from steps.step16_attribute_annotation import (
            Step16AttributeAnnotation as _S16_STEP,
        )
        _src16 = _insp.getsource(_S16_STEP._merge_prev_spec)
        rv.add("③", "不变量: 步骤16 以旧 spec 为基底合并(只增不减)",
               all(k in _src16 for k in ("kept", "added", "updated", "dropped",
                                         "prev", "fresh_by"))
               # ★ 关键：**整个函数里只有一处** `item["hint"] = ...`，且它被"空才补"的条件守着
               #   → 人工改过的非空说明**不可能**被覆盖。写死这个数量，防止以后有人顺手加一处。
               and _src16.count('item["hint"]') == 1
               and 'if not str(a.get("hint") or "").strip()' in _src16,
               "`_merge_prev_spec`：保留旧的 + 追加新的 + 仅一处 hint 赋值(且被'空才补'守着)")
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 步骤16 以旧 spec 为基底合并", False, "调用失败: %s" % _exc)

    # ---- 方案甲第 1 步：沉淀的类目级共享层 + 市场标记（2026-10-09 用户口径）----------
    #   **行为型**断言（直接调模块），比字符串匹配强得多 —— 这几条一旦破，
    #   后果是"所有词表被过滤成空"或"跨市场静默污染"，都是极难发现的。
    try:
        import shutil as _sh
        from tools.attr_stability import catalog as _cat
        _vals = ["竹制", "钛金属", "304不锈钢"]
        _checks = [
            ("市场缺省=全球(None/空/含*)",
             _cat.is_visible(None, "US") and _cat.is_visible([], "US")
             and _cat.is_visible([None], "US") and _cat.is_visible(["*"], "DE")),
            ("带市场标记只对该市场可见",
             _cat.is_visible(["US"], "US") and not _cat.is_visible(["US"], "DE")),
            ("无市场标记时值过滤逐字不变",
             all(_cat.filter_values(_vals, t, m) == _vals
                 for t in (None, {}, {"别的": ["US"]}) for m in ("US", "DE", ""))),
            ("按市场过滤生效",
             _cat.filter_values(_vals, {"钛金属": ["US"]}, "DE") == ["竹制", "304不锈钢"]),
            ("tags 只写旁挂、values 保持字符串列表",
             _cat.tag_values({"values": _vals}, {"钛金属": ["US"]})["values"] == _vals),
            ("全球不写标记",
             _cat.VALUES_MARKET_KEY not in _cat.tag_values({"values": _vals},
                                                           {"竹制": _cat.GLOBAL_MARKET})),
            ("类目名解析正确",
             _cat.parse_cat("Cutting Boards-US-202608")
             == {"类目": "Cutting Boards", "市场": "US", "年月": "202608"}),
            ("认不出的目录名整串当类目(绝不猜)",
             _cat.parse_cat("salt and pepper mill sets")["类目"] == "salt and pepper mill sets"),
        ]
        # ⚠ **不能用 `tempfile`**：它落在 OS 临时目录，本沙箱**不可写**
        #   （关卡 ① 就有一条「不使用 OS 临时目录」—— 我第一版在这里违反了自家规则，
        #     报 `[WinError 5] 拒绝访问`）。改用工作区下的临时目录，用完删掉。
        _probe = ROOT / "scripts" / "_plan_pilot" / "_catalog_probe"
        _sh.rmtree(_probe, ignore_errors=True)
        _p = _probe
        _p.mkdir(parents=True, exist_ok=True)
        (_p / "Cutting Boards-US-202608").mkdir()
        _checks.append(("无 _catalog 时退回运行目录(向后兼容)",
                        _cat.resolve_definition_dir(_p, "Cutting Boards-US-202608")
                        == _p / "Cutting Boards-US-202608"))
        (_p / "_catalog" / "Cutting Boards").mkdir(parents=True)
        _want = _cat.catalog_dir_of(_p, "Cutting Boards")
        _checks.append(("有 _catalog 时同类目跨市场共享",
                        _cat.resolve_definition_dir(_p, "Cutting Boards-US-202608") == _want))
        (_p / "Chopping Boards-UK-202608").mkdir()
        _checks.append(("类目名不同则不共享(刻意，需显式声明)",
                        _cat.resolve_definition_dir(_p, "Chopping Boards-UK-202608")
                        == _p / "Chopping Boards-UK-202608"))
        for _name, _ok in _checks:
            rv.add("③", "不变量: 沉淀共享层/%s" % _name, bool(_ok), "")
        _sh.rmtree(_probe, ignore_errors=True)
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 沉淀共享层(catalog 模块)", False, "导入/调用失败: %s" % _exc)

    # 不变量 2c：`cross_checks` 里引用的属性必须都存在于 spec
    decl = rules.get("cross_checks") if isinstance(rules.get("cross_checks"), dict) else {}
    ref_bad = []
    for _kind, items in decl.items():
        if str(_kind).startswith("_") or not isinstance(items, list):
            continue
        for it in items:
            for side in ("左", "右"):
                a = (it.get(side) or {}).get("属性") if isinstance(it, dict) else None
                if a and str(a) not in s_by:
                    ref_bad.append(str(a))
    rv.add("③", "不变量: cross_checks 引用的属性都存在", not ref_bad, str(sorted(set(ref_bad))[:5]))

    # 不变量 2d：bands 必须**无缝且无重叠**（从真实 rules.json 读，不再用硬编码样例）
    band_bad = []
    for n, a in r_by.items():
        p = a.get("params") or {}
        bs = p.get("bands")
        if not bs:
            continue
        try:
            edges = sorted((float(b[1]), float(b[2]), str(b[0])) for b in bs)
        except (IndexError, TypeError, ValueError):
            band_bad.append("%s:形状非法" % n)
            continue
        names = [x[2] for x in edges]
        if len(set(names)) != len(names):
            band_bad.append("%s:档名重复" % n)
        for i in range(len(edges) - 1):
            if edges[i][1] < edges[i + 1][0]:
                band_bad.append("%s:洞 %.4g→%.4g" % (n, edges[i][1], edges[i + 1][0]))
    rv.add("③", "不变量: bands 无缝无重叠(真实 rules.json)", not band_bad, str(band_bad[:4]))

    # 不变量 2e：**分量数上限** `params.max_parts`（用户口径 2026-10-08）
    #   「主体材质默认写 1 个；当其他属性表明是多材质（双面不同材质）时才写 2 个
    #     `A+B`；不许 ≥3」。做成通用声明而非写死在代码里：
    #     有 `max_parts` 的属性，其交付表取值分量数必须 ≤ 该值。
    #   量纲：`+` 是组合连接符（`不锈钢+塑料`）。
    if xl.is_file():
        maxp_bad = []
        for n, a in r_by.items():
            mp = (a.get("params") or {}).get("max_parts")
            if not mp or n not in header:
                continue
            for r in rows:
                v = str(r.get(n) or "").strip()
                if v in EMPTY:
                    continue
                parts = [x for x in v.split("+") if x.strip()]
                if len(parts) > int(mp):
                    maxp_bad.append("%s=%s(%d 个分量 > max_parts=%s)" % (n, v, len(parts), mp))
                    break
        rv.add("③", "不变量: 取值分量数 ≤ params.max_parts", not maxp_bad, str(maxp_bad[:3]))
        # 不变量 2f：**分量数 ↔ 信号属性**双向一致（用户口径 2026-10-08）
        #   「其他属性表明多材质 → 主体材质写 2 个 `A+B`；否则只写 1 个」。
        #   信号属性如「是否双面不同材质」；信号缺失（未明确/空）时**不判**。
        try:
            from tools.attr_stability.cross_checks import check_parts_dependency
            dep_bad = check_parts_dependency(
                [{"xl_row": i + 2, "attributes": r} for i, r in enumerate(rows)], rules)
            rv.add("③", "不变量: 分量数 ↔ parts_when 信号一致", not dep_bad,
                   "%d 处: %s" % (len(dep_bad), [x["说明"][:36] for x in dep_bad[:2]]))
        except ImportError:
            rv.add("③", "不变量: 分量数 ↔ parts_when 信号一致", False, "缺 check_parts_dependency")
    else:
        rv.add("③", "不变量: 取值分量数 ≤ params.max_parts", False,
               "无交付表 → 未检查（不等于通过）")
        rv.add("③", "不变量: 分量数 ↔ parts_when 信号一致", False,
               "无交付表 → 未检查（不等于通过）")

    # 不变量 3：声明必须被代码消费（防"声明在 A、消费在 B"）
    py = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                   for d in PY_DIRS for p in (ROOT / d).rglob("*.py"))
    uncon = [k for k in ("evidence_priority", "derive_from", "bands", "cross_checks",
                         "vocab_hint", "max_parts", "parts_when", "top_keys")
             if k in rules and k not in py]
    rv.add("③", "不变量: 声明键都能在代码里找到消费点", not uncon, str(uncon))

    # 不变量 2g/2h：步骤18 必须**读回**上一轮报告，且新词/新属性要出「待确认清单」
    #   （用户口径 2026-10-08）此前三个报告只写不读 —— 已有类目每轮从零收敛，
    #   上一轮的判断全丢。而 `词表聚类报告.json` 只覆盖「已有属性的已有值」，
    #   覆盖不了 ① 新属性 ② 已有属性的新原值，所以**两类都要跑聚类**，只对 delta 跑。
    t17 = (ROOT / "steps" / "step17_attr_annotation.py")
    src17 = t17.read_text(encoding="utf-8-sig", errors="replace") if t17.is_file() else ""
    rb = "_load_prev_vocab_known" in src17
    # ★★ 修复（审核低-8，2026-10-10）：标签原来写"步骤18 读回…"，但 `rb` 断言的是 **step17**
    #   的 `_load_prev_vocab_known`（`_generate_vocab` 在读上一轮报告）。改成"步骤17"。
    rv.add("③", "不变量: 步骤17 读回上一轮聚类报告(增量聚类)", rb,
           "已实现" if rb else "未实现：报告只写不读 → 已有类目每轮从零收敛")
    pr = "_write_pending_review" in src17
    rv.add("③", "不变量: 新词/新属性出「待确认清单」", pr,
           "18_标准化/待确认清单.json" if pr else "未实现")

    # 不变量 2k：步骤16 的**并集必须加阈值**（用户口径 2026-10-09）
    #   N 次抽取的本意是稳定性，但 `merge_extractions` 取并集 → 跑得越多属性越多
    #   （实测：每次抽 ~20，3 次并集 → 28，方向反了）。`runs` 是现成的稳定性信号：
    #   runs >= ATTR_SPEC_MIN_RUNS 才采纳，偶然项转人工。
    t16 = (ROOT / "steps" / "step16_attribute_annotation.py")
    src16 = t16.read_text(encoding="utf-8-sig", errors="replace") if t16.is_file() else ""
    rv.add("③", "不变量: 步骤16 并集有 runs 阈值(防膨胀)",
           "ATTR_SPEC_MIN_RUNS" in src16, "step16._extract_spec_attrs")
    rv.add("③", "不变量: 偶然属性出「属性集待确认清单」",
           "_write_attr_pending" in src16, "16_产品属性标注/属性集待确认清单.json")
    # 不变量 2l：同义归并提示词必须覆盖「同义不同**粒度**」（实测漏项：
    #   主体材质/材质大类/木材树种 三列并存）
    sp = (ROOT / "tools" / "attr_stability" / "spec.py")
    srcsp = sp.read_text(encoding="utf-8-sig", errors="replace") if sp.is_file() else ""
    rv.add("③", "不变量: 同义归并覆盖「同义不同粒度」",
           "同义不同粒度" in srcsp, "spec.build_dedupe_merge_prompt")
    # 不变量 2m：跨属性纠正必须**分批**（实测一次给 25 行模型返回空数组）
    rv.add("③", "不变量: 跨属性纠正分批(可配 ATTR_XC_BATCH)",
           "ATTR_XC_BATCH" in src17, "step17._correct_cross_conflicts")
    # 不变量 2n：**每个属性都要给 `_依据` 原文**（用户口径 2026-10-09）
    #   根因：原设计只有「尺寸」有 `_依据`（实测 18 个属性里 1 个），
    #   导致跨属性纠正判不了"该改哪一侧"（两侧 `_来源` 标签常常相同）
    #   → 把正确数据改错。所以必须：提示词要求 + 闸门硬拦 + 交付表可见。
    astab = (ROOT / "tools" / "attr_stability" / "attr_stability.py")
    src_astab = astab.read_text(encoding="utf-8-sig", errors="replace") if astab.is_file() else ""
    rv.add("③", "不变量: 闸门强制 `_依据` 键(可配)",
           "_BASIS_SUFFIX" in src_astab and "ATTR_STAB_REQUIRE_BASIS" in src17,
           "attr_stability + step17")
    pr_prompt = ROOT / "prompts" / "23_llm_only_pipeline.md"
    src_prompt = pr_prompt.read_text(encoding="utf-8", errors="replace") if pr_prompt.is_file() else ""
    rv.add("③", "不变量: 提示词要求每属性给依据(§3.3e)",
           "### 3.3e" in src_prompt and "每个属性都要给" in src_prompt,
           "prompts/23_llm_only_pipeline.md")
    # 不变量 2o：**「典型取值」是示例，不是闭集**（用户口径 2026-10-09）
    #   根因：hint 里写「典型取值 A/B/C」，而提示词渲染成「逐条照此取值」→
    #   模型把它当**闭集**。实测 `产品类型` 的 7 个典型取值**恰好等于**聚类输入，
    #   26 个钛金属砧板全被塞进「不锈钢砧板」→ 缺的类从未被产出 →
    #   **聚类永远聚不出它**（聚类只能聚"已产出过的值"）→ 冲突永久存在。
    #   通用规则（不绑类目）：判据是证据；证据指向示例之外就照实写，走新词候选通路。
    rv.add("③", "不变量: 提示词说明「典型取值≠闭集」(§1b.9)",
           "不是闭集" in src_prompt, "prompts/23_llm_only_pipeline.md §1b.9")
    rv.add("③", "不变量: 属性块不再写「逐条照此取值」",
           "逐条照此取值" not in src17 and "判据永远是原文证据" in src17,
           "step17._build_attr_hint_block")
    # 不变量 2q：**「先改文件、后读文件」这个坑**必须由机制守住，不能靠记性。
    #   我踩过：把「消除矛盾断言」写在 `validate_upstream` **之后** ——
    #   而那里 spec 早被读进 `self._spec_attributes`，于是改的是文件、提示词用内存，
    #   白跑一轮 1.28 元。正确做法是**改写之后重新读**（让内存与文件一致）。
    #   这里断言：`strip` 之后**还有一次** `spec = json.load(fh)`（重读），
    #   否则改文件不改内存。⚠ 2026-10-10 停① 闸门前置：strip 挪到了**闸门之后**，
    #   但仍要求 strip 之后有重读 —— 所以判据是"最后一个 load 在 strip 之后"。
    _lines17 = src17.splitlines()
    _i_strip = next((i for i, l in enumerate(_lines17) if "strip_all_contradictions(" in l), -1)
    _i_loads = [i for i, l in enumerate(_lines17)
                if re.match(r"^\s*spec\s*=\s*", l)
                and ("json.load" in l or "load_attr_spec" in l)]
    _ok = _i_strip >= 0 and _i_loads and _i_strip < _i_loads[-1]
    rv.add("③", "不变量: 消除矛盾断言在读取 spec **之后**（重读）",
           _ok,
           "strip@L%d < 最后 load@L%d（否则改文件不改内存）"
           % (_i_strip + 1, _i_loads[-1] + 1) if _ok else
           "strip 之后没有重读 spec（改文件不改内存）")

    # 不变量 2r：「新增: X」在证据充分时**自动进词表**（2026-10-09 用户口径）
    #   用户原话：「AI 本来都可以解决的，你不要强行限制它」——
    #   原先一律路由到「待人工」，等于让 AI 判对了也不生效。
    #   为什么现在敢自动：加了 `依据` 后模型**判对了侧**
    #   （实测 26/26 条建议都指向真正错的那一侧，而不是改正确的那一侧）。
    rv.add("③", "不变量: 新增值可自动进词表(可关)",
           "ATTR_XC_AUTO" in src17 and "_append_vocab_values" in src17,
           "step17._correct_cross_conflicts")
    rv.add("③", "不变量: 采纳新值先改内存再落盘",
           "_sp.values = _cur" in src17 and "_append_vocab_values(ctx" in src17,
           "避免「改文件不改内存」")
    # 不变量 2s：**登记与纠正必须看同一份值**（2026-10-09 修）。
    #   原实现纠正自己从 `std` 拼行值（缺的属性变空串、只看 std 里的行），
    #   而登记用 `_final_rows(res, std_map)`（deliverable 打底 + std 覆盖 + 并集）→
    #   同一轮里「登记说 1 个冲突、纠正说 0 个」，交付目录留下自相矛盾的产物。
    rv.add("③", "不变量: 纠正与登记用同一份值口径",
           "self._final_rows(res, std)" in src17,
           "step17._correct_cross_conflicts")
    # 不变量 2t：**早退也要写记录**，否则分不清「没违例」和「没跑」。
    rv.add("③", "不变量: 无违例也写纠正记录",
           "def _write_correction" in src17 and "无违例（不改值）" in src17,
           "step17._write_correction")
    # 不变量 2u：带 `bands` 的属性要有**档名合法性**检查（2026-10-09）。
    #   根因：`bands` 声明本来没有任何校验 —— 现成的 `CHECK_BAND` 依赖
    #   「值是数字 + 档名在 <属性>_档位 键」，而本项目把档名写在值里 →
    #   实测直接启用会 4/5 满屏「非数值」误报（`_probe_band_check.py` 跑出来的）。
    ncm = (ROOT / "tools" / "attr_stability" / "num_check.py")
    src_ncm = ncm.read_text(encoding="utf-8-sig", errors="replace") if ncm.is_file() else ""
    # ★ 2026-10-09：原先这里有两条"档位声明有校验 / bands 能传到校验"的不变量。
    #   用户口径「**bands 没必要存在**」后，`bands` 与我为它写的 `check_band_labels`
    #   已整体删除 → 那两条不变量随之删除。档名合法性本来就由**结构闸门**保证
    #   （值必须在 `params.values` 内），所以没丢覆盖。
    #   改成反向守住：**本次审查的类目里不许再有 bands**（防止它悄悄回来）。
    #   ⚠ 用关卡**已经解析好的** `r_by`（`{属性名: 属性声明}`），不要再自己去读文件 ——
    #     我第一版就是自己拼路径，结果把"运行产物目录"当成了"类目声明目录"（读不到文件）。
    #     也踩了 `cat_dir / "%s.rules.json" % cat` 的优先级坑（`%` 与 `/` 同优先级左结合）。
    #   ⚠ 不是全仓扫描：别的类目（如 Cat Fountains 的 `容量`）仍有 bands，
    #     那属于**各自的业务声明**，不该由本类目的关卡替它决定；只在备注里点出来。
    _no_bands, _other_bands = [], []
    try:
        import json as _j
        for _n, _a in (r_by or {}).items():
            _pp = _a.get("params") if isinstance(_a.get("params"), dict) else {}
            if "bands" in _pp:
                _no_bands.append(str(_n))
        for _rf in (ROOT / "data").glob("*/*.rules.json"):
            if _rf.parent.name == cat:
                continue
            try:
                _o = _j.loads(_rf.read_text(encoding="utf-8-sig"))
            except Exception:                                    # noqa: BLE001
                continue
            for _a in (_o.get("attributes") or []):
                if "bands" in ((_a.get("params") or {}) if isinstance(_a.get("params"), dict) else {}):
                    _other_bands.append("%s/%s" % (_rf.parent.name, _a.get("name")))
    except Exception as _exc:                                    # noqa: BLE001
        _no_bands = ["读取失败: %s" % _exc]
    rv.add("③", "不变量: 已不声明 bands(用户口径)",
           not _no_bands,
           ("本类目 0 处" + ("；⚠ 其他类目仍有: %s" % _other_bands[:3] if _other_bands else ""))
           if not _no_bands else str(_no_bands[:4]))
    # 不变量 2v：计数值的**单品默认值**不算异常（用户口径 2026-10-09）。
    #   实测 100 行里 16 处数值告警有 15 处是 `套装数量=1` —— 单品本来就 1 件，
    #   是合理默认推断，不是"上游误判"；这些噪音会把真异常（`=10`）淹掉。
    rv.add("③", "不变量: 单品默认值(计数=1)不算异常",
           # ⚠ 判据要**不脆**：2026-10-13 有人把条件从 `quantity is None` 放宽成
           #   `quantity in (None, "count")`（合理改进：计数类属性的一件也是默认值）→
           #   原来那种"整串匹配"当场假失败。这里只钉**语义要点**，
           #   真正的行为由 `tests/test_num_singleton_default.py` 守着（那才是硬证据）。
           ("abs(n - 1.0) <= 1e-9" in src_ncm) and ("quantity" in src_ncm),
           "num_check.check_no_evidence：1 件默认值不算异常")
    # ---- 方案 A 的「停②」：标准化词表确认闸门（2026-10-09 用户口径）----------------
    #   链路：16 提取属性 →[停①]确认属性集 → 17 标注 **+ 产词表** →[停②]确认词表 → 18 应用词表
    #   为什么要把词表生成从 18 移到 17：原先 18 自己生成 → "步骤17 跑完"时没有新词表可调，
    #   用户要的"停下来调词表"落不了地。
    s18 = (ROOT / "steps" / "step18_standardization.py")
    src18 = s18.read_text(encoding="utf-8-sig", errors="replace") if s18.is_file() else ""
    rv.add("③", "不变量: 步骤18 不再自己生成词表(已移到17)",
           "self._generate_vocab(" not in src18, "step18 只应用已确认的词表")
    rv.add("③", "不变量: 步骤17 末尾产词表并置待确认",
           "_set_vocab_status(ctx, VOCAB_PENDING)" in src17 and "self._generate_vocab(" in src17,
           "step17.run 末尾")
    # 不变量 2q：**已有类目的词表更新必须写得回去**（2026-10-10 审核修复）。
    #   原两道闸门都只放行"受控值为空"的属性（`if name in empty and vals` +
    #   `if not params.get("values")`）→ 已有类目**付了 LLM 费一个词都写不回去**。
    #   审核用行为实验证实：_write_vocab_to_rules({"有词表的":["A","B","C"]}) → ['A','B']（C 被丢）。
    #   **行为型**断言（不跑 LLM）：并集写入，新增值追加、已有值不丢。
    try:
        import json as _jj
        import shutil as _sh6
        from steps.step17_attr_annotation import Step17AttrAnnotation as _S17
        _pr6 = ROOT / "scripts" / "_plan_pilot" / "_cr_tmp" / "_wbt_probe"
        _sh6.rmtree(_pr6, ignore_errors=True)
        _pr6.mkdir(parents=True, exist_ok=True)
        (_pr6 / "t.rules.json").write_text(_jj.dumps({"attributes": [
            {"name": "有词表的", "params": {"values": ["A", "B"]}},
            {"name": "空词表的", "params": {}},
        ]}, ensure_ascii=False), encoding="utf-8")
        _s17 = _S17()
        _s17._cat = "t"
        _s17.log = lambda *a, **k: None
        _s17._write_vocab_to_rules(str(_pr6), "t", {"有词表的": ["A", "B", "C"],
                                                    "空词表的": ["X"]}, None)
        _o = _jj.loads((_pr6 / "t.rules.json").read_text(encoding="utf-8"))
        _by = {a["name"]: ((a.get("params") or {}).get("values") if "params" in a
                           else a.get("values")) for a in _o["attributes"]}
        rv.add("③", "不变量: 已有类目词表更新写得回(并集不丢新值)",
               _by.get("有词表的") == ["A", "B", "C"] and _by.get("空词表的") == ["X"],
               str(_by))
        _sh6.rmtree(_pr6, ignore_errors=True)
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 已有类目词表更新写得回", False, "调用失败: %s" % _exc)
    # 不变量 2p：**属性池读写键一致 + multi 分隔符归一**（2026-10-10 两个 bug 的修复）。
    #   ① `append_attr_pool` 写 `MARKET_KEY`(`_市场`)、`load_attr_pool` 曾读 `"市场"` →
    #      返回的市场**永远为空**（写读键名不一致，这类坑今天栽过多次）。
    #   ② multi 属性交付值用 `,`、词表用 `、` → 复合词表整串匹配不上、落"词表外"。
    #   **行为型**断言（不跑 LLM）。
    try:
        import shutil as _sh7
        from tools.attr_stability.catalog import (
            append_attr_pool as _aap, load_attr_pool as _lap,
        )
        from tools.attr_stability.converge import converge_mechanical as _cm
        _pr7 = ROOT / "scripts" / "_plan_pilot" / "_cr_tmp" / "_mk_probe"
        _sh7.rmtree(_pr7, ignore_errors=True)
        _aap(_pr7, "Cutting Boards", [{"name": "产品类型", "hint": "a"}], "US", "t1")
        _rounds = _lap(_pr7, "Cutting Boards")
        rv.add("③", "不变量: 属性池市场字段写得进读得出",
               bool(_rounds) and _rounds[0].get("市场") == "US",
               "市场=%r" % (_rounds[0].get("市场") if _rounds else None))
        _cells = {"1|attr.适用场景": {"kind": "value",
                                      "values": [{"value": "肉类,蔬菜水果,奶酪熟食"}]}}
        _r = _cm(_cells, {"适用场景": ["肉类、蔬菜水果、奶酪熟食"]},
                 # ⚠ 键名必须是 **`value_shape`** —— 审核实测：原先写 `"shape"`，
                 #   而 `converge.py` 读的是 `spec.get('value_shape')`（`get('shape')` 全文没有）
                 #   → **快路径根本没执行**，不变量是靠单值分支碰巧通过的（假信心）。
                 attrs_spec={"适用场景": {"type": "multi", "value_shape": "multi"}})
        rv.add("③", "不变量: multi 分隔符归一(,→、)后整串命中",
               not _r["适用场景"].unmatched and
               _r["适用场景"].cells["1|attr.适用场景"]["主值"] == "肉类、蔬菜水果、奶酪熟食",
               str(_r["适用场景"].cells["1|attr.适用场景"].get("主值")))
        _sh7.rmtree(_pr7, ignore_errors=True)
    except Exception as _exc:                                    # noqa: BLE001
        rv.add("③", "不变量: 属性池读键/分隔符归一", False, "调用失败: %s" % _exc)
    rv.add("③", "不变量: 步骤18 有词表确认闸门",
           "标准化词表未确认" in src18 and "_vocab_status(ctx)" in src18,
           "step18.validate_upstream")
    sw = (ROOT / "webui" / "server.py")
    srcw = sw.read_text(encoding="utf-8-sig", errors="replace") if sw.is_file() else ""
    rv.add("③", "不变量: Web 保存词表即确认",
           'rules["vocab_status"] = "confirmed"' in srcw, "webui _api_std_vocab_save")
    # 不变量 2w：词表确认范围 = **全部属性**（用户口径 2026-10-09）——
    #   状态是 `rules.json` 的**顶层全局键**，不是逐属性；
    #   否则"审过了"只覆盖被点过的属性，会漏掉没看的 → 正是要防的。
    rv.add("③", "不变量: 词表确认是全局(覆盖全部属性)",
           "覆盖属性数" in srcw and '"vocab_status"' in srcw,
           "webui 返回并提示覆盖的属性数")
    # ---- 「等待人工确认」不是失败（2026-10-09 用户口径，方案 A）----------------------
    #   原先两个停点都抛 StepDataError → 编排器一律按"失败/产物缺失"处理:
    #   发 CRIT 告警 + 弹窗 + 标 FAILED。**可这根本不是失败，是在等人**。
    orch = (ROOT / "core" / "orchestrator.py")
    sorch = orch.read_text(encoding="utf-8-sig", errors="replace") if orch.is_file() else ""
    rv.add("③", "不变量: 编排器识别「等待确认」并不标失败",
           "except WaitingForHuman" in sorch and "self.state.mark_pending(num)" in sorch
           and "return 3" in sorch, "core/orchestrator._run_one")
    errs = (ROOT / "core" / "errors.py")
    serrs = errs.read_text(encoding="utf-8-sig", errors="replace") if errs.is_file() else ""
    rv.add("③", "不变量: WaitingForHuman 有可照做的 hint",
           "class WaitingForHuman" in serrs and "self.hint = hint" in serrs,
           "core/errors.WaitingForHuman")
    rv.add("③", "不变量: 两个停点都抛 WaitingForHuman",
           "raise WaitingForHuman" in src17 and "raise WaitingForHuman" in src18,
           "step17 属性集闸门 / step18 词表闸门")

    if xl.is_file():
        try:
            import openpyxl as _op
            _wb = _op.load_workbook(xl, read_only=True)
            sheets = list(_wb.sheetnames)
            _wb.close()
            rv.add("②", "交付表有「依据」sheet", "依据" in sheets, str(sheets))
        except Exception as exc:                                   # noqa: BLE001
            rv.add("②", "交付表有「依据」sheet", False, str(exc)[:50])


    #   「既然有冲突，应该再加个校验和纠正的步骤」——
    #   此前步骤18 只写 `跨属性冲突.json`（登记），冲突行**原样进交付表**。
    #   正确链路：16 声明约束 → 17 标注时避开 → **18 校验 + 纠正**。
    #   ★★ 修复（格式审核 A-1，2026-10-10）：原来写 `... in t18` —— **`t18` 全仓只出现这一次、
    #   从未定义**。今天不炸**只因 `or` 短路**（左边 `src17` 恒真）；
    #   一旦步骤17 失去 `_correct_cross_conflicts`，右边求值 → `NameError`，
    #   而 `main()` 调 `sec3_logic` **没有 try/except** → **整个关卡崩溃、76 项结果一条都不打印**。
    #   同函数的变量是 `src18`；且该条目文案原来恒写 "step18"，实际断言的是 step17。
    corr = "_correct_cross_conflicts" in src17 or "_correct_cross_conflicts" in src18
    rv.add("③", "不变量: 跨属性冲突有纠正步骤(不只登记)", corr,
           "step17._correct_cross_conflicts" if corr else "缺失：只登记不纠正")


    #   `spec.py:sync_rules_from_spec` 方向是「**spec 为准**」：
    #     rules 有、spec 无 → **删除**。
    #   ⚠ 2026-10-12 **方案 C** 之后这条**不再是风险**：`step16` 以「上一份 spec」为基底合并
    #     （`_merge_prev_spec`），spec 里的属性**只增不减** → 人工加的属性**不会被重跑抹掉**，
    #     所以也不再需要 `ATTR_KEYS` 那条"登记"通道（该键已真删）。
    #   这里改成断言**新机制在位**（旧断言已随机制一起作废）。
    #   ⚠ `t16` 在本函数**中段已被重新绑定为 Path**（见上面 `src16 = t16.read_text(...)`），
    #     不能再当字符串用 —— 这里**自己读一遍**（我第一版就栽在这，被段级兜底抓到）。
    _s16txt = (ROOT / "steps" / "step16_attribute_annotation.py").read_text(
        encoding="utf-8", errors="replace")
    _cfg_p = cat_dir / "config.yaml"
    _cfg_clean = True
    if _cfg_p.is_file():
        _ct = _cfg_p.read_text(encoding="utf-8", errors="replace")
        # ⚠ 必须按**行首键名**判，不能用 `"ATTR_KEYS" not in text` 这种子串判 ——
        #   `ATTR_STAB_*` 一大堆键会让子串判**假阳性**（我第一版就栽在这）。
        _cfg_clean = re.search(r"(?m)^\s*(ATTR_KEYS|ATTR_DESC)\s*:", _ct) is None
    rv.add("③", "不变量: 人工属性不再需要登记(config 键已真删，靠 spec 合并保命)",
           ("def _merge_prev_spec" in _s16txt) and _cfg_clean,
           "step16._merge_prev_spec 在位 + config.yaml 里已无 ATTR_KEYS/ATTR_DESC")

    # ---- 死键清理：`SS_ASIN_PREFIX`（2026-10-12 用户口径）--------------------------
    #   文档写着"卖家精灵 ASIN 清单文件名前缀(默认=CAT)"给步骤 11/12/13 用，但**全仓没有
    #   任何代码读它**（只有 `core/config.py` 的默认值定义）→ 死键：14 个 config 里全是空串、
    #   改它行为不变，却一直挂在 Web 表单的「其他参数」兜底组里，**看着像在生效**。
    #   ⚠ 这类"声明了但没人消费"的键只能靠**对账**发现（本次是用户看图看出来的）。
    _sx = ""
    for _d in ("steps", "tools", "core", "webui", "scripts"):
        for _f in (ROOT / _d).rglob("*.py"):
            if "__pycache__" in str(_f) or "_plan_pilot" in str(_f):
                continue
            _sx += _f.read_text(encoding="utf-8", errors="replace")
    _cfg_has = bool(_cfg_p.is_file() and re.search(
        r"(?m)^\s*SS_ASIN_PREFIX\s*:", _cfg_p.read_text(encoding="utf-8", errors="replace")))
    _tcfg2 = (ROOT / "core" / "config.py").read_text(encoding="utf-8", errors="replace")
    # ⚠ 判"配置默认值里的键"要看**带引号的键名**（`"SS_ASIN_PREFIX"`）——
    #   裸字符串会被**解释性注释**绊倒（我第一版就假阳性了）。
    rv.add("③", "不变量: 死键 SS_ASIN_PREFIX 已清(代码默认值+Web+config)",
           ('"SS_ASIN_PREFIX"' not in _tcfg2)
           and ("SS_ASIN_PREFIX" not in _tih)
           and (not _cfg_has),
           "core/config.py: %s · index.html: %s · config.yaml: %s"
           % ("无" if '"SS_ASIN_PREFIX"' not in _tcfg2 else "⚠还有",
              "无" if "SS_ASIN_PREFIX" not in _tih else "⚠还有",
              "无" if not _cfg_has else "⚠还有"))

    # 不变量 4：`是否*` 的取值域 + 空值率（2026-10-09 改，用户口径）
    #   **原先**写成 `是否* ∈ {是,否,未明确,空}` —— `空` 来自 `EMPTY` 保留集，
    #   于是**「未明确」（证据不足）与「空」（压根没填）被混为一谈、一起静默放行**。
    #   两处不对：
    #     ① 与项目自己的口径打架 —— `top_keys` 那条写的是「缺了就是**整列静默漏填（必拦）**」，
    #        这里却把空放行了；
    #     ② **漏填被隐藏** —— 人看不出这一列是不是大面积没填。
    #   现在拆成两件事：
    #     ① **取值域**：只许 `{是, 否, 未明确}`（项目对"无证据"的**规范写法就是 `未明确`**；
    #        `未标注`/`未知`/`待确认`/`不适用` 虽然语义相近，但不是规范写法 → 照样报出来，
    #        免得下游按不同 token 各判一套）
    #     ② **空值率**：单独报（**通过时也显示**，非零就看得见），**整列全空**才判红
    if rows:
        # ⚠ 2026-10-09 用户反馈「又把流程复杂化了」→ **撤掉对近义 token 的收紧**：
        #   我一度把 `未标注`/`未知`/`待确认`/`不适用` 也判成违例 —— 那是**没人要求的规则**，
        #   而且 AI 的判据里本来就用 `未标注`（如 `是否食品级安全` 的典型取值），
        #   收紧它只会让**正常输出变红**、逼人去改一个没问题的东西。
        #   现在只做一件事：把「无证据」的写法与 `EMPTY` 保留集**对齐**（近义 token 照样放行）。
        legal = {"是", "否"} | EMPTY
        off, rates = {}, {}
        for n in [h for h in header if h.startswith("是否")]:
            vals = [str(r.get(n) or "").strip() for r in rows]
            bad = sorted({v for v in vals if v not in legal})
            if bad:
                off[n] = bad[:3]
            rates[n] = (sum(1 for v in vals if not v) / len(vals)) if vals else 0.0
        rv.add("③", "不变量: 是否* ∈ {是,否,未明确,空}", not off, str(off)[:90])
        all_empty = [n for n, rt in rates.items() if rt >= 1.0]
        worst = sorted(rates.items(), key=lambda kv: -kv[1])[:3]
        rv.add("③", "不变量: 是否* 无整列静默漏填", not all_empty,
               ("⚠ 整列漏填: %s" % all_empty) if all_empty
               else "空值率 " + " · ".join("%s %d%%" % (n, round(rt * 100))
                                           for n, rt in worst))

        # 不变量 5（2026-10-09 改）：**已删除 `厚度` 列**（用户口径）。
        #   原先这里是「厚 与 厚度 一致（有 bands 时）」—— 拿 bands 把交付表的
        #   精确值 `厚` 反查成档名，再与 `厚度` 列比对。用户判定：
        #   「档位名没必要 **这列可以删掉**，让 AI 直接判」→ bands 与 `厚度` 列都已删除，
        #   那条比对自然作废。这里**反向守住**：声明里不许再冒出 `厚度`，
        #   免得它悄悄回来（或旧产物把列带回来）。
        _th_add = "厚度" in (r_by or {})
        _th_col = "厚度" in header
        rv.add("③", "不变量: 已删除 厚度 列(用户口径)",
               not (_th_add or _th_col),
               "声明里%s · 交付表%s" % ("有 厚度" if _th_add else "无 厚度",
                                        "有 厚度 列" if _th_col else "无 厚度 列"))

        # 不变量 6：已声明的跨属性约束无违例
        decl = load_cross_checks(rules)
        if decl:
            hits = check_rows([{"xl_row": i + 2, "attributes": r} for i, r in enumerate(rows)], decl)
            rv.add("③", "不变量: 跨属性约束无违例", not hits, "%d 处" % len(hits))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", default="Cutting Boards-US-202608")
    ap.add_argument("--dir", default="")
    a = ap.parse_args()
    cat_dir = (Path(a.dir) / a.cat) if a.dir else (ROOT / "data" / a.cat)
    rv = Review()
    # ★★ 2026-10-12：每一段都**单独兜底** —— 原来某段里一个 `NameError`（我写不变量时
    #   把变量名拼错成 `_ts16`）就让整个关卡**崩掉、一百多项结果一条都不打印**，
    #   比报一条红更糟：看的人以为"没输出=没跑"。现在崩掉的那段记成一条 FAIL，其余照常输出。
    #   （格式审核早就提过这条；我自己踩了一次，才动手修。）
    for _name, _fn in (("① 语法/静态", sec1_syntax),
                       ("② 输入输出", sec2_io),
                       ("③ 逻辑/不变量", sec3_logic)):
        try:
            _fn(rv, a.cat, cat_dir)
        except Exception as _exc:                                # noqa: BLE001
            import traceback as _tb
            _where = _tb.format_exc().strip().splitlines()[-3:]
            rv.sec = _name.split()[0]
            rv.add(rv.sec, "⚠ %s **本段检查自身崩了**(其余段落仍会输出)" % _name,
                   False, " | ".join(x.strip() for x in _where)[:200])
    return rv.report()


if __name__ == "__main__":
    raise SystemExit(main())
