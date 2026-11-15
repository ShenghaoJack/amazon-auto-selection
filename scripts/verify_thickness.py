# -*- coding: utf-8 -*-
"""CLI: 核实「厚度」里的异常大值 —— 是单块真厚, 还是多块叠起来的整叠高度。

用法
====
    python scripts/verify_thickness.py --cat "Cutting Boards-US-202608"            # 先看候选
    python scripts/verify_thickness.py --cat "..." --run                            # 跑文本+图片核实
    python scripts/verify_thickness.py --cat "..." --run --apply                    # 写回
    python scripts/verify_thickness.py --cat "..." --apply                          # 用已有结果写回

背景(用户 2026-09-24)
=====================
"厚度列异常的例如 9cm 的, 这个是不是要用文字和图片去核实下是否真实, 还是说把多块栈板的厚度
叠加了, 并把真实厚度更新到厚度列和尺寸规格列"。

实测: 套装里的 `Product Dimensions` 那一维常是**整叠高度** ——
`3 件套 43.2x30.5x6.1 cm`(6.1 ≈ 3×2.03)、`5 件套 50.8x35.56x10.16`(10.16 = 5×2.032)、
`10 件套 40.64x25.4x13.0048`(13 ÷ 10 = 1.3)、`12 件套 16.1x10.2x5.1 in`(整包)。
所以候选 = **套装且厚度/规格高 ≥ 3.5cm** 或 **单件且 ≥ 6cm**;
逐行用**文字比对 + 图片视觉**判"单块 or 整叠", 再把真实单块厚度写回「厚度」与「尺寸规格」。
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

from core.config import Config                                       # noqa: E402
from core.llm import LLMClient                                       # noqa: E402
from tools.attr_stability.size_thickness_sync import (               # noqa: E402
    parse_size_spec, parse_cm, _rewrite_spec, _looks_like_stack, MISSING,
)
from tools.attr_stability.thickness import (                         # noqa: E402
    ThicknessRefiner, _vision_with_fallback,
)
from tools.attr_stability.text_llm import quote_in_text, run_batches   # noqa: E402
from tools.attr_stability.vision_json import unwrap_vision            # noqa: E402

STEP17_DIR = "17_属性标注"
STD_DIR = "18_标准化"
NAME = "厚度-异常值核实.json"
CKPT = "厚度-异常值核实.checkpoint.json"

TEXT_CORE = """你是亚马逊砧板类目(Cutting Boards)的数据校对员。
下面每行是一件砧板商品(可能是**套装**)。已知它的尺寸里那个"厚度方向"的数值, 需要我们判断
**这是单块砧板的厚度, 还是多块叠起来(整套/整包)的高度**。

请回答:
1. `单块厚度_cm`: **每块**砧板的厚度(cm)。资料里能看出单块厚度就填(如 `0.8 inch` → `2.032`);
   判断不出填 `未明确`。
2. `是否整叠`: 资料里那个厚度方向的数值, 是**整叠/整套**的高度吗? 填 `是` / `否` / `无法判断`。
3. `整叠块数`: 若判定是整叠, 是**几块**叠起来的高度(整数); 判不出填 `无`。
4. `原文`: 你判断所依据的**原句片段, 照抄不要改写**(核验时会用它在该行资料里定位)。

注意
· 套装常见写法: `Product Dimensions:17"L x 12"W x 2.4"Th` —— 这一维往往是**整叠/包装**高度;
  而 `each board is 0.8 inch thick`、`0.6"Th` 配单块长宽、五点里 `0.31 inches thick` 是**单块**。
· 只有"加厚/超厚"这类形容词、没有数字时, `单块厚度_cm` 填 `未明确`。
· 单位换算: 1 inch = 2.54 cm, 1 mm = 0.1 cm。单块厚度合理范围 0.03~4 cm。

输出严格 JSON:
{"rows": [{"xl_row": <行号>, "单块厚度_cm": <数值或"未明确">, "是否整叠": "是/否/无法判断",
           "整叠块数": <整数或"无">, "原文": "<照抄>"}]}
"""

VISION_CORE = """你是亚马逊砧板类目(Cutting Boards)的数据校对员。
这是一件**砧板商品**(可能是套装)。请**看图**回答:

1. `单块厚度_cm`: **每一块**砧板的厚度是多少 cm? 优先读图上的尺寸标注/规格小字(OCR):
   `0.6in`、`1.5cm`、`Thickness: 0.8 inch`、`10mm`。判不出填 `未明确`。
2. `是否整叠`: 图上那个厚度方向的量, 是不是**多块叠起来/整套包装**的高度? 填 `是`/`否`/`无法判断`。
3. `整叠块数`: 若是整叠, 几块叠起来(整数); 判不出填 `无`。
4. `依据`: 第几张图看到了什么。

注意: 不要把整叠的总高当成单块厚度; 也不要猜。单位换算 1 inch = 2.54 cm, 1 mm = 0.1 cm。
输出严格 JSON: {"xl_row": <行号>, "单块厚度_cm": <数值或"未明确">, "是否整叠": "是/否/无法判断",
                "整叠块数": <整数或"无">, "依据": "..."}
"""

FIELDS = ("标题", "五点描述", "SKU", "详细参数", "商品尺寸", "商品尺寸（单位换算）")


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


def _asin_index(step17: str) -> dict:
    rows = json.load(open(os.path.join(step17, "input.json"), encoding="utf-8"))["rows"]
    idx = {}
    for i, r in enumerate(rows, start=2):
        xl = r.get("xl_row")
        try:
            xl = int(xl)
        except (TypeError, ValueError):
            xl = i
        idx[(str(r.get("父ASIN") or ""), str(r.get("变体ASIN") or ""))] = xl
    return idx


def _set_num(v) -> int:
    m = re.search(r'(\d+)', str(v or ""))
    return int(m.group(1)) if m else 1


def _load(cat_dir: str, cat: str, step17: str):
    import openpyxl
    path = _std_path(cat_dir, cat)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = {n: [list(r) for r in wb[n].iter_rows(values_only=True)] for n in wb.sheetnames}
    wb.close()
    main = "属性标注" if "属性标注" in sheets else list(sheets)[0]
    hdr = sheets[main][0]
    idx = _asin_index(step17)
    ip = hdr.index("父ASIN") if "父ASIN" in hdr else None
    iv = hdr.index("变体ASIN") if "变体ASIN" in hdr else None
    decl, xls = {}, []
    for k in range(1, len(sheets[main])):
        r = sheets[main][k]
        key = (str(r[ip] or ""), str(r[iv] or "")) if (ip is not None and iv is not None) else None
        xl = idx.get(key) if key else None
        xl = xl if xl is not None else k + 1
        xls.append(xl)
        decl[xl] = {"厚度": r[hdr.index("厚度")], "尺寸规格": r[hdr.index("尺寸规格")],
                    "套装数量": r[hdr.index("套装数量")],
                    "主体材质": r[hdr.index("主体材质")] if "主体材质" in hdr else "",
                    "变体ASIN": r[iv] if iv is not None else ""}
    return sheets, main, ("来源" if "来源" in sheets else ""), decl, xls


def find_candidates(decl: dict) -> dict:
    """套装且(厚度或规格高)≥3.5cm, 或单件且 ≥6cm。"""
    out = {}
    for xl, d in decl.items():
        n = _set_num(d.get("套装数量"))
        th = _max_th(d.get("厚度"))
        hs = [s["h"] for s in parse_size_spec(d.get("尺寸规格")) if s["h"] is not None]
        mx = max([x for x in ([th] + hs) if x is not None], default=None)
        if mx is None:
            continue
        if (n >= 2 and mx >= 3.5) or (n < 2 and mx >= 6.0):
            out[xl] = {"n": n, "max": mx, "厚度": str(d.get("厚度") or ""),
                       "尺寸规格": str(d.get("尺寸规格") or ""),
                       "asin": d.get("变体ASIN") or ""}
    return out


def _max_th(v) -> float | None:
    s = str(v or "")
    vals = [parse_cm(p.split(":")[-1]) for p in re.split(r'[,，]', s) if p.strip()]
    vals = [x for x in vals if x is not None]
    return max(vals) if vals else None


def _num(v):
    s = str(v or "").strip()
    if not s or s in ("未明确", "无", "None"):
        return None
    m = re.search(r'(\d+(?:[.,]\d+)?)', s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _decide(total: float | None, n: int, rec: dict, cur: float | None) -> tuple:
    """由核实结论定出**单块厚度** → `(值, 说明, 动作)`。

    动作: `use` 直接用核实值 / `divide` 整叠高度÷块数(推算) / `keep` 保留原值 / `missing` 记缺。
    """
    one = _num(rec.get("单块厚度_cm"))
    if one is not None and 0.03 <= one <= 4.0:
        return round(one, 4), "核实到单块厚度", "use"
    stack = str(rec.get("是否整叠") or "").strip()
    if stack == "是" and n >= 2 and total:
        k = _num(rec.get("整叠块数"))
        kk = int(k) if (k and 2 <= k <= 200) else n
        q = total / kk
        if 0.03 <= q <= 4.0:
            # 推算值四舍五入到 2 位(它本来就是"总高÷块数"的估计, 不该装作精确)
            return round(q, 2), "判定为整叠 → %g ÷ %d 块(推算)" % (total, kk), "divide"
    if n < 2:
        return cur, "单件: 该尺寸即板厚(保留)", "keep"
    if stack == "否":
        return cur, "核实为非整叠(保留)", "keep"
    return None, "无法判定单块厚度", "missing"


THIN_MATERIALS = ("不锈钢", "钛", "玻璃", "塑料", "硅胶")
ROUND2_CORE = """你是亚马逊砧板类目(Cutting Boards)的数据校对员。
这一行的"厚度方向"数值**需要复核**: 它**可能**是包装盒 / 带锁盖 / 带壳的外形尺寸,
**也完全可能**是砧板本体的真实厚度(例如商用塑料砧板就是 1.25 英寸 / 1.5 英寸厚,
端面木砧板常有 2 英寸)。

请回答:
1. `板体厚度_cm`: **砧板本体**的厚度, 单位 cm。
   · 资料里明确写了板体厚度就用它(如 `1.25 inch thick`、`0.6"Th` 配单块长宽、`1.5mm`);
   · 若那个数其实是**包装/带盖/带壳**的外形尺寸(例如"带锁盖的拼盘托盘高 3 英寸"、
     "一次性砧板卷的分配盒高 3.7 英寸"、"砧板套装叠放总高"), 且资料里没有板体厚度 → 填 `未明确`;
   · 判不出就填 `未明确`, **不要猜**。
2. `原文或图片依据`: 你依据的原句片段(照抄) 或 第几张图看到了什么。
3. `原值是否为包装尺寸`: `是` / `否` / `无法判断`。

输出严格 JSON:
{"rows": [{"xl_row": <行号>, "板体厚度_cm": <数值或"未明确">,
           "原文或图片依据": "...", "原值是否为包装尺寸": "是/否/无法判断"}]}
"""


def _spec_lwh(spec: object):
    segs = parse_size_spec(spec)
    for s in segs:
        if s.get("l") and s.get("w"):
            return s["l"], s["w"], s.get("h")
    return None, None, None


def find_candidates2(decl: dict, rows_by_asin: dict) -> dict:
    """第二轮候选: 材质本身就薄(不锈钢/钛/玻璃/塑料)却报厚 / 尺寸不像板(高 > 宽) / 商品尺寸小得多。"""
    out = {}
    for xl, d in decl.items():
        mx = _max_th(d.get("厚度"))
        if mx is None or mx < 3.0:
            continue
        mat = str(d.get("主体材质") or "")
        l, w, h = _spec_lwh(d.get("尺寸规格"))
        src = rows_by_asin.get(d.get("变体ASIN")) or {}
        try:
            cs = [parse_cm(x) for x in re.findall(r'([\d.]+)\s*cm', str(src.get("商品尺寸（单位换算）") or ""))]
            cs = [x for x in cs if x]
        except Exception:  # noqa: BLE001
            cs = []
        why = []
        if any(m in mat for m in THIN_MATERIALS):
            why.append("材质为 %s(本身很薄)" % mat)
        if l and w and h and h > min(l, w):
            why.append("高 > 宽, 不像板体")
        if cs and cs[-1] < mx / 2:
            why.append("商品尺寸的高 %g 远小于当前值" % cs[-1])
        if why:
            out[xl] = {"n": _set_num(d.get("套装数量")), "max": mx,
                       "厚度": str(d.get("厚度") or ""),
                       "尺寸规格": str(d.get("尺寸规格") or ""),
                       "asin": d.get("变体ASIN") or "", "原因": "；".join(why)}
    return out


def _round2(cat_dir, args, sheets, main, src, decl, xls, step17, rows_all, by_asin) -> int:
    """第二轮: 核实"其实是包装尺寸"的高值 → 板体厚度, 判不出就写「缺」。"""
    import openpyxl
    import shutil
    cand = find_candidates2(decl, by_asin)
    print("第二轮候选(材质本身薄 / 尺寸不像板 / 商品尺寸小得多): %d 行" % len(cand))
    for xl, c in cand.items():
        print("   %-12s 厚=%-12s 材质=%-10s 规格=%-24s | %s"
              % (c["asin"], c["厚度"], str(decl[xl].get("主体材质")), c["尺寸规格"][:24], c["原因"]))
    if not cand:
        return 0
    out_path = os.path.join(step17, "厚度-异常值核实-第二轮.json")
    result = {}
    if os.path.exists(out_path) and not args.run:
        try:
            result = json.load(open(out_path, encoding="utf-8")).get("rows") or {}
        except (OSError, ValueError):
            result = {}
    if args.run or (args.apply and not result):
        cfg = Config.load(os.path.join(cat_dir, "config.yaml"))
        ctx = _Ctx(cfg)
        by_xl = {}
        for i, r in enumerate(rows_all, start=2):
            try:
                by_xl[int(r.get("xl_row"))] = r
            except (TypeError, ValueError):
                by_xl[i] = r
        items = [by_xl[xl] for xl in cand if xl in by_xl]

        def _build(batch):
            out = [ROUND2_CORE, "", "=" * 60, "## 本次待判定的行", ""]
            for r in batch:
                xl = int(r.get("xl_row"))
                c = cand.get(xl, {})
                out.append("### xl_row=%d（当前厚度值 %s cm；尺寸规格 %s；主体材质 %s）"
                           % (xl, ("%g" % c["max"]) if c.get("max") else "?", c.get("尺寸规格"),
                              decl.get(xl, {}).get("主体材质")))
                for f in FIELDS:
                    v = r.get(f)
                    if v:
                        s = str(v)
                        out.append("- %s: %s" % (f, s[:1100] + ("…" if len(s) > 1100 else "")))
                out.append("")
            return "\n".join(out)

        def _accept(rec):
            xl = rec.get("xl_row")
            try:
                row = by_xl[int(xl)]
            except (KeyError, TypeError, ValueError):
                return False
            if _num(rec.get("板体厚度_cm")) is not None:
                if not quote_in_text(rec.get("原文或图片依据"), [row.get(f) for f in FIELDS], min_len=6):
                    rec["板体厚度_cm"] = "未明确"
                    rec["_rejected"] = "引用未通过"
            return True

        print("[1/2] 文本核实 ...")
        tres = run_batches(ctx, items, _build, accept=_accept,
                           key_of=lambda r: str(r.get("xl_row")),
                           batch=4, workers=args.workers, label="厚度二轮-文本",
                           ckpt_path="", plan_key="")
        print("[2/2] 图片核实 ...")
        from concurrent.futures import ThreadPoolExecutor, as_completed
        vres = {}

        def _one(xl):
            row = by_xl.get(xl)
            if not row:
                return None
            urls = ThicknessRefiner.row_image_urls(row, 5, 3)
            if not urls:
                return None
            c = cand[xl]
            ev = "\n".join("- %s: %s" % (f, str(row.get(f))[:600])
                           for f in ("标题", "五点描述", "SKU", "详细参数") if row.get(f))
            p = (ROUND2_CORE.replace('{"rows": [{"xl_row": <行号>, "板体厚度_cm": <数值或"未明确">,\n           "原文或图片依据": "...", "原值是否为包装尺寸": "是/否/无法判断"}]}',
                                     '{"xl_row": %d, "板体厚度_cm": "", "原文或图片依据": "", "原值是否为包装尺寸": ""}' % xl)
                 + "\n\n## 本行提示\n- 当前厚度值: %s cm\n- 尺寸规格: %s\n- 主体材质: %s\n\n## 文字资料\n%s\n\n共 %d 张本行图片。"
                 % (("%g" % c["max"]) if c.get("max") else "?", c.get("尺寸规格"),
                    decl.get(xl, {}).get("主体材质"), ev, len(urls)))
            r, _e = _vision_with_fallback(ctx, p, urls, detail="high", max_tokens=900)
            return {"xl_row": xl, "vision": unwrap_vision(r, ("板体厚度_cm", "原文或图片依据")) or {}}

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_one, xl): xl for xl in cand}
            for fut in as_completed(futs):
                r = fut.result()
                if r:
                    vres[r["xl_row"]] = r["vision"]
        for xl in cand:
            t = tres.get(str(xl)) or {}
            v = vres.get(xl) or {}
            one = _num(t.get("板体厚度_cm"))
            srcu = "文本"
            if one is None or not (0.03 <= one <= 4.0):
                one = _num(v.get("板体厚度_cm"))
                srcu = "图片视觉/OCR"
                if one is not None and not (0.03 <= one <= 4.0):
                    one = None
            result[str(xl)] = {"xl_row": xl, "asin": cand[xl]["asin"], "原因": cand[xl]["原因"],
                               "原厚度": cand[xl]["厚度"], "原尺寸规格": cand[xl]["尺寸规格"],
                               "原最大厚度": cand[xl]["max"],
                               "板体厚度_cm": one, "来源": srcu if one else "",
                               "原值是否为包装尺寸": str(t.get("原值是否为包装尺寸")
                                                       or v.get("原值是否为包装尺寸") or ""),
                               "文本": t, "视觉": v}
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cat": args.cat, "rows": result}, fh, ensure_ascii=False)
        os.replace(tmp, out_path)
        print("第二轮核实完成: %d/%d 有板体厚度" % (sum(1 for v in result.values() if v.get("板体厚度_cm")), len(result)))

    if not args.apply:
        print("(加 --apply 写回)")
        return 0
    path = _std_path(cat_dir, args.cat)
    bak = path + ".bak-before-verify2.xlsx"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    hdr = sheets[main][0]
    i_t, i_z = hdr.index("厚度"), hdr.index("尺寸规格")
    i_src = sheets[src][0].index("厚度") if src and "厚度" in sheets[src][0] else None
    n = 0
    stat = collections.Counter()
    for k, xl in enumerate(xls):
        rec = result.get(str(xl))
        if not isinstance(rec, dict):
            continue
        one = rec.get("板体厚度_cm")
        pack = str(rec.get("原值是否为包装尺寸") or "")
        old = str(rec.get("原尺寸规格") or "")
        segs = parse_size_spec(old)
        if one:
            new_th = "%gcm" % round(float(one), 4)
            per = [float(one)] * len(segs) if segs else []
            note = "二轮核实板体厚度: %g cm（%s）" % (one, rec.get("来源") or "")
            act = "use"
        elif pack == "是":
            # 图片/文本明确指认"这个数不是板体厚度" → 记「缺」, 不留包装尺寸冒充板厚
            new_th = MISSING
            per = [None] * len(segs) if segs else []
            note = "二轮核实: 原 %s 疑为包装/带盖/带壳尺寸, 板体厚度判不出 → 记「缺」" % rec.get("原厚度")
            act = "missing"
        else:
            # 证据不足以判定 → **保留原值**, 只在来源里标注存疑(不擅自清零)
            new_th = str(rec.get("原厚度") or "").strip() or MISSING
            per = [parse_cm(x) or None for x in []] or []
            new_spec = old
            note = "二轮核实存疑: 原 %s 可能是板体厚度也可能是包装尺寸, 证据不足未改动" % rec.get("原厚度")
            if i_src is not None:
                sheets[src][k + 1][i_src] = note
            stat["keep"] += 1
            continue
        new_spec = _rewrite_spec(old, segs, per) if segs else old
        if str(sheets[main][k + 1][i_t] or "").strip() != new_th:
            sheets[main][k + 1][i_t] = new_th
        if new_spec != old:
            sheets[main][k + 1][i_z] = new_spec
        if i_src is not None:
            sheets[src][k + 1][i_src] = note
        n += 1
        stat[act] += 1
    out = openpyxl.Workbook()
    first = out.active
    first.title = main
    for j, name in enumerate([main] + [s for s in sheets if s != main]):
        ws = first if j == 0 else out.create_sheet(name)
        for row in sheets[name]:
            ws.append(row)
    out.save(path)
    print("[写回] 第二轮更新 %d 行(保留 %d) 动作分布=%s" % (n, stat["keep"], dict(stat)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="核实厚度异常大值(单块 vs 整叠)")
    ap.add_argument("--cat", required=True)
    ap.add_argument("--run", action="store_true", help="调用文本+图片核实")
    ap.add_argument("--round2", action="store_true",
                    help="第二轮: 材质本身薄/尺寸不像板/商品尺寸小得多的单件行, 核实板体厚度")
    ap.add_argument("--apply", action="store_true", help="写回交付表")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    cat_dir = os.path.join(_ROOT, "data", args.cat)
    step17 = os.path.join(cat_dir, STEP17_DIR)
    sheets, main, src, decl, xls = _load(cat_dir, args.cat, step17)
    rows_all = json.load(open(os.path.join(step17, "input.json"), encoding="utf-8"))["rows"]
    by_asin = {str(r.get("变体ASIN") or ""): r for r in rows_all}
    if args.round2:
        return _round2(cat_dir, args, sheets, main, src, decl, xls, step17, rows_all, by_asin)
    cand = find_candidates(decl)
    if args.limit:
        cand = dict(list(cand.items())[:args.limit])
    print("候选(套装且厚/规格高≥3.5cm, 或单件且≥6cm): %d 行" % len(cand))
    for xl, c in list(cand.items())[:15]:
        print("   %-12s 厚=%-14s N=%-3s 规格=%s" % (c["asin"], c["厚度"], c["n"], c["尺寸规格"][:60]))
    if not args.run and not args.apply:
        print("(加 --run 跑核实, 或 --apply 用已有结果写回)")
        return 0

    out_path = os.path.join(step17, NAME)
    result: dict = {}
    if os.path.exists(out_path):
        try:
            result = json.load(open(out_path, encoding="utf-8")).get("rows") or {}
        except (OSError, ValueError):
            result = {}

    if args.run:
        cfg = Config.load(os.path.join(cat_dir, "config.yaml"))
        ctx = _Ctx(cfg)
        rows = json.load(open(os.path.join(step17, "input.json"), encoding="utf-8"))["rows"]
        by_xl = {}
        for i, r in enumerate(rows, start=2):
            try:
                by_xl[int(r.get("xl_row"))] = r
            except (TypeError, ValueError):
                by_xl[i] = r
        items = [by_xl[xl] for xl in cand if xl in by_xl]

        def _build(batch):
            out = [TEXT_CORE, "", "=" * 60, "## 本次待判定的行", ""]
            for r in batch:
                xl = int(r.get("xl_row"))
                c = cand.get(xl, {})
                out.append("### xl_row=%d（%s 件套；资料里那个厚度方向的数是 %s cm）"
                           % (xl, c.get("n"), ("%g" % c["max"]) if c.get("max") else "?"))
                for f in FIELDS:
                    v = r.get(f)
                    if v:
                        s = str(v)
                        out.append("- %s: %s" % (f, s[:1200] + ("…" if len(s) > 1200 else "")))
                out.append("")
            return "\n".join(out)

        def _accept(rec):
            xl = rec.get("xl_row")
            try:
                row = by_xl[int(xl)]
            except (KeyError, TypeError, ValueError):
                return False
            if _num(rec.get("单块厚度_cm")) is not None:
                if not quote_in_text(rec.get("原文"), [row.get(f) for f in FIELDS], min_len=6):
                    rec["单块厚度_cm"] = "未明确"
                    rec["_rejected"] = "引用未通过"
            return True

        print("[1/2] 文本核实 ...")
        tres = run_batches(ctx, items, _build, accept=_accept,
                           key_of=lambda r: str(r.get("xl_row")),
                           batch=5, workers=args.workers, label="厚度异常-文本",
                           ckpt_path=os.path.join(step17, CKPT),
                           plan_key="verify|%d" % len(items))
        print("[2/2] 图片核实(逐行) ...")
        from concurrent.futures import ThreadPoolExecutor, as_completed
        todo = [xl for xl in cand if xl in by_xl]
        vres = {}

        def _one(xl):
            row = by_xl[xl]
            urls = ThicknessRefiner.row_image_urls(row, 5, 3)
            if not urls:
                return None
            ev = "\n".join("- %s: %s" % (f, str(row.get(f))[:600])
                           for f in ("标题", "五点描述", "SKU", "详细参数") if row.get(f))
            c = cand[xl]
            p = (VISION_CORE + "\n\n## 本行提示\n- 件数: %s\n- 资料里那个厚度方向的数: %s cm\n\n## 文字资料\n%s"
                 % (c.get("n"), ("%g" % c["max"]) if c.get("max") else "?", ev))
            r, _e = _vision_with_fallback(ctx, p, urls, detail="high", max_tokens=900)
            u = unwrap_vision(r, ("单块厚度_cm", "是否整叠", "依据"))
            return {"xl_row": xl, "vision": u or {}}

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_one, xl): xl for xl in todo}
            n = 0
            for fut in as_completed(futs):
                r = fut.result()
                n += 1
                if r:
                    vres[r["xl_row"]] = r["vision"]
                if n % 20 == 0 or n == len(todo):
                    print("   图片核实 行 %d/%d" % (n, len(todo)))
        for xl in todo:
            t = tres.get(str(xl)) or {}
            v = vres.get(xl) or {}
            c = cand[xl]
            total = c.get("max")
            cur = _max_th(c.get("厚度"))
            # 文本优先; 文本判不出再看图片
            one, why, act = _decide(total, c["n"], t, cur)
            src_used = "文本"
            if act == "missing" and v:
                one2, why2, act2 = _decide(total, c["n"], v, cur)
                if act2 != "missing":
                    one, why, act = one2, why2, act2
                    src_used = "图片视觉/OCR"
            elif act == "missing":
                one2, why2, act2 = _decide(total, c["n"], v, cur)
                if act2 != "missing":
                    one, why, act = one2, why2, act2
                    src_used = "图片视觉/OCR"
            if act == "missing" and v:
                # 文本与图片合看: 任一判"是整叠"就按整叠处理
                merged = {"是否整叠": t.get("是否整叠") or v.get("是否整叠"),
                          "整叠块数": t.get("整叠块数") or v.get("整叠块数")}
                one, why, act = _decide(total, c["n"], merged, cur)
                src_used = "文本+图片"
            rec = {"xl_row": xl, "asin": c["asin"], "件数": c["n"], "原厚度": c["厚度"],
                   "原尺寸规格": c["尺寸规格"], "原最大厚度": total,
                   "单块厚度_cm": one, "结论": why, "动作": act,
                   "来源": src_used if act != "missing" else "",
                   "文本": t, "视觉": v}
            result[str(xl)] = rec
        payload = {"cat": args.cat, "候选": len(cand), "rows": result}
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, out_path)
        ok = sum(1 for v in result.values() if v.get("单块厚度_cm"))
        print("核实完成: %d 行有单块厚度结论 → %s" % (ok, out_path))

    # ---------------- 写回
    if args.apply:
        import openpyxl
        import shutil
        path = _std_path(cat_dir, args.cat)
        bak = path + ".bak-before-verify.xlsx"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
        hdr = sheets[main][0]
        i_t, i_z = hdr.index("厚度"), hdr.index("尺寸规格")
        i_src = sheets[src][0].index("厚度") if src and "厚度" in sheets[src][0] else None
        n = 0
        stat = collections.Counter()
        for k, xl in enumerate(xls):
            rec = result.get(str(xl))
            if not isinstance(rec, dict):
                continue
            # 口径改过/旧产物没有 `动作` → 用留档的文本+图片结论**离线重算**(不调模型)
            if not rec.get("动作"):
                t = rec.get("文本") or {}
                v = rec.get("视觉") or {}
                total = rec.get("原最大厚度")
                cur = _max_th(rec.get("原厚度"))
                n_ = int(rec.get("件数") or 1)
                one_, why_, act_ = _decide(total, n_, t, cur)
                src_ = "文本"
                if act_ == "missing" and v:
                    one2, why2, act2 = _decide(total, n_, v, cur)
                    if act2 != "missing":
                        one_, why_, act_ = one2, why2, act2
                        src_ = "图片视觉/OCR"
                if act_ == "missing":
                    merged = {"是否整叠": t.get("是否整叠") or v.get("是否整叠"),
                              "整叠块数": t.get("整叠块数") or v.get("整叠块数")}
                    one_, why_, act_ = _decide(total, n_, merged, cur)
                    src_ = "文本+图片"
                rec.update({"单块厚度_cm": one_, "结论": why_, "动作": act_,
                            "来源": src_ if act_ != "missing" else ""})
            one = rec.get("单块厚度_cm")
            act = rec.get("动作") or ("use" if one else "missing")
            old = str(rec.get("原尺寸规格") or "")
            segs = parse_size_spec(old)
            if act == "missing":
                # 判不出单块 → 厚度写「缺」, 尺寸规格的高也写「缺」(不留整叠高度冒充板厚)
                new_th = MISSING
                per = [None] * len(segs) if segs else []
                note = "核实: %s → 单块厚度记「缺」" % (rec.get("结论") or "无法判定")
            else:
                new_th = "%gcm" % round(float(one), 4)
                per = [float(one)] * len(segs) if segs else []
                tag = {"use": "核实到单块厚度", "divide": "整叠高度÷块数(推算)",
                       "keep": "保留原值"}.get(act, act)
                note = "%s: %s；%s" % (tag, rec.get("结论"), rec.get("来源") or "")
            new_spec = _rewrite_spec(old, segs, per) if segs else old
            if str(sheets[main][k + 1][i_t] or "").strip() != new_th:
                sheets[main][k + 1][i_t] = new_th
            if new_spec != old:
                sheets[main][k + 1][i_z] = new_spec
            if i_src is not None:
                sheets[src][k + 1][i_src] = note
            n += 1
            stat[act] += 1
        out = openpyxl.Workbook()
        first = out.active
        first.title = main
        for j, name in enumerate([main] + [s for s in sheets if s != main]):
            ws = first if j == 0 else out.create_sheet(name)
            for row in sheets[name]:
                ws.append(row)
        out.save(path)
        print("[写回] 更新 %d 行(厚度 + 尺寸规格的高) 动作分布=%s" % (n, dict(stat)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
