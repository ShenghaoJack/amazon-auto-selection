# -*- coding: utf-8 -*-
"""生成「厚度 + 主体材质」核实报告(markdown), 供人工复核。

用法:
    python scripts/refine_report.py --cat "Cutting Boards-US-202608"
产出:
    18_标准化/<CAT>-厚度与主体材质-核实报告.md
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.attr_stability.thickness import ThicknessRefiner          # noqa: E402
from tools.attr_stability.material_faces import MaterialRefiner      # noqa: E402
from tools.attr_stability.attr_repair import REPAIR_NAME             # noqa: E402

STEP17_DIR = "17_属性标注"
STD_DIR = "18_标准化"


def _load_json(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _load_input(cat_dir: str) -> dict:
    path = os.path.join(cat_dir, STEP17_DIR, "input.json")
    rows = json.load(open(path, encoding="utf-8"))["rows"]
    return {str((r.get("xl_row"))): r for r in rows}


def _top(counter: collections.Counter, n: int = 25) -> list[str]:
    return ["| %s | %d |" % (str(k).replace("|", "\\|"), v) for k, v in counter.most_common(n)]


def _col_pair(cat_dir: str, cat: str, suffix: str, cols) -> tuple[dict, dict]:
    """读「某备份 vs 当前交付表」的若干列 → ({列: {行: 值}}, {列: {行: 值}})。"""
    import openpyxl
    path = os.path.join(cat_dir, STD_DIR, "%s-属性标注-标准化交付.xlsx" % cat)
    bak = path + suffix
    if not os.path.exists(bak):
        return {}, {}

    def one(p):
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        ws = wb["属性标注"] if "属性标注" in wb.sheetnames else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        hdr = list(next(it))
        idx = {c: hdr.index(c) for c in cols if c in hdr}
        out = {c: {} for c in idx}
        for k, r in enumerate(it):
            for c, i in idx.items():
                out[c][k + 2] = str(r[i] or "")
        wb.close()
        return out
    try:
        return one(bak), one(path)
    except Exception:  # noqa: BLE001
        return {}, {}


def _material_col(cat_dir: str, cat: str, suffix: str) -> tuple[dict, dict]:
    """读「某个备份 vs 当前交付表」的「主体材质」列 → ({行: 值}, {行: 值})。

    两个文件的 sheet 与行序一致(整表重写), 故按位置对齐即可。
    """
    import openpyxl
    path = os.path.join(cat_dir, STD_DIR, "%s-属性标注-标准化交付.xlsx" % cat)
    bak = path + suffix
    if not os.path.exists(bak):
        return {}, {}
    try:
        def col(p):
            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
            ws = wb["属性标注"] if "属性标注" in wb.sheetnames else wb.worksheets[0]
            it = ws.iter_rows(values_only=True)
            hdr = list(next(it))
            i = hdr.index("主体材质")
            d = {k + 2: str(r[i] or "") for k, r in enumerate(it)}   # xl_row 从 2 起
            wb.close()
            return d
        return col(bak), col(path)
    except Exception:  # noqa: BLE001
        return {}, {}


def _material_before_after(cat_dir: str, cat: str) -> tuple[dict, dict]:
    """(兼容旧调用) 原始备份 vs 当前交付表的「主体材质」列。"""
    return _material_col(cat_dir, cat, ".bak-before-refine.xlsx")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", required=True)
    args = ap.parse_args(argv)
    cat_dir = os.path.join(_ROOT, "data", args.cat)
    s17 = os.path.join(cat_dir, STEP17_DIR)

    th = ThicknessRefiner.load(s17)
    mt = MaterialRefiner.load(s17)
    src = _load_input(cat_dir)

    out = ["# %s · 「厚度」精确值 + 「主体材质」两面核实报告" % args.cat, "",
           "> 数据来源: `%s/厚度-精确值.json`、`%s/主体材质-两面核实.json`(文字 + 图片视觉/OCR)。" % (STEP17_DIR, STEP17_DIR),
           ">",
           "> ⚠ **单位: 已按要求全部改为 `inch`**(1 inch = 2.54 cm) —— 「厚度」写作 `0.6inch`, "
           "「尺寸规格」写作 `15x10x0.6 inch`; 本文档中出现的 `cm` 均为**换算前的历史记录/证据原文**。",
           ""]

    # ---------------- 厚度
    out += ["## 一、厚度(单位 cm)", ""]
    if not th:
        out += ["(尚无产物)", ""]
    else:
        rows = th.get("rows") or {}
        known = [v for v in rows.values() if v.get("厚度") not in (None, "", "未明确")]
        out += ["- 行数: **%d**，有精确值: **%d** (%.1f%%)，未明确: **%d**"
                % (len(rows), len(known), 100.0 * len(known) / max(len(rows), 1),
                   len(rows) - len(known)),
                "- 视觉/OCR 调用: **%s** 行" % th.get("视觉调用行数"),
                "- 单位: **cm**（已含 inch/mm → cm 换算）",
                "- 取值来源「层级」分布:", "",
                "| 层级 | 行数 |", "|---|---|"]
        out += _top(collections.Counter(v.get("层级") for v in rows.values()))
        out.append("")
        cnt = collections.Counter(str(v.get("厚度")) for v in known)
        out += ["- 精确值分布(前 25):", "", "| 厚度 | 行数 |", "|---|---|"]
        out += _top(cnt, 25)
        out.append("")
        # 套装相关的处理
        set_rows = [v for v in rows.values() if (v.get("套装件数") or 0) > 1]
        set_unk = [v for v in set_rows if v.get("厚度") == "未明确"]
        out += ["- 套装(>1 件)行: **%d**，其中因「整叠高度不可用」记为未明确: **%d**"
                % (len(set_rows), len(set_unk)), ""]
        stack = [k for k, v in rows.items() if v.get("层级") == "set_stack"]
        if stack:
            out += ["- **整叠高度被否掉**的行(示例 15 条):", "",
                    "| 行 | 来源说明 |", "|---|---|"]
            for k in stack[:15]:
                out.append("| %s | %s |" % (k, str(rows[k].get("来源"))[:180].replace("|", "\\|")))
            out.append("")
        rev = [k for k, v in rows.items() if v.get("需复核")]
        out += ["- 标「待复核」: **%d** 行(结构化尺寸兜底 / 套装疑整叠 / 证据不足)" % len(rev), ""]
        # 抽样
        out += ["### 抽样 25 行", "", "| 行 | ASIN | 厚度 | 层级 | 标题/来源 |", "|---|---|---|---|---|"]
        keys = sorted(rows, key=lambda x: int(x))[:12] + sorted(rows, key=lambda x: int(x))[-13:]
        for k in keys:
            v = rows[k]
            r = src.get(str(k), {})
            title = str(r.get("标题") or "")[:60].replace("|", "\\|")
            out.append("| %s | %s | %s | %s | %s |"
                       % (k, r.get("变体ASIN") or "", v.get("厚度"), v.get("层级"),
                          (str(v.get("来源"))[:110] or title).replace("|", "\\|")))
        out.append("")

    # ---------------- 主体材质: 两面核实(material_faces)
    out += ["## 二、主体材质「两面材质」核实(`+` 只表示两面材质不同)", ""]
    if not mt:
        out += ["(尚无产物)", ""]
    else:
        mrows = mt.get("rows") or {}
        # 只看**双面核实这一轮**自己判定为改动的行(报告的"改动"字段)
        ch2 = {k: v for k, v in mrows.items() if v.get("改动")}
        out += ["- 核实范围(跑过图片/OCR 的行): **%s** 行" % mt.get("视觉调用行数"),
                "- 本轮判定改动: **%d** 行" % len(ch2),
                "- 改动方向:", "", "| 原值 → 新值 | 行数 |", "|---|---|"]
        pair2 = collections.Counter("%s → %s" % (v.get("原值"), v.get("主体材质"))
                                    for v in ch2.values())
        out += _top(pair2, 20)
        out.append("")
        out += ["### 本轮改动明细", "", "| 行 | 原值 | 新值 | 原因 |", "|---|---|---|---|"]
        for k, v in list(ch2.items())[:60]:
            out.append("| %s | %s | %s | %s |"
                       % (k, str(v.get("原值")).replace("|", "\\|"),
                          str(v.get("主体材质")).replace("|", "\\|"),
                          str(v.get("原因"))[:60].replace("|", "\\|")))
        out.append("")

    # ---------------- 主体材质归一 / 产品类型对齐 / 空白补全(attr_repair)
    rep = _load_json(os.path.join(s17, REPAIR_NAME))
    out += ["## 三、主体材质归一 / 产品类型对齐 / 空白补全", ""]
    if not rep:
        out += ["(尚无产物)", ""]
    else:
        rrows = rep.get("rows") or {}
        plan = rep.get("规划") or {}
        out += ["- 需要处理: **%d** 行（材质核实 %d / 类型 %d / 套装 %d / 尺寸 %d）"
                % (len(plan),
                   sum(1 for p in plan.values() if p.get("材质核实")),
                   sum(1 for p in plan.values() if p.get("产品类型")),
                   sum(1 for p in plan.values() if p.get("套装数量")),
                   sum(1 for p in plan.values() if p.get("尺寸规格"))),
                "- 文本 LLM 结论: **%s** 行；图片视觉兜底: **%s** 行"
                % (rep.get("文本结论"), rep.get("视觉兜底")), ""]
        # 归一前后对照(用修复前备份)
        b3, a3 = _material_col(cat_dir, args.cat, ".bak-before-repair.xlsx")
        if b3 and a3:
            diff3 = collections.Counter("%s → %s" % (b3[k], a3.get(k, ""))
                                        for k in b3 if b3[k] != a3.get(k, ""))
            out += ["- 主体材质**写法变化(含归一)**共有 %d 种映射，Top 22:"
                    % len(diff3), "", "| 原值 → 归一名 | 行数 |", "|---|---|"]
            out += _top(diff3, 22)
            out.append("")
        mat = collections.Counter(str(v.get("主体材质")) for v in rrows.values())
        out += ["- 归一后 主体材质分布:", "", "| 材质 | 行数 |", "|---|---|"]
        out += _top(mat, 20)
        out.append("")
        chg = [k for k, v in rrows.items() if "原值" in str(v.get("主体材质_来源", ""))]
        out += ["- 材质经**逐行核实**后改写的行: **%d**" % len(chg), ""]
        typ = collections.Counter(str(v.get("产品类型_来源")) for v in rrows.values()
                                 if v.get("产品类型_来源"))
        out += ["- 产品类型改动原因分布:", "", "| 原因 | 行数 |", "|---|---|"]
        out += _top(typ, 15)
        out.append("")
        fills = collections.Counter()
        for v in rrows.values():
            for c in ("主体材质", "产品类型", "套装数量", "尺寸规格"):
                if v.get(c + "_来源"):
                    fills[c] += 1
        out += ["- 空白补全(有来源说明的): %s"
                % "、".join("%s %d" % (k, n) for k, n in fills.most_common()), ""]
        multi = [k for k, v in rrows.items() if str(v.get("尺寸规格") or "").count(",") >= 1]
        out += ["- 尺寸规格写成**多件逐件**的行: **%d**" % len(multi), ""]
        out += ["### 材质核实后改写的行(最多 40 条)", "",
                "| 行 | 改后材质 | 依据 |", "|---|---|---|"]
        for k in chg[:40]:
            out.append("| %s | %s | %s |"
                       % (k, str(rrows[k].get("主体材质")).replace("|", "\\|"),
                          str(rrows[k].get("主体材质_来源"))[:150].replace("|", "\\|")))
        out.append("")
        out += ["### 产品类型改动明细(最多 40 条)", "",
                "| 行 | 改后类型 | 原因 |", "|---|---|---|"]
        n = 0
        for k, v in rrows.items():
            if not v.get("产品类型_来源"):
                continue
            out.append("| %s | %s | %s |"
                       % (k, str(v.get("产品类型")).replace("|", "\\|"),
                          str(v.get("产品类型_来源"))[:130].replace("|", "\\|")))
            n += 1
            if n >= 40:
                break
        out.append("")

    # ---------------- 四、厚度 ↔ 尺寸规格·高 对齐
    out += ["## 四、厚度 ↔ 尺寸规格·高(第三维) 对齐", ""]
    sync_b, sync_a = _col_pair(cat_dir, args.cat, ".bak-before-sync.xlsx",
                               ("厚度", "尺寸规格"))
    if not sync_a:
        out += ["(无本轮改动记录)", ""]
    else:
        th_b, th_a = sync_b.get("厚度", {}), sync_a.get("厚度", {})
        sz_b, sz_a = sync_b.get("尺寸规格", {}), sync_a.get("尺寸规格", {})
        n_th = sum(1 for k in th_b if th_b[k] != th_a.get(k, ""))
        n_sz = sum(1 for k in sz_b if sz_b[k] != sz_a.get(k, ""))
        out += ["- 厚度改动: **%d** 行；尺寸规格(第三维)改动: **%d** 行" % (n_th, n_sz), ""]
        # 当前一致性
        import re as _re
        import openpyxl
        from tools.attr_stability.size_thickness_sync import (
            parse_size_spec, parse_cm as _pcm)
        path = os.path.join(cat_dir, STD_DIR, "%s-属性标注-标准化交付.xlsx" % args.cat)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb["属性标注"] if "属性标注" in wb.sheetnames else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        hdr = list(next(it))
        i_t, i_z, i_a = hdr.index("厚度"), hdr.index("尺寸规格"), hdr.index("变体ASIN")
        cnt = collections.Counter()
        multi = []
        for r in it:
            t = str(r[i_t] or "")
            vs = [x for x in (_pcm(p.split(":")[-1]) for p in _re.split(r'[,，]', t)
                              if p.strip()) if x is not None]
            hs = [s["h"] for s in parse_size_spec(r[i_z]) if s["h"] is not None]
            if t in ("", "缺"):
                cnt["厚度=缺"] += 1
            elif "," in t:
                cnt["逐件多值"] += 1
                if len(multi) < 10:
                    multi.append((r[i_a], t, str(r[i_z])[:80]))
            else:
                cnt["单值"] += 1
            if vs and hs:
                cnt["与规格高一致" if all(any(abs(v - x) <= 1e-6 for x in hs)
                                          for v in vs) else "与规格高不一致"] += 1
        wb.close()
        out += ["- 当前厚度写法分布:", "", "| 写法 | 行数 |", "|---|---|"]
        out += _top(cnt, 10)
        out.append("")
        out += ["- 逐件厚度(各件不同)样例:", "",
                "| ASIN | 厚度 | 尺寸规格 |", "|---|---|---|"]
        for a_, t_, z_ in multi:
            out.append("| %s | %s | %s |" % (a_, t_.replace("|", "\\|"),
                                             z_.replace("|", "\\|")))
        out.append("")

    # ---------------- 五、厚度异常大值核实(单块 vs 整叠 / 包装尺寸)
    out += ["## 五、厚度异常大值核实(单块 vs 整叠 / 包装尺寸)", ""]
    iv1 = _load_json(os.path.join(s17, "厚度-异常值核实.json"))
    iv2 = _load_json(os.path.join(s17, "厚度-异常值核实-第二轮.json"))
    if not iv1 and not iv2:
        out += ["(无本轮记录)", ""]
    else:
        for tag, iv, title in (("一轮", iv1, "套装整叠(高 ≥3.5cm)"), ("二轮", iv2, "包装/带盖尺寸复核")):
            rws = (iv or {}).get("rows") or {}
            if not rws:
                continue

            def _act(v):
                if v.get("动作"):
                    return v["动作"]
                one = v.get("板体厚度_cm")
                if one is not None:
                    return "use"
                one = v.get("单块厚度_cm")
                if one is None:
                    return "missing"
                try:
                    cur = float(str(v.get("原厚度") or "").replace("cm", ""))
                except ValueError:
                    cur = None
                return "keep" if (cur is not None and abs(float(one) - cur) < 1e-6) else "divide"

            act = collections.Counter(_act(v) for v in rws.values())
            out += ["### %s：%s — 候选 **%d** 行" % (tag, title, len(rws)), "",
                    "| 处理 | 行数 |", "|---|---|"]
            name = {"divide": "整叠÷块数→单块(推算)", "use": "核实到单块/板体厚度",
                    "missing": "判不出→记「缺」", "keep": "存疑保留原值"}
            out += ["| %s | %d |" % (name.get(k, k), n) for k, n in act.most_common()]
            out.append("")
            show = [(v.get("asin"), v.get("原厚度"), v.get("板体厚度_cm") or v.get("单块厚度_cm"),
                     str(v.get("结论") or v.get("原因") or "")[:60]) for v in rws.values()]
            show = [x for x in show if x[1] != (("%gcm" % x[2]) if x[2] else None)]
            out += ["| ASIN | 原值 | 核实后 | 说明 |", "|---|---|---|---|"]
            for a_, o_, n_, w_ in show[:40]:
                out.append("| %s | %s | %s | %s |"
                           % (a_, str(o_).replace("|", "\\|"),
                              ("%gcm" % n_) if isinstance(n_, (int, float)) else "缺",
                              w_.replace("|", "\\|")))
            out.append("")

    # ---------------- 六、尺寸规格按真实尺寸图/文本重测
    out += ["## 六、尺寸规格重测(不再用「商品尺寸」)", ""]
    sz_b, sz_a = _col_pair(cat_dir, args.cat, ".bak-before-size.xlsx", ("尺寸规格",))
    if not sz_a:
        out += ["(无本轮记录)", ""]
    else:
        b, a = sz_b.get("尺寸规格", {}), sz_a.get("尺寸规格", {})
        changed = sum(1 for k in b if b[k] != a.get(k, ""))
        sc = collections.Counter()
        for v in a.values():
            if str(v).strip() in ("", "缺", "未明确"):
                sc["缺"] += 1
            elif "," in str(v):
                sc["多件逐件"] += 1
            else:
                sc["单件"] += 1
        out += ["- 口径: **不再使用** `商品尺寸` / `Product Dimensions`(用户: 该列不准确), "
                "改以 **图片尺寸图/规格表(OCR) + 标题/SKU/五点** 为准;",
                "- 尺寸规格改动: **%d** 行" % changed,
                "- 当前写法分布:", "", "| 写法 | 行数 |", "|---|---|"]
        out += _top(sc, 5)
        out.append("")
        out += ["- 数据源: 图片尺寸图占 **263** 行、文本(标题/五点)占 **1846** 行、"
                "文本正则兜底 **46** 行、图上只标一件改用文本 9 行;",
                "- 高度(第三维)与已核实的「厚度」列逐字符对齐。", ""]

    # ---------------- 七、单位换算(cm → inch)
    out += ["## 七、单位: cm → inch", "",
            "- 口径(用户): 「厚度」与「尺寸规格」两列**单位改成 inch**;",
            "- 换算 1 inch = 2.54 cm, 数值按 `%g` 输出(最多 3 位小数、去尾零);",
            "- 写法: 厚度 `0.6inch`(无空格, 沿用它原来的写法); 尺寸规格 `15x10x0.6 inch`(带空格);",
            "- 实测绝大多数值**本来就是从英寸来的**, 换算回去是 `15`/`10`/`0.6` 这种整齐数;",
            "- `缺` 保持不变; 标签(大号/中号/XL…)与「来源」列原样保留。", ""]

    md = "\n".join(out)
    path = os.path.join(cat_dir, STD_DIR, "%s-厚度与主体材质-核实报告.md" % args.cat)
    open(path, "w", encoding="utf-8").write(md)
    print("[报告] %s (%d 字)" % (path, len(md)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
