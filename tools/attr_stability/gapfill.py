# -*- coding: utf-8 -*-
"""定向追问（gap-fill）：只对**未明确**的格换一种问法再问一次

为什么需要它（与"全量重跑"的区别）
================================================================================
  · **全量重跑**（同一份提示词、同一个任务）只能靠**随机性**撞出不同答案。
    实测 12 行 × 25 属性 × 5 份：并集比最好单次只多补 11 格（+0.8%）。
  · **定向追问**改变了任务本身：只问那几个填不出来的属性，
    并把口径从「未提及即未明确」放宽为「找出任何间接线索/候选表述，给出依据」。
    这才可能把"模型没抓住"和"证据确实不存在"区分开。

两条硬性纪律
================================================================================
  ① **追问产出的是候选值，不是结论** —— 必须带 `依据`，且**不得直接覆盖**原值；
     它进 `候选值` 列与待裁定队列，由归类层/人工定夺。
  ② **仍要过结构闸门** —— 追问调用同样受 `check_row` 约束（词表越界、空值写法等）。

成本
================================================================================
  全量重跑 4 遍：`ceil(行数/chunk) × 4` 次调用（159 行 → 14×4 = 56 次）
  定向追问     ：按**行**聚合（每行只问它自己的待补属性）→ `ceil(行数/chunk)` 次
                且 prompt 更短（只列待补属性，不列全部 25 个）
  实测 12 行样例：全量 4 遍 = 56 次 → 定向追问 = 1 次（12 行一批）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

try:                                              # 包内
    from .vision_json import unwrap_vision
except ImportError:                               # 直接当脚本/被拷走单文件用
    from vision_json import unwrap_vision         # type: ignore

__all__ = ["GapPlan", "find_gaps", "build_gap_fill_prompt", "build_block_gap_prompt",
           "build_vision_prompt", "merge_candidates", "parse_vision_answer",
           "load_attr_notes", "GAP_FILL_RULES", "GAP_FILL_CORE", "SOURCE_PRIORITY"]

# 证据来源优先级（**冲突时**按此顺序取值）
#
# 为什么要写进提示词：此前只在类目配置里有 `evidence_priority`，
# 但提示词从未引用它 —— 等于"声明了优先级却没执行"，模型只是被要求"写出依据"，
# 不同来源冲突时取哪个全凭模型自己决定。
SOURCE_PRIORITY = ['标题', '亮点', '五点描述', '详细参数', '图片OCR', '图片视觉', 'SKU']
_PRIORITY_BLOCK = """\
【证据来源优先级 —— 多个来源冲突时按此顺序取值】

    %s

含义：
  · 序号越靠前的来源**权威性越高**。同一属性在两个来源给出不同答案时，取靠前的那个
  · 请在 `依据` 里写明取自哪个来源（如"依据：标题"），便于回查
  · **缺失不算冲突**：靠前的来源没提到，就用靠后的来源，这叫补充不叫冲突
  · 一旦发生冲突且你最终采用了低优先级来源，必须在 `依据` 里写明
    "与<高优先级来源>冲突，因<原因>采用<低优先级来源>"

**全类目统一，没有例外**（2026-10-08 用户口径）：任何属性都按上面这个顺序，
**不得**因为"详细参数是结构化字段"就把它提到五点描述前面。
此前的"材质牌号 / 商品尺寸与厚度 以详细参数为准"例外已删除 —— 它与固定顺序冲突，
实测导致**同样类型**的冲突有时"取五点"、有时"取参数"（模型自己记的 `冲突` 里两种并存）。
""" % (' > '.join(SOURCE_PRIORITY))

# 追问 prompt 的**短基座**：只留"怎么输出 + 通用纪律"，
# 不含主提示词里那 25 个属性的全部判据（那才是 81 KB / 45k token 的来源）
GAP_FILL_CORE = """\
你是电商商品属性标注员。本次只做「**定向追问**」：
针对指定的少量属性，从原始资料里找出线索。

""" + _PRIORITY_BLOCK + """
【通用纪律（与原标注一致，不因追问而放宽）】
  · 所有判断必须**有依据**；无依据宁可不填，也不要编造
  · 不得凭外观推断内部材质牌号 / 电池容量 / 电机功率 / 研磨档数 / 认证 / 可否水洗
  · 不得用常识补（"大多数研磨器都可调" 不是依据）
  · 依据要**可回查**：写出原文片段或图片位置
  · 数值必须带单位；单位换算用 1 inch = 2.54 cm、1 oz = 28.3495 g、1 lb = 453.592 g
  · 只输出 JSON，不要任何解释性文字
"""

# 追问口径（与全量提示词的差异集中在这里）
GAP_FILL_RULES = """\
【本次是「定向追问」，与常规标注的口径**不同**，请严格照本节执行】

常规标注要求「原文未提及 → 填未明确」。本次不是：
**请只针对下面列出的待补属性，主动去找任何与之相关的线索**，来源包括：
  1. 标题 / 五点描述 / SKU / 详细参数里的**间接表述**
     （例：要判 `led_light`，看到 "Blue LED illuminates the grinding area" → 是）
  2. 图片里的**可见结构或文字**（OCR 文字、外观、附件、按钮、接口、底座）
  3. **规格字段的其它项**：如 `Unit Count` / `Number of Items` / `Material` 列表里
     未被用到的项，往往藏着别的属性的答案
  4. 同款不同变体的表述（若有）

对每个待补属性，输出：
  · `候选值`：你找到的最贴切的表述（**可以是原文片段**，不必是受控值）
  · `依据`  ：原文片段或图片位置，**必须具体**（写不出依据就填"无依据"）
  · `置信度`：`强`（原文直接写明） / `中`（明确间接线索） / `弱`（仅推测）

【注意：`不适用` 与 `未明确` 是两件事，不要混】
  · `未明确` = **证据不足**，判不出来
  · `不适用` = **判得出来，但这个属性对本产品根本不存在**，
    例如：不锈钢板问「木材种类」、木板问「不锈钢等级」、无电气结构的板问「充电底座」
  · 判 `不适用` 时，`依据` 必须写出**为什么不存在**（如"主体材质为 316 不锈钢，
    全篇无任何木质部件表述"），`置信度` 通常为 `强` 或 `中`
  · 各属性的适用条件见下面的判据表（有 `适用条件` 一项时按它判）

【三态隔离的两个高频错误，务必避开】
  ① **结构类属性看不到该结构 → 要填 `否`，不是 `不适用`**
     手柄 / 汁槽 / 抽屉 / 刻度 / 支架 / 防滑 / 可折叠 … 对**每一件**产品都适用，
     "这块板没有手柄" 是 `否`；只有"木材种类"这种属性才可能 `不适用`。
  ② **属性恒真时 → 要填 `是`，不是 `不适用`**
     例：全篇无塑料件的木砧板问「不含BPA」→ `是`（无塑料则 BPA 无从存在），
     不是 `不适用`。

【是否型属性：不要只找正面表述，**主动扫否证**】
  `否` 不等于"没找到正面证据"，而是"找到了否定证据"。除了正面表述，还要扫：
    `No` / `None` / `Without` / `Not included` / `Manual`（非电动）/ `Free of`
    / `-free` / 无需 / 不含 / 不支持 / 不可 / 非
  找到否证 → `否`，并把否证原文写进 `依据`。
  正面、否证**都没有** → 才填 `未明确`。
  **不要因为"没正面写"就填 `未明确`** —— 这是空值的主要来源。

【材质类属性：默认**只给一个主材质**】
  · 主材质 = **按来源优先级扫描，第一个明确写出材质的来源里最先提到的那个**
  · **只有**该行「双面可用 = 是」且两面材质确实不同时，才写 `材质A+材质B`
    （**用 `+` 连接**，不要用"和 / 与 / 、"）
  · **上限两个**；三个及以上必是配件混入
  · **配件 / 辅料 / 涂层不算材质**：
      `竹木砧板配不锈钢收集盘` → `竹`；`木质砧板、不锈钢刀具` → `木材`；
      `不锈钢钛合金涂层` → `不锈钢`；`塑料、硅胶、橡胶`（硅胶/橡胶是防滑脚）→ `塑料`

【数值与尺寸的格式】
  · 一律 `数字+单位`、**中间无空格**：`1cm` ✅　`1 cm` ❌
  · 单位换算到**类目规则指定的目标单位**（见判据表里的 `单位`）
  · **尺寸**写成 `长x宽x高 单位`，缺的维度写 `缺`：`40x30x缺 cm`
  · **套装多尺寸逐块归一后用 `、` 连接并保留标签**：
      `大号:40x28x缺 cm、中号:34x23x缺 cm`
    **绝不要把多个尺寸合并成一个三元组**
    （反例：`25 x 15 cm + 29 x 20 cm` 被读成 `25x15x29 cm`——静默造假）
  · 数值解析不出来时按判据表的 `on_parse_fail` 处理（如 `加厚` → `未明确`）

**仍然禁止的事**（不因放宽口径而解除）：
  · 不得凭外观推断内部材质牌号 / 电池容量 / 电机功率 / 研磨档数 / 认证 / 可否水洗
  · 不得用常识补（"大多数研磨器都可调" 不是依据）
  · 写不出依据的，`置信度=弱` 且 `依据=无依据`，**不要编造依据**

确实找不到任何线索的属性，照样填 `未明确`，`依据=无依据`。
"""


@dataclass
class GapPlan:
    """待补计划：按行聚合的未明确属性。"""
    items: list[dict[str, Any]] = field(default_factory=list)
    """[{'xl_row':…, '待补属性':[属性名…], '说明':…}]"""
    n_gaps: int = 0
    n_rows: int = 0

    @property
    def max_per_row(self) -> int:
        return max((len(it['待补属性']) for it in self.items), default=0)

    def calls(self, chunk: int = 12) -> int:
        """按行聚合后需要多少次追问调用（每 chunk 行一次）。"""
        return (self.n_rows + chunk - 1) // chunk if self.n_rows else 0

    def summary(self) -> str:
        return ('待补 %d 格，分布在 %d 行（平均每行 %.1f 个属性）'
                % (self.n_gaps, self.n_rows,
                   self.n_gaps / max(self.n_rows, 1)))


def find_gaps(
    cells: Mapping[str, Mapping[str, Any]],
    attrs: Optional[Sequence[str]] = None,
    *,
    include_low_yield: bool = True,
) -> GapPlan:
    """从（并集）结果里找出**全部运行都判未明确**的 (行, 属性)。

    `cells` 用 `union_cells()` 的产物；只用 `标准` 列。
    `include_low_yield=False` 时跳过「有真值率极低」的属性（如被规则判为不可证的），
    那类属性追问通常也补不出来 —— 但仍建议至少追问一次，
    以便把「证据确实不存在」和「模型没抓住」区分开（前者可标记为已尽力）。
    """
    # ★ 2026-10-14 修: 原来 `不适用` 也算"缺值" → 文本侧**明确判定"本属性不适用"的格**
    #   会被再问一次图片, 而视觉答案直接**覆盖**原值。冷启动真跑实测: 637 个视觉填充格里
    #   **55 格**属于"两遍都判不适用"却被覆盖(例: `吸力` 两遍"不适用" → 被写成 `246MPH` / `62m/s`)。
    #   `不适用` 是**结论**(不是缺值), 与 `未明确` 语义相反, 必须排除在追问范围外。
    EMPTY = {'未明确', '未知', '待确认', '', 'None'}

    def real(v):
        if isinstance(v, list):
            return len(v) > 0
        return v is not None and str(v).strip() not in EMPTY

    per_row: dict[Any, list[str]] = {}
    n_gaps = 0
    for key, d in cells.items():
        if d.get("kind") != "value":
            continue
        rid, _, path = key.partition("|")
        if not path.startswith("attr."):
            continue
        attr = path[len("attr."):]
        if attrs and attr not in attrs:
            continue
        if any(real(g["value"]) for g in d["values"]):
            continue                      # 至少一份给出了值 → 不是"全未明确"
        per_row.setdefault(rid, []).append(attr)
        n_gaps += 1
    plan = GapPlan(n_gaps=n_gaps, n_rows=len(per_row))
    for rid in sorted(per_row, key=lambda x: (int(x) if str(x).isdigit() else 0)):
        plan.items.append({
            'xl_row': int(rid) if str(rid).isdigit() else rid,
            '待补属性': sorted(per_row[rid]),
        })
    return plan


def load_attr_notes(cat_dir: str, cat: Optional[str] = None) -> dict[str, dict[str, str]]:
    """从类目配置里取每个属性的短判据（`include`/`exclude`/受控值）。

    这是定向追问能"短"的关键：主提示词把 25 个属性的判据全写了一遍（45k token），
    而追问只需要**待补那几个**属性的判据 —— 通常 6 个左右，约 1/4 的体量。
    """
    try:
        from .catrules import load_category       # 包内
    except ImportError:                           # 直接当脚本/被拷走单文件用
        from catrules import load_category        # type: ignore
    cf = load_category(cat_dir, cat)
    out: dict[str, dict[str, str]] = {}
    for a in cf.attributes:
        if not isinstance(a, Mapping) or not a.get("name"):
            continue
        d: dict[str, str] = {}
        for k in ("include", "exclude", "type", "shape", "unit", "part"):
            v = a.get(k)
            if v:
                d[k] = str(v)
        vals = a.get("values")
        if isinstance(vals, Sequence) and vals:
            d["values"] = '、'.join(str(x) for x in vals[:12])
        out[str(a["name"])] = d
    return out


def _notes_block(notes: Mapping[str, Mapping[str, str]],
                 gap_attrs: Sequence[str]) -> list[str]:
    lines = ['## 这些属性的判据（**只**判定这些）', '']
    for a in gap_attrs:
        d = notes.get(a, {}) or {}
        desc = []
        if d.get("applicability"):
            desc.append('**适用条件**：%s' % d["applicability"])
        if d.get("include"):
            desc.append('应包含：%s' % d["include"])
        if d.get("exclude"):
            desc.append('**应排除**：%s' % d["exclude"])
        if d.get("values"):
            desc.append('受控值：%s' % d["values"])
        if d.get("unit"):
            desc.append('单位：%s' % d["unit"])
        lines.append('- `%s`%s' % (a, ('  ' + '；'.join(desc)) if desc else ''))
    lines.append('')
    return lines


def build_gap_fill_prompt(
    row: Mapping[str, Any],
    gap_attrs: Sequence[str],
    *,
    notes: Optional[Mapping[str, Mapping[str, str]]] = None,
    base_prompt: str = GAP_FILL_CORE,
    evidence_cols: Sequence[str] = ('标题', '亮点', '五点描述', 'SKU', '详细参数',
                                    '商品尺寸', '商品重量_换算'),
    image_urls: Optional[Sequence[str]] = None,
    original_values: Optional[Mapping[str, Any]] = None,
    max_chars: int = 2600,
) -> str:
    """拼一次「定向追问」的提示词。

    `base_prompt` 默认用短基座（`GAP_FILL_CORE`），**不要**塞主提示词全文 ——
    主提示词 81 KB，每次调用都送是步骤17 全量重跑烧掉 315 万 token 的根因。
    """
    ev = []
    for c in evidence_cols:
        v = row.get(c)
        if v:
            s = str(v)
            if len(s) > max_chars:
                s = s[:max_chars] + '…'
            ev.append('### %s\n%s' % (c, s))
    out = [
        base_prompt.rstrip(),
        '',
        '=' * 60,
        GAP_FILL_RULES.strip(),
        '=' * 60,
        '',
    ]
    out += _notes_block(notes or {}, gap_attrs)
    if image_urls:
        out += ['## 图片（随附，最多 4 张）',
                '可参考图片中**可见**的结构、按钮、接口、底座、附件与印字。', '']
    if original_values:
        keep = [(k, v) for k, v in original_values.items() if k in gap_attrs]
        if keep:
            out += ['## 上一次标注对这些属性的结论', '']
            for k, v in keep:
                out += ['- `%s`：%s' % (k, v)]
            out += ['（本次请判断：是"确实没有线索"，还是"上次漏看了"）', '']
    out += ['## 本行原始资料', ''] + ev
    out += [
        '',
        '## 输出格式（严格 JSON，不要输出其它文字）',
        '```json',
        json.dumps({'xl_row': row.get('xl_row'),
                    '追问': {a: {'候选值': '', '依据': '', '置信度': ''}
                             for a in gap_attrs}}, ensure_ascii=False, indent=2),
        '```',
    ]
    return '\n'.join(out)


def build_block_gap_prompt(
    plan_items: Sequence[Mapping[str, Any]],
    rows: Mapping[Any, Mapping[str, Any]],
    *,
    notes: Optional[Mapping[str, Mapping[str, str]]] = None,
    base_prompt: str = GAP_FILL_CORE,
    evidence_cols: Sequence[str] = ('标题', '亮点', '五点描述', 'SKU', '详细参数',
                                    '商品尺寸', '商品重量_换算'),
    max_chars: int = 2600,
) -> str:
    """**块级**追问提示词：`plan_items` 是 `find_gaps()` 里一个 chunk 的条目。

    与逐行拼的差别（也是成本的关键）
    ------------------------------------------------------------------
      · 基座（`GAP_FILL_CORE`）与**判据表**只写一次 —— 判据取本块全部待补属性的并集
      · 每行只追加自己的资料 + 自己的待补属性清单
      · 输出是一行一个对象（保持块级调用的形状，便于并回原表）

    这样每次调用的**固定开销**从"整篇提示词+规则 45.8 KB"降到"短基座+本块判据 ≈ 3.5 KB"，
    而块内证据（约 k × 2.5 KB）不可避免要重送 —— 所以省的是固定开销，不是证据。
    """
    all_attrs: list[str] = []
    for it in plan_items:
        for x in it['待补属性']:
            if x not in all_attrs:
                all_attrs.append(x)
    out = [base_prompt.rstrip(), '',
           '=' * 60, GAP_FILL_RULES.strip(), '=' * 60, '']
    out += _notes_block(notes or {}, all_attrs)
    out += ['## 本块有 %d 行需要补值，逐行给结论' % len(plan_items),
            '每行**只**回答它自己列出的待补属性，不要替别的行/别的属性作答。', '']
    for it in plan_items:
        rid = it['xl_row']
        r = rows.get(rid) or rows.get(str(rid)) or {}
        out.append('### 行 %s —— 待补：%s' % (rid, '、'.join('`%s`' % x for x in it['待补属性'])))
        for c in evidence_cols:
            v = r.get(c)
            if v:
                s = str(v)
                if len(s) > max_chars:
                    s = s[:max_chars] + '…'
                out.append('**%s**：%s' % (c, s))
        img = r.get('图片链接')
        if img:
            out.append('**图片**：随附（最多 4 张）')
        out.append('')
    shape = {str(it['xl_row']): {a: {'候选值': '', '依据': '', '置信度': ''}
                                 for a in it['待补属性']} for it in plan_items}
    out += ['## 输出格式（严格 JSON；键=行号，只含该行的待补属性）', '```json',
            json.dumps({'追问': shape}, ensure_ascii=False, indent=2), '```']
    return '\n'.join(out)


VISION_EXTRA = """\
【本次有**图片**：请优先依据图中可见的结构判断】

与纯文本追问的区别：
  · 你拿到了该行商品的图片，**图中看得见的结构就是证据**
  · 依据必须写成可回查的描述，例如
      「图片中可见：板体顶部有一个椭圆形孔洞（悬挂孔），无外延握持结构」
  · 看不到、看不清的，照样填 `未明确` —— **不要猜**

⚠ 即使图上有文字，以下仍然**不得**只凭外观下结论（以文本证据为准）：
  · 材质牌号（304 / 316 / 18-8）、木材具体树种
  · 厚度数值（图中没有比例尺）
  · 食品安全认证（LFGB / FDA / FSC …）、是否含 BPA
  · 抗菌防霉性、可否洗碗机清洗、耐温上限

下面同时给出该行的**文本资料**，可与图片互相印证；
文本与图片冲突时，**以能写出具体依据的一方为准**，并在 `依据` 里写明冲突。
"""


def _values_block(values_by_attr: Mapping[str, Sequence[str]] | None,
                  gap_attrs: Sequence[str]) -> list[str]:
    """把每个待补属性的**受控值词表**注入提示词。

    ★ 2026-10-07 新增（改动清单 §15 根因 1）：原先视觉提示词**不带受控值**，
    于是模型对「是否X」类属性自然答 `是`/`否`，而词表要的是 `带X`/`不带X`、
    `含X`/`不含X`、`无支架` —— 实测**331 格（占视觉补充 57%）是 是/否 形态**，
    与词表口径不一致 → 语义归类时只能靠模型二次猜（词表缺"否定项"时直接失败，
    如 `是否含负离子` 词表只有 `['含负离子']` → 58 格全掉）。

    **同一个病根**也出现在文本标注提示词（`_build_attr_hint_block` 只给 hint_short，
    不给 `params.values`）与 step18 归类载荷（§13）—— 即"受控值词表没有贯穿到
    每个『问模型』的环节"。
    """
    if not values_by_attr:
        return []
    lines = ['## 每个属性的**受控值**（取值必须逐字符来自这里）', '',
             '**硬性要求**：',
             '· 该属性**有受控值**时，`追问.<属性>` 必须**逐字符**取自下面列出的选项；',
             '  **不要**换成同义说法（受控值写 `不带显示屏` 就**不要**答 `否`；',
             '  写 `无支架` 就**不要**答 `否`）。',
             '· 所有选项都不合适时才输出自然语言短语，并让 `来源` 写清依据。',
             '· 判不出填 `未明确`，**不要猜**。', '']
    for a in gap_attrs:
        vals = [str(x) for x in (values_by_attr.get(a) or []) if str(x).strip()]
        if vals:
            lines.append('- `%s`：%s' % (a, ' ｜ '.join(vals)))
        else:
            lines.append('- `%s`：（本属性暂无受控值，按原文表述，尽量简短）' % a)
    lines.append('')
    return lines


def build_vision_prompt(
    row: Mapping[str, Any],
    gap_attrs: Sequence[str],
    *,
    notes: Optional[Mapping[str, Mapping[str, str]]] = None,
    values_by_attr: Optional[Mapping[str, Sequence[str]]] = None,
    base_prompt: str = GAP_FILL_CORE,
    n_images: int = 0,
    evidence_cols: Sequence[str] = ('标题', '亮点', '五点描述', 'SKU', '详细参数',
                                    '商品尺寸', '商品重量（单位换算）'),
    max_chars: int = 1500,
) -> str:
    """**视觉**版追问提示词：一行一调，随附该行图片（URL 直传）。

    为什么一行一调：图片是**按行**的，混多行会让"这张图属于哪一行"变得含糊。
    图片张数与 detail(low/high) 由调用方决定：含 OCR/文字类属性时用 high 读小字，
    其余 low 省 token；`n_images` 只影响提示词里"共 N 张"的说明文字。

    `values_by_attr`：`{属性: 受控值列表}` —— **必须传**，否则模型只能自由发挥
    （实测口径不一致导致 331 格是/否形态无法归类，见 `_values_block`）。
    """
    out = [base_prompt.rstrip(), '',
           '=' * 60, GAP_FILL_RULES.strip(), '=' * 60, '',
           VISION_EXTRA.strip(), '']
    out += _notes_block(notes or {}, gap_attrs)
    out += _values_block(values_by_attr, gap_attrs)
    if n_images:
        out += ['## 随附图片', '',
                '共 %d 张，均为**本行商品**的图片（主图在前）。' % n_images, '']
    ev = []
    for c in evidence_cols:
        v = row.get(c)
        if v:
            s = str(v)
            if len(s) > max_chars:
                s = s[:max_chars] + '…'
            ev.append('- **%s**：%s' % (c, s))
    if ev:
        out += ['## 本行文本资料（与图片互证）', ''] + ev + ['']
    out += [
        '## 输出格式（严格 JSON，不要输出其它文字）',
        '```json',
        json.dumps({'xl_row': row.get('xl_row'),
                    '追问': {a: '' for a in gap_attrs},
                    '来源': {a: '' for a in gap_attrs}}, ensure_ascii=False, indent=2),
        '```',
        '',
        '每个待补属性输出两样：`追问.<属性>` = 补出的值(判不出填 未明确)、',
        '`来源.<属性>` = 依据来源(标题/亮点/五点描述/详细参数/图片OCR/图片视觉)。',
    ]
    return '\n'.join(out)


def merge_candidates(
    cells: Mapping[str, Mapping[str, Any]],
    gap_results: Mapping[Any, Any],
) -> dict[str, Any]:
    """把视觉追问结果并入候选池（**不覆盖**已有结论）。

    新 schema(2026-09-24): 视觉补充输出「追问(值) + 来源」两个平铺 dict：
      · 逐行: `[{xl_row:…, 追问:{属性: 值}, 来源:{属性: 来源}}, …]`
      · 或 `{行号: {追问:{…}, 来源:{…}}}`
    返回 `{'并入': n, '跳过已有值': m, '按属性': {属性: [{行号, 值, 来源}]}}` —— 值进交付, 来源进来源列。

    ★ 2026-10-14 修: 签名里的 `cells` 原来**从未被使用** —— "不覆盖已有结论"完全依赖调用方
    过滤, 一旦调用方漏过滤就把文本侧已给出的结论覆盖掉(实测: `不适用` 被视觉写成 `246MPH`)。
    现在这里**自己回查 `cells`**: 该格已有真值(含 `不适用` 这类明确结论) → 丢弃视觉答案。
    """
    # ---- 归一成 [(行号, 追问dict, 来源dict), …]
    flat: list[tuple[Any, Mapping[str, Any], Mapping[str, Any]]] = []

    _ASK_EMPTY = {'未明确', '未知', '待确认', '', 'None'}

    def _already_real(rid: Any, attr: str) -> bool:
        """该 (行, 属性) 在文本侧是否已有真值 → 有则视觉不得覆盖(含 `不适用`)。"""
        d = cells.get("%s|attr.%s" % (rid, attr))
        if not isinstance(d, Mapping):
            return False
        for g in (d.get("values") or []):
            v = g.get("value") if isinstance(g, Mapping) else g
            if isinstance(v, list):
                if v:
                    return True
            elif v is not None and str(v).strip() and str(v).strip() not in _ASK_EMPTY:
                return True
        return False

    def _walk(node, cur_rid=None):
        if isinstance(node, (list, tuple)):
            for el in node:
                _walk(el, cur_rid)
            return
        if not isinstance(node, Mapping):
            return
        if 'xl_row' in node and node.get('xl_row') is not None:
            cur_rid = node.get('xl_row')
        ask = node.get('追问')
        src = node.get('来源') or {}
        if isinstance(ask, Mapping):
            if any(isinstance(v, Mapping) for v in ask.values()):
                # {"追问": {行号: {属性: 值}}} 块级 → 拆开
                for rid, attrs in ask.items():
                    _walk({'xl_row': rid, '追问': attrs, '来源': src}, rid)
            else:
                flat.append((cur_rid, ask, src if isinstance(src, Mapping) else {}))
        for k, v in node.items():
            if k in ('xl_row', '追问', '来源'):
                continue
            if isinstance(v, Mapping):
                _walk(v, k)

    _walk(gap_results)

    merged: dict[str, list[dict[str, Any]]] = {}
    n = 0
    skipped = 0
    for rid, ask, src in flat:
        for attr, val in ask.items():
            v = str(val or '').strip()
            if not v or v in ('未明确', '不适用', '无', '未知', '待确认'):
                continue
            if _already_real(rid, attr):            # ★ 不覆盖文本侧已有结论
                skipped += 1
                continue
            s = str(src.get(attr) or '').strip()
            merged.setdefault(str(attr), []).append({'行号': rid, '值': v, '来源': s})
            n += 1
    return {'并入': n, '跳过已有值': skipped, '按属性': merged}


def parse_vision_answer(
    rec: Any,
    want_attrs: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把视觉返回解析成 `({属性: 值}, {属性: 来源})`，**只保留 `want_attrs` 里的属性**。

    为什么需要它（2026-10-08，改动清单 §20.6）
    ================================================================================
    `build_vision_prompt` 要的形状是::

        {"xl_row": 2, "追问": {属性: 值}, "来源": {属性: 来源}}

    但模型有时**不直接回这个形状**，而是回一层包装（该失败模式在
    `vision_json.unwrap_vision` 的文件头已记录）::

        {"type": "json_object", "response": {…规范形状…}}
        {"type": "json_object", "content": "<json 字符串>"}
        {"type": "json_object"}                       ← 内容已丢

    只写 `rec.get("追问", rec)` 时，一旦拿不到 `追问` 就**把整个信封当成属性表** ——
    实测 Dog Dryer 在 `temperature=1.0` 下三次跑各出现 3/1/1 格
    `type = "json_object"`（这些格在 Dog Dryer 规格书里根本没有对应属性，
    且**该行真正的视觉答案被丢掉**）。因为交付表的列名取自规格书，这些表外键
    写盘时被静默丢弃，所以症状只是"白跑一次 + 那几格没补上"，很难发现。

    两道防线
    --------------------------------------------------------------------------------
      ① `unwrap_vision` 逐层剥壳 → **救回被包住的答案**（含 `content` 是字符串的情形）；
      ② 按 `want_attrs` 白名单过滤 → **保证表外属性名永远进不来**（模型编造的属性名同理）。

    返回 `({}, {})` 表示这一行没有可用答案，调用方应照"未补出"处理，
    **不要**把信封键当数据写进候选池。

    `want_attrs` 给空列表时直接返回空（没有要读的属性，就没有可接受的值）。
    """
    if isinstance(rec, (list, tuple)):
        # 模型偶尔回 `[{...}]` 列表（而非单个对象）：逐个元素试，取第一个能解析出待补属性的。
        for el in rec:
            if isinstance(el, Mapping):
                v, s = parse_vision_answer(el, want_attrs)
                if v:
                    return v, s
        return {}, {}
    if not isinstance(rec, Mapping):
        return {}, {}
    want = [str(a).strip() for a in (want_attrs or []) if str(a).strip()]
    if not want:
        return {}, {}
    allow = set(want)

    # ① 值层 = 含任一待补属性的那一层（剥壳会一路穿透 response/content/追问）
    body = unwrap_vision(rec, tuple(want))
    # 来源层 = 含「追问 / 来源」的那一层（值层常常已经在「追问」里面了，来源不在）
    meta = unwrap_vision(rec, ("追问", "来源"))

    vals: dict[str, Any] = {}
    if isinstance(body, Mapping):
        for k, v in body.items():
            name = str(k).strip()
            if name in allow:                     # ② 白名单
                vals[name] = v

    srcs: dict[str, Any] = {}
    src = meta.get("来源") if isinstance(meta, Mapping) else None
    if isinstance(src, Mapping):
        for k, v in src.items():
            name = str(k).strip()
            if name in allow:
                srcs[name] = v

    return vals, srcs


def main() -> None:
    """离线测算：待补格数、按行聚合后的调用次数、与全量重跑的对比。"""
    import argparse
    import io as _io
    import os
    import sys as _sys
    if hasattr(_sys.stdout, 'buffer'):
        _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8',
                                        errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('--prefix', default='r3')
    ap.add_argument('--rows', type=int, default=159)
    ap.add_argument('--chunk', type=int, default=12)
    ap.add_argument('--runs', type=int, default=5)
    ap.add_argument('--cat-dir', default='categories')
    ap.add_argument('--cat', default='salt_pepper_mill')
    a = ap.parse_args()
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from attr_stability import (RuleSource, load_run_files, load_rows, union_cells)

    rules = RuleSource.load(a.cat_dir, a.cat)
    runs = load_run_files('results/llm_only', a.prefix)
    cells = union_cells(runs, sorted(runs))
    plan = find_gaps(cells)
    print('=' * 90)
    print('定向追问（gap-fill）测算 —— 基于 %s 的并集结果' % a.prefix)
    print('=' * 90)
    print('   样例规模：12 行 × %d 属性' % len(rules.attrs))
    print('   %s' % plan.summary())
    print()
    print('   待补格最多的行（前 5）：')
    for it in sorted(plan.items, key=lambda x: -len(x['待补属性']))[:5]:
        print('      行%-5s %2d 个待补：%s'
              % (it['xl_row'], len(it['待补属性']), '、'.join(it['待补属性'])))
    print()
    # 逐属性统计待补次数
    import collections
    c = collections.Counter()
    for it in plan.items:
        for x in it['待补属性']:
            c[x] += 1
    print('   按属性看「多少行需要补」（%d 行）：' % len(plan.items))
    for x, n in c.most_common():
        mark = '  ← 全部行都补不出' if n == len(plan.items) else ''
        print('      %-24s %2d 行%s' % (x, n, mark))
    print()
    # 成本对比
    n_rows = a.rows
    sample_rows = 12
    scale = n_rows / sample_rows
    full_calls = (n_rows + a.chunk - 1) // a.chunk * a.runs
    gap_rows = max(1, int(round(plan.n_rows * scale)))
    gap_calls = (gap_rows + a.chunk - 1) // a.chunk
    print('   成本对比（%d 行、chunk %d、%d 份）' % (n_rows, a.chunk, a.runs))
    print('      ① 全量重跑 %d 遍          ：%d 次调用' % (a.runs, full_calls))
    print('      ② 全量 1 遍 + 定向追问     ：%d + %d = **%d 次**'
          % ((n_rows + a.chunk - 1) // a.chunk, gap_calls,
             (n_rows + a.chunk - 1) // a.chunk + gap_calls))
    print('         → 比 ① 省 **%.0f%%**'
          % (100.0 * (full_calls - ((n_rows + a.chunk - 1) // a.chunk + gap_calls))
             / full_calls))
    print('      ③ 且追问 prompt 更短：只列待补属性，不列全部 %d 个' % len(rules.attrs))

    # ---- prompt 体量实测（这才是 token 成本的真因）----
    import os as _os
    rows = load_rows('results/llm_only/input.json')
    by_row = {str(it['xl_row']): it['待补属性'] for it in plan.items}
    notes = load_attr_notes(a.cat_dir, a.cat)
    sizes = []
    for r in rows:
        g = by_row.get(str(r.get('xl_row')))
        if not g:
            continue
        p = build_gap_fill_prompt(r, g, notes=notes,
                                 image_urls=[r.get('图片链接')] if r.get('图片链接') else None,
                                 original_values=r.get('原始属性值'))
        sizes.append(len(p.encode('utf-8')))
    main_p = _os.path.join('prompts', '23_llm_only_pipeline.md')
    form_p = _os.path.join('prompts', '22_form_layer_executor.md')
    rules_md = _os.path.join(a.cat_dir, '%s.rules.md' % a.cat)
    parts = [(p, _os.path.getsize(p)) for p in (main_p, form_p, rules_md)
             if _os.path.exists(p)]
    main_sz = sum(sz for _, sz in parts)
    avg = sum(sizes) / max(len(sizes), 1)
    print()
    print('   prompt 体量实测 —— 这是 token 成本的真正大头：')
    for p, sz in parts:
        print('      %-38s %7.1f KB' % (p, sz / 1024))
    print('      %-38s %7.1f KB  ← 每次调用都重送'
          % ('【合计】全量调用的载荷', main_sz / 1024))
    print('      %-38s %7.1f KB  ← 含本行资料' % ('【定向追问】载荷（平均）', avg / 1024))
    print('      → 单次缩小 **%.1f 倍**：%d token → %d token'
          % (main_sz / max(avg, 1), main_sz / 2.6, avg / 2.6))
    n_rows_all = a.rows
    full_tok = main_sz / 2.6 * full_calls
    gap_tok = (main_sz / 2.6) * ((n_rows_all + a.chunk - 1) // a.chunk) \
        + (avg / 2.6) * (n_rows_all + a.chunk - 1) // a.chunk
    print('      → %d 行总计：全量 %d 遍 ≈ %.0f 万 token → 1 遍+追问 ≈ %.0f 万 token（省 %.0f%%）'
          % (n_rows_all, a.runs, full_tok / 10000, gap_tok / 10000,
             100.0 * (full_tok - gap_tok) / full_tok))


if __name__ == '__main__':
    main()
