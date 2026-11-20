# -*- coding: utf-8 -*-
"""属性标注的「结构闸门 + 多运行一致性」核心库（可合并进 auto-workflow-python）

设计要点
================================================================================
1. **本模块不产生任何属性值**。它只做三件事：
   ① 结构闸门：判断一份运行结果的**形状**是否合法（pass / reject）
   ② 稳定性筛选：算各运行与众数的偏离度，剔除语义离群运行
   ③ 多数表决：把合格运行的众数装配成交付表
   值仍由 LLM 产出（见 attr_runner.py 的生产者）。

2. **本模块不含任何类目词**（如具体品类/材质/场景词）。类目信息一律从
   `categories/<cat>.json` 与 `<cat>.detail.json` **读取**（引用，非硬编码）。
   模块自带 `lint_category_words()` 自检；`tests` 里应断言其返回空列表。

3. 零第三方依赖（只用标准库）。不需要 pandas / openpyxl。读写 Excel 由调用方处理。

与其他层的关系
================================================================================
    产出（LLM，可多次运行） → 【结构闸门】 → 【稳定性筛选】 → 【多数表决】 → 交付
                                    ↑ 本模块（确定性、不产出值）

为什么闸门必须是代码而不是 LLM：**独立验证必须与被验证对象机制不同**。
让产出它的模型再校验自己，会把同一个错误"校验通过"。

术语
================================================================================
- **运行（run）**：同一输入、同一提示词的一次完整产出，标识如 `r1`/`r2`。
- **格（cell）**：一行的某个属性的某个字段，路径形如 `attr.<属性>.<字段>`，
  或行级字段如 `尺寸_拆列.宽`、`suitable_scene_规范串`。
- **独异格率**：某运行与其余运行众数不同的格占该运行可比格的比例。越低越稳。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "AttrSpec", "RuleSource", "GateIssue", "GateResult",
    "check_row", "gate_run", "check_source_value",
    "flatten_row", "cell_key", "cell_kind",
    "alone_rates", "majority", "union_cells", "review_queue", "process_union",
    "consistency",
    "build_retry_brief", "lint_category_words",
    "DEFAULT_TOP_KEYS", "DEFAULT_ALLOW_KEYS", "DEFAULT_SOURCE_NAMES",
    "ERR_DATA", "ERR_SCHEMA", "ERR_QUALITY", "MISSING_KEY",
    "FREE_TEXT_FIELDS", "PROCESS_FIELDS",
]

# ---------------------------------------------------------------- 常量
# 行级字段的**必需**集。**不同类目/不同版本可能不同**，故允许从配置覆盖，默认值
# 仅为兼容本仓库既有 schema（砧板系列）。优先级：显式参数 > 类目配置 > 默认值。
#   · 显式参数：RuleSource.load(..., top_keys=[...], allow_keys=[...])
#   · 类目配置：`<cat>.rules.json` 顶层 `"top_keys": [...]` / `"allow_keys": [...]`
#
# **必需(top_keys) 与 允许(allow_keys) 必须分开**（2026-09-29 血的教训）：
# 曾经只有一张白名单，于是「本类目合法的额外键」和「必需键」是同一个集合。
# Salt and Pepper 的旧提示词会多输出一个 `形态`（件数形态），把它写进白名单后，
# 一旦换成不输出 `形态` 的新提示词，它就从「多余」变成「缺失」→ 整批又 REJECT。
#   缺 top_keys 里的键   → 整列静默漏填 → ERR/data，必拦
#   出现 allow_keys 的键 → 本类目合法扩展 → 放行，且**不要求必须出现**
#   两者都不是的键       → ERR/schema，只记录（多半是忘了登记）
DEFAULT_TOP_KEYS: set[str] = {
    "xl_row", "category_check", "attributes",
}

# 允许出现但**不要求**出现的行级键。
# ★ 2026-10-26 修：把「派生/过程字段」从 top_keys 挪到 allow_keys。
#   为什么：`top_keys` 的语义是"每行**必须**有"（缺了=整列静默漏填，必拦），
#   而下面这些都只是**按需派生/辅助**的列，模型只在有对应属性时才写：
#     · `尺寸_拆列` / `尺寸_依据` —— 尺寸拆列改由步骤18 做，步骤17 已禁止写；
#       且定向修复的提示词又把 `尺寸_拆列` 改写回 `尺寸` → 校验与修复互相打架，
#       实测 202 行每行都报「缺键 ['尺寸_拆列']」，修 12 批仍是 202，永远过不了。
#     · `pack_count_数值` / `suitable_scene_规范串` —— 包装件数/适用场景的派生列，
#       实测 Dehumidifiers 只有 106/80 行（202 行里）会写；要求每行都有 = 假 REJECT。
#     · `词表外词` / `跨列纠错` / `冲突` —— 只用于**上报**的辅助数组，模型漏写或用
#       对象代替数组很常见（实测 121 行写成非数组）→ 不该因此拦整份交付。
#   改成 allow_keys 后：老数据里有 → 放行不当"未登记键"；新数据没写 → 不报缺键。
DEFAULT_ALLOW_KEYS: set[str] = {
    "尺寸_拆列", "尺寸_依据", "pack_count_数值", "suitable_scene_规范串",
    "词表外词", "跨列纠错", "冲突",
}

# ---------------------------------------------------------------- 错误分级
# 闸门原本把「配置问题」和「数据问题」混在一个 errors 计数里，一起 REJECT。
# 后果：类目新增一个合法行级键 → 整批假 REJECT；真出数据错时又淹没在假警报里。
# 现在分两类：
#   ERR_DATA   「这份数据本身不完整/不合法」→ 必须拦，verdict = REJECT。
#              例：attributes 不是对象、漏属性、漏来源键、category_check 判定非法、
#                  尺寸_拆列 与输入比对漏填、行级**缺键**、closed 词表越界。
#              判据：这类错误意味着「有整列/整属性没被填出来」，自洽检查查不出，
#                    正是闸门存在的理由。
#   ERR_SCHEMA 「这份数据比闸门登记的行级键**多**了」→ 只记录不拦，verdict 不受影响。
#              例：类目新增包装键未登记进配置。方向是「多」不是「缺」，不会造成
#                  静默漏填；且若它真顶掉了 attributes，会同时触发 ERR_DATA。
# 缺键仍算 ERR_DATA：整列静默漏填是唯一必须硬拦的方向。
ERR_DATA = "data"
ERR_SCHEMA = "schema"
# 质量类错误(词表越界/来源标签非法): 触发定向修复, 但最终不拦交付(记下来即可)。
ERR_QUALITY = "quality"

# 自由文本字段：逐次必然不同，不计入一致率（否则会把指标压到无意义）
FREE_TEXT_FIELDS: set[str] = {"证据原文", "尺寸_依据", "备注"}

# 过程记录字段：记录「判定过程中遇到了什么」，不是交付值。
# 不同运行走不同推理路径，遇到的矛盾自然不同 → 一致率结构性偏低，
# **正确用法是多运行取并集交人工复核**，而不是要求它们一致。
PROCESS_FIELDS: set[str] = {"冲突", "跨列纠错"}

# 属性在 attributes 里平铺为「值键 + 来源键」两个键(2026-09-24 精简):
#   · 值键   = 属性名本身(如 `材质`), 值为标注出的取值(标量字符串)
#   · 来源键 = 属性名 + `_来源`(如 `材质_来源`), 值为来源(标题/五点描述/详细参数/…)
# 不再输出 明细/候选值/置信度/证据原文/数值/单位/附加 —— 这些在标注阶段是冗余溯源细节。
_SOURCE_SUFFIX = "_来源"
# 依据键 = 属性名 + `_依据`，值为**原文**（2026-10-09 新增，见提示词 §3.3e）。
# 与 `_来源` 的区别：`_来源` 是**标签**（标题/五点描述/…），`_依据` 是**原文片段**。
# 为什么必须有：事后校验要判「两属性矛盾时该改哪一侧」，只看标签判不了
# （冲突两侧常常标签相同）；实测当时 18 个属性里只有「尺寸」有依据 → 纠正把正确数据改错。
_BASIS_SUFFIX = "_依据"

# 是否强制每个属性都给依据。默认 True；置 False 可兼容旧产物（灰度）。
# 由调用方按 config 的 `ATTR_STAB_REQUIRE_BASIS` 设置（见 `set_require_basis`）。
_REQUIRE_BASIS = True


def set_require_basis(flag: bool) -> None:
    """由步骤按 config 设置「是否强制依据键」。"""
    global _REQUIRE_BASIS
    _REQUIRE_BASIS = bool(flag)

# 保留标记：不是受控值，是「证据不足/不适用」的占位
RESERVED_VALUES: set[str] = {"未明确", "不适用", "未知", "待确认", ""}

# 缺键哨兵：某份运行**根本没有这个格**时用的占位（只用于比对/统计）。
# ⚠ 2026-09-29 实测事故：`union_cells` 把它当普通提名参与表决，且
#   `agree = (分组数==1)` —— 于是「所有运行都没有这个属性」时哨兵**唯一且当选**，
#   交付值直接写成字符串 `<缺键>`，再被步骤18 聚类写进受控值词表
#   （实测: 盐胡椒类目 `是否防潮保鲜` 的 values 里真的出现了 `<缺键>`）。
#   **哨兵永远不能成为交付值**：它不是数据，是「没有数据」。
MISSING_KEY = "<缺键>"

# 置信度的合法取值（三值，边界固定）
CONFIDENCE_VALUES: set[str] = {"提取", "推断", "未采用"}

DEFAULT_SOURCE_NAMES: set[str] = {
    "标题", "亮点", "五点描述", "详细参数", "SKU", "图片OCR", "图片视觉",
}


def check_source_value(
    value: Any,
    names: Optional[set[str]] = None,
) -> Optional[str]:
    """校验 `来源` 字段，返回错误说明（None = 合法）。

    **刻意不写死"单一来源 vs 多来源"这条政策** —— 那是类目规则的选择：
      · 政策 A（单一来源）：只写 evidence_priority 最高的那一个
      · 政策 B（全部印证来源）：用 `+` 按固定顺序连接
    闸门两种都放行，只要求：空串，或 `+` 连接的**已知来源名**、无重复、无空白段。
    否则换个类目/改一次规则，闸门就会刷出几百条假错（实测踩过：一次 500 条）。
    """
    names = names or DEFAULT_SOURCE_NAMES
    s = "" if value is None else str(value).strip()
    if s == "":
        return None
    # 多来源分隔符: 兼容 `+` 与 `；`/`;`（用户 2026-10-27 口径: 多个来源也可用「；」合并写进一个 `_来源` 键）
    parts = re.split(r"[+；;]", s)
    for p in parts:
        if p.strip() == "":
            return "`来源` 含空段（多余或缺失的 `+`）：%r" % s
        if p.strip() not in names:
            return "`来源` 含未知来源名 %r（合法：%s）" % (p.strip(), sorted(names))
    if len(parts) != len(set(parts)):
        return "`来源` 有重复来源：%r" % s
    return None
DEFAULT_VERDICT_VALUES: set[str] = {"属于本类目", "不属于本类目"}

# 用空值写法的黑名单：这些字符串**不应**用来表示空（应为 ""）
_NEG_EMPTY = {"null", "None", "无", "—", "-", "N/A", "na"}


def _out_of_vocab_words(std: str, values: Sequence[str], shape: str) -> list[str]:
    """返回 `标准` 串里**不在词表内**的词（single → 整个串；multi → 按分隔符切词）。"""
    if not std:
        return []
    if shape == "multi":
        words = [w.strip() for w in re.split(r"[、,，;；+/]", std) if w.strip()]
        return [w for w in words if w not in values]
    return [std] if std not in values else []


# ================================================================ 规则源
@dataclass
class AttrSpec:
    """一个属性的规格（全部来自类目配置，非硬编码）"""
    name: str
    values: list[str] = field(default_factory=list)
    unit: str = ""
    type: str = "enum"
    shape: str = "single"           # single | multi
    # 2026-09-21 用户方案 A：词表放权等级
    #   closed（默认，现行为）   词表外值 = ERR 硬拦（物理枚举应保持 closed）
    #   suggestive              词表外值允许，但必须有「证据原文」，并聚合为"新词候选"交审
    #   open                    词表外值完全放开，仅计入新词候选（不要求依据）
    vocab_policy: str = "closed"    # closed | suggestive | open


# 新格式(rule/subcols/params)的 rule → 旧 AttrSpec 字段映射
_MODERN_TYPE_MAP = {
    "enum": "enum", "color": "enum", "noise": "enum", "material": "enum",
    "multi": "multi", "layer_material": "multi", "count_modes": "multi",
    "numeric": "numeric", "open": "open", "ocr": "open",
}
_MODERN_MULTI = {"multi", "layer_material", "count_modes"}


def attr_spec_from_modern(a: Mapping[str, Any]) -> "AttrSpec":
    """把新格式规则(rule/subcols/params)映射成旧闸门用的 AttrSpec。

    新格式受控值在 `params.values`(enum/multi/color/noise) 或 `params.main_map` 的键(material);
    numeric/open/ocr 无词表。vocab_policy: open/ocr 放开, 其余 closed。
    """
    rule = str(a.get("rule") or "enum")
    params = a.get("params") if isinstance(a.get("params"), dict) else {}
    values: list[str] = []
    if isinstance(params.get("values"), list):
        values = [str(v) for v in params["values"]]
    elif isinstance(params.get("main_map"), dict):
        values = [str(k) for k in params["main_map"]]
    unit = str(params.get("unit") or "")
    return AttrSpec(
        name=str(a.get("name") or ""),
        values=values,
        unit=unit,
        type=_MODERN_TYPE_MAP.get(rule, "enum"),
        shape="multi" if rule in _MODERN_MULTI else "single",
        vocab_policy="open" if rule in ("open", "ocr") else "closed",
    )


@dataclass
class RuleSource:
    """类目规则源：闸门校验时引用它，但规则本身来自类目配置/提示词。"""
    attrs: dict[str, AttrSpec] = field(default_factory=dict)
    detail_classes: set[str] = field(default_factory=set)
    top_keys: set[str] = field(default_factory=lambda: set(DEFAULT_TOP_KEYS))
    allow_keys: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOW_KEYS))
    source_names: set[str] = field(default_factory=lambda: set(DEFAULT_SOURCE_NAMES))
    warnings: list[str] = field(default_factory=list)
    verdict_values: set[str] = field(default_factory=lambda: set(DEFAULT_VERDICT_VALUES))

    # ---------------------------------------------------------- 加载
    @classmethod
    def load(
        cls,
        cat_dir: str,
        cat: Optional[str] = None,
        *,
        top_keys: Optional[Iterable[str]] = None,
        allow_keys: Optional[Iterable[str]] = None,
    ) -> "RuleSource":
        """从类目目录读规则 —— 走统一加载器（4 份 → 2 份收敛后仍可用）。

        cat_dir:   存放 `*.rules.json`/`*.ledger.json` 的目录（旧 4 份布局自动兼容）
        cat:       类目 ID。省略时取目录下唯一的类目。
        top_keys:  覆盖**必需**行级键集合（不同 schema 版本用）。
        allow_keys:覆盖**允许但不要求**的行级键集合（本类目合法扩展）。
        """
        try:
            from .catrules import load_category      # 包内
        except ImportError:                          # 直接当脚本/被拷走单文件用
            try:
                from catrules import load_category   # type: ignore
            except ImportError:
                load_category = None                 # type: ignore

        if load_category is not None:
            cf = load_category(cat_dir, cat)
            cat = cf.cat
            attrs: dict[str, AttrSpec] = {}
            for a in cf.attributes:
                if not isinstance(a, dict) or not a.get("name"):
                    continue
                if "rule" in a or "params" in a:
                    # 新格式(rule/subcols/params) → 映射成闸门用的 AttrSpec
                    attrs[a["name"]] = attr_spec_from_modern(a)
                else:
                    attrs[a["name"]] = AttrSpec(
                        name=a["name"],
                        values=[str(v) for v in (a.get("values") or [])],
                        unit=str(a.get("unit") or ""),
                        type=str(a.get("type") or "enum"),
                        shape=str(a.get("value_shape") or "single"),
                        vocab_policy=str(a.get("vocab_policy") or "closed"),
                    )
            rs = cls(attrs=attrs, detail_classes=cf.detail_classes)
            # 行级键优先级：显式参数 > 类目配置 > 代码默认值
            cfg_top = cf.main.get("top_keys")
            cfg_allow = cf.main.get("allow_keys")
            if top_keys is not None:
                rs.top_keys = set(top_keys)
            elif isinstance(cfg_top, (list, tuple, set)) and cfg_top:
                rs.top_keys = {str(k) for k in cfg_top}
            if allow_keys is not None:
                rs.allow_keys = set(allow_keys)
            elif isinstance(cfg_allow, (list, tuple, set)):
                rs.allow_keys = {str(k) for k in cfg_allow}
            rs.warnings = list(cf.warnings)
            return rs

        # ---------------- 兜底：没有 catrules 时按旧 4 份布局自读
        return cls._load_legacy(cat_dir, cat, top_keys, allow_keys)

    @classmethod
    def _load_legacy(cls, cat_dir, cat, top_keys, allow_keys=None):
        if cat is None:
            names = [f[:-5] for f in sorted(os.listdir(cat_dir))
                     if f.endswith(".json") and not f.startswith("_")
                     and not f.endswith((".dict.json", ".detail.json", ".values.json"))]
            if len(names) != 1:
                raise ValueError(
                    "cat_dir 下有 %d 个类目 json，请显式指定 cat：%s" % (len(names), names))
            cat = names[0]

        main_path = os.path.join(cat_dir, "%s.json" % cat)
        with open(main_path, encoding="utf-8") as fh:
            main = json.load(fh)

        attrs: dict[str, AttrSpec] = {}
        for a in (main.get("attributes") or []):
            if not isinstance(a, dict) or not a.get("name"):
                continue
            attrs[a["name"]] = AttrSpec(
                name=a["name"],
                values=[str(v) for v in (a.get("values") or [])],
                unit=str(a.get("unit") or ""),
                type=str(a.get("type") or "enum"),
                shape=str(a.get("value_shape") or "single"),
                vocab_policy=str(a.get("vocab_policy") or "closed"),
            )

        detail_classes: set[str] = set()
        dp = os.path.join(cat_dir, "%s.detail.json" % cat)
        if os.path.exists(dp):
            with open(dp, encoding="utf-8") as fh:
                det = json.load(fh)
            for c in ((det.get("grind_detail") or {}).get("categories") or []):
                if isinstance(c, (list, tuple)) and c:
                    detail_classes.add(str(c[0]))
                    if len(c) > 1 and isinstance(c[1], str):
                        detail_classes.add(c[1].split("，")[0].strip())

        rs = cls(attrs=attrs, detail_classes=detail_classes)
        if top_keys is not None:
            rs.top_keys = set(top_keys)
        elif isinstance(main.get("top_keys"), (list, tuple, set)) and main["top_keys"]:
            rs.top_keys = {str(k) for k in main["top_keys"]}
        if allow_keys is not None:
            rs.allow_keys = set(allow_keys)
        elif isinstance(main.get("allow_keys"), (list, tuple, set)):
            rs.allow_keys = {str(k) for k in main["allow_keys"]}
        return rs

    # ---------------------------------------------------------- 自检
    def lint(self) -> list[str]:
        """基础一致性自检：配置内部是否自相矛盾。返回问题列表（空=健康）。"""
        problems: list[str] = []
        for name, spec in self.attrs.items():
            dup = [v for v in set(spec.values) if spec.values.count(v) > 1]
            if dup:
                problems.append("属性 %s 的 values 有重复项：%s" % (name, dup))
            bad = [v for v in spec.values if v in RESERVED_VALUES]
            if bad:
                problems.append(
                    "属性 %s 的 values 含保留标记 %s —— 保留标记不是受控值，"
                    "列进词表会让模型把它当合法枚举成员选中" % (name, bad))
        return problems


def lint_category_words(source_text: str) -> list[str]:
    """检查某段源码里是否出现了类目词（返回命中的词）。

    用途：给闸门当自检 —— **闸门代码里不得出现任何类目词**。
    一旦出现，说明把类目知识写死进了代码，等同于回到「代码做判断」的旧架构；
    需要类目信息时必须从 `RuleSource` 读。

    ⚠️ 两个坑（都踩过）：
      ① 只剥 `'''` 不剥 `\"\"\"` 的文档字符串 → 例子里的词会被算成代码；
      ② 把类目词写成**字面量** → 检查用的词表自身就在源码里，永远自检失败。
         故此处用 unicode 转义构造词表，源码里不出现这些字。
    """
    body = re.sub(r'"""(?:.|\n)*?"""', "", source_text, count=1)
    body = re.sub(r"'''(?:.|\n)*?'''", "", body, count=1)
    words = ["\u7814\u78e8", "\u80e1\u6912", "\u4e0d\u9508\u94a2",
             "\u53a8\u623f", "\u9676\u74f7", "\u4e9a\u514b\u529b"]
    return [w for w in words if w in body]


# ================================================================ 展平与分格
def cell_key(path: str) -> str:
    return path


def flatten_row(row: Mapping[str, Any], top_keys: Optional[set[str]] = None) -> dict[str, Any]:
    """把一行结果展平成 `{格路径: 值}`。

    - `attributes.<属性>.<字段>` → `attr.<属性>.<字段>`
    - `attributes.<属性>`（非 dict） → `attr.<属性>`
    - 其余 dict 展开一层；标量原样
    """
    out: dict[str, Any] = {}
    for k, v in (row or {}).items():
        if k == "xl_row":
            continue
        if k == "attributes" and isinstance(v, dict):
            for a, f in v.items():
                if isinstance(f, dict):
                    for f2, v2 in f.items():
                        out["attr.%s.%s" % (a, f2)] = v2
                else:
                    out["attr.%s" % a] = f
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                out["%s.%s" % (k, k2)] = v2
        else:
            out[k] = v
    return out


def cell_kind(path: str) -> str:
    """分格类别：free（自由文本）/ proc（过程记录）/ value（交付值）。

    新 schema(2026-09-24): `attr.<属性>` = 交付值(value),
      `attr.<属性>_来源` = 来源(free, 逐次写法可能不同但语义等价, 不计入一致率)。
    2026-10-09 追加: `attr.<属性>_依据` = **原文依据**(同样 free —— 引哪一句原文
      逐次本来就会不同, 算进一致率只会把指标压到无意义)。
    """
    f = path.rsplit(".", 1)[-1]
    if f in FREE_TEXT_FIELDS:
        return "free"
    if path in PROCESS_FIELDS:
        return "proc"
    if isinstance(f, str) and (f.endswith(_SOURCE_SUFFIX) or f.endswith(_BASIS_SUFFIX)):
        return "free"
    return "value"


def _key_of(v: Any) -> str:
    """语义键：数值归一、数组归一，避免 `2` 与 `2.0` 被当成不同值。"""
    if isinstance(v, list):
        return "L:" + json.dumps([str(x).strip() for x in v], ensure_ascii=False)
    try:
        t = str(v).strip()
        if t:
            return "N:%.6f" % float(t)
    except (TypeError, ValueError):
        pass
    return "S:" + str(v).strip()


# ================================================================ 结构闸门
@dataclass
class GateIssue:
    level: str          # ERR | WARN
    xl_row: Optional[int]
    field: str
    why: str
    kind: str = ERR_DATA   # data(数据问题,必拦) | schema(登记缺口,只记)
    required: bool = False  # 完整性错误：宽松标注也必须补跑或阻断


@dataclass
class GateResult:
    tag: str
    rows: int = 0
    errors: int = 0
    warnings: int = 0
    issues: list[GateIssue] = field(default_factory=list)

    @property
    def required_errors(self) -> int:
        return sum(i.level == "ERR" and i.required for i in self.issues)

    @property
    def verdict(self) -> str:
        """只有**数据类** ERR 才拦。schema 类（行级多键）记录不拦截。"""
        return "REJECT" if (self.errors and self.data_errors) else "PASS"

    @property
    def err_issues(self) -> list[GateIssue]:
        return [i for i in self.issues if i.level == "ERR"]

    @property
    def err_kinds(self) -> dict[str, int]:
        """ERR 按 kind 拆分：{"data": n, "schema": n, "quality": n}"""
        out = {ERR_DATA: 0, ERR_SCHEMA: 0, ERR_QUALITY: 0}
        for i in self.err_issues:
            k = i.kind if i.kind in out else ERR_DATA
            out[k] += 1
        return out

    @property
    def data_errors(self) -> int:
        return self.err_kinds[ERR_DATA]

    @property
    def schema_errors(self) -> int:
        return self.err_kinds[ERR_SCHEMA]

    @property
    def quality_errors(self) -> int:
        return self.err_kinds[ERR_QUALITY]

    @property
    def rows_with_err(self) -> int:
        """出**数据类** ERR 的行数（去重）。用于区分「1308 行各错 1 条」
        和「1 行错 1308 条」——前者是整列问题，后者是单行问题。"""
        return len({i.xl_row for i in self.err_issues
                    if i.kind == ERR_DATA and i.xl_row is not None})

    @property
    def err_summary(self) -> str:
        if not self.errors:
            return ""
        return "ERR %d(data %d/行 %d · schema %d · quality %d)" % (
            self.errors, self.data_errors, self.rows_with_err, self.schema_errors,
            self.quality_errors)


# ★ 2026-10-27 用户口径: 数值 / 组合 / 自由文本型属性的「受控值」只是历史示例,
#   新商品必然产新值 —— 词表越界**直接放行**（收进"新词候选"），不判 ERR。
_NUMERIC_RE = re.compile(r"\d")
_COMBO_RE = re.compile(r"[、＋+;,；]")


def is_loose_vocab_attr(spec) -> bool:
    """属性是否属于「数值 / 组合 / 自由文本」—— 是则词表越界直接放行。

    判据(任一):
      · type == "numeric"（噪音/重量/水箱容量…）；
      · shape == "multi"（声明为多选）；
      · 受控值 ≥60% 含数字（尺寸 "14.45\\"D…" / 覆盖面积 "1000 sq.ft" / 除湿量 "50 pints/day"）；
      · 受控值 ≥60% 含组合分隔符（适用场景 "地下室、居家房间"）。
    """
    if str(getattr(spec, "type", "") or "").strip().lower() == "numeric":
        return True
    if str(getattr(spec, "shape", "") or "").strip().lower() == "multi":
        return True
    vals = [str(v) for v in (getattr(spec, "values", []) or [])]
    n = len(vals)
    if not n:
        return False
    num = sum(1 for v in vals if _NUMERIC_RE.search(v))
    combo = sum(1 for v in vals if _COMBO_RE.search(v))
    return num >= max(1, int(n * 0.6)) or combo >= max(1, int(n * 0.6))


def check_row(
    row: Mapping[str, Any],
    rules: RuleSource,
    inp: Optional[Mapping[int, Mapping[str, Any]]] = None,
    new_vocab: Optional[list] = None,
    require_basis: Optional[bool] = None,
) -> list[GateIssue]:
    """校验一行的**结构合法性**。不判断取值对错（那是语义层）。

    inp: 可选，输入行映射 `{xl_row: 原始输入}`。提供时启用**交叉校验**
         ——自洽检查查不出「整列漏填」，与输入比对才能发现。
    new_vocab: 可选，收集器 —— 非 closed 属性里出现的词表外词会 append 进来
         （方案 A/B 的"新词候选"，供独立初审与批量确认）。
    """
    issues: list[GateIssue] = []

    def bad(field: str, why: str, level: str = "ERR",
            kind: str = ERR_DATA, required: bool = False) -> None:
        issues.append(GateIssue(level, row.get("xl_row"), field, why, kind, required))

    if not isinstance(row, Mapping):
        return [GateIssue("ERR", None, "(row)", "不是对象", required=True)]

    # --- A 结构：行级键
    # 缺键 = 整列静默漏填 → data(必拦)；多键 = 类目扩展未登记 → schema(只记录)。
    # allow_keys 里的键**允许出现但不要求出现**（见 DEFAULT_TOP_KEYS 上方说明）。
    permitted = rules.top_keys | rules.allow_keys
    miss = rules.top_keys - set(row)
    extra = set(row) - permitted
    if miss:
        bad("(行级)", "缺键 %s" % sorted(miss), required=True)
    if extra:
        bad("(行级)", "多出未登记键 %s（若是本类目合法扩展键，"
                      "请写入 <cat>.rules.json 的 allow_keys；"
                      "若它是必需的，写入 top_keys）" % sorted(extra),
            ERR_DATA, ERR_SCHEMA)
    wrapper_ok = not miss

    # --- B 枚举：类目边界判定
    cc = row.get("category_check") or {}
    if isinstance(cc, Mapping) and cc.get("判定") not in rules.verdict_values:
        bad("category_check.判定", "非法枚举 %r" % cc.get("判定"))

    # --- C 与输入交叉校验：抓「整列漏填」
    sd = row.get("尺寸_拆列")
    if inp and isinstance(sd, Mapping):
        it = inp.get(row.get("xl_row")) or {}
        raw = str(it.get("size") or "")
        for dim in ("长", "宽", "高"):
            if dim in raw and str(sd.get(dim) or "").strip() == "":
                bad("尺寸_拆列.%s" % dim,
                    "输入 size=%r 含「%s」字样，但该维度为空（漏填）"
                    % (raw[:26], dim), required=True)

    # --- 属性级
    A = row.get("attributes")
    if not isinstance(A, Mapping):
        bad("attributes", "不是对象", required=True)
        return issues
    if not wrapper_ok:
        # 包装层版本不符时只报行级错；再逐属性报会把同一原因刷成几百条噪声
        return issues

    # attributes 平铺为「值键 + 来源键 + 依据键」:
    #   值键=属性名, 来源键=属性名_来源(标签), 依据键=属性名_依据(**原文**)。
    # 2026-10-09 新增依据键（用户要求）：**每个属性都要给原文依据** ——
    #   事后校验（跨属性冲突纠正 / 体检复核）要判「两个属性矛盾时该改哪一侧」，
    #   而只看 `_来源` 标签判不了（冲突两侧常常标签相同）。实测当时 18 个属性里
    #   **只有「尺寸」一个有 `_依据`** → 纠正只能靠猜 → 把正确数据改错。
    value_keys: set[str] = set()
    source_keys: dict[str, str] = {}   # 属性名 -> 来源值
    basis_keys: dict[str, str] = {}    # 属性名 -> 依据原文
    for k, v in A.items():
        if isinstance(k, str) and k.endswith(_BASIS_SUFFIX):
            basis_keys[k[: -len(_BASIS_SUFFIX)]] = v
        elif isinstance(k, str) and k.endswith(_SOURCE_SUFFIX):
            source_keys[k[: -len(_SOURCE_SUFFIX)]] = v
        else:
            value_keys.add(k)

    unknown = value_keys - set(rules.attrs)
    missing = set(rules.attrs) - value_keys
    if unknown:
        bad("attributes", "出现未定义属性 %s" % sorted(unknown))
    if missing:
        bad("attributes", "漏属性 %s" % sorted(missing), required=True)
    blank_values = [n for n in rules.attrs if n in A and
                    (A[n] is None or (isinstance(A[n], str) and not A[n].strip()))]
    if blank_values:
        bad("attributes", "属性值为空 %s" % sorted(blank_values), required=True)

    # 来源键必须与值键一一对应: 有值无来源 / 有来源无值 都算结构错
    extra_src = set(source_keys) - set(rules.attrs)
    lack_src = set(rules.attrs) - set(source_keys)
    if extra_src:
        bad("attributes", "出现未定义属性的来源键 %s" % sorted("%s_来源" % a for a in extra_src))
    if lack_src:
        bad("attributes", "漏来源键 %s" % sorted("%s_来源" % a for a in lack_src), required=True)

    # 依据键：**每个属性都要有**，且不能是空串（空 = 整列静默漏填，属 ERR_DATA）。
    #   可由 `ATTR_STAB_REQUIRE_BASIS=false` 关掉（灰度/兼容旧产物时用）。
    extra_basis = set(basis_keys) - set(rules.attrs)
    if extra_basis:
        bad("attributes", "出现未定义属性的依据键 %s" % sorted("%s_依据" % a for a in extra_basis))
    basis_required = _REQUIRE_BASIS if require_basis is None else require_basis
    if basis_required:
        lack_basis = set(rules.attrs) - set(basis_keys)
        if lack_basis:
            bad("attributes", "漏依据键 %s（每个属性都要写 `_依据` 原文，见提示词 §3.3e）"
                % sorted("%s_依据" % a for a in lack_basis), required=True)
        empty_basis = [a for a in rules.attrs
                       if a in basis_keys and not str(basis_keys.get(a) or "").strip()]
        if empty_basis:
            bad("attributes", "依据键为空 %s（空串等于没写）"
                % sorted("%s_依据" % a for a in empty_basis), required=True)

    declared_out = [str(x).strip() for x in (row.get("词表外词") or [])]

    for name in rules.attrs:
        spec = rules.attrs.get(name) or AttrSpec(name=name)
        std = str(A.get(name) or "").strip()

        # --- E 词表：值必须落在受控值内（保留标记除外）
        if std and std not in RESERVED_VALUES and spec.values:
            policy = (spec.vocab_policy or "closed").strip().lower()
            out_words = _out_of_vocab_words(std, spec.values, spec.shape)
            if policy == "closed":
                if is_loose_vocab_attr(spec):
                    # ★ 2026-10-27 用户口径: 数值/组合/自由文本型属性, 词表越界**直接放行**,
                    #   只把新值收进"新词候选"(供步骤18 聚类), 不判 ERR_QUALITY。
                    for w in out_words:
                        if new_vocab is not None:
                            new_vocab.append({
                                "属性": name, "词": w, "行号": row.get("xl_row"),
                                "policy": "open",
                            })
                # 现行为：单选词表外 = ERR；多值允许词表外词但必须列入 `词表外词`
                elif spec.shape == "multi":
                    undeclared = [w for w in out_words if w not in declared_out]
                    if undeclared:
                        bad(name, "多值串含词表外词但未列入 `词表外词`：%s"
                            % "、".join(undeclared), "WARN")
                elif out_words:
                    bad(name, "`%s` 不在受控值词表内：%r" % (name, std[:40]),
                        kind=ERR_QUALITY)
            else:
                # 2026-09-21 方案 A：suggestive / open —— 词表外**不再 ERR**，
                # 但统一收集为"新词候选"(供方案 B 的独立初审 + 批量确认)。
                for w in out_words:
                    if new_vocab is not None:
                        new_vocab.append({
                            "属性": name, "词": w, "行号": row.get("xl_row"),
                            "policy": policy,
                        })

        # --- 空值写法(值 + 来源都查)
        if std in _NEG_EMPTY:
            bad(name, "`%s` 用了非法空值写法 %r" % (name, std), "WARN")
        src = str(source_keys.get(name) or "").strip()
        src_err = check_source_value(src if src else "", rules.source_names)
        if src_err:
            bad(name + _SOURCE_SUFFIX, src_err, kind=ERR_QUALITY)

    # --- 数组字段的键集合
    for arr, keys in (("冲突", {"属性", "取值", "依据"}),
                      ("跨列纠错", {"原属性", "原值", "建议归入"})):
        o = row.get(arr)
        if not isinstance(o, list):
            # ★ 2026-10-26: 辅助数组写成非数组只是"上报格式"问题, 不该拦整份交付 → 降为 WARN
            bad(arr, "不是数组", "WARN")
            continue
        for i, e in enumerate(o):
            if not isinstance(e, Mapping):
                bad("%s[%d]" % (arr, i), "不是对象")
                continue
            if set(e) != keys:
                bad("%s[%d]" % (arr, i), "键集合应为 %s，实为 %s"
                    % (sorted(keys), sorted(e)))
    if not isinstance(row.get("词表外词"), list):
        # ★ 2026-10-26: 同上, 上报字段格式问题 → WARN（实测 121 行写成非数组）
        bad("词表外词", "不是数组", "WARN")
    return issues


def gate_run(
    rows: Sequence[Mapping[str, Any]],
    rules: RuleSource,
    inp: Optional[Mapping[int, Mapping[str, Any]]] = None,
    tag: str = "",
    new_vocab: Optional[list] = None,
    require_basis: Optional[bool] = None,
) -> GateResult:
    """对一份运行结果跑闸门。`new_vocab` 若提供，收集非 closed 属性的词表外词。"""
    res = GateResult(tag=tag, rows=len(rows))
    if not rows:
        res.issues.append(GateIssue("ERR", None, "(rows)", "无有效行", required=True))
        res.errors = 1
        return res
    if inp is not None:
        ids = [r.get("xl_row") for r in rows if isinstance(r, Mapping)]
        actual = set(ids)
        for why in (
            "缺输入行 %s" % sorted(set(inp) - actual, key=str) if set(inp) - actual else "",
            "额外输出行 %s" % sorted(actual - set(inp), key=str) if actual - set(inp) else "",
            "输出行号重复" if len(ids) != len(actual) else "",
        ):
            if why:
                res.issues.append(GateIssue("ERR", None, "(rows)", why, required=True))
                res.errors += 1
    for r in rows:
        for it in check_row(r, rules, inp, new_vocab=new_vocab, require_basis=require_basis):
            res.issues.append(it)
            if it.level == "ERR":
                res.errors += 1
            else:
                res.warnings += 1
    return res


# ================================================================ 一致性
def _runs_to_flat(runs: Mapping[str, Mapping[int, Mapping[str, Any]]]) -> dict[str, dict[int, dict]]:
    return {t: {x: flatten_row(r) for x, r in rows.items()} for t, rows in runs.items()}


def alone_rates(
    runs: Mapping[str, Mapping[int, Mapping[str, Any]]],
    tags: Optional[Sequence[str]] = None,
) -> dict[str, float]:
    """各运行的「独异格率」（%）：与众数不同意的格占其可比格的比例。

    ⚠️ 这是**诊断指标**，不能拿它当成绩。它衡量的是「这一份运行有多离群」。
    """
    flat = _runs_to_flat(runs)
    tags = list(tags or sorted(flat))
    row_ids = sorted(set().union(*[set(flat[t]) for t in tags]))
    paths = sorted(set().union(*[set(flat[t][x]) for t in tags for x in flat[t]
                                if x in flat[t]]))
    alone: dict[str, int] = {t: 0 for t in tags}
    total: dict[str, int] = {t: 0 for t in tags}
    for x in row_ids:
        for p in paths:
            _f = p.rsplit(".", 1)[-1]
            # free 文本不计入独异格率：`_来源`（标签）与 `_依据`（原文）逐次写法不同，
            # 算进去会把指标压到无意义（`_依据` 是 2026-10-09 新加的同类字段）。
            if _f in FREE_TEXT_FIELDS or _f.endswith(_SOURCE_SUFFIX) or _f.endswith(_BASIS_SUFFIX):
                continue
            vals = [flat[t].get(x, {}).get(p, MISSING_KEY) for t in tags]
            ks = [_key_of(v) for v in vals]
            top = _most_common(ks)
            for t, k in zip(tags, ks):
                total[t] += 1
                if k != top:
                    alone[t] += 1
    return {t: (100.0 * alone[t] / total[t] if total[t] else 0.0) for t in tags}


def _most_common(keys: Sequence[str]) -> str:
    counts: dict[str, int] = {}
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def union_cells(
    runs: Mapping[str, Mapping[int, Mapping[str, Any]]],
    used: Optional[Sequence[str]] = None,
) -> dict[str, dict[str, Any]]:
    """**并集口径**聚合：收集每一格被提名过的**所有**值，不丢少数意见。

    为什么需要它（与 `majority` 的区别）
    ------------------------------------------------------------------
    多运行的目的有两种，聚合方向**相反**：

      · **一致性**（`majority`）：要"5 份跑出同一个答案" → 剔除离群运行、丢少数意见
      · **补充召回**（本函数）：一次标注提不全，多跑几次把漏掉的收进来
        → 必须取**并集**，丢少数就等于丢召回

    实测（12 行 × 25 属性 × 5 份）：并集 226 格有真值，最好单次 221 格，
    且存在 **4 格是单份独家找到**（连最好那份都没有）—— 这些正是 `majority` 会丢掉的。

    返回 `{"<行>|<格>": {mode, agree, values:[{value, by:[运行]}], n_votes, kind}}`
      · `mode`  主值：有明确多数提名时取多数；否则留空 `""` 并置 `needs_review=True`
      · `values` 全部提名（含各自由哪些运行提出）→ 写进候选值/待裁定队列
    """
    flat = _runs_to_flat(runs)
    tags = list(used or sorted(flat))
    if not tags:
        return {}
    row_ids = sorted(set().union(*[set(flat[t]) for t in tags]))
    paths = sorted(set().union(*[set(flat[t][x]) for t in tags for x in flat[t]
                                if x in flat[t]]))
    out: dict[str, dict[str, Any]] = {}
    for x in row_ids:
        for p in paths:
            vals = [flat[t].get(x, {}).get(p, MISSING_KEY) for t in tags]
            groups: dict[str, dict[str, Any]] = {}
            for t, v in zip(tags, vals):
                k = _key_of(v)
                g = groups.setdefault(k, {"value": v, "by": []})
                g["by"].append(t)
            ordered = sorted(groups.values(), key=lambda g: -len(g["by"]))
            n_votes = len(ordered)
            # 哨兵不得当选：它代表「所有运行都没有这个格」, 交付值必须留空。
            top_is_missing = _key_of(ordered[0]["value"]) == _key_of(MISSING_KEY)
            mode = "" if top_is_missing else ordered[0]["value"]
            agree = n_votes == 1
            # 有明确多数（>50%）才敢当主值；否则留空交人工，避免"猜一个"
            majority_ok = (not top_is_missing) and (
                agree or len(ordered[0]["by"]) * 2 > len(tags))
            out["%s|%s" % (x, p)] = {
                "mode": mode if majority_ok else "",
                "agree": agree,
                "values": ordered,
                "n_votes": n_votes,
                "kind": cell_kind(p),
                "needs_review": not agree,
                "no_majority": not majority_ok,
            }
    return out


def _fingerprint(v: Any) -> tuple:
    """「内容指纹」：抽出数字与字符（忽略顺序、空白、分隔符），用于判断
    两个提名是不是**只是写法不同**。

    例（指纹相同 → 仅写法差异，不必人工裁定）：
      `size：2.36 inch宽 × 7.87 inch高`
      `size：宽 2.36 inch × 高 7.87`
      `size：宽2.36 × 高7.87 inch`
    例（指纹不同 → 真·提名分歧，需裁定）：
      `机身旋钮` vs `外部齿轮旋钮`
      `内置可充电电池（USB充电）` vs `内置可充电电池`
    """
    s = str(v or '')
    digits = ''.join(sorted(re.findall(r'\d', s)))
    chars = ''.join(sorted(c for c in s if not c.isspace() and not c.isdigit()))
    return (digits, chars)


def review_queue(
    cells: Mapping[str, Mapping[str, Any]],
    include_process: bool = False,
    real_conflicts_only: bool = False,
    scope: str = "standard",
) -> list[dict[str, Any]]:
    """把「需要裁定的格」整理成人工复核队列（按格出现的不同提名数降序）。

    这就是"补充"的产物：多跑几份不是为了得到更多格，而是把**口径分歧暴露出来**，
    交人一眼裁定。实测 12 行 × 25 属性 × 5 份出 29 格不同提名，例如：
      `供电方式` 有的给 `内置可充电电池（USB充电）`、有的给 `内置可充电电池`
      `adjustment_type` 有的给 `机身旋钮`、有的给 `外部齿轮旋钮`

    `scope`（**重要**，决定这个队列能不能用）：
      · `"standard"`（默认）只收 `attr.<属性>` —— **交付值字段**（新 schema 平铺,
        值键=属性名, 来源键=属性名_来源 已由 cell_kind 归 free 排除）。
        这才是"口径分歧"：不同运行在同一属性上选了不同受控值，人一眼可裁定。
      · `"all"` 收全部字段。**不建议**：来源键等是文本型, 写法差异天然很多,
        会把队列淹成几百条噪声。

    `include_process=False`（默认）**不含** `冲突`/`跨列纠错`：
    那两个是**过程记录**——不同运行走了不同推理路径，记录到的矛盾本来就不同，
    逐格比对没有意义。它们应当取**并集**交人工浏览（见 `process_union`）。
    """
    q = []
    for key, d in cells.items():
        kind = d.get("kind")
        if kind == "free" or d.get("agree") or d.get("conflict_resolution") == "多数确定":
            continue
        if kind == "proc" and not include_process:
            continue
        rid, _, path = key.partition("|")
        # 交付值格 = attr.<属性>(value 类); 来源格 attr.<属性>_来源 已被 cell_kind 归 free
        if scope == "standard" and kind != "value":
            continue
        fps = {_fingerprint(g["value"]) for g in d["values"]}
        fmt_only = len(fps) == 1
        if real_conflicts_only and fmt_only:
            continue
        q.append({
            "行号": int(rid) if rid.isdigit() else rid,
            "字段": path,
            "类别": {"value": "交付值", "proc": "过程记录"}.get(kind, kind),
            "提名": [{"值": g["value"], "提出运行": g["by"]} for g in d["values"]],
            "提名数": d["n_votes"],
            "有明确多数": not d.get("no_majority"),
            "疑似仅写法差异": fmt_only,
            "当前主值": d.get("mode") or "（留空待裁定）",
        })
    q.sort(key=lambda r: (r["疑似仅写法差异"], -r["提名数"], r["行号"]))
    return q


def process_union(
    cells: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[Any]]:
    """过程记录字段（`冲突`/`跨列纠错`）的**并集**：把各运行报出的条目合并去重。

    为什么单独处理：它们记录「判定过程中遇到了什么矛盾」，不是交付值。
    不同运行走不同路径、遇到的矛盾自然不同 → 一致率结构性低（实测 0%），
    **但它们的并集是有价值的**：各运行发现的矛盾合并起来才是完整清单。
    """
    out: dict[str, list[Any]] = {}
    for key, d in cells.items():
        if d.get("kind") != "proc":
            continue
        rid, _, path = key.partition("|")
        bucket = out.setdefault(path, [])
        for g in d["values"]:
            v = g["value"]
            items = v if isinstance(v, list) else [v]
            for it in items:
                s = json.dumps(it, ensure_ascii=False, sort_keys=True) \
                    if isinstance(it, (dict, list)) else str(it)
                if not any(json.dumps(x, ensure_ascii=False, sort_keys=True) == s
                           if isinstance(x, (dict, list)) else str(x) == s
                           for x in bucket):
                    bucket.append(it)
    return out


def majority(
    runs: Mapping[str, Mapping[int, Mapping[str, Any]]],
    used: Optional[Sequence[str]] = None,
) -> dict[str, dict[str, Any]]:
    """多数表决：`{"<行>|<格路径>": {mode, agree, n, total, vals, kind}}`"""
    flat = _runs_to_flat(runs)
    tags = list(used or sorted(flat))
    if not tags:
        return {}
    row_ids = sorted(set().union(*[set(flat[t]) for t in tags]))
    paths = sorted(set().union(*[set(flat[t][x]) for t in tags for x in flat[t]
                                if x in flat[t]]))
    out: dict[str, dict[str, Any]] = {}
    for x in row_ids:
        for p in paths:
            vals = [flat[t].get(x, {}).get(p, MISSING_KEY) for t in tags]
            ks = [_key_of(v) for v in vals]
            counts: dict[str, int] = {}
            for k in ks:
                counts[k] = counts.get(k, 0) + 1
            top = max(counts.items(), key=lambda kv: kv[1])
            mode = next(v for v, k in zip(vals, ks) if k == top[0])
            if _key_of(mode) == _key_of(MISSING_KEY):
                mode = ""      # 哨兵不得成为交付值(同 union_cells)
            out["%s|%s" % (x, p)] = {
                "mode": mode, "agree": len(counts) == 1, "n": top[1],
                "total": len(tags), "vals": vals, "kind": cell_kind(p),
            }
    return out


def consistency(
    runs: Mapping[str, Mapping[int, Mapping[str, Any]]],
    used: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """三类字段的一致率 + 总体。

    返回 `{"value": {"ok","n","rate"}, "proc": ..., "free": ..., "combined": {...}}`
    其中 `value` = 交付值字段（应当要求高）、`proc` = 过程记录字段（结构性偏低）、
    `free` = 自由文本（不计入交付判定）。
    """
    cells = majority(runs, used)
    acc: dict[str, list[int]] = {k: [0, 0] for k in ("value", "proc", "free")}
    drift: dict[str, int] = {}
    for key, d in cells.items():
        kind = d["kind"]
        acc[kind][1] += 1
        if d["agree"]:
            acc[kind][0] += 1
        elif kind != "free":
            # 自由文本（证据原文等）逐次必然不同，列进"残余分歧"会把真问题淹掉
            f = key.split("|", 1)[1].rsplit(".", 1)[-1]
            drift[f] = drift.get(f, 0) + 1
    res: dict[str, Any] = {}
    for kind in ("value", "proc", "free"):
        ok, n = acc[kind]
        res[kind] = {"ok": ok, "n": n, "rate": round(100.0 * ok / n, 4) if n else None}
    ok = acc["value"][0] + acc["proc"][0]
    n = acc["value"][1] + acc["proc"][1]
    res["combined"] = {"ok": ok, "n": n, "rate": round(100.0 * ok / n, 4) if n else None}
    res["drift_by_field"] = dict(sorted(drift.items(), key=lambda kv: -kv[1]))
    return res


def deliverable_from_cells(
    cells: Mapping[str, Mapping[str, Any]],
    row_ids: Sequence[int],
) -> list[dict[str, Any]]:
    """把表决结果装配成交付表(新 schema 平铺)：`[{"xl_row":…, "attributes": {属性: 值, 属性_来源: 来源}}]`

    O(格数) 一遍扫描按行号归组。原实现是「逐行扫全部格」= O(行数×格数)，
    2466 行 × 11 万格 ≈ 2.7 亿次迭代，纯 Python 要跑几分钟（2026-09-24 实测卡顿）。
    """
    from collections import defaultdict

    by_row: dict[int, dict[str, Any]] = defaultdict(dict)
    for key, d in cells.items():
        rid, _, path = key.partition("|")
        if not path.startswith("attr."):
            continue
        try:
            x = int(rid)
        except (TypeError, ValueError):
            continue
        # path = attr.<属性> / attr.<属性>_来源 / attr.<属性>_依据
        # 防御: 哨兵/None 一律写空, 绝不落进交付表(见 MISSING_KEY 说明)
        v = d.get("mode")
        if v is None or str(v).strip() == MISSING_KEY:
            v = ""
        # ★ `_依据` 是**自由文本**（逐次引的原文本来就不一样）→ **多数表决没有众数**
        #   → `mode` 投出空串，交付表「依据」列大面积空白（实测 30 行里 `厚度` 只有 7 行有值）。
        #   自由文本的正确口径是「取一条有代表性的」，不是「取一致的」。
        #   ⚠ 只对 `_依据` 这么办：`_来源` 也是 free，但它**必须保持单一来源**
        #     （取最长会变成 `标题+五点描述`，违反 §3.6）。
        if not str(v).strip() and path.endswith(_BASIS_SUFFIX):
            cands = [str((g or {}).get("value") or "").strip()
                     for g in (d.get("values") or [])]
            cands = [c for c in cands if c and c != MISSING_KEY]
            if cands:
                v = max(cands, key=len)          # 最长的一条 = 信息量最大
        by_row[x][path[len("attr."):]] = v
    return [{"xl_row": x, "attributes": by_row.get(x, {})} for x in row_ids]


# ================================================================ 重跑指令
def build_retry_brief(
    tag: str,
    gate: GateResult,
    *,
    output_file: str,
    input_file: str = "",
    max_items: int = 100,
) -> str:
    """把一个被拒运行的违规点，转成**指名到行**的重跑指令（markdown）。

    比"让模型重读整篇提示词"省得多：只列违规点，并要求其余字段**不要重做**，
    既能修掉问题，又不会引入新的分歧。
    """
    errs = gate.err_issues
    lines = [
        "# 重跑指令（结构闸门未通过）：%s" % tag,
        "",
        "你的上一次运行**未通过结构闸门**：%d 条 ERR、%d 条 WARN。"
        % (gate.errors, gate.warnings),
        "",
        "**只修下列问题，其余字段保持你原来的判断**"
        "（不要因为重跑就顺手改其它值——那会引入新的不一致）。",
        "",
        "## 必须修正的条目",
        "",
        "| 行号 | 字段 | 违规说明 |",
        "|---|---|---|",
    ]
    for i in errs[:max_items]:
        lines.append("| %s | `%s` | %s |"
                     % (i.xl_row, i.field, i.why.replace("|", "\\|")))
    if len(errs) > max_items:
        lines.append("| … | 另有 %d 条 | 同类型 |" % (len(errs) - max_items))
    lines += ["", "## 要求", ""]
    if input_file:
        lines.append("1. 输入仍为 `%s`；" % input_file)
    lines.append("2. 输出写入 `%s`（覆盖原文件）；" % output_file)
    lines.append("3. 提交前逐条核对上表——**每一条都应消失**；")
    lines.append("4. 其余字段不要重做，保持与原运行一致。")
    lines.append("")
    return "\n".join(lines)
