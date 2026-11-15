# -*- coding: utf-8 -*-
"""CLI: 把「厚度」与「尺寸规格·高(第三维)」对齐(2026-09-24 第三轮)。

用法
====
    python scripts/sync_thickness_size.py --cat "Cutting Boards-US-202608"            # 只看统计
    python scripts/sync_thickness_size.py --cat "..." --apply                         # 写回
    python scripts/sync_thickness_size.py --cat "..." --apply --vision-fill           # 残余「缺」再用图片试补

口径(用户 2026-09-24)
=====================
1. 「厚度」要与 `尺寸规格` 的厚度(高, 第三维)**对齐**, 两列不矛盾;
2. 多件套**各件厚度相同 → 只写一个值**; **不同 → 逐件写** `大号:0.5cm,小号:0.4cm`;
3. **没有厚度写 `缺`**(取代「未明确」);
4. 冲突时**以核实到的单块厚度为准, 同时修正 `尺寸规格` 的高**。
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

from core.config import Config                                          # noqa: E402
from core.llm import LLMClient                                          # noqa: E402
from tools.attr_stability.size_thickness_sync import (                  # noqa: E402
    reconcile, parse_cm, MISSING,
)
from tools.attr_stability.thickness import ThicknessRefiner             # noqa: E402

STEP17_DIR = "17_属性标注"
STD_DIR = "18_标准化"


class _Ctx:
    def __init__(self, config):
        self.config = config
        self._llm = None

    def llm(self):
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm


def _std_path(cat_dir: str, cat: str) -> str:
    return os.path.join(cat_dir, STD_DIR, "%s-属性标注-标准化交付.xlsx" % cat)


def _asin_index(step17_dir: str) -> dict:
    rows = json.load(open(os.path.join(step17_dir, "input.json"), encoding="utf-8"))["rows"]
    idx = {}
    for i, r in enumerate(rows, start=2):
        xl = r.get("xl_row")
        try:
            xl = int(xl)
        except (TypeError, ValueError):
            xl = i
        idx[(str(r.get("父ASIN") or ""), str(r.get("变体ASIN") or ""))] = xl
    return idx


def _load(cat_dir: str, cat: str, step17_dir: str):
    import openpyxl
    path = _std_path(cat_dir, cat)
    if not os.path.exists(path):
        raise SystemExit("[错误] 找不到交付表: %s" % path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = {n: [list(r) for r in wb[n].iter_rows(values_only=True)] for n in wb.sheetnames}
    wb.close()
    main = "属性标注" if "属性标注" in sheets else list(sheets)[0]
    hdr = sheets[main][0]
    idx = _asin_index(step17_dir)
    ip = hdr.index("父ASIN") if "父ASIN" in hdr else None
    iv = hdr.index("变体ASIN") if "变体ASIN" in hdr else None
    decl, xls = {}, []
    for k in range(1, len(sheets[main])):
        r = sheets[main][k]
        key = (str(r[ip] or ""), str(r[iv] or "")) if (ip is not None and iv is not None) else None
        xl = idx.get(key) if key else None
        xl = xl if xl is not None else k + 1
        xls.append(xl)
        decl[xl] = {c: r[hdr.index(c)] for c in ("厚度", "尺寸规格", "套装数量") if c in hdr}
    return sheets, main, ("来源" if "来源" in sheets else ""), decl, xls


def _set_num(v) -> int:
    m = re.search(r'(\d+)', str(v or ""))
    return int(m.group(1)) if m else 0


def _vision_fill(ctx, rows_by_xl, targets, *, max_images=5, aplus=3, workers=8, log=print):
    """对仍然「缺」的行, 用**只问厚度**的提示词再试一次图片(不再给结构化尺寸, 免得被锚定)。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from tools.attr_stability.thickness import _vision_with_fallback
    prompt_head = (
        "你是亚马逊砧板类目的数据校对员。请看图回答：**这一行商品「单块(单块板体)」的厚度是多少 cm**。\n"
        "要求：\n"
        "1. 优先读图上的**尺寸标注/规格小字(OCR)**: `0.6in`、`1.5cm`、`Thickness: 0.8 inch`、`10mm`。\n"
        "2. 图上若只有**整叠/整摞**的高度, 那不是单块厚度, 填 `未明确`。\n"
        "3. 看不清或图上没有厚度信息就填 `未明确`, **不要猜**。合理范围 0.03~15 cm。\n"
        "4. `依据` 写清第几张图看到了什么。\n"
        "输出严格 JSON: {\"xl_row\": <行号>, \"厚度_cm\": <数值或\"未明确\">, \"依据\": \"...\"}\n\n"
        "## 本行文字资料(仅辅助, 不要照抄长/宽)\n")
    out = {}

    def _one(xl):
        row = rows_by_xl.get(xl)
        if not row:
            return None
        urls = ThicknessRefiner.row_image_urls(row, max_images, aplus)
        if not urls:
            return None
        ev = "\n".join("- %s: %s" % (c, str(row.get(c))[:700])
                       for c in ("标题", "五点描述", "SKU", "详细参数") if row.get(c))
        p = prompt_head + ev + "\n\n共 %d 张本行图片。" % len(urls)
        r, _err = _vision_with_fallback(ctx, p, urls, detail="high", max_tokens=900)
        if not isinstance(r, dict):
            return None
        from tools.attr_stability.vision_json import unwrap_vision
        u = unwrap_vision(r, ("厚度_cm", "依据"))
        cm = parse_cm(u.get("厚度_cm"))
        if cm is None:
            s = str(u.get("厚度_cm") or "")
            m = re.search(r'([\d.]+)', s)
            if m:
                try:
                    v = float(m.group(1))
                    cm = v if 0.03 <= v <= 15 else None
                except ValueError:
                    cm = None
        return {"xl_row": xl, "cm": cm, "依据": str(u.get("依据") or "")[:200]}

    todo = sorted(targets)
    log("残余「缺」行用图片再试: %d 行" % len(todo))
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo) or 1))) as ex:
        futs = {ex.submit(_one, xl): xl for xl in todo}
        n = 0
        for fut in as_completed(futs):
            r = fut.result()
            n += 1
            if r:
                out[futs[fut]] = r
            if n % 25 == 0 or n == len(todo):
                log("   图片补 行 %d/%d" % (n, len(todo)))
    ok = sum(1 for v in out.values() if v.get("cm"))
    log("图片补出厚度 %d 行" % ok)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="厚度 ↔ 尺寸规格·高 对齐")
    ap.add_argument("--cat", required=True)
    ap.add_argument("--apply", action="store_true", help="写回交付表(同时修正尺寸规格的高)")
    ap.add_argument("--vision-fill", action="store_true", help="残余「缺」再用图片试补(会调模型)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)

    cat_dir = os.path.join(_ROOT, "data", args.cat)
    step17 = os.path.join(cat_dir, STEP17_DIR)
    if not os.path.exists(os.path.join(cat_dir, "config.yaml")):
        print("[错误] 找不到配置")
        return 2
    sheets, main, src, decl, xls = _load(cat_dir, args.cat, step17)
    hdr = sheets[main][0]
    i_th, i_sz = hdr.index("厚度"), hdr.index("尺寸规格")

    plan = {}
    for xl in xls:
        d = decl.get(xl) or {}
        r = reconcile(d.get("尺寸规格"), d.get("厚度"), _set_num(d.get("套装数量")))
        r["原厚度"] = str(d.get("厚度") or "")
        r["原尺寸规格"] = str(d.get("尺寸规格") or "")
        plan[xl] = r

    stat = collections.Counter()
    for xl, r in plan.items():
        if not r["改动"]:
            stat["无需改动"] += 1
            continue
        if r["原厚度"].strip() != r["厚度"]:
            stat["厚度改动"] += 1
        if r["原尺寸规格"].strip() != r["尺寸规格"]:
            stat["尺寸规格改动"] += 1
        stat["有改动"] += 1
    miss = [xl for xl, r in plan.items() if r["厚度"] == MISSING]
    multi = [xl for xl, r in plan.items() if "," in r["厚度"]]
    print("=" * 64)
    print("行数=%d  有改动=%d(厚度 %d / 尺寸规格 %d)  无需改动=%d"
          % (len(plan), stat["有改动"], stat["厚度改动"], stat["尺寸规格改动"],
             stat["无需改动"]))
    print("逐件厚度(多值)的行: %d" % len(multi))
    print("结果为「缺」的行: %d" % len(miss))
    print("改动原因分布:")
    for k, v in collections.Counter(r["说明"] for r in plan.values()
                                    if r["改动"]).most_common(12):
        print("   %5d  %s" % (v, k))

    vis = {}
    if args.vision_fill and miss:
        cfg = Config.load(os.path.join(cat_dir, "config.yaml"))
        rows = json.load(open(os.path.join(step17, "input.json"), encoding="utf-8"))["rows"]
        by_xl = {}
        for i, r in enumerate(rows, start=2):
            try:
                by_xl[int(r.get("xl_row"))] = r
            except (TypeError, ValueError):
                by_xl[i] = r
        vis = _vision_fill(_Ctx(cfg), by_xl, miss, workers=args.workers)
        got = 0
        for xl, rec in vis.items():
            if not rec.get("cm"):
                continue
            got += 1
            d = decl.get(xl) or {}
            plan[xl] = reconcile(d.get("尺寸规格"), "%gcm" % rec["cm"],
                                 _set_num(d.get("套装数量")))
            plan[xl]["来源"] = "图片视觉/OCR: %s" % rec.get("依据", "")
        print("图片补出 %d 行; 补后仍为「缺」的行: %d"
              % (got, sum(1 for r in plan.values() if r["厚度"] == MISSING)))

    if not args.apply:
        print("(未写回; 加 --apply 生效)")
        return 0

    import openpyxl
    import shutil
    path = _std_path(cat_dir, args.cat)
    bak = path + ".bak-before-sync.xlsx"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    i_src = sheets[src][0].index("厚度") if src and "厚度" in sheets[src][0] else None
    n = 0
    for k, xl in enumerate(xls):
        r = plan.get(xl)
        if not r or not r["改动"]:
            continue
        sheets[main][k + 1][i_th] = r["厚度"]
        sheets[main][k + 1][i_sz] = r["尺寸规格"]
        if i_src is not None and r.get("来源"):
            sheets[src][k + 1][i_src] = r["来源"]
        n += 1
    out = openpyxl.Workbook()
    first = out.active
    first.title = main
    for j, name in enumerate([main] + [s for s in sheets if s != main]):
        ws = first if j == 0 else out.create_sheet(name)
        for row in sheets[name]:
            ws.append(row)
    out.save(path)
    print("[写回] 改动 %d 行" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
