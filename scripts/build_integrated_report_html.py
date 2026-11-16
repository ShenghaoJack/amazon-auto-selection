# -*- coding: utf-8 -*-
"""整合市场报告(HTML 一键生成) —— **三份输入缺一不可**  [2026-10-09 新增]

## 输入(全部必需; 缺任何一个都**拒绝生成**, 不做"部分报告")

  ① 步骤8  关键词报告          `data/<CAT>/<CAT>-keyword-report.xlsx`
  ② 步骤18 属性标注-标准化交付  `data/<CAT>/18_标准化/<CAT>-属性标注-标准化交付.xlsx`
  ③ 外部提供的 VOC报告        `data/<CAT>/19_VOC报告/<CAT>-VOC报告.xlsx`

用户的硬要求(2026-10-09): "这三个报告缺一个不可, 缺其中一个都无法生成整合报告"。

## 输出

  `data/<CAT>/20_整合市场报告/html/index.html`   ← **单文件、离线、双击即看**(4 个模块页签)
  `data/<CAT>/20_整合市场报告/html/report.json`   ← 契约 JSON(可复算/可复现/可再渲染)
  `data/<CAT>/20_整合市场报告/html/构建日志.txt`  ← 输入指纹 + 表数 + 输出路径

## 实现要点

* **契约与查看器** vendored 自桌面 kit `Build_Amazon_Integrated_Market_Report_Skill_HTML_Scripts`
  (见 `tools/market_report_html/VENDOR.md`)。契约 = 恰好 4 个 sheet, 顺序固定
  `关键词分析 → 市场大盘分析 → VOC差评分析 → 选品建议`; 每个 table 必须有
  `id/title/headers/columns/sourceHeaderRow/rows`, `columns` 与 `headers` 等长,
  `sourceRow/sourceHeaderRow` 为正整数, 值不得含 NaN/Infinity。
* **数据读取复用既有引擎** `tools/cat_report`(与 Excel 版整合报告 `scripts/build_integrated_report.py`
  **同一口径**, 不另起一套算法):
    - 产品 / 关键词: `tools.cat_report.engine.Sources.load_product()/load_keyword()`
    - 价格分段:      `engine.choose_price_bands()`
    - 功能三态:      `engine.is_positive()`(未知 ≠ 否)
    - VOC:           `tools.cat_report.voc_reader.read_voc_workbook()`
* **缺失不补 0**: 源字段缺失一律留空(`null`)并写进 notes / 局限。
* 图表由查看器按**中文表头/表名**推断(kit 的 `app.js::chartSpec`), 故表头按可出图的名字写
  (价格段+月销额 / 生命周期+占比 / 月份+类别合成搜索量 / 品牌+中位价 …)。

## 用法

    python scripts/build_integrated_report_html.py --cat "Dog Dryer-US-202608"
    python scripts/build_integrated_report_html.py --cat "..." --check-only     # 只做门槛检查
    python scripts/build_integrated_report_html.py --cat "..." \\
        --keyword a.xlsx --product b.xlsx --voc c.xlsx                         # 显式指定三份输入
    python scripts/build_integrated_report_html.py --cat "..." --no-html       # 只产 report.json

退出码: 0 成功 · 2 **输入不齐(门槛拦截)** · 1 其它错误(读取/契约/渲染失败)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import statistics as st
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook                              # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.cat_report.engine import (UNKNOWN, choose_price_bands, is_positive, num,  # noqa: E402
                                     Sources)
from tools.cat_report.voc_reader import read_voc_workbook               # noqa: E402
# 2026-10-12 同步 skill: 数值边界只留一处 —— vendored 自 `analysis_rules.py`(见 tools/report_rules/VENDOR.md)
from tools.report_rules import analysis_rules as _rules                    # noqa: E402

OUT_DIR = "20_整合市场报告"
HTML_SUBDIR = "html"
PROGRESS_NAME = ".build-progress.json"      # 进度文件(类目中心的按钮据此显示"正在做什么")
SHEET_ORDER = ["决策摘要", "关键词分析", "市场大盘分析", "VOC差评分析", "选品建议"]
# 2026-10-10(参照 .codex skill `html-report-contract.md` §2「Executive decision and scope」):
# 报告首屏 = 决策摘要(源时间窗/样本规模/价格锚/集中度/主要机会/主要反证/决策状态) + 模块状态 + 来源索引。
VENDOR_DIR = _ROOT / "tools" / "market_report_html"

# 门槛: (步骤号, 人类可读名, 相对类目根的候选路径模板)
GATE_INPUTS = (
    (8, "关键词报告", ("{cat}-keyword-report.xlsx",)),
    (18, "属性标注-标准化交付", ("18_标准化/{cat}-属性标注-标准化交付.xlsx",)),
    (19, "VOC报告", ("19_VOC报告/{cat}-VOC报告.xlsx",)),
)
# 非承诺性取值: 不算"明确宣称", 也不当作"否"
VAGUE = {"", UNKNOWN, "缺", "未提供", "无数据", "—", "-", "None", "nan",
         "不适用", "不涉及", "无", "N/A", "n/a", "NA", "未知", "其他"}   # 这些是"没有取值"而不是"某个配置"(2026-10-10 实测 Dog Dryer 容量列全是「不适用」)


# --------------------------------------------------------------------------- 进度上报

def progress_path(out_dir) -> Path:
    """进度文件位置 = **产出目录的上一级** `…/20_整合市场报告/.build-progress.json`。

    默认产出目录是 `data/<CAT>/20_整合市场报告/html` → 进度落在 `data/<CAT>/20_整合市场报告/`,
    正是看板 `webui/server.py::integrated_progress()` 读的那个位置; 传自定义 `--out-dir` 时
    进度跟着走(看板找不到就不显示进度, 不影响生成)。
    """
    return Path(out_dir).parent / PROGRESS_NAME


def _progress(path: Path | None, phase: str, pct: int, *, done: bool = False,
              error: str = "") -> None:
    """写一次进度(原子替换)。**故意做成"尽力而为"** —— 进度写失败绝不该影响报告生成。"""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"phase": phase, "pct": int(pct), "done": bool(done), "error": error,
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pid": os.getpid()}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------- 小工具

def _f(v) -> float | None:
    x = num(v)
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(x) or math.isinf(x)) else x


def _i(v) -> int | None:
    x = _f(v)
    return None if x is None else int(round(x))


def _s(v, limit: int = 0) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return s[:limit] if limit else s


def _share(part: float | None, whole: float | None) -> float | None:
    """占比以 **0–1 小数**返回(查看器按表头名决定是否 ×100 显示)。"""
    if part is None or not whole:
        return None
    return round(part / whole, 4)


def _sum(rows, key) -> float:
    return float(sum(r[key] for r in rows if r.get(key) is not None))


def _mean(vals) -> float | None:
    xs = [v for v in vals if v is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def _median(vals) -> float | None:
    xs = [v for v in vals if v is not None]
    return round(st.median(xs), 2) if xs else None


def _fmt_num(v, digits: int = 0) -> str:
    """千分位数字格式化; None/空 → 『未提供』(skill §1: 缺失不许当 0)。"""
    if v is None or v == "":
        return "未提供"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _s(v) or "未提供"
    return ("{:,." + str(int(digits)) + "f}").format(f)


def kpi(label, value, source_ref: str = "", source_row: int = 1) -> dict:
    out = {"label": label, "value": value}
    if source_ref:
        out["sourceRef"] = source_ref
    if source_row:
        out["sourceRow"] = source_row
    return out


def table(tid: str, title: str, headers: list[str], rows: list[list],
          source_label: str = "", source_ref: str = "", source_header_row: int = 1) -> dict:
    """契约表: columns 与 headers 等长; 行给正整数 sourceRow(计算表统一 1, 真来源放 sourceRef)。"""
    clean = []
    for r in rows:
        vals = list(r) + [None] * (len(headers) - len(r))
        vals = [(_s(v, 240) if isinstance(v, str) else v) for v in vals[:len(headers)]]
        row = {"values": vals, "sourceRow": 1}
        if source_ref:
            row["sourceRef"] = source_ref
        clean.append(row)
    out = {"id": tid, "title": title, "headers": list(headers),
           "columns": list(range(1, len(headers) + 1)),
           "sourceHeaderRow": int(source_header_row), "rows": clean}
    if source_label:
        out["sourceLabel"] = source_label
    if source_ref:
        out["sourceRef"] = source_ref
    return out


def section(sid: str, title: str, tables: list[dict] | None = None,
            notes: list[str] | None = None, insights: list | None = None) -> dict:
    """`insights` 可以给字符串, 也可以给 `{"number","text","sourceRef"}` —— 后者会被 kit 查看器
    渲染成带编号的洞察卡片(app.js `insight-card`), 这正是 skill §7 要的"结论+价值+基准+含义"。"""
    def _item(x):
        return x if isinstance(x, dict) else {"text": x}
    return {"id": sid, "title": title, "tables": tables or [],
            "notes": [_item(t) for t in (notes or [])],
            "insights": [_item(t) for t in (insights or [])]}


def sheet(name: str, title: str, subtitle: str, kpis: list[dict], sections: list[dict]) -> dict:
    return {"name": name, "title": title, "subtitle": subtitle, "kpis": kpis, "sections": sections}


# --------------------------------------------------------------------------- ① 门槛

def locate_inputs(cat: str, data_root: str = "data", keyword: str | None = None,
                  product: str | None = None, voc: str | None = None) -> dict:
    """定位三份输入(显式传入的路径优先)。返回 {step: {name, expected, path, exists, explicit}}。"""
    cat_dir = Path(data_root) / cat
    overrides = {8: keyword, 18: product, 19: voc}
    out = {}
    for step, label, tpls in GATE_INPUTS:
        own = overrides.get(step)
        expected = [str(cat_dir / t.format(cat=cat)) for t in tpls]
        if own:
            p = Path(own)
            out[step] = {"name": label, "expected": expected, "path": str(p),
                         "exists": p.is_file(), "explicit": True}
            continue
        found = next((e for e in expected if Path(e).is_file()), None)
        out[step] = {"name": label, "expected": expected, "path": found or expected[0],
                     "exists": bool(found), "explicit": False}
    return out


def check_gate(inputs: dict) -> list[dict]:
    return [{"step": s, **v} for s, v in sorted(inputs.items()) if not v["exists"]]


def print_gate(inputs: dict, missing: list[dict]) -> None:
    print("=" * 96)
    print("整合市场报告 · 输入门槛检查(三份缺一不可)")
    print("=" * 96)
    for step, v in sorted(inputs.items()):
        print(f"  {'✅ 就位' if v['exists'] else '❌ 缺失'}  步骤{step:<2} {v['name']}")
        print(f"          使用路径: {v['path']}")
        if not v["exists"]:
            for e in v["expected"]:
                print(f"          期望路径: {e}")
    if missing:
        names = ", ".join("步骤%d %s" % (m["step"], m["name"]) for m in missing)
        print("\n" + "-" * 96)
        print(f"✗ 缺 {len(missing)} 份输入({names})")
        print("  口径(2026-10-09 用户要求): 三份缺一不可 → **拒绝生成整合报告**, 不产出部分报告。")
        print('  补齐方式: 先跑对应步骤(可单步, 例: python main.py step 19 --config "data/<CAT>/config.yaml"),')
        print("            再重新执行本脚本。")
    else:
        print("\n✓ 三份输入齐备, 继续生成。")


# --------------------------------------------------------------------------- ② 各模块

def _is_longtail_meta(row: dict, repeated: dict) -> bool:
    """`长尾词` 表里这一行是不是**方法/口径行**(不是关键词)?

    **委托给全仓库共用的规则** `core/keyword_report_clean.is_method_row()` —— 写入端(step8)、
    清理脚本(`scripts/clean_keyword_report.py`)与本报告用同一套判定, 避免"各处各写一版过滤"再次漂移。
    取不到该模块时退化为最小规则(仅认方法名), 保证报告生成不因缺依赖而失败。
    """
    kw = str(row.get("keyword") or "").strip()
    dim = str(row.get("dim") or "")
    note = str(row.get("note") or "")
    try:
        from core.keyword_report_clean import is_method_row
        return bool(is_method_row(kw, dim, note, repeated.get(kw, 1)))
    except Exception:                                   # noqa: BLE001
        return (not kw) or kw in ("搜索配置", "搜索口径", "口径", "方法", "备注")


def _kw_longtail_raw(src: Sources) -> list[dict]:
    """直读关键词报告 `长尾词` 表 → `[{keyword, dim, note}]`。

    为什么不用 `load_keyword()` 的 `longtail`: 引擎只保留 `长尾词/维度` 两列, **丢掉 `说明`** ——
    而源表里"方法/口径行"的 `说明` 才是搜索口径正文(2026-10-10 用户发现"维度列全空"后核出:
    源表把 `搜索页数/广告处理/清单合并` 写进了 `维度` 列, 方法说明在 `说明` 列)。
    """
    path = Path(getattr(src, "dir", ".")) / ("%s-keyword-report.xlsx" % src.cat)
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:                                  # noqa: BLE001
        return []
    try:
        if "长尾词" not in wb.sheetnames:
            return []
        it = wb["长尾词"].iter_rows(values_only=True)
        hdr = [str(h) if h is not None else "" for h in next(it)]
        idx = {c: hdr.index(c) for c in ("长尾词", "维度", "说明") if c in hdr}
        out = []
        for row in it:
            kw = _s(row[idx["长尾词"]]) if "长尾词" in idx and idx["长尾词"] < len(row) else ""
            if not kw:
                continue
            out.append({"keyword": kw,
                        "dim": _s(row[idx["维度"]]) if "维度" in idx and idx["维度"] < len(row) else "",
                        "note": _s(row[idx["说明"]]) if "说明" in idx and idx["说明"] < len(row) else ""})
        return out
    finally:
        try:
            wb.close()
        except Exception:                              # noqa: BLE001
            pass


def _kw_verdict_reasons(src: Sources) -> dict:
    """从关键词报告 `关键词总表` 读 `判定/原因`(核心词分析表没有"原因"列, 而 skill §3 的
    选中表要求 `说明`)。读不到就返回空字典, 由调用方降级——绝不编原因。"""
    path = Path(getattr(src, "dir", ".")) / ("%s-keyword-report.xlsx" % src.cat)
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:                                  # noqa: BLE001
        return {}
    try:
        if "关键词总表" not in wb.sheetnames:
            return {}
        it = wb["关键词总表"].iter_rows(values_only=True)
        hdr = [str(h) if h is not None else "" for h in next(it)]
        i_kw = hdr.index("关键词") if "关键词" in hdr else 0
        i_v = hdr.index("判定") if "判定" in hdr else None
        i_r = hdr.index("原因") if "原因" in hdr else None
        out = {}
        for row in it:
            kw = _s(row[i_kw]) if i_kw < len(row) else ""
            if not kw:
                continue
            out[kw] = (_s(row[i_v]) if i_v is not None and i_v < len(row) else "",
                       _s(row[i_r]) if i_r is not None and i_r < len(row) else "")
        return out
    finally:
        try:
            wb.close()
        except Exception:                              # noqa: BLE001
            pass


def _kw_trend_text(w: dict, month_row: dict | None) -> str:
    """`搜索趋势` 单元格: 站内同比 + Google 同比 + 源表季节性(旺季/淡季/峰谷比)。缺失一律写『未提供』。"""
    bits = []
    if w.get("mom_yoy") is not None:
        bits.append("站内同比 %+.1f%%" % w["mom_yoy"])
    else:
        bits.append("站内同比 未提供")
    if w.get("google_yoy") is not None:
        bits.append("Google同比 %+.1f%%" % w["google_yoy"])
    if w.get("season_in"):
        bits.append(w["season_in"])
    return " · ".join(bits)


def _kw_june(month_row: dict | None):
    return (month_row or {}).get("6月")


def _norm_phrase(text) -> str:
    """英文短语归一: 小写 + 标点/连字符→空格 + 折叠空白。

    只用于**匹配**(contract §2 要求归一化同时保留原文); 展示一律用源文原样。
    """
    t = str(text or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _kw_raw_json(src: Sources, suffix: str):
    """读关键词流水线的 FR-0x 原始文件(双榜 ASIN / 词指标 / 站内月度 / 判定); 缺失返回 None。"""
    p = Path(getattr(src, "dir", ".")) / ("%s-%s" % (src.cat, suffix))
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                   # noqa: BLE001
        return None


def _kw_dual_rank(src: Sources, phrases: list) -> dict:
    """**双榜词组支持**(FR-00 榜单 + FR-01 详情)。

    contract §2: 保留**全部榜单出现**(两榜 Top50 即 100 次), 但分析按 **ASIN 去重**并保留双榜归属;
    唯一支持数与出现次数**分开计**; 覆盖率分母用唯一 ASIN(不得拿原始出现数当分母);
    `NR − BS` 百分点差: ≥ +8pp 新品增强 / ≤ −8pp 成熟榜更强 / 其余两榜接近(语言覆盖信号, 非搜索量)。
    """
    fr00 = _kw_raw_json(src, "bestseller-newrelease.json")
    if not isinstance(fr00, dict) or not (fr00.get("bs") or fr00.get("nr")):
        return {"available": False, "reason": "FR-00 双榜榜单文件缺失或为空", "rows": [], "counts": []}
    bs = [str(a) for a in (fr00.get("bs") or [])]
    nr = [str(a) for a in (fr00.get("nr") or [])]
    union = [str(a) for a in (fr00.get("union") or list(dict.fromkeys(bs + nr)))]
    bs_set, nr_set = set(bs), set(nr)
    details, fetched, bad = {}, 0, 0
    p = Path(getattr(src, "dir", ".")) / ("%s-dual-details.jsonl" % src.cat)
    if p.is_file():
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:                       # noqa: BLE001
                    bad += 1
                    continue
                if not ev.get("ok"):
                    continue
                details[str(ev.get("request_asin"))] = (
                    _norm_phrase(ev.get("title")),
                    _norm_phrase(" ".join([str(ev.get("title") or "")] + [str(b) for b in (ev.get("bullets") or [])])),
                )
                fetched += 1
    both = bs_set & nr_set
    counts = [
        ["双榜原始出现次数(BS+NR)", len(bs) + len(nr), "保留全部榜单出现(每榜按 ASIN 计 1 次)"],
        ["唯一 ASIN 数(分析分母)", len(union), "按 ASIN 去重; **不接受**把原始出现数当分母"],
        ["同时在两榜的 ASIN", len(both), "保留双榜归属, 不重复计权重"],
        ["仅 BS(成熟榜)", len(bs_set - nr_set), "—"],
        ["仅 NR(新品榜)", len(nr_set - bs_set), "—"],
        ["详情抓取成功数", fetched, "FR-01 ok=true; 解析失败 %d 行" % bad],
        ["标题/全文可用数", len(details), "缺失 ASIN 以归一标识兜底并标注歧义"],
    ]
    rows = []
    for ph in phrases:
        np_ = _norm_phrase(ph)
        if not np_:
            continue
        uniq = title_hit = full_hit = occ = 0
        b_hit = n_hit = 0
        for asin in union:
            t, full = details.get(asin, ("", ""))
            if not t and not full:
                continue
            hit_title = bool(np_) and np_ in t
            hit_full = bool(np_) and (np_ in full or hit_title)
            if hit_title:
                title_hit += 1
            if hit_full:
                full_hit += 1
                uniq += 1
                occ += (1 if asin in bs_set else 0) + (1 if asin in nr_set else 0)
            if asin in bs_set and hit_full:
                b_hit += 1
            if asin in nr_set and hit_full:
                n_hit += 1
        bs_cov = (b_hit / len(bs_set)) if bs_set else None
        nr_cov = (n_hit / len(nr_set)) if nr_set else None
        gap = (nr_cov - bs_cov) * 100.0 if (bs_cov is not None and nr_cov is not None) else None
        _base = _rules.dual_rank_status(gap)               # ±8pp 边界定义在 skill 里
        if uniq == 0:
            sig = "无榜单支持(未命中)"
        else:
            sig = {"新品增强": "新品增强(≥+8pp)", "成熟榜更强": "成熟榜更强(≤-8pp)",
                   "两榜接近": "两榜接近"}.get(_base, _base)
        rows.append([ph, uniq, occ, title_hit, full_hit, len(union),
                     _pct(bs_cov) if bs_cov is not None else "未提供",
                     _pct(nr_cov) if nr_cov is not None else "未提供",
                     ("%+.1f" % gap) if (gap is not None and uniq) else "未提供", sig,
                     ("唯一支持 0 → 不做 NR−BS 信号判定; " if uniq == 0 else "")
                     + "组合支持=标题或五点命中; 分母=唯一 ASIN %d" % len(union)])
    return {"available": True, "rows": rows, "counts": counts, "reason": "",
            "bs_n": len(bs), "nr_n": len(nr), "union_n": len(union), "both_n": len(both)}


def _month_key(target: str, month: int) -> str:
    """`202608` + 月号 → `2026-08`。"""
    return "%s-%02d" % (str(target)[:4], month)


def _yoy_from_index(series: dict, target: str, month: int):
    """Google 指数同比 = 目标月 / 去年同月 − 1; 分母缺失/为 0 → None(**不臆断**)。"""
    if not isinstance(series, dict) or not target:
        return None, None, None
    cur_k = _month_key(target, month)
    prev_k = "%d-%02d" % (int(str(target)[:4]) - 1, month)
    cur, prev = series.get(cur_k), series.get(prev_k)
    if not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)) or prev <= 0:
        return None, cur, prev
    return (cur / prev - 1.0) * 100.0, cur, prev


def _yoy_from_monthly(rows, target: str, month: int):
    """站内同比**重算** = 目标月 / 去年同月 − 1(FR-06 逐月源行); 返回 (重算, 源 yoy, 目标月行)。"""
    if not isinstance(rows, list) or not target:
        return None, None, None
    cur_k = _month_key(target, month)
    prev_k = "%d-%02d" % (int(str(target)[:4]) - 1, month)
    cur = next((r for r in rows if str(r.get("month")) == cur_k), None)
    prev = next((r for r in rows if str(r.get("month")) == prev_k), None)
    if not cur:
        return None, None, None
    src_yoy = cur.get("yoy")
    cv, pv = cur.get("search_volume"), (prev or {}).get("search_volume")
    if not isinstance(cv, (int, float)) or not isinstance(pv, (int, float)) or pv <= 0:
        return None, src_yoy, cur
    return (cv / pv - 1.0) * 100.0, src_yoy, cur


def _kw_admission(src: Sources, words: list, target: str = "") -> tuple:
    """**四条准入判定**(contract §1/§6): 逐条给值、阈值、判定与失败/缺失原因, 并与源判定**对账**。

    阈值(默认): ①站内月搜索量 ≥30,000 ②站内同比 ≥-20% ③Google 同比 ≥-20% ④CPC <$3.00;
    边界: 前三条等号**通过**, CPC 恰为 3.00 **不通过**; 全部通过=达标; 有观测失败=不达标(并列出缺失项);
    无失败但有缺失/不可比=数据不足。**不做**加权评分。
    """
    fr05 = {e.get("keyword"): e for e in (_kw_raw_json(src, "amazon-keyword-data.json") or [])}
    fr06 = _kw_raw_json(src, "amazon-monthly.json") or {}
    fr07 = {e.get("keyword"): e for e in (_kw_raw_json(src, "screen-results.json") or [])}
    month = int(str(target)[4:6]) if len(str(target)) == 6 else 8
    rows, tally_src, tally_calc = [], {}, {}
    passed_names, failed_lines = [], []
    for w in words:
        name = w.get("keyword") or ""
        e = fr05.get(name)
        # **缺失 ≠ 0**(contract §6: 无观测失败时不得判不达标): 词不在 FR-05(源标注"关键字段缺失")
        # 或取值为 0/空时, 该指标视为**缺失**, 而不是"观测到 0 所以失败"。
        missing_all = e is None
        e = e or {}
        vol = e.get("monthly_search_volume")
        if vol is None:
            vol = w.get("search")
        if missing_all or not vol:
            vol = None
        cpc = e.get("cpc")
        if cpc is None:
            cpc = w.get("cpc")
        if missing_all or not cpc:
            cpc = None
        yoy_a, yoy_a_src, _ = _yoy_from_monthly(fr06.get(name), target, month)
        yoy_g, g_cur, g_prev = _yoy_from_index(e.get("google_monthly_index") or {}, target, month)
        # 判定规则本身来自 skill `keyword_admission`(阈值/等号边界/失败优先于缺失都在那里定义);
        # 本报告只负责把源操作数换算成它要求的量纲(同比 → 比率)并展示逐条结果。
        if vol is not None and vol < 0:
            vol = None                                    # 负值视为无效, 防止函数抛错
        if cpc is not None and cpc < 0:
            cpc = None
        res = _rules.keyword_admission(
            vol,
            (yoy_a / 100.0) if yoy_a is not None else None,
            (yoy_g / 100.0) if yoy_g is not None else None,
            cpc)
        _lbl = {"Amazon月搜索量": "①月搜索量", "Amazon同比": "②站内同比",
                "Google同比": "③Google同比", "Amazon CPC（USD）": "④CPC"}
        failed = [_lbl.get(x, x) for x in res["failed"]]
        missing = [_lbl.get(x, x) for x in res["missing"]]
        crit = []
        for label, val, txt in (("①月搜索量", vol, "≥30,000"), ("②站内同比", yoy_a, "≥-20%"),
                                ("③Google同比", yoy_g, "≥-20%"), ("④CPC", cpc, "<$3.00")):
            mark = "不通过" if label in failed else ("缺失" if label in missing else "通过")
            crit.append((label, txt, mark, (float(val) if val is not None else None)))
        fails, misses, final = failed, missing, res["status"]
        src_v = (fr07.get(name) or {}).get("verdict") or w.get("verdict") or "未提供"
        tally_src[src_v] = tally_src.get(src_v, 0) + 1
        tally_calc[final] = tally_calc.get(final, 0) + 1
        why = []
        for c in crit:
            if c[2] == "不通过":
                why.append("%s %s 未达阈值 %s" % (c[0], _fmt_num(c[3], 2), c[1]))
        for c in crit:
            if c[2] == "缺失":
                why.append("缺失: %s(需 %s 的可比值)" % (c[0], "去年同期" if "同比" in c[0] else "源字段"))
        if not why:
            why.append("四条全部通过(无缺失)")
        if final == "达标":
            passed_names.append(name)
        else:
            failed_lines.append("%s: %s" % (name, "; ".join(why)))
        recon = "一致" if src_v == final else "**不一致**(源: %s → 重算: %s)" % (src_v, final)
        aud = []
        if yoy_a is not None and isinstance(yoy_a_src, (int, float)):
            aud.append("站内同比重算 %s%% / 源 yoy %s%%" % (_fmt_num(yoy_a, 2), _fmt_num(yoy_a_src, 2)))
        if yoy_g is not None:
            aud.append("Google %s→%s: %s→%s" % (g_prev, g_cur, _fmt_num(g_prev, 0), _fmt_num(g_cur, 0)))
        rows.append([name, _fmt_num(vol, 0), crit[0][2], _fmt_num(yoy_a, 2), crit[1][2],
                     _fmt_num(yoy_g, 2), crit[2][2], _fmt_num(cpc, 2), crit[3][2],
                     final, "; ".join(why), recon + ("(" + "; ".join(aud) + ")" if aud else "")])
    t = table("kw-admission", "四条准入逐条判定(重算 + 与源判定对账)",
              ["关键词", "①月搜索量", "①判定", "②站内同比%", "②判定", "③Google同比%", "③判定",
               "④CPC(USD)", "④判定", "最终判定", "失败 / 缺失原因(逐条)", "与源判定对账"],
              rows, source_label="FR-05 词指标 + FR-06 站内月度 + FR-07 源判定",
              source_ref="阈值: 30,000 / -20% / -20% / $3.00")
    sum_rows = [
        ["四条件定义(完整)", "①站内月搜索量 ≥30,000 · ②站内同比 ≥-20% · ③Google 同比 ≥-20% · ④CPC <$3.00",
         "前三条**等号通过**, CPC 恰为 3.00 **不通过**; 币种 USD, **未做**他币转换"],
        ["重算计数", "达标 %d / 不达标 %d / 数据不足 %d"
         % (tally_calc.get("达标", 0), tally_calc.get("不达标", 0), tally_calc.get("数据不足", 0)),
         "口径: 全部通过=达标; 有观测失败=不达标(同时列出缺失项); 无失败但有缺失=数据不足"],
        ["源判定计数(对账)", "达标 %d / 不达标 %d / 数据不足 %d"
         % (tally_src.get("达标", 0), tally_src.get("不达标", 0), tally_src.get("数据不足", 0)),
         "来自 FR-07/源表 `判定` 列; 与重算不一致时**两值并列**, 不静默取一"],
        ["达标词", "、".join(passed_names) or "无", "这些词满足全部四条; **该门槛不是产品 Hero 的否决条件**"],
        ["未通过词与原因", " / ".join(failed_lines) or "无", "逐词给到**具体哪一条**未过或缺失(不用聚合分掩盖)"],
        ["目标月与口径", "目标月 %s(来源: 步骤8 `结论总览`); 站内同比用 FR-06 逐月源行重算; "
                         "Google 同比用 FR-05 逐月同尺度指数重算" % (target or "未提供"),
         "保留源 yoy 字段并列展示(审计); 不以相关词替代同一关键词; 比较月=上月与去年同月"],
    ]
    t_sum = table("kw-admission-summary", "准入口径、计数与逐词原因", ["项目", "内容", "口径 / 边界"],
                  sum_rows, source_label="keyword-analysis.md §月度指标与准入",
                  source_ref="FR-05 / FR-06 / FR-07")
    return t, t_sum


def _kw_season_gate(src: Sources, words: list) -> tuple:
    """**Google 同尺度季节门槛**(contract §7): 唯一门槛来源 = Google 共享尺度类别合成。

    步骤: ①各核心词逐月指数取**月份交集**(必须同尺度)→ 逐月**求和**得类别合成(不得平均各自归一化的请求);
    ②只取**完整自然年**; ③各年按当年月均值归一(年均=1.00); ④逐日历年月求跨年平均 → 月序指数;
    ⑤峰=最高 3 个月, 谷=最低 3 个月: **强度** mean(峰)/mean(谷) ≥1.20; **重复性** 峰月至少 2 个在 ≥2 年 >1.10
    且 谷月至少 2 个在 ≥2 年 <0.90(仅两年时两年都要满足)。不成立/数据不足 → **不生成**旺淡季对比块, 只留方法行。
    """
    fr05 = {e.get("keyword"): e for e in (_kw_raw_json(src, "amazon-keyword-data.json") or [])}
    series = {}
    for w in words:
        g = (fr05.get(w.get("keyword")) or {}).get("google_monthly_index") or {}
        if g:
            series[w.get("keyword")] = g
    if not series:
        return (table("kw-season-gate", "Google 同尺度季节门槛", ["项目", "值/结论", "口径"],
                      [["门槛结论", "季节性数据不足", "FR-05 未提供 Google 逐月同尺度序列"]],
                      source_label="keyword-monthly-seasonality §7", source_ref="FR-05"), None,
                ["Google 季节门槛数据不足 → 不生成旺/淡季对比块。"])
    common = set.intersection(*[set(g) for g in series.values()])
    months_all = sorted(common)
    comp = {m: sum(float(g[m]) for g in series.values()) for m in months_all}
    # 年度合成矩阵(每年 12 个实际月值, 缺月不补 0) → 交给 skill `seasonality_gate`;
    # 它会取**最近三个完整年**、按年均归一、算峰谷强度与两年重复性(阈值与并列规则都在 skill 内)。
    annual = {}
    for y in sorted({m[:4] for m in months_all}):
        vals = []
        for mm in range(1, 13):
            v = comp.get("%s-%02d" % (int(y), mm))
            vals.append(v)
        if all(v is not None for v in vals):
            annual[int(y)] = vals
    years = [str(y) for y in sorted(annual)]
    gate = _rules.seasonality_gate(annual, completed_through_year=(max(annual) if annual else 0),
                                   shared_comparison=True)
    verdict = gate["status"]
    gate_pass = verdict == "季节性成立"
    detail_ok = verdict != "季节性数据不足"
    month_idx = {i + 1: gate["indices"][i] for i in range(12)} if gate.get("indices") else {}
    peak3 = list(gate.get("peak_months") or [])
    off3 = list(gate.get("off_months") or [])
    strength = gate.get("peak_off_ratio")
    repeat_peak = gate.get("repeat_peak_count")
    repeat_off = gate.get("repeat_off_count")
    mn = lambda ms: "、".join("%d月" % m for m in ms) or "未提供"
    _gate_years = gate.get("years") or []
    gate_rows = [
        ["门槛来源", "Google Trends **共享尺度**类别合成(逐词同尺度序列求和; 取**最近三个完整年**, "
                     "阈值/重复性口径来自 skill `analysis_rules.seasonality_gate`)", "Amazon 月度/CPC 是下游对比指标, **不**用于判断季节块是否存在"],
        ["数据覆盖", "月份交集 %d 个月(%s → %s); 核心词 %d 个"
         % (len(months_all), months_all[0] if months_all else "—", months_all[-1] if months_all else "—", len(series)),
         "只保留各词都有的月份, 保证同尺度可比"],
        ["完整自然年", "%s(可用 %d 个%s)"
         % ("、".join(str(y) for y in _gate_years) if _gate_years else "无", len(_gate_years),
            "" if _gate_years else "; 原因: %s" % (gate.get("reason") or "未提供")),
         "要求 ≥2 个完整年(取最近三个); 13 个月 MoM/YoY 序列**不满足**两年门槛"],
        ["月序指数(归一化)", ("、".join("%d月 %.2f" % (m, month_idx[m]) for m in range(1, 13) if month_idx.get(m) is not None)
                          if month_idx else "未提供"),
         "各年按当年月均值归一(年均=1.00)后跨年平均"],
        ["候选峰月 / 谷月", "%s / %s" % (mn(peak3), mn(off3)), "峰=指数最高 3 个月, 谷=最低 3 个月"],
        ["强度(峰/谷)", _fmt_num(strength, 3) if strength is not None else "未提供", "门槛 ≥1.20"],
        ["重复性", "峰月达标 %s 个 / 谷月达标 %s 个(需各 ≥2 个且在 ≥2 年成立)"
         % (repeat_peak if repeat_peak is not None else "未提供", repeat_off if repeat_off is not None else "未提供"),
         "仅两年数据时两年都必须支持重复"],
        ["门槛结论", "**%s**" % verdict,
         "不成立/数据不足时: **不生成**旺淡季站内/Google/CPC 对比块, 只保留本方法行与原因"],
    ]
    t_gate = table("kw-season-gate", "Google 同尺度季节门槛(唯一下游门槛)", ["项目", "值 / 结论", "口径"],
                   gate_rows, source_label="keyword-monthly-seasonality.md §7",
                   source_ref="FR-05 google_monthly_index")
    notes = ["门槛用**逐月同尺度** Google 指数重算; Amazon 月度(源表 `季节性分析`)只作源数据视图, 不作门槛。"]
    detail = None
    if gate_pass:
        fr06 = _kw_raw_json(src, "amazon-monthly.json") or {}
        last_year = int(years[-1])
        rows = []

        def _avg(seq):
            xs = [x for x in seq if isinstance(x, (int, float))]
            return (sum(xs) / len(xs)) if xs else None

        for name in list(series) + ["【类别合成】"]:
            g = comp if name == "【类别合成】" else series[name]
            g_pk = _avg([g.get("%d-%02d" % (last_year, m)) for m in peak3])
            g_of = _avg([g.get("%d-%02d" % (last_year, m)) for m in off3])
            g_pk_py = _avg([g.get("%d-%02d" % (last_year - 1, m)) for m in peak3])
            a_rows = fr06.get(name) if name != "【类别合成】" else None
            a_pk = a_of = a_pk_py = None
            if isinstance(a_rows, list):
                bym = {str(r.get("month")): r.get("search_volume") for r in a_rows}
                a_pk = _avg([bym.get("%d-%02d" % (last_year, m)) for m in peak3])
                a_of = _avg([bym.get("%d-%02d" % (last_year, m)) for m in off3])
                a_pk_py = _avg([bym.get("%d-%02d" % (last_year - 1, m)) for m in peak3])
            rows.append([
                name,
                _fmt_num(a_pk, 0), _fmt_num(a_of, 0),
                ("%+.1f%%" % ((a_pk / a_of - 1) * 100)) if (a_pk and a_of) else "未提供",
                ("%+.1f%%" % ((a_pk / a_pk_py - 1) * 100)) if (a_pk and a_pk_py) else "未提供",
                _fmt_num(g_pk, 1), _fmt_num(g_of, 1),
                ("%+.1f%%" % ((g_pk / g_of - 1) * 100)) if (g_pk and g_of) else "未提供",
                ("%+.1f%%" % ((g_pk / g_pk_py - 1) * 100)) if (g_pk and g_pk_py) else "未提供",
                "未提供", "未提供(源报告只有当期 CPC 快照, 无逐月 CPC/Bid)",
            ])
        detail = table("kw-season-detail", "旺季 / 淡季对比(仅在门槛成立时生成)",
                       ["关键词", "旺季站内月均", "淡季站内月均", "站内峰/谷差", "旺季站内同比",
                        "旺季Google月均", "淡季Google月均", "Google峰/谷差", "旺季Google同比",
                        "非旺季−旺季 CPC 差", "CPC/Bid 月度"],
                       rows, source_label="FR-05 Google 序列 + FR-06 站内月度",
                       source_ref="峰月 %s / 谷月 %s; 参考年 %d 与 %d" % (mn(peak3), mn(off3), last_year, last_year - 1))
        notes.append("非旺季−旺季 CPC **差为正=淡季更贵(成本上升)**, 为负=旺季更贵; 本类目缺逐月 CPC → 如实 `未提供`。")
    else:
        notes.append("门槛结论 **%s** → 依合同要求**省略**旺淡季站内/Google/CPC 对比块; 源表 `执行建议` 中的旺季备货口径"
                     "属**源报告(站内月度)口径**, 未通过 Google 同尺度门槛, 不作为本报告结论。" % verdict)
    return t_gate, detail, notes


def _keyword_sheet(src: Sources) -> dict:
    """Sheet 1: 关键词分析(对齐 skill `report-spec.md` §3)。

    spec 组成: KPI + 核心词选中表 + 覆盖率图 + CPC 对比 + 搜索趋势折线 + `总结与洞察`≥5。
    本类目关键词报告**没有 `综合覆盖率` 列**(用户 2026-10-10 确认不出该维度) → 选中口径用
    `站内月搜索量` 并在源注里声明替代; 其余维度(KPI / CPC 对比 / 趋势折线 / 洞察卡片)按 spec 落地。
    """
    kw = src.load_keyword()
    if kw.get("missing"):
        raise FileNotFoundError("关键词报告缺失: %s" % kw.get("path"))
    words = kw["keywords"]
    qualified = [w for w in words if w["verdict"] == "达标"]
    ranked = sorted([w for w in words if (w["search"] or 0) > 0], key=lambda w: -(w["search"] or 0))
    top3 = ranked[:3]
    top = ranked[0] if ranked else None
    cpcs = [w["cpc"] for w in words if w["cpc"] is not None]
    cpc_mid = _median(cpcs)
    month_of = {r.get("关键词"): r for r in (kw.get("seasonality") or [])}
    reasons = _kw_verdict_reasons(src)
    longtail = _kw_longtail_raw(src) or [
        {"keyword": w.get("keyword"), "dim": w.get("dim"), "note": ""}
        for w in (kw.get("longtail") or [])]
    # 方法/口径行**整行丢弃**: 它们不是长尾词(35/35 份源报告都有这 3 行, `维度` 列被当成方法标签用),
    # 用户 2026-10-10 明确要求不要出现在报告任何位置 —— 连旁注也不留, 且不计入任何 KPI。
    _rep = {}
    for w in longtail:
        _rep[str(w.get("keyword") or "").strip()] = _rep.get(str(w.get("keyword") or "").strip(), 0) + 1
    lt_words = [w for w in longtail if not _is_longtail_meta(w, _rep)]
    lt_meta = [w for w in longtail if _is_longtail_meta(w, _rep)]

    q_search = sum(w["search"] or 0 for w in qualified)
    ref = "核心词分析!A1:K%d" % (len(words) + 1)

    kpis = [
        kpi("核心词数", len(words), ref), kpi("达标核心词", len(qualified), ref),
        kpi("达标词合计站内月搜索量", round(q_search, 1), ref),
        kpi("最高需求词", (top or {}).get("keyword") or "—", ref),
        kpi("CPC 中位(USD)", cpc_mid, ref),
        kpi("CPC 区间(USD)", ("%.2f–%.2f" % (min(cpcs), max(cpcs))) if cpcs else "未提供", ref),
        kpi("长尾词数(已剔除源表方法行)", len(lt_words), "长尾词!A1:C%d" % (len(longtail) + 1)),
    ]

    # ---- 核心词选中表(Top3) —— spec 列口径: 关键词|搜索趋势|CPC|说明(+ 量化证据列)
    core_rows = []
    for w in top3:
        mr = month_of.get(w["keyword"])
        v, why = reasons.get(w["keyword"], (w["verdict"], ""))
        note = (v or w["verdict"] or "未提供")
        if why:
            note += "(%s)" % why
        core_rows.append([w["keyword"], w["search"], _kw_june(mr), _kw_trend_text(w, mr),
                          w["cpc"], note])
    t_core = table("kw-core", "核心词选中(Top3, 按站内月搜索量; 本类目无综合覆盖率字段)",
                   ["关键词", "站内月搜索量", "6月搜索量", "搜索趋势", "CPC(USD)", "说明"],
                   core_rows, source_label="步骤8 关键词报告 · 核心词分析", source_ref=ref)

    # ---- CPC 对比 —— 选中词 vs 全体核心词基准(spec: CPC comparison)
    cpc_rows = [[w["keyword"], w["cpc"], w["cpc_min"], w["cpc_max"], "选中词(Top3)"] for w in top3]
    if cpcs:
        lows = [w["cpc_min"] for w in words if w["cpc_min"] is not None]
        highs = [w["cpc_max"] for w in words if w["cpc_max"] is not None]
        cpc_rows.append(["全体核心词基准(中位)", cpc_mid, _median(lows), _median(highs),
                         "本类目 %d 个核心词" % len(words)])
    t_cpc = table("kw-cpc", "CPC 对比(选中词 vs 类目基准)",
                  ["关键词/基准", "CPC(USD)", "CPC下限", "CPC上限", "口径"],
                  cpc_rows, source_label="步骤8 关键词报告 · 核心词分析(CPC/下限/上限)",
                  source_ref=ref)

    # ---- 搜索趋势折线(首列 月份 + 类别合成搜索量 → kit 折线图触发条件)
    base_names = {w["keyword"] for w in (qualified or kw.get("seasonality") or [])}
    month_rows = []
    for i in range(1, 13):
        m = "%d月" % i
        tot, hit = 0.0, 0
        for w in (kw.get("seasonality") or []):
            if w.get("关键词") in base_names and w.get(m) is not None:
                tot += w[m]
                hit += 1
        month_rows.append([m, round(tot, 1) if hit else None])
    t_month = table("kw-monthly", "核心词合计月搜索量(达标词 %d 个)" % len(base_names),
                    ["月份", "类别合成搜索量"], month_rows,
                    source_label="步骤8 关键词报告 · 季节性分析(按自然月求和)",
                    source_ref="季节性分析!A1:M%d" % (len(kw.get("seasonality") or []) + 1))

    # ---- 长尾词(spec: Top1 长尾 + 缺字段写『未提供』并说明补数需求)
    # 源表 `长尾词` 表里**混着方法/口径行**: `长尾词` 列的值是 `搜索配置`, 而把 `搜索页数/广告处理/清单合并`
    # 写进了 `维度` 列(2026-10-10 用户发现"维度列全是空白"时核出的真因)。这些行**不是关键词**:
    # 既不能当长尾词列出来, 也不该占一列"维度" —— 它们是搜索口径, 归到旁注披露。
    lt = None
    for cand in lt_words:
        m = next((w for w in words if w["keyword"] == cand["keyword"]), None)
        if m:
            lt = {"keyword": cand["keyword"], "dim": cand.get("dim") or "", "metrics": m}
            break
    if lt is None and lt_words:
        lt = {"keyword": lt_words[0]["keyword"], "dim": (lt_words[0].get("dim") or ""),
              "metrics": None}
    lt_rows = []
    if lt:
        m = lt["metrics"]
        mr = month_of.get(lt["keyword"])
        lt_rows.append([lt["keyword"], (m or {}).get("search", "未提供"),
                        (m or {}).get("mom_yoy", "未提供"), (m or {}).get("google_yoy", "未提供"),
                        (m or {}).get("cpc", "未提供"),
                        ("源表无可量化字段(仅人工清单) → 需求规模未验证, 需补搜索量/CPC 后再评估"
                         if not m else "命中核心词分析, 量化字段已补全")])
    t_long_top1 = table("kw-long-top1", "Top1 长尾词(按站内月搜索量在可匹配词中取最高)",
                        ["长尾词", "站内月搜索量", "站内同比%", "Google同比%", "CPC(USD)", "说明"],
                        lt_rows, source_label="步骤8 关键词报告 · 长尾词 + 核心词分析",
                        source_ref="长尾词!A1:C%d" % (len(longtail) + 1))
    # 用户口径(2026-10-10): 长尾词**不要"维度"这一维度** —— 固定 `# | 长尾词 | 说明` 两列+序号,
    # 不论源表 `维度` 列有没有值都不展示(全仓库 35/35 份源报告该列从未填过真实值)。
    long_rows = [[i + 1, w["keyword"], _s(w.get("note")) or "来自 FR-04 关键词文件(人工清单)"]
                 for i, w in enumerate(lt_words[:20])]
    long_head = ["#", "长尾词", "说明"]
    t_long = table("kw-long", "长尾词清单(前 20)",
                   long_head, long_rows, source_label="步骤8 关键词报告 · 长尾词",
                   source_ref="长尾词!A1:C%d" % (len(longtail) + 1))

    # ---- 总结与洞察(spec §3 Minimum insights: 竞争/最高价值/长尾/CPC/趋势/SEO-PPC)
    ins = []
    n = 0

    def add(text: str, sref: str = ref):
        nonlocal n
        n += 1
        ins.append({"number": str(n), "text": text, "sourceRef": sref})

    if top:
        share1 = (top["search"] or 0) / (sum(w["search"] or 0 for w in ranked) or 1)
        add("核心词竞争: 需求最高的是 `%s`(站内月搜索量 %s, 占前 %d 个有量核心词的 %.1f%%), "
            "第 2 名 `%s` 为 %s; 头部集中在少数词上, 投放预算应优先压在这些词上。"
            % (top["keyword"], _fmt_num(top["search"]), len(ranked), share1 * 100,
               (top3[1]["keyword"] if len(top3) > 1 else "—"),
               _fmt_num(top3[1]["search"]) if len(top3) > 1 else "—"))
    if words:
        qn = len(qualified)
        add("最高价值词(按需求且达标): %s; 全类目达标率 %d/%d = %.1f%%。"
            "达标=满足筛选标准, 未达标词**不能直接当主词投放**(原因见选中表『说明』列)。"
            % (("`%s`" % top["keyword"]) if top else "无有量核心词", qn, len(words),
               qn / len(words) * 100))
    if lt:
        if lt["metrics"]:
            add("长尾机会: `%s` 在源表中可匹配到量化字段(站内月搜索量 %s, CPC %s), "
                "长尾词通常 CPC 更低、意图更明确 → 适合做 PPC 精准长尾与 Listing 长尾覆盖。"
                % (lt["keyword"], _fmt_num(lt["metrics"].get("search")),
                   _fmt_num(lt["metrics"].get("cpc"))))
        else:
            add("长尾机会: Top1 长尾 `%s` 在关键词报告里**只有人工清单、无搜索量/CPC**(源表未提供) → "
                "需求规模未验证; 结论: 先补数(搜索量/CPC)再决定是否投入。缺字段不按 0 计。"
                % lt["keyword"])
    if cpcs:
        cheap = min((w for w in words if w["cpc"] is not None), key=lambda w: w["cpc"])
        dear = max((w for w in words if w["cpc"] is not None), key=lambda w: w["cpc"])
        add("CPC 规律: 中位 %.2f USD(区间 %.2f–%.2f), 最低 `%s` %.2f, 最高 `%s` %.2f —— "
            "最高/最低差 %.1f 倍; 高 CPC 词需要更强的 Listing 转化支撑, 否则 ROI 会被竞价吃掉。"
            % (cpc_mid, min(cpcs), max(cpcs), cheap["keyword"], cheap["cpc"],
               dear["keyword"], dear["cpc"], (dear["cpc"] / cheap["cpc"]) if cheap["cpc"] else 0))
    if ranked:
        yoy_all = [w["mom_yoy"] for w in words if w["mom_yoy"] is not None]
        yoy_q = [w["mom_yoy"] for w in qualified if w["mom_yoy"] is not None]
        if yoy_all:
            add("搜索趋势变化: 全体核心词站内同比均值 %+.1f%%%s —— "
                "需求%s; 趋势下行时更要靠差异化配置而不是堆投放。"
                % (sum(yoy_all) / len(yoy_all),
                   ("; 达标词均值 %+.1f%%" % (sum(yoy_q) / len(yoy_q))) if yoy_q else "",
                   "在收缩" if sum(yoy_all) / len(yoy_all) < 0 else "在扩张"))
        peaks = []
        for w in ranked:
            mr = month_of.get(w["keyword"]) or {}
            vals = [mr.get("%d月" % i) for i in range(1, 13)]
            vals = [v for v in vals if v]
            if len(vals) >= 12 and min(vals) > 0:
                peaks.append((max(vals) / min(vals), w["keyword"]))
        if peaks:
            peaks.sort(reverse=True)
            add("季节性: 峰谷比最高的是 `%s`(%.2f 倍) —— 备货与投放节奏应跟着峰值月走, "
                "谷值月降低站内竞价、把预算挪到 Listing 与评论积累。" % (peaks[0][1], peaks[0][0]))
    add("SEO/PPC 动作(在证据支持范围内): ① 主词用 `%s`(需求最高)做 Listing 标题/五点核心覆盖; "
        "② 长尾用清单词做 PPC 精准匹配(CPC 更低); ③ 未达标词先做原因整改再投放。"
        "以上为**关联与横截面对比**, 不构成因果证明(无转化/销量归因数据)。"
        % ((top or {}).get("keyword") or "—"))

    _ov = _kw_overview(src) or {}
    _target = str(kw.get("target_month") or _ov.get("目标月份") or "").strip()
    t_adm, t_adm_sum = _kw_admission(src, words, target=_target)
    _phrases = [w.get("keyword") for w in words if w.get("keyword")] + [x.get("keyword") for x in lt_words]
    dual = _kw_dual_rank(src, [p for p in _phrases if p])
    dual_tabs, dual_notes = [], []
    if dual.get("available"):
        dual_tabs.append(table("kw-dual", "双榜词组支持(原始出现 vs 唯一 ASIN)",
                               ["词组", "唯一ASIN支持", "原始出现支持", "标题支持", "全文支持", "唯一分母",
                                "BS 覆盖率", "NR 覆盖率", "NR−BS(pp)", "信号", "口径"],
                               dual["rows"], source_label="FR-00 双榜榜单 + FR-01 详情(标题/五点)",
                               source_ref="BS %d / NR %d / 唯一 %d" % (dual["bs_n"], dual["nr_n"], dual["union_n"])))
        dual_tabs.append(table("kw-dual-counts", "双榜计数审计(原始 vs 唯一)", ["项目", "值", "口径"],
                               dual["counts"], source_label="contract §2: 唯一支持数与出现次数分开计",
                               source_ref="FR-00"))
        dual_notes.append("信号阈值: `NR−BS ≥ +8pp` 新品增强 · `≤ −8pp` 成熟榜更强 · 其余两榜接近; "
                          "这是**语言覆盖信号**, 不等于搜索量/销量增长或人群结论。")
        dual_notes.append("命中判定用英文归一(小写/标点→空格); 展示保留原词; 覆盖率分母=**唯一 ASIN**。")
    else:
        dual_tabs.append(table("kw-dual-na", "双榜词组支持(数据不足)", ["项目", "结论", "原因"],
                               [["双榜词组支持", "数据不足", dual.get("reason") or "缺少原始榜单"],
                                ["口径", "不做臆断", "需 FR-00 榜单 + FR-01 详情(标题/五点)才能算覆盖与 NR−BS 差"]],
                               source_label="contract §2", source_ref="FR-00 / FR-01"))
    t_gate, t_season_detail, gate_notes = _kw_season_gate(src, words)
    sea_tabs = [t_gate] + ([t_season_detail] if t_season_detail else [])
    secs = [
        section("kw-1", "01 核心词选中与达标", [t_core], notes=[
            (kw.get("substitution") or ["关键词排序口径见源表"])[0],
            "spec 原要求 `覆盖率` 列与覆盖率图; 本类目关键词报告**无该字段**, 已按用户口径取消该维度。",
            "`搜索趋势` = 站内同比 + Google 同比 + 源表季节性(旺季/淡季/峰谷比); 缺失写『未提供』。",
        ]),
        section("kw-adm", "02 四条准入逐条判定(重算 + 对账)", [t_adm, t_adm_sum], notes=[
            "四条默认阈值来自 `keyword-analysis.md`: 30,000 / -20% / -20% / $3.00; 边界: 前三条等号通过, "
            "CPC 恰好 3.00 不通过。",
            "**重算优先**: 站内同比用 FR-06 逐月源行重算(与源 `yoy` 字段并列展示作审计); "
            "Google 同比用 FR-05 逐月同尺度指数重算。",
            "判定只用四条不等式, **不做**加权评分; `数据不足` ≠ `不达标`(无观测失败时不得判不达标)。",
        ]),
        section("kw-dual", "03 双榜词组支持(原始 vs 唯一)", dual_tabs, notes=dual_notes),
        section("kw-sea", "04 Google 同尺度季节门槛", sea_tabs, notes=gate_notes),
        section("kw-2", "05 CPC 对比", [t_cpc],
                notes=["对比基准 = 本类目全部核心词的 CPC 中位/下限中位/上限中位(源表逐词给区间)。"]),
        section("kw-3", "06 站内月度搜索趋势(源数据, 不作为季节门槛)", [t_month], notes=[
            "合计口径: 已达标核心词按自然月求和(源表 `季节性分析` 的 1–12 月列); 未达标/数据不足词不参与。",
            "**本块是源数据视图**: 站内月度不构成季节门槛(门槛只用 Google 同尺度序列, 见 04 节)。"]),
        section("kw-4", "07 长尾词机会", [t_long_top1, t_long],
                notes=["长尾词取自 FR-04 人工清单, 源表只给到词与说明、**没有量化字段** → 缺少的搜索量/CPC/趋势一律写 "
                       "`未提供`, 并注明需补数后再评估(不按 0 计)。"]),
        section("kw-5", "08 总结与洞察", [], insights=ins,
                notes=["写法遵循 skill §7: 结论 + 价值 + 对比基准 + 含义; 占比一位小数; 不确定处就近标注。"]),
    ]
    return sheet("关键词分析", "关键词分析",
                 "核心词选中 / CPC 对比 / 季节性趋势 / 长尾机会(来源: 步骤8 关键词报告)", kpis, secs)


def _in_band(band: dict, price: float | None) -> bool:
    """价格是否落在该段内(左闭右开; hi=None 表示最高段无上界)。"""
    if price is None:
        return False
    lo, hi = band.get("lo"), band.get("hi")
    if lo is not None and price < lo:
        return False
    if hi is not None and price >= hi:
        return False
    return True


def _band_of(bands: list[dict], price: float | None) -> str | None:
    """价格所属段标签; 超出最高段(极端尾部)归入最高段; 无价格返回 None。"""
    if price is None or not bands:
        return None
    for b in bands:
        if _in_band(b, price):
            return b.get("label")
    return bands[-1].get("label")


def _read_feature_matrix(workbook: str | None, headers: list[str]) -> dict:
    """把交付表里**所有** `是否*`/属性列的取值按 `xl_row` 读出来 → `{列名: {xl_row: 取值}}`。

    为什么不能只靠 `rec`: `tools/cat_report/engine.py` 的 `load_product()` 只把 `BINARY_FEATURES`
    (写死的 10 个 Cutting Boards 列)灌进记录 —— 其它类目的 `是否便携/是否免手持/是否带加热`
    在 `rec` 里**根本不存在**(`.get()` 得到 None)。直读源表才能让功能/属性迭代做到**类目无关**
    (2026-10-10 真事故: Dog Dryer 的功能迭代表全空)。
    """
    cols = [h for h in headers
            if str(h).startswith("是否") or str(h) in ATTR_COLS or str(h) in DIM_COLS]
    if not cols or not workbook:
        return {}
    try:
        wb = load_workbook(workbook, read_only=True, data_only=True)
    except Exception:                                  # noqa: BLE001
        return {}
    out: dict = {c: {} for c in cols}
    try:
        ws = wb["属性标注"] if "属性标注" in wb.sheetnames else wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        hdr = [str(h) if h is not None else "" for h in next(it)]
        idx = {c: hdr.index(c) for c in cols if c in hdr}
        for k, row in enumerate(it, start=2):          # 与 rec["xl_row"] 对齐(表头=1)
            for c, i in idx.items():
                out[c][k] = _s(row[i]) if i < len(row) else ""
    finally:
        try:
            wb.close()
        except Exception:                              # noqa: BLE001
            pass
    return out


def _feature_dist_rows(workbook: str | None, headers: list[str]) -> list[list]:
    """**直接从步骤18 交付表**读 `是否*` 列的取值分布(不依赖引擎的类目词表)。

    为什么绕开 `rec[...]`: `tools/cat_report/engine.py` 只把 `BINARY_FEATURES`(写死的 10 个
    Cutting Boards 列)灌进记录, 其它类目的 `是否*` 列(如 Dog Dryer 的 是否便携/是否免手持/是否带加热)
    在记录里根本不存在 → 取到 None → 宣称率表与取值分布都空。这里按列名直读源表, 通用且不做是/否臆断。
    """
    cols = [h for h in headers if str(h).startswith("是否")]
    if not cols or not workbook:
        return []
    try:
        wb = load_workbook(workbook, read_only=True, data_only=True)
    except Exception:                                  # noqa: BLE001 — 读不到就退回"无数据"
        return []
    try:
        ws = wb["属性标注"] if "属性标注" in wb.sheetnames else wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        hdr = [str(h) for h in next(it)]
        idx = {c: hdr.index(c) for c in cols if c in hdr}
        counts = {c: {} for c in idx}
        for row in it:
            for c, i in idx.items():
                if i >= len(row):
                    continue
                v = _s(row[i])
                if v and v not in VAGUE:
                    counts[c][v] = counts[c].get(v, 0) + 1
    finally:
        try:
            wb.close()
        except Exception:                              # noqa: BLE001
            pass
    rows = []
    for col, cnt in counts.items():
        tot = sum(cnt.values())
        for v, n in sorted(cnt.items(), key=lambda x: -x[1])[:4]:
            rows.append([col, v, n, _share(n, tot)])
    return rows


ATTR_COLS = ("产品类型", "机身材质", "主体材质", "材质", "套装数量", "形状", "尺寸规格", "容量",
             "干燥方式", "电机类型", "过热保护", "适用宠物体型", "适用毛发类型", "配件类型", "配件数量",
             "温度档位", "风速档位", "线长", "额定功率", "噪音水平", "吸力大小", "最大风速")
DIM_COLS = ("商品尺寸", "商品尺寸（单位换算）", "包装尺寸", "商品重量", "包装重量")
MATERIAL_COLS = ("机身材质", "主体材质", "材质", "材料", "碗体材质")


def _parse_dims(val) -> tuple | None:
    """从 `商品尺寸` 这类文本里取 (长, 宽, 高)(取前 3 个数字, 单位不参与计算)。
    spec §4 要求: **包装尺寸不能与单品尺寸混用** → 调用方只传单品尺寸列。"""
    nums = re.findall(r"\d+(?:\.\d+)?", str(val or ""))
    if len(nums) < 3:
        return None
    try:
        return tuple(float(x) for x in nums[:3])
    except ValueError:
        return None


def _first_col(headers: list[str], *cands: str) -> str | None:
    """返回表里实际存在的第一个候选列名(如 机身材质 / 主体材质 的差异)。"""
    for c in cands:
        if c in headers:
            return c
    return None


ATTR_COLS_LEGACY_UNUSED = ("产品类型", "主体材质", "套装数量", "形状", "尺寸规格", "容量", "用途", "风格")


def _concentration(shares: list[float]) -> tuple[dict, float]:
    """品牌集中度: CRn = Top-n 品牌销额占比之和; HHI = Σ(占比²) × 10,000(skill §1 明确口径)。"""
    cr = {k: round(sum(shares[:k]), 4) for k in (1, 3, 5)}
    return cr, round(sum(s * s for s in shares) * 10000, 1)


def _col_value(r: dict, col: str, extra: dict | None) -> str:
    """取该行在某列的取值: 优先**源表矩阵**(类目无关), 回退 `rec`(引擎已映射的列)。"""
    if extra and col in extra:
        v = extra[col].get(r.get("xl_row"))
        if v is not None:
            return _s(v)
    return _s(r.get(col))


def _cohort_stats(valid: list[dict], col: str, positive_of, extra: dict | None = None) -> dict | None:
    """一个新老品对比行(coverage 口径: 具备数/已明确数/覆盖率/证据完整度)。

    `positive_of(value) -> True|False|None`(None=未明确)。返回 None 表示该列样本不足。
    """
    rows = []
    for lc in ("新品", "老品"):
        grp = [r for r in valid if _s(r.get("lifecycle")) == lc]
        known = [v for v in (positive_of(_col_value(r, col, extra)) for r in grp) if v is not None]
        yes = sum(1 for v in known if v)
        rows.append({"total": len(grp), "known": len(known), "yes": yes,
                     "rate": (yes / len(known)) if known else None,
                     "complete": (len(known) / len(grp)) if grp else 0.0})
    if rows[0]["known"] < 5 or rows[1]["known"] < 5:
        return None
    return {"new": rows[0], "old": rows[1]}


def _dominant_stats(valid: list[dict], col: str, extra: dict | None = None) -> dict | None:
    """未登记『是/否』词表的列: 用**该列取值最多的配置**(主配置)对比新老品 —— 不做是/否臆断。"""
    cnt = {}
    for r in valid:
        v = _col_value(r, col, extra)
        if v and v not in VAGUE:
            cnt[v] = cnt.get(v, 0) + 1
    if not cnt:
        return None
    dom = sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[0][0]
    rows = []
    for lc in ("新品", "老品"):
        grp = [r for r in valid if _s(r.get("lifecycle")) == lc]
        vals = [_col_value(r, col, extra) for r in grp]
        known = [v for v in vals if v and v not in VAGUE]
        hit = sum(1 for v in known if v == dom)
        rows.append({"total": len(grp), "known": len(known), "yes": hit,
                     "rate": (hit / len(known)) if known else None,
                     "complete": (len(known) / len(grp)) if grp else 0.0})
    if rows[0]["known"] < 5 or rows[1]["known"] < 5:
        return None
    return {"dominant": dom, "new": rows[0], "old": rows[1]}


def _sample_note(st: dict) -> str:
    """信号谨慎线(market-analysis §Lifecycle): 每队列**已明确数 ≥30 且完整度 ≥50%** 才可信;
    肯定数 <20 记『明确数少』。谨慎线只降低结论强度, **不否决**有销量支撑的单个新品。"""
    # `known` 只在队列统计里存在; 价段样本充足度那种精简调用只有 complete/yes → 仅当有该键时应用 ≥30 规则
    _kn = lambda side: st[side]["known"] if isinstance(st.get(side), dict) and "known" in st[side] else None
    thin = (st["new"]["complete"] < 0.5 or st["old"]["complete"] < 0.5
            or (_kn("new") is not None and _kn("new") < 30)
            or (_kn("old") is not None and _kn("old") < 30))
    sparse = st["new"]["yes"] < 20 or st["old"]["yes"] < 20
    if thin:
        return "证据覆盖低"
    if sparse:
        return "明确数少"
    return "样本充分"


def _life_rows(valid: list[dict], col: str, stats: dict, dominant: bool) -> list:
    d = (stats["new"]["rate"] or 0) - (stats["old"]["rate"] or 0)
    if dominant:
        return [stats["dominant"], col,
                stats["new"]["yes"], stats["old"]["yes"],
                stats["new"]["known"], stats["old"]["known"],
                (round(stats["new"]["rate"], 4) if stats["new"]["rate"] is not None else None),
                (round(stats["old"]["rate"], 4) if stats["old"]["rate"] is not None else None),
                round(d * 100, 2), _sample_note(stats),
                round(stats["new"]["complete"], 4), round(stats["old"]["complete"], 4)]
    return [col, col,
            stats["new"]["yes"], stats["old"]["yes"],
            stats["new"]["known"], stats["old"]["known"],
            (round(stats["new"]["rate"], 4) if stats["new"]["rate"] is not None else None),
            (round(stats["old"]["rate"], 4) if stats["old"]["rate"] is not None else None),
            round(d * 100, 2), _sample_note(stats),
            round(stats["new"]["complete"], 4), round(stats["old"]["complete"], 4)]


def _voc_brand_map(path) -> dict:
    """从 VOC 工作簿 `品牌_ASIN对比` 读**品牌级**证据 —— spec §4 ⑩ 要求"**只在能直接匹配时**给品牌 VOC,
    匹配不上的留在类目级风险块并标注"。读不到就返回空字典 → 调用方一律写 `类目级VOC，非品牌归因`。
    """
    if not path:
        return {}
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:                                  # noqa: BLE001
        return {}
    try:
        if "品牌_ASIN对比" not in wb.sheetnames:
            return {}
        it = wb["品牌_ASIN对比"].iter_rows(values_only=True)
        hdr, out = None, {}
        for row in it:
            if row and str(row[0]).strip() == "父ASIN":
                hdr = [str(h).strip() if h is not None else "" for h in row]
                continue
            if hdr is None or not row or row[0] in (None, ""):
                continue
            d = {hdr[i]: row[i] for i in range(min(len(hdr), len(row)))}
            b = _s(d.get("品牌"))
            if b:
                out[b] = d
        return out
    finally:
        try:
            wb.close()
        except Exception:                              # noqa: BLE001
            pass


def _market_sheet(src: Sources, recs: list[dict], extra: dict | None = None,
                  voc_path: str | None = None) -> dict:
    """Sheet 2: 市场大盘分析(对齐 skill `report-spec.md` §4 的 11 个模块顺序)。

    表名/表头**刻意**与 vendored 查看器的 `chartSpec()` 约定一致(`价格段`+月销额→柱图、
    `生命周期`+三口径占比→构成图、`属性/功能`+覆盖率/主配置占比→配对图、`品牌`→分散图、
    `履约方式`/`产品类型`/`主体材质`+销额占比→环图、`功能`+均价差→正负价差图), 否则图表不出。
    """
    headers = list(src.header or [])
    valid = _valid_variants(recs)
    prices = [r["price"] for r in valid if (r.get("price") or 0) > 0]
    total_rev, total_sales = _sum(valid, "revenue"), _sum(valid, "sales")
    zero_rating = [r for r in valid if r.get("rating") == 0]
    ratings = [r["rating"] for r in valid if (r.get("rating") or 0) > 0]
    brands = sorted({_s(r.get("brand")) for r in valid if _s(r.get("brand"))})
    parents = sorted({_s(r.get("parent")) for r in valid if _s(r.get("parent"))})
    ref = "属性标注!A1:DQ%d" % (len(recs) + 1)

    band_note = None
    try:
        info = choose_price_bands(prices) or {}
    except Exception as exc:                                   # noqa: BLE001
        info, band_note = {}, "价格分段计算失败(%s: %s) → 本节按整体给一行。" % (type(exc).__name__, exc)
    bands = [b for b in (info.get("bands") or []) if isinstance(b, dict)]
    if not bands and valid:
        bands = [{"lo": None, "hi": None, "label": "全部(样本过少, 未分段)"}]
        band_note = band_note or ("有效变体仅 %d 个 → 动态分段不可靠, 本节按**整体**给一行(不分段)。"
                                  % len(valid))

    brand_rows = []
    for b in brands:
        grp = [r for r in valid if _s(r.get("brand")) == b]
        rev = _sum(grp, "revenue")
        brand_rows.append([b, len(grp), round(rev, 2), _share(rev, total_rev),
                           _median([r["price"] for r in grp if r.get("price") is not None])])
    brand_rows.sort(key=lambda r: -(r[3] or 0))
    cr, hhi = _concentration([r[3] or 0 for r in brand_rows])

    life = {}
    for name in ("新品", "老品"):
        grp = [r for r in valid if _s(r.get("lifecycle")) == name]
        life[name] = {"n": len(grp), "sale": _sum(grp, "sales"), "rev": _sum(grp, "revenue")}

    kpis = [
        kpi("有效变体数", len(valid), ref), kpi("父体数", len(parents)),
        kpi("变体月销量合计", round(total_sales, 1)),
        kpi("变体月销额合计(USD)", round(total_rev, 2)),
        kpi("均价(USD)", _mean(prices)), kpi("中位价(USD)", _median(prices)),
        kpi("品牌 HHI(×10,000)", hhi),
        kpi("成交加权价格(USD, 可选口径)",
            round(total_rev / total_sales, 2) if total_sales else None, ref),
    ]

    # ---------------------------------------------------------------- ① 报告摘要
    feat_cols = [h for h in headers if str(h).startswith("是否")]
    dom_bits = []
    for col in feat_cols:
        st = _dominant_stats(valid, col, extra)
        if st:
            dom_bits.append("%s=%s(%.1f%%)" % (col, st["dominant"], (st["new"]["rate"] or 0) * 100))
    missing_cols = [c for c in ATTR_COLS if c in headers]
    if not _first_col(headers, *MATERIAL_COLS):
        missing_cols.append("主体材质")      # 只有**一个材质列都没有**时才报缺(别名不算缺:S 机身材质)
    empty_attrs = [c for c in missing_cols if c not in headers or not any(
        _col_value(r, c, extra) and _col_value(r, c, extra) not in VAGUE for r in valid)]
    sum_rows = [
        ["样本", "有效变体 %d 个 / 父体 %d 个 / 品牌 %d 个" % (len(valid), len(parents), len(brands)),
         "变体口径(源表 变体月销量/变体月销额)"],
        ["规模", "月销量 %s / 月销额 %s USD" % (_fmt_num(total_sales, 1), _fmt_num(total_rev, 2)),
         "全部有效变体合计"],
        ["价格", "均价 %s / 中位价 %s / 成交加权价 %s USD"
         % (_fmt_num(_mean(prices), 2), _fmt_num(_median(prices), 2),
            _fmt_num(round(total_rev / total_sales, 2) if total_sales else None, 2)),
         "均价=算术平均; 成交加权价另列, 不当均价用(skill §1)"],
        ["价格结构", "%d 段: %s" % (len(bands), " / ".join(str(b.get("label")) for b in bands[:8])),
         info.get("rationale") or "样本过少未分段"],
        ["新老品", "新品 数量 %.1f%% / 销量 %.1f%% / 销额 %.1f%%"
         % ((_share(life["新品"]["n"], len(valid)) or 0) * 100,
            (_share(life["新品"]["sale"], total_sales) or 0) * 100,
            (_share(life["新品"]["rev"], total_rev) or 0) * 100),
         "三口径分母分别为 变体数/月销量/月销额"],
        ["集中度", "CR1 %.4f / CR3 %.4f / CR5 %.4f / HHI %.1f" % (cr[1], cr[3], cr[5], hhi),
         "品牌销额占比(分母=全部有效变体月销额); HHI=Σ占比²×10,000"],
        ["功能主配置", "; ".join(dom_bits[:3]) or "未提供", "该列取值最多的配置(不做是/否判定)"],
        ["评分", "有效评分均值 %s(排除 评分=0 的 %d 个变体)"
         % (_fmt_num(_mean(ratings), 2), len(zero_rating)), "skill §1: 评分=0 不视为差评"],
        ["缺失披露", ("源表整体缺失这些属性列: %s" % "、".join(empty_attrs)) if empty_attrs
         else "属性列齐备", "缺失一律披露, 不按 0 计"],
    ]
    t_sum = table("m-summary", "报告摘要(关键结论与口径)", ["维度", "结论", "口径/基准"],
                  sum_rows, source_label="步骤18 属性标注-标准化交付", source_ref=ref)

    # ---------------------------------------------------------------- ② 市场大盘与价格段
    band_rows = []
    for b in bands:
        grp = [r for r in valid if _in_band(b, r.get("price"))]
        rev = _sum(grp, "revenue")
        band_rows.append([b.get("label") or "全部", len(grp), _share(len(grp), len(valid)),
                          round(_sum(grp, "sales"), 1), round(rev, 2), _share(rev, total_rev)])
    t_price = table("m-price", "价格段",                    # ← 表名必须恰好是 `价格段`(柱图触发)
                    ["价格段", "变体数", "数量占比", "月销量", "月销额", "销额占比"], band_rows,
                    source_label="步骤18 · 变体口径", source_ref=ref)
    qs = sorted(prices)

    def _q(p):
        return round(qs[min(len(qs) - 1, int(len(qs) * p))], 2) if qs else None

    t_price_q = table("m-price-q", "价格分位(USD)", ["分位", "价格(USD)", "口径"],
                      [["P10", _q(0.10), "有效变体价格(%d 个)" % len(qs)],
                       ["P25", _q(0.25), "左闭右开分位"],
                       ["P50(中位)", _q(0.50), ""],
                       ["P75", _q(0.75), ""],
                       ["P90", _q(0.90), ""],
                       ["P100(最高)", _q(0.999), ""]],
                      source_label="步骤18 · 价格列", source_ref=ref)

    # ---------------------------------------------------------------- ③ 新品老品分析
    life_rows_t = []
    for name in ("新品", "老品"):
        d = life[name]
        if not d["n"]:
            continue
        life_rows_t.append([name, d["n"], _share(d["n"], len(valid)), round(d["sale"], 1),
                            _share(d["sale"], total_sales), round(d["rev"], 2),
                            _share(d["rev"], total_rev)])
    t_life = table("m-life", "生命周期",                 # ← 表名必须恰好是 `生命周期`(构成图触发)
                   ["生命周期", "变体数", "数量占比", "月销量", "销量占比", "月销额", "销额占比"],
                   life_rows_t, source_label="步骤18 · 新老品", source_ref=ref)

    # ---------------------------------------------------------------- ④ 新品老品功能与属性迭代
    func_rows, claim_rows = [], []
    for col in feat_cols:
        st_cov = _cohort_stats(valid, col, lambda v, c=col: _attr_state(c, v), extra)
        if st_cov:
            func_rows.append(_life_rows(valid, col, st_cov, dominant=False))
        st_dom = _dominant_stats(valid, col, extra)
        if st_dom:
            claim_rows.append(_life_rows(valid, col, st_dom, dominant=True))
    func_rows.sort(key=lambda r: -abs(r[8] or 0))
    claim_rows.sort(key=lambda r: -abs(r[8] or 0))
    t_func = (table("m-life-func",
                   "功能迭代(覆盖率口径: 具备数 / 已明确数 / 证据完整度)",
                   ["属性/功能", "所属列", "新品具备数", "老品具备数", "新品已明确数", "老品已明确数",
                    "新品覆盖率", "老品覆盖率", "百分点差", "样本判定", "新品证据完整度", "老品证据完整度"],
                    func_rows[:18],
                    source_label="步骤18 · 是否* 列(三态; 未明确不计入分母)", source_ref=ref)
              if func_rows else None)      # 无行不出表(空表会被查看器渲染成空壳)
    t_claim = (table("m-life-claim",
                    "功能取值迭代(主配置口径; 本类目取值未登记『是/否』词表 → 不做是/否判定)",
                    ["属性/功能", "所属列", "新品主配置数", "老品主配置数", "新品已明确数", "老品已明确数",
                     "新品主配置占比", "老品主配置占比", "百分点差", "样本判定",
                     "新品证据完整度", "老品证据完整度"],
                     claim_rows[:18],
                     source_label="步骤18 · 是否* 列(取值最多的配置=主配置)", source_ref=ref)
               if claim_rows else None)
    attr_rows = []
    for col in [c for c in ATTR_COLS if c in headers]:
        st = _dominant_stats(valid, col, extra)
        if st:
            attr_rows.append(_life_rows(valid, col, st, dominant=True))
    attr_rows.sort(key=lambda r: -abs(r[8] or 0))
    t_attr = (table("m-life-attr", "属性结构迭代(主配置口径: 产品类型/材质/套装等)",
                   ["属性/功能", "所属列", "新品主配置数", "老品主配置数", "新品已明确数", "老品已明确数",
                    "新品主配置占比", "老品主配置占比", "百分点差", "样本判定",
                    "新品证据完整度", "老品证据完整度"],
                    attr_rows[:10], source_label="步骤18 · 属性列", source_ref=ref)
              if attr_rows else None)

    # ---------------------------------------------------------------- ⑤ 品牌与履约集中度
    t_brand = table("m-brand", "品牌",                   # ← 表名必须恰好是 `品牌`(分散图触发)
                    ["品牌", "变体数", "月销额", "销额占比", "中位价"], brand_rows[:10],
                    source_label="步骤18 · 品牌(分母=全部有效变体月销额)", source_ref=ref)
    top_brand_rows = []
    for b, n, rev, share, med in brand_rows[:10]:
        grp = [r for r in valid if _s(r.get("brand")) == b]
        new_n = sum(1 for r in grp if _s(r.get("lifecycle")) == "新品")
        old_n = sum(1 for r in grp if _s(r.get("lifecycle")) == "老品")
        ps = [r["price"] for r in grp if r.get("price") is not None]
        rs = [r["rating"] for r in grp if (r.get("rating") or 0) > 0]
        ful = {}
        for r in grp:
            k = _s(r.get("fulfilment")) or "未提供"
            ful[k] = ful.get(k, 0) + (_i(r.get("revenue")) or 0)
        ful_txt = " / ".join("%s %.0f%%" % (k, v / (sum(ful.values()) or 1) * 100)
                             for k, v in sorted(ful.items(), key=lambda x: -x[1])[:3])
        top_brand_rows.append([b, n, new_n, old_n, round(_sum(grp, "sales"), 1), rev, share,
                               ("%s–%s" % (_fmt_num(min(ps), 2), _fmt_num(max(ps), 2))) if ps else "未提供",
                               _mean(ps), med, _mean(rs), ful_txt or "未提供"])
    t_topbrand = table("m-topbrand", "Top-brand 对比(变体/新老/销量/销额/价格/评分/履约)",
                       ["品牌", "变体数", "新品数", "老品数", "月销量", "月销额", "销额占比",
                        "价格区间(USD)", "均价(USD)", "中位价(USD)", "有效评分均值", "履约构成"],
                       top_brand_rows, source_label="步骤18 · 品牌聚合", source_ref=ref)
    t_hhi = table("m-concentration", "品牌集中度(CR1/CR3/CR5/HHI)",
                  ["指标", "值", "口径"],
                  [["CR1", cr[1], "Top1 品牌销额占比"],
                   ["CR3", cr[3], "Top3 品牌销额占比合计"],
                   ["CR5", cr[5], "Top5 品牌销额占比合计"],
                   ["HHI", hhi, "Σ(品牌销额占比²) × 10,000(skill §1)"],
                   ["品牌数", len(brands), "有效变体去重品牌"]],
                  source_label="步骤18 · 品牌销额占比", source_ref=ref)
    ful_rows = []
    for name in sorted({_s(r.get("fulfilment")) for r in valid if _s(r.get("fulfilment"))}):
        grp = [r for r in valid if _s(r.get("fulfilment")) == name]
        rev = _sum(grp, "revenue")
        ful_rows.append([name, len(grp), _share(len(grp), len(valid)), round(rev, 2),
                         _share(rev, total_rev)])
    if not ful_rows:
        ful_rows.append(["未提供", None, None, None, None])
    t_ful = table("m-fulfil", "履约方式",               # ← 表名必须恰好是 `履约方式`(构成图触发)
                  ["履约方式", "变体数", "数量占比", "月销额", "销额占比"], ful_rows,
                  source_label="步骤18 · 配送方式(分母=全部有效变体月销额)", source_ref=ref)

    # ---------------------------------------------------------------- ⑥ 产品类型与属性结构
    def _cat_table(tid, col, key, title=None):
        vals = {}
        for r in valid:
            v = _col_value(r, col, extra)
            if v and v not in VAGUE:
                vals[v] = vals.get(v, 0) + 1
        if not vals:
            return table(tid, title or col, [col, "变体数", "数量占比", "月销额", "销额占比"],
                         [[("未提供"), None, None, None, None]],
                         source_label="步骤18 · %s(源表整体缺失)" % col, source_ref=ref)
        rows = []
        for name in sorted(vals, key=lambda x: -vals[x]):
            grp = [r for r in valid if _col_value(r, col, extra) == name]
            rev = _sum(grp, "revenue")
            rows.append([name, len(grp), _share(len(grp), len(valid)),
                         round(_sum(grp, "sales"), 1), round(rev, 2), _share(rev, total_rev)])
        return table(tid, title or col, [col, "变体数", "数量占比", "月销量", "月销额", "销额占比"],
                     rows[:12], source_label="步骤18 · %s" % col, source_ref=ref)

    t_type = _cat_table("m-type", "产品类型", "product_type", title="产品类型")
    mat_col = _first_col(headers, *MATERIAL_COLS)
    t_mat = (_cat_table("m-material", mat_col, "material", title=mat_col) if mat_col
             else table("m-material", "主体材质", ["主体材质", "变体数", "数量占比", "月销量", "月销额",
                                              "销额占比"],
                        [["未提供", None, None, None, None, None]],
                        source_label="步骤18 · 源表无材质列", source_ref=ref))
    dist_rows = _feature_dist_rows(getattr(src, "product_path", None), headers)
    t_feature = (table("m-feature-dist", "是否* 列取值分布(取值直出, 不做是/否判定)",
                       ["属性", "取值", "变体数", "占该列明确取值"], dist_rows[:24],
                       source_label="步骤18 · 是否* 列", source_ref=ref) if dist_rows else None)

    # ---------------------------------------------------------------- ⑦ 功能溢价分析
    prem_rows = []
    for col in feat_cols:
        st = _dominant_stats(valid, col, extra)
        if not st or st["new"]["rate"] is None:
            continue
        dom = st["dominant"]
        grp_dom = [r for r in valid if _col_value(r, col, extra) == dom and r.get("price")]
        grp_oth = [r for r in valid if _col_value(r, col, extra) and _col_value(r, col, extra) not in VAGUE
                   and _col_value(r, col, extra) != dom and r.get("price")]
        if len(grp_dom) < 5 or len(grp_oth) < 5:
            continue
        a, b_ = _mean([r["price"] for r in grp_dom]), _mean([r["price"] for r in grp_oth])
        prem_rows.append([col, dom, len(grp_dom), round(a, 2), len(grp_oth), round(b_, 2),
                          round(a - b_, 2)])
    prem_rows.sort(key=lambda r: -abs(r[6]))
    t_prem = table("m-premium", "功能",           # ← 表名必须恰好是 `功能`(正负价差图触发)
                   ["功能", "对比取值", "该取值变体数", "该取值均价(USD)",
                    "其余取值变体数", "其余取值均价(USD)", "均价差(USD)"],
                   prem_rows[:18], source_label="步骤18 · 是否* 列 × 价格", source_ref=ref)

    # ---------------------------------------------------------------- ⑨ Top20 产品分析
    top20 = sorted(valid, key=lambda r: -(r.get("revenue") or 0))[:20]
    t_top = table("m-top20", "Top20 产品(按变体月销额)",
                  ["排名", "变体ASIN", "标题", "品牌", "价格($)", "评分", "评分数", "月销量", "月销额",
                   "价格段", "新老品", "商品链接"],
                  [[i + 1, _s(r.get("asin")), _s(r.get("title"), 60), _s(r.get("brand")),
                    r.get("price"), r.get("rating"), _i(r.get("reviews")), r.get("sales"),
                    r.get("revenue"), _band_of(bands, r.get("price")), _s(r.get("lifecycle")),
                    _s(r.get("url"))] for i, r in enumerate(top20)],
                  source_label="步骤18 属性标注-标准化交付", source_ref=ref)

    # ---- 全量变体矩阵(合同 §2「Provide the full all-product matrix too」; 不只 Top20)
    _matc = _first_col(list(src.header or []), *MATERIAL_COLS)
    allvar_rows = []
    for r in sorted(valid, key=lambda x: -(x.get("revenue") or 0)):
        allvar_rows.append([_s(r.get("asin")), _s(r.get("parent")), _s(r.get("brand")),
                            _s(r.get("title"), 46), _num_of(r.get("price")), _num_of(r.get("sales")),
                            _num_of(r.get("revenue")), _num_of(r.get("rating")),
                            _i(r.get("reviews")), _band_of(bands, r.get("price")),
                            _s(r.get("lifecycle")),
                            _col_value(r, "履约方式", extra) or _s(r.get("fulfilment")) or "未提供",
                            _col_value(r, "产品类型", extra) or "未提供",
                            (_col_value(r, _matc, extra) if _matc else None) or "未提供",
                            _s(r.get("url"))])
    t_allvar = table("m-allvar", "全量变体矩阵(全部 %d 个有效变体, 非仅 Top20)" % len(valid),
                     ["变体ASIN", "父ASIN", "品牌", "标题", "价格($)", "月销量", "月销额", "评分",
                      "评分数", "价格段", "新老品", "履约方式", "产品类型", "主体材质", "商品链接"],
                     allvar_rows, source_label="步骤18 属性标注-标准化交付(全量有效变体)",
                     source_ref=ref)

    # ---- Top5 品牌: 完整品牌指标 + **该品牌全部源变体**(合同 §2 L44 / QA L10)
    _brev = {}
    for r in valid:
        _b = _s(r.get("brand")) or "未提供"
        _brev[_b] = _brev.get(_b, 0.0) + (_num_of(r.get("revenue")) or 0.0)
    _top_brands = [b for b, _ in sorted(_brev.items(), key=lambda kv: -kv[1])[:5]]
    _tot_rev = sum(_brev.values()) or 0.0
    bkpi_rows, bvar_rows = [], []
    for b in _top_brands:
        grp = [r for r in valid if (_s(r.get("brand")) or "未提供") == b]
        prices = [p for p in (_num_of(r.get("price")) for r in grp) if p is not None]
        ratings = [x for x in (_num_of(r.get("rating")) for r in grp) if x is not None and x > 0]
        rev = sum((_num_of(r.get("revenue")) or 0.0) for r in grp)
        new_n = sum(1 for r in grp if _s(r.get("lifecycle")) == "新品")
        ful = {}
        for r in grp:
            k = _col_value(r, "履约方式", extra) or _s(r.get("fulfilment")) or "未提供"
            ful[k] = ful.get(k, 0.0) + (_num_of(r.get("revenue")) or 0.0)
        bkpi_rows.append([b, len(grp), "%d 新 / %d 老" % (new_n, len(grp) - new_n),
                          _fmt_num(_sum(grp, "sales"), 0), _fmt_num(rev, 2),
                          ("%.1f%%" % (rev / _tot_rev * 100)) if _tot_rev else "未提供",
                          ("$%s–%s" % (_fmt_num(min(prices), 2), _fmt_num(max(prices), 2))) if prices else "未提供",
                          _fmt_num(_median(prices), 2),
                          _fmt_num((sum(prices) / len(prices)) if prices else None, 2),
                          _fmt_num(_median(ratings), 2) if ratings else "未提供(无有效评分; 0 分不计入)",
                          _fmt_num(sum(_i(r.get("reviews")) or 0 for r in grp), 0),
                          ("; ".join("%s %.1f%%" % (k, v / rev * 100)
                                     for k, v in sorted(ful.items(), key=lambda kv: -kv[1])[:3])
                           if rev else "未提供")])
        for r in sorted(grp, key=lambda x: -(x.get("revenue") or 0)):
            bvar_rows.append([b, _s(r.get("asin")), _s(r.get("title"), 44), _num_of(r.get("price")),
                              _num_of(r.get("sales")), _num_of(r.get("revenue")), _num_of(r.get("rating")),
                              _i(r.get("reviews")), _s(r.get("lifecycle")),
                              _col_value(r, "履约方式", extra) or _s(r.get("fulfilment")) or "未提供",
                              _col_value(r, "产品类型", extra) or "未提供", _s(r.get("url"))])
    t_bkpi = table("m-brand-kpi", "Top5 品牌完整指标(变体/生命周期/价格/评分/履约)",
                   ["品牌", "变体数", "生命周期构成", "月销量", "月销额(USD)", "销额占比",
                    "价格区间(USD)", "价格中位(USD)", "价格均值(USD)", "有效评分中位(除 0)",
                    "评分数合计", "履约销额构成"],
                   bkpi_rows, source_label="步骤18 品牌聚合(全部源变体, 未抽样)", source_ref=ref)
    t_bvar = table("m-brandvar", "Top5 品牌的全部源变体明细(%d 行)" % len(bvar_rows),
                   ["品牌", "变体ASIN", "标题", "价格($)", "月销量", "月销额", "评分", "评分数",
                    "新老品", "履约方式", "产品类型", "商品链接"],
                   bvar_rows, source_label="步骤18 属性标注-标准化交付(逐变体, 无代表替代)",
                   source_ref=ref)
    _bchk = sum(len([r for r in valid if (_s(r.get("brand")) or "未提供") == b]) for b in _top_brands)
    t_brecon = table("m-brand-recon", "品牌矩阵对账(与源表)", ["项目", "值", "口径"],
                     [["Top5 品牌变体行数", "%d 行" % _bchk, "等于源表中这 5 个品牌的全部有效变体数"],
                      ["Top5 品牌销额合计", _fmt_num(sum(_brev[b] for b in _top_brands), 2),
                       "占全量销额 %s" % (("%.1f%%" % (sum(_brev[b] for b in _top_brands) / _tot_rev * 100))
                                       if _tot_rev else "未提供")],
                      ["与全量矩阵一致性", "行数 %d / 全量 %d" % (_bchk, len(valid)),
                       "品牌明细与全量矩阵同源同口径(不重复计父体)"],
                      ["代表变体", "仅作补充, **不替代**品牌全量明细", "合同 §2 L44 / QA L10"]],
                     source_label="对账口径", source_ref=ref)

    # ---------------------------------------------------------------- ⑧ 价格段 × 属性/功能差异分析
    def _band_rev(band) -> float:
        return _sum([r for r in valid if _in_band(band, r.get("price"))], "revenue")

    def _dom_share_in_band(band, col) -> tuple:
        """段内某属性列的主导取值 + **段内销额占比**(spec §4: 属性占比分母 = 段内变体月销额)。"""
        grp = [r for r in valid if _in_band(band, r.get("price"))]
        tot = _sum(grp, "revenue")
        cnt: dict = {}
        for r in grp:
            v = _col_value(r, col, extra)
            if v and v not in VAGUE:
                cnt[v] = cnt.get(v, 0) + (_i(r.get("revenue")) or 0)
        if not cnt or not tot:
            return None, None
        k = max(cnt, key=lambda x: cnt[x])
        return k, cnt[k] / tot

    mix_rows = []
    for b in bands:
        grp = [r for r in valid if _in_band(b, r.get("price"))]
        ps = [r["price"] for r in grp if r.get("price") is not None]
        rev = _sum(grp, "revenue")
        parts = [b.get("label") or "全部", len(grp),
                 ("$%s–%s" % (_fmt_num(min(ps), 2), _fmt_num(max(ps), 2))) if ps else "未提供",
                 # 段内价格分布(2026-10-10, 定价模块 §2 要求"comparable range, median and P25–P75")
                 _fmt_num(_quantile(ps, 0.25), 2), _fmt_num(_median(ps), 2), _fmt_num(_quantile(ps, 0.75), 2),
                 _share(len(grp), len(valid)), _share(_sum(grp, "sales"), total_sales),
                 _share(rev, total_rev)]
        for col in (("产品类型",) + (mat_col,) if mat_col else ("产品类型",)):
            k, sh = _dom_share_in_band(b, col)
            parts.append(("%s %.1f%%" % (k, sh * 100)) if k else "未提供")
        # 段内新品表现 + 段内品牌集中度(spec §6: 价段吸引要用销量/销额+生命周期证据; 竞争余量要用段内品牌集中度)
        new_grp = [r for r in grp if _s(r.get("lifecycle")) == "新品"]
        parts.append(_share(_sum(new_grp, "revenue"), rev))
        bshare = {}
        for r in grp:
            k2 = _s(r.get("brand")) or "未提供"
            bshare[k2] = bshare.get(k2, 0) + (_i(r.get("revenue")) or 0)
        top3 = sorted(bshare.values(), reverse=True)[:3]
        parts.append(round(sum(top3) / rev, 4) if rev else None)
        # 单品尺寸中位(只有单品维度列, 不与包装混用)
        dim_col = _first_col(headers, "商品尺寸", "商品尺寸（单位换算）")
        triples = [_parse_dims(_col_value(r, dim_col, extra)) for r in grp] if dim_col else []
        triples = [t for t in triples if t]
        if triples:
            med = tuple(round(_median([t[i] for t in triples]) or 0, 2) for i in range(3))
            parts.append("%s×%s×%s(n=%d)" % (med[0], med[1], med[2], len(triples)))
        else:
            parts.append("未提供")
        parts.append(_sample_note({"new": {"complete": 1.0 if len(grp) >= max(5, int(0.03 * len(valid)))
                                           else 0.0, "yes": len(grp)},
                                   "old": {"complete": 1.0, "yes": len(grp)}}))
        mix_rows.append(parts)
    mix_hdr = ["价格段", "样本数", "观测价格区间", "段内P25(USD)", "段内中位价(USD)", "段内P75(USD)",
               "数量占比", "销量占比", "销额占比",
               "主导产品类型(段内销额占比)"] + (["主导材质(段内销额占比)"] if mat_col else []) + \
              ["新品销额占比", "Top3品牌销额占比(段内)", "单品尺寸中位(in)", "样本判定"]
    t_mix = table("m-band-mix", "价格段内结构(样本/价格区间/三口径/主导类型与材质/尺寸)",
                  mix_hdr, mix_rows, source_label="步骤18 · 价格段口径同 02 节", source_ref=ref)

    type_vals = sorted({_col_value(r, "产品类型", extra) for r in valid
                        if _col_value(r, "产品类型", extra) not in VAGUE})[:12]
    type_rows = []
    for tv in type_vals:
        row = [tv]
        for b in bands:
            grp = [r for r in valid if _in_band(b, r.get("price"))
                   and _col_value(r, "产品类型", extra) == tv]
            row.append(_share(_sum(grp, "revenue"), _band_rev(b)))
        row.append(_share(_sum([r for r in valid if _col_value(r, "产品类型", extra) == tv], "revenue"),
                          total_rev))
        type_rows.append(row)
    t_band_type = table("m-band-type", "价格段 × 产品类型(值=段内销额占比)",
                        ["产品类型"] + [str(b.get("label")) for b in bands] + ["全类目销额占比"],
                        type_rows, source_label="步骤18 · 段内销额分母", source_ref=ref)

    # 功能热力图: 表头首列必须恰好是 `功能`, 且所在 section 标题含"热力图" → 查看器渲染热力图
    func_vals = []
    for col in feat_cols:
        cnt: dict = {}
        for r in valid:
            v = _col_value(r, col, extra)
            if v and v not in VAGUE:
                cnt[(col, v)] = cnt.get((col, v), 0) + (_i(r.get("revenue")) or 0)
        for k, v in cnt.items():
            func_vals.append((k, v))
    func_vals.sort(key=lambda x: -x[1])
    heat_rows = []
    for (col, val), _rev in func_vals[:14]:
        row = ["%s=%s" % (col.replace("是否", ""), val)]
        for b in bands:
            grp = [r for r in valid if _in_band(b, r.get("price")) and _col_value(r, col, extra) == val]
            row.append(_share(_sum(grp, "revenue"), _band_rev(b)))
        heat_rows.append(row)
    t_heat = (table("m-band-func", "功能 × 价格段(值=段内销额占比; 空白=该段无此配置)",
                    ["功能"] + [str(b.get("label")) for b in bands], heat_rows,
                    source_label="步骤18 · 段内销额分母", source_ref=ref) if heat_rows else None)

    # 价格配置阶梯: 逐档 新增/强化/回落/稳定 + 代表变体 + 证据强度
    def _band_profile(band) -> dict:
        prof = {}
        for col in feat_cols:
            k, sh = _dom_share_in_band(band, col)
            if k:
                prof[col] = (k, sh)
        return prof

    stair_rows, prev, prev_lbl, prev_grp = [], None, None, None
    for b in bands:
        lbl = str(b.get("label"))
        prof = _band_profile(b)
        grp = [r for r in valid if _in_band(b, r.get("price"))]
        news, strong, fall, stable = [], [], [], []
        if prev:
            for col, (k, sh) in prof.items():
                pk, psh = prev.get(col, (None, 0.0))
                # 默认描述规则(market-analysis §staircase): 新增=前档<10% 且 本档≥20%;
                # 强化 ≥+15pp; 回落 ≤-15pp; 其余稳定。**原始差异一律保留在单元格里**。
                if pk is None or (pk != k and psh < 0.10 and sh >= 0.20):
                    news.append("%s=%s(%.0f%%)" % (col.replace("是否", ""), k, sh * 100))
                elif sh - psh >= 0.15:
                    strong.append("%s=%s(%.0f%%→%.0f%%)" % (col.replace("是否", ""), k, psh * 100, sh * 100))
                elif sh - psh <= -0.15:
                    fall.append("%s=%s(%.0f%%→%.0f%%)" % (col.replace("是否", ""), k, psh * 100, sh * 100))
                else:
                    stable.append("%s=%s" % (col.replace("是否", ""), k))
        else:
            stable = ["%s=%s" % (c.replace("是否", ""), v[0]) for c, v in list(prof.items())[:4]]
        top_var = max(grp, key=lambda r: (r.get("revenue") or 0)) if grp else None
        need = max(5, int(0.03 * len(valid)))          # 普通档下限 = max(5, 3% 有效变体)
        base_ok = (min(len(grp), len(prev_grp)) >= 5) if prev_grp else (len(grp) >= 5)
        strength = ("证据不足(相邻档样本<5, 原始差异已保留)" if not base_ok
                    else ("证据充分" if len(grp) >= need else "稀疏(样本<下限 %d)" % need))
        stair_rows.append([lbl, ("相较 %s" % prev_lbl) if prev_lbl else "起点档",
                           " / ".join(news[:3]) or "—", " / ".join(strong[:3]) or "—",
                           " / ".join(fall[:3]) or "—", " / ".join(stable[:3]) or "—",
                           ("%s $%s" % (_s((top_var or {}).get("asin")),
                                        _fmt_num((top_var or {}).get("price"), 2))) if top_var else "未提供",
                           strength])
        prev, prev_lbl, prev_grp = prof, lbl, grp
    t_stair = table("m-stair", "价格配置阶梯矩阵(相较前一档: 新增/强化/回落/稳定)",
                    ["价格段", "相较前一档", "新增要素", "强化要素", "回落要素", "稳定(基线)要素",
                     "代表变体", "证据强度"],
                    stair_rows, source_label="步骤18 · 是否* 列 × 价格段", source_ref=ref)

    # ---------------------------------------------------------------- ⑨ Top20 产品级阶梯
    t20_bands = {}
    for r in top20:
        t20_bands[_band_of(bands, r.get("price"))] = t20_bands.get(_band_of(bands, r.get("price")), 0) + 1
    t20_rows = []
    seen_cfg: set = set()
    for b in bands:
        lbl = str(b.get("label"))
        grp = [r for r in valid if _in_band(b, r.get("price"))]
        g20 = [r for r in grp if r in top20]
        cfg = []
        for col in feat_cols:
            k, sh = _dom_share_in_band(b, col)
            if k and (col, k) not in seen_cfg and sh >= 0.3:
                cfg.append("%s=%s" % (col.replace("是否", ""), k))
                seen_cfg.add((col, k))
        rep = max(g20, key=lambda r: (r.get("revenue") or 0)) if g20 else None
        t20_rows.append([lbl, len(grp), len(g20),
                         round(_share(_sum(g20, "revenue"), total_rev) * 100, 2) if total_rev else None,
                         ("%s $%s / 月销额 %s" % (_s((rep or {}).get("asin")),
                                                  _fmt_num((rep or {}).get("price"), 2),
                                                  _fmt_num((rep or {}).get("revenue"), 0))) if rep else "未提供",
                         " / ".join(cfg[:3]) or "—"])
    t_t20_stair = table("m-top20-stair", "Top20 产品级阶梯(价格段 × Top20 数量/销额/代表/本段首现配置)",
                        ["价格段", "全市场变体数", "Top20 数量", "Top20 销额占比(%)",
                         "Top20 段内代表", "本段首现配置(段内销额占比≥30%)"],
                        t20_rows, source_label="步骤18 · Top20 按变体月销额", source_ref=ref)

    def _profile_shares(rows: list[dict], col: str) -> dict:
        tot = _sum(rows, "revenue")
        cnt: dict = {}
        for r in rows:
            v = _col_value(r, col, extra)
            if v and v not in VAGUE:
                cnt[v] = cnt.get(v, 0) + (_i(r.get("revenue")) or 0)
        return {k: (v / tot if tot else 0.0) for k, v in cnt.items()}

    vs_rows = []
    for col in ("产品类型",) + tuple(feat_cols[:4]):
        a, bsh = _profile_shares(top20, col), _profile_shares(valid, col)
        keys = sorted(set(a) | set(bsh), key=lambda k: -(a.get(k, 0)))[:3]
        for k in keys:
            av, bv = a.get(k, 0.0) * 100, bsh.get(k, 0.0) * 100
            vs_rows.append([col, k, round(av, 2), round(bv, 2), round(av - bv, 2)])
    t_top20_vs = table("m-top20-vs", "Top20 配置画像 vs 全市场(值=销额占比, 差=百分点)",
                       ["维度", "取值", "Top20 占比(%)", "全市场占比(%)", "差异(pp)"],
                       vs_rows, source_label="步骤18 · Top20 vs 全部有效变体", source_ref=ref)

    # ---------------------------------------------------------------- ⑩ Top5 品牌组合对标
    voc_brand = _voc_brand_map(voc_path)
    top5_rows = []
    for b, bn, brev, bshare, bmed in brand_rows[:5]:
        grp = [r for r in valid if _s(r.get("brand")) == b]
        # 价格梯队: 该品牌在各价格段的销额占比(取前 2 段)
        ladder = []
        for bd in bands:
            sub = [r for r in grp if _in_band(bd, r.get("price"))]
            srev = _sum(sub, "revenue")
            if sub and srev:
                ladder.append((srev / (brev or 1), bd.get("label"), len(sub)))
        ladder.sort(reverse=True)
        ladder_txt = " / ".join("%s %.1f%%(n=%d)" % (lb, sh * 100, n) for sh, lb, n in ladder[:2]) \
            or "未提供"
        # 功能覆盖: 该品牌在这两列上的主配置 + 占比
        cover = []
        for col in feat_cols[:2]:
            cnt = {}
            for r in grp:
                v = _col_value(r, col, extra)
                if v and v not in VAGUE:
                    cnt[v] = cnt.get(v, 0) + 1
            if cnt:
                k = max(cnt, key=lambda x: cnt[x])
                cover.append("%s=%s(%d/%d)" % (col, k, cnt[k], sum(cnt.values())))
        top_var = max(grp, key=lambda r: (r.get("revenue") or 0)) if grp else None
        rep_txt = ("%s $%s / 月销额 %s" % (_s((top_var or {}).get("asin")),
                                          _fmt_num((top_var or {}).get("price"), 2),
                                          _fmt_num((top_var or {}).get("revenue"), 0))
                   if top_var else "未提供")
        seg_txt = ("%s 占该品牌销额 %.1f%%(n=%d)" % (ladder[0][1], ladder[0][0] * 100, ladder[0][2])
                   if ladder else "未提供")
        vb = voc_brand.get(b)
        if vb:
            voc_txt = "直配: 评论 %s / 1–3★ %s / 负向占比 %s / 主要问题 %s" % (
                _fmt_num(vb.get("评论数")), _fmt_num(vb.get("1–3★数")),
                ("%.1f%%" % (float(vb.get("负向占比")) * 100)) if vb.get("负向占比") not in (None, "") else "未提供",
                _s(vb.get("主要问题")) or "未提供")
        else:
            voc_txt = "类目级VOC，非品牌归因(该品牌无可直接匹配的 ASIN 级证据)"
        top5_rows.append([b, bshare, ladder_txt, " / ".join(cover) or "未提供", rep_txt, seg_txt, voc_txt])
    t_top5 = (table("m-top5", "Top5 品牌组合对标(价格梯队 / 功能覆盖 / 代表变体 / 目标段 / 直配 VOC)",
                    ["品牌", "销额占比", "价格梯队(段内销额占比)", "功能覆盖(主配置)",
                     "代表变体(月销额最高)", "目标段证据", "直配 VOC 证据"],
                    top5_rows, source_label="步骤18 品牌聚合 + 步骤19 品牌_ASIN对比",
                    source_ref=ref) if top5_rows else None)

    # ---------------------------------------------------------------- ⑪ 市场总结与洞察

    ins = []
    n = 0

    def add(text: str, sref: str = ref):
        nonlocal n
        n += 1
        ins.append({"number": str(n), "text": text, "sourceRef": sref})

    if life.get("新品", {}).get("n"):
        add("新品结构: 新品 %d 个变体, 数量占比 %.1f%% / 销量占比 %.1f%% / 销额占比 %.1f%% —— "
            "%s"
            % (life["新品"]["n"],
               (_share(life["新品"]["n"], len(valid)) or 0) * 100,
               (_share(life["新品"]["sale"], total_sales) or 0) * 100,
               (_share(life["新品"]["rev"], total_rev) or 0) * 100,
               "新品的销额占比低于数量占比, 说明新品还没抢到老品的成交位(新品效率待验证)"
               if (_share(life["新品"]["rev"], total_rev) or 0) < (_share(life["新品"]["n"], len(valid)) or 0)
               else "新品销额占比不低于数量占比, 新品正在有效抢量"))
    if brand_rows:
        add("品牌集中度: CR1 %.1f%% / CR3 %.1f%% / CR5 %.1f%%, HHI %.0f(×10,000) —— %s"
            % (cr[1] * 100, cr[3] * 100, cr[5] * 100, hhi,
               "集中度高, 头部品牌定价与配置主导类目, 新品需从细分场景切入"
               if hhi >= 1000 else "集中度不高, 长尾仍有空间, 可用差异化配置争夺价格段"))
    if prem_rows:
        top_prem = prem_rows[0]
        add("功能溢价(关联, 非因果): `%s=%s` 组均价 %.2f USD vs 其余取值 %.2f USD, 差 %+.2f USD —— "
            "同一价格段内该配置的拥有率决定它是『标配』还是『溢价点』(见价格段×功能矩阵)。"
            % (top_prem[0], top_prem[1], top_prem[3], top_prem[5], top_prem[6]))
    if prices:
        add("价格结构: 有效变体 %d 个, 均价 %.2f / 中位价 %.2f USD, 分成 %d 段(%s) —— "
            "选品目标价段应落在**销额占比最高**而非变体数最多的段。"
            % (len(prices), _mean(prices) or 0, _median(prices) or 0, len(bands),
               info.get("rationale") or "样本过少未分段"))
    if band_rows:
        best = max(band_rows, key=lambda r: (r[5] or 0))
        add("价格段销额集中: 销额占比最高的是 `%s`(销额占比 %.1f%%, 变体 %d 个 / 数量占比 %.1f%%) —— "
            "该段是类目成交主战场; 新品若在该段站位不足, 应优先在此段做配置差异化, 而不是全线铺开。"
            % (best[0], (best[5] or 0) * 100, best[1], (best[2] or 0) * 100))
    if ful_rows and ful_rows[0][0] != "未提供":
        f0 = max(ful_rows, key=lambda r: (r[4] or 0))
        add("履约结构: `%s` 占销额 %.1f%%(变体 %s 个) —— 履约方式直接影响成本结构与 Prime 流量权重; "
            "立项前需确认目标价段下该履约方式仍留有毛利空间(skill §4 要求给出 FBA/FBM/AMZ 含义)。"
            % (f0[0], (f0[4] or 0) * 100, f0[1]))
    if empty_attrs:
        add("数据边界(影响结论强度): 源表缺少 %s 列 → 材质/尺寸/套装等结构维度无法比较, "
            "本页对应表写 `未提供`(不补 0); 这些维度补数后才能进入选品判断。"
            % "、".join(empty_attrs))

    if claim_rows or attr_rows:
        pool = (claim_rows or []) + (attr_rows or [])
        up = max(pool, key=lambda r: (r[8] or 0))
        down = min(pool, key=lambda r: (r[8] or 0))
        add("新老品功能/属性迭代(配对对比): 新品相对老品**最强化**的是 `%s=%s`(%+.1f 个百分点, %s); "
            "**最弱化**的是 `%s=%s`(%+.1f 个百分点, %s) —— 说明新品正在把资源押在哪些配置上; "
            "信号强度见 02A 的『样本判定』列, 标『证据覆盖低/明确数少』的行只能当方向。"
            % (up[1], up[0], up[8] or 0, up[9], down[1], down[0], down[8] or 0, down[9]))
    if brand_rows:
        b0 = brand_rows[0]
        top10 = sum((r[3] or 0) for r in brand_rows[:10])
        add("头部品牌结构: Top1 `%s` 销额占比 %.1f%%(变体 %d 个), Top10 合计 %.1f%% —— "
            "%s"
            % (b0[0], (b0[3] or 0) * 100, b0[1], top10 * 100,
               "头部尚未形成垄断, 但 Top10 已握有半数以上销额, 新进者要么打价格段空档、要么打配置差异"
               if top10 >= 0.5 else "Top10 不足半数, 长尾分散, 可用单点差异切入"))
    if top20:
        t20_rev = _sum(top20, "revenue")
        add("Top20 集中度: 20 个变体贡献销额 %.1f%%(合计 %s USD; 全部有效变体 %d 个) —— "
            "类目成交高度依赖少数 ASIN; 选品时应对照 Top20 的价格段与配置组合, 而不是只看类目平均。"
            % (_share(t20_rev, total_rev) * 100 if total_rev else 0, _fmt_num(t20_rev, 0), len(valid)))
    if top5_rows:
        segs = [r[5].split(" 占")[0] for r in top5_rows]
        add("Top5 品牌组合差异: 头部的目标段分别是 %s —— %s"
            % ("、".join(segs),
               "头部集中在同一价格段, 该段正面竞争最激烈, 差异化必须落在配置/VOC 短板上"
               if len(set(segs)) <= 2 else "头部目标段分散, 说明类目存在多个可立足的价格带"))
    if attr_rows and headers:
        types = [c for c in ("产品类型",) if c in headers]
        if types:
            cnt = {}
            for r in valid:
                v = _col_value(r, "产品类型", extra)
                if v and v not in VAGUE:
                    cnt[v] = cnt.get(v, 0) + 1
            if cnt:
                k = max(cnt, key=lambda x: cnt[x])
                add("产品类型结构: `%s` 是主流形态(%d/%d = %.1f%% 的变体) —— "
                    "偏离主流形态(配件/耗材或小众形态)需要额外需求证据, 否则很难在搜索与评价体系里拿到位置。"
                    % (k, cnt[k], sum(cnt.values()), cnt[k] / sum(cnt.values()) * 100))
    if stair_rows:
        ups = [r[2] for r in stair_rows if r[2] != "—"]
        falls = [r[4] for r in stair_rows if r[4] != "—"]
        mono = "随着价格上升, 配置基本是**单向上移**" if ups and not falls else \
               ("配置**并非单调上移**, 存在高价段回落: %s" % "、".join(falls[:2]) if falls
                else "各档配置变化不明显")
        add("价格配置阶梯: %s; 起点档基线为 %s; 逐档『相较前一档』的新增/强化/回落/稳定见 08 节矩阵 —— "
            "新品最稳的打法是在**目标档的基线配置之上**加 1 个该档的强化要素, 而不是堆全档功能。"
            % (mono, (stair_rows[0][5] or "—")[:60]))
    if vs_rows:
        gap = max(vs_rows, key=lambda r: abs(r[4] or 0))
        add("Top20 vs 全市场(关联, 非因果): 差异最大的是 `%s=%s`(Top20 %.1f%% vs 全市场 %.1f%%, 差 %+.1f pp) —— "
            "头部产品在配置画像上与大盘的系统性偏离, 是选品定义时最值得对齐/反其道的一处。"
            % (gap[0], gap[1], gap[2] or 0, gap[3] or 0, gap[4] or 0))
    if mix_rows:
        top_band = max(mix_rows, key=lambda r: (r[5] or 0))
        dom = top_band[6] if len(top_band) > 6 else "未提供"
        add("低→高价段演进: 销额占比最高的 `%s` 段的主导形态是 %s; 结构差异按段列出(见 08 节) —— "
            "该段是『要不要进、以什么形态进』的第一判断依据; 偏离该段主导形态需要额外需求证据。"
            % (top_band[0], dom))
    notes = [
        "口径: 一律**变体口径**(`变体月销量`/`变体月销额`); 均价=有效变体价格算术平均;"
        "成交加权价格=∑销额/∑销量(单列, 不等于均价)。",
        "评分=0 的变体 %d 个已从评分均值中排除(不视为差评)。" % len(zero_rating),
        "集中度: CR1=%.4f / CR3=%.4f / CR5=%.4f, HHI=%.1f(分母=全部有效变体月销额)。"
        % (cr[1], cr[3], cr[5], hhi),
        "价格段由 `choose_price_bands()` 动态推导(与 Excel 版整合报告同一组边界): %s"
        % (info.get("rationale") or "样本过少, 未分段"),
        "功能/属性迭代的**信号门槛**(skill §4): 每队列已明确数 ≥5 才出行; 证据完整度 <50% 标『证据覆盖低』, "
        "主配置/具备数 <20 标『明确数少』。",
        "缺失不补 0: 源表没有的字段留空并按 skill 要求写 `未提供`。",
    ]
    if band_note:
        notes.append("⚠ " + band_note)

    secs = [
        section("m-1", "01 报告摘要", [t_sum],
                notes=["本模块只陈述可核对的样本/规模/结构事实; 结论性判断在 11 市场总结与洞察。"]),
        section("m-2", "02 市场大盘与价格段", [t_price, t_price_q],
                notes=[notes[3], notes[0], notes[5]] + ([notes[6]] if band_note else [])),
        section("m-3", "03 新品老品分析", [t_life],
                notes=["三口径(数量/销量/销额)合计应为 100%; 构成图按原报告总量口径绘制。"]),
        section("m-3A", "02A 新品老品功能与属性迭代", [t for t in (t_func, t_claim, t_attr) if t],
                notes=[notes[4],
                       "配对图 = 新品 vs 老品同一属性/功能的率差(哑铃图); 未明确**不视为否**, 不计入分母。",
                       "本类目 `是否*` 取值未登记『是/否』词表时, 只出**主配置口径**(该列取值最多的配置), "
                       "不做是/否臆断; 登记 `tools/cat_report/engine.py::POSITIVE` 后自动切换为覆盖率口径。"]),
        section("m-4", "05 品牌与履约集中度", [t_brand, t_hhi, t_ful, t_topbrand],
                notes=["品牌/履约占比分母 = 全部有效变体月销额(skill §1); 分散图 = 中位价 × 销额占比, 气泡=变体数。",
                       "Top-brand 对比表含变体数/新老拆分/销量/销额/价格区间/均价/中位价/有效评分/履约构成(skill §4)。"]),
        section("m-5", "06 产品类型与属性结构", [t for t in (t_type, t_mat, t_feature) if t],
                notes=["源表缺失的属性列写 `未提供`(不补 0); `是否*` 列只做取值分布, 不做是/否判定。"]),
        section("m-6", "07 功能溢价分析", [t_prem],
                notes=["均价差 = 主配置均价 − 其余取值均价(正值=该配置更贵)。**关联而非因果**: "
                       "配置差异可能与价格段、品牌、套装结构混杂(skill §7 要求写『关联』)。"]),
        section("m-6A", "08 价格段 × 属性/功能差异分析(含功能热力图)", 
                [t for t in (t_mix, t_band_type, t_heat, t_stair) if t],
                notes=["属性/功能占比的**分母 = 该价格段内的变体月销额**(spec §4), 因此行内可横向比较; "
                       "空白 = 该段没有这个配置(不是 0 条评论)。",
                       "价格段边界来自 02 节的分位切分, **不使用功能/属性字段参与切分**(spec §4 明确要求), "
                       "这样矩阵才能检验配置差异而不是把差异切进分段。",
                       "稀疏段保留并标『稀疏』(spec §4: 目标每段 ≥ max(5, 3%×有效变体)); "
                       "尺寸只用**单品尺寸**列, 不与包装尺寸混用; 源表没有的维度写 `未提供`。",
                       "阶梯矩阵的『相较前一档』= 该配置在该段的主导度与上一档比: 新增(<15%→≥30%)/强化(+20pp)/"
                       "回落(-20pp)/稳定; 证据强度按段样本量判定, 稀疏档不得单独解读。"]),
        section("m-7", "09 Top20 产品分析", [t_top, t_top20_vs, t_t20_stair, t_allvar],
                notes=["按变体月销额取前 20; 价格段与 02 节共用同一组边界。",
                       "产品级阶梯: 每个价格段的 Top20 数量/销额占比/段内代表 ASIN/本段**首现配置**; "
                       "『vs 全市场』表给出 Top20 与全部有效变体的配置画像差异(销额占比口径)。"]),
        section("m-7A", "10 Top5品牌组合对标", [t for t in (t_top5, t_bkpi, t_bvar, t_brecon) if t],
                notes=["只在**能直接匹配**品牌/ASIN 证据时给品牌级 VOC(来源: VOC 工作簿 `品牌_ASIN对比`); "
                       "匹配不上的写 `类目级VOC，非品牌归因`, 不把类目结论套到单个品牌头上(spec §4 ⑩ / §5)。",
                       "价格梯队按**该品牌在各价格段的销额占比**给出(与 02 节同一组边界)。"]),
        section("m-8", "11 市场总结与洞察", [], insights=ins,
                notes=["写法遵循 skill §7: 结论 + 价值 + 对比基准 + 含义; 缺字段处就近标注不确定性。"]),
    ]
    return sheet("市场大盘分析", "市场大盘分析",
                 "报告摘要 / 价格段 / 新老品 / 功能属性迭代 / 品牌履约 / 属性结构 / 功能溢价 / Top20 "
                 "(来源: 步骤18 标准化交付)", kpis, secs)


VOC_FUNC_KEYS = ("核心效果", "净化能力", "效果", "速度", "风量", "覆盖", "吸力", "功率", "精准", "精度",
                 "传感", "一致性", "可靠", "耐用", "寿命", "损坏", "故障", "安全", "信任", "保护",
                 "过热", "兼容", "集成", "软件", "APP", "连接", "数据", "wifi", "Wi-Fi", "蓝牙")
VOC_EXP_KEYS = ("安装设置", "操控", "易用", "操作", "清洁", "维护", "收纳", "外观", "尺寸", "贴合",
                "人体工学", "材质", "做工", "噪音", "舒适", "环境", "耗材", "配件", "持有成本",
                "包装", "物流", "开箱", "客服", "退货", "保修", "价格", "价值", "预期", "期望",
                "与描述不符", "场景", "线长", "重量", "气味")


def _num_share(v) -> float | None:
    """把 `占比` 值统一成 0–1 小数(源表可能给 0.35 也可能给 `35%`)。"""
    if v is None or v == "":
        return None
    txt = str(v).strip()
    if txt.endswith("%"):
        try:
            return round(float(txt[:-1]) / 100, 6)
        except ValueError:
            return None
    x = num(txt)
    if x is None:
        return None
    return round(x / 100, 6) if x > 1.0 else round(x, 6)


def _ratio_or_none(ratio, count):
    """源表『问题占比』整列为空时会被读成 0.0 → 计数 > 0 且占比为 0/空时**留空**。

    (2026-10-10 真事故: Dog Dryer 的问题占比列整列为空, 直接透传会渲染成『0.0%』,
     把"没填"读成"是 0"。)
    """
    c = _i(count) or 0
    if c > 0 and (ratio in (None, 0, 0.0)):
        return None
    return ratio


def _int_of(v) -> int | None:
    """宽容取整: 源表可能是 int、`'174'`、甚至 `'1,177'` —— 统一成 int(失败返回 None, 不当 0)。"""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    txt = str(v).strip().replace(",", "")
    try:
        return int(float(txt))
    except ValueError:
        return None


def _num_of(v) -> float | None:
    """宽容取浮点(优先级分等), 失败返回 None。"""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _voc_group(attr: str) -> str:
    """按 **VOC skill `taxonomy-and-scoring.md` §2 的 17 类通用属性**把问题归口到
    `核心功能性差评` / `体验感差评`(spec §5 的两张紧凑表)。

    判据是关键词命中数(功能优先于体验, 因为"性能/可靠性/安全"决定能否完成任务);
    命中数相同或都没命中 → `未分类`(单列披露, **不硬塞**进两张表 —— VOC skill §6 要求保留证据)。
    """
    a = str(attr or "")
    if not a.strip():
        return "未分类"
    f = sum(1 for k in VOC_FUNC_KEYS if k in a)
    e = sum(1 for k in VOC_EXP_KEYS if k in a)
    if f > e:
        return "功能"
    if e > f:
        return "体验"
    return "未分类"


def _voc_table_rows(path: str | None, sheet: str, first_cell: str) -> list[dict]:
    """直读 VOC 工作簿某张表(表头行以 `first_cell` 起头) → [{列名: 值}]。

    为什么不走 `read_voc_workbook`: 它只解析 6 张表(`差评问题分析/好评价值驱动/正负属性对照/
    Listing卖点_风险/总览/抓取审计`), 而 spec §5 还需要 `用户场景分析`、`安全与合规风险`,
    spec §4 还需要 `品牌_ASIN对比`。直读不动 VOC 流水线。
    """
    if not path:
        return []
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:                                  # noqa: BLE001
        return []
    try:
        if sheet not in wb.sheetnames:
            return []
        it = wb[sheet].iter_rows(values_only=True)
        hdr, out = None, []
        for row in it:
            if not first_cell:                              # 自动找表头: 首个"≥3 个非空单元格"的行
                if hdr is None and row and sum(1 for c in row if c not in (None, "")) >= 3:
                    hdr = [str(h).strip() if h is not None else "" for h in row]
                    continue
                if hdr is None or not row or all(c in (None, "") for c in row):
                    continue
                out.append({hdr[i]: row[i] for i in range(min(len(hdr), len(row)))})
                continue
            if row and str(row[0]).strip().startswith(first_cell):
                hdr = [str(h).strip() if h is not None else "" for h in row]
                continue
            if hdr is None or not row or str(row[0] or "").strip() == "":
                continue
            out.append({hdr[i]: row[i] for i in range(min(len(hdr), len(row)))})
        return out
    finally:
        try:
            wb.close()
        except Exception:                              # noqa: BLE001
            pass


def _voc_star_rows(overview: dict) -> list[list]:
    """把 `VOC分析_总览` 的『星级分布』(如 `1★:534、2★:283…`) 解析成 1–5★ 构成表。"""
    raw = ""
    for k, v in (overview or {}).items():
        if str(k).startswith("星级分布"):
            raw = str(v or "")
            break
    out = []
    total = 0
    vals = {}
    for part in re.split(r"[、,;\s]+", raw):
        m = re.match(r"([1-5])★[:：]\s*(\d+)", part)
        if m:
            vals[int(m.group(1))] = int(m.group(2))
            total += int(m.group(2))
    for star in (1, 2, 3, 4, 5):
        n = vals.get(star)
        out.append(["%d★" % star, n, (n / total) if (n is not None and total) else None,
                    "负向口径(1–3★)" if star <= 3 else "正向口径(4–5★)"])
    if total:
        out.append(["1–3★ 合计", vals.get(1, 0) + vals.get(2, 0) + vals.get(3, 0),
                    ((vals.get(1, 0) + vals.get(2, 0) + vals.get(3, 0)) / total), "差评分母"])
    return out


def _voc_evidence(voc_path: str | None, cap: int = 400) -> tuple:
    """**评论证据库(可检索)** —— 从 VOC 工作簿 `评论证据库` 表读入带**全文**的证据行。

    contract §2/§3: 证据视图必须保留 评论 ID / 源表 / **全文**, 且搜索要能命中**评论正文**
    (「零表行但有正文命中」不等于"没有结果")。源口径此前是"不贴摘录" → 2026-10-12 起提供
    **有上限 + 有披露**的可检索证据库; 全量仍在源工作簿, 未收录部分不得宣称已复核。
    选取策略(可复现): ①先取**已被分析引用**的行(问题/属性/场景/证据角色任一非空);
    ②其余按 **负向优先(星级升序) + 正文长度降序** 补足到上限; 超长正文截断到 1200 字符并标注。
    """
    rows = _voc_table_rows(voc_path, "评论证据库", "评论ID") if voc_path else []
    if not rows:
        return (table("v-evi-na", "评论证据库(数据不足)", ["项目", "结论", "原因"],
                      [["评论证据库", "数据不足", "VOC 工作簿缺少 `评论证据库` 表或无法读取"],
                       ["口径", "不做臆断", "证据视图必须给出 评论ID + 全文, 缺失时不编造摘录"]],
                      source_label="VOC 工作簿 · 评论证据库", source_ref="步骤19"),
                None, ["证据库缺失 → 本报告不提供评论正文检索。"])

    def _linked(r) -> bool:
        return any(_s(r.get(k)) for k in ("问题", "属性", "场景", "证据角色", "安全等级"))

    def _star(r):
        v = _num_of(r.get("星级"))
        return v if v is not None else 9

    linked = [r for r in rows if _linked(r)]
    rest = [r for r in rows if not _linked(r)]
    rest.sort(key=lambda r: (_star(r), -len(str(r.get("完整正文") or ""))))
    picked = (linked + rest)[:cap]
    out_rows = []
    for r in picked:
        body = str(r.get("完整正文") or "").strip()
        if len(body) > 1200:
            body = body[:1200] + " …(截断, 全文见源表 `评论证据库`)"
        out_rows.append([
            _s(r.get("评论ID")) or "未提供", _fmt_num(_num_of(r.get("星级")), 0),
            _s(r.get("品牌")) or "未提供", _s(r.get("父ASIN")) or "未提供",
            _s(r.get("变体ASIN")) or "未提供", _s(r.get("评论日期")) or "未提供",
            _s(r.get("属性")) or "未提供",
            " / ".join(x for x in (_s(r.get("问题")), _s(r.get("场景"))) if x) or "未提供",
            _s(r.get("极性")) or "未提供", _s(r.get("证据角色")) or "未提供",
            _s(r.get("安全等级")) or "—", body,
        ])
    from collections import Counter as _C
    star_all = _C(_fmt_num(_num_of(r.get("星级")), 0) for r in rows)
    t = table("v-evi", "评论证据库(可检索: 评论ID / 全文 / 属性·问题·场景链接)",
              ["评论ID", "星级", "品牌", "父ASIN", "变体ASIN", "评论日期", "属性", "问题 / 场景",
               "极性", "证据角色", "安全等级", "正文(原文)"],
              out_rows, source_label="VOC 工作簿 · 评论证据库(reviewId 去重)",
              source_ref="外部VOC报告!评论证据库")
    note = table("v-evi-note", "证据库口径与收录披露", ["项目", "内容", "口径 / 边界"], [
        ["证据库总量", "%s 条(reviewId 去重, 每条唯一评论一行)" % _fmt_num(len(rows), 0),
         "源表 `评论证据库`; 星级构成 %s" % "、".join("%s★ %d" % (k, v) for k, v in sorted(star_all.items()))],
        ["本报告收录", "%d 条(上限 %d)" % (len(out_rows), cap),
         "策略: ①已被分析引用(问题/属性/场景/证据角色非空, 共 %d 条) ②其余按 **负向优先 + 正文长度降序** 补足"
         % len(linked)],
        ["全文与截断", "正文为源表全文; 超 1200 字符截断并标注", "全文与**未收录部分**见源工作簿; "
         "不得据本报告收录范围宣称已复核全部评论"],
        ["检索范围", "页面搜索命中仅限**收录行**; 零表行但有正文命中时页面会单独提示命中条数",
         "contract §3: 评论正文命中 ≠ 表格行命中, 不得显示成『没有结果』"],
        ["归因与计数边界", "评论→变体为源表归因; 星级计数 **≠** 满意度/故障率/转化率",
         "样本为页数上限内合集; Vine/核实购买等元数据以源表为准"],
    ], source_label="contract §2 证据与计算 / §3 检索", source_ref="VOC 工作簿 · 评论证据库")
    notes = ["本页首次提供**可检索的评论正文**(此前源口径为『不贴摘录』); 检索按评论ID/正文/ASIN/品牌/问题均可命中。",
             "收录有上限且已披露; 未收录行请在源工作簿 `评论证据库` 按 reviewId 检索(不得把本报告收录范围当成全量复核)。"]
    return t, note, notes


def _voc_score_audit(voc: dict) -> dict | None:
    """**VOC 分项重算审计**: 用 skill `negative_priority_score` 从 F/S/H/T/C 重算分与优先级, 与源值对账。

    skill 口径: 分量 1–5 分, 权重 .25/.30/.25/.10/.10, ×20 → 0–100; P0≥80 / P1≥65 / P2≥50 / P3;
    安全等级 A/B 触发**优先级下限 P1**; H 与安全等级不一致时函数抛错 → 记为源表口径冲突(不静默改数)。
    """
    rows = voc.get("issues") or []
    if not rows:
        return None
    out, same = [], 0
    for r in rows:
        comp = {k: _num_of(r.get(k)) for k in ("F", "S", "H", "T", "C")}
        grade = str(r.get("安全等级") or "").strip()
        g = next((c for c in "ABCD" if c in grade), None)
        src_score, src_pri = _num_of(r.get("负向优先级分")), _s(r.get("优先级"))
        calc, note = None, ""
        try:
            calc = _rules.negative_priority_score(comp["F"], comp["S"], comp["H"], comp["T"], comp["C"],
                                                 credible_grade=g)
        except ValueError as exc:                       # 分量越界或 H 与安全等级冲突
            note = "重算被拒: %s" % exc
        new_score = (calc or {}).get("score")
        new_pri = (calc or {}).get("priority")
        if new_score is not None and src_score is not None and abs(new_score - src_score) < 0.05 \
                and new_pri == src_pri:
            recon, same = "一致", same + 1
        elif calc is None:
            recon = "无法重算(见说明)"
        else:
            recon = "**不一致**"
        out.append([_s(r.get("问题")) or "未提供",
                    _fmt_num(comp["F"], 0), _fmt_num(comp["S"], 0), _fmt_num(comp["H"], 0),
                    _fmt_num(comp["T"], 0), _fmt_num(comp["C"], 0), grade or "—",
                    _fmt_num(src_score, 1), _fmt_num(new_score, 1), src_pri or "—", new_pri or "—",
                    recon, note])
    t = table("v-score-audit", "负向优先级分重算审计(F/S/H/T/C → 分与优先级)",
              ["问题", "F", "S", "H", "T", "C", "安全等级", "源分", "重算分", "源优先级", "重算优先级",
               "对账", "说明"], out,
              source_label="skill `analysis_rules.negative_priority_score`(分量 1–5 × 权重 ×20)",
              source_ref="VOC 工作簿 · 差评问题分析(源分列并列展示)")
    return {"table": t, "same": same, "total": len(out)}


def _voc_buy_block(voc: dict) -> dict:
    """**购买驱动 vs 体验收益**(新版 SKILL.md §14 要求两者分开)。

    源报告建模的是"**体验后**的收益驱动"(好评价值驱动: 属性/正向数/驱动分/解读),
    并未建模"**明确说出的购买理由**" → 后者如实标 `数据不足`, 并写清需要什么证据才能补。
    """
    drivers = voc.get("drivers") or []
    top = drivers[:3]
    have = "; ".join("%s(正向 %s 条, 驱动分 %s)"
                     % (_s(d.get("属性")), _fmt_num(_num_of(d.get("正向评论数")), 0),
                        _fmt_num(_num_of(d.get("正向驱动分")), 1)) for d in top) or "未提供"
    rows = [
        ["体验收益(源报告有)", have,
         "源表 `好评价值驱动`(购买**之后**的体验收益; 与购买理由不是同一件事)"],
        ["明确购买理由(源报告未建模)", "数据不足",
         "源表未按『购买理由/选购动因』标注; 需要评论正文里的显式表述(如 chose/picked/bought because)或 Listing 转化数据"],
        ["复购与推荐", "数据不足", "『would buy again / 准备再买』是**意向**, 不等于已完成复购; "
         "需 证据角色/Vine/核实购买 等元数据才可判强度"],
        ["计数边界", "星级计数 ≠ 满意度/转化率/故障率",
         "多标签占比不可相加; 分母见各表口径"],
    ]
    return {"table": table("v-buy", "购买驱动 vs 体验收益(分开呈现)",
                           ["项目", "内容", "口径 / 所需证据"], rows,
                           source_label="新版 SKILL.md §14 + new-product-hero-concept §5",
                           source_ref="VOC 工作簿 · 好评价值驱动 / 评论证据库")}


def _voc_sheet(voc: dict, voc_path: str | None = None) -> dict:
    """Sheet 3: VOC 差评分析(对齐 skill `report-spec.md` §5)。

    spec 组成: KPI + 1–3★ 构成 + `差评分析_总览`(Top10 问题 + 图) + `核心功能性差评` +
    `体验感差评` + `用户场景分析` + `VOC总结与洞察`≥6。
    源处理: **不展示** `分析口径`/`数据质量与限制` 模块; **不贴**代表评论摘录; 品牌/ASIN 只在
    能直接匹配时归因, 否则标 `类目级VOC，非品牌归因`。
    """
    if not voc.get("available", True):
        raise ValueError("VOC 工作簿无法解析: %s" % voc.get("reason"))
    # 问题清单**直读源表** `差评问题分析`(16 列全拿: 评论数/问题占比/F-S-H-T-C/优先级/安全等级/根因假设);
    # reader 只保留了其中一部分键(缺 `评论数`), 用它会让总览表出现空列、洞察取错"最高频"行。
    issues = _voc_table_rows(voc.get("path"), "差评问题分析", "问题") or (voc.get("issues") or [])
    drivers = voc.get("drivers") or []
    matrix = voc.get("matrix") or []
    claims = voc.get("claims") or []
    overview = voc.get("overview") or {}
    limits = voc.get("limitations") or []
    ov = {str(k): str(v) for k, v in overview.items()}
    src_line = ov.get("采集来源", "")
    parents = ov.get("父ASIN 数", "")
    uniq = ov.get("去重后唯一评论", "")
    neg = ov.get("1–3★(负向)评论", "")
    pos = ov.get("4–5★(正向)评论", "")
    ref = "差评问题分析!A1:P%d" % (len(issues) + 4)

    # 改进动作/验证方式: 从 `改进与保留路线图` 按对象名匹配(不搬 证据摘录 —— spec §5 禁止贴评论摘录)
    roadmap = _voc_table_rows(voc.get("path"), "改进与保留路线图", "优先级")
    rm = {}
    for r in roadmap:
        obj = _s(r.get("对象"))
        if obj:
            rm[obj] = r
    def _improve(name: str) -> tuple:
        r = rm.get(name)
        if not r:
            for k, v in rm.items():
                if k and (k in name or name in k):
                    r = v
                    break
        if not r:
            return "未提供", "未提供"
        return (_s(r.get("类型")) or "未提供", _s(r.get("验证方式")) or "未提供")

    kpis = [
        kpi("去重后唯一评论", _int_of(uniq), "VOC分析_总览!B7"),
        kpi("父ASIN 数", _int_of(parents), "VOC分析_总览!B6"),
        kpi("1–3★(负向)评论", _int_of(neg), "VOC分析_总览!B8"),
        kpi("4–5★(正向)评论", _int_of(pos), "VOC分析_总览!B9"),
        kpi("问题条目", len(issues), ref), kpi("安全证据条数", _int_of(ov.get("安全证据条数")), "VOC分析_总览!B16"),
        kpi("Top 负向问题", (issues[0].get("问题") if issues else "—"), ref),
    ]

    t_star = table("v-star", "1–5★ 星级构成", ["星级", "评论数", "占比", "口径"],
                   _voc_star_rows(overview), source_label="外部VOC报告 · VOC分析_总览",
                   source_ref="VOC分析_总览!A10:B10")

    def _issue_row(r: dict, with_group: bool = False) -> list:
        typ, how = _improve(_s(r.get("问题")))
        base = [_s(r.get("问题")), _s(r.get("所属属性"))]
        if with_group:
            base.append(_voc_group(_s(r.get("所属属性"))))
        # 源表两列在实测中相等; 某些源表只给 `负向评论数` → 用它兜底, 不显示空列
        n_all = _int_of(r.get("评论数"))
        if n_all is None:
            n_all = _int_of(r.get("负向评论数"))
        return base + [n_all, _int_of(r.get("负向评论数")),
                       _ratio_or_none(r.get("问题占比"), r.get("负向评论数")),
                       _int_of(r.get("F")), _int_of(r.get("S")), _int_of(r.get("H")),
                       _int_of(r.get("T")), _int_of(r.get("C")),
                       _num_of(r.get("负向优先级分")), _s(r.get("优先级")), _s(r.get("安全等级")),
                       _s(r.get("根因假设")) or "未提供", typ, how]

    head = ["问题", "所属属性", "评论数", "负向评论数", "问题占比",
            "F", "S", "H", "T", "C", "负向优先级分", "优先级", "安全等级",
            "根因假设", "改进类型", "验证方式"]
    top10 = sorted(issues, key=lambda r: -(_i(r.get("负向优先级分")) or 0))[:10]
    t_core = table("v-core", "差评分析_总览(Top10 问题, 按负向优先级分)",
                   head, [_issue_row(r) for r in top10],
                   source_label="外部VOC报告 · 差评问题分析", source_ref=ref)

    func = [r for r in issues if _voc_group(_s(r.get("所属属性"))) == "功能"]
    exp = [r for r in issues if _voc_group(_s(r.get("所属属性"))) == "体验"]
    unc = [r for r in issues if _voc_group(_s(r.get("所属属性"))) == "未分类"]
    func.sort(key=lambda r: -(_i(r.get("负向优先级分")) or 0))
    exp.sort(key=lambda r: -(_i(r.get("负向优先级分")) or 0))
    t_func = (table("v-func", "核心功能性差评(工程口径: 可靠性/性能/安全/兼容)",
                    head, [_issue_row(r) for r in func[:15]],
                    source_label="外部VOC报告 · 差评问题分析 · 所属属性归口=核心功能性",
                    source_ref=ref) if func else None)
    t_exp = (table("v-exp", "体验感差评(体验/服务口径: 易用/噪音/清洁/包装/服务/价格)",
                   head, [_issue_row(r) for r in exp[:15]],
                   source_label="外部VOC报告 · 差评问题分析 · 所属属性归口=体验感",
                   source_ref=ref) if exp else None)

    scenes = _voc_table_rows(voc.get("path"), "用户场景_人群", "场景(评论明确写出)")
    scene_rows = [[_s(r.get("场景(评论明确写出)")), _int_of(r.get("评论数")),
                   _num_share(r.get("占比"))] for r in scenes if _s(r.get("场景(评论明确写出)"))]
    scene_rows.sort(key=lambda r: -(r[1] or 0))
    t_scene = table("v-scene", "用户场景分析(只统计评论中明确写出的场景)",
                    ["场景(评论明确写出)", "评论数", "占比"],
                    scene_rows[:12], source_label="外部VOC报告 · 用户场景_人群",
                    source_ref="用户场景_人群!A1:D%d" % (len(scenes) + 4))

    safe = _voc_table_rows(voc.get("path"), "安全与合规风险", "评论ID")
    agg: dict = {}
    for r in safe:
        lvl = _s(r.get("安全等级")) or "未提供"
        mech = _s(r.get("机制/风险")) or "未提供"
        key = (lvl, mech)
        a = agg.setdefault(key, {"n": 0, "attr": set(), "fix": _s(r.get("处理建议"))})
        a["n"] += _int_of(r.get("评论数")) or 1
        if _s(r.get("属性")):
            a["attr"].add(_s(r.get("属性")))
    safety_rows = [[lv, mech, v["n"], "、".join(sorted(v["attr"])) or "未提供", v["fix"] or "未提供"]
                   for (lv, mech), v in sorted(agg.items(), key=lambda x: (x[0][0], -x[1]["n"]))]
    if not safety_rows:
        safety_rows = [["未提供", "本类目 VOC 未产出安全风险条目", None, "未提供", "未提供"]]
    t_safety = table("v-safety", "安全与合规风险(按等级 × 机制聚合)",
                     ["安全等级", "机制/风险", "评论条数", "涉及属性", "处理建议"],
                     safety_rows[:15], source_label="外部VOC报告 · 安全与合规风险",
                     source_ref="安全与合规风险!A1:H%d" % (len(safe) + 4))

    t_driver = table("v-driver", "好评价值驱动(Top 12)", ["属性", "正向评论数", "正向驱动分", "解读"],
                     [[r.get("属性"), r.get("正向评论数"), r.get("正向驱动分"), r.get("解读")]
                      for r in drivers[:12]], source_label="外部VOC报告 · 好评价值驱动",
                     source_ref="好评价值驱动!A1:N%d" % (len(drivers) + 4))
    t_matrix = table("v-matrix", "正负属性对照(象限)",
                     ["属性", "象限", "正向提及率", "负向问题率", "证据合计"],
                     [[r.get("属性"), r.get("象限"), r.get("正向提及率"), r.get("负向问题率"),
                       r.get("证据合计")] for r in matrix[:18]],
                     source_label="外部VOC报告 · 正负属性对照",
                     source_ref="正负属性对照!A1:N%d" % (len(matrix) + 4))
    t_claim = (table("v-claim", "Listing 卖点风险",
                     ["宣称方向", "对应属性", "负向证据条数", "风险等级"],
                     [[r.get("宣称方向"), r.get("对应属性"), r.get("负向证据条数"), r.get("风险等级")]
                      for r in claims], source_label="外部VOC报告 · Listing卖点_风险",
                     source_ref="Listing卖点_风险!A1:D%d" % (len(claims) + 4)) if claims else None)

    # ---- VOC总结与洞察(spec §5 Minimum insights, ≥6 条)
    ins = []
    n = 0

    def add(text: str, sref: str = ref):
        nonlocal n
        n += 1
        ins.append({"number": str(n), "text": text, "sourceRef": sref})

    neg_n = _int_of(neg) or 0
    if issues:
        i0 = max(issues, key=lambda r: -(_int_of(r.get("负向评论数")) or 0))
        add("最高频抱怨: `%s`(负向 %s 条, 占负向评论 %s 的 %.1f%%, 优先级 %s) —— "
            "这是改进投入的第一顺位; 多标签问题占比不可相加(skill §1)。"
            % (_s(i0.get("问题")), _fmt_num(i0.get("负向评论数")),
               _fmt_num(neg_n), ((_i(i0.get("负向评论数")) or 0) / neg_n * 100) if neg_n else 0,
               _s(i0.get("优先级"))))
    rel = [r for r in func if any(k in _s(r.get("所属属性")) for k in ("可靠", "耐用", "寿命", "损坏", "故障"))]
    if rel:
        add("可靠性/机械缺陷: %d 个问题条目、合计负向 %s 条(其中最高优先级 %s) —— "
            "这类问题直接导致退货与差评升级, 应在量产前用**加速寿命/连续运行**验证。"
            % (len(rel), _fmt_num(sum(_int_of(r.get("负向评论数")) or 0 for r in rel)),
               sorted({_s(r.get("优先级")) for r in rel})[0]))
    lvls = {}
    for r in safe:
        lvls[_s(r.get("安全等级")) or "未提供"] = lvls.get(_s(r.get("安全等级")) or "未提供", 0) + 1
    if lvls:
        add("安全风险与严重度: 安全证据 %s 条, 等级分布 %s —— A/B 级(确认事故/直接观察到危险)才可升级为"
            "硬约束; D 级(传闻/第三方警告)不得单独升级(skill §5)。"
            % (_fmt_num(ov.get("安全证据条数")), "、".join("%s:%d" % (k, v) for k, v in sorted(lvls.items()))))

    def _topic(keys, label, advice):
        hit = [r for r in issues if any(k.lower() in _s(r.get("问题")).lower()
                                        or k.lower() in _s(r.get("所属属性")).lower() for k in keys)]
        if hit:
            add("%s: %d 个条目、合计负向 %s 条(最高优先级 %s) —— %s"
                % (label, len(hit), _fmt_num(sum(_int_of(r.get("负向评论数")) or 0 for r in hit)),
                   sorted({_s(r.get("优先级")) for r in hit})[0], advice))
        else:
            add("%s: 本类目样本**未出现**该主题的独立问题条目(不代表不存在, 只说明本批评论没有集中提及) —— "
                "如需结论需扩大样本或补采。" % label)

    _topic(("软件", "APP", "连接", "wifi", "蓝牙"), "APP/Wi-Fi/连接",
           "软件类问题通常以固件/配网引导解决, 验证方式是复现率与配网成功率。")
    _topic(("包装", "物流", "开箱", "破损", "DOA"), "包装/DOA",
           "运输破损属可控项: 用跌落/振动测试 + 包装结构复核验证。")
    _topic(("客服", "退货", "保修", "售后"), "服务/保修",
           "服务类差评影响复购与评分, 验证方式是响应时长与一次性解决率。")
    if scene_rows:
        s0 = scene_rows[0]
        add("场景机会: 评论里最常明确写出的场景是 `%s`(%s 条) —— 产品定义与 Listing 文案应围绕该场景"
            "构建使用画面; 占比只反映**明确写出**的场景, 不做人口/用途推断(skill 证据规则)。"
            % (s0[0], _fmt_num(s0[1])))
    if roadmap:
        r0 = roadmap[0]
        add("产品改进动作(来自 VOC 改进与保留路线图): 优先级 `%s` / 类型 `%s` / 对象 `%s`, "
            "验证方式: %s —— 改进项必须绑定证据条数与验证方式, 不输出无证据支撑的动作。"
            % (_s(r0.get("优先级")), _s(r0.get("类型")), _s(r0.get("对象")),
               _s(r0.get("验证方式")) or "未提供"))
    if unc:
        add("未归口披露: %d 个问题条目(如 `%s`)的属性无法判定功能/体验 → 单列 `未分类`, "
            "**不硬塞**进两张表(VOC skill §6: 保留其他/未分类证据); 占比 %s 条负向评论。"
            % (len(unc), "、".join(_s(r.get("问题")) for r in unc[:3]),
               _fmt_num(sum(_int_of(r.get("负向评论数")) or 0 for r in unc))))

    t_evi, t_evi_note, evi_notes = _voc_evidence(voc_path)
    _audit = _voc_score_audit(voc)
    _buy = _voc_buy_block(voc)
    # 补齐 V3 等价视图(QA L11): 源工作簿里有、此前未渲染的 5 张表
    _v3_specs = [("v-brandraw", "品牌_ASIN对比(源表全量)", "品牌_ASIN对比", "父ASIN", 60),
                 ("v-accessory", "配件耗材与服务", "配件耗材_服务", "", 60),
                 ("v-opportunity", "产品机会矩阵", "产品机会矩阵", "", 60),
                 ("v-roadmap", "改进与保留路线图", "改进与保留路线图", "", 60),
                 ("v-dict", "标签字典与参数", "标签字典_参数", "", 80)]
    _v3_tabs, _v3_notes = [], []
    for tid, title, sheet_name, anchor, cap in _v3_specs:
        rows = _voc_table_rows(voc_path, sheet_name, anchor) if voc_path else []
        if not rows:
            continue
        hdrs = [h for h in rows[0].keys() if str(h).strip()][:14]
        body = [[_s(r.get(h))[:160] for h in hdrs] for r in rows[:cap]]
        _v3_tabs.append(table(tid, "%s(源表直出%s)" % (title, "" if len(rows) <= cap else ", 前 %d 行" % cap),
                              hdrs, body, source_label="外部VOC 工作簿 · %s" % sheet_name,
                              source_ref="VOC报告!%s" % sheet_name))
        _v3_notes.append("%s: 源表 %d 行%s" % (title, len(rows),
                                          "" if len(rows) <= cap else ", 本页展示前 %d 行" % cap))
    secs = [
        section("v-1", "01 差评分析_总览", [t_star, t_core],
                notes=["口径(取自源表): 来源 %s; 父ASIN %s; 去重后唯一评论 %s; 负向(1–3★) %s / 正向(4–5★) %s。"
                       % (src_line or "未提供", _fmt_num(parents), _fmt_num(uniq),
                          _fmt_num(neg), _fmt_num(pos)),
                       "占比分母 = 各自评论集合(负向=1–3★); 多标签问题占比**不可相加**; "
                       "样本为页数上限内评论合集, 不是全部历史评论 → 不得当作转化率/故障率。",
                       "按 spec §5 **不展示** `分析口径`/`数据质量与限制` 模块(原始口径与审计见源表 "
                       "`数据质量_抓取审计`); 代表评论**正文**见 08 节证据库(可检索, 收录上限已披露)。",
                       ("限制: " + "; ".join(str(x)[:80] for x in limits[:2])) if limits else "限制: 见源表。"]),
        section("v-2", "02 核心功能性差评", [t for t in (t_func, _audit["table"] if _audit else None) if t],
                notes=["归口规则: 按 VOC skill `taxonomy-and-scoring.md` §2 的 17 类通用属性, 关键词命中数"
                       "判功能/体验(功能优先); 保留 F/S/H/T/C 与优先级/安全等级/根因假设/改进类型/验证方式。"
                       + ("重算对账: 源分与 skill 公式 %d/%d 条一致%s"
                          % (_audit["same"], _audit["total"],
                             "; 不一致或无法重算的逐行见说明列" if _audit["same"] != _audit["total"] else "")
                          if _audit else "")]),
        section("v-3", "03 体验感差评", [t for t in (t_exp,) if t],
                notes=["体验/服务口径: 易用/清洁/噪音/材质/包装/客服/价格等 —— 影响评分与复购, "
                       "通常不构成硬否决, 但决定 Listing 与售后成本。"]),
        section("v-4", "04 用户场景分析", [t_scene],
                notes=["只统计评论中**明确写出**的场景; 不做人口/用途推断(skill 证据规则)。"
                       "代表评论 ID 与正文可在 08 节证据库按 ID/正文检索。"]),
        section("v-5", "05 安全与合规风险", [t_safety],
                notes=["等级口径: A=确认伤害/起火/漏电; B=直接观察到的危险事件; C=可信危险机制; D=传闻/第三方警告。"
                       "D 级不得单独升级为 P0/P1(skill §5)。"]),
        section("v-6", "06 VOC总结与洞察", [], insights=ins,
                notes=["覆盖 spec §5 的最低洞察集(最高频/可靠性/安全/APP-连接/包装-DOA/服务/场景机会/改进动作); "
                       "本类目未出现的主题**如实写未出现**, 不用其他类目结论填空。"]),
        section("v-7", "07 支撑模块(好评驱动 / 属性象限 / Listing 风险)", 
                [t for t in (t_driver, _buy["table"], t_matrix, t_claim) if t],
                notes=["spec §5 未把这三块列入必需组成, 但它们来自同一份 VOC 工作簿且对 Listing/产品定义有直接用处, "
                       "故保留在本页末作为**支撑模块**(可视需要关闭)。"]),
        section("v-8", "08 评论证据库(可检索)", [t for t in (t_evi, t_evi_note) if t],
                notes=evi_notes),
        section("v-9", "09 V3 等价视图(品牌 / 配件服务 / 机会矩阵 / 路线图 / 字典)",
                _v3_tabs, notes=_v3_notes or ["源工作簿未提供这些视图 → 如实缺省(不做臆断填充)。"]),
    ]
    return sheet("VOC差评分析", "VOC差评分析",
                 "差评总览 / 核心功能性 vs 体验感 / 用户场景 / 安全风险 / 洞察 "
                 "(来源: 外部VOC报告 · V3 14 表)", kpis, secs)


RECO_WEIGHTS = {"关键词需求": 20, "价段吸引": 20, "竞争余量": 15,
                "配置白地": 20, "VOC问题价值": 15, "证据置信": 10}
RECO_GATE = {"关键词需求或VOC问题价值≥60": "一二选一", "价段吸引≥50": True,
             "竞争余量≥40或配置白地≥60": "二选一", "证据置信≥50": True, "总分≥60": True}
CONFIG_VOC_MAP = (
    (("加热", "温控", "过热", "温度"), "安全", 1.0),
    (("梳刷", "美容剪", "导梳"), "核心效果", 0.8),
    (("吸尘", "吹吸"), "核心效果", 0.9),
    (("无线", "线长", "便携", "免手持"), "安装", 0.8),
    (("功率", "风速", "风量", "吸力", "风速档位", "温度档位"), "速度", 1.0),
    (("材质", "外壳", "机身"), "可靠", 0.8),
    (("配件", "收纳"), "耗材", 0.7),
    (("显示屏", "指示"), "软件", 0.7),
)


def _first_num(row: dict, *keys, default=None):
    """按多个候选列名取数值(列名在类目间可能略有差异, 如 `(段)` vs `(段内)`)。"""
    for k in keys:
        v = _num(row.get(k))
        if v is not None:
            return v
    return default


def _tab_index(sheet_obj: dict) -> dict:
    return {t["id"]: t for s in sheet_obj["sections"] for t in s["tables"]}


def _rows_dicts(tab: dict) -> list[dict]:
    return [dict(zip(tab["headers"], r["values"])) for r in tab["rows"]]


def _num(v, default=None):
    return v if isinstance(v, (int, float)) else default


def _voc_attr_link(col_name: str) -> tuple:
    """把某个功能/属性列名映射到 VOC 属性关键词(用于"这个配置要解决哪个 VOC 问题")。

    返回 (VOC属性关键词, 解决相关度)。映射是**显式词表**, 未命中 → ("", 0) 表示不建立关联。
    """
    for keys, attr, rel in CONFIG_VOC_MAP:
        if any(k in col_name for k in keys):
            return attr, rel
    return "", 0.0


def _attr_state(col: str, value):
    """`是否*` 列的**三态**判定 → True(肯定) / False(否定) / None(未知)。

    优先用引擎 `is_positive`(已登记词表的类目/列)。**未登记**的列在实测里是**语义取值**
    (Dog Dryer `是否含美容剪` = `含美容剪` / `不含美容剪` / `未明确`), 这时 `is_positive` 一律返回
    None → 会把"有肯定证据"误判成"未知"(2026-10-10 实测: Hero 差异型因此恒为 0)。
    兜底按**词形**判定(contract §1: yes 只能来自肯定证据, no 只能来自明确否定, 空白/含糊保持未知):
      · 明确否定(`不含美容剪`/`无X`/`非X`/以 不·无·非 开头) → False
      · 含核心词(`含美容剪`)或标准肯定词(是/有/支持/包含/带) → True
      · 其余(未明确/不适用/空) → None
    """
    st = is_positive(col, value)
    if st is not None:
        return st
    v = _s(value)
    if not v or v in VAGUE:
        return None
    core = str(col).replace("是否", "").strip()
    if not core:
        return None
    if ("不" + core) in v or ("无" + core) in v or ("非" + core) in v or v[0] in "不无非":
        return False
    if core in v or v in ("是", "有", "支持", "包含", "带"):
        return True
    return None


def _valid_variants(recs) -> list:
    """**有效变体**口径(市场大盘与新品/Hero 模块共用同一口径, 否则两张表对不上)。"""
    return [r for r in (recs or []) if r.get("revenue") is not None or r.get("sales") is not None]


def _quantile(vals, p: float):
    """线性插值分位数(与 Excel PERCENTILE.INC 同口径); 单值返回自身; 空返回 None。"""
    xs = sorted(float(v) for v in vals if isinstance(v, (int, float)))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * p
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _band_bounds(label: str):
    """从价格段标签(`$90-163` / `$286+`)解析数值边界。"""
    nums = re.findall(r"\d+(?:\.\d+)?", str(label).replace(",", ""))
    if not nums:
        return (None, None)
    lo = float(nums[0])
    hi = float(nums[1]) if len(nums) > 1 else float("inf")
    return (lo, hi)


def _config_pair(config: str):
    """候选配置声明 `A=B`(含美容剪=不含美容剪) → (A, B); 解析不了返回 None。"""
    body = str(config or "").split("(")[0].strip()
    if "=" not in body:
        return None
    left, right = body.split("=", 1)
    return (left.strip(), right.strip())


def _pct(v) -> str:
    """占比显示为百分数: 0–1 小数 → `72.5%`; 已是百分数字符串/数值(>1) → 原样加 %。"""
    x = _num_of(v)
    if x is None:
        return "未提供"
    return "%.1f%%" % (x * 100 if x <= 1.0 else x)


def _first_of(row: dict, *keys, numeric: bool = False):
    for k in keys:
        if k in row and row.get(k) not in (None, ""):
            return _num_of(row.get(k)) if numeric else row.get(k)
    return None


def _asins_top(rows, key_num: str, n: int = 3):
    def _val(r):
        return _first_of(r, key_num, "月销额(USD)", "变体月销额(USD)", numeric=True) or 0
    return [r for r in sorted(rows, key=lambda r: -_val(r))[:n]]


def _hero_section(recs, extra, mix_by_label, voc_sheet: dict, passed: list) -> dict:
    """**新品 → Hero**(contract §2 第 6 块 + new-product-hero-concept.md §2/§6)。

    角色四类(不设固定配额/不造评分): 销售型候选 · 差异型候选 · 相邻任务参考 · 观察/反例。
    销售 / 改动 / 增长 / 体验 四个维度**分开呈现**(不得压成一个置信度)。
    """
    valid = _valid_variants(recs)
    news = [r for r in valid if _s(r.get("lifecycle")) == "新品"]
    vc = _tab_index(voc_sheet)
    n_neg = _fmt_num(_sheet_kpi(voc_sheet, "1–3★(负向)评论"))
    n_pos = _fmt_num(_sheet_kpi(voc_sheet, "4–5★(正向)评论"))
    review_txt = "类目级 VOC(负向 %s / 正向 %s 条); **变体级评论归因: 未提供**" % (n_neg, n_pos)

    cand_cfg = passed[0]["config"] if passed else None
    pair = _config_pair(cand_cfg) if cand_cfg else None
    cand_band = passed[0]["band"] if passed else None
    cand_prev = passed[0].get("prevalence") if passed else None

    def _hit_of(variant):
        """候选配置 `A=B` 的逐变体命中判定 → (True/False/None, 列名)。

        真实列名常带 `是否` 前缀(实测 `是否含美容剪`), 右侧 `不X` 只是否定措辞 → 不当作第二列。
        三态用 engine.is_positive(未知 ≠ 否); 找不到对应列时返回 None(**数据不足**, 不臆断未命中)。
        """
        if not pair:
            return (None, None)
        left = pair[0]
        core = left.replace("是否", "").strip()
        cands = [left, "是否" + left, core]
        col = next((c for c in cands if c and c in (extra or {})), None)
        if col is None and core:
            col = next((c for c in (extra or {}) if core and core in str(c)), None)
        if col is None:
            return (None, left)
        return (_attr_state(col, _col_value(variant, col, extra)) is True, col)

    def _rev(r) -> float:
        return _num_of(r.get("revenue")) or 0.0

    revs = sorted((_rev(r) for r in news), reverse=True)
    med_rev = _median(revs) or 0.0
    top3_asins = {_s(r.get("asin")) for r in _asins_top(news, "revenue", 3)} if news else set()

    rows = []
    for r in sorted(news, key=lambda x: -_rev(x)):
        band = None
        for lb in mix_by_label:
            lo, hi = _band_bounds(lb)
            pr = _num_of(r.get("price"))
            if pr is not None and lo is not None and lo <= pr < (hi if hi != float("inf") else 1e18):
                band = lb
                break
        mrow = mix_by_label.get(band) or {}
        dom_type = str(mrow.get("主导产品类型(段内销额占比)") or "未提供")
        vtype = _s(r.get("product_type")) or _col_value(r, "产品类型", extra) or "未提供"
        same_type = bool(dom_type != "未提供" and vtype != "未提供" and vtype in dom_type)
        hit, hit_col = _hit_of(r)
        if hit is None:
            hit_txt = ("无法判定: %s → 源表无 `%s` 对应列(**数据不足**, 不臆断)" % (cand_cfg, hit_col)
                       if cand_cfg else "无可比配置声明")
        elif hit:
            hit_txt = "命中: %s(列 `%s` 为肯定取值; 段内渗透 %s)" % (
                cand_cfg, hit_col, ("%.1f%%" % (cand_prev * 100)) if cand_prev is not None else "未提供")
        else:
            hit_txt = "未命中: %s(列 `%s` 为**明确否定**, 非肯定值)" % (cand_cfg, hit_col)
        if hit and _rev(r) >= med_rev:
            role = "差异型候选"
            why = "命中候选差异化配置 且 销额 ≥ 新品中位(%s)" % _fmt_num(med_rev, 2)
        elif _s(r.get("asin")) in top3_asins:
            role = "销售型候选"
            why = "新品销额前 3(销额 %s USD; 占新品销额 %s)" % (
                _fmt_num(_rev(r), 2),
                ("%.1f%%" % (_rev(r) / sum(revs) * 100)) if sum(revs) else "未提供")
        elif not same_type and _rev(r) > 0:
            role = "相邻任务参考"
            why = "形态/类型与段内主导不同(段内主导: %s)" % dom_type
        else:
            role = "观察/反例"
            if hit is None:
                why = "销额低于新品中位, 且候选配置无法逐变体判定(限制: 差异证据缺失; 低量**不构成永久否定**)"
            elif not hit:
                why = "销额低于新品中位且未命中候选差异化配置(限制: 销量与差异证据都不足; 低量**不构成永久否定**)"
            else:
                why = "配置命中但销额低于中位(需更多观测)"
        rows.append([_s(r.get("asin")) or "未提供", _s(r.get("brand")) or "未提供",
                     _fmt_num(_num_of(r.get("price")), 2), _fmt_num(_num_of(r.get("sales")), 0),
                     _fmt_num(_rev(r), 2), vtype, dom_type, hit_txt, review_txt, role, why])

    from collections import Counter as _Counter
    cnt = _Counter(r[9] for r in rows)
    t_universe = table("r-hero-universe", "新品变体宇宙(全部源标注新品, 未按销量筛选)",
                       ["变体ASIN", "品牌", "价格(USD)", "月销量", "月销额(USD)", "形态(类型)",
                        "段内主导形态", "候选差异化配置命中", "评审证据", "候选角色", "角色理由 / 决定性限制"],
                       rows, source_label="步骤18 交付表(lifecycle=新品) + VOC 汇总口径",
                       source_ref="与市场大盘 03 节同一有效变体口径")
    t_summary = table("r-hero-summary", "Hero 筛选口径与角色分布",
                      ["项目", "内容", "依据 / 边界"],
                      [["新品宇宙", "全部 %d 个源标注新品变体(**未按销量筛选**)" % len(news),
                        "源表 lifecycle 标签原样保留; 与 listing 日期冲突时标疑, 不静默改标签"],
                       ["角色分布", "销售型 %d / 差异型 %d / 相邻任务 %d / 观察·反例 %d"
                        % (cnt.get("销售型候选", 0), cnt.get("差异型候选", 0),
                           cnt.get("相邻任务参考", 0), cnt.get("观察/反例", 0)),
                        "四类角色来自独立可见证据维度; **无固定配额、无固定评分、无评论数门槛**"],
                       ["三态区分", "首轮 Hero 入围(本表) ≠ 正式 Hero 确认(需重复可比观测) ≠ 我方 Concept 草案(下节)",
                        "new-product-hero-concept §1/§6: 不得因用户要求继续就升级为已确认 Hero"],
                       ["共享父体", "同一父体下各变体分别计销售; 父体总额不重复摊到子体",
                        "共享父体**不是**自动排除理由; 颜色/尺寸/换链接变体不算独立创新"],
                       ["四个维度分开", "销售(销量/销额) · 改动(配置差异) · 增长(逐期字段) · 体验(评论证据)",
                        "不得压成一个置信度; 销量不证明满意, 也不证明某功能导致购买"],
                       ["增长字段", "源报告无逐期增长字段 → **数据不足**, 不编造增长与低基数诊断",
                        "new-product-hero-concept §2: 低基数增长诊断不是历史观测"]],
                      source_label="new-product-hero-concept.md §1/§2/§6",
                      source_ref="本报告 选品建议")
    return section("r-hero", "新品 → Hero(首轮入围, 未确认)", [t_universe, t_summary],
                   notes=["**角色是『入围理由』, 不是开发批准**: 市场 Hero 只是可参照的在售产品, "
                          "不等于我方未来旗舰; 工程/成本/认证均未评估。",
                          "评审证据为**类目级** VOC(变体级归因未提供); 评论稀疏限制体验证据的推广, "
                          "但**不否定**该变体的销售接受度。"])


def _xsource_section(recs, extra, mix_by_label, voc_sheet: dict, passed: list,
                     voc_path: str | None) -> dict:
    """**跨源证据矩阵**(新版 SKILL.md 工作流步骤 3): 把 变体/配置 ↔ 用户任务 ↔ 需求 串起来,
    并明确标出**匹配级证据**(品牌直配)与**类目级证据**(category-only), 避免把类目结论当成该变体证据。
    """
    brand_map = _voc_brand_map(voc_path) if voc_path else {}
    _vc = _tab_index(voc_sheet)
    _scenes = _rows_dicts(_vc["v-scene"]) if "v-scene" in _vc else []
    scene_txt = ("类目级(评论明确写出): %s(正向 %s 条)"
                 % (_s(_scenes[0].get("场景(评论明确写出)")), _fmt_num(_num_of(_scenes[0].get("评论数")), 0))
                 if _scenes else "类目级: 未提供")
    news = [r for r in _valid_variants(recs) if _s(r.get("lifecycle")) == "新品"]
    pool = news or _valid_variants(recs)
    cand_cfg = passed[0]["config"] if passed else None
    pair = _config_pair(cand_cfg) if cand_cfg else None

    def _hit(r):
        if not pair:
            return "未提供"
        core = pair[0].replace("是否", "").strip()
        col = next((c for c in (pair[0], "是否" + pair[0], core) if c in (extra or {})), None)
        if col is None and core:
            col = next((c for c in (extra or {}) if core and core in str(c)), None)
        if col is None:
            return "无法判定(源表无对应列)"
        st = _attr_state(col, _col_value(r, col, extra))
        return "具备" if st is True else ("不具备" if st is False else "未知")

    rows = []
    for r in sorted(pool, key=lambda x: -(_num_of(x.get("revenue")) or 0))[:12]:
        brand = _s(r.get("brand")) or "未提供"
        b = brand_map.get(brand) or {}
        _negk = next((k for k in b if "1–3" in str(k) or "1-3" in str(k) or "负向" in str(k)), None)
        matched = ("品牌直配: 评论 %s / 1–3★ %s" % (_fmt_num(_num_of(b.get("评论数")), 0),
                                                _fmt_num(_num_of(b.get(_negk)) if _negk else None, 0))
                   if b else "无品牌级直配证据")
        rows.append([_s(r.get("asin")) or "未提供", brand, _fmt_num(_num_of(r.get("price")), 2),
                     _fmt_num(_num_of(r.get("revenue")), 2),
                     "%s / %s" % (_s(r.get("product_type")) or _col_value(r, "产品类型", extra) or "未提供",
                                  _hit(r)),
                     scene_txt,
                     matched,
                     "关键词需求: 类目级(见关键词模块)", 
                     "匹配(品牌级)" if b else "**仅类目级**(category-only)"])
    notes = ["`匹配级` = 该 ASIN 所属品牌在 VOC 工作簿里有**直配**汇总(品牌_ASIN对比); "
             "`仅类目级` = 只有类目级结论可引用 —— **不得**把类目级痛点写成该变体的已证事实。",
             "矩阵只列销额前 12 个变体(其余同口径可查); 未列出不等于无证据。"]
    t = table("r-xsrc", "跨源证据矩阵(变体/配置 ↔ 用户任务 ↔ 需求, 标注匹配级)",
              ["变体ASIN", "品牌", "价格(USD)", "月销额(USD)", "形态 / 候选配置命中",
               "用户任务·场景", "品牌直配 VOC(匹配级)", "关键词需求", "证据匹配级别"],
              rows, source_label="步骤18 交付表 + VOC 品牌_ASIN对比 + 步骤8 关键词报告",
              source_ref="workflow §3 cross-source evidence matrix")
    return section("r-xsrc", "跨源证据矩阵", [t], notes=notes)


def _concept_section(passed: list, mix_by_label, voc_sheet: dict, market_sheet: dict) -> dict:
    """**Concept 画像(7 维)**(contract §2「Detailed Concept portrait and why it fits」)。"""
    vc = _tab_index(voc_sheet)
    scenes = _rows_dicts(vc["v-scene"]) if "v-scene" in vc else []
    drivers = _rows_dicts(vc["v-driver"]) if "v-driver" in vc else []
    scene_txt = ("%s(正向 %s 条)" % (scenes[0].get("场景(评论明确写出)"), _fmt_num(scenes[0].get("评论数")))
                 if scenes else "未提供")
    driver_txt = ("%s(正向驱动分 %s)" % (drivers[0].get("属性"), _fmt_num(drivers[0].get("正向驱动分")))
                  if drivers else "未提供")
    DIMS = ("Target", "Product", "Configuration", "Mechanism", "Business fit", "User fit", "Status")
    tabs, notes = [], []
    if not passed:
        tabs.append(table("r-concept-0", "Concept 画像(无可执行机会 → 不编造画像)",
                          ["维度", "内容", "依据 / 来源"],
                          [[d, "未提供", "本报告未形成足够支持的 Concept 草案"] if d != "Status" else
                           [d, "**未形成 Concept 草案**(无候选通过门槛)",
                            "门槛与标签判定见『候选产品方案评分』; 需要新证据才能改变该结论"]
                           for d in DIMS],
                          source_label="选品建议 r-label",
                          source_ref="门槛: 硬否决 → 置信度封顶 → 总分带 → 分项最低"))
        notes.append("无候选通过门槛时**不编造**产品画像与价格建议(contract §2 允许短期清单为空)。")
        return section("r-concept", "Concept 画像(7 维)", tabs, notes=notes)
    for i, c in enumerate(passed, 1):
        mrow = mix_by_label.get(c["band"]) or {}
        iss = c.get("issue") or {}
        prev = c.get("prevalence")
        p25, med, p75 = (_first_of(mrow, "段内P25(USD)", numeric=True),
                         _first_of(mrow, "段内中位价(USD)", numeric=True),
                         _first_of(mrow, "段内P75(USD)", numeric=True))
        rows = [
            ["Target", "目标人群/场景: %s; 核心任务: 该类目主用途(见关键词需求节); "
                       "**排除用例**: 未提供(源数据未覆盖)" % scene_txt, "VOC 用户场景_人群 + 关键词需求节"],
            ["Product", "在售形态: %s(段内主导: %s); 架构/材质/尺寸/使用序列: 未提供"
                        "(源数据只有单品尺寸中位)" % (mrow.get("主导产品类型(段内销额占比)") or "未提供",
                                                mrow.get("主导产品类型(段内销额占比)") or "未提供"),
             "价格段内结构表 + 产品类型属性结构"],
            ["Configuration", "**基线**: 段内主导配置(%s); **差异目标**: `%s`(段内渗透 %s); "
                              "参数参考区间: **未提供**(不发明被测规格)"
             % (mrow.get("主导产品类型(段内销额占比)") or "未提供", c["config"],
                ("%.1f%%" % (prev * 100)) if prev is not None else "未提供"),
             "机会白地矩阵 + 功能渗透热力图"],
            ["Mechanism", "痛点 `%s`(优先级 %s, 负向 %s 条, 安全等级 %s) → 配置 `%s` → "
                          "建议机制: 由该配置直接改善该痛点(机制细节待工程定义); "
                          "**反证/限制**: %s"
             % (iss.get("问题") or "未提供", iss.get("优先级") or "未提供",
                _fmt_num(iss.get("负向评论数")), iss.get("安全等级") or "未提供", c["config"],
                "关联为**弱关联**(挂到的痛点优先级为 %s)" % (iss.get("优先级") or "未提供")),
             "VOC 差评分析 + 功能溢价(横截面关联, 非因果)"],
            ["Business fit", "段销额占比 %s; 段样本 %s 个变体; 段内新品销额占比 %s; "
                             "段内 Top3 品牌占销额 %s; 入选理由: 机会分 %s(排名 %s)"
             % (_pct(_num_of(mrow.get("销额占比"))), _fmt_num(_num_of(mrow.get("样本数")), 0),
                _pct(_num_of(mrow.get("新品销额占比"))),
                _pct(_first_of(mrow, "Top3品牌销额占比(段内)", "段内 Top3 品牌占比(%)", numeric=True)),
                c["total"], c.get("rank")),
             "价格段内结构 + 候选评分(100 分制)"],
            ["User fit", "明确收益线索: 好评驱动 Top `%s`; 购买理由/异议与**变体级归因**: 未提供"
             % driver_txt, "VOC 好评价值驱动 + 用户场景(星级计数不能替代收益计数)"],
            ["Status", "**我方 Concept 草案(未验证)**; 市场 Hero 参照: 见上节首轮入围; "
                       "已确立: 段需求与配置白地; 暂定: 机制与参数; 延后: 成本/工程/认证; "
                       "改变选择所需证据: 该配置的变体级评论与可比新品在售价"
             + ("; 价格窗口 P25–P75 = %s–%s" % (_fmt_num(p25, 2), _fmt_num(p75, 2))
                if p25 is not None and p75 is not None else ""),
             "contract §2: 市场 Concept 状态**不要求**成本/认证已知, 也**不通过**这些检验"],
        ]
        tid = "r-concept-%d" % i
        tabs.append(table(tid, "Concept 画像(7 维) · 候选 %d: %s 段 + %s" % (i, c["band"], c["config"]),
                          ["维度", "内容", "依据 / 来源"], rows,
                          source_label="选品建议 + 市场大盘 + VOC(同源)",
                          source_ref="contract §2 Concept portrait"))
        # 材料风险**留在受影响方向旁**(新版 SKILL.md: 默认不要独立的风险模块)
        risk_rows = []
        for r in (vc.get("v-core") or {}).get("rows", [])[:4]:
            d = dict(zip(vc["v-core"]["headers"], r["values"]))
            if _s(d.get("优先级")) in ("P0", "P1"):
                risk_rows.append([_s(d.get("问题")), _s(d.get("优先级")) or "—",
                                  _fmt_num(_num_of(d.get("负向评论数")), 0),
                                  _s(d.get("安全等级")) or "—",
                                  "影响该方向的可靠性与口碑; 需在样机/首批验证" ])
        if risk_rows:
            tabs.append(table("r-crisk-%d" % i, "该方向的材料风险(源表 P0/P1 问题)",
                              ["材料风险", "优先级", "负向评论数", "安全等级", "对该方向的影响"],
                              risk_rows, source_label="VOC 差评分析(源口径)",
                              source_ref="与 Concept 同方向并列, 不另立风险模块"))
        notes.append("候选 %d 的画像**只覆盖有证据的维度**: 架构/材质/使用序列、被测参数、成本与认证一律 `未提供`, "
                     "不得据此宣称『已验证的更好产品』。" % i)
    return section("r-concept", "Concept 画像(7 维)", tabs, notes=notes)


def _price_section(passed: list, mix_by_label, market_sheet: dict, recs) -> dict:
    """**详细定价建议(5 点)**(contract §2「Detailed pricing recommendation」)。"""
    mk = _tab_index(market_sheet)
    top20 = _rows_dicts(mk.get("m-top20") or {"rows": [], "headers": []})
    prem_tabs = [mk[k] for k in mk if "prem" in k.lower()]
    tabs, notes = [], []
    if not passed:
        tabs.append(table("r-price-0", "详细定价建议(无可执行机会 → 不编造价格)",
                          ["要点", "内容", "依据 / 来源"],
                          [["1 现价锚", "未提供", "无过门槛候选"], ["2 可比区间", "未提供", "同上"],
                           ["3 建议价", "未提供", "同上"], ["4 价值论证", "未提供", "同上"],
                           ["5 改价条件", "同上", "市场定价建议不是 WTP 最优/利润预测"]],
                          source_label="选品建议 r-label", source_ref="contract §2 pricing"))
        notes.append("无候选通过门槛 → 不给出价格建议(不制造虚假精度)。")
        return section("r-price", "详细定价建议(5 点)", tabs, notes=notes)
    for i, c in enumerate(passed, 1):
        mrow = mix_by_label.get(c["band"]) or {}
        lo, hi = _band_bounds(c["band"])
        p25 = _first_of(mrow, "段内P25(USD)", numeric=True)
        med = _first_of(mrow, "段内中位价(USD)", numeric=True)
        p75 = _first_of(mrow, "段内P75(USD)", numeric=True)
        # 实测 m-top20 自带 `价格段` 列与 `价格($)`/`月销额`/`变体ASIN` → 直接按段取在位者(不再用价格区间猜)
        in_band = [r for r in top20 if _s(r.get("价格段")) == _s(c["band"])]
        anchors = _asins_top(in_band, "月销额", 3)
        anchor_txt = "; ".join(
            "%s/%s $%s(月销额 %s)" % (_s(a.get("变体ASIN")) or "未提供", _s(a.get("品牌")) or "未提供",
                                    _fmt_num(_num_of(a.get("价格($)")), 2),
                                    _fmt_num(_num_of(a.get("月销额")), 2))
            for a in anchors) or "未提供(该段无 Top20 在位者样本)"
        new_in_band = [r for r in _valid_variants(recs)
                       if (lambda p: p is not None and lo is not None and lo <= p < (hi if hi != float("inf") else 1e18))(_num_of(r.get("price")))
                       and _s(r.get("lifecycle")) == "新品"]
        new_txt = "; ".join("%s/%s $%s(月销额 %s)" % (_s(a.get("asin")) or "未提供", _s(a.get("brand")) or "未提供",
                                                   _fmt_num(_num_of(a.get("price")), 2), _fmt_num(_num_of(a.get("revenue")), 2))
                            for a in _asins_top(new_in_band, "revenue", 2)) or "未提供(该段无源标注新品)"
        prem_txt = "未提供(该配置无横截面价差记录)"
        for pt in prem_tabs:
            for r in _rows_dicts(pt):
                if str(c["config"]).split("=")[0] in str(r.get("功能") or r.get("属性") or ""):
                    prem_txt = ("参考幅度: 均价差 %s(横截面关联, **非因果、非 BOM**)"
                                % _fmt_num(_first_of(r, "均价差(USD)", "均价差", numeric=True), 2))
                    break
        gap = (p75 / med - 1.0) if (p75 and med) else None
        rec_txt = ("以段内**中位价** $%s 为基准, 试算区间 $%s–$%s(相对中位 %s); 差异化配置参考: %s"
                   % (_fmt_num(med, 2), _fmt_num(med, 2), _fmt_num(p75, 2),
                      ("+%.1f%%" % (gap * 100)) if gap is not None else "未提供", prem_txt)
                   if med is not None else "未提供(该段有效价格样本不足)")
        rows = [
            ["1 现价锚(在位者 + 已接受的新进入者)",
             "在位者 Top3(段内): %s; 新进入者: %s" % (anchor_txt, new_txt),
             "步骤18 交付表价格 + Top20(来源快照); 优惠券/税费/当前渠道价**未提供**, 不与历史价混用"],
            ["2 可比区间(中位与 P25–P75)",
             "段样本 %s 个; 观测价格区间 %s; P25 $%s / 中位 $%s / P75 $%s"
             % (_fmt_num(_num_of(mrow.get("样本数")), 0), mrow.get("观测价格区间") or "未提供",
                _fmt_num(p25, 2), _fmt_num(med, 2), _fmt_num(p75, 2)),
             "同段内有效价格样本(形态可比性: 段内主导 %s)" % (mrow.get("主导产品类型(段内销额占比)") or "未提供")],
            ["3 建议价 / 区间与溢价·折价", rec_txt,
             "在位者中位是**锚**, 不是新品自动目标价; 无成本数据 → 不生成利润/加价率结论"],
            ["4 价值论证(为什么值这个价)",
             "差异化配置 `%s` + 关联痛点 `%s`(优先级 %s, 负向 %s 条) + 好评驱动 `%s`; "
             "权衡: 段内 Top3 品牌占销额 %s(竞争强度), 关联证据为弱关联"
             % (c["config"], (c.get("issue") or {}).get("问题") or "未提供",
                (c.get("issue") or {}).get("优先级") or "未提供",
                _fmt_num((c.get("issue") or {}).get("负向评论数")),
                "见 Concept 画像 User fit 行",
                _pct(_first_of(mrow, "Top3品牌销额占比(段内)", "段内 Top3 品牌占比(%)", numeric=True))),
             "功能溢价(横截面关联; 不是因果支付意愿, 也不是 BOM 成本)"],
            ["5 改价条件与不可断言项",
             "复核触发: 段内中位价变动 >10% · 出现新的被接受新品 · 差评主题迁移 · 成本/工程数据补齐; "
             "**不可断言**: 市场定位价 ≠ 支付意愿最优/利润预测; 不含税费与促销; 未含成本",
             "contract §2/§8: 上市/常规/条件升级价分开, 不做自动涨价"],
        ]
        tid = "r-price-%d" % i
        tabs.append(table(tid, "详细定价建议(5 点) · 候选 %d: %s 段" % (i, c["band"]),
                          ["要点", "内容", "依据 / 来源"], rows,
                          source_label="价格段内结构 + Top20 + 候选评分",
                          source_ref="contract §2 pricing recommendation"))
    notes.append("价格建议是**市场定位价**, 不是盈利最优或授权涨价; 数字支撑不足时给有界区间与缺失输入, "
                 "不给虚假精度。")
    return section("r-price", "详细定价建议(5 点)", tabs, notes=notes)


def _recommend_sheet(kw_sheet: dict, market_sheet: dict, voc_sheet: dict,
                     recs=None, extra=None, voc_path: str | None = None) -> dict:
    """Sheet 4: 选品建议(对齐 skill `report-spec.md` §6)。

    模块顺序: `选品结论摘要` → `机会白地矩阵` → `候选产品方案评分`(100 分制) → `产品定义矩阵` →
    `风险与验证计划` → `Listing与上市验证` → `不建议方向与停止条件`。

    **全部输入只来自本报告的前三张 sheet**(不引入外部假设): 价格段 `m-band-mix`/`m-price`、
    功能渗透 `m-band-func`、VOC `v-core`/`v-func`/`v-exp`/`v-scene`、关键词 `kw-core`。
    评分口径、归一化方式、缺失处理、门槛与标签判定顺序都在旁注里写明(spec §6 要求可复现)。
    """
    kw = _tab_index(kw_sheet)
    mk = _tab_index(market_sheet)
    vc = _tab_index(voc_sheet)
    bands = _rows_dicts(mk["m-price"])
    mix = _rows_dicts(mk["m-band-mix"])
    mix_by_label = {str(r.get("价格段")): r for r in mix}
    bands_sorted = [str(b.get("价格段")) for b in bands]
    # 功能渗透: m-band-func 行=功能, 列=各价格段(值=段内销额占比)
    heat = _rows_dicts(mk["m-band-func"]) if "m-band-func" in mk else []
    heat_cols = list(mk["m-band-func"]["headers"][1:]) if "m-band-func" in mk else []
    issues = []
    for tid in ("v-core", "v-func", "v-exp"):
        if tid in vc:
            for r in _rows_dicts(vc[tid]):
                if r.get("问题") and r["问题"] not in {x.get("问题") for x in issues}:
                    issues.append(r)
    scenes = _rows_dicts(vc["v-scene"]) if "v-scene" in vc else []
    scene_top = str(scenes[0].get("场景(评论明确写出)")) if scenes else "未提供"
    kw_rows = _rows_dicts(kw["kw-core"]) if "kw-core" in kw else []
    kw_s = [(_num(r.get("站内月搜索量"), 0), str(r.get("关键词"))) for r in kw_rows]
    kw_s.sort(reverse=True)
    kw_top_s, kw_top = (kw_s[0] if kw_s else (0, "未提供"))
    trend_txt = str(kw_rows[0].get("搜索趋势")) if kw_rows else "未提供"
    kw_raw = 70.0 + (30.0 * (0.5 if "同比" not in trend_txt else 0.5))   # 类目级: 见 notes 披露

    def _issue_for(attr_key: str) -> dict | None:
        """按 VOC 属性关键词找**负向优先级分最高**的问题行(关联口径: 属性名包含关键词)。"""
        hit = [r for r in issues if attr_key and attr_key in str(r.get("所属属性"))]
        return max(hit, key=lambda r: (_num(r.get("负向优先级分"), 0))) if hit else None

    cands = []
    for label in bands_sorted:
        mrow = mix_by_label.get(label) or {}
        share = _num(mrow.get("销额占比"))
        if share is None:
            continue
        n_band = _num(mrow.get("样本数"), 0)
        need = 5
        # 差异化配置: 该段内渗透最低、且能挂到 VOC 问题的配置(spec: 低渗透 ≠ 机会, 必须有需求/痛点证据)
        best = None
        for h in heat:
            fn = str(h.get("功能"))
            prev = _num(h.get(label))
            if prev is None or prev >= 0.5:
                continue
            attr, rel = _voc_attr_link(fn)
            iss = _issue_for(attr)
            if not iss:
                continue
            pr = _num(iss.get("负向优先级分"), 0)
            score = pr * (1.0 - prev) * rel
            if best is None or score > best[0]:
                best = (score, fn, prev, iss, rel)
        seg_new = _num(mrow.get("新品销额占比"))
        best_iss = (best[3] if best else None) or {}      # 没有差异化配置时不能下标 best(会 TypeError)
        top3 = _first_num(mrow, "Top3品牌销额占比(段内)", "Top3品牌销额占比(段)")
        need_band = max(5, int(0.03 * max(1, len(_valid_variants(recs)))))   # 3% 有效变体(非段数)
        suf = min(1.0, (n_band or 0) / need_band)
        comp = {
            "关键词需求": round(kw_raw, 1),
            "价段吸引": round(70.0 * min(1.0, (share or 0) / max([_num(b.get("销额占比"), 0) for b in bands] or [1]))
                              + 30.0 * min(1.0, (seg_new or 0) / 0.5), 1),
            "竞争余量": round(100.0 * (1.0 - min(1.0, top3 if top3 is not None else 1.0)), 1),
            "配置白地": (round(100.0 * (1.0 - (best[2] or 0))
                               * (1.0 if str(best_iss.get("优先级")) == "P0" else 0.85), 1)
                       if best else None),
            "VOC问题价值": (round(_num(best_iss.get("负向优先级分"), 0) * best[4], 1) if best else None),
            "证据置信": round(100.0 * (0.4 * suf
                                       + 0.3 * min(1.0, (_num(best_iss.get("C"), 0) or 0) / 5.0)
                                       + 0.3 * (1.0 if str(mrow.get("样本判定")) == "样本充分" else 0.5)), 1),
        }
        avail = {k: v for k, v in comp.items() if v is not None}
        wsum = sum(RECO_WEIGHTS[k] for k in avail)
        total = round(sum(avail[k] * RECO_WEIGHTS[k] for k in avail) / (wsum or 1), 1)
        # ---- 标签判定顺序(spec §6): 硬否决 → 置信度封顶 → 总分带 → 优先开发的分项最低
        veto = None
        safety_txt = str((best[3] or {}).get("安全等级")) if best else ""
        if best and str((best[3] or {}).get("优先级")) == "P0" and ("A" in safety_txt or "B" in safety_txt):
            veto = "链接到未解决的 P0 安全问题(安全等级 %s)" % safety_txt
        if not best:
            veto = "本段找不到『低渗透 + 有 VOC 痛点证据』的差异化配置 → 无可辩护的差异化"
        if (n_band or 0) < 5:
            veto = veto or "段样本 <5, 价格段基线能力证据不足"
        conf = comp["证据置信"] or 0
        if veto:
            label_ = "不建议进入"
        else:
            cap = "持续观察" if conf < 50 else ("小批验证" if conf < 70 else "优先开发")
            band_lbl = ("优先开发" if total >= 75 else "小批验证" if total >= 60
                        else "持续观察" if total >= 45 else "不建议进入")
            order = {"优先开发": 3, "小批验证": 2, "持续观察": 1, "不建议进入": 0}
            label_ = cap if order[cap] < order[band_lbl] else band_lbl
            mins_ok = ((comp["关键词需求"] or 0) >= 60 or (comp["VOC问题价值"] or 0) >= 60) and \
                      (comp["价段吸引"] or 0) >= 50 and (comp["配置白地"] or 0) >= 60 and conf >= 70
            if label_ == "优先开发" and not mins_ok:
                label_ = "小批验证" if conf >= 50 else "持续观察"
        gate_ok = (not veto) and total >= 60 and (comp["价段吸引"] or 0) >= 50 and conf >= 50 and \
                  ((comp["关键词需求"] or 0) >= 60 or (comp["VOC问题价值"] or 0) >= 60) and \
                  (((comp["竞争余量"] or 0) >= 40) or ((comp["配置白地"] or 0) >= 60))
        cands.append({"band": label, "share": share, "comp": comp, "total": total, "label": label_,
                      "gate": gate_ok, "config": (best[1] if best else "未提供"),
                      "prevalence": (best[2] if best else None),
                      "issue": (best[3] if best else None), "veto": veto,
                      "row": mrow, "scene": scene_top})
    cands.sort(key=lambda c: -c["total"])
    for i, c in enumerate(cands, 1):
        c["rank"] = i
    passed = [c for c in cands if c["gate"]]

    # ---------------------------------------------------------------- 摘要
    top1 = cands[0] if cands else None
    sum_rows = [
        ["候选数", "%d 个(每个价格段一个候选方案)" % len(cands), "候选=价格段 × 段内低渗透且挂到 VOC 痛点的配置"],
        ["过门槛数", "%d 个" % len(passed), "门槛见本页『评分口径』旁注(spec §6 默认门槛)"],
        ["当前可执行机会", "有" if passed else "无(→ 类目结论 `暂不建议进入`)",
         "spec §6: 至少一个候选通过门槛才算当前可执行机会"],
        ["最高分方案", ("%s 段 + %s(总分 %.1f, 标签 %s)" % (top1["band"], top1["config"], top1["total"], top1["label"]))
         if top1 else "未提供", "标签与总分口径见下"],
        ["标签分布", "、".join("%s:%d" % (lb, sum(1 for c in cands if c["label"] == lb))
                            for lb in ("优先开发", "小批验证", "持续观察", "不建议进入") if any(
                                c["label"] == lb for c in cands)) or "未提供", "判定顺序: 硬否决→置信度封顶→总分带→分项最低"],
        ["未提供输入", "成本/费用/毛利、工程可行性、认证工作量(源数据未提供, 不参与评分)",
         "spec §6: 无成本数据时不得生成财务否决; 缺失一律写 `未提供`"],
        ["VOC 关联强度", ("有 P0/P1 强关联" if any(str((c["issue"] or {}).get("优先级")) in ("P0", "P1")
                                          for c in cands) else
                          "⚠ 本轮候选的差异化配置只挂到 P2/P3 问题(弱关联) → 按 spec §6 不应标 `优先开发`, "
                          "结论以 `小批验证/持续观察` 为主"),
         "关联 = 配置列名经 CONFIG_VOC_MAP 命中问题的 `所属属性`(显式词表, 见旁注)"],
        ["引用锚点", "关键词 `%s`(站内月搜索量 %s)、价格段 `%s`、VOC `%s`" % (
            kw_top, _fmt_num(kw_top_s), (top1["band"] if top1 else "未提供"),
            (_s((top1["issue"] or {}).get("问题")) if top1 else "未提供")),
         "spec §6: 每条建议至少可追溯到 关键词/市场/属性/品牌-Top20/阶梯/VOC 之一"],
    ]
    t_sum = table("r-summary", "选品结论摘要", ["项目", "结论", "口径/依据"], sum_rows,
                  source_label="本报告前三张 sheet", source_ref="市场大盘分析!A1")

    # ---------------------------------------------------------------- 机会白地矩阵
    white_rows = []
    for c in cands:
        iss = c["issue"] or {}
        white_rows.append([c["band"], c["scene"], "关键词 `%s` 月搜索量 %s" % (kw_top, _fmt_num(kw_top_s)),
                           round((c["share"] or 0) * 100, 2), 
                           ("%.1f" % ((_num(c["row"].get("新品销额占比"), 0) or 0) * 100)),
                           _fmt_num((_num(c["row"].get("样本数"), 0))),
                           ("%.1f" % ((_first_num(c["row"], "Top3品牌销额占比(段内)",
                                                         "Top3品牌销额占比(段)") or 0) * 100)),
                           ("%s %.1f%%" % (c["config"], (c["prevalence"] or 0) * 100)) if c["prevalence"] is not None else "未提供",
                           ("%s(%s, 负向优先级分 %s)%s" % (
                               _s(iss.get("问题")), _s(iss.get("优先级")),
                               _fmt_num(iss.get("负向优先级分")),
                               "[弱关联: 该问题优先级为 %s]" % _s(iss.get("优先级"))
                               if _s(iss.get("优先级")) not in ("P0", "P1") else "")) if iss else "未提供",
                           str(c["row"].get("样本判定"))])
    t_white = table("r-white", "机会白地矩阵(价格段 × 场景 × 配置)",
                    ["价格段", "目标场景(评论明确写出)", "需求证据", "段销额占比(%)", "段内新品销额占比(%)",
                     "段样本数", "段内 Top3 品牌占比(%)", "差异化配置(段内渗透率)", "VOC 相关性", "证据完整度"],
                    white_rows, source_label="市场大盘 02/02A/08 + VOC 02/04",
                    source_ref="市场大盘分析!A1")

    # ---------------------------------------------------------------- 候选产品方案评分(100 分制)
    score_head = ["方案", "排名"]
    for k in RECO_WEIGHTS:
        # 加权列名带上分项名: 两个 20% 分项若都叫「加权(20%)」会重名 → 查看器 metricIndex/表格定位都会歧义
        score_head += ["%s(0-100)" % k, "%s加权(%d%%)" % (k, RECO_WEIGHTS[k])]
    score_head += ["总分"]
    score_rows = []
    for c in cands:
        row = ["%s段+%s" % (c["band"], c["config"]), c["rank"]]
        for k in RECO_WEIGHTS:
            v = c["comp"].get(k)
            row += [v if v is not None else "未提供",
                    (round(v * RECO_WEIGHTS[k] / 100.0, 2) if v is not None else "未提供(权重不计入分母)")]
        row.append(c["total"])
        score_rows.append(row)
    t_score = table("r-score", "候选产品方案评分(市场机会分, 100 分制)",
                    score_head, score_rows, source_label="权重 20/20/15/20/15/10 = 100%",
                    source_ref="市场大盘分析!A1")
    t_label = table("r-label", "门槛与标签判定结果",
                    ["方案", "是否过门槛", "标签", "硬否决原因", "总分", "证据置信", "分项最低是否满足"],
                    [[c["row"] and "%s段+%s" % (c["band"], c["config"]), "通过" if c["gate"] else "未通过",
                      c["label"], c["veto"] or "—", c["total"], c["comp"].get("证据置信"),
                      ("满足" if ((c["comp"].get("关键词需求") or 0) >= 60 or (c["comp"].get("VOC问题价值") or 0) >= 60)
                       and (c["comp"].get("价段吸引") or 0) >= 50 and (c["comp"].get("配置白地") or 0) >= 60
                       and (c["comp"].get("证据置信") or 0) >= 70 else "不满足(优先开发需 关键词/VOC≥60、价段≥50、白地≥60、置信≥70)")]
                     for c in cands], source_label="spec §6 Selection decision-label rules",
                    source_ref="市场大盘分析!A1")

    # ---------------------------------------------------------------- 产品定义矩阵(只含可执行机会)
    def_rows = []
    for c in passed:
        mrow = c["row"]
        p25 = p75 = "未提供"
        def_rows.append([c["band"], "%s / %s" % (c["scene"], kw_top),
                         "段内中位价支撑: 见 08 节价格分位(%s)" % ("%s 元段" % c["band"]),
                         "主导形态: %s" % (mrow.get("主导产品类型(段内销额占比)") or "未提供"),
                         "基线: %s" % (mrow.get("主导材质(段内销额占比)") or "未提供"),
                         "差异化: %s(段内渗透 %.1f%%)" % (c["config"], (c["prevalence"] or 0) * 100),
                         _s((c["issue"] or {}).get("问题")) or "未提供",
                         "置信度 %.1f(证据置信分)" % (c["comp"].get("证据置信") or 0),
                         "需验证: 该配置的实测收益(D0 样机 / D30 小批)"] )
    if not def_rows:
        def_rows = [["未提供", "—", "—", "—", "—", "—", "—", "—",
                     "当前没有候选通过门槛 → 按 spec §6 **不编造产品画像与价格建议**"]]
    t_def = table("r-def", "产品定义矩阵(仅含可执行机会)",
                  ["目标价格段", "人群/场景 + 核心关键词", "价格窗口依据", "产品形态", "基线配置",
                   "差异化配置", "要解决的 VOC 问题", "置信度", "必需验证"],
                  def_rows, source_label="市场大盘 08 + VOC", source_ref="市场大盘分析!A1")

    # ---------------------------------------------------------------- 风险与验证计划
    risk_rows = []
    for c in cands[:5]:
        iss = c["issue"] or {}
        risk_rows.append([c["band"], _s(iss.get("问题")) or "需求/差异化证据不足",
                          "安全等级 %s / 优先级 %s" % (_s(iss.get("安全等级")) or "无", _s(iss.get("优先级")) or "未提供"),
                          "受影响: 全部目标用户(类目级 VOC, 非品牌归因)",
                          _s(iss.get("根因假设")) or "未提供",
                          "样机 + 可靠性/连续运行测试",
                          "待定义(源数据未提供工程阈值)" if not _s(iss.get("验证方式")) else _s(iss.get("验证方式")),
                          "不通过则回到 `持续观察`"])
    t_test = table("r-test", "风险与验证计划",
                   ["价格段", "风险项", "证据(等级/优先级)", "受影响用户/场景", "根因假设",
                    "验证方式", "通过标准", "依赖该测试的决策"],
                   risk_rows or [["未提供", "无候选", "—", "—", "—", "—", "—", "—"]],
                   source_label="VOC 差评问题分析 + 改进与保留路线图", source_ref="VOC差评分析!A1")

    # ---------------------------------------------------------------- Listing 与上市验证
    claims_tab = vc.get("v-claim")
    claim_rows = _rows_dicts(claims_tab) if claims_tab else []
    listing_rows = []
    for c in cands[:5]:
        risk_claims = "、".join(_s(r.get("宣称方向")) for r in claim_rows[:3]) or "未提供"
        listing_rows.append([c["band"], "围绕 `%s` + 场景『%s』" % (kw_top, c["scene"]),
                             "需规避/谨慎的宣称: %s" % risk_claims,
                             "覆盖: 标题+五点含 `%s`; 长尾词做 PPC 精准" % kw_top,
                             "上市 30 天: 核心词自然位次、转化率、差评主题是否包含已修问题"])
    t_listing = table("r-listing", "Listing 与上市验证",
                      ["价格段", "卖点主线", "宣称边界", "关键词/PPC 安排", "上市验证指标"],
                      listing_rows or [["未提供", "—", "—", "—", "—"]],
                      source_label="关键词 kw-core + VOC Listing卖点_风险", source_ref="关键词分析!A1")

    # ---------------------------------------------------------------- 不建议方向与停止条件
    no_rows = []
    for c in cands:
        if c["label"] == "不建议进入" or c["veto"]:
            no_rows.append(["%s段" % c["band"], c["veto"] or "总分低于 45",
                            "需要改变的条件: " + ("解决该安全问题并复测" if "安全" in str(c["veto"])
                                            else "补齐段样本或找到可辩护的差异化配置"),
                            "复核触发: 新增样本 ≥30 条 / 新一期 VOC / 新竞品进入"])
    for c in cands:
        if c["config"] != "未提供" and c["prevalence"] is not None and c["prevalence"] >= 0.5:
            no_rows.append(["%s段的高渗透配置「%s」" % (c["band"], c["config"]),
                            "已是段内主流(渗透 ≥50%) → 作为差异化无意义",
                            "只能作为**基线能力**而非卖点",
                            "复核触发: 该配置渗透率跌破 50% 时重新评估"])
    if not no_rows:
        no_rows = [["未提供", "本轮没有硬否决/不建议方向", "—", "—"]]
    t_no = table("r-no", "不建议方向与停止条件",
                 ["方向/配置", "决定性原因", "需要改变的条件", "复核触发"],
                 no_rows[:15], source_label="spec §6 硬否决与停止条件", source_ref="市场大盘分析!A1")

    notes = [
        "**评分口径(spec §6, 全部 0–100 后再加权)**: %s —— 权重合计 100%%。"
        % "、".join("%s %d" % (k, v) for k, v in RECO_WEIGHTS.items()),
        "**分项规则**: ①关键词需求 = 类目级(最高需求核心词 + 趋势) —— ⚠ 本类目**没有按价位段切分的关键词数据**, "
        "故该分项对所有候选同值, 需要按段关键词数据才能细分; ②价段吸引 = 0.7×段销额占比归一 + 0.3×段内新品销额占比归一 "
        "(销额+生命周期证据); ③竞争余量 = 100×(1 − 段内 Top3 品牌销额占比); ④配置白地 = 100×(1 − 该配置段内渗透率) × "
        "痛点权重(P0=1.0 / 其他=0.85); ⑤VOC问题价值 = 该问题负向优先级分 × 解决相关度(显式词表 CONFIG_VOC_MAP); "
        "⑥证据置信 = 0.4×段样本充足度 + 0.3×(问题 C 值/5) + 0.3×段样本判定。",
        "**缺失处理**: 任一分项缺失 → 该分项不参与(权重不计入分母), 并在表里写 `未提供`; 成本/费用/毛利/工程可行性一律 `未提供` "
        "且**不生成财务否决**(spec §6)。",
        "**门槛(spec §6 默认)**: 关键词需求或 VOC 价值 ≥60; 价段吸引 ≥50; 竞争余量 ≥40 或配置白地 ≥60; 证据置信 ≥50; "
        "总分 ≥60; 且无硬否决。",
        "**标签判定顺序(spec §6)**: ①硬否决 → `不建议进入`; ②证据置信 <50 封顶 `持续观察`、50–69 封顶 `小批验证`、"
        "≥70 才允许 `优先开发`; ③总分带 75/60/45 → 优先开发/小批验证/持续观察/不建议进入; ④`优先开发` 还需 关键词或 VOC ≥60、"
        "价段 ≥50、白地 ≥60、置信 ≥70。两条规则冲突时取**更保守**的结果。",
        "**候选生成口径**: 每个价格段一个候选 = 段内**低渗透(<50%)且能挂到 VOC 痛点**的配置(渗透低 ≠ 机会, 必须同时有痛点证据); "
        "挂不上 VOC 问题的配置不作为候选。",
        "**边界**: 市场机会分是**比较性机会分**, 不是利润预测/成功概率/技术可行性(spec §6); 配置与价格的差异表述为"
        "**横截面关联**, 不是因果提升(spec §7)。",
    ]
    secs = [
        section("r-1", "选品结论摘要", [t_sum], notes=[notes[0], notes[6]]),
        section("r-2", "机会白地矩阵", [t_white],
                notes=[notes[5], "空白/低渗透单元格必须同时有需求或痛点证据才可视为机会(spec §6)。"]),
        section("r-3", "候选产品方案评分", [t_score, t_label],
                notes=[notes[0], notes[1], notes[2], notes[3], notes[4]]),
        section("r-4", "产品定义矩阵", [t_def],
                notes=["按 spec §6 只列**可执行机会**(通过门槛的候选); 无候选时不编造产品画像与价格建议。",
                       "价格窗口需用**该段详细价格段与可比配置价格分布**(中位与 P25–P75)交叉验证, 见 08 节价格分位。"]),
        _hero_section(recs, extra, mix_by_label, voc_sheet, passed),
        _xsource_section(recs, extra, mix_by_label, voc_sheet, passed, voc_path),
        _concept_section(passed, mix_by_label, voc_sheet, market_sheet),
        _price_section(passed, mix_by_label, market_sheet, recs),
        # 2026-10-12 同步新版 SKILL.md §14: 默认**不设**独立的 `风险与验证计划`/`Listing与上市验证` 模块;
        # 材料风险改为贴在受影响方向旁(见 r-crisk-*), Listing/PPC 细节留在源表与证据库。
        section("r-7", "不建议方向与停止条件", [t_no],
                notes=["包含: 过度饱和配置、无需求/无溢价证据的功能、经济性证据薄弱的价段、未解决的安全/可靠性风险, "
                       "以及触发重新评估的条件(spec §6)。"]),
    ]
    return sheet("选品建议", "选品建议",
                 "结论摘要 / 机会白地矩阵 / 100 分制评分 / 产品定义 / 新品→Hero / 跨源证据矩阵 / "
                 "Concept 画像(含方向旁材料风险) / 详细定价 / 不建议方向"
                 "(来源: 前三张 sheet 的同源事实 + 步骤18 变体级数据; 新版 SKILL.md 默认不设独立风险与 Listing 模块)", [], secs)


def _sheet_kpi(sheet_obj: dict, label: str):
    """从某张 sheet 的 KPI 卡里取值(决策摘要只引用, 不重算 —— 避免两套口径)。"""
    for k in sheet_obj.get("kpis") or []:
        if k.get("label") == label:
            return k.get("value")
    return None


def _tab_of(sheet_obj: dict, tid: str):
    for sec in sheet_obj.get("sections") or []:
        for t in sec.get("tables") or []:
            if t.get("id") == tid:
                return t
    return None


def _row_by(tab, key, col: int = 0) -> dict:
    for r in (tab or {}).get("rows") or []:
        if r["values"][col] == key:
            return dict(zip(tab["headers"], r["values"]))
    return {}


def _mark_status(sheet_obj: dict, text: str) -> None:
    """给模块打**状态标签**(contract: 每个模块必须标 recomputed/imported/unverified/insufficient)。"""
    secs = sheet_obj.get("sections") or []
    if secs:
        secs[0].setdefault("notes", []).insert(0, {"text": text})


def _kw_overview(src: Sources) -> dict:
    """读步骤8 报告 `结论总览`(项目→内容): 决策摘要要引用双榜样本/目标月份/季节性结论等原始口径。"""
    path = Path(getattr(src, "dir", ".")) / ("%s-keyword-report.xlsx" % src.cat)
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:                                   # noqa: BLE001
        return {}
    try:
        if "结论总览" not in wb.sheetnames:
            return {}
        out = {}
        for row in wb["结论总览"].iter_rows(values_only=True):
            if row and row[0] not in (None, ""):
                out[_s(row[0])] = _s(row[1]) if len(row) > 1 else ""
        return out
    finally:
        try:
            wb.close()
        except Exception:                               # noqa: BLE001
            pass


def _exec_sheet(kw_sheet: dict, market_sheet: dict, voc_sheet: dict, reco_sheet: dict,
                inputs: dict, overview: dict) -> dict:
    """首屏「决策摘要与范围」: 只**引用**前三张 sheet 的同源事实, 不重算(避免两套口径)。

    contract §2 要求: 源时间窗/样本规模/价格锚/集中度/主要机会/主要反证/当前决策状态,
    且明确标注"源报告覆盖样本"而不是全市场; 每个模块标状态; 来源可追溯。
    """
    r_sum = _tab_of(reco_sheet, "r-summary")
    scope_rows = [
        ["源时间窗与样本",
         "关键词目标月份 %s; 双榜 ASIN 样本 %s(BS %s / NR %s); 有效变体 %s; VOC 去重评论 %s(负向 %s)"
         % (overview.get("目标月份") or "未提供", overview.get("双榜ASIN样本") or "未提供",
            overview.get("BS合并") or "未提供", overview.get("NR合并") or "未提供",
            _fmt_num(_sheet_kpi(market_sheet, "有效变体数")),
            _fmt_num(_sheet_kpi(voc_sheet, "去重后唯一评论")),
            _fmt_num(_sheet_kpi(voc_sheet, "1–3★(负向)评论"))),
         "各源报告口径; 抓取时点快照, 非全市场/TAM"],
        ["规模与价格锚",
         "变体月销额 %s USD; 均价 %s / 中位价 %s / 成交加权价 %s USD "
         "(%s)"
         % (_fmt_num(_sheet_kpi(market_sheet, "变体月销额合计(USD)"), 2),
            _fmt_num(_sheet_kpi(market_sheet, "均价(USD)"), 2),
            _fmt_num(_sheet_kpi(market_sheet, "中位价(USD)"), 2),
            _fmt_num(_sheet_kpi(market_sheet, "成交加权价格(USD, 可选口径)"), 2),
            _row_by(_tab_of(market_sheet, "m-summary"), "价格结构").get("结论", "价格结构见 02 节")),
         "变体口径(变体月销量/变体月销额); 均价=算术平均, 成交加权价单列"],
        ["集中度",
         "%s" % (_row_by(_tab_of(market_sheet, "m-summary"), "集中度").get("结论", "未提供"),),
         "品牌销额占比分母=全部有效变体月销额; HHI=Σ占比²×10,000"],
        ["新品结构",
         "%s" % (_row_by(_tab_of(market_sheet, "m-summary"), "新老品").get("结论", "未提供"),),
         "数量/销量/销额三口径分别以 变体数/月销量/月销额 为分母"],
        ["主要机会",
         "%s" % (_row_by(r_sum, "最高分方案").get("结论", "未提供"),),
         "机会分=100 分制(权重 20/20/15/20/15/10); 门槛与标签判定顺序见选品建议模块"],
        ["主要反证",
         "%s" % (_row_by(r_sum, "VOC 关联强度").get("结论", "未提供"),),
         "反证与限制来自 VOC 模块与源表缺失披露; 不因反证而删除机会结论"],
        ["当前决策状态",
         "%s; 标签分布 %s" % (_row_by(r_sum, "当前可执行机会").get("结论", "未提供"),
                             _row_by(r_sum, "标签分布").get("结论", "未提供")),
         "spec 口径: 至少一个候选通过门槛才算当前可执行机会"],
        ["覆盖范围声明",
         "以上均为**源报告覆盖样本**内的结论, 不是全市场/TAM; 价格与销量为抓取时点快照, "
         "历史趋势类结论受样本期限制; 缺失字段在各模块就近标注 `未提供`/`数据不足`",
         "contract §2: 明确标注样本尺度, 不把源覆盖当全市场"],
    ]
    t_scope = table("e-scope", "决策状态与范围", ["维度", "结论", "口径/基准"], scope_rows,
                    source_label="步骤8 关键词报告 + 步骤18 交付表 + 外部VOC 报告",
                    source_ref="本报告前三张 sheet")

    opp_rows = []
    price_tab = _tab_of(market_sheet, "m-price")
    best_band = None
    for r in (price_tab or {}).get("rows") or []:
        d = dict(zip(price_tab["headers"], r["values"]))
        if d.get("销额占比") is None:
            continue
        if best_band is None or (d.get("销额占比") or 0) > (best_band.get("销额占比") or 0):
            best_band = d
    band_txt = ("`%s` 段销额占比 %.1f%%(变体 %s 个 / 数量占比 %s) — 该段是成交主战场"
                % (best_band.get("价格段"), (best_band.get("销额占比") or 0) * 100,
                   _fmt_num(best_band.get("变体数")),
                   ("%.1f%%" % ((best_band.get("数量占比") or 0) * 100))
                   if best_band.get("数量占比") is not None else "未提供")) if best_band else "未提供"
    opp_rows.append(["机会", "成交主战场价格段", band_txt, "市场大盘 02 节(价格段销额占比)"])
    white = _tab_of(reco_sheet, "r-white")
    if white and white["rows"]:
        w0 = dict(zip(white["headers"], white["rows"][0]["values"]))
        opp_rows.append(["机会", "差异化配置(段内低渗透 + 挂 VOC 痛点)",
                         "%s 段: %s; 关联 VOC: %s"
                         % (w0.get("价格段"), w0.get("差异化配置(段内渗透率)"), w0.get("VOC 相关性")),
                         "选品建议 机会白地矩阵 + 市场大盘 08 节"])
    passed = [dict(zip(_tab_of(reco_sheet, "r-label")["headers"], r["values"]))
              for r in (_tab_of(reco_sheet, "r-label") or {}).get("rows", [])
              if dict(zip(_tab_of(reco_sheet, "r-label")["headers"], r["values"])).get("是否过门槛") == "通过"]
    if passed:
        opp_rows.append(["机会", "通过门槛的候选(可执行机会)", passed[0].get("方案", "未提供"),
                         "选品建议 r-label(门槛见旁注)"])
    issues_tab = _tab_of(voc_sheet, "v-core")
    if issues_tab and issues_tab["rows"]:
        i0 = dict(zip(issues_tab["headers"], issues_tab["rows"][0]["values"]))
        opp_rows.append(["反证", "最高优先级差评问题",
                         "%s(优先级 %s, 负向 %s 条, 安全等级 %s)"
                         % (i0.get("问题"), i0.get("优先级"), _fmt_num(i0.get("负向评论数")), i0.get("安全等级")),
                         "VOC 差评分析 01/02 节"])
    if white and white["rows"]:
        def _top3_of(rowd):
            # 列名在类目间可能微调(实测 `段内 Top3 品牌占比(%)`), 按表头**模糊匹配**更稳;
            # 值可能是 0–1 小数(段内占比)也可能是已格式化的百分数字符串("63.8") → 统一成 0–1。
            for k, v in rowd.items():
                ks = str(k)
                if "Top3" not in ks or not ("占比" in ks or "%" in ks):
                    continue
                x = v if isinstance(v, (int, float)) else _num_of(str(v).strip().rstrip("%"))
                if x is None:
                    continue
                return x / 100.0 if x > 1.0 else x
            return None

        wb_rows = [dict(zip(white["headers"], r["values"])) for r in white["rows"]]
        cand = [d for d in wb_rows if _top3_of(d) is not None]
        if cand:
            hard = max(cand, key=lambda d: _top3_of(d) or 0)
            opp_rows.append(["反证", "竞争最激烈的价格段(段内 Top3 品牌占比最高)",
                             "`%s` 段: 段内 Top3 品牌占销额 %.1f%%(该段变体 %s 个)"
                             % (hard.get("价格段"), (_top3_of(hard) or 0) * 100, _fmt_num(_num_of(hard.get("段样本数")) or _num_of(hard.get("样本数")))),
                             "选品建议 机会白地矩阵(段内销额分母)"])
    miss = _row_by(_tab_of(market_sheet, "m-summary"), "缺失披露").get("结论") or ""
    limit_txt = []
    if miss and miss != "属性列齐备":
        limit_txt.append(miss)
    limit_txt.append("价格/销量为抓取时点快照, 不得与历史趋势混用")
    limit_txt.append("季节/趋势类结论需 ≥2 完整年数据(当前源报告不足, 一律按 `数据不足` 处理)")
    limit_txt.append("价格段仅覆盖有价变体; 无价变体保留在总量口径但不参与分段")
    opp_rows.append(["反证", "样本与口径限制", "; ".join(limit_txt),
                     "市场大盘 01 节缺失披露行 + 各模块口径旁注"])
    t_opp = table("e-opp", "主要机会 / 主要反证", ["类型", "要点", "证据", "来源"], opp_rows,
                  source_label="同源事实汇总(机会与反证并列, 不互相抵消)",
                  source_ref="前三张 sheet")

    def _src_line(step: int) -> str:
        v = (inputs or {}).get(step) or {}
        p = Path(str(v.get("path") or ""))
        return "%s(%s)" % (p.name or "未提供", "显式指定" if v.get("explicit") else "自动定位")

    idx_rows = [
        ["关键词分析", "重新计算(四条准入逐条) + 沿用源报告(FR-07 原始判定)",
         "搜索量≥30000 / 站内同比≥-20% / Google同比≥-20% / CPC<3; 季节门槛需 ≥2 完整年",
         _src_line(8)],
        ["市场大盘分析", "重新计算",
         "变体口径; 价格段由 choose_price_bands 动态推导; HHI=Σ占比²×10,000; 段内占比分母=段内变体月销额",
         _src_line(18)],
        ["VOC差评分析", "沿用源报告(V3 14 表) + 无法独立复核(LLM 语义标注)",
         "问题/驱动/安全/场景等分类与评分来自 VOC 工作簿; 本报告只做归口(功能 vs 体验)与汇总",
         _src_line(19)],
        ["选品建议", "重新计算(评分/门槛/标签) + 数据不足(成本/工程/认证)",
         "100 分制: 关键词需求20/价段吸引20/竞争余量15/配置白地20/VOC价值15/证据置信10; "
         "无成本数据时不生成财务否决",
         "前三张 sheet 的同源事实"],
        ["本 HTML", "重新计算(本次构建)",
         "单文件离线(内嵌 CSS/JS/数据); 图表均配可核对数据表; 源文本安全转义",
         Path(__file__).name],
    ]
    t_idx = table("e-src", "模块状态与来源索引", ["模块", "状态", "依据/口径", "来源文件"],
                  idx_rows, source_label="contract §2/§3: 模块级状态与可追溯来源",
                  source_ref="本报告")

    notes = [
        "本页只做**汇总与索引**: 所有数字均引用其余四个模块的同源事实(不重算, 避免两套口径); "
        "结论冲突时以对应模块的证据与限制为准。",
        "**样本尺度**: 以上是源报告覆盖样本内的结论, 不等于全市场/TAM; 抓取时点快照的价格与销量"
        "不得与历史趋势混用。",
        "**未完成/无法完成的事**: 成本、工程可行性、认证与利润率数据源未提供 → 一律 `未提供`, "
        "不得据此生成财务否决; VOC 的语义标注为上游 LLM 结果, 本报告**无法独立复核**。",
    ]
    kpis = [
        kpi("有效变体数", _sheet_kpi(market_sheet, "有效变体数")),
        kpi("变体月销额(USD)", _sheet_kpi(market_sheet, "变体月销额合计(USD)")),
        kpi("均价(USD)", _sheet_kpi(market_sheet, "均价(USD)")),
        kpi("中位价(USD)", _sheet_kpi(market_sheet, "中位价(USD)")),
        kpi("品牌 HHI(×10,000)", _sheet_kpi(market_sheet, "品牌 HHI(×10,000)")),
        kpi("1–3★(负向)评论", _sheet_kpi(voc_sheet, "1–3★(负向)评论")),
        kpi("VOC 问题条目", _sheet_kpi(voc_sheet, "问题条目")),
        kpi("过门槛候选", _row_by(r_sum, "过门槛数").get("结论", "未提供")),
    ]
    secs = [
        section("e-1", "01 决策状态与范围", [t_scope], notes=notes),
        section("e-2", "02 主要机会 / 主要反证", [t_opp],
                notes=["机会与反证并列呈现: 反证用于限定结论强度与验证优先级, 不自动否定机会。"]),
        section("e-3", "03 模块状态与来源索引", [t_idx],
                notes=["状态口径: **重新计算**=本报告从源数据算出 · **沿用源报告**=直接引用上游产物 · "
                       "**无法独立复核**=依赖上游语义判断 · **数据不足**=源数据缺失(不猜、不补 0)。"]),
    ]
    return sheet("决策摘要", "决策摘要", "源时间窗与样本 / 价格锚与集中度 / 主要机会与反证 / 模块状态与来源",
                 kpis, secs)


_BAD_POINTERS = ("undefined", "None!", "null!", "Aundefined")


def _check_source_pointers(report: dict) -> None:
    """contract §2 L95: **绝不显示** `Aundefined` 这类坏来源指针(KPI 卡与表的来源标注都会拼字符串)。"""
    bad = []
    for sh in report.get("sheets") or []:
        for k in sh.get("kpis") or []:
            txt = "%s%s%s" % (k.get("sourceRef") or "", k.get("sourceRow") or "", k.get("sourceCol") or "")
            if any(b in txt for b in _BAD_POINTERS):
                bad.append("%s / %s -> %s" % (sh.get("name"), k.get("label"), txt))
        for sec in sh.get("sections") or []:
            for t in sec.get("tables") or []:
                txt = "%s %s" % (t.get("sourceLabel") or "", t.get("sourceRef") or "")
                if any(b in txt for b in _BAD_POINTERS):
                    bad.append("%s / %s -> %s" % (sh.get("name"), t.get("id"), txt))
    if bad:
        raise ValueError("坏来源指针: %s" % "; ".join(bad[:5]))


def validate_report(report: dict) -> None:
    """镜像 kit `build_offline_html.validate()` 的规则(缺一即拒, 防出坏 HTML)。"""
    if not isinstance(report, dict):
        raise ValueError("报告必须是 JSON 对象")
    for k in ("id", "title", "filename", "generatedAt"):
        if not isinstance(report.get(k), str) or not report[k].strip():
            raise ValueError(f"顶层字段缺失或为空: {k}")
    sheets = report.get("sheets")
    _check_source_pointers(report)
    if not isinstance(sheets, list) or [s.get("name") for s in sheets] != SHEET_ORDER:
        raise ValueError("sheets 必须按 关键词分析、市场大盘分析、VOC差评分析、选品建议 排列")
    ids: set[str] = set()
    for s in sheets:
        for k in ("title", "subtitle", "kpis", "sections"):
            if k not in s:
                raise ValueError(f"[{s.get('name')}] 缺少字段 {k}")
        if not s["sections"]:
            raise ValueError(f"[{s['name']}] sections 不能为空")
        for kp in s["kpis"]:
            if "label" not in kp or "value" not in kp:
                raise ValueError(f"[{s['name']}] KPI 缺 label/value")
            if kp.get("sourceRow") is not None and int(kp["sourceRow"]) < 1:
                raise ValueError(f"[{s['name']}] KPI sourceRow 必须 ≥1")
        for sec in s["sections"]:
            for k in ("id", "title", "tables", "notes", "insights"):
                if k not in sec:
                    raise ValueError(f"[{s['name']}] section 缺 {k}")
            for t in sec["tables"]:
                for k in ("id", "title", "headers", "columns", "sourceHeaderRow", "rows"):
                    if k not in t:
                        raise ValueError(f"[{s['name']}] table 缺 {k}")
                if t["id"] in ids:
                    raise ValueError(f"表 id 重复: {t['id']}")
                ids.add(t["id"])
                if not t["headers"] or len(t["columns"]) != len(t["headers"]):
                    raise ValueError(f"[{t['id']}] columns 与 headers 不等长")
                if int(t["sourceHeaderRow"]) < 1:
                    raise ValueError(f"[{t['id']}] sourceHeaderRow 必须 ≥1")
                for r in t["rows"]:
                    if len(r["values"]) != len(t["headers"]):
                        raise ValueError(f"[{t['id']}] 行 values 长度 ≠ 表头数")
                    if int(r.get("sourceRow", 0)) < 1:
                        raise ValueError(f"[{t['id']}] 行 sourceRow 必须 ≥1")
                    for v in r["values"]:
                        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                            raise ValueError(f"[{t['id']}] 含 NaN/Infinity")


def _render_html(report: dict, out_html: Path) -> None:
    """单文件离线 HTML: CSS/JS/数据全部内联(vendored `package_standalone.build`)。"""
    spec = importlib.util.spec_from_file_location("mrh_package_standalone",
                                                 VENDOR_DIR / "package_standalone.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                     # type: ignore[union-attr]
    out_html.parent.mkdir(parents=True, exist_ok=True)
    data_js = out_html.parent / ".report-data.tmp.js"
    payload = json.dumps([report], ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # contract §3: 内嵌 JSON 必须安全序列化 —— 源文本里若出现 `</script>`/`<!--`/`<script`
    # 会提前闭合脚本块, 整页崩掉。把 `<` 转成 \u003c(JSON 合法且解析后仍是原文)。
    payload = payload.replace("<", "\\u003c")
    data_js.write_text("window.REPORTS = " + payload + ";\n", encoding="utf-8")
    try:
        mod.build(out_html, data_js)
    finally:
        try:
            data_js.unlink()
        except OSError:
            pass
    html = out_html.read_text(encoding="utf-8")
    # kit 侧栏写死了示例名 `Cat Fountains`, 用报告标题替换 —— 必须**转义**(2026-10-10 真 bug:
    # 未转义时源数据里的 `</script>` 会直接破坏页面; contract §3 要求安全渲染源文本)。
    from html import escape as _esc
    _title = _esc(str(report.get("title") or "")[:40], quote=True)
    html = html.replace(">Cat Fountains<", ">%s<" % _title)
    out_html.write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------- ④ 主流程

def build(cat: str, data_root: str = "data", product: str | None = None,
          voc: str | None = None, keyword: str | None = None, progress=None) -> tuple[dict, dict]:
    """装配 4 模块报告。**调用前必须自行过门槛**(本函数假定三份输入都在)。

    未显式传 `product`/`voc` 时**按类目自动定位**(与门槛同一套路径规则) —— 漏这步会得到
    `VOC 工作簿路径不存在: None`(2026-10-10 真事故: 看板只传 `--cat`, 脚本却把 None 交给 VOC
    解析器; 前端只看到一句 "HTTP 200", 因为错误详情被吞了)。

    `progress(phase, pct)` 可选回调(看板按钮显示"正在做什么"); 缺省不报进度。
    """
    def step(phase: str, pct: int) -> None:
        if progress:
            progress(phase, pct)

    if product is None or voc is None:                 # ← 自动定位(显式传入优先)
        found = locate_inputs(cat, data_root, keyword, product, voc)
        product = product or (found[18]["path"] if found[18]["exists"] else None)
        voc = voc or (found[19]["path"] if found[19]["exists"] else None)

    cat_dir = str(Path(data_root) / cat)
    src = Sources(cat, cat_dir)
    step("读取步骤18 属性标注-标准化交付", 30)
    recs = src.load_product(product)
    step("读取步骤8 关键词报告", 50)
    kw_sheet = _keyword_sheet(src)
    step("装配市场大盘模块", 60)
    extra_cols = _read_feature_matrix(getattr(src, "product_path", None), list(src.header or []))
    market_sheet = _market_sheet(src, recs, extra_cols, voc_path=voc)
    step("解析外部VOC报告", 72)
    voc_data = read_voc_workbook(voc)
    if not voc_data.get("available", True):
        raise ValueError("VOC 工作簿无法解析: %s" % voc_data.get("reason"))
    voc_sheet = _voc_sheet(voc_data, voc_path=voc)
    step("归纳选品建议", 82)
    rec_sheet = _recommend_sheet(kw_sheet, market_sheet, voc_sheet, recs=recs, extra=extra_cols,
                                voc_path=voc)
    step("汇总决策摘要", 88)
    _inputs = locate_inputs(cat, data_root, keyword, product, voc)
    exec_sheet = _exec_sheet(kw_sheet, market_sheet, voc_sheet, rec_sheet, _inputs,
                             _kw_overview(src))
    # contract §2: 每个模块必须带状态标签(重新计算/沿用源报告/无法独立复核/数据不足)
    _mark_status(kw_sheet, "模块状态: **重新计算**(四条准入逐条判定) + 沿用源报告(FR-07 原始判定)")
    _mark_status(market_sheet, "模块状态: **重新计算**(分档/集中度/Top20/阶梯均由步骤18 交付表算出)")
    _mark_status(voc_sheet, "模块状态: **沿用源报告**(外部VOC V3 14 表口径) + "
                            "**无法独立复核**(LLM 语义标注)")
    _mark_status(rec_sheet, "模块状态: **重新计算**(机会分/门槛/标签由前三张 sheet 同源事实算出) + "
                            "**数据不足**(成本/工程可行性/认证)")
    report = {
        "id": "%s-integrated-%s" % (cat, datetime.now().strftime("%Y%m%d")),
        "title": "%s 市场调研整合报告" % cat,
        "filename": "%s 市场调研整合报告.html" % cat,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "sheets": [exec_sheet, kw_sheet, market_sheet, voc_sheet, rec_sheet],
    }
    _apply_notes_policy(report)          # 表下只留结论; 例行口径集中到每张 sheet 的「方法与口径」
    meta = {"cat": cat, "valid_variants": len(recs),
            "voc_issues": len(voc_data.get("issues") or []),
            "voc_evidence": len(voc_data.get("evidence_ids") or [])}
    return report, meta


_NOTE_METHOD_RE = re.compile(r"口径|阈值|公式|分母|来源|spec §|contract §|签名|触发|依据|基准|取数")
_NOTE_KEEP_RE = re.compile(r"结论|不生成|省略|不成立|数据不足|不一致|风险|建议|判定为|因此|未提供|注意")

_SHEET_PREFIX = {"决策摘要": "e", "关键词分析": "kw", "市场大盘分析": "m",
                 "VOC差评分析": "v", "选品建议": "r"}


def _apply_notes_policy(report: dict) -> dict:
    """**表下只留结论/洞察**(contract §2 L95 / QA L17)。

    常规计算、排名、来源与口径注释不再各表下重复 —— 按 sheet 集中到末尾的 `方法与口径` 一节(单张表)。
    判定: 含"结论/不成立/数据不足/风险/建议…"的**结果性**说明保留; 含"口径/阈值/公式/分母/来源/spec §…"的
    例行说明迁移; `模块状态:` 开头的一律保留(模块级状态标签是要求可见的)。
    """
    for sh in report.get("sheets") or []:
        moved, seconds = [], 0
        for sec in sh.get("sections") or []:
            notes = sec.get("notes") or []
            keep = []
            for n in notes:
                text = str(n.get("text") if isinstance(n, dict) else n).strip()
                if text.startswith("模块状态:"):
                    keep.append(n)
                    continue
                if _NOTE_KEEP_RE.search(text) and not _NOTE_METHOD_RE.search(text):
                    keep.append(n)
                    continue
                if _NOTE_METHOD_RE.search(text):
                    moved.append([sec.get("title") or "—", text])
                    seconds += 1
                else:
                    keep.append(n)
            sec["notes"] = keep
        if moved:
            pre = _SHEET_PREFIX.get(sh.get("name"), "x")
            sh.setdefault("sections", []).append(section(
                "%s-method" % pre, "方法与口径(集中一处, 不在各表下重复)",
                [table("%s-method" % pre, "方法与口径 / 公式 / 来源(按所属模块)",
                       ["所属模块", "口径 / 公式 / 来源"], moved,
                       source_label="contract §2 L95: 例行计算与来源注释集中; 表下只留结论与洞察",
                       source_ref="本报告")],
                notes=["表下只保留**结论与洞察**; 上表集中列出被迁移的例行口径、公式与来源(共 %d 条)。"
                       % len(moved)]))
    return report


def _write_log(out_dir: Path, inputs: dict, report: dict, meta: dict, html_path: Path | None) -> Path:
    lines = ["整合市场报告(HTML) 构建日志", "=" * 60,
             "生成时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "类目: %s" % report["title"], "", "【输入(三份缺一不可)】"]
    for step, v in sorted(inputs.items()):
        p = Path(v["path"])
        if p.is_file():
            st = p.stat()
            lines.append("  步骤%-2d %-16s %s" % (step, v["name"], p))
            lines.append("        %s  %s  %d 字节" % ("显式指定" if v.get("explicit") else "自动定位",
                                                    datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                                                    st.st_size))
        else:
            lines.append("  步骤%-2d %-16s (缺失!) %s" % (step, v["name"], p))
    lines += ["", "【产物】"]
    for s in report["sheets"]:
        n = sum(len(sec["tables"]) for sec in s["sections"])
        lines.append("  %-8s sections=%d tables=%d kpis=%d" % (s["name"], len(s["sections"]), n, len(s["kpis"])))
    lines += ["", "【输出】", "  HTML : %s" % (html_path or "(未生成)"),
              "  JSON : %s" % (out_dir / "report.json"), "",
              "【口径】变体口径(变体月销量/变体月销额); 价格段=choose_price_bands 动态推导; "
              "功能三态用 is_positive(未知≠否); 缺失不补 0。",
              "【规模】有效变体 %s 个; VOC 问题 %s 条; VOC 证据 %s 条。"
              % (meta.get("valid_variants"), meta.get("voc_issues"), meta.get("voc_evidence"))]
    p = out_dir / "构建日志.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # type: ignore[attr-defined]
    except Exception:                                # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(
        prog="build_integrated_report_html.py",
        description="整合市场报告(HTML): 步骤8 关键词 + 步骤18 标准化交付 + 外部VOC, 三份缺一不可")
    ap.add_argument("--cat", required=True, help='类目名(如 "Dog Dryer-US-202608")')
    ap.add_argument("--data-root", default="data", help="数据根目录(默认 data)")
    ap.add_argument("--keyword", default=None, help="显式指定步骤8 关键词报告")
    ap.add_argument("--product", default=None, help="显式指定步骤18 属性标注-标准化交付")
    ap.add_argument("--voc", default=None, help="显式指定外部VOC报告")
    ap.add_argument("--out-dir", default=None, help="输出目录(默认 data/<CAT>/20_整合市场报告/html)")
    ap.add_argument("--check-only", action="store_true", help="只做门槛检查, 不生成")
    ap.add_argument("--no-html", action="store_true", help="只产 report.json, 不产 HTML")
    args = ap.parse_args(argv)

    inputs = locate_inputs(args.cat, args.data_root, args.keyword, args.product, args.voc)
    missing = check_gate(inputs)
    print_gate(inputs, missing)
    if missing:
        return 2
    if args.check_only:
        return 0

    out_dir = (Path(args.out_dir) if args.out_dir
               else Path(args.data_root) / args.cat / OUT_DIR / HTML_SUBDIR)
    prog = progress_path(out_dir)
    _progress(prog, "检查三份输入", 8)
    try:
        report, meta = build(args.cat, args.data_root, args.product, args.voc,
                             progress=lambda phase, pct: _progress(prog, phase, pct))
        _progress(prog, "契约自检", 88)
        validate_report(report)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
        html_path = None
        if not args.no_html:
            _progress(prog, "渲染单文件 HTML", 95)
            html_path = out_dir / "index.html"
            _render_html(report, html_path)
        log = _write_log(out_dir, inputs, report, meta, html_path)
    except Exception as exc:                         # noqa: BLE001
        _progress(prog, "生成失败", 100, done=True, error="%s: %s" % (type(exc).__name__, exc))
        print(f"\n✗ 生成失败: {type(exc).__name__}: {exc}")
        return 1
    _progress(prog, "完成", 100, done=True)

    print("-" * 96)
    for s in report["sheets"]:
        n = sum(len(sec["tables"]) for sec in s["sections"])
        print(f"  {s['name']:<8} sections={len(s['sections'])}  tables={n}  kpis={len(s['kpis'])}")
    print("-" * 96)
    if html_path:
        print(f"✓ 整合报告(HTML): {html_path}  ({html_path.stat().st_size:,} 字节, 双击即看)")
    print(f"✓ 契约 JSON     : {out_dir / 'report.json'}")
    print(f"✓ 构建日志      : {log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
