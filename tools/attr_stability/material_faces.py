# -*- coding: utf-8 -*-
"""「主体材质」两面核实(文字 + 图片视觉)。

用户口径(2026-09-24 明确)
==========================
1. `A+B` **只表示"砧板两个切面材质不同"**(主材质在前);
2. 两面材质一样 → **只写一个材质**;
3. **配件不算"面"**: 塑料砧板垫、抽拉托盘、收纳架/盒、SAN 盖、硅胶包边、不锈钢钢条/钢架、
   磨刀器 —— 都不是切面材质, 不写进「主体材质」;
4. 套装里不同件材质不同(如"2 件套: 不锈钢板 + 相思木板") → 只写**主材质**, 不串联;
5. 双面可用 / 双面不同材质的行都要过一遍**图片视觉**核实 —— 其中会藏着一部分
   "实际两面材质不同、但材质列只写了一个" 的行。

为什么要单独做一层
==================
实测交付表 2466 行里: `是否双面可用` = 双面可用 1173 / 双面不同材质 80 / 未明确 1210;
而「主体材质」含 `+` 的只有 108 行, 其中
  · 73 行 双面不同材质 → **正确**;
  · 20 行 双面可用 / 15 行 未明确 → 多为**套装不同件**或**本体+配件**(如"竹木+PP塑料"=
    竹砧板+6 张内置塑料垫, "钛金属+硅胶"=钛板+硅胶包边) → 该去掉 `+`;
  · 还有 **7 行标了"双面不同材质"却没有 `+`** → 该补上。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping, Optional, Sequence

from .thickness import _is_image_url, _vision_with_fallback
from .vision_json import unwrap_vision

__all__ = [
    "DEFAULT_MATERIAL_VOCAB", "is_dual_face", "current_materials", "split_join",
    "build_two_face_prompt", "TWO_FACE_CORE", "decide_material", "MaterialRefiner",
]

# 受控材质词表(rules.json 主体材质 values; 另见 deliverable 里实际出现过的写法)
DEFAULT_MATERIAL_VOCAB = [
    "竹木", "PP塑料", "304不锈钢", "相思木", "钛金属", "柚木", "钢化玻璃", "枫木",
    "硅胶", "塑料", "不锈钢", "大理石", "胡桃木", "花岗岩", "TPU", "实木", "桧木",
    "橄榄木", "橡胶木", "亚克力", "树脂", "小麦秸秆PP", "木纤维", "乌木",
    "山毛榉", "樱桃木",
]

# 「双面可用」侧的取值(需要过图片核实的行)
DUAL_VALUES = ("双面可用", "双面不同材质")
# 视觉返回里我们真正要读的字段(用于 vision_json 剥壳时定位)
_WANT_KEYS = ("两面材质是否不同", "主材质", "另一面材质", "配件材质", "件数与件材质")

# 配件线索: 这些材质通常是**配件**而不是切面(用户口径 3)
ACCESSORY_HINTS = (
    "钢条", "钢架", "收纳", "支架", "托盘", "垫", "盖", "盖板", "包边", "边框",
    "磨刀", "刮板", "底座", "手柄套", "硅胶脚", "防滑脚", "防滑垫", "锁扣",
)
# 文字里的"两面不同材质"线索
RE_DUAL_TEXT = re.compile(
    r'(one|the\s+other|1st|2nd|reverse|back)\s+side|other\s+face|another\s+side'
    r'|\u4e00\u9762|\u53e6\u4e00\u9762|\u53cc\u9762\u4e0d\u540c|\u6b63\u53cd\u4e24\u9762', re.I)
RE_NOT_DUAL = re.compile(
    r'same\s+material|both\s+sides?\s+(?:are\s+)?(?:made\s+of\s+)?same'
    r'|\u4e24\u9762\u540c\u6750\u8d28|\u540c\u4e00\u6750\u8d28', re.I)

TWO_FACE_CORE = """你是亚马逊砧板类目(Cutting Boards)的属性审校员。
任务：判断这一行商品的「主体材质」该怎么写。

口径(必须逐条遵守)
1. 只有在**砧板两个切面(两个可切食物的面)材质不同**时，才用 `A+B` 把两个材质串起来，**主材质在前**。
2. 两面材质一样 → **只写一个材质**。
3. **配件不算"面"**：塑料砧板垫、抽拉托盘、收纳架/收纳盒、SAN 盖、硅胶包边、不锈钢钢条/钢架、
   磨刀器 等都不是切面材质，**不要**写进主体材质。
4. 套装里**不同件**材质不同(如"2 件套：不锈钢板 + 相思木板"、"3 件套：1 块纯钛 + 2 块竹木")
   → **只写主材质**(主体砧板那一个)，**不要**用 `+`。
5. 材质名统一用下面受控词表里的写法。

判定依据
· 先看文本：标题/五点/详细参数 里的 `one side ... the other side ...`、`double-sided`、
  `2-in-1`、`reversible`、`Material: A, B` 等；
· 再看图片：正面/反面实拍、材质说明图与小字(OCR)、套装全家福(能看出有几种板)；
· 文本与文本、文本与图片冲突时，以**能看清两面实拍 / 能数清件数**的一方为准。

受控材质词表
%s

输出严格 JSON（不要输出其它文字）：
{
  "两面材质是否不同": "是/否/无法判断",
  "主材质": "<受控词表里的材质；判不出填 未明确>",
  "另一面材质": "<受控词表里的材质；两面相同或没有证据填 无>",
  "配件材质": "<若图上/文本里有配件，列出其材质；没有填 无>",
  "件数与件材质": "<如 '2件套:不锈钢+相思木'；单件填 '单件'>",
  "依据": "<文本原句 或 第几张图看到什么>"
}
"""


def is_dual_face(declared: Any) -> bool:
    """交付表「是否双面可用」是否属于需要核实的双面行。"""
    return str(declared or "").strip() in DUAL_VALUES


def split_join(value: Any) -> list[str]:
    """把 `A+B` 拆成 `[A, B]`; 无 `+` 返回单元素列表(空值返回 [])。"""
    s = str(value or "").strip()
    if not s or s in ("未明确", "None"):
        return []
    return [p.strip() for p in re.split(r'[+\uff0b]', s) if p.strip()]


def current_materials(value: Any, dual_declared: Any = None) -> dict:
    """对现状做**文字侧**预判: 是否像"本体+配件"/"成套不同件", 以及疑似主材质。"""
    parts = split_join(value)
    dual = is_dual_face(dual_declared)
    return {"parts": parts, "joined": len(parts) > 1, "dual_declared": dual}


def _notes(row: Mapping[str, Any], declared_value: Any, dual_declared: Any,
           vocab: Sequence[str]) -> str:
    """交给视觉模型的文本侧线索。"""
    lines = ["## 交付表现状", "",
             "- 「主体材质」当前标注：`%s`" % (declared_value or "未明确"),
             "- 「是否双面可用」当前标注：`%s`" % (dual_declared or "未明确")]
    parts = split_join(declared_value)
    if len(parts) > 1:
        lines.append("  注意：当前用了 `+`；如果这两个材质其实是**配件**(垫/托盘/架/盖/包边/钢条)"
                     "或**套装里的不同件**，就**不该**用 `+`。")
    lines.append("")
    lines.append("## 受控材质词表")
    lines.append("`%s`" % "、".join(vocab))
    return "\n".join(lines)


def build_two_face_prompt(row: Mapping[str, Any], declared_value: Any, dual_declared: Any,
                          *, vocab: Sequence[str], n_images: int = 0,
                          max_chars: int = 1100) -> str:
    """「主体材质两面核实」的视觉/OCR 提示词(一行一调, 图片 URL 直传)。"""
    out = [TWO_FACE_CORE % "、".join(vocab), "", "=" * 60,
           _notes(row, declared_value, dual_declared, vocab), "=" * 60]
    if n_images:
        out += ["", "## 随附图片", "",
                "共 %d 张，均为**本行商品**的图片（主图在前，其后是 A+ 图文）。" % n_images]
    ev = []
    for col in ("标题", "亮点", "五点描述", "SKU", "详细参数"):
        v = row.get(col)
        if not v:
            continue
        s = str(v)
        if len(s) > max_chars:
            s = s[:max_chars] + "…"
        ev.append("- **%s**：%s" % (col, s))
    if ev:
        out += ["", "## 本行文本资料", ""] + ev
    return "\n".join(out)


def _norm_material(name: Any, vocab: Sequence[str]) -> str:
    """把模型给的材料名收敛到受控词表写法(精确 → 包含 → 原样)。"""
    s = str(name or "").strip().strip("`*「」\"'")
    if not s or s in ("无", "未明确", "None", "单件", "-"):
        return ""
    for v in vocab:
        if s == v:
            return v
    for v in vocab:
        if v and (v in s or s in v):
            return v
    return s


def decide_material(row: Mapping[str, Any], vision_rec: Optional[Mapping[str, Any]],
                    declared_value: Any, dual_declared: Any,
                    vocab: Sequence[str] = tuple(DEFAULT_MATERIAL_VOCAB)) -> dict:
    """图片(+ 文本) → 新的「主体材质」写法。

    **保守口径(重要)**: 只动 `+` 的有无与第二个材质, **不改主体材质本身**。
    实测视觉模型会把 `竹木` 认成 `胡桃木`、把 `枫木` 认成 `胡桃木`(看图猜树种),
    而主体材质在步骤17 是**读文本 M`aterial:` 字段**得到的、比看图可靠 ——
    所以这里:
      · 现有 `A+B` 且视觉判"两面相同" → 去掉 `+`, 只留主材质;
      · 现有 `A+B` 且视觉判"两面不同/无法判断" → **保持原样**(不覆盖已有判断);
      · 现有单材质且视觉判"两面材质不同"、另一面不是配件 → `主材质+另一面`;
      · 其余 → **不改动**。

    返回 `{value, source, reason, review, changed}`; `value=None` 表示保持原值不变。
    """
    cur = str(declared_value or "").strip()
    parts = split_join(cur)
    base = parts[0] if parts else ""
    vision_rec = unwrap_vision(vision_rec, _WANT_KEYS)
    if not vision_rec or not any(k in vision_rec for k in _WANT_KEYS):
        return {"value": None, "source": "", "reason": "无视觉结果", "review": True,
                "changed": False}

    dual = str(vision_rec.get("两面材质是否不同") or "").strip()
    other = _norm_material(vision_rec.get("另一面材质"), vocab)
    acc = _norm_material(vision_rec.get("配件材质"), vocab)
    vmain = _norm_material(vision_rec.get("主材质"), vocab)
    note = str(vision_rec.get("依据") or "").strip()
    pieces = str(vision_rec.get("件数与件材质") or "").strip()
    src_bits = [note[:220]] if note else []
    if acc:
        src_bits.append("配件材质=%s(不计入主体材质)" % acc[:30])
    if pieces and pieces != "单件":
        src_bits.append("件数=%s" % pieces[:60])
    src = "图片视觉/OCR: " + "；".join(src_bits)

    # 另一面与配件同名 → 那是配件不是"面"
    if other and acc and other == acc:
        other = ""
    if not base:
        base = vmain           # 原值空/未明确: 只能采信视觉
        if not base:
            return {"value": None, "source": src, "reason": "视觉判不出主材质",
                    "review": True, "changed": False}

    if len(parts) > 1:                     # ---- 现有 A+B
        if dual == "否":
            val, reason = base, "视觉判两面材质相同 → 去掉 `+`, 只留主材质"
        else:
            return {"value": None, "source": src,
                    "reason": "现有 A+B 与视觉一致(两面材质不同) → 保持",
                    "review": False, "changed": False}
    else:                                  # ---- 现有单材质
        if dual == "是" and other and other != base:
            val = "%s+%s" % (base, other)
            reason = "视觉判两面材质不同 → 补成 A+B"
        else:
            return {"value": None, "source": src, "reason": "无需改动",
                    "review": False, "changed": False}
    return {"value": val, "source": src[:460], "reason": reason,
            "review": bool(dual == "无法判断"), "changed": (val != cur)}


class MaterialRefiner:
    """跑一遍「主体材质」两面核实: 文字线索 → 图片视觉/OCR → 落盘。

    · 并发数 = `MATERIAL_WORKERS`(默认取 `ATTR_STAB_WORKERS`, 再默认 6);
    · 行级断点: 每完成一行写 `主体材质-核实.checkpoint.json`;
    · 单行失败隔离: 失败行 1 行/次串行重试, 最多 3 轮。
    """

    OUT_NAME = "主体材质-两面核实.json"
    CKPT_NAME = "主体材质-核实.checkpoint.json"

    def __init__(self, ctx, *, vocab: Sequence[str] = (), log=None):
        self.ctx = ctx
        self._log = log or (lambda msg: None)
        cfg = ctx.config
        self.workers = int(cfg.get("MATERIAL_WORKERS") or cfg.get("ATTR_STAB_WORKERS") or 6)
        self.detail = str(cfg.get("MATERIAL_VISION_DETAIL") or "high").strip().lower()
        self.max_images = int(cfg.get("MATERIAL_MAX_IMAGES") or 4)
        self.aplus_images = int(cfg.get("MATERIAL_APLUS_IMAGES") or 2)
        self.vocab = list(vocab) or list(DEFAULT_MATERIAL_VOCAB)
        # {xl_row: {"主体材质":…, "是否双面可用":…}}
        self.declared: dict[int, dict] = {}

    @staticmethod
    def _split_urls(raw: Any) -> list[str]:
        # 分隔符兼顾 `,` 与 `;`：step10 产出用 `;`，step14 统一成 `,`（见改动清单 §19）
        s = str(raw or "").strip().strip('"\'')
        return [u.strip().strip('"\'') for u in re.split(r"[|;,\n]+", s) if u.strip()]

    @classmethod
    def row_image_urls(cls, row: Mapping[str, Any], max_main: int, max_aplus: int) -> list[str]:
        urls: list[str] = []
        for col, cap in (("图片链接", max_main), ("A+图片链接", max_aplus), ("主图", 1)):
            if cap <= 0:
                continue
            for u in cls._split_urls(row.get(col))[:cap]:
                u = re.sub(r"\._[A-Za-z0-9,]+(?:_[A-Za-z0-9,]+)*_\.", ".", u)
                if u.startswith("http") and _is_image_url(u) and u not in urls:
                    urls.append(u)
        return urls

    def needs_vision(self, row: Mapping[str, Any]) -> bool:
        """需要核实的行: 「是否双面可用」= 双面可用/双面不同材质, 或材质列已用 `+`。"""
        try:
            xl = int(row.get("xl_row"))
        except (TypeError, ValueError):
            xl = None
        d = self.declared.get(xl) or {}
        return is_dual_face(d.get("是否双面可用")) or len(split_join(d.get("主体材质"))) > 1

    def run(self, rows: Sequence[Mapping[str, Any]], out_dir: str, *,
            declared: Optional[Mapping[Any, Mapping[str, Any]]] = None,
            progress: bool = True, retry_unusable: bool = False) -> dict:
        """`declared`: {xl_row: {"主体材质":…, "是否双面可用":…}}(来自交付表)。

        `retry_unusable=True`: 只重跑**已有产物里视觉记录不可用**的行(模型把
        `response_format` 原样回吐 / 返回空 → 剥壳后仍读不到字段), 其余沿用。
        """
        os.makedirs(out_dir, exist_ok=True)
        ckpt_path = os.path.join(out_dir, self.CKPT_NAME)
        out_path = os.path.join(out_dir, self.OUT_NAME)
        self.declared = {}
        for k, v in (declared or {}).items():
            try:
                self.declared[int(k)] = dict(v or {})
            except (TypeError, ValueError):
                continue
        row_by: dict[int, Mapping[str, Any]] = {}
        for i, r in enumerate(rows, start=2):
            xl = r.get("xl_row")
            row_by[int(xl) if xl is not None else i] = r

        todo = [xl for xl in row_by if self.needs_vision(row_by[xl])]
        seed: dict[int, dict] = {}
        if retry_unusable:
            prev = (self.load(out_dir).get("rows") or {})
            keep, retry = 0, []
            for xl in todo:
                u = unwrap_vision((prev.get(str(xl)) or {}).get("视觉"), _WANT_KEYS)
                if any(k in u for k in _WANT_KEYS):
                    seed[xl] = {"xl_row": xl, "vision": u}
                    keep += 1
                else:
                    retry.append(xl)
            self._log("视觉记录复查: 可用 %d 行沿用, 需重跑 %d 行" % (keep, len(retry)))
            todo = retry
        suffix = "retry" if retry_unusable else ""
        plan_key = "%d|%s|%d|%d|%s|%s" % (len(rows), self.detail, self.max_images,
                                         self.aplus_images, len(self.vocab), suffix)
        done: dict[int, dict] = dict(seed)
        if os.path.exists(ckpt_path):
            try:
                with open(ckpt_path, encoding="utf-8") as fh:
                    o = json.load(fh)
                if (o.get("input_key") or "") == plan_key:
                    for it in (o.get("done") or []):
                        if isinstance(it, dict) and it.get("xl_row") is not None:
                            done[int(it["xl_row"])] = it
            except (OSError, ValueError):
                done = {}
        if done:
            self._log("主体材质核实断点续跑: 已读回 %d 行, 只跑剩余" % len(done))

        def _save_ckpt() -> None:
            try:
                tmp = ckpt_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"input_key": plan_key,
                               "done": [done[k] for k in sorted(done)]},
                              fh, ensure_ascii=False)
                os.replace(tmp, ckpt_path)
            except OSError:
                pass

        def _fill_one(xl: int) -> Optional[dict]:
            row = row_by.get(xl)
            if row is None:
                return None
            urls = self.row_image_urls(row, self.max_images, self.aplus_images)
            if not urls:
                return {"xl_row": xl, "vision": None, "skipped": "无图片"}
            d = self.declared.get(xl) or {}
            prompt = build_two_face_prompt(row, d.get("主体材质"), d.get("是否双面可用"),
                                           vocab=self.vocab, n_images=len(urls))
            out, err = _vision_with_fallback(self.ctx, prompt, urls,
                                             detail=self.detail, max_tokens=900)
            if out is None:
                self._log("主体材质视觉失败(行 %s): %s" % (xl, err))
                return None
            return {"xl_row": xl, "vision": out if isinstance(out, dict) else None}

        pending = [xl for xl in todo if xl not in done]
        if pending:
            n, t0 = len(pending), __import__("time").time()
            use = self.workers if n > 1 else 1
            if use > 1:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                with ThreadPoolExecutor(max_workers=use) as ex:
                    futs = {ex.submit(_fill_one, xl): xl for xl in pending}
                    fin = 0
                    for fut in as_completed(futs):
                        xl = futs[fut]
                        r = fut.result()
                        fin += 1
                        if r is not None:
                            done[xl] = r
                            _save_ckpt()
                        if progress and (fin % 25 == 0 or fin == n):
                            self._log("   主体材质核实 行 %d/%d (累计 %d, %.0fs)"
                                      % (fin, n, len(done), __import__("time").time() - t0))
            else:
                for xl in pending:
                    r = _fill_one(xl)
                    if r is not None:
                        done[xl] = r
                        _save_ckpt()
        missing = [xl for xl in todo if xl not in done]
        rnd = 0
        while missing and rnd < 3:
            rnd += 1
            self._log("   主体材质核实 %d 行失败, 逐行重试第 %d 轮" % (len(missing), rnd))
            still = []
            for xl in missing:
                r = _fill_one(xl)
                if r is not None:
                    done[xl] = r
                    _save_ckpt()
                else:
                    still.append(xl)
            missing = still
        if missing:
            self._log("   主体材质核实 %d 行重试后仍失败(保持原值)" % len(missing))

        results: dict[str, Any] = {}
        stats: dict[str, int] = {}
        for xl in sorted(row_by):
            d = self.declared.get(xl) or {}
            cur = d.get("主体材质")
            fin = decide_material(row_by[xl], (done.get(xl) or {}).get("vision"),
                                  cur, d.get("是否双面可用"), self.vocab)
            val = fin["value"] if fin["value"] else (str(cur or "").strip() or "未明确")
            results[str(xl)] = {
                "原值": str(cur or ""),
                "主体材质": val,
                "改动": bool(fin["changed"]),
                "原因": fin["reason"],
                "来源": fin["source"],
                "需复核": bool(fin["review"]),
                "视觉": (done.get(xl) or {}).get("vision"),
            }
            key = "%s|%s" % (fin["reason"][:16], "改" if fin["changed"] else "不改")
            stats[key] = stats.get(key, 0) + 1
        payload = {"cat": getattr(self.ctx.config, "cat", ""), "rows": results,
                   "统计": stats, "视觉调用行数": len(done)}
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, out_path)
        if not missing and os.path.exists(ckpt_path):
            try:
                os.remove(ckpt_path)
            except OSError:
                pass
        changed = sum(1 for r in results.values() if r["改动"])
        self._log("主体材质核实已写入 %s (视觉 %d 行, 建议改动 %d 行)"
                  % (out_path, len(done), changed))
        return payload

    def redecide(self, rows: Sequence[Mapping[str, Any]], out_dir: str, *,
                 declared: Optional[Mapping[Any, Mapping[str, Any]]] = None) -> dict:
        """**用已有的视觉记录**重跑定案(不调用模型)。

        调了判定口径后不必重跑 1200+ 次图片调用 —— 产物 JSON 里存了原始 `视觉` 记录。
        """
        self.declared = {}
        for k, v in (declared or {}).items():
            try:
                self.declared[int(k)] = dict(v or {})
            except (TypeError, ValueError):
                continue
        old = (self.load(out_dir).get("rows") or {})
        row_by: dict[int, Mapping[str, Any]] = {}
        for i, r in enumerate(rows, start=2):
            xl = r.get("xl_row")
            row_by[int(xl) if xl is not None else i] = r
        results: dict[str, Any] = {}
        stats: dict[str, int] = {}
        for xl in sorted(row_by):
            d = self.declared.get(xl) or {}
            cur = d.get("主体材质")
            fin = decide_material(row_by[xl], (old.get(str(xl)) or {}).get("视觉"),
                                  cur, d.get("是否双面可用"), self.vocab)
            val = fin["value"] if fin["value"] else (str(cur or "").strip() or "未明确")
            results[str(xl)] = {
                "原值": str(cur or ""), "主体材质": val, "改动": bool(fin["changed"]),
                "原因": fin["reason"], "来源": fin["source"],
                "需复核": bool(fin["review"]), "视觉": (old.get(str(xl)) or {}).get("视觉"),
            }
            key = "%s|%s" % (fin["reason"][:16], "改" if fin["changed"] else "不改")
            stats[key] = stats.get(key, 0) + 1
        payload = {"cat": getattr(self.ctx.config, "cat", ""), "rows": results,
                   "统计": stats,
                   "视觉调用行数": sum(1 for v in results.values() if v.get("视觉"))}
        tmp = os.path.join(out_dir, self.OUT_NAME) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, os.path.join(out_dir, self.OUT_NAME))
        changed = sum(1 for r in results.values() if r["改动"])
        self._log("主体材质核实(重定案) 建议改动 %d 行" % changed)
        return payload

    @staticmethod
    def load(out_dir: str) -> dict:
        path = os.path.join(out_dir, MaterialRefiner.OUT_NAME)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}
