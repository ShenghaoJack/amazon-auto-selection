# -*- coding: utf-8 -*-
"""CLI: 重测「尺寸规格」——以**图片里的真实尺寸图** + 标题/五点描述为准(2026-09-24 第五轮)。

用户口径
========
"尺寸规格都按真实尺寸图来改正, 表中的**商品尺寸列不准确, 不能用来作为依据**"。

所以本工具**完全不读** `商品尺寸` / `商品尺寸（单位换算）` / `详细参数` 的 `Product Dimensions`
(这三个是同一份不可靠数据), 只从两处取:
  ① **图片**: 尺寸标注图 / 规格表 / 参数小字(A+ 图文里最常见) —— 用视觉 + OCR 读;
  ② **文本**: 标题 / SKU / 五点描述 里逐件列出的尺寸(如 `extra large 18"x12", large 15"x10"`)。

合并: 图上真读到 → 用图上的; 否则用文本的; 都没有 → `缺`。
高度(第三维)与已核实的「厚度」列**保持一致**(厚度是本项目逐行核实过的权威值)。
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

from core.config import Config                                        # noqa: E402
from core.llm import LLMClient                                        # noqa: E402
from tools.attr_stability.thickness import (                          # noqa: E402
    ThicknessRefiner, _vision_with_fallback,
)
from tools.attr_stability.size_thickness_sync import (                # noqa: E402
    parse_cm, MISSING, parse_size_spec,
)
from tools.attr_stability.text_llm import quote_in_text, run_batches   # noqa: E402
from tools.attr_stability.vision_json import unwrap_vision            # noqa: E402

STEP17 = "17_属性标注"
STD = "18_标准化"
OUT_NAME = "尺寸规格-重测.json"
CKPT = "尺寸规格-重测.checkpoint.json"

# 只读这几个字段 —— **绝不**把 商品尺寸 / 详细参数 喂进去
TEXT_FIELDS = ("标题", "SKU", "五点描述")

SIZE_TEXT_CORE = """你是亚马逊砧板类目(Cutting Boards)的数据校对员。
下面是每行商品的 **标题 / SKU / 五点描述**(注意: **没有**给"商品尺寸"字段 —— 那个字段不准, 不许引用它)。
请从这些文字里读出**每一件砧板**的长 × 宽( × 高)。

要求
1. 逐件读出: 如 `large (15"x10") & medium (12"x8")` → 两件; `24 inch x 18 inch x 1/2 inch` → 一件三维。
2. 单位一律换算成 **cm**(1 inch = 2.54 cm, 1 mm = 0.1 cm), 保留 2 位小数以内。
3. 只写了 `Large/Medium/Small` 而没给数字 → 那件填 `未明确`。
4. `原文` 必须是标题/SKU/五点里的**原句片段, 照抄**。
5. 文字里确实没有尺寸, 就整体填 `未明确`。

输出严格 JSON:
{"rows": [{"xl_row": <行号>,
  "尺寸": [{"名称": "<大号/中号/小号/XL…; 单件留空>", "长": <cm 数值>, "宽": <cm 数值>, "高": <cm 数值或 null>}],
  "原文": "<照抄>"}]}
"""

SIZE_VISION_CORE = """你是亚马逊砧板类目(Cutting Boards)的数据校对员。
请**看图**回答: 这一行商品**每一件砧板**的长 × 宽( × 高) 是多少。

要求
1. 优先读图上的**尺寸标注图 / 规格表 / 参数小字(OCR)**: `38cm/14.96inch`、`20"x14"`、`33x24x1.5cm`、
   `Extra Large: 17" x 12"`。这类图常在 **A+ 图文**或第 4~7 张详情图, 请**每张都看**。
2. 图上给了**多件**尺寸就**每件都列**, 名称照图上的(如 large/medium/small 或 大号/中号)。
3. 单位换算成 **cm**(1 inch = 2.54 cm, 1 mm = 0.1 cm), 保留 2 位小数以内。
4. 图上没有尺寸标注 → `尺寸` 填空数组, `图上是否有尺寸标注` 填 `否`。
   **不要**用"看起来多大"来估, 不要猜。
5. `依据` 写清第几张图、图上原文数字。

输出严格 JSON:
{"xl_row": <行号>, "尺寸": [{"名称": "", "长": 0, "宽": 0, "高": null}],
 "依据": "...", "图上是否有尺寸标注": "是/否"}
"""


SIZE_TEXT2_CORE = """你是亚马逊砧板类目(Cutting Boards)的数据校对员。
下面每行的资料里, 常规抽取**没找到**砧板的长宽。请你再仔细找一遍。

**只用** 标题 / SKU / 五点描述 / 详细参数 里**除 `Product Dimensions` 之外**的文字
(⚠ `Product Dimensions` / `商品尺寸` 是**不可靠字段, 禁止使用**, 见了也要跳过)。
可以看的例如: `Size: 17"L x 12"W`、`XL size (17"x12")`、包装上印的 `18"X12"`、`Item Dimensions: ...`。

要求
1. 逐件读出长 × 宽( × 高); 单位一律换算成 **cm**(1 inch = 2.54 cm, 1 mm = 0.1 cm)。
2. 数字没写单位时: 若同一段里出现过 inch/" 就按 inch, 出现过 cm 就按 cm;
   **两者都没有时不要猜**, 该件填 `未明确`。
3. 名称照原文(如 XL / large / 大号)。
4. `原文` 照抄原句片段。
5. 确实找不到就 `尺寸` 给空数组。

输出严格 JSON:
{"rows": [{"xl_row": <行号>,
  "尺寸": [{"名称": "", "长": <cm>, "宽": <cm>, "高": <cm 或 null>}],
  "原文": "<照抄>"}]}
"""

SIZE_TEXT2_FIELDS = ("标题", "SKU", "五点描述", "详细参数")


class _Ctx:
    def __init__(self, config):
        self.config = config
        self._llm = None

    def llm(self):
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm


# ---------------------------------------------------------------- 正则兜底
RE_PAIR = re.compile(
    r'(\d+(?:\.\d+)?)\s*(?P<u1>"|\u201d|\u2033|\u82f1\u5bf8|inch(?:es)?|in\b|cm|mm|\u5398\u7c73|\u6beb\u7c73)?'
    r'\s*[x\u00d7*]\s*'
    r'(\d+(?:\.\d+)?)\s*(?P<u2>"|\u201d|\u2033|\u82f1\u5bf8|inch(?:es)?|in\b|cm|mm|\u5397\u7c73|\u5398\u7c73|\u6beb\u7c73)?',
    re.I)
_LABEL_WORDS = [
    (re.compile(r'extra\s+extra\s+large|xxl', re.I), "超大号"),
    (re.compile(r'extra\s+large|xl\b', re.I), "特大号"),
    (re.compile(r'\blarge\b|\bl\b', re.I), "大号"),
    (re.compile(r'\bmedium\b|\bm\b', re.I), "中号"),
    (re.compile(r'\bsmall\b|\bs\b', re.I), "小号"),
    (re.compile(r'\bmini\b', re.I), "迷你"),
]


def _unit_cm(tok: str, ctx: str) -> float | None:
    s = (tok or "").lower()
    if not s:
        # 数字旁没有单位 → 看上下文(±24 字)找单位; 还是找不到就不猜
        s = (ctx or "").lower()
    if any(k in s for k in ('"', "\u201d", "\u2033", "inch", "\u82f1\u5bf8")) or \
            re.search(r'\bin\b', s):
        return 2.54
    if "mm" in s or "\u6beb\u7c73" in s:
        return 0.1
    if "cm" in s or "\u5398\u7c73" in s:
        return 1.0
    return None


def regex_sizes(row, limit: int = 6) -> list[dict]:
    """从 标题/SKU/五点 里直接抠 `长 x 宽`(带单位上下文) —— 模型漏抽时的兜底。"""
    out = []
    for f in TEXT_FIELDS:
        t = str(row.get(f) or "")
        for m in RE_PAIR.finditer(t):
            ctx = t[max(0, m.start() - 24):m.end() + 24]
            f1 = _unit_cm(m.group("u1"), ctx)
            f2 = _unit_cm(m.group("u2"), ctx)
            unit = f1 or f2
            if unit is None:
                continue
            try:
                l = float(m.group(1)) * unit
                w = float(m.group(3)) * unit
            except (TypeError, ValueError):
                continue
            if not (3 <= l <= 200 and 3 <= w <= 200):
                continue
            lab = ""
            pre = t[max(0, m.start() - 26):m.start()]
            for rx, name in _LABEL_WORDS:
                if rx.search(pre):
                    lab = name
            out.append({"名称": lab, "长": round(l, 2), "宽": round(w, 2), "高": None})
    # 去重(同尺寸只留一条)
    seen, uniq = set(), []
    for s in out:
        k = (round(s["长"], 1), round(s["宽"], 1))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)
    return uniq[:limit]


_LABEL_CN = {
    "extra extra large": "超大号", "xxl": "超大号", "extra large": "特大号", "xl": "特大号",
    "large": "大号", "l": "大号", "medium": "中号", "m": "中号", "small": "小号", "s": "小号",
    "mini": "迷你", "默认": "", "default": "",
}


def cn_label(name: str) -> str:
    """把模型给的英文尺寸名统一成中文写法(大号/中号/小号…), 保持列内一致。"""
    s = str(name or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low in _LABEL_CN:
        return _LABEL_CN[low]
    for k, v in _LABEL_CN.items():
        if k and k in low:
            return v
    return s[:12]


def _fmt(x) -> str:
    if x is None:
        return MISSING
    v = round(float(x), 2)
    return "%g" % v


def _fmt_h(x) -> str:
    """高度与「厚度」列**逐字符一致** → 保留 4 位(厚度列就是 `%g` + 4 位)。"""
    if x is None:
        return MISSING
    return "%g" % round(float(x), 4)


def _norm_sizes(raw) -> list[dict]:
    """把模型返回的 `尺寸` 规范成 `[{名称, 长, 宽, 高}]`(cm, 过滤不合理值)。"""
    out = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return out
    for it in raw:
        if not isinstance(it, dict):
            continue
        def num(*keys):
            for k in keys:
                v = it.get(k)
                if v is None or v == "":
                    continue
                m = re.search(r'(\d+(?:\.\d+)?)', str(v))
                if m:
                    try:
                        return float(m.group(1))
                    except ValueError:
                        continue
            return None
        l, w, h = num("长", "length", "L"), num("宽", "width", "W"), num("高", "height", "H")
        if not l or not w:
            continue
        if not (3 <= l <= 200 and 3 <= w <= 200):
            continue
        if h is not None and not (0.02 <= h <= 20):
            h = None
        out.append({"名称": str(it.get("名称") or it.get("name") or "").strip(),
                    "长": l, "宽": w, "高": h})
    return out


def _render(sizes: list[dict], thick) -> str:
    """渲染成既有写法: `大号:38.1x25.4x1.5 cm,...`; 单件不带标签。"""
    if not sizes:
        return MISSING
    multi = len(sizes) > 1
    parts = []
    for s in sizes:
        h = s.get("高")
        if h is None:
            h = thick
        body = "%sx%sx%s" % (_fmt(s["长"]), _fmt(s["宽"]), _fmt_h(h))
        lab = cn_label(s.get("名称"))
        parts.append(("%s:%s cm" % (lab, body)) if (multi and lab) else ("%s cm" % body))
    return ",".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按图片尺寸图重测尺寸规格")
    ap.add_argument("--cat", required=True)
    ap.add_argument("--run-text", action="store_true", help="跑文本抽取(标题/SKU/五点)")
    ap.add_argument("--run-text2", action="store_true",
                    help="第二轮: 只对两层都没找到尺寸的行做聚焦复核(禁用 Product Dimensions)")
    ap.add_argument("--run-vision", action="store_true", help="跑图片尺寸图识别")
    ap.add_argument("--only", default="", help="只跑指定 ASIN(逗号分隔, 调试用)")
    ap.add_argument("--apply", action="store_true", help="合并并写回交付表")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)

    cat_dir = os.path.join(_ROOT, "data", args.cat)
    s17 = os.path.join(cat_dir, STEP17)
    rows = json.load(open(os.path.join(s17, "input.json"), encoding="utf-8"))["rows"]
    only = {a.strip() for a in args.only.split(",") if a.strip()}
    if only:
        rows = [r for r in rows if str(r.get("变体ASIN") or "") in only]
    by_key = {}
    for i, r in enumerate(rows, start=2):
        try:
            by_key[int(r.get("xl_row"))] = r
        except (TypeError, ValueError):
            by_key[i] = r
    print("待处理 %d 行" % len(by_key))
    if not (args.run_text or args.run_text2 or args.run_vision or args.apply):
        print("(加 --run-text / --run-text2 / --run-vision / --apply)")
        return 0

    out_path = os.path.join(s17, OUT_NAME)
    store = {"text": {}, "vision": {}}
    if os.path.exists(out_path):
        try:
            store.update(json.load(open(out_path, encoding="utf-8")))
        except (OSError, ValueError):
            pass
    cfg = Config.load(os.path.join(cat_dir, "config.yaml"))
    ctx = _Ctx(cfg)

    # ---------------- 文本层
    if args.run_text:
        items = [by_key[k] for k in sorted(by_key)]

        def _build(batch):
            out = [SIZE_TEXT_CORE, "", "=" * 60, "## 本次待判定的行", ""]
            for r in batch:
                out.append("### xl_row=%s" % r.get("xl_row"))
                for f in TEXT_FIELDS:
                    v = r.get(f)
                    if v:
                        s = str(v)
                        out.append("- %s: %s" % (f, s[:1300] + ("…" if len(s) > 1300 else "")))
                out.append("")
            return "\n".join(out)

        def _accept(rec):
            xl = rec.get("xl_row")
            try:
                row = by_key[int(xl)]
            except (KeyError, TypeError, ValueError):
                return False
            sizes = _norm_sizes(rec.get("尺寸"))
            if sizes and not quote_in_text(rec.get("原文"),
                                          [row.get(f) for f in TEXT_FIELDS], min_len=4):
                rec["尺寸"] = []
                rec["_rejected"] = "引用未通过"
            else:
                rec["尺寸"] = sizes
            return True

        print("[文本] 抽取标题/SKU/五点里的尺寸 ...")
        tr = run_batches(ctx, items, _build, accept=_accept,
                         key_of=lambda r: str(r.get("xl_row")),
                         batch=8, workers=args.workers, label="尺寸-文本",
                         ckpt_path=os.path.join(s17, CKPT), plan_key="size-text|%d" % len(items))
        store["text"] = dict(store.get("text") or {}, **{
            k: {"尺寸": _norm_sizes(v.get("尺寸")), "原文": str(v.get("原文") or "")}
            for k, v in tr.items()})
        n = sum(1 for v in store["text"].values() if v["尺寸"])
        print("文本层: %d/%d 行有尺寸" % (n, len(items)))

    # ---------------- 图片层
    if args.run_vision:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        todo = [k for k in sorted(by_key)]
        done = store.get("vision") or {}
        todo = [k for k in todo if str(k) not in done]
        print("[图片] 识别尺寸图: %d 行(已缓存 %d)" % (len(todo), len(done)))
        t0 = __import__("time").time()

        def _one(xl):
            row = by_key.get(xl)
            if not row:
                return None
            urls = ThicknessRefiner.row_image_urls(row, 6, 4)
            if not urls:
                return {"xl_row": xl, "vision": {"尺寸": [], "图上是否有尺寸标注": "无图片"}}
            ev = "\n".join("- %s: %s" % (f, str(row.get(f))[:500])
                           for f in ("标题", "五点描述") if row.get(f))
            p = (SIZE_VISION_CORE
                 + "\n\n## 本行文字资料(仅供互证; 图上没有标注就填 否, 不要照抄这里的数字)\n"
                 + ev + "\n\n共 %d 张本行图片(主图在前, 其后 A+ 图文)。" % len(urls))
            r, _e = _vision_with_fallback(ctx, p, urls, detail="high", max_tokens=1100)
            u = unwrap_vision(r, ("尺寸", "图上是否有尺寸标注", "依据")) or {}
            return {"xl_row": xl, "vision": {"尺寸": _norm_sizes(u.get("尺寸")),
                                             "图上是否有尺寸标注": str(u.get("图上是否有尺寸标注") or ""),
                                             "依据": str(u.get("依据") or "")[:300]}}

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_one, k): k for k in todo}
            n = 0
            for fut in as_completed(futs):
                r = fut.result()
                n += 1
                if r:
                    done[str(r["xl_row"])] = r["vision"]
                if n % 25 == 0 or n == len(todo):
                    print("   图片 %d/%d (%.0fs) 累计有尺寸 %d"
                          % (n, len(todo), __import__("time").time() - t0,
                             sum(1 for v in done.values() if v.get("尺寸"))))
                if n % 100 == 0:
                    _save(out_path, store, done)
        store["vision"] = done
        n = sum(1 for v in done.values() if v.get("尺寸"))
        print("图片层: %d/%d 行从图上读到尺寸" % (n, len(done)))

    # ---------------- 文本层(第二轮: 只查两层都没找到的行)
    if args.run_text2:
        items = []
        for k in sorted(by_key):
            if (store.get("text") or {}).get(str(k), {}).get("尺寸"):
                continue
            if (store.get("vision") or {}).get(str(k), {}).get("尺寸"):
                continue
            items.append(by_key[k])
        print("[文本2] 聚焦复核 %d 行(两层都没找到尺寸)" % len(items))

        def _build2(batch):
            out = [SIZE_TEXT2_CORE, "", "=" * 60, "## 本次待判定的行", ""]
            for r in batch:
                out.append("### xl_row=%s" % r.get("xl_row"))
                for f in SIZE_TEXT2_FIELDS:
                    v = r.get(f)
                    if v:
                        s = str(v)
                        out.append("- %s: %s" % (f, s[:1100] + ("…" if len(s) > 1100 else "")))
                out.append("")
            return "\n".join(out)

        def _accept2(rec):
            xl = rec.get("xl_row")
            try:
                row = by_key[int(xl)]
            except (KeyError, TypeError, ValueError):
                return False
            sizes = _norm_sizes(rec.get("尺寸"))
            if sizes and not quote_in_text(rec.get("原文"),
                                          [row.get(f) for f in SIZE_TEXT2_FIELDS], min_len=4):
                rec["尺寸"] = []
            else:
                rec["尺寸"] = sizes
            return True

        tr2 = run_batches(ctx, items, _build2, accept=_accept2,
                          key_of=lambda r: str(r.get("xl_row")),
                          batch=6, workers=args.workers, label="尺寸-文本2",
                          ckpt_path="", plan_key="")
        merged = dict(store.get("text") or {})
        for k, v in tr2.items():
            if _norm_sizes(v.get("尺寸")):
                merged[k] = {"尺寸": _norm_sizes(v.get("尺寸")),
                             "原文": str(v.get("原文") or "")}
        store["text"] = merged
        print("文本2: 新补 %d 行" % (len(merged) - (len(tr2) - sum(
            1 for v in tr2.values() if _norm_sizes(v.get("尺寸"))))))

    _save(out_path, store, store.get("vision") or {})
    print("已写出 %s" % out_path)

    # ---------------- 合并 + 写回
    if args.apply:
        return _apply(cat_dir, args.cat, s17, store, by_key)
    return 0


def _save(path: str, store: dict, vision: dict) -> None:
    store["vision"] = vision
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _apply(cat_dir: str, cat: str, s17: str, store: dict, by_key: dict) -> int:
    import openpyxl
    import shutil
    path = os.path.join(cat_dir, STD, "%s-属性标注-标准化交付.xlsx" % cat)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = {n: [list(r) for r in wb[n].iter_rows(values_only=True)] for n in wb.sheetnames}
    wb.close()
    main = "属性标注" if "属性标注" in sheets else list(sheets)[0]
    src = "来源" if "来源" in sheets else ""
    hdr = sheets[main][0]
    i_z, i_t, ia = hdr.index("尺寸规格"), hdr.index("厚度"), hdr.index("变体ASIN")
    i_src = sheets[src][0].index("尺寸规格") if src and "尺寸规格" in sheets[src][0] else None
    by_asin = {str(sheets[main][k][ia] or ""): k for k in range(1, len(sheets[main]))}

    text, vision = store.get("text") or {}, store.get("vision") or {}
    stat = collections.Counter()
    plan = {}
    for xl, row in by_key.items():
        asin = str(row.get("变体ASIN") or "")
        k = by_asin.get(asin)
        if k is None:
            continue
        cur_th = str(sheets[main][k][i_t] or "").strip()
        thick = parse_cm(cur_th)
        v = (vision.get(str(xl)) or {})
        t = (text.get(str(xl)) or {})
        vs, ts = v.get("尺寸") or [], t.get("尺寸") or []
        # 图上有尺寸标注 → 以图为准; 但**图上只标了一件而文本逐件列全**时, 用文本(别丢件)
        if vs and (not ts or len(vs) >= len(ts)):
            sizes, srcname = vs, "图片尺寸图/OCR: " + str(v.get("依据") or "")[:150]
            stat["图片"] += 1
        elif vs and ts:
            sizes, srcname = ts, "文本(标题/SKU/五点; 图上只标一件): " + str(t.get("原文") or "")[:110]
            stat["文本(图上不全)"] += 1
        elif ts:
            sizes, srcname = ts, "文本(标题/SKU/五点): " + str(t.get("原文") or "")[:120]
            stat["文本"] += 1
        else:
            # 模型漏抽 → 正则兜底(带单位上下文)
            rs = regex_sizes(row)
            if rs:
                sizes, srcname = rs, "文本正则兜底(标题/SKU/五点里的 LxW)"
                stat["文本兜底"] += 1
            else:
                plan[xl] = {"值": MISSING, "来源": "图片与标题/五点均无尺寸标注 → 记「缺」"}
                stat["缺"] += 1
                continue
        # 高度与已核实的「厚度」列保持一致(厚度是本项目逐行核实过的权威值)
        per_th = None
        if "," in cur_th:
            per_th = [parse_cm(q.split(":")[-1]) for q in re.split(r'[,，]', cur_th) if q.strip()]
        if per_th and len(per_th) == len(sizes):
            for s, th in zip(sizes, per_th):
                s["高"] = th
        elif thick is not None:
            for s in sizes:
                s["高"] = thick
        val = _render(sizes, thick)
        plan[xl] = {"值": val, "来源": srcname}
        stat["有值"] += 1

    print("合并结果:", dict(stat))
    for xl, p in list(plan.items())[:12]:
        print("   %-12s %s → %s" % (by_key[xl].get("变体ASIN"),
                                    str(sheets[main][by_asin.get(str(by_key[xl].get('变体ASIN') or ''), 0)][i_z] or "")[:26],
                                    p["值"][:60]))

    bak = path + ".bak-before-size.xlsx"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    n = 0
    for xl, p in plan.items():
        k = by_asin.get(str(by_key[xl].get("变体ASIN") or ""))
        if k is None:
            continue
        if str(sheets[main][k][i_z] or "").strip() != p["值"]:
            sheets[main][k][i_z] = p["值"]
            n += 1
        if i_src is not None:
            sheets[src][k][i_src] = p["来源"]
    out = openpyxl.Workbook()
    first = out.active
    first.title = main
    for j, name in enumerate([main] + [s for s in sheets if s != main]):
        ws = first if j == 0 else out.create_sheet(name)
        for row in sheets[name]:
            ws.append(row)
    out.save(path)
    print("[写回] 尺寸规格改动 %d 行" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
