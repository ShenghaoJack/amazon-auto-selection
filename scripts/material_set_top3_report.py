# -*- coding: utf-8 -*-
"""统计报告: 「主体材质 × 套装数量」分组下, 变体月销额 Top3 的**尺寸规格**。

用法
====
    python scripts/material_set_top3_report.py --cat "Cutting Boards-US-202608"

产出(写入 `18_标准化/`)
======================
· `<CAT>-材质x套装-变体月销额Top3-尺寸规格报告.md`  —— 人读版
· `<CAT>-材质x套装-变体月销额Top3-尺寸规格.xlsx`  —— `Top3明细` + `分组汇总` + `尺寸规格频次` 三个 sheet

口径
====
· 分组: **主体材质 × 套装数量**(两个属性列的原值); 组内按 **变体月销额** 降序取前 3;
· 尺寸规格/厚度单位均为 **inch**(交付表现行单位);
· 「主尺寸」= 该行尺寸里**面积最大那一件**的 `长x宽`(多件套看最大件), 用于把不同件数的写法归一到同一口径。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

STD = "18_标准化"
MISSING = ("", "缺", "未明确", "None")


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_spec(v) -> dict:
    """解析尺寸规格(单位 inch) → `{段:[{label,l,w,h}], 件数, 主尺寸, 全部长宽}`。"""
    s = str(v or "").strip()
    segs = []
    if s and s not in MISSING:
        for part in re.split(r'[,，]', s):
            part = part.strip()
            if not part:
                continue
            lab, _, body = part.partition(":")
            if not _:
                lab, body = "", part
            nums = re.findall(r'(\d+(?:\.\d+)?)', body)
            if len(nums) >= 2:
                segs.append({"label": lab.strip(),
                             "l": float(nums[0]), "w": float(nums[1]),
                             "h": float(nums[2]) if len(nums) >= 3 else None})
    areas = [x["l"] * x["w"] for x in segs]
    main = ""
    if segs:
        big = segs[areas.index(max(areas))]
        main = "%gx%g" % (big["l"], big["w"])
    return {"段": segs, "件数": len(segs), "主尺寸": main,
            "全部": ",".join("%gx%g" % (x["l"], x["w"]) for x in segs)}


def load(cat_dir: str, cat: str):
    import openpyxl
    path = os.path.join(cat_dir, STD, "%s-属性标注-标准化交付.xlsx" % cat)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["属性标注"] if "属性标注" in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    hdr = rows[0]

    def i(name, default=None):
        return hdr.index(name) if name in hdr else default

    cols = {"材质": i("主体材质"), "套数": i("套装数量"), "销额": i("变体月销额"),
            "销量": i("变体月销量"), "规格": i("尺寸规格"), "厚度": i("厚度"),
            "asin": i("变体ASIN"), "父": i("父ASIN"), "标题": i("标题"),
            "类型": i("产品类型"), "价格": i("价格($)")}
    out = []
    for r in rows[1:]:
        d = {k: (r[v] if v is not None else None) for k, v in cols.items()}
        d["销额数"] = _num(r[cols["销额"]]) if cols["销额"] is not None else None
        d["销量数"] = _num(r[cols["销量"]]) if cols["销量"] is not None else None
        d["价格数"] = _num(r[cols["价格"]]) if cols["价格"] is not None else None
        # 厚度(inch): 单值直接用; 多件套取最大值
        th_raw = str(r[cols["厚度"]] or "") if cols["厚度"] is not None else ""
        ths = [float(m) for m in re.findall(r'(\d+(?:\.\d+)?)', th_raw)] if th_raw not in MISSING else []
        d["厚度数"] = max(ths) if ths else None
        d.update(parse_spec(r[cols["规格"]] if cols["规格"] is not None else None))
        out.append(d)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="材质×套装数量 的 变体月销额 Top3 尺寸规格统计")
    ap.add_argument("--cat", required=True)
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args(argv)

    cat_dir = os.path.join(_ROOT, "data", args.cat)
    rows = load(cat_dir, args.cat)

    groups: dict[tuple, list] = collections.defaultdict(list)
    for d in rows:
        groups[(str(d["材质"] or "未明确"), str(d["套数"] or "未明确"))].append(d)

    # 组内按销额降序(无销额的排最后)
    detail = []
    summary = []
    for (mat, cnt), lst in groups.items():
        lst2 = sorted(lst, key=lambda x: (x["销额数"] is not None, x["销额数"] or 0),
                      reverse=True)
        top = lst2[:args.top]
        tot = sum(x["销额数"] or 0 for x in lst)
        top_tot = sum(x["销额数"] or 0 for x in top)
        sizes = [x for x in top if x["主尺寸"]]
        c = collections.Counter(x["主尺寸"] for x in sizes)
        mode = c.most_common(1)[0][0] if c else "—"
        summary.append({
            "主体材质": mat, "套装数量": cnt, "组内行数": len(lst),
            "组销额合计": round(tot, 2), "Top3销额合计": round(top_tot, 2),
            "Top3占比": round(100.0 * top_tot / tot, 1) if tot else 0.0,
            "Top3主尺寸": "、".join(dict.fromkeys(x["主尺寸"] for x in sizes)) or "缺",
            "最常见主尺寸": mode,
            "Top3均厚(inch)": round(sum(x["厚度数"] for x in top if x.get("厚度数")) /
                                 max(1, sum(1 for x in top if x.get("厚度数"))), 3)
            if any(x.get("厚度数") for x in top) else None,
        })
        for rank, x in enumerate(top, 1):
            detail.append({
                "主体材质": mat, "套装数量": cnt, "排名": rank,
                "变体ASIN": x.get("asin"), "产品类型": x.get("类型"),
                "变体月销额": x.get("销额数"), "变体月销量": x.get("销量数"),
                "价格($)": x.get("价格数"),
                "尺寸规格": str(x.get("规格") or ""),
                "厚度": str(x.get("厚度") or ""),
                "主尺寸(最大件 长x宽)": x.get("主尺寸") or "缺",
                "件数(规格段)": x.get("件数"),
                "标题": str(x.get("标题") or "")[:90],
            })
    summary.sort(key=lambda s: (s["组销额合计"]), reverse=True)

    # 尺寸规格频次(Top3 汇总)
    freq = collections.Counter()
    for d in detail:
        if d["主尺寸(最大件 长x宽)"] != "缺":
            freq[d["主尺寸(最大件 长x宽)"]] += 1
    mat_freq = collections.defaultdict(collections.Counter)
    for d in detail:
        if d["主尺寸(最大件 长x宽)"] != "缺":
            mat_freq[d["主体材质"]][d["主尺寸(最大件 长x宽)"]] += 1

    # ---------------- markdown
    out = ["# %s · 主体材质 × 套装数量 · 变体月销额 Top%d 尺寸规格统计" % (args.cat, args.top), "",
           "- 分组: **主体材质 × 套装数量**(交付表原值), 组内按 **变体月销额** 降序取前 %d;" % args.top,
           "- **单位: inch**;「主尺寸」= 该行尺寸里**面积最大那一件**的 `长x宽`(多件套取最大件, 便于跨件数比较);",
           "- 行数 %d, 分组 %d 个; 有销额的行 %d。"
           % (len(rows), len(groups), sum(1 for d in rows if d["销额数"] is not None)), ""]

    out += ["## 一、分组汇总(按组销额合计降序)", "",
            "| 主体材质 | 套装数量 | 组内行数 | 组销额合计 | Top%d销额合计 | Top%d占比 | 最常见主尺寸 | Top%d均厚(inch) |"
            % (args.top, args.top, args.top),
            "|---|---|---|---|---|---|---|---|"]
    for s in summary:
        out.append("| %s | %s | %d | %s | %s | %s%% | %s | %s |"
                   % (s["主体材质"], s["套装数量"], s["组内行数"],
                      "{:,.0f}".format(s["组销额合计"]), "{:,.0f}".format(s["Top3销额合计"]),
                      s["Top3占比"], s["最常见主尺寸"],
                      ("%g" % s["Top3均厚(inch)"]) if s["Top3均厚(inch)"] is not None else "—"))
    out.append("")

    out += ["## 二、Top%d 明细(按材质分组)" % args.top, ""]
    cur = None
    for d in sorted(detail, key=lambda x: (x["主体材质"], x["套装数量"], x["排名"])):
        g = (d["主体材质"], d["套装数量"])
        if g != cur:
            cur = g
            out += ["", "### %s · %s" % g, "",
                    "| 排名 | 变体ASIN | 变体月销额 | 变体月销量 | 尺寸规格 | 厚度 | 主尺寸 |",
                    "|---|---|---|---|---|---|---|"]
        out.append("| %d | %s | %s | %s | %s | %s | %s |"
                   % (d["排名"], d["变体ASIN"],
                      "{:,.0f}".format(d["变体月销额"]) if d["变体月销额"] is not None else "缺",
                      "{:,.0f}".format(d["变体月销量"]) if d["变体月销量"] is not None else "缺",
                      d["尺寸规格"].replace("|", "\\|") or "缺",
                      d["厚度"].replace("|", "\\|") or "缺",
                      d["主尺寸(最大件 长x宽)"]))
    out.append("")

    out += ["## 三、Top%d 的尺寸规格频次" % args.top, "",
            "| 主尺寸(长x宽, inch) | 出现次数 |", "|---|---|"]
    for k, v in freq.most_common(30):
        out.append("| %s | %d |" % (k, v))
    out.append("")
    out += ["### 分材质的主尺寸频次", ""]
    for mat in sorted(mat_freq, key=lambda m: -sum(mat_freq[m].values())):
        items = "、".join("%s(%d)" % (k, v) for k, v in mat_freq[mat].most_common(8))
        out += ["- **%s**: %s" % (mat, items)]
    out.append("")

    md_path = os.path.join(cat_dir, STD, "%s-材质x套装-变体月销额Top%d-尺寸规格报告.md"
                           % (args.cat, args.top))
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("[报告] %s (%d 字)" % (md_path, len("\n".join(out))))

    # ---------------- xlsx
    import openpyxl
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Top%d明细" % args.top
    keys = ["主体材质", "套装数量", "排名", "变体ASIN", "产品类型", "变体月销额", "变体月销量",
            "价格($)", "尺寸规格", "厚度", "主尺寸(最大件 长x宽)", "件数(规格段)", "标题"]
    ws1.append(keys)
    for d in sorted(detail, key=lambda x: (x["主体材质"], x["套装数量"], x["排名"])):
        ws1.append([d.get(k) for k in keys])
    ws2 = wb.create_sheet("分组汇总")
    sk = ["主体材质", "套装数量", "组内行数", "组销额合计", "Top3销额合计", "Top3占比",
          "Top3主尺寸", "最常见主尺寸", "Top3均厚(inch)"]
    ws2.append(sk)
    for s in summary:
        ws2.append([s.get(k) for k in sk])
    ws3 = wb.create_sheet("尺寸规格频次")
    ws3.append(["分组口径", "主尺寸(长x宽, inch)", "出现次数"])
    for k, v in freq.most_common():
        ws3.append(["全部 Top%d" % args.top, k, v])
    for mat, c in mat_freq.items():
        for k, v in c.most_common():
            ws3.append([mat, k, v])
    xlsx_path = os.path.join(cat_dir, STD, "%s-材质x套装-变体月销额Top%d-尺寸规格.xlsx"
                             % (args.cat, args.top))
    wb.save(xlsx_path)
    print("[表格] %s" % xlsx_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
