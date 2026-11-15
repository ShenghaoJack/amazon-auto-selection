# -*- coding: utf-8 -*-
"""汇总「属性标注-标准化交付」中仍然存疑/待人工核对的遗留项 ASIN 清单。

只读交付表, 不改动任何数据; 输出 xlsx + md。

分类:
  S1a 单件却列多件    套装数量=1件, 但尺寸规格列了多件(件数与规格直接矛盾)
  S1b 多件却列1件     套装数量>=2件, 但尺寸规格只列 1 件(可能同尺寸套装, 也可能漏列)
  S1c 件数与规格不符  套装数量>=2 且 规格件数>=2, 但两者不相等
  S2  厚高不一致      厚度 与 尺寸规格里的高 对不上(按去重值比较)
  S3  厚度缺          厚度 = 缺
  S4  尺寸规格缺      尺寸规格 = 缺
  S5  材质未明确      主体材质 为「未明确」或空
  S6  套装数量未明确  套装数量 为「未明确」或空
  S7  低置信来源      尺寸规格 来源 = 文本正则兜底 / 图上只标一件(改用文本)
  S8  厚度第二轮核实  曾经进入厚度异常值第二轮核实(已处理, 供复核)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re

import openpyxl

STD = "18_标准化"
CAT = "Cutting Boards-US-202608"
MISSING = "缺"

RE_ENTRY = re.compile(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)(?:\s*x\s*(\d+(?:\.\d+)?|缺))?")
RE_NUM = re.compile(r"(\d+)")

CATS = [
    ("S1a", "单件却列多件"),
    ("S1b", "多件却列1件"),
    ("S1c", "件数与规格不符"),
    ("S2", "厚高不一致"),
    ("S3", "厚度缺"),
    ("S4", "尺寸规格缺"),
    ("S5", "材质未明确"),
    ("S6", "套装数量未明确"),
    ("S7", "低置信来源"),
    ("S8", "厚度第二轮核实"),
]

COLS = ["xl_row", "变体ASIN", "父ASIN", "主体材质", "产品类型", "套装数量",
        "厚度", "尺寸规格", "变体月销额", "问题说明", "标题"]


def spec_entries(val: str) -> list[dict]:
    """解析尺寸规格。段内若含「:」则只取冒号后的部分, 避免把标签里的
    `16x12x0.8 in:16x12x1.5 inch` 之类的前半段误当成一个尺寸条目。"""
    out = []
    for seg in re.split(r"[,，]", val or ""):
        seg = seg.strip()
        if not seg or seg == MISSING:
            continue
        body = seg.split(":")[-1] if ":" in seg else seg
        for m in RE_ENTRY.finditer(body):
            out.append({"长": float(m.group(1)), "宽": float(m.group(2)),
                        "高": None if (m.group(3) in (None, "", MISSING)) else float(m.group(3))})
    return out


def thick_values(val: str) -> list[float]:
    out = []
    for part in re.split(r"[,，]", val or ""):
        part = part.strip()
        if not part or part == MISSING:
            continue
        part = part.split(":")[-1].replace("inch", "").strip()
        m = re.search(r"(\d+(?:\.\d+)?)", part)
        if m:
            out.append(float(m.group(1)))
    return out


def set_count(val: str):
    s = str(val or "").strip()
    if not s or s in ("未明确", MISSING):
        return None
    m = RE_NUM.search(s)
    return int(m.group(1)) if m else None


def num(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except Exception:
        return 0.0


def load(cat_dir: str) -> list[dict]:
    """载入标准化交付物。

    2026-10-09 修: 原实现用模块常量 CAT 拼文件名 → 任何 cat_dir 都会去找
    "Cutting Boards-US-202608-属性标注-标准化交付.xlsx", 非砧板类目直接
    FileNotFoundError(与 audit_spec_order.load 同类问题)。现按目录内匹配。
    """
    std_dir = os.path.join(cat_dir, STD)
    cands = []
    if os.path.isdir(std_dir):
        cands = [os.path.join(std_dir, f) for f in sorted(os.listdir(std_dir))
                 if f.endswith(".xlsx") and "标准化交付" in f]
    path = cands[0] if cands else os.path.join(std_dir, "%s-属性标注-标准化交付.xlsx" % CAT)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    names = wb.sheetnames
    main = "属性标注" if "属性标注" in names else names[0]
    src = "来源" if "来源" in names else ""
    m_rows = [list(r) for r in wb[main].iter_rows(values_only=True)]
    s_rows = [list(r) for r in wb[src].iter_rows(values_only=True)] if src else []
    wb.close()

    hdr = [str(h) if h is not None else "" for h in m_rows[0]]
    ix = {h: i for i, h in enumerate(hdr)}
    i_sz_src = None
    if s_rows:
        shdr = [str(h) if h is not None else "" for h in s_rows[0]]
        if "尺寸规格" in shdr:
            i_sz_src = shdr.index("尺寸规格")

    rows = []
    for k, r in enumerate(m_rows[1:], start=2):
        d = {
            "xl_row": k,
            "父ASIN": str(r[ix["父ASIN"]] or ""),
            "变体ASIN": str(r[ix["变体ASIN"]] or ""),
            "标题": str(r[ix["标题"]] or ""),
            "主体材质": str(r[ix["主体材质"]] or "").strip(),
            "产品类型": str(r[ix["产品类型"]] or "").strip(),
            "套装数量": str(r[ix["套装数量"]] or "").strip(),
            "厚度": str(r[ix["厚度"]] or "").strip(),
            "尺寸规格": str(r[ix["尺寸规格"]] or "").strip(),
            "变体月销额": num(r[ix["变体月销额"]]),
            "尺寸规格来源": str(s_rows[k - 1][i_sz_src] or "") if (i_sz_src is not None and k - 1 < len(s_rows)) else "",
        }
        rows.append(d)
    return rows


def classify(rows: list[dict], cat_dir: str) -> dict[str, list[dict]]:
    hits: dict[str, list[dict]] = {code: [] for code, _ in CATS}

    for d in rows:
        n = set_count(d["套装数量"])
        ents = spec_entries(d["尺寸规格"])
        k = len(ents)

        # S1 件数 vs 尺寸规格件数
        if n is not None and k:
            if n == 1 and k > 1:
                d1 = dict(d)
                d1["问题说明"] = "套装数量=1件, 但尺寸规格列了 %d 件" % k
                hits["S1a"].append(d1)
            elif n >= 2 and k == 1:
                d1 = dict(d)
                d1["问题说明"] = "套装数量=%d件, 但尺寸规格只列 1 件(需确认是同尺寸套装还是漏列)" % n
                hits["S1b"].append(d1)
            elif n >= 2 and k >= 2 and n != k:
                d1 = dict(d)
                d1["问题说明"] = "套装数量=%d件, 尺寸规格列 %d 件" % (n, k)
                hits["S1c"].append(d1)

        # S2 厚度 vs 尺寸规格·高(同厚只写一个值属正常, 故按去重值比较)
        th = thick_values(d["厚度"])
        hs = [e["高"] for e in ents if e["高"] is not None]
        if th and hs:
            TOL = 0.021  # inch; 吸收 cm→inch 各自取整带来的 ±0.01 误差

            def uniq(vs, tol=TOL):
                out = []
                for v in sorted(vs):
                    if not out or abs(v - out[-1]) > tol:
                        out.append(v)
                return out

            ua, ub = uniq(th), uniq(hs)
            same = len(ua) == len(ub) and all(abs(a - b) <= TOL for a, b in zip(ua, ub))
            if not same:
                d2 = dict(d)
                d2["问题说明"] = "厚度=%s, 尺寸规格高=%s" % (
                    "/".join("%g" % v for v in ua), "/".join("%g" % v for v in ub) or "缺")
                hits["S2"].append(d2)

        # S3 / S4 缺
        if d["厚度"] == MISSING:
            d3 = dict(d)
            d3["问题说明"] = "厚度记为「缺」(文本/图片均无厚度标注)"
            hits["S3"].append(d3)
        if d["尺寸规格"] == MISSING:
            d4 = dict(d)
            d4["问题说明"] = d["尺寸规格来源"] or "尺寸规格记为「缺」"
            hits["S4"].append(d4)

        # S5 / S6
        if not d["主体材质"] or d["主体材质"] == "未明确":
            d5 = dict(d)
            d5["问题说明"] = "主体材质=%s" % (d["主体材质"] or "(空)")
            hits["S5"].append(d5)
        if not d["套装数量"] or d["套装数量"] == "未明确":
            d6 = dict(d)
            d6["问题说明"] = "套装数量=%s" % (d["套装数量"] or "(空)")
            hits["S6"].append(d6)

        # S7 低置信来源
        s = d["尺寸规格来源"]
        if "正则兜底" in s or "图上只标一件" in s:
            d7 = dict(d)
            d7["问题说明"] = s[:200]
            hits["S7"].append(d7)

    # S8 厚度第二轮核实过的行
    p2 = os.path.join(cat_dir, "17_属性标注", "厚度-异常值核实-第二轮.json")
    if os.path.exists(p2):
        o = json.load(io.open(p2, encoding="utf-8"))
        by_xl = {d["xl_row"]: d for d in rows}
        for key, rec in (o.get("rows") or {}).items():
            xl = rec.get("xl_row") or int(key)
            d = by_xl.get(int(xl))
            if not d:
                continue
            d8 = dict(d)
            d8["问题说明"] = "第二轮核实(%s): 原厚度=%s → 最终厚度=%s" % (
                rec.get("原因") or "厚度异常", rec.get("原厚度"), d["厚度"])
            hits["S8"].append(d8)

    for code in hits:
        hits[code].sort(key=lambda x: -x["变体月销额"])
    return hits


def write_xlsx(hits: dict, out_path: str) -> None:
    wb = openpyxl.Workbook()
    first = wb.active
    first.title = "汇总"
    first.append(["分类", "说明", "行数"])
    for code, name in CATS:
        first.append(["%s %s" % (code, name), "", len(hits[code])])
    all_rows = {}
    for code, name in CATS:
        for d in hits[code]:
            r = all_rows.setdefault(d["xl_row"], dict(d))
            prev = r.get("问题说明", "")
            tag = "%s %s" % (code, name)
            r["问题说明"] = (prev + "; " + tag) if prev and tag not in prev else (prev or tag)
    first.append(["合计(去重)", "至少命中一类问题的行数", len(all_rows)])
    first.append([])
    first.append(["附: 各类问题(去重后按 ASIN 合并, 见「全量遗留ASIN」)"])

    for code, name in CATS:
        ws = wb.create_sheet("%s %s" % (code, name))
        ws.append(COLS)
        for d in hits[code]:
            ws.append([d.get(c, "") for c in COLS])

    ws = wb.create_sheet("全量遗留ASIN")
    ws.append(["问题分类"] + COLS)
    for xl in sorted(all_rows, key=lambda x: -all_rows[x]["变体月销额"]):
        d = all_rows[xl]
        types = []
        for code, name in CATS:
            if any(h["xl_row"] == xl for h in hits[code]):
                types.append("%s %s" % (code, name))
        ws.append(["; ".join(types)] + [d.get(c, "") for c in COLS])

    wb.save(out_path)


def write_md(hits: dict, out_path: str) -> None:
    lines = ["# %s · 遗留项 ASIN 清单(待人工核对)" % CAT, ""]
    lines.append("数据源: `%s/%s/%s-属性标注-标准化交付.xlsx`(只读); 按分类逐行列出, 组内按**变体月销额**降序。" % ("data", STD, CAT))
    lines.append("")
    lines.append("| 分类 | 含义 | 行数 |")
    lines.append("|---|---|---|")
    meaning = {
        "S1a": "套装数量=1件, 但尺寸规格列了多件(件数与规格直接矛盾)",
        "S1b": "套装数量>=2件, 但尺寸规格只列1件(需确认同尺寸套装 or 漏列)",
        "S1c": "套装数量与规格件数都>=2 但不相等",
        "S2": "厚度 与 尺寸规格里的高 对不上",
        "S3": "厚度 = 缺",
        "S4": "尺寸规格 = 缺",
        "S5": "主体材质 = 未明确/空",
        "S6": "套装数量 = 未明确/空",
        "S7": "尺寸规格来源为文本正则兜底 / 图上只标一件(改用文本)",
        "S8": "曾进入厚度异常值第二轮核实(已处理, 供复核)",
    }
    for code, name in CATS:
        lines.append("| %s %s | %s | %d |" % (code, name, meaning[code], len(hits[code])))
    union = set()
    for code, _ in CATS:
        for d in hits[code]:
            union.add(d["xl_row"])
    lines.append("")
    lines.append("**合计(去重): %d 行**至少命中一类问题(占 2466 行的 %.1f%%); 逐行明细见同名 xlsx 的「全量遗留ASIN」sheet。"
                 % (len(union), 100.0 * len(union) / 2466))
    lines.append("")

    detail = {
        "S1a": ["xl_row", "变体ASIN", "套装数量", "尺寸规格", "变体月销额"],
        "S1b": ["xl_row", "变体ASIN", "套装数量", "厚度", "尺寸规格", "变体月销额"],
        "S1c": ["xl_row", "变体ASIN", "套装数量", "厚度", "尺寸规格", "变体月销额"],
        "S2": ["xl_row", "变体ASIN", "主体材质", "套装数量", "厚度", "尺寸规格", "变体月销额"],
        "S3": ["xl_row", "变体ASIN", "主体材质", "套装数量", "尺寸规格", "变体月销额"],
        "S4": ["xl_row", "变体ASIN", "主体材质", "套装数量", "厚度", "变体月销额"],
        "S5": ["xl_row", "变体ASIN", "产品类型", "套装数量", "厚度", "尺寸规格", "变体月销额"],
        "S6": ["xl_row", "变体ASIN", "主体材质", "厚度", "尺寸规格", "变体月销额"],
        "S7": ["xl_row", "变体ASIN", "主体材质", "套装数量", "尺寸规格", "变体月销额"],
        "S8": ["xl_row", "变体ASIN", "主体材质", "套装数量", "厚度", "尺寸规格", "变体月销额"],
    }
    INLINE_LIMIT = 10 ** 9
    for code, name in CATS:
        rows = hits[code]
        lines.append("## %s %s(%d 行)" % (code, name, len(rows)))
        lines.append("")
        if not rows:
            lines.append("无。")
            lines.append("")
            continue
        cols = detail[code]
        if len(rows) <= INLINE_LIMIT:
            lines.append("| " + " | ".join(cols) + " |")
            lines.append("|" + "---|" * len(cols))
            for d in rows:
                vals = []
                for c in cols:
                    v = d.get(c, "")
                    if c == "变体月销额":
                        v = "{:,.0f}".format(v) if v else ""
                    v = str(v).replace("|", "/")
                    if c == "尺寸规格" and len(v) > 70:
                        v = v[:70] + "…"
                    elif c == "标题" and len(v) > 70:
                        v = v[:70] + "…"
                    vals.append(v)
                lines.append("| " + " | ".join(vals) + " |")
        else:
            lines.append("行数较多(%d), 仅列 ASIN(正文见 xlsx 同名 sheet):" % len(rows))
            lines.append("")
            for d in rows:
                lines.append("- `%s` (xl_row %d, %s, %s)" % (
                    d["变体ASIN"], d["xl_row"], d["主体材质"] or "材质空", d["套装数量"] or "套装空"))
        lines.append("")
    io.open(out_path, "w", encoding="utf-8").write("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat-dir", default=os.path.join("data", CAT))
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    cat_dir = args.cat_dir
    out_dir = args.out_dir or os.path.join(cat_dir, STD)
    rows = load(cat_dir)
    hits = classify(rows, cat_dir)
    xlsx = os.path.join(out_dir, "%s-遗留项ASIN清单.xlsx" % CAT)
    md = os.path.join(out_dir, "%s-遗留项ASIN清单.md" % CAT)
    write_xlsx(hits, xlsx)
    write_md(hits, md)
    print("行数 %d" % len(rows))
    for code, name in CATS:
        print("  %s %-10s %d" % (code, name, len(hits[code])))
    print("[写] %s" % xlsx)
    print("[写] %s" % md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
