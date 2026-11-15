# -*- coding: utf-8 -*-
"""审查并纠正「尺寸规格」列的三值顺序: 必须为 长 x 宽 x 厚(高), 且 长 >= 宽 >= 厚。

判定(每条尺寸):
  A 宽大于长        第 2 个值 > 第 1 个值
  B 厚度不是最小值  第 3 个值 > 第 1 或第 2 个值
  C 两个值相等      长==宽 / 长==厚 / 宽==厚 (不算错, 仅记录)

用法:
  python scripts/audit_spec_order.py                 # 只扫描出报告
  python scripts/audit_spec_order.py --apply         # 写回交付表(先备份)
"""
from __future__ import annotations

import argparse
import io
import os
import re
import shutil
from collections import Counter

import openpyxl

CAT = "Cutting Boards-US-202608"
STD = "18_标准化"
MISSING = "缺"
TOL = 0.0015

# 一段尺寸(冒号后)里的 长x宽x厚, 厚可为「缺」
RE_TRIPLE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)(?:\s*[xX×*]\s*(\d+(?:\.\d+)?|缺))?"
)


def fmt(v: float) -> str:
    s = "%.3f" % v
    s = s.rstrip("0").rstrip(".")
    return s or "0"


def segments(val: str) -> list[tuple[int, int, str]]:
    """把尺寸规格切成若干段: (起点, 终点, 段文本)。段 = 逗号/分号分隔且非空。"""
    out = []
    for m in re.finditer(r"[^,，;；]+", val or ""):
        seg = m.group(0)
        if seg.strip():
            out.append((m.start(), m.end(), seg))
    return out


def body_span(seg: str) -> tuple[int, int]:
    """段里冒号之后的部分为数值体(标签里可能也含数字, 不能参与解析)。"""
    if ":" in seg:
        i = seg.rfind(":") + 1
    elif "：" in seg:
        i = seg.rfind("：") + 1
    else:
        i = 0
    return i, len(seg)


def parse_seg(seg: str) -> list[dict]:
    i0, i1 = body_span(seg)
    out = []
    for m in RE_TRIPLE.finditer(seg, i0):
        if m.start() >= i1:
            break
        h = m.group(3)
        out.append({
            "span": (m.start(), m.end()),
            "长": float(m.group(1)),
            "宽": float(m.group(2)),
            "厚": None if (h in (None, "", MISSING)) else float(h),
        })
    return out


def order_of(a: float, b: float, h: float | None) -> str:
    """返回违反类型: '' / 'A' / 'B' / 'AB' / 'C'"""
    bad = ""
    if b > a + TOL:
        bad += "A"
    if h is not None and (h > a + TOL or h > b + TOL):
        bad += "B"
    if abs(b - a) <= TOL:
        bad += "C"
    return bad


def scan(val: str) -> list[dict]:
    res = []
    for s0, s1, seg in segments(val):
        for e in parse_seg(seg):
            bad = order_of(e["长"], e["宽"], e["厚"])
            if bad:
                res.append({"段": seg, "bad": bad, **e})
    return res


def fix_seg(seg: str) -> str:
    """把段里每个三值组按 长>=宽>=厚 重排。

    只对调数值 token 的**原始文本**(保留 15.701 / 0.06 之类原有写法与
    末尾的「缺 inch」等字符), 不改动标签、冒号、单位。仅在需要时改动。
    """
    ents = parse_seg(seg)
    if not ents:
        return seg
    out = seg
    for e in sorted(ents, key=lambda x: -x["span"][0]):
        a, b, h = e["长"], e["宽"], e["厚"]
        vals = [a, b] + ([h] if h is not None else [])
        srt = sorted(vals, reverse=True)
        if h is not None:
            if abs(srt[0] - a) <= TOL and abs(srt[1] - b) <= TOL and abs(srt[2] - h) <= TOL:
                continue
        elif abs(srt[0] - a) <= TOL and abs(srt[1] - b) <= TOL:
            continue

        i0, i1 = e["span"]
        parts = re.split(r"([xX×*])", seg[i0:i1])   # 偶数位=token, 奇数位=分隔符
        slots = [i for i, p in enumerate(parts) if re.fullmatch(r"\d+(?:\.\d+)?", p)]
        texts = [parts[i] for i in slots]
        order = sorted(range(len(texts)), key=lambda j: -float(texts[j]))
        newp = list(parts)
        for slot, j in zip(slots, order):
            newp[slot] = texts[j]
        out = out[:i0] + "".join(newp) + out[i1:]
    return out


def fix_val(val: str) -> str:
    res = []
    pos = 0
    for s0, s1, seg in segments(val):
        res.append((val[pos:s0], seg))
        pos = s1
    res.append((val[pos:], ""))
    out = []
    for gap, seg in res:
        out.append(gap)
        if seg:
            out.append(fix_seg(seg))
    return "".join(out)


def load(cat_dir: str):
    """载入标准化交付物。

    2026-10-09 修: 原实现用模块常量 CAT 拼文件名, 于是**任何 cat_dir 都会去找
    "Cutting Boards-US-202608-属性标注-标准化交付.xlsx"** —— 整合报告因此只能对
    Cutting Boards 类目跑通(其余类目报 FileNotFoundError)。现改为从 cat_dir 目录内
    匹配 "*标准化交付*.xlsx", 找不到再退回常量 CAT(兼容旧目录结构)。
    """
    cands = []
    std_dir = os.path.join(cat_dir, STD)
    if os.path.isdir(std_dir):
        cands = [os.path.join(std_dir, f) for f in sorted(os.listdir(std_dir))
                 if f.endswith(".xlsx") and "标准化交付" in f]
    if not cands:
        cands = [os.path.join(std_dir, "%s-属性标注-标准化交付.xlsx" % CAT)]
    path = cands[0]
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = {n: [list(r) for r in wb[n].iter_rows(values_only=True)] for n in wb.sheetnames}
    wb.close()
    return path, sheets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat-dir", default=os.path.join("data", CAT))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    cat_dir = args.cat_dir
    path, sheets = load(cat_dir)
    main_sheet = "属性标注" if "属性标注" in sheets else list(sheets)[0]
    rows = sheets[main_sheet]
    hdr = [str(h) if h is not None else "" for h in rows[0]]
    iz = hdr.index("尺寸规格")
    ia = hdr.index("变体ASIN")
    iset = hdr.index("套装数量")
    ith = hdr.index("厚度")

    stat = Counter()
    bad_rows = []
    c_rows = []
    for k in range(1, len(rows)):
        val = str(rows[k][iz] or "").strip()
        if not val or val == MISSING:
            stat["尺寸规格缺"] += 1
            continue
        hits = scan(val)
        if not hits:
            stat["合规"] += 1
            continue
        types = "".join(sorted({h["bad"] for h in hits}))
        real = [c for c in types if c in "AB"]
        if not real:
            stat["仅长=宽(圆形/方形, 不算错)"] += 1
            c_rows.append({
                "xl_row": k + 1,
                "变体ASIN": str(rows[k][ia] or ""),
                "套装数量": str(rows[k][iset] or ""),
                "厚度": str(rows[k][ith] or ""),
                "尺寸规格": val,
            })
            continue
        rec = {
            "xl_row": k + 1,
            "变体ASIN": str(rows[k][ia] or ""),
            "套装数量": str(rows[k][iset] or ""),
            "厚度": str(rows[k][ith] or ""),
            "尺寸规格": val,
            "问题": "".join(real),
            "问题段": " ; ".join(sorted({h["段"].strip() for h in hits if h["bad"] in "AB"})),
            "修正后": fix_val(val),
        }
        stat["违规行"] += 1
        for ch in real:
            stat["类型" + ch] += 1
        if "C" in types:
            rec["问题"] += "(含长=宽)"
        bad_rows.append(rec)

    lines = ["# 尺寸规格 三值顺序审查", "",
             "规则: `长x宽x厚(高)`, 且 **长 >= 宽 >= 厚**。", "",
             "| 指标 | 行数 |", "|---|---|"]
    for kk in ["合规", "违规行", "尺寸规格缺", "类型A", "类型B", "仅长=宽(圆形/方形, 不算错)"]:
        if stat[kk]:
            lines.append("| %s | %d |" % (kk, stat[kk]))
    lines.append("")
    lines.append("类型A = 宽 > 长; 类型B = 厚(高)不是最小值。共 **%d 行需纠正**。" % len(bad_rows))
    lines.append("")
    lines.append("| xl_row | 变体ASIN | 套装数量 | 厚度 | 尺寸规格 | 问题 | 修正后 |")
    lines.append("|---|---|---|---|---|---|---|")
    for d in sorted(bad_rows, key=lambda x: x["问题"]):
        lines.append("| %d | %s | %s | %s | %s | %s | %s |" % (
            d["xl_row"], d["变体ASIN"], d["套装数量"], d["厚度"],
            d["尺寸规格"].replace("|", "/"), d["问题"], d["修正后"].replace("|", "/")))
    lines.append("")
    lines.append("## 附: 长=宽 的行(圆形/方形砧板, 属正常, 不修改) %d 行" % len(c_rows))
    lines.append("")
    lines.append("| xl_row | 变体ASIN | 套装数量 | 厚度 | 尺寸规格 |")
    lines.append("|---|---|---|---|---|")
    for d in c_rows:
        lines.append("| %d | %s | %s | %s | %s |" % (
            d["xl_row"], d["变体ASIN"], d["套装数量"], d["厚度"], d["尺寸规格"].replace("|", "/")))
    rep = args.report or os.path.join(cat_dir, STD, "%s-尺寸规格顺序审查.md" % CAT)
    io.open(rep, "w", encoding="utf-8").write("\n".join(lines))
    print("统计:", dict(stat))
    print("需纠正 %d 行" % len(bad_rows))
    print("[写] %s" % rep)

    if not args.apply:
        return 0

    if not bad_rows:
        print("无需修正")
        return 0

    bak = path + ".bak-before-order.xlsx"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    src_name = "来源"
    i_src = None
    if src_name in sheets and "尺寸规格" in [str(x) if x is not None else "" for x in sheets[src_name][0]]:
        i_src = [str(x) if x is not None else "" for x in sheets[src_name][0]].index("尺寸规格")
    n = 0
    for d in bad_rows:
        k = d["xl_row"] - 1
        rows[k][iz] = d["修正后"]
        n += 1
        if i_src is not None:
            cur = str(sheets[src_name][k][i_src] or "")
            note = "顺序修正(长>=宽>=厚)"
            sheets[src_name][k][i_src] = (cur + "; " + note) if (cur and note not in cur) else (cur or note)
    out = openpyxl.Workbook()
    first = out.active
    first.title = main_sheet
    for j, name in enumerate([main_sheet] + [s for s in sheets if s != main_sheet]):
        ws = first if j == 0 else out.create_sheet(name)
        for r in sheets[name]:
            ws.append(r)
    out.save(path)
    print("[写回] 尺寸规格顺序修正 %d 行" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
