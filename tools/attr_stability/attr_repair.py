# -*- coding: utf-8 -*-
"""交付表列级修复: 「主体材质」归一 + 「产品类型」对齐 + 空白格补全(文字 + 图片视觉)。

用户口径(2026-09-24 追加)
==========================
1. **材质归一**(作用于全部行, 带 `+` 的单元格只换里面的材质名、`+` 格式不变):

   | 现值 | 归一为 |
   |---|---|
   | 相思木/柚木/枫木/桧木/橡胶木/胡桃木/核桃木/乌木/山毛榉/樱桃木/橄榄木/实木/木纤维/木材 | **木制** |
   | PP塑料/塑料/小麦秸秆PP/TPU/亚克力/树脂 | **塑料** |
   | 304不锈钢/不锈钢 | **不锈钢** |
   | 钢化玻璃 | **玻璃** |
   | 大理石/花岗岩 | **石材** |
   | 竹木 | **竹制** |
   | 钛金属 | **钛** |
   | 硅胶 | 硅胶(不变) |

2. **逐行核实材质真伪, 再按材质对齐「产品类型」**。实测有 **361 行**「主体材质含钛金属、
   产品类型却写不锈钢砧板」(listing 标题基本是 Titanium / 100% Pure Titanium) —— 这类必须逐行
   看文本/图片定案, 不能靠猜。
3. **空白格补全**: 只补**真正的空值**(不含「未明确」)——「套装数量/尺寸规格/主体材质/产品类型」。

为什么"材质真伪"要用**文本优先 + 引用核验**
============================================
`详细参数` 里的 `Material:` 字段与标题是卖家自己的声明, 比看图猜材质可靠得多;
但模型也可能瞎写, 所以要求它给出**原文片段**, 由 `text_llm.quote_in_text` 在该行资料里核验。
文字判不出时才走图片视觉。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping, Optional, Sequence

from .text_llm import quote_in_text, run_batches
from .thickness import ThicknessRefiner, _vision_with_fallback

__all__ = [
    "MATERIAL_MAP", "TYPE_BY_MATERIAL", "FORM_TYPES", "normalize_material",
    "material_group", "align_type", "AttrRepair", "REPAIR_NAME",
]

# 材质归一表(键 = 旧写法, 值 = 归一名)
MATERIAL_MAP: dict[str, str] = {
    # 木种 → 木制
    "相思木": "木制", "柚木": "木制", "枫木": "木制", "桧木": "木制", "橡胶木": "木制",
    "胡桃木": "木制", "核桃木": "木制", "乌木": "木制", "山毛榉": "木制", "樱桃木": "木制",
    "橄榄木": "木制", "实木": "木制", "木纤维": "木制", "复合木纤维": "木制", "木材": "木制",
    "松木": "木制", "沙比利木": "木制", "黑胡桃": "木制",
    # 塑料族 → 塑料
    "PP塑料": "塑料", "塑料": "塑料", "小麦秸秆PP": "塑料", "TPU": "塑料",
    "亚克力": "塑料", "树脂": "塑料", "聚丙烯": "塑料", "ABS": "塑料",
    # 金属
    "304不锈钢": "不锈钢", "316不锈钢": "不锈钢", "不锈钢": "不锈钢",
    "钛金属": "钛", "钛": "钛",
    # 玻璃 / 石材
    "钢化玻璃": "玻璃", "玻璃": "玻璃",
    "大理石": "石材", "花岗岩": "石材",
    # 竹 / 硅胶
    "竹木": "竹制", "竹制": "竹制", "竹": "竹制",
    "硅胶": "硅胶",
}

# 材质 → 「产品类型」里对应的**材质型**取值
TYPE_BY_MATERIAL: dict[str, str] = {
    "木制": "实木砧板",
    "竹制": "竹制砧板",
    "不锈钢": "不锈钢砧板",
    "钛": "钛金属砧板",
    "塑料": "塑料砧板",
    "玻璃": "玻璃砧板",
    "石材": "石材砧板",
    "硅胶": "塑料砧板",
}
# **形态型**产品类型: 与材质无关, 对齐时不动
FORM_TYPES = ("拼盘托盘", "带标识分类砧板")
# 塑料系的两种形态: 已经写着就不动(软垫 vs 硬板 是形态差异)
PLASTIC_FORMS = ("塑料砧板", "塑料软砧板垫")

REPAIR_NAME = "属性修复-材质与空白.json"
REPAIR_CKPT = "属性修复.checkpoint.json"

# 材质分组: 用于判断「产品类型」与「主体材质」是否矛盾
_TYPE_GROUP = {
    "实木砧板": "木制", "竹制砧板": "竹制", "不锈钢砧板": "不锈钢",
    "钛金属砧板": "钛", "塑料砧板": "塑料", "塑料软砧板垫": "塑料",
    "玻璃砧板": "玻璃", "石材砧板": "石材",
}
# 「产品类型」**可接受**的材质主分组。硅胶是塑料族的软垫料, 与塑料砧板/软垫不冲突。
_TYPE_ACCEPTS = {
    "实木砧板": {"木制"},
    "竹制砧板": {"竹制"},
    "不锈钢砧板": {"不锈钢"},
    "钛金属砧板": {"钛"},
    "塑料砧板": {"塑料", "硅胶"},
    "塑料软砧板垫": {"塑料", "硅胶"},
    "玻璃砧板": {"玻璃"},
    "石材砧板": {"石材"},
}


def type_conflicts(material_norm: Any, product_type: Any) -> bool:
    """「产品类型」点名的材质族 与「主体材质」是否真矛盾(材质未明确时不判矛盾)。"""
    acc = _TYPE_ACCEPTS.get(str(product_type or "").strip())
    if not acc:
        return False
    g = material_group(material_norm)
    if not g or g == "未明确":
        return False
    return g not in acc


def normalize_material(value: Any) -> str:
    """按归一表把材质名归一; **保留 `+` 结构**(`A+B` → 归一后仍 `A+B`, 重复项去重)。"""
    s = str(value or "").strip()
    if not s or s in ("未明确", "None"):
        return "" if not s or s == "None" else "未明确"
    parts = [p.strip() for p in re.split(r'[+\uff0b]', s) if p.strip()]
    out: list[str] = []
    for p in parts:
        t = MATERIAL_MAP.get(p)
        if t is None:
            # 兜底: 含"木"→木制、含"PP"/"塑料"→塑料(覆盖未列举的写法)
            if any(k in p for k in ("木",)):
                t = "竹制" if "竹" in p else "木制"
            elif any(k in p for k in ("PP", "塑料", "亚克力", "树脂", "TPU")):
                t = "塑料"
            elif "不锈钢" in p:
                t = "不锈钢"
            elif "钛" in p:
                t = "钛"
            elif "玻璃" in p:
                t = "玻璃"
            elif any(k in p for k in ("大理石", "花岗岩", "石")):
                t = "石材"
            else:
                t = p
        if t not in out:
            out.append(t)
    return "+".join(out)


def material_group(value: Any) -> str:
    """取材质的主分组(第一段), 用于和「产品类型」比对。"""
    norm = normalize_material(value)
    if not norm:
        return ""
    return norm.split("+")[0]


def type_group(product_type: Any) -> str:
    """「产品类型」隐含的材质分组; 形态型/未知返回 ""。"""
    return _TYPE_GROUP.get(str(product_type or "").strip(), "")


def align_type(material_norm: str, current_type: Any, flexible: bool = False) -> str:
    """按**已归一的材质**给「产品类型」。形态型(拼盘托盘/带标识分类砧板)与塑料两种形态保持不变。

    硅胶是塑料族的软垫料: 当前已是 `塑料砧板`/`塑料软砧板垫` 时**保持原形态**, 不降级成硬板。
    """
    cur = str(current_type or "").strip()
    if cur in FORM_TYPES:
        return cur
    grp = material_group(material_norm)
    if not grp:
        return cur
    if grp in ("塑料", "硅胶") and cur in PLASTIC_FORMS:
        return cur
    if flexible and grp in ("塑料", "硅胶"):
        return "塑料软砧板垫"
    if grp == "硅胶":
        return cur or "塑料软砧板垫"
    return TYPE_BY_MATERIAL.get(grp, cur)


# ------------------------------------------------------------------ 提示词
REPAIR_CORE = """你是亚马逊砧板类目(Cutting Boards)的属性审校员。
下面每行是一件砧板商品的**文字资料**。请依据资料回答下面几个字段。

## 字段与口径

**主体材质**: 砧板**本体(两个切面)**的材质, 从
`木制 / 竹制 / 塑料 / 不锈钢 / 钛 / 玻璃 / 石材 / 硅胶` 里选;
只有**两个切面材质不同**时才写 `A+B`(主材质在前); **配件材质不算**
(塑料垫/抽拉托盘/收纳架盒/盖子/硅胶包边/不锈钢钢条都属配件)。
注意区别 **titanium(钛)** 与 **stainless steel(不锈钢)** —— 写 Titanium / 纯钛 的选「钛」, 不要选不锈钢。
`详细参数` 里的 `Material:` 字段与标题是最可靠的依据。

**产品类型**: 从 `实木砧板 / 竹制砧板 / 不锈钢砧板 / 钛金属砧板 / 塑料砧板 /
塑料软砧板垫 / 玻璃砧板 / 石材砧板 / 拼盘托盘 / 带标识分类砧板` 里选。
软性可卷的砧板垫选「塑料软砧板垫」; 摆盘/奶酪拼盘选「拼盘托盘」; 带食材分类标识的选「带标识分类砧板」。
**产品类型必须与主体材质一致**(材质是钛就不能选不锈钢砧板)。

**套装数量**: 这套是几件, 写成 `1件` / `2件套` / `3件套` …(数的是**砧板**件数, 不含配件)。

**尺寸规格**: 各件砧板的 `长x宽x高`, 单位 **cm**(1 inch = 2.54 cm, 1 mm = 0.1 cm)。
单件直接写 `38.1x25.4x1.3 cm`; 多件写 `大号:40.6x28.4x0.94 cm,中号:35.1x24.4x0.94 cm`。
没有高度数据时那一位写 `缺`。**不要**把 inch/mm 数字直接写成 cm。

## 输出要求
- 每个字段都要给 `原文`(资料里的原句片段, **照抄不要改写**); 判不出就把值写 `未明确`。
- 输出严格 JSON(不要输出其它文字):
{"rows": [{"xl_row": <行号>,
           "主体材质": "<值>", "主体材质_原文": "<照抄>",
           "产品类型": "<值>", "产品类型_原文": "<照抄>",
           "套装数量": "<值>", "套装数量_原文": "<照抄>",
           "尺寸规格": "<值>", "尺寸规格_原文": "<照抄>"}]}
"""

REPAIR_FIELDS = ("标题", "亮点", "五点描述", "SKU", "详细参数", "商品尺寸", "商品尺寸（单位换算）")


def build_repair_prompt(batch: Sequence[Mapping[str, Any]], max_chars: int = 1300) -> str:
    out = [REPAIR_CORE, "", "=" * 60, "## 本次待判定的行", ""]
    for r in batch:
        out.append("### xl_row=%s" % r.get("xl_row"))
        for c in REPAIR_FIELDS:
            v = r.get(c)
            if not v:
                continue
            s = str(v)
            if len(s) > max_chars:
                s = s[:max_chars] + "…"
            out.append("- %s: %s" % (c, s))
        out.append("")
    return "\n".join(out)


REPAIR_VISION_CORE = """你是亚马逊砧板类目(Cutting Boards)的属性审校员。
任务: 对下面这一行商品, **结合图片**给出字段(只给下面列出的字段, 其余留空)。

口径
· **主体材质**: 砧板**本体两个切面**的材质, 从 `木制 / 竹制 / 塑料 / 不锈钢 / 钛 / 玻璃 / 石材 / 硅胶` 选;
  两面材质不同才写 `A+B`; **配件不算**(垫/托盘/架/盒/盖/包边/钢条)。
  注意区分 **titanium(钛)** 与 **stainless steel(不锈钢)** —— 看标题/图上小字, 别猜。
· **产品类型**: `实木砧板/竹制砧板/不锈钢砧板/钛金属砧板/塑料砧板/塑料软砧板垫/玻璃砧板/石材砧板/拼盘托盘/带标识分类砧板`。
· **套装数量**: 数**砧板**件数(看图上到底几块板), 写 `1件`/`3件套`…
· **尺寸规格**: 各件 `长x宽x高` cm; 图上若标 inch/mm 要换算; 单位缺失的那位写 `缺`。
· 判不出就写 `未明确`, **不要猜**; 但图上/文本里明确有的信息必须用上。

输出严格 JSON:
{"xl_row": <行号>, "主体材质": "", "产品类型": "", "套装数量": "", "尺寸规格": "", "依据": "<图上/文本看到的>"}
"""


SIZE_DETAIL_CORE = """你是亚马逊砧板类目(Cutting Boards)的数据校对员。
这件商品是**多件套**。请依据下面的文字资料, 给出**每一件**砧板的长x宽x高。

要求
1. 单位一律 **cm**(1 inch = 2.54 cm, 1 mm = 0.1 cm); 原文是 inch/mm 的必须换算。
   **注意**: `详细参数`/`商品尺寸` 里那一组常常是**整套最大件或包装尺寸**, 不是每件;
   要优先采用 标题/五点描述 里**逐个列出**的每件尺寸(如 `extra large 18"x12", large 15"x10"`)。
2. 按从大到小写成 `大号:长x宽x高 cm,中号:长x宽x高 cm,小号:长x宽x高 cm`。
   缺少高度的位写 `缺`(如 `大号:45.7x30.5x缺 cm`)。
3. 原文里确实没有逐件尺寸时, 值填 `未明确`。
4. `原文` 必须是资料里的原句片段, **照抄不要改写**。

输出严格 JSON:
{"rows": [{"xl_row": <行号>, "尺寸规格": "<值>", "原文": "<照抄>"}]}
"""


def build_size_detail_prompt(batch: Sequence[Mapping[str, Any]], set_counts: Mapping[Any, Any],
                             max_chars: int = 1300) -> str:
    out = [SIZE_DETAIL_CORE, "", "=" * 60, "## 本次待判定的行", ""]
    for r in batch:
        out.append("### xl_row=%s（%s 件套）" % (r.get("xl_row"),
                                                str(set_counts.get(r.get("xl_row"), "")).replace("件套", "")))
        for c in REPAIR_FIELDS:
            v = r.get(c)
            if not v:
                continue
            s = str(v)
            if len(s) > max_chars:
                s = s[:max_chars] + "…"
            out.append("- %s: %s" % (c, s))
        out.append("")
    return "\n".join(out)


def _set_num(v: Any) -> Optional[int]:
    """从 `3件套` / `5 件(标题/SKU 推定)` / `3` 里取件数。"""
    m = re.search(r'(\d+)', str(v or ""))
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n if 1 <= n <= 200 else None


class AttrRepair:
    """跑一遍「材质核实 + 产品类型对齐 + 空白补全」并落盘。"""

    def __init__(self, ctx, *, log=None):
        self.ctx = ctx
        self._log = log or (lambda m: None)
        cfg = ctx.config
        self.workers = int(cfg.get("REPAIR_WORKERS") or cfg.get("ATTR_STAB_WORKERS") or 6)
        self.batch = int(cfg.get("REPAIR_BATCH") or 8)
        self.detail = str(cfg.get("REPAIR_VISION_DETAIL") or "high").strip().lower()
        self.max_images = int(cfg.get("REPAIR_MAX_IMAGES") or 4)
        self.aplus_images = int(cfg.get("REPAIR_APLUS_IMAGES") or 2)

    # ---------------------------------------------------------------- 规划
    @staticmethod
    def plan(declared: Mapping[Any, Mapping[str, Any]]) -> dict:
        """按现状决定每行要核实/补哪些字段。

        `declared`: {xl_row: {主体材质, 产品类型, 套装数量, 尺寸规格, 是否双面可用}}
        返回 `{xl_row: {"材质核实":bool, "产品类型":bool, "套装数量":bool, "尺寸规格":bool, "原因":str}}`
        """
        plan: dict = {}
        for xl, d in declared.items():
            m = str(d.get("主体材质") or "").strip()
            ty = str(d.get("产品类型") or "").strip()
            st = str(d.get("套装数量") or "").strip()
            sz = str(d.get("尺寸规格") or "").strip()
            need_m = False
            reasons = []
            if m in ("", "未明确", "None"):
                reasons.append("材质空白")
                # 类型点明了材质族, 但材质空缺/未明确 → 必须核实材质才能定类型
                if str(ty) in _TYPE_ACCEPTS:
                    need_m = True
                    reasons.append("类型(%s)需要材质佐证" % ty)
            if ty in ("", "None"):
                reasons.append("类型空白")
            # 材质/类型是否**真**矛盾
            if type_conflicts(m, ty):
                need_m = True
                reasons.append("类型(%s)与材质(%s)矛盾" % (ty, m))
            if not type_group(ty) and ty in ("砧板", ""):
                need_m = True
                reasons.append("类型过于笼统")
            if st in ("", "None"):
                reasons.append("套装数量空白")
            if sz in ("", "None"):
                reasons.append("尺寸规格空白")
            if reasons:
                plan[xl] = {
                    "材质核实": need_m,
                    "产品类型": (ty in ("", "None") or need_m),
                    "套装数量": st in ("", "None"),
                    "尺寸规格": sz in ("", "None"),
                    "原因": "、".join(reasons),
                }
        return plan

    # ---------------------------------------------------------------- 文本层
    def text_pass(self, rows: Sequence[Mapping[str, Any]], out_dir: str,
                  wanted: Mapping[Any, Mapping[str, Any]], *, progress: bool = True) -> dict:
        """文本 LLM: 对 `wanted` 里的行问 材质/类型/套装数量/尺寸规格, 并核验原文引用。"""
        items, by_key = [], {}
        for r in rows:
            try:
                xl = int(r.get("xl_row"))
            except (TypeError, ValueError):
                continue
            if xl not in wanted:
                continue
            items.append(r)
            by_key[str(xl)] = r
        if not items:
            return {}

        def _accept(rec: Mapping[str, Any]) -> bool:
            row = by_key.get(str(rec.get("xl_row")))
            if row is None:
                return False
            texts = [row.get(c) for c in REPAIR_FIELDS]
            # 每个字段独立核验引用; 核验不过的把该字段降级为"未明确"
            for f in ("主体材质", "产品类型", "套装数量", "尺寸规格"):
                val = str(rec.get(f) or "").strip()
                q = rec.get(f + "_原文")
                if not val or val in ("未明确", "None"):
                    rec[f] = "未明确"
                    continue
                if not quote_in_text(q, texts, min_len=4):
                    rec[f] = "未明确"
                    rec.setdefault("_rejected", []).append(f)
            return True

        recs = run_batches(
            self.ctx, items, build_repair_prompt, accept=_accept,
            key_of=lambda r: str(r.get("xl_row")),
            batch=self.batch, workers=self.workers, log=self._log, label="属性修复-文本",
            ckpt_path=os.path.join(out_dir, REPAIR_CKPT),
            plan_key="repair|%d|%d" % (len(items), self.batch))
        out = {}
        for k, rec in recs.items():
            try:
                out[int(k)] = rec
            except (TypeError, ValueError):
                continue
        self._log("属性修复-文本: %d 行有结论" % len(out))
        return out

    # ---------------------------------------------------------------- 视觉层
    def vision_pass(self, rows: Sequence[Mapping[str, Any]], out_dir: str,
                    need: Mapping[Any, Sequence[str]], text_recs: Mapping[Any, Mapping[str, Any]],
                    *, progress: bool = True) -> dict:
        """文字判不出的行 → 图片视觉。`need` = {xl_row: [需要的字段]}。"""
        by_key = {}
        for r in rows:
            try:
                xl = int(r.get("xl_row"))
            except (TypeError, ValueError):
                continue
            if xl in need:
                by_key[xl] = r
        todo = sorted(by_key)
        if not todo:
            return {}
        done: dict = {}
        t0 = __import__("time").time()

        def _one(xl: int) -> Optional[dict]:
            row = by_key[xl]
            urls = ThicknessRefiner.row_image_urls(row, self.max_images, self.aplus_images)
            if not urls:
                return {"xl_row": xl, "vision": None, "skipped": "无图片"}
            fields = list(need[xl])
            t = text_recs.get(xl) or {}
            lines = ["## 本行文本资料", ""]
            for c in REPAIR_FIELDS:
                v = row.get(c)
                if v:
                    s = str(v)
                    lines.append("- **%s**: %s" % (c, s[:600] + ("…" if len(s) > 600 else "")))
            tpl = {f: "" for f in fields}
            tpl["依据"] = ""
            prompt = "\n".join([
                REPAIR_VISION_CORE.replace("{\"xl_row\": <行号>, \"主体材质\": \"\", \"产品类型\": \"\", \"套装数量\": \"\", \"尺寸规格\": \"\", \"依据\": \"<图上/文本看到的>\"}",
                                           json.dumps(dict({"xl_row": row.get("xl_row")}, **tpl),
                                                      ensure_ascii=False)),
                "", "## 需要给出的字段: %s" % "、".join(fields),
                "（其它字段留空即可）", "", *lines,
                "", "共 %d 张本行图片（主图在前）。" % len(urls),
            ])
            try:
                out, err = _vision_with_fallback(
                    self.ctx, prompt, urls, detail=self.detail, max_tokens=1100)
            except Exception as exc:  # noqa: BLE001
                out, err = None, str(exc)
            if out is None:
                self._log("属性修复-视觉失败(行 %s): %s" % (xl, str(err)[:110]))
                return None
            return {"xl_row": xl, "vision": out if isinstance(out, dict) else None}

        use = max(1, min(self.workers, len(todo)))
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=use) as ex:
            futs = {ex.submit(_one, xl): xl for xl in todo}
            fin = 0
            for fut in as_completed(futs):
                xl = futs[fut]
                r = fut.result()
                fin += 1
                if r is not None:
                    done[xl] = r
                if progress and (fin % 25 == 0 or fin == len(todo)):
                    self._log("   属性修复-视觉 行 %d/%d (%.0fs)"
                              % (fin, len(todo), __import__("time").time() - t0))
        # 失败重试(逐行, 最多 2 轮)
        rnd = 0
        while rnd < 2:
            miss = [xl for xl in todo if xl not in done]
            if not miss:
                break
            rnd += 1
            for xl in miss:
                r = _one(xl)
                if r is not None:
                    done[xl] = r
        from .vision_json import unwrap_vision
        out = {}
        for xl, rec in done.items():
            u = unwrap_vision(rec.get("vision"),
                              ("主体材质", "产品类型", "套装数量", "尺寸规格"))
            out[xl] = u
        self._log("属性修复-视觉: %d 行" % len(out))
        return out

    # ---------------------------------------------------------------- 多件套尺寸细化
    def size_detail_pass(self, rows: Sequence[Mapping[str, Any]], out_dir: str,
                         set_counts: Mapping[Any, Any]) -> dict:
        """对**多件套但只给了单一尺寸**的行, 专门追问「每一件」的尺寸。

        为什么单开一遍: 综合提示词下模型常直接抄 `商品尺寸（单位换算）` 那一组
        (整套最大件/包装尺寸), 而用户要的是**各个大小的尺寸数据**。
        这里把件数写进标题、把"整套尺寸≠每件"写进口径, 逼它去逐件取。
        """
        items, by_key = [], {}
        for r in rows:
            try:
                xl = int(r.get("xl_row"))
            except (TypeError, ValueError):
                continue
            if xl in set_counts:
                items.append(r)
                by_key[str(xl)] = r
        if not items:
            return {}

        def _accept(rec: Mapping[str, Any]) -> bool:
            row = by_key.get(str(rec.get("xl_row")))
            if row is None:
                return False
            val = str(rec.get("尺寸规格") or "").strip()
            if not val or val in ("未明确", "None"):
                rec["尺寸规格"] = "未明确"
                return True
            if not quote_in_text(rec.get("原文"), [row.get(c) for c in REPAIR_FIELDS], min_len=5):
                rec["尺寸规格"] = "未明确"
                rec["_rejected"] = "引用核验未通过"
            return True

        recs = run_batches(
            self.ctx, items,
            lambda b: build_size_detail_prompt(b, {str(r.get("xl_row")): set_counts.get(
                self._as_int(r.get("xl_row")), "") for r in b}),
            accept=_accept, key_of=lambda r: str(r.get("xl_row")),
            batch=max(2, self.batch // 2), workers=self.workers, log=self._log,
            label="尺寸细化")
        out = {}
        for k, rec in recs.items():
            try:
                out[int(k)] = rec
            except (TypeError, ValueError):
                continue
        ok = sum(1 for v in out.values()
                 if str(v.get("尺寸规格") or "") not in ("", "未明确", "None"))
        self._log("尺寸细化: %d 行有结论(可用 %d)" % (len(out), ok))
        return out

    @staticmethod
    def _as_int(v: Any) -> Any:
        try:
            return int(v)
        except (TypeError, ValueError):
            return v

    # ---------------------------------------------------------------- 定案
    def decide(self, declared: Mapping[Any, Mapping[str, Any]],
               plan: Mapping[Any, Mapping[str, Any]],
               text_recs: Mapping[Any, Mapping[str, Any]],
               vision_recs: Mapping[Any, Mapping[str, Any]]) -> dict:
        """产出每一列的最终值 + 来源。"""
        out: dict = {}
        for xl, d in declared.items():
            cur_m = str(d.get("主体材质") or "").strip()
            cur_t = str(d.get("产品类型") or "").strip()
            cur_s = str(d.get("套装数量") or "").strip()
            cur_z = str(d.get("尺寸规格") or "").strip()
            p = plan.get(xl) or {}
            t = text_recs.get(xl) or {}
            v = vision_recs.get(xl) or {}

            def pick(field: str) -> tuple[str, str]:
                """字段值: 文本优先, 其次视觉; 返回 (值, 来源说明)。"""
                tv = str(t.get(field) or "").strip()
                if tv and tv not in ("未明确", "None"):
                    q = str(t.get(field + "_原文") or "").strip()
                    return tv, "文本: %s，原文「%s」" % (field, q[:80])
                vv = str(v.get(field) or "").strip()
                if vv and vv not in ("未明确", "None"):
                    return vv, "图片视觉/OCR: %s（%s）" % (vv, str(v.get("依据") or "")[:110])
                return "", ""

            rec: dict = {"原因": p.get("原因", "")}
            # ---- 主体材质: 先归一现值; 需要核实的再看核实结果
            base_m = normalize_material(cur_m) or cur_m
            if p.get("材质核实"):
                got, src = pick("主体材质")
                if got:
                    got_n = normalize_material(got)
                    g_cur, g_got = material_group(base_m), material_group(got_n)
                    if g_cur and g_got and g_cur == g_got:
                        # **材质族一致 → 保留原写法**(含 `+` 的第二材质)。
                        # 实测核实模型常只抄 `Material:` 一个字段, 会把 `钛+塑料` 压成 `钛`,
                        # 把"两面不同材质"这条信息丢掉; 族一致时没有改的必要。
                        rec["主体材质_来源"] = "材质核实一致: %s" % src
                    elif g_got and g_got != g_cur:
                        base_m = got_n
                        rec["主体材质_来源"] = "材质核实: %s（原值 %s）" % (src, cur_m or "空")
                    else:
                        rec["主体材质_来源"] = "材质核实: %s" % src
            rec["主体材质"] = base_m or cur_m
            # ---- 产品类型: 有把握时按材质对齐
            new_t = cur_t
            if cur_t in FORM_TYPES:
                new_t = cur_t
            elif base_m and base_m not in ("未明确", ""):
                new_t = align_type(base_m, cur_t, flexible=(cur_t == "塑料软砧板垫"))
                # 类型空白 → 用核实结果(文本/视觉给的)或按材质推
                if not cur_t:
                    got, src = pick("产品类型")
                    new_t = got or new_t
                    if got:
                        rec["产品类型_来源"] = src
                if new_t != cur_t and "产品类型_来源" not in rec:
                    rec["产品类型_来源"] = ("按主体材质「%s」对齐(原类型 %s)"
                                          % (base_m, cur_t or "空"))
            elif not cur_t:
                got, src = pick("产品类型")
                new_t = got or ""
                if got:
                    rec["产品类型_来源"] = src
            rec["产品类型"] = new_t or cur_t
            # ---- 套装数量(只补真空)
            rec["套装数量"] = cur_s
            if not cur_s:
                got, src = pick("套装数量")
                if got:
                    rec["套装数量"] = got
                    rec["套装数量_来源"] = src
            # ---- 尺寸规格(只补真空)
            rec["尺寸规格"] = cur_z
            if not cur_z:
                got, src = pick("尺寸规格")
                if got:
                    rec["尺寸规格"] = got
                    rec["尺寸规格_来源"] = src
            out[xl] = rec
        return out

    def run(self, rows: Sequence[Mapping[str, Any]], out_dir: str,
            declared: Mapping[Any, Mapping[str, Any]], *,
            reload_text: bool = False) -> dict:
        plan = self.plan(declared)
        self._log("属性修复规划: %d 行需要处理(材质核实 %d / 类型 %d / 套装 %d / 尺寸 %d)"
                  % (len(plan),
                     sum(1 for p in plan.values() if p["材质核实"]),
                     sum(1 for p in plan.values() if p["产品类型"]),
                     sum(1 for p in plan.values() if p["套装数量"]),
                     sum(1 for p in plan.values() if p["尺寸规格"])))
        tpath = os.path.join(out_dir, "属性修复-文本.json")
        if reload_text and os.path.exists(tpath):
            try:
                text_recs = {int(k): v for k, v in
                             (json.load(open(tpath, encoding="utf-8")).get("rows") or {}).items()}
                self._log("属性修复-文本: 复用已有 %d 行" % len(text_recs))
            except (OSError, ValueError):
                text_recs = {}
        else:
            text_recs = self.text_pass(rows, out_dir, plan)
            tmp = tpath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"cat": getattr(self.ctx.config, "cat", ""),
                           "rows": {str(k): v for k, v in text_recs.items()}},
                          fh, ensure_ascii=False)
            os.replace(tmp, tpath)
        # 视觉兜底: 文本没给到**该行需要的字段**
        need: dict = {}
        for xl, p in plan.items():
            want = []
            if p["材质核实"]:
                want.append("主体材质")
            if p["产品类型"]:
                want.append("产品类型")
            if p["套装数量"]:
                want.append("套装数量")
            if p["尺寸规格"]:
                want.append("尺寸规格")
            t = text_recs.get(xl) or {}
            miss = [f for f in want
                    if not str(t.get(f) or "").strip()
                    or str(t.get(f)).strip() in ("未明确", "None")]
            if miss:
                need[xl] = miss
        self._log("需要图片兜底的行: %d" % len(need))
        vision_recs = self.vision_pass(rows, out_dir, need, text_recs) if need else {}

        # ---- 多件套尺寸细化: 只给了一个尺寸的, 追问「每一件」
        self._size_detail(rows, out_dir, declared, plan, text_recs, vision_recs)

        decided = self.decide(declared, plan, text_recs, vision_recs)
        payload = {"cat": getattr(self.ctx.config, "cat", ""),
                   "rows": {str(k): v for k, v in decided.items()},
                   "文本结论": len(text_recs), "视觉兜底": len(vision_recs),
                   "规划": {str(k): v for k, v in plan.items()},
                   # 原始模型结论留档 → 之后调口径可 `--redecide` 离线重算, 不重调模型
                   "文本原始": {str(k): v for k, v in text_recs.items()},
                   "视觉原始": {str(k): v for k, v in vision_recs.items()}}
        self._save(out_dir, payload)
        return payload

    @staticmethod
    def _save(out_dir: str, payload: dict) -> None:
        tmp = os.path.join(out_dir, REPAIR_NAME) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, os.path.join(out_dir, REPAIR_NAME))

    def _size_detail(self, rows: Sequence[Mapping[str, Any]], out_dir: str,
                     declared: Mapping[Any, Mapping[str, Any]],
                     plan: Mapping[Any, Mapping[str, Any]],
                     text_recs: dict, vision_recs: Mapping[Any, Mapping[str, Any]]) -> None:
        """找出「多件套但只给了一个尺寸」的行, 逐件追问并把结果并回 `text_recs`。"""
        targets: dict = {}
        for xl, p in plan.items():
            if not p.get("尺寸规格"):
                continue
            t = text_recs.get(xl) or {}
            v = vision_recs.get(xl) or {}
            val = str(t.get("尺寸规格") or "").strip()
            if not val or val in ("未明确", "None"):
                val = str(v.get("尺寸规格") or "").strip()
            if not val or val in ("未明确", "None"):
                continue
            if re.search(r'大号|中号|小号', val):
                continue
            n = (_set_num(declared.get(xl, {}).get("套装数量"))
                 or _set_num(t.get("套装数量")) or _set_num(v.get("套装数量")))
            if n and n > 1:
                targets[xl] = n
        if not targets:
            return
        self._log("多件套但只给一个尺寸: %d 行 → 逐件细化" % len(targets))
        det = self.size_detail_pass(rows, out_dir, targets)
        for xl, rec in det.items():
            val = str(rec.get("尺寸规格") or "").strip()
            if val and val not in ("未明确", "None"):
                text_recs.setdefault(xl, {})["尺寸规格"] = val
                text_recs[xl]["尺寸规格_原文"] = rec.get("原文")

    def redecide(self, declared: Mapping[Any, Mapping[str, Any]], payload: Mapping[str, Any],
                 *, rows: Optional[Sequence[Mapping[str, Any]]] = None, out_dir: str = "",
                 with_size_detail: bool = False) -> dict:
        """用产物里留档的**原始文本/视觉结论**重跑定案(不调用模型)。

        `with_size_detail=True` 时, 额外对「多件套但只给一个尺寸」的行跑一遍逐件细化
        (这一步会调模型, 只涉及几十行)。
        """
        plan = self.plan(declared)
        text_recs = {}
        for k, v in (payload.get("文本原始") or {}).items():
            try:
                text_recs[int(k)] = dict(v)
            except (TypeError, ValueError):
                continue
        vision_recs = {}
        for k, v in (payload.get("视觉原始") or {}).items():
            try:
                vision_recs[int(k)] = v
            except (TypeError, ValueError):
                continue
        if with_size_detail and rows is not None and out_dir:
            self._size_detail(rows, out_dir, declared, plan, text_recs, vision_recs)
        decided = self.decide(declared, plan, text_recs, vision_recs)
        out = dict(payload)
        out["rows"] = {str(k): v for k, v in decided.items()}
        out["规划"] = {str(k): v for k, v in plan.items()}
        out["文本原始"] = {str(k): v for k, v in text_recs.items()}
        return out
        n_m = sum(1 for k, v in decided.items()
                  if str(declared.get(k, {}).get("主体材质") or "").strip() != v["主体材质"])
        n_t = sum(1 for k, v in decided.items()
                  if str(declared.get(k, {}).get("产品类型") or "").strip() != v["产品类型"])
        n_s = sum(1 for k, v in decided.items()
                  if not str(declared.get(k, {}).get("套装数量") or "").strip()
                  and v["套装数量"])
        n_z = sum(1 for k, v in decided.items()
                  if not str(declared.get(k, {}).get("尺寸规格") or "").strip()
                  and v["尺寸规格"])
        self._log("属性修复完成: 材质改动 %d / 类型改动 %d / 套装补 %d / 尺寸补 %d"
                  % (n_m, n_t, n_s, n_z))
        return payload
