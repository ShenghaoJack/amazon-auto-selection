"""步骤 17：属性标注 —— N 次补充 + 闸门 + 表决 + 图片视觉补充。

只做标注: 产出 单遍原始 + 唯一值统计 + 稳定性结果.json; **归并标准化在步骤18**。

断点续跑（步骤内部）: CallableProducer 每跑完一批就把已产出行落盘到
`runs/.<run_tag>.checkpoint.json`(输入指纹校验), 中断后重启只补跑剩余行;
整份运行跑完则直接复用 `runs/<run_tag>.json`, 不再重复调 LLM。

上游
================================================================================
  · 步骤16 已确认的属性集规格 `<cat>-attribute-spec.json`(status=confirmed);
  · 步骤14 流量拆分输入 `14_流量拆分/流量拆分数据-需人工补充.xlsx`(sheet AI已补充)。

配置项（建议加进 CONFIG_GUIDE.md）
================================================================================
  标注固定两遍，取值冲突的单元格单独补判一次。
  ATTR_STAB_MAX_ALONE 默认 10.0 独异格率阈值(%)，超过的运行不进交付；<=0 不过滤
  ATTR_STAB_CHUNK     默认 12   单次送几行
  ATTR_STAB_MAX_RETRY 默认 2    完整性错误/产出失败后最多重跑几轮
  ATTR_STAB_MIN_RATE  默认 85.0 交付值一致率**提示**门槛
                      ⚠ **只提示、不阻断**（2026-10-12 核实并更正：此处旧措辞称"低于就会拦下本步"，
                        与代码不符 —— `run()` 里那条分支只 `self.log` 一条警告就过了；
                        Web 的提示词当时也跟着错，害人以为它是个硬闸门。
                        设 0 = **连提示都不打**；真正会让本步失败的是"无交付内容 / LLM 全失败"那两条）。
  ATTR_STAB_PROMPT    默认 prompts/step17_general_core.md（相对 cat_root 或项目根）
"""
from __future__ import annotations

import json
import os
import re
import sys

from core.errors import StepDataError, StepFailedError, WaitingForHuman
from steps.base import BaseStep, StepContext

# 让 `from tools.attr_stability import ...` 可用（项目根在 sys.path 时亦可省）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.attr_stability import (          # noqa: E402
    CallableProducer, RuleSource, load_rows, run_round,
)
# 注：`MISSING_KEY` / `RESERVED_VALUES` 原先从这里导入但**从未使用**（pyflakes F401），
#     已删除导入。若后续要用，从 `tools.attr_stability` 取。
from tools.attr_stability.spec import (     # noqa: E402
    cat_rules_stem, create_rules_skeleton, resolve_rules_file,
)
from tools.attr_stability.spec_io import (  # noqa: E402
    assert_rule_alignment, load_attr_spec, make_annotation_contract, own_spec_path,
    spec_revision, verify_annotation_contract, write_json_atomic,
)

# 上游依赖(2026-09-21 Phase 2 重构: 16b → 步骤17 属性标注与标准化):
#   ① 步骤16 已确认的属性集规格 `{cat}-attribute-spec.json`(人工确认后 status=confirmed);
#   ② 步骤14 流量拆分输入 `14_流量拆分/流量拆分数据-需人工补充.xlsx`(sheet AI已补充)。
UPSTREAM_SPLIT = "14_流量拆分/流量拆分数据-需人工补充.xlsx"
UPSTREAM_SPLIT_SHEET = "AI已补充"
SPEC_TPL = "{cat}-attribute-spec.json"
SPEC_CONFIRMED = "confirmed"
# ---- 标准化词表的确认状态（2026-10-09，方案 A 的「停②」）------------------------------
# 链路：16 提取属性 →[停①] 确认属性集 → 17 标注 **+ 产词表** →[停②] 确认词表 → 18 应用词表
# 为什么把词表生成从步骤18 移到步骤17：原先步骤18 **自己**生成词表，
# 于是"步骤17 跑完"时**根本没有新词表可调** —— 用户要的"停下来调词表"落不了地。
# 状态存在 `rules.json` 顶层 `vocab_status`（与 spec 的 `status` 同构）。
VOCAB_STATUS_KEY = "vocab_status"
VOCAB_PENDING = "pending_confirm"
VOCAB_CONFIRMED = "confirmed"
OUT_DIR = "17_属性标注"
RESULT_NAME = "稳定性结果.json"
RAW_XLSX_TPL = "{cat}-属性标注-单遍原始.xlsx"
STD_XLSX_TPL = "{cat}-属性标注-标准化交付.xlsx"
UNIQUE_STATS_NAME = "唯一值统计.json"

# 尺寸三列（2026-10-08，用户要求）：
#   模型本来就产出 `尺寸_拆列.长/.宽/.高` + `尺寸_依据`，但交付表的列只由规格书属性驱动，
#   这些被归为"过程字段"→ 从没写进交付表。这里把它们**拆成三列**落到交付表，
#   位置紧跟在 `尺寸规格` 之后；单位统一 cm；多件/多尺寸用 `，` 连接、单位写在末尾。
SIZE_SPLIT_PREFIX = "尺寸_拆列"
SIZE_SPLIT_BASIS = "尺寸_依据"

# 键名硬约束（2026-10-23 用户口径: 只允许 spec 属性名 + `_依据`/`_来源`）。
# 背景: 实测 a2 被闸门拒的直接原因是 LLM **造键** ——
#   `是否无线_依据补充` / `转速_转速_依据` / `材质_来源_依据` / `风速_来源_依据`，
#   这些在闸门里是 "出现未定义属性" 的 required 错误 → 整份运行直接 REJECT。
KEY_CONTRACT = """## 完整性硬约束（漏一个属性 = 整份作废）

**「属性定义」里的每一个属性，在每一行里都必须出现** —— 一个都不能省。
包括：

- 答案为 **`否`** 的属性（例如商品没有滤网，也要写 `是否带滤网 = 否`，不能省略这个键）;
- 答案为 **`未明确`** 的属性（也要把键写出来，值为 `未明确`）。

**常见错误**: 模型认为"这个商品没有这个特征，就不用写了" —— 这是错的。
省略 = 闸门判「漏属性」→ 整份运行 REJECT。

## 输出键名硬约束（违反即整份作废）

每行 `attributes` 对象里**只允许**出现下面三类键, 其它一律禁止:

1. **上方「属性定义」里列出的每一个属性名本身** —— 必须原样照抄,
   不得加字/减字/改写, 不得写成 `是否无线_依据补充` 这种带后缀的形式;
2. `<属性名>_依据` —— 每个属性都必须给（原文摘录）;
3. `<属性名>_来源` —— 每个属性都必须给（来源标识）。

**明令禁止**:
- 任何「属性定义」里没有的属性名;
- 形如 `X_X_依据`（后缀重复）;
- `_依据补充` / `_dup` / `_extra` 之类的自造后缀;
- 把 `_来源` 再套一层 `_依据`（`X_来源_依据` 是错的，应为 `X_依据`）;
- 除下条「尺寸」特别说明外，**任何嵌套对象**。

## 尺寸属性怎么写（重要，写错=整份作废）

- **只写 `尺寸`**：`尺寸` + `尺寸_来源` + `尺寸_依据` 三件套，**一个都不能少**；
- **禁止写 `尺寸_拆列`**（2026-10-26 用户口径）——尺寸拆成 长/宽/高 是**步骤18 的事**，
  本步不要做，也不要输出 `尺寸_拆列.{长,宽,高}` 这个嵌套对象；
- 把原始尺寸字符串（如 `32x17.8x22.9cm`、`23.6 x 18.5 x 16.9 inches`）**原样写进 `尺寸` 的值**，
  单位不统一也没关系（步骤18 会统一换算并拆列）。

### 尺寸要写几个：先判断这一行是「变体 / 套件 / 单件」

**第 1 步：这一行是变体吗？**（同一个父体下有 S/M/L 多个 ASIN）

- 看 `SKU` 字段有没有 `Size:` / `尺寸:` / `Größe:` 这类**变体主题**；
- 或标题/五点里明确写 `Available in S/M/L` 且**本行 SKU 已指定其中一个**；
- → **是变体：只写当前变体的那 1 个尺寸**（来自 `SKU` 的 Size），
  **不要**把父体文案里的 "Small 12x8, Medium 15x10" 并列写进来（那些属于别的 ASIN）。

**第 2 步：不是变体，原文里有几组尺寸？**

- 出现 `Set of 3` / `Includes 3 sizes` / `3-piece set` / 「套装含大中小三块」
  → **套件：全部保留**，写成 `小 20x15cm / 中 25x18cm / 大 30x22cm`（用 ` / ` 分隔，
  每段带标签；**能识别到 小/中/大 就按 小→中→大 排序，识别不到就按原文顺序**）；
- 只有一组 `Product Dimensions` → **单件：写 1 个**；
- 只有 `Package Dimensions` → 写 1 个（依据里注明是包装尺寸）。

**第 3 步：只保留「商品本体」的尺寸**

- 配件（备用滤芯、装饰贴纸、螺丝、说明书）的尺寸**一律不要**；
- 组装后才有的总高（`assembled height`）可写，但要写进 `尺寸_依据` 说明。

### 尺寸_依据 必须能看出来源

```
✅ 尺寸_依据 = "SKU: Size: 17\"L x 12\"W x 0.5\"H"
✅ 尺寸_依据 = "Product Dimensions: 17 x 12 x 0.5 in"
✅ 尺寸_依据 = "Set includes 3 boards: Small 8x6, Medium 10x7, Large 12x9 in"
```

这样复核时能看出「取的是变体尺寸 / 商品尺寸 / 套装三件」。

## 多条证据：按优先级只采纳一条（重要）

一个属性**只有一个** `_依据` 和一个 `_来源`。如果同一个属性在**多个字段**都看到了，
按下面的**优先级**取**最高**的那一条，其余**不要**写成 `_依据2` / `_来源2`：

    标题  >  五点描述  >  详细参数  >  SKU

- 例: 标题和五点描述都提到"箱式" → 采纳**标题**那条，`_来源 = 标题`;
- 只有五点描述提到 → `_来源 = 五点描述`;
- 只有详细参数提到 → `_来源 = 详细参数`;
- 只有 SKU 提到（如 `Type: Box Dryer`）→ `_来源 = SKU`。

**禁止** `X_依据1` / `X_依据2` / `X_来源1` / `X_来源2` / `X_dup` 等任何拆分写法。
（图片来源 `图片OCR` / `图片视觉` 同属证据，优先级排在文本字段之后。）

## 值与来源的区别（写错会被判错）

- **值**（`<属性名>` 本身）：找不到数据 → 写 `未明确`。
- **来源**（`<属性名>_来源`）：**只填一个字段名**，必须是这 6 个词之一（原样，不得改写）：
  `标题` / `五点描述` / `详细参数` / `SKU` / `图片OCR` / `图片视觉`。
  按上面「多条证据」的优先级取**最高的那一个**，**不要**用 `+` 连接多个字段。
  **没有来源 → 留空（空串）**，**绝不能**写 `未明确`。

⚠ 常见错误：值为 `未明确` 时顺手把来源也写成 `未明确` —— 这是**类别错误**
（来源回答的是"从哪个字段读到的"，不是"有没有数据"）。空串才是合法写法。"""
GENERAL_FLAT_CONTRACT = """## 输出格式
严格返回 JSON：{"rows":[{"xl_row":行号,"category_check":{"判定":"属于本类目","信号":[]},"attributes":{"属性名":"值","属性名_来源":"来源","属性名_依据":"依据"},"冲突":[],"跨列纠错":[],"词表外词":[]}]}。
每行按已确认属性名称输出三键，不能省略或新增属性。各来源只写一个字段名；未明确写明证据不足。禁止输出尺寸拆列。"""
SIZE_SPLIT_DIMS = ("长", "宽", "高")      # 三列：长/宽/高（2026-10-20 用户口径，
                                          # 原为「厚」，现统一成「高」以覆盖所有尺寸类属性）
# 模型输出的维度名可能是 `高` / `厚`（`尺寸_拆列.高`、`.厚`）→ 统一映射成交付列名「高」。
# 早期交付列叫「厚」，历史数据里也有 `厚`，这里一并归到「高」。
SIZE_SPLIT_DIM_ALIAS = {"长": "长", "宽": "宽", "高": "高", "厚": "高"}
# 多值串里「这一块缺该维度」的占位符（只在多值串内部合法；单独一个要留空）
_SIZE_MISSING = "缺"
SIZE_SPEC_ATTR = "尺寸规格"                # 插在这一列之后
SIZE_SPLIT_UNIT = "cm"
# 冲突登记文件名
CROSS_CHECK_NAME = "跨属性冲突.json"


# ============================================================================
# 配置读取：**不要把 `or 默认值` 用在数值键上**
# ----------------------------------------------------------------------------
# 数值 0 可能是合法配置，例如 MAX_ALONE=0 表示不过滤。
# 正确做法：**只有 None / 空串才算没配**，0 是合法取值。
# ============================================================================
def _cfg_num(cfg, key, default, cast):
    v = cfg.get(key, None)
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return default
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


def _cfg_str(cfg, key, default=""):
    v = cfg.get(key, None)
    return default if v is None or str(v).strip() == "" else str(v)


# 标注所需的最小输入列(B3, 2026-10-14): 缺任一 → 硬失败。
# 为什么: 原来只校验"表头非空/不重复/不含 xl_row", 上游改名或漏列时 N 遍会**整列填『未明确』
# 却报成功**; 而 `build_prompt` 对缺字段一律填空串, 没有任何告警。
# 实测: 13 个类目的 `14_流量拆分` 表全部含这 5 列(78~80 列), 因此约束不会误伤。
REQUIRED_INPUT_FIELDS: tuple[str, ...] = ("标题", "五点描述", "详细参数", "图片链接", "SKU")
TEXT_ANNOTATION_FIELDS: tuple[str, ...] = ("标题", "五点描述", "详细参数", "SKU")


def compact_annotation_prompt(base: str, attr_block: str, cross_block: str) -> str:
    """仅对内置模板移除旧输出格式；自定义模板保留原文。"""
    marker = '## 4. 输出格式（**逐字符遵守，不得增删键**）'
    if marker in base and '## 5. 强制自检（提交前逐条走）' in base:
        base = base.split(marker, 1)[0].rstrip()
        start, end = '### 3.2 每个属性', '### 3.3e `依据`'
        if start in base and end in base:
            left, rest = base.split(start, 1)
            _, right = rest.split(end, 1)
            base = (left + '### 3.2 每个属性\n逐个判断全部已确认属性；未提及不等于否，'
                    '没有明确证据写未明确。来源只取一个字段，保留原文依据。\n\n'
                    + end + right)
    parts = [base, attr_block, cross_block]
    # 保留变体/套装尺寸和来源规则；输出键契约由生产者统一追加。
    if not base.startswith('# 步骤17通用属性标注核心规则'):
        parts.append('## 尺寸属性怎么写' + KEY_CONTRACT.split('## 尺寸属性怎么写', 1)[1])
    parts.append('本次仅提供标题、五点描述、详细参数和 SKU；没有图片或图片 OCR，'
                 '不得声称看过图片。图片在独立视觉补充阶段处理。')
    return '\n\n'.join(p.strip() for p in parts if p and p.strip())


def annotation_chunk_size(configured: int, attribute_count: int) -> int:
    """控制每批属性格数，配置值仍为上限，不影响覆盖和轮次。"""
    return max(1, min(max(1, configured), 720 // max(1, attribute_count) or 1))


def _save_workbook_atomic(wb, path: str) -> None:
    """原子保存工作簿: 同目录临时文件 → 回读校验 → `os.replace`。

    ★ 2026-10-14(B1): 交付表(`_write_deliverables`)一直是这么写的, 而 `单遍原始.xlsx`
    是直写 `wb.save(path)` —— 中断会留下半成品, 而 DONE 判定只看 `os.path.exists`
    (`steps/base.py`)。这里复用同一套模式, 不新造机制。
    """
    import tempfile

    import openpyxl
    target_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=target_dir)
    os.close(fd)
    try:
        wb.save(tmp)
        chk = openpyxl.load_workbook(tmp, read_only=True)      # 回读验证可打开
        chk.close()
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _deliverable_attr_names(deliverable) -> list[str]:
    """从 deliverable 里取**交付列名**(属性名), 过滤内部键 `_来源` / `_依据`。

    ★ 2026-10-14: 抽成模块级函数是为了可测 —— 原来这段内联在 `_write_raw` 里,
    只过滤 `_来源` 而漏了 `_依据`, 冷启动真跑把 33 个内部字段写成了交付列
    (176 行 × 145 列)。`_write_deliverables`(步骤18 交付表)用的是同一条判据。
    """
    names: list[str] = []
    for d in (deliverable or []):
        if not isinstance(d, dict):
            continue
        for a in (d.get("attributes") or {}):
            if not str(a).endswith("_来源") and not str(a).endswith("_依据") and a not in names:
                names.append(a)
    return names


class Step17AttrAnnotation(BaseStep):
    number = 17
    title = "属性标注"
    description = ("FR-17: 对已确认属性集做 N 次补充标注 + 结构闸门 + 一致性表决 + 图片视觉补充(仅未明确), "
                   "产出 单遍原始 + 唯一值统计; 归并标准化见步骤18")

    def __init__(self):
        self._cat = None
        self._spec_attributes: list[dict] = []

    @property
    def artifacts(self) -> list[str]:
        cat = self._cat or ""
        return [
            f"{OUT_DIR}/{RESULT_NAME}",
            f"{OUT_DIR}/{RAW_XLSX_TPL.format(cat=cat)}",
            f"{OUT_DIR}/{UNIQUE_STATS_NAME}",
        ]

    # 归档额外中间产物(供步骤18 重跑与审计还原): 输入快照 + 视觉补充 + 词表聚类历史。
    # runs/、retry_briefs/ 等大体积逐遍调试件不入归档。
    # ★ 词表聚类历史.json 写在 18_标准化/(步骤17 的 _generate_vocab 产出),
    #   这里登记是为了**步骤17 单独跑**时也能把它带进快照; 步骤18 也会再登记一次
    #   (它在 18_标准化/ 目录里, 与词表聚类报告.json 同口径)。
    extra_archive_files = [
        f"{OUT_DIR}/input.json",
        f"{OUT_DIR}/视觉补充.json",
        "18_标准化/词表聚类历史.json",
    ]

    # ------------------------------------------------------------ 上游校验
    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        # ① 步骤16 已确认的属性集规格
        spec_name = SPEC_TPL.format(cat=self._cat)
        sp = ctx.resolve_artifact(spec_name)
        if not os.path.exists(sp):
            raise StepDataError("缺少属性集规格 %s(步骤16 未完成?)" % spec_name)
        try:
            spec = load_attr_spec(sp)
        except Exception as exc:  # noqa: BLE001
            raise StepDataError("属性集规格解析失败 %s: %s" % (spec_name, exc))
        if str(spec.get("status") or "") != SPEC_CONFIRMED:
            # ★ 「停①」：这不是失败，是**等人工确认属性集**（2026-10-09 方案 A）。
            #   编排器捕获 WaitingForHuman → 不标 FAILED、不发告警、退出码 3。
            raise WaitingForHuman(
                "属性集规格未确认(当前状态: %s)" % (spec.get("status") or "?"),
                hint="在 Web 的「步骤16 属性集」面板确认属性集后，重跑本命令即从本步续跑")
        # 确认后的继承定义也只能在本市场副本上消矛盾、补版本。
        import uuid
        local_sp = own_spec_path(ctx.paths.cat_root)
        needs_write = os.path.abspath(sp) != str(local_sp.resolve()) or not spec.get("definition_revision")
        if needs_write:
            from tools.attr_stability.spec_io import spec_lock
            with spec_lock(ctx.paths.cat_root):
                if (spec_revision(load_attr_spec(sp)) != spec_revision(spec) or
                        (os.path.abspath(sp) != str(local_sp.resolve()) and local_sp.exists())):
                    raise StepDataError("属性集在准备标注期间发生变化，请重新运行步骤17")
                if os.path.abspath(sp) != str(local_sp.resolve()):
                    spec.setdefault("_inherited_from", os.path.basename(sp))
                spec["cat"] = self._cat
                if not spec.get("definition_revision"):
                    spec["definition_revision"] = uuid.uuid4().hex
                write_json_atomic(local_sp, spec)
            sp = str(local_sp)
        # ★★ 修复（审核中6，2026-10-10）：`strip_all_contradictions` **写 spec 文件**，
        #   原来放在**闸门之前** —— 未确认的 spec 在"仅被校验"时就被改写了（校验带副作用）。
        #   现在**先判停①（确认）**，确认之后才消矛盾、并**重读**让内存与文件一致。
        #   注意仍要在 `self._spec_attributes` 赋值**之前**（否则改文件不改内存，白跑）。
        try:
            from tools.attr_stability.spec_sync import strip_all_contradictions
            for _nm, _cl in strip_all_contradictions(sp, expected_revision=spec_revision(spec)):
                for _line in _cl:
                    ctx.log.info("已消除自相矛盾断言 [%s]: %s" % (_nm, _line[:70]))
        except Exception as exc:  # noqa: BLE001 — 消除失败不拦运行
            from tools.attr_stability.spec_sync import SpecConcurrentChange
            if isinstance(exc, SpecConcurrentChange):
                raise StepDataError(str(exc)) from exc
            ctx.log.info("警告: 消除矛盾断言失败(忽略): %s" % exc)
        # ★★ 修复（逻辑审核中3，2026-10-10）：**重读移出 try** —— 原来重读与 strip 同一个 try，
        #   若 strip 写成功而重读失败，`spec` 仍是 strip **之前**那份 → 文件新、内存旧，
        #   正是上面注释声明要防的「改文件不改内存」，而且只留一行 log（静默）。
        #   现在重读失败即 **raise**：宁可让这一步停下，也不要用旧判据跑一轮（白花钱）。
        try:
            spec = load_attr_spec(sp)                       # strip 改写了文件 → 必须重读
        except Exception as exc:  # noqa: BLE001
            raise StepDataError("消除矛盾断言后重读属性集失败 %s: %s"
                                "（拒绝用旧内存继续，避免「改文件不改内存」）" % (spec_name, exc))
        from tools.attr_stability.feature_groups import expand_attributes
        self._spec_attributes = [a for a in expand_attributes(spec.get("attributes") or [])
                                 if isinstance(a, dict) and str(a.get("name") or "").strip()]
        if not self._spec_attributes:
            raise StepDataError("属性集规格为空(无属性), 请先在 Web 补充并确认")
        self._validated_spec_revision = spec_revision(spec)
        # ② 步骤14 流量拆分输入
        if not os.path.exists(ctx.resolve_artifact(UPSTREAM_SPLIT)):
            raise StepDataError("缺少上游输入 %s(步骤14 未完成?)" % UPSTREAM_SPLIT)

    def _upstream_name(self) -> str:
        return UPSTREAM_SPLIT

    # ------------------------------------------------------------ 跳过条件
    def skip_when(self, ctx: StepContext) -> bool:
        """步骤17固定两遍，是否执行由流程步骤选择决定。"""
        return False

    # ------------------------------------------------------------ 产物质量
    def validate_artifacts(self, ctx: StepContext) -> None:
        """断点续跑前校验：产物(稳定性结果.json + 单遍原始 xlsx + 唯一值统计)存在。

        宽松模式(2026-09-24)：不再按交付值一致率门槛阻断续跑 —— 步骤17 只负责把
        属性值跑出来, 一致性/标准化是步骤18 的事。
        """
        missing = self.missing_artifacts(ctx)
        if missing:
            raise StepDataError("产物缺失: %s" % missing)
        p = ctx.resolve_artifact(f"{OUT_DIR}/{RESULT_NAME}")
        if not os.path.exists(p):
            raise StepDataError("缺少 %s" % p)
        self._verify_result_contract(ctx)

    def _verify_result_contract(self, ctx: StepContext) -> None:
        try:
            spec = load_attr_spec(ctx.resolve_artifact(SPEC_TPL.format(cat=ctx.config.cat)))
            if spec.get("status") != SPEC_CONFIRMED:
                raise ValueError("属性集未确认，请先确认并重新运行步骤17")
            with open(self._rules_json_path(ctx), encoding="utf-8-sig") as fh:
                rules = json.load(fh)
            with open(ctx.resolve_artifact(f"{OUT_DIR}/{RESULT_NAME}"), encoding="utf-8-sig") as fh:
                result = json.load(fh)
            rows = load_rows(ctx.resolve_artifact(f"{OUT_DIR}/input.json"))
            verify_annotation_contract(spec, rules, result, rows,
                                       source_path=ctx.resolve_artifact(UPSTREAM_SPLIT))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise StepDataError("步骤17交付版本校验失败: %s" % exc) from exc

    # ------------------------------------------------------------ 主流程
    def run(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        out_dir = ctx.resolve_artifact(OUT_DIR)
        os.makedirs(out_dir, exist_ok=True)
        spec_names = [str(a.get("name") or "").strip()
                      for a in (self._spec_attributes or []) if str(a.get("name") or "").strip()]
        if spec_names:
            self.log(ctx, "已确认属性集 %d 个: %s"
                     % (len(spec_names), ",".join(spec_names[:15])
                        + ("…" if len(spec_names) > 15 else "")))

        # ① 类目规则源：统一入口；共享定义写入前建立当前类目副本
        cat_dir, cat_id = self._ensure_rules(ctx)
        if cat_dir is None:
            raise StepDataError("找不到类目规则文件(*.rules.json), 请确认 data/<CAT>/ 下有 <snake>.rules.json")
        rules = RuleSource.load(cat_dir, cat_id)
        for w in rules.warnings:
            self.log(ctx, "规则源提示：%s" % w)
        bad = rules.lint()
        if bad:
            for b in bad:
                self.log(ctx, "⚠ 类目配置自检：%s" % b)

        # ② 输入行：核对上游摘要后复用本步快照，变化或旧快照无摘要时刷新
        rows = self._load_rows(ctx, out_dir)
        if not rows:
            raise StepDataError("没有可标注的输入行（见 %s/input.json）" % OUT_DIR)
        spec = load_attr_spec(ctx.resolve_artifact(SPEC_TPL.format(cat=self._cat)))
        if spec_revision(spec) != getattr(self, "_validated_spec_revision", None):
            raise StepDataError("属性集在步骤17校验后发生变化，请重新运行步骤17")
        contract = make_annotation_contract(spec, rows,
                                            source_path=ctx.resolve_artifact(UPSTREAM_SPLIT),
                                            input_mode=str(ctx.config.get("ATTR_STAB_INPUT_MODE", "auto")))
        if contract["input_mode"] == "auto":
            with open(os.path.join(out_dir, "input.json"), encoding="utf-8-sig") as fh:
                snapshot_source = json.load(fh).get("source") or {}
            if snapshot_source.get("sha256") != contract["source_sha256"]:
                raise StepDataError("上游在生成输入快照后发生变化，请重新运行步骤17")
        self.log(ctx, "输入 %d 行，类目 %s（属性 %d 个）"
                 % (len(rows), self._cat, len(rules.attrs)))

        if ctx.dry_run:
            self.log(ctx, "dry-run：跳过 LLM 调用，仅写出输入与规则摘要")
            self._write_dry_run(ctx, out_dir, rules, rows)
            return

        # ③ 提示词 + 属性定义段(步骤16 的 hint_short, 提升取值准确性)
        prompt = self._load_prompt(ctx)
        if not prompt:
            raise StepDataError("找不到属性标注提示词（ATTR_STAB_PROMPT 未配置且默认路径不存在）")
        base_prompt = prompt
        attr_block = self._build_attr_hint_block(ctx, rules)
        if attr_block:
            prompt = prompt.rstrip() + "\n\n" + attr_block
        # 跨属性约束前移到标注阶段（用户口径 2026-10-08）：模型手里有原文与图片时
        # 就把冲突避开，比事后在步骤18 纠正更省、更准。
        cross_block = self._build_cross_check_block(ctx)
        compact_prompt = compact_annotation_prompt(base_prompt, attr_block, cross_block)
        if cross_block:
            prompt = prompt.rstrip() + "\n\n" + cross_block
            self.log(ctx, "已注入跨属性约束到标注提示词（%d 条）"
                     % cross_block.count("\n- **"))
        # ★ 2026-10-23 键名硬约束（用户口径: 只允许 spec 属性名 + `_依据`/`_来源`）。
        #   放在**最后** —— 提示词越长, 越靠后的段落越容易被当成"最终要求"遵守。
        #   实测 a2 被拒的直接原因就是 LLM 造键（`是否无线_依据补充` / `转速_转速_依据` /
        #   `材质_来源_依据` / `风速_来源_依据`），这些在闸门里是 required 错误。
        prompt = prompt.rstrip() + "\n\n" + (GENERAL_FLAT_CONTRACT if base_prompt.startswith('# 步骤17通用属性标注核心规则') else KEY_CONTRACT)
        prompt += ("\n\n本次文本标注仅提供标题、五点描述、详细参数和 SKU，"
                   "没有提供图片或图片 OCR；不得声称看到图片或使用图片作为依据。"
                   "图片将在独立视觉补充阶段处理。")

        # ④ 生产者：把 ctx.llm().chat_json 绑进来
        #    —— 该客户端已强制非思考模式 / JSON 模式 / 截断提上限 / 失败换温度重试
        llm = ctx.llm()
        n_runs = 2
        configured_chunk = _cfg_num(ctx.config, "ATTR_STAB_CHUNK", 12, int)
        chunk = annotation_chunk_size(configured_chunk, len(rules.attrs))
        if chunk != configured_chunk:
            self.log(ctx, "属性较多：每批行数 %d → %d，以减少截断和补跑" % (configured_chunk, chunk))
        # ★ 2026-10-22 修「输出被截断」: 实测 Dog Dryer 51 属性 × 12 行/批 → 单次输出 ~15,952 tokens，
        #   **正好撞上原来的 max_tokens=16000**，导致 24 行漏掉「尺寸」(连带 72 条闸门错误 → REJECT)。
        #   模型本身能给到约 64K，所以把上限提到 **64000**（可用 `ATTR_STAB_MAX_TOKENS` 覆盖）。
        #   注意: 输出总量不变（还是那些键值对），提上限只是让一次调用装得下，不再截断后重跑。
        _attr_max_tokens = _cfg_num(ctx.config, "ATTR_STAB_MAX_TOKENS", 64000, int)
        if _attr_max_tokens <= 0:
            _attr_max_tokens = 64000
        ctx.log.info("标注输出上限: max_tokens=%d（每批 %d 行；模型能力约 64K）"
                     % (_attr_max_tokens, chunk))
        prod = CallableProducer(
            fn=lambda p: llm.chat_json(p, max_tokens=_attr_max_tokens),
            rows=rows, out_dir=os.path.join(out_dir, "runs"), prefix="a",
            prompt_text=prompt,
            chunk=chunk, log=ctx.log.info,
            workers=_cfg_num(ctx.config, "ATTR_STAB_WORKERS", 3, int),
            cache_context=self._annotation_cache_context(ctx),
            input_fields=["xl_row", *TEXT_ANNOTATION_FIELDS],
            compact_attributes=list(rules.attrs), compact_prompt_text=compact_prompt,
            adjudication_hints={str(a.get("name") or ""): str((a.get("hint") if a.get("use_full_hint") else a.get("hint_short")) or a.get("hint") or a.get("hint_short") or "")
                                for a in self._spec_attributes})
        self.log(ctx, "开始标注: %d 行 × %d 属性, 跑 %d 遍(每批 %d 行, 完整性硬校验、其余问题只记录)"
                 % (len(rows), len(rules.attrs), n_runs, chunk))

        # ⑤ 跑闭环
        # ★ 依据键强制（2026-10-09 用户要求）：每个属性都要给 `<属性名>_依据` 原文。
        #   由 config 的 `ATTR_STAB_REQUIRE_BASIS` 控制（默认 true）。
        #   为什么在闸门里硬拦：**漏整列是唯一必须拦的方向**（同项目的既有原则）——
        #   不拦的话模型会静默只填部分属性，而"事后校验要判哪一侧错"完全没有依据。
        _rb = str(ctx.config.get("ATTR_STAB_REQUIRE_BASIS", "true")).strip().lower()
        require_basis = _rb not in ("0", "false", "no", "off")
        self.log(ctx, "依据键强制: %s" % ("开" if require_basis else "关"))
        res = run_round(
            prod, rules,
            n_runs=n_runs,
            max_alone=_cfg_num(ctx.config, "ATTR_STAB_MAX_ALONE", 10.0, float),
            max_retry=_cfg_num(ctx.config, "ATTR_STAB_MAX_RETRY", 2, int),
            input_rows=rows,
            brief_dir=os.path.join(out_dir, "retry_briefs"),
            output_template=os.path.join(out_dir, "runs", "a%(tag)s.json"),
            strict_gate=False,
            require_complete=True,
            require_basis=require_basis,
            resolve_conflicts=True,
            log=ctx.log.info)

        # ★ 2026-10-22 ③: **"依据比 value 更具体"自查**（只对「具体优先」属性）。
        #   通用做法: 若某行的值很短(≤4 字)、而它的 `_依据` 里出现了英文复合词
        #   （如 Alloy Steel / ABS Plastic / Stainless Steel），说明证据里有更具体的种类，
        #   值可能被降级成了上位词 → 只**报警**（不阻断），供人工/后续重判参考。
        try:
            self._report_under_specified(ctx, rules, res)
        except Exception as exc:  # noqa: BLE001
            self.log(ctx, "⚠ 上位词自查失败(忽略): %s" % exc)

        # ⑥ 落盘
        rp = os.path.join(out_dir, RESULT_NAME)
        result_data = res.to_dict()
        result_data["annotation_contract"] = contract
        write_json_atomic(rp, result_data)
        self.log(ctx, res.summary())
        self.log(ctx, "结果已写入 %s" % rp)

        # ⑦ 图片视觉补充(仅未明确) → 落盘视觉补充.json, 供步骤18 合并(值 + 来源)
        vision = self._vision_gapfill(ctx, res, rows, rules)
        vision_src = getattr(self, "_vision_sources", {})
        self.log(ctx, "图片视觉补充: 补出 %d 格(仅「未明确」格用图片再问)" % len(vision))
        with open(os.path.join(out_dir, "视觉补充.json"), "w", encoding="utf-8") as fh:
            json.dump({"cat": self._cat,
                       "filled": {"%s|%s" % (k[0], k[1]): v for k, v in vision.items()},
                       "sources": {"%s|%s" % (k[0], k[1]): v for k, v in vision_src.items()}},
                      fh, ensure_ascii=False, indent=2)

        # ⑧ 硬门槛
        # 2026-09-24 用户诉求：步骤17 只把属性值跑出来，不因一致性门槛中断标注
        #   → 「交付值一致率」降级为**仅提示**（不再抛错）。
        # 2026-09-29 补上真正必须拦的事故：**闸门后交付为空**。
        # 教训：原判据 `if not res.passed` 在 strict_gate=False 下含义已变——
        #   res.passed 是「产出成功的运行」而不是「过闸门的运行」，故只要 1 份产出
        #   成功就通过该检查；而 deliverable 为空（多数表决<2 份 → run_round 提前返回）
        #   时无人拦，于是步骤18 读到「0 属性」仍写出空交付表、流水线报「全部步骤完成」。
        #   实测：Salt and Pepper 2026-09-29（a1 末批超时被弃 + a2 过闸门失败）。
        #   故此处改为**按 deliverable 判定**，并保留 res.passed 判据(LLM 全失败)。
        floor = 0.0  # 固定为 0；历史配置不再覆盖。
        rate = ((res.metrics_delivery or {}).get("value") or {}).get("rate")
        if rate is None:
            self.log(ctx, "交付值一致率无可表决的格（提示门槛 0%，不阻断）")
        elif rate < floor:
            self.log(ctx, "⚠ 交付值一致率 %.2f%% 低于门槛 %.2f%%（宽松模式不阻断，仅提示）"
                     % (rate, floor))
        else:
            self.log(ctx, "交付值一致率 %.2f%%（门槛 %.2f%%，达标）" % (rate, floor))
        if not res.passed:
            raise StepFailedError("没有任何运行成功产出标注（LLM 调用全部失败）；详见 %s" % rp)
        gate_passed = [t for t, g in (res.gates or {}).items() if g.verdict == "PASS"]
        if not res.deliverable:
            raise StepFailedError(
                "闸门后无可交付内容：产出成功 %d 份、过闸门 %d 份（多数表决要求 ≥2 份）；"
                "被拒 %s。已产生的 LLM 用量不退，请重跑本步。详见 %s"
                % (len(res.passed), len(gate_passed),
                   "、".join(res.rejected) or "无", rp))

        # ⑨ 写 单遍原始 xlsx + 唯一值统计.json(供步骤18 标准化)
        self._write_raw(ctx, out_dir, rows, res, vision)
        self.log(ctx, "标注完成: 交付 %d 行 × %d 属性, 新词候选 %d 个"
                 % (len(rows), len(rules.attrs), len(res.vocab_candidates or [])))

        # ⑩ ★ 生成/更新标准化词表（2026-10-09 由步骤18 移来，为实现「停② 人工调整词表」）
        #   为什么移到这里：步骤18 原先**自己**生成词表 → 步骤17 跑完时"没有东西可调"。
        #   移来之后链路变成：
        #     16 提取属性 →[停①]确认属性集→ 17 标注 + 产词表 →[停②]确认词表→ 18 应用词表。
        #   并标记 `pending_confirm`：**步骤18 会拒绝在词表未确认时运行**（闸门在 18 里）。
        #   失败了也不拦步骤17（步骤18 的闸门会拒绝，用户能看见原因）。
        if spec_revision(load_attr_spec(ctx.resolve_artifact(SPEC_TPL.format(cat=self._cat)))) != contract["spec_sha256"]:
            raise StepDataError("属性集在步骤17运行中发生变化，请重新运行步骤17")
        try:
            self._set_vocab_status(ctx, VOCAB_PENDING)
        except (OSError, ValueError) as exc:
            raise StepDataError("无法使旧词表确认失效: %s" % exc) from exc
        try:
            self._synced_spec_revision = contract["spec_sha256"]
            self._generate_vocab(ctx, rules, res, cat_dir, cat_id)
            n = self._set_vocab_status(ctx, VOCAB_PENDING)
            self.log(ctx, "标准化词表待人工确认(已置 %s=%s, 影响 %d 个属性) —— "
                          "请在 Web「标准化词表」面板调整并**保存**（保存即确认）后再跑步骤18"
                     % (VOCAB_STATUS_KEY, VOCAB_PENDING, n))
        except Exception as exc:  # noqa: BLE001 — 词表生成失败不拦本步
            from tools.attr_stability.spec_sync import SpecConcurrentChange
            if isinstance(exc, SpecConcurrentChange):
                raise StepDataError(str(exc)) from exc
            self.log(ctx, "警告: 词表生成/标记待确认失败(步骤18 会拒绝运行): %s" % exc)
        current_spec = load_attr_spec(ctx.resolve_artifact(SPEC_TPL.format(cat=self._cat)))
        if (current_spec.get("definition_revision") != contract["definition_revision"] or
                spec_revision(current_spec) != getattr(self, "_synced_spec_revision", contract["spec_sha256"])):
            raise StepDataError("属性集在词表生成中发生变化，请重新运行步骤17")
        # 词表生成可以同步 hint 中的典型值；记录本轮完成时的完整属性集摘要。
        contract["spec_sha256"] = spec_revision(current_spec)
        contract["completed"] = True
        write_json_atomic(rp, result_data)
        self._verify_result_contract(ctx)

    # ------------------------------------------------------------ 辅助
    def _ensure_rules(self, ctx: StepContext) -> tuple[str | None, str | None]:
        """定位类目规则目录与主干; 目录下无 rules.json 时按已确认属性集自动建骨架再重定位。

        覆盖「已在 Web 确认属性集、但步骤16 的 _sync_rules 曾因缺规则文件跳过」的情形,
        避免步骤17 因缺 `*.rules.json` 报错。
        """
        cat_dir, cat_id = self._resolve_rules(ctx, writable=True)
        if cat_dir is not None:
            if self._spec_attributes:
                try:
                    with open(os.path.join(cat_dir, cat_id + ".rules.json"), encoding="utf-8-sig") as fh:
                        raw_rules = json.load(fh)
                    assert_rule_alignment({"attributes": self._spec_attributes}, raw_rules)
                except (OSError, ValueError) as exc:
                    raise StepDataError(str(exc)) from exc
            return cat_dir, cat_id
        stem = cat_rules_stem(self._cat)
        rules_path = os.path.join(str(ctx.paths.cat_root), stem + ".rules.json")
        create_rules_skeleton(rules_path, self._cat, self._spec_attributes or [])
        self.log(ctx, "未找到 rules.json, 已按已确认属性集自动创建骨架: %s(%d 个属性)"
                 % (rules_path, len(self._spec_attributes or [])))
        return self._resolve_rules(ctx)

    def _resolve_rules(self, ctx: StepContext, *, writable: bool = False) -> tuple[str | None, str | None]:
        """读取、确认和应用都通过统一入口；写继承规则时先建立本类目副本。"""
        path = self._rules_json_path(ctx, writable=writable)
        if not path:
            return None, None
        return os.path.dirname(path), os.path.basename(path)[:-len(".rules.json")]

    @staticmethod
    def _auto_onboard(ctx: StepContext) -> bool:
        return str(ctx.config.get("ATTR_STAB_AUTO_ONBOARD", "")).strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _gen_vocab(ctx: StepContext) -> bool:
        return str(ctx.config.get("ATTR_STAB_GEN_VOCAB", "")).strip().lower() in ("1", "true", "yes", "on")

    # 冷启动词表聚类提示词里，每个属性最多列出多少个唯一值。
    # 2026-09-29 由 80 提到 200：实测本类目 颜色 152 个、高度/尺寸 167 个唯一值，
    # 80 的上限会让 70+ 个写法**根本不进提示词** → 「归并」无从覆盖 → 那些值永远收敛不了、
    # 只能原样留在交付表里。截断时提示词会显式说明还剩多少个未列出。
    VOCAB_PROMPT_MAX_VALUES = 200

    @staticmethod
    def _vocab_hints(ctx: StepContext, cat_dir: str | None = None,
                     cat_id: str | None = None) -> dict:
        """人工给的**口径建议**（自然语言）：`{属性: "建议文本"}`。

        2026-10-08 用户口径：**不要**数字粒度旋钮（`ATTR_STAB_VOCAB_GRANULARITY` 已删）——
        那种 0~100 的阈值说不清"到底想怎么合"。改成人工用自然语言写建议，
        由 LLM 照着做，例如::

            "把具体树种（柚木/相思木/胡桃木…）合并成「木制」；竹类统一叫「竹制」"

        来源两处（后者覆盖前者）：
          · 全局：config 的 `ATTR_STAB_VOCAB_HINT`（整段文字，对全部属性生效）
          · 逐属性：rules.json 里该属性的 `params.vocab_hint`
        """
        out: dict = {}
        g = str(ctx.config.get("ATTR_STAB_VOCAB_HINT", "") or "").strip()
        if g:
            out["*"] = g
        if cat_dir and cat_id:
            try:
                import json as _json
                rp = os.path.join(cat_dir, cat_id + ".rules.json")
                if os.path.exists(rp):
                    with open(rp, encoding="utf-8-sig") as fh:
                        raw = _json.load(fh)
                    for a in (raw.get("attributes") or []):
                        p = a.get("params") if isinstance(a.get("params"), dict) else {}
                        h = str(p.get("vocab_hint") or "").strip()
                        if h and a.get("name"):
                            out[str(a["name"])] = h
            except (OSError, ValueError):
                pass
        return out

    @staticmethod
    def _gen_vocab_prompt(uniq: dict, hints: dict | None = None,
                          history: dict | None = None) -> str:
        cap = Step17AttrAnnotation.VOCAB_PROMPT_MAX_VALUES
        hints = hints or {}
        lines = []
        for attr, vals in uniq.items():
            head = vals[:cap]
            shown = "、".join("%s×%d" % (str(v)[:20], n) for v, n in head)
            tail = ("" if len(vals) <= cap
                    else "…（另有 %d 个低频写法未列出，**不要**为它们硬造类别）"
                         % (len(vals) - cap))
            lines.append("- 属性「%s」(去重 %d 个, 本次列出 %d 个): %s%s"
                         % (attr, len(vals), len(head), shown, tail))
        # 人工建议：全局一条 + 逐属性若干条
        hint_lines = []
        if hints.get("*"):
            hint_lines.append("- （**对所有属性**）%s" % hints["*"])
        for attr, h in hints.items():
            if attr == "*" or attr not in uniq:
                continue
            hint_lines.append("- 属性「%s」：%s" % (attr, h))
        hint_block = ""
        if hint_lines:
            hint_block = (
                "\n【**人工口径建议（优先级最高，必须照做）**】\n"
                "下面的建议是人工给出的归并口径，**与你的直觉冲突时以它为准**；\n"
                "若某条建议与数据明显不符，照建议做并在报告里说明。\n"
                + "\n".join(hint_lines) + "\n")
        # ★ 多轮历史(L1 全部口径建议 + L2 最近 5 轮完整结果): 按属性拼，供 LLM 在此基础上修正
        from tools.attr_stability.onboard import _vocab_history_block
        hist_parts = []
        for attr in uniq:
            rounds = (history or {}).get(attr) or []
            if not rounds:
                continue
            block = _vocab_history_block(rounds).strip()
            if block:
                hist_parts.append("【属性「%s」】\n%s" % (attr, block))
        hist_block = ""
        if hist_parts:
            hist_block = ("\n【历史轮次（各属性累积口径建议 + 最近 5 轮结果；按轮次，越后越新）】\n"
                          + "\n".join(hist_parts) + "\n")
        return (
            "你是类目属性词表生成助手。下面是每个属性在一批商品资料里**实际观察到**的取值清单(已去重, 附出现次数)。\n"
            "请按语义把同义/同类的写法归并成**规范类别**, 每个类别给一个规范写法。\n"
            "规范值要能覆盖绝大多数取值; 拿不准的写法并入最接近的类别, 实在没法归的不要硬造类别。\n"
            "**粒度**：以能覆盖绝大多数取值为准，不要为低频写法单独造类别。\n"
            "**禁止过度归并**：语义不同的类别**不许合并** —— 材质/形态/用途的差异、上位与下位概念"
            "上下位概念也可能表达不同粒度，需核对是否会丢失可区分的信息；"
            "只有同义、同写法、笔误、大小写差异才合并。\n"
            "%s\n"
            "%s"
            "%s\n\n"
            "严格输出 JSON: {\"attributes\": [{\"属性\": \"...\", \"规范值\": [\"...\"], "
            "\"归并\": [{\"原值\": \"...\", \"归入\": \"...\"}]}]}\n"
            "「归并」必须**逐条列出上面每个观察到的原始取值**, 标注它归入哪个规范值"
            "(原值本身就是规范值时, 归入=原值)。\n"
            % (hint_block, hist_block, "\n".join(lines))
        )

    def _generate_vocab(self, ctx: StepContext, rules, res, cat_dir, cat_id) -> None:
        """冷启动词表生成: 对「无受控词表」的 enum/multi 属性, 从唯一值语义聚类生成规范值。

        · 所有唯一值都送聚类(笔误/噪声由 LLM 聚类归并, 不做词频预过滤);
        · **归并口径由人工用自然语言给建议**（config `ATTR_STAB_VOCAB_HINT` 全局 +
          rules.json `params.vocab_hint` 逐属性），LLM 照做 ——
          原先的 `ATTR_STAB_VOCAB_GRANULARITY`(0~100) 已按用户要求删除：
          数字阈值说不清"到底想怎么合"。
        写回 rules.json 的 `params.values`(新格式), 并更新内存 rules.attrs 供随后归并标准化使用。
        """
        if not self._gen_vocab(ctx):
            return
        from tools.attr_stability.units import (
            QUANTITY_BASE, is_measure_like, looks_multi_dim, mixed_quantity_report,
            unit_factors_for,
        )
        from tools.attr_stability.unknowns import filter_unknown_words, resolve_unknowns
        # 0-2：接通类目 `unknown_values` 声明（此前只有注释提到、从未被读）
        _declared_unk: list = []
        try:
            _rp = os.path.join(cat_dir, cat_id + ".rules.json") if (cat_dir and cat_id) else ""
            if _rp and os.path.exists(_rp):
                with open(_rp, encoding="utf-8-sig") as _fh:
                    _declared_unk = (json.load(_fh) or {}).get("unknown_values") or []
        except Exception:  # noqa: BLE001
            _declared_unk = []
        _unk = resolve_unknowns(_declared_unk)
        # ★ 已有类目**也要**跑聚类（用户口径 2026-10-08）。
        #   原因：`词表聚类报告.json` 只覆盖「**已有属性的已有值**」，
        #   覆盖不了 ① **新属性**（spec 新增，如「是否双面不同材质」）
        #            ② **已有属性的新原值**（本轮才出现的写法）
        #   —— 这两类必须重新聚类，否则永远收敛不了。
        #   做法：候选属性从「受控值为空的」放宽到「全部 enum/multi」，
        #   收完唯一值后按上一轮报告算 **delta**，只对 delta 调 LLM（省钱且精准）。
        #   注：原 `empty = [...]` 变量已删（格式审核：采纳闸门去掉 `name in empty` 后
        #       它就没人读了，是本轮修复留下的死代码）。
        with open(os.path.join(cat_dir, cat_id + ".rules.json"), encoding="utf-8-sig") as fh:
            declared_rules = {a.get("name"): a.get("rule") or "enum" for a in json.load(fh).get("attributes", [])}
        enum_multi = {n for n, s in rules.attrs.items() if s.type in ("enum", "multi")
                      and declared_rules.get(n, "enum") in ("enum", "multi")}
        if not enum_multi:
            return
        prev_known = self._load_prev_vocab_known(ctx)
        # 归并口径改由**人工自然语言建议**给出（数字阈值 granularity 已删）
        hints = self._vocab_hints(ctx, cat_dir, cat_id)
        min_freq = 1  # 固定: 所有唯一值都送聚类, 笔误/噪声交给 LLM 聚类归并
        freq: dict[str, dict[str, int]] = {}
        for key, d in (res.cells or {}).items():
            if not isinstance(d, dict) or d.get("kind") != "value":
                continue
            _, _, path = key.partition("|")
            if not path.startswith("attr."):
                continue
            attr = path[len("attr."):]
            if attr not in enum_multi:
                continue
            for g in (d.get("values") or []):
                v = str((g or {}).get("value") or "").strip()
                # ★ 0-7（2026-10-07）：用**唯一权威哨兵集**过滤，不再靠本地零散白名单。
                # 原实现只排除了 RESERVED_VALUES + MISSING_KEY，**漏了 `未标注`** ——
                # 它于是进了 `dog_dryer.rules.json` 当合法受控值，`线长` 覆盖率虚高到 56.3%。
                kept, _dropped = filter_unknown_words([v], _unk)
                if kept:
                    f = freq.setdefault(attr, {})
                    f[kept[0]] = f.get(kept[0], 0) + 1
        uniq: dict[str, list[tuple[str, int]]] = {}
        for attr, counter in freq.items():
            kept = [(v, n) for v, n in counter.items() if n >= min_freq]
            kept.sort(key=lambda x: -x[1])
            if kept:
                uniq[attr] = kept

        # ★ 0-8 / 0-9（2026-10-07）：**数值型属性不做 enum 聚类**。
        # 实测：`容量` 被当枚举聚类 → `['55L','53L','108L','62L','50L']` 成了 5 个"类别"，
        # 换个型号（`1.3L`）立刻是词表外；`最大风速` 更混了 4 种单位。
        # 正确做法是 rule=numeric + **按物理量的 unit_factors**。
        numeric_decls: dict[str, dict] = {}
        mixed_warn: dict[str, str] = {}
        for attr in list(uniq.keys()):
            vals = [v for v, _n in uniq[attr]]
            if not is_measure_like(vals):
                continue
            if looks_multi_dim(vals, 0.15):
                # **多维尺寸串**（`13.5x6.5x15.49 cm`）不是单值计量 → 不能声明 numeric。
                # 实测 Salt&Pepper `高度/尺寸` 261 格三维串因被声明 numeric 而全部落"非数值"；
                # 且该属性**只有 19% 的取值是多维**（多数是 `8英寸`），
                # 阈值用 0.15：只要有**可观比例**是三维串，单值 numeric 列就容不下它们。
                # 这类属性走尺寸处理（保留原串，缺维由 Q3 补数），故保持不聚类、不改 rule。
                self.log(ctx, "0-8 跳过「%s」: 取值含多维尺寸串(%d 个)，不做 numeric 单值声明"
                         % (attr, len(vals)))
                uniq.pop(attr, None)
                continue
            mx = mixed_quantity_report(vals)
            q = mx.get("主口径")
            if not q or q == "ambiguous":
                continue
            unit = QUANTITY_BASE.get(q, "")
            numeric_decls[attr] = {
                "unit": unit, "quantity": q,
                "unit_factors": unit_factors_for(q, unit),
                "subcols": ["%s_数值" % attr, "%s_档位" % attr],
            }
            if mx.get("混量") or mx.get("少数物理量"):
                mixed_warn[attr] = mx["说明"]
            uniq.pop(attr, None)                     # 不送聚类
        if numeric_decls:
            self._write_numeric_declaration(cat_dir, cat_id, numeric_decls, ctx)
            for _n, _d in numeric_decls.items():
                _s = rules.attrs.get(_n)
                if _s is not None:
                    _s.type = "numeric"
                    _s.values = []
                    _s.unit = _d["unit"]
            self.log(ctx, "0-8/0-9 数值型声明: %d 个属性改 rule=numeric(按物理量给 unit_factors): %s"
                     % (len(numeric_decls),
                        {a: d["quantity"] for a, d in numeric_decls.items()}))
        if mixed_warn:
            self.log(ctx, "⚠ 0-9 混量提示(结构性错误属性，需拆属性或声明主口径): %s"
                     % json.dumps(mixed_warn, ensure_ascii=False)[:400])

        # ★ 按上一轮报告算 **delta**：只把「报告没覆盖的」送聚类。
        #   已有类目因此只花在新值/新属性上；冷启动时 `prev_known` 为空 → delta == 全量（行为不变）。
        known_total = sum(len(v) for v in uniq.values())
        if prev_known:
            delta_uniq: dict[str, list[tuple[str, int]]] = {}
            for _a, _vals in uniq.items():
                # ★ 若该属性**当前受控值为空**（如冷启动：`data/` 里被清空过），
                #   即使上一轮报告说"已知"，也必须**全量聚类** ——
                #   报告里的"已知值"要落到 `params.values` 才能用，而它是空的。
                #   实测踩过：报告存在 + 词表为空 → 判"无增量"→ 跳过聚类 →
                #   **词表永远是空的**，交付表回落原值、跨属性纠正也拿不到受控值
                #   （纠正只能建议 `新增: X`，因为受控值列表是空的）。
                _spec_a = self._rules_attr_values(rules, _a)
                _known = set() if not _spec_a else (prev_known.get(_a) or set())
                _nv = [(v, n) for v, n in _vals if v not in _known]
                if _nv:
                    delta_uniq[_a] = _nv
            uniq = delta_uniq
            self.log(ctx, "词表聚类**增量**: 上一轮报告已覆盖 %d 个原值 → 只对 %d 个新值聚类%s"
                     % (known_total - sum(len(v) for v in uniq.values()),
                        sum(len(v) for v in uniq.values()),
                        ("；新属性 %d 个" % len([a for a in uniq if a not in prev_known])
                         if any(a not in prev_known for a in uniq) else "")))

        if not uniq:
            self.log(ctx, "词表聚类: 本轮**无新增值/新属性**（上一轮报告已覆盖）→ 跳过 LLM 调用")
            return
        # ★ 多轮历史: 读该属性历史轮次(L1 全部口径建议 + L2 最近 5 轮结果)拼进 prompt
        _hist_path = os.path.join(ctx.resolve_artifact("18_标准化"), "词表聚类历史.json")
        _hist = {}
        try:
            if os.path.exists(_hist_path):
                _h = json.loads(open(_hist_path, encoding="utf-8-sig").read())
                if isinstance(_h, dict):
                    _hist = _h
        except Exception:                          # noqa: BLE001 — 历史损坏按无历史处理
            _hist = {}
        if _hist:
            self.log(ctx, "词表聚类历史: %d 个属性有历史轮次" % len(_hist))
        self.log(ctx, "冷启动词表生成: 待聚类属性 %d 个%s: %s"
                 % (len(uniq),
                    ("（含人工口径建议 %d 条）" % (len(hints) - (1 if hints.get("*") else 0))
                     if hints else ""),
                    {a: len(v) for a, v in uniq.items()}))
        try:
            raw = ctx.llm().chat_json(self._gen_vocab_prompt(uniq, hints, history=_hist),
                                      temperature=0.1, max_tokens=8000)
        except Exception as exc:  # noqa: BLE001
            self.log(ctx, "冷启动词表生成调用失败(忽略): %s" % exc)
            return
        attrs = raw.get("attributes") if isinstance(raw, dict) else None
        if not isinstance(attrs, list):
            self.log(ctx, "冷启动词表生成返回格式异常, 跳过")
            return
        adopted: dict[str, list[str]] = {}
        mapping: dict[str, dict[str, str]] = {}   # 属性 -> {原值: 归入规范值}
        for it in attrs:
            if not isinstance(it, dict):
                continue
            name = str(it.get("属性") or "").strip()
            vals = [str(v).strip() for v in (it.get("规范值") or []) if str(v).strip()]
            # ★★ 修复（逻辑审核高2，2026-10-10）：**输出侧哨兵守卫**。
            #   LLM 可能把 `未明确`/`未标注`/`未知` 当"规范值"返回（输入侧有
            #   `filter_unknown_words`，输出侧原来没有）→ 实测
            #   `{'主体材质': ['不锈钢','未标注']}` 会被写进 `params.values` 当合法受控值。
            #   注释自己记着这个坑（`未标注` 曾进 `dog_dryer.rules.json`，覆盖率虚高 56.3%）。
            #   ⚠ 并集修复**放大了暴露面**：原来只写"受控值为空"的属性，现在每个被采纳的都写。
            try:
                from tools.attr_stability.unknowns import filter_unknown_words
                vals = filter_unknown_words(vals)[0]
            except Exception:                                    # noqa: BLE001 — 守卫失败不拦生成
                pass
            # ★★ **采纳闸门去掉 `name in empty`**（2026-10-10 审核修复）
            #   原写法 `if name in empty and vals:` —— 只采纳**受控值为空**的属性，
            #   于是**已有类目每轮为新值付 LLM 费，然后一个词都写不回去**：
            #     行为实验: _write_vocab_to_rules({"有词表的":["A","B","C"],"空词表的":["X"]})
            #               → 有词表的 ['A','B']（新增的 'C' 被丢）、空词表的 ['X']
            #   而本函数开头的注释（L510-516）**明确宣称"已有类目也要跑聚类"** —— 注释与实现矛盾。
            #   冷启动测不出这条（那时全部属性都为空、全被采纳）。
            #   **改成采纳全部有 delta 的属性**；写回侧按下方的**并集**语义（只增不减），
            #   所以不会覆盖人工已确认的值。
            m = {}
            for e in (it.get("归并") or []):
                if isinstance(e, dict):
                    src = str(e.get("原值") or "").strip()
                    dst = str(e.get("归入") or "").strip()
                    if src and dst:
                        m[src] = dst
            if m:
                mapping[name] = m
                # ★ 2026-10-27: **规范值必须与归并自洽** —— 只保留「归入目标」,
                #   去掉被归并掉的原值。实测 LLM 会同时把「小型除湿机」列进规范值、
                #   又写「小型除湿机→除湿机」, 导致受控词表里两者并存
                #   (Web 上表现为"归并完还是有小型除湿机")。
                canonical = {v for v in m.values()}
                vals = [v for v in vals if v in canonical or v not in m]
                for c in canonical:
                    if c not in vals:
                        vals.append(c)
            if vals:
                adopted[name] = vals
        if not adopted:
            return
        # ★★ 修复（审核低危6，2026-10-10）：`是否*` 属性是**三态布尔**（是/否/未明确），
        #   词表里至少要有 `是` 和 `否`。聚类有时只聚出 `['是']`（如「是否双面可用」），
        #   → 否定答案（`否`）**无法表达**（实测「是否食品级」= 是(86)+未明确(14)，无一个否）。
        #   这里**确定性**补上缺失的布尔值（不需 LLM）。
        for name in list(adopted.keys()):
            if str(name).startswith("是否"):
                _v = adopted[name]
                for _b in ("是", "否"):
                    if _b not in _v:
                        _v.append(_b)
                adopted[name] = _v
        for name, vals in adopted.items():
            rules.attrs[name].values = list(vals)
        # ★ 2026-10-27: 把「被归并掉的原值」传下去，让并集写入能删掉它们
        _drop_by_attr = {n: {k for k, v in m.items() if k != v} for n, m in mapping.items()}
        _drop_by_attr = {n: d for n, d in _drop_by_attr.items() if d}
        self._write_vocab_to_rules(cat_dir, cat_id, adopted, ctx, drop_by_attr=_drop_by_attr)
        # ★ 同步 spec.hint 的「典型取值」到刚生成的受控值（改动清单 §22）。
        #   同一件事写两份必然漂移：hint 是"例子"、rules.values 是"法条"，
        #   例子越界就会诱导模型输出闸门不接受的值（实测 `是否含负离子`
        #   rules 只有 1 个值、hint 写了 5 个 → 58 格全掉）。不变量：例子 ⊆ 法条。
        try:
            from tools.attr_stability.spec_sync import sync_typical_values
            _sp = os.path.join(cat_dir, "%s-attribute-spec.json" % self._cat)
            _revision_out = {}
            _n = sync_typical_values(_sp, {a: v for a, v in adopted.items()},
                                     expected_revision=getattr(self, "_validated_spec_revision", None),
                                     revision_out=_revision_out)
            if _revision_out:
                self._synced_spec_revision = _revision_out["revision"]
            if _n:
                self.log(ctx, "已同步 spec.hint 的「典型取值」到受控值(%d 处)" % _n)
        except Exception as exc:  # noqa: BLE001 — 同步失败不影响交付
            from tools.attr_stability.spec_sync import SpecConcurrentChange
            if isinstance(exc, SpecConcurrentChange):
                raise
            self.log(ctx, "⚠ spec.hint 同步失败(忽略): %s" % exc)
        self._write_vocab_report(ctx, adopted, mapping, uniq, hints)
        # ★ 多轮历史: 把本轮(每属性)追加进 词表聚类历史.json（**本市场专用，不共享**）
        try:
            from datetime import datetime as _dt
            _now = _dt.now().isoformat(timespec="seconds")
            for name in adopted:
                rounds = list(_hist.get(name) or [])
                rounds.append({
                    "轮次": len(rounds) + 1,
                    "时间": _now,
                    "口径建议": str(hints.get(name) or hints.get("*") or "").strip(),
                    "规范值": list(adopted[name]),
                    "归并": [{"原值": k, "归入": v} for k, v in (mapping.get(name) or {}).items()],
                    "已保存": False,
                })
                _hist[name] = rounds
            os.makedirs(os.path.dirname(_hist_path), exist_ok=True)
            with open(_hist_path, "w", encoding="utf-8") as _fh:
                json.dump(_hist, _fh, ensure_ascii=False, indent=2)
            self.log(ctx, "词表聚类历史: 已追加本轮(%d 个属性) → %s" % (len(adopted), _hist_path))
        except Exception as exc:  # noqa: BLE001 — 历史写入失败不影响主流程
            self.log(ctx, "⚠ 词表聚类历史写入失败(忽略): %s" % exc)
        # ★ 新属性 / 已有属性的新值 → 「待确认清单」（用户口径 2026-10-08）
        try:
            self._write_pending_review(
                ctx, ctx.resolve_artifact("18_标准化"),
                delta={a: [(v, n) for v, n in vals] for a, vals in uniq.items()},
                mapping=mapping,
                # ★ 传**跑之前**算好的 prev_known（此刻报告已被本轮覆盖，
                #   函数内部再读就变成"读到本轮自己的报告"→ 冷启动误判成增量）
                prev_known=prev_known)
        except Exception as exc:  # noqa: BLE001 — 清单失败不影响交付
            self.log(ctx, "⚠ 待确认清单写入失败(忽略): %s" % exc)
        self.log(ctx, "冷启动词表生成: %d 个属性生成了受控词表 %s"
                 % (len(adopted), {a: len(v) for a, v in adopted.items()}))
        # 归并过程写入日志(只列「原值≠归入」的同义/笔误归并, 原值=归入的不刷屏)
        for name in adopted:
            mp = mapping.get(name) or {}
            merged = [(v, mp[v]) for v, _ in uniq.get(name, [])
                      if mp.get(v) and mp[v] != v]
            if merged:
                shown = "、".join("%s→%s" % (a, b) for a, b in merged[:15])
                self.log(ctx, "  词表归并[%s]: %d 个同义写法 → %s%s"
                         % (name, len(merged), shown, "…" if len(merged) > 15 else ""))

    @staticmethod
    def _rules_attr_values(rules, name: str) -> list:
        """取该属性**当前**的受控值（兼容 `RuleSource` 对象与原始 dict）。

        为什么需要：`rules` 在步骤18 里是 `RuleSource`（`.attrs[name].values`），
        在别处可能是 rules.json 的 dict（`attributes[].params.values`）。
        两种都取不到就返回 `[]`（视为"词表为空"→ 触发全量聚类）。
        """
        from collections.abc import Mapping      # 局部导入（本模块顶部没导它）

        try:
            attrs = getattr(rules, "attrs", None)
            if isinstance(attrs, Mapping) and name in attrs:
                return list(getattr(attrs[name], "values", None) or [])
        except (AttributeError, TypeError):
            pass
        try:
            for a in (rules or {}).get("attributes") or []:
                if str(a.get("name") or "") == name:
                    p = a.get("params") if isinstance(a.get("params"), Mapping) else {}
                    return list(p.get("values") or a.get("values") or [])
        except (AttributeError, TypeError):
            pass
        return []

    def _load_prev_vocab_known(self, ctx: StepContext) -> dict[str, set]:
        """读回**上一轮** `词表聚类报告.json`，返回 `{属性: {已知原值}}`。

        为什么要读回（用户口径 2026-10-08）：此前三个报告**只写不读** ——
        已有类目每轮都从零收敛，上一轮"哪个原值归到哪个规范值"的判断全丢。
        本函数只取「已知原值集合」用于算 delta；映射本身由 `learned_map` 负责套用。
        读不到（冷启动/首次）→ 返回 `{}`，此时 delta == 全量，行为与原来一致。
        """
        out: dict[str, set] = {}
        try:
            p = os.path.join(ctx.resolve_artifact("18_标准化"), "词表聚类报告.json")
            if not os.path.exists(p):
                return out
            with open(p, encoding="utf-8-sig") as fh:
                rep = json.load(fh)
            for a in (rep.get("attributes") or []):
                if not isinstance(a, dict):
                    continue
                name = str(a.get("属性") or "").strip()
                if not name:
                    continue
                known = out.setdefault(name, set())
                for e in (a.get("归并") or []):
                    if isinstance(e, dict) and str(e.get("原值") or "").strip():
                        known.add(str(e["原值"]).strip())
                for v in (a.get("规范值") or []):
                    if str(v).strip():
                        known.add(str(v).strip())
        except (OSError, ValueError):
            return {}
        return out

    def _write_pending_review(self, ctx: StepContext, out_dir: str,
                              delta: dict | None = None, mapping: dict | None = None,
                              cands: list | None = None,
                              prev_known: dict | None = None) -> str | None:
        """写「**待确认清单**」：新属性 / 新原值 / 词表外新词，交人工确认（用户口径 2026-10-08）。

        为什么要单独一张：新词原本只躺在 `新词初审裁决.json` 里，且**新属性**
        （spec 新增、上一轮报告没有）根本没人列出来。人工需要一张一眼能看完的清单。
        产物：`18_标准化/待确认清单.json`（+ 同名 .md 便于直接读）。

        ★ `prev_known` 必须由调用方**在写本轮报告之前**算好并传入（2026-10-09 修正）。
          踩过的坑：本函数内部自己 `_load_prev_vocab_known()`，而它是在
          `_write_vocab_report()` **之后**被调用的 → 读到的是**本轮刚写下去的报告**
          → 冷启动被误判成「增量」，实测报了 12 个假「新属性」。
        """
        import json as _json
        from datetime import datetime

        if prev_known is None:
            prev_known = self._load_prev_vocab_known(ctx)
        # ⚠ 用 `not prev_known` 判冷启动，**不能**用 `prev_known is None` ——
        #   冷启动传进来的是 `{}`（空 dict，不是 None），用 is None 会判成"增量"
        #   → 又把全部属性列成"新属性"（实测踩过）。
        cold = not prev_known
        items: list[dict] = []

        # ① 新属性：本轮提交聚类的属性里，上一轮报告完全没有的
        #    **冷启动不列**（`cold=True`）：没有参照物时"新"没有意义。
        if not cold:
            for a in (delta or {}):
                if a not in prev_known:
                    items.append({"类型": "新属性", "属性": a,
                                  "说明": "本轮首次出现（上一轮聚类报告里没有），"
                                          "已对新值聚类；请在词表面板确认它的口径",
                                  "新值": [v for v, _n in (delta or {}).get(a, [])][:20]})

        # ② 新原值：已有属性、但上一轮报告没覆盖的写法
        #    ★ **自己按 prev_known 过滤**，不信任调用方传进来的 delta。
        #    实测踩过：调用方把已知值也塞进 delta → 清单里出现"新值：柚木/竹制"，
        #    而它们在上一轮报告里明明已有 —— 清单一旦不准，人工就不会再看它。
        for a, vals in (delta or {}).items():
            if a not in prev_known:
                continue
            _known = prev_known.get(a) or set()
            fresh = [(v, n) for v, n in vals if v not in _known]
            if not fresh:
                continue
            items.append({"类型": "已有属性的新值", "属性": a,
                          "新值": [v for v, _n in fresh][:30],
                          "归入": {v: (mapping or {}).get(a, {}).get(v, "")
                                   for v, _n in fresh if (mapping or {}).get(a, {}).get(v)}})

        # ③ 词表外新词（AI 初审结论）
        for c in (cands or []):
            if not isinstance(c, dict):
                continue
            items.append({"类型": "词表外新词", "属性": c.get("属性"),
                          "原值": c.get("原值") or c.get("值"),
                          "AI建议": c.get("建议") or c.get("结论"),
                          "理由": c.get("理由")})

        if not items:
            return None
        payload = {
            "_说明": ("**待确认清单**：新属性 / 已有属性的新值 / 词表外新词。"
                      "AI 初审已按 ATTR_STAB_AUTO_ONBOARD 处理；本清单供人工复核，"
                      "确认后在「步骤18 标准化词表」面板改口径建议并重新生成。"),
            "cat": self._cat,
            "模式": "冷启动（无上一轮报告，不列「新属性」）" if cold else "增量（对比上一轮报告）",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "统计": {k: sum(1 for i in items if i["类型"] == k)
                     for k in ("新属性", "已有属性的新值", "词表外新词")},
            "items": items,
        }
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, "待确认清单.json")
        with open(p, "w", encoding="utf-8") as fh:
            _json.dump(payload, fh, ensure_ascii=False, indent=2)
        # 同名 .md：人工直接读
        lines = ["# 待确认清单 —— %s" % self._cat, "",
                 "生成时间：%s" % payload["generated_at"], "",
                 "| 类型 | 属性 | 内容 |", "|---|---|---|"]
        for i in items:
            content = (i.get("新值") or i.get("原值") or "")
            if isinstance(content, list):
                content = "、".join(str(x) for x in content[:12]) + ("…" if len(content) > 12 else "")
            lines.append("| %s | %s | %s |" % (i.get("类型"), i.get("属性"), content))
        with open(os.path.join(out_dir, "待确认清单.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        self.log(ctx, "待确认清单: %s → %s"
                 % (payload["统计"], os.path.basename(p)))
        return p

    def _correct_cross_conflicts(self, ctx: StepContext, rules, res, rows, std: dict,
                                 attrs_spec: dict | None = None) -> dict:
        """**校验 + 纠正** 跨属性约束违例（用户口径 2026-10-09）。

        链路：**16 声明约束 → 17 标注时避开 → 18 校验 + 纠正**（本方法）。
        此前步骤18 只写 `跨属性冲突.json`（**登记**），冲突行原样进交付表 ——
        用户指出这不够："既然有冲突，应该再加个校验和纠正的步骤"。

        做法（保守：**宁可不改，也不错改**）
        ------------------------------------------------------------------
          ① 用声明的约束在**当前 std** 上复算违例（代码判定，不靠 LLM）；
          ② 把「违例行 + 违约约束 + 两侧当前值/来源/依据 + 受控值词表」交 LLM，
             让它判「**改哪一侧、改成什么**」（它可以答 `无解`）；
          ③ **代码侧闸门**（独立验证，不信任 LLM）：
             · 新值必须逐字符在受控值内，或形如 `新增: X`（越界即弃）；
             · 应用后**复算不得再违例**（否则弃）；
             · 只允许改**被点名的那一格**，其余不动；
          ④ 通过 → 改 std；不通过 → 进「待人工」。全过程写
             `18_标准化/跨属性冲突纠正.json`。

        返回 `{"已纠正": [...], "待人工": [...], "依据": str}`。
        """
        import json as _json
        from collections.abc import Mapping, Sequence

        from tools.attr_stability.cross_checks import check_row, load_cross_checks

        decl = load_cross_checks(rules)
        if not decl:
            return {}
        spec = attrs_spec or {}

        def _vals_of(s) -> list[str]:
            """取受控值，**兼容两种形状**。

            踩过的坑（与 `derive._params_of` 同一类）：`attrs_spec` 在项目里有两种形态 ——
              · 嵌套：`{属性: {"values": [...], "type": …}}`（`attr_spec_from_modern` 产出）
              · 扁平：`{属性: [v1, v2, …]}`
            只处理嵌套会让扁平形态直接 `AttributeError`。
            """
            if isinstance(s, Mapping):
                vals = s.get("values")
            elif isinstance(s, Sequence) and not isinstance(s, (str, bytes)):
                vals = s
            else:
                vals = None
            return [str(v) for v in (vals or [])]

        vals_by = {n: _vals_of(s) for n, s in spec.items()}
        cells = res.cells or {}

        def _ev(rid, attr) -> dict:
            out = {}
            for suf in ("来源", "依据"):
                d = cells.get("%s|attr.%s_%s" % (rid, attr, suf))
                if isinstance(d, dict) and str(d.get("mode") or "").strip():
                    out[suf] = str(d["mode"])[:160]
            return out

        # ★★ 必须把**原文**喂进去（2026-10-09 实测）：
        #   只给「来源=标题」这种标签，两侧看起来**一模一样**，模型无从判别该改哪侧 ——
        #   实测它一律改「主体材质」，把 `钛金属+木制`（依据就是纯钛+木纤维）改成 `不锈钢`，
        #   **把正确数据改错**。必须给它 标题/五点 的原文，它才能看出
        #   「标题是 Titanium，而 产品类型 填了 不锈钢砧板」才是矛盾的那个。
        row_by: dict = {}
        for _r in (rows or []):
            if not isinstance(_r, Mapping):
                continue
            try:
                row_by[int(_r.get("xl_row"))] = _r
            except (TypeError, ValueError):
                continue

        def _text(rid) -> str:
            r = row_by.get(rid) or {}
            bits = []
            for k in ("标题", "商品标题", "五点描述", "详细参数"):
                v = str(r.get(k) or "").strip()
                if v:
                    bits.append("%s：%s" % (k, v[:260]))
            return " ｜ ".join(bits)

        # ① 复算违例
        # ★★ 必须与**登记**用同一份值（2026-10-09 修，实测踩到的真分歧）：
        #   原实现自己从 `std` 拼行值 —— 与 `_register_cross_conflicts` 用的
        #   `_final_rows(res, std_map)` **不是同一份**：
        #     · `_final_rows` 用 `deliverable` **打底** + `std` 覆盖（取并集）；
        #     · 原实现只看 `std`，**缺的属性一律变空串**、且只看 `std` 出现过的行。
        #   后果：同一轮里「登记说 1 个冲突、纠正说 0 个」——
        #   交付目录里留下自相矛盾的两个产物（人复核时分不清谁对）。
        hits: list[dict] = []
        for _rw in self._final_rows(res, std):
            _rid = _rw.get("xl_row")
            _attr = _rw.get("attributes") or {}
            rowv = {a: str(_attr.get(a, "") or "") for a in vals_by}
            for hh in check_row(rowv, decl, _rid):
                hh["_rowv"] = rowv
                hits.append(hh)
        if not hits:
            # ★ 早退**也要写记录**（2026-10-09 修）：否则人复核时分不清
            #   「这一轮没有违例」和「这一步根本没跑」—— 我自己就被绕进去过一轮。
            self._write_correction(ctx, {
                "已纠正": [], "待人工": [], "已采纳新值": {},
                "模式": "无违例（不改值）",
                "_说明": "本轮**没有检测到**跨属性约束违例（与「登记」同一份值口径）。",
                "cat": self._cat})
            return {"已纠正": [], "待人工": [], "依据": "无违例"}

        # ② 交 LLM 判定
        payload = []
        for h in hits:
            la, ra = str(h.get("左属性")), str(h.get("右属性"))
            payload.append({
                "行": h.get("行号"), "违约约束": h.get("说明"),
                # ★ 原文是判「哪一侧错」的**唯一可靠依据**（两侧的 来源 标签往往相同）
                "原文": _text(h.get("行号")),
                "左": {"属性": la, "当前值": h.get("左值"), "受控值": vals_by.get(la, [])[:30],
                       **_ev(h.get("行号"), la)},
                "右": {"属性": ra, "当前值": h.get("右值"), "受控值": vals_by.get(ra, [])[:30],
                       **_ev(h.get("行号"), ra)},
            })
        def _mk_prompt(batch: list) -> str:
            return (
                "你是亚马逊类目属性审校员。下面每一行都**违反了**一条已确认的跨属性硬约束。\n"
                "请判断：**该改哪一侧、改成什么值**，才能同时满足约束且不违背证据。\n\n"
                "要求：\n"
                "1. 只改**一处**（左或右），另一处不动；\n"
                "2. 新值必须**逐字符**来自该属性的「受控值」，或形如 `新增: X`"
                "（当受控值里确实没有能正确表达的值时——**不要硬塞语义不符的值**）；\n"
                "3. ★★ **绝对不许改「依据支持它」的那一侧**（最容易犯的错）：\n"
                "   先逐侧看 `依据` 原文 —— 若某侧的**依据明确支持它当前的值**，"
                "   那么这一侧是**对的**，改它就是**把正确数据改错**。\n"
                "   此时必须改**另一侧**；若另一侧的 `依据` 也支持它当前的值 → 答 `无解`。\n"
                "   判据：哪一侧的「依据」与「它自己的当前值」**互相矛盾**，错的就是那一侧。\n"
                "   （实测教训：`主体材质=钛金属+木制`，依据「纯钛+木纤维」——明确支持它，"
                "    却被改成 `不锈钢`，把正确数据改错了；真正矛盾的是 `产品类型=不锈钢砧板`，"
                "    而它的依据写着「标题含 Titanium」。）\n"
                "4. ★ **上面每一行都必须出现在输出的数组里，一个都不能少**：\n"
                "   · 能判断 → 给 `属性` + `新值`（受控值 或 `新增: X`）\n"
                "   · 判不了 → 也要给一条，`新值` 写 `无解`，并在「理由」里说明缺什么证据\n"
                "   ⚠ **不许沉默省略** —— 省略会让下游分不清「没问题」和「你没看」，\n"
                "     实测就因为写了「无法判断的行不要出现」导致模型**整批省略**、返回空数组。\n\n"
                + _json.dumps(batch, ensure_ascii=False, indent=2)
                + '\n\n严格输出 JSON: {"纠正": [{"行": 159, "属性": "产品类型", '
                  '"新值": "钛金属砧板", "理由": "标题是 Titanium，受控值无此类"}, '
                  '{"行": 160, "属性": "主体材质", "新值": "无解", "理由": "…"}]}\n'
                  "（条目数必须等于上面给出的行数）"
            )

        # ★ **分批**（2026-10-09 实测）：一次给 25 行时模型返回空数组（一条都不提），
        #   分批后每批 8 行它才逐条判。批次大小可配 `ATTR_XC_BATCH`（默认 8）。
        batch_size = max(1, int(_cfg_num(ctx.config, "ATTR_XC_BATCH", 8, int)))
        raw_items: list = []
        n_batch = 0
        for i in range(0, len(payload), batch_size):
            batch = payload[i:i + batch_size]
            n_batch += 1
            try:
                raw = ctx.llm().chat_json(_mk_prompt(batch), temperature=0.2, max_tokens=4000)
            except Exception as exc:  # noqa: BLE001
                self.log(ctx, "跨属性冲突纠正第 %d 批调用失败(忽略该批): %s" % (n_batch, exc))
                continue
            got = raw.get("纠正") if isinstance(raw, dict) else None
            if isinstance(got, list):
                raw_items.extend(got)
        if n_batch:
            self.log(ctx, "跨属性冲突纠正: 分 %d 批送判（每批 %d 行），共得建议 %d 条"
                     % (n_batch, batch_size, len(raw_items)))
        items = raw_items

        # ③ 代码侧闸门 + 应用
        hit_by_row = {h.get("行号"): h for h in hits}
        fixed: list[dict] = []
        manual: list[dict] = []
        # ★★ 安全阀（2026-10-09）：纠正**默认只报告、不落库**。
        #   理由：实测纠正会**改错侧**（把 `主体材质=钛金属+木制` 改成 `不锈钢`），
        #   而代码侧闸门只校验「一致」（新值在词表内 + 改完不再违例），
        #   **校验不了「真实」** —— 于是它能把正确数据改成自洽但错误的值。
        #   在纠正准确率被验证之前，默认只写建议交人工。
        #   ⚠ 这里必须是 **if/elif 互斥分支**，不能"先 fixed=[] 再照跑循环" ——
        #     那样循环会把 fixed 又填回去，**安全阀被自己架空了**（我踩过）。
        apply_fix = str(ctx.config.get("ATTR_XC_AUTO", "true")).strip().lower() not in (
            "0", "false", "no", "off")
        if str(ctx.config.get("ATTR_XC_APPLY", "")).strip().lower() in ("1", "true", "yes", "on"):
            apply_fix = True                      # 旧开关仍认（兼容）
        adopted_new: dict[str, list[str]] = {}
        if not apply_fix:
            for it in items:
                if not isinstance(it, dict):
                    continue
                manual.append({"行": it.get("行"), "属性": str(it.get("属性") or ""),
                               "建议值": str(it.get("新值") or ""),
                               "理由": str(it.get("理由") or "")[:120],
                               "原因": "仅报告模式（ATTR_XC_AUTO 已关）——不自动改值"})
        else:
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("行")
                attr = str(it.get("属性") or "").strip()
                nv = str(it.get("新值") or "").strip()
                h = hit_by_row.get(rid)
                why = ""
                adopt = ""                          # 需要**新采纳**进词表的值
                eff = nv                            # 实际要写进那一格的值
                if h is None:
                    why = "行号不在违例清单里"
                elif attr not in (str(h.get("左属性")), str(h.get("右属性"))):
                    why = "改名了不相关的属性(%s)" % attr
                elif not nv or nv.lower() == "无解":
                    why = "LLM 判为无解"
                elif nv.startswith("新增"):
                    # ★★ 「新增: X」= 模型说"受控值里没有能表达证据的值"。
                    #   2026-10-09 起**自动采纳**（用户口径：AI 该自己完成，不要一律推给人工）。
                    #   为什么现在敢自动：加了 `依据` 之后模型**判对了侧** ——
                    #   实测 26/26 条建议都是 `产品类型 → 新增: 钛金属砧板`
                    #   （而不是把 `主体材质=钛金属` 改成 `不锈钢`）。证据充分。
                    #   仍需过下面这些**代码侧判据**（不靠模型自述）：
                    x = nv.split(":", 1)[-1].split("：", 1)[-1].strip()
                    if not x:
                        why = "新增值为空"
                    elif len(x) > 20:
                        why = "新增值过长(%d字)，疑似不是取值" % len(x)
                    elif x in vals_by.get(attr, []):
                        # ★ 已在词表里 → **直接应用**，这不是错。
                        #   2026-10-09 实测（100 行）：纠正是**分批**跑的，
                        #   第 1 批采纳 `钛金属砧板` 之后，第 2 批里同样提
                        #   `新增: 钛金属砧板` 的 **9 行被这条判据误拒** →
                        #   那 9 行留在错误值上（登记仍有 9 个冲突）。
                        eff = x
                    else:
                        adopt = x
                        eff = x
                elif nv not in vals_by.get(attr, []):
                    why = "新值不在受控值内: %r" % nv
                if not why:
                    # 预演：改完是否还有违例？有就不改
                    trial = dict(std)
                    trial[(rid, attr)] = eff
                    rowv2 = {a: str(trial.get((rid, a), "") or "") for a in vals_by}
                    if check_row(rowv2, decl, rid):
                        why = "改完仍有违例（不应用）"
                if why:
                    manual.append({"行": rid, "属性": attr, "建议值": nv, "原因": why,
                                   "说明": (h or {}).get("说明")})
                    continue
                if adopt:
                    lst = adopted_new.setdefault(attr, [])
                    if adopt not in lst:
                        lst.append(adopt)
                    # 内存先行：让**同一批后续行**按长大后的词表校验（也避免"改副本"）
                    vals_by.setdefault(attr, []).append(adopt)
                std[(rid, attr)] = eff
                fixed.append({"行": rid, "属性": attr,
                              "原值": (h or {}).get("左值" if attr == h.get("左属性") else "右值"),
                              "新值": eff, "理由": it.get("理由"),
                              "违约约束": (h or {}).get("说明"),
                              "新增受控值": adopt or None})

        # 自动采纳的新值 → **先改内存、再落盘**（见"改副本"的教训：顺序反了就白改）
        if adopted_new:
            try:
                for _a, _vs in adopted_new.items():
                    _sp = getattr(rules, "attrs", {}).get(_a)
                    if _sp is not None and hasattr(_sp, "values"):
                        _cur = [str(x) for x in (getattr(_sp, "values", None) or [])]
                        _sp.values = _cur + [v for v in _vs if v not in _cur]
                _n = self._append_vocab_values(ctx, adopted_new)
                self.log(ctx, "已自动采纳 %d 个新受控值进词表: %s"
                         % (_n, {k: v for k, v in adopted_new.items()}))
            except Exception as exc:  # noqa: BLE001 — 落盘失败不影响本轮交付
                self.log(ctx, "警告: 自动采纳的新值写回失败(内存内已生效): %s" % exc)

        out = {"已纠正": fixed, "待人工": manual,
               "已采纳新值": adopted_new,
               "模式": "自动应用（含新增值自动进词表）" if apply_fix else "仅报告（不改值）",
               "_说明": ("跨属性冲突的**纠正记录**。`自动应用` 模式下只落库过了代码侧闸门的改动"
                         "（值必须在受控值内、且改完不再违例）；"
                         "模型答 `新增: X` 的会**自动进词表**（`已采纳新值`），"
                         "条件是：由**已声明的跨属性约束**触发 + 值形态合法 + 改完不再违例。"
                         "`仅报告` 模式下**一个字都不改**。"),
               "cat": self._cat}
        self._write_correction(ctx, out)
        return out

    def _write_correction(self, ctx, out: dict) -> None:
        """落盘 `18_标准化/跨属性冲突纠正.json`（**含"无违例"的情况**）。

        为什么单独抽出来：原先写盘只在"有违例"的路径上，`hits` 为空就**早退不写** ——
        于是人复核时分不清「这一轮没违例」和「这一步根本没跑」，
        我自己就被这个坑绕进去查了一轮（拿上一轮的陈旧文件当当前状态）。
        """
        import json as _json
        try:
            d = ctx.resolve_artifact("18_标准化")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "跨属性冲突纠正.json"), "w", encoding="utf-8") as fh:
                _json.dump(out, fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            self.log(ctx, "警告: 纠正记录写盘失败(忽略): %s" % exc)

    def _write_vocab_report(self, ctx, adopted: dict, mapping: dict,
                            uniq: dict, hints: dict | None = None) -> None:
        """把「原始值 → 规范值」归并过程落成报告, 供人工审核(也进 Web 弹窗)。"""
        from datetime import datetime
        attrs_report = []
        for name, vals in adopted.items():
            entries = []
            mp = mapping.get(name) or {}
            for v, n in (uniq.get(name) or []):
                entries.append({"原值": v, "次数": n, "归入": mp.get(v, v)})
            attrs_report.append({"属性": name, "规范值": vals, "归并": entries})
        report = {
            "cat": self._cat,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "hints": {k: v for k, v in (hints or {}).items()},   # 人工口径建议（可审计）
            "attributes": attrs_report,
        }
        out_dir = ctx.resolve_artifact("18_标准化")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "词表聚类报告.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        self.log(ctx, "词表聚类报告已写入: %s" % path)

    def _append_vocab_values(self, ctx: StepContext, adds: dict) -> int:
        """把**自动采纳的新受控值追加**进 rules.json（只追加，绝不覆盖已有值）。

        为什么不复用 `_write_vocab_to_rules`：那个函数语义不同（并集写入全部被采纳属性）。
        这里是往**已有词表**里追加**跨属性纠正采纳的新值**。

        返回实际新增的值的个数。**内存由调用方先改**（见 `_correct_cross_conflicts`）——
        顺序不能反：改文件不改内存 = 白改（这个坑我踩过）。

        ★★ 修复（逻辑审核高2，2026-10-10）：**输出侧哨兵守卫** —— `新增: 未明确` 这类
          会被追加进 `params.values` 当合法受控值（输入侧有 `filter_unknown_words`，
          输出侧原来没有；注释自己记着这个坑：`未标注` 曾进 `dog_dryer.rules.json`）。
          这里统一 `str().strip()` + **丢哨兵** + 去空串。
        """
        path = self._rules_json_path(ctx, writable=True)
        if not path:
            return 0
        try:
            from tools.attr_stability.unknowns import filter_unknown_words
            adds = {k: filter_unknown_words(
                        [s for s in (str(v).strip() for v in vs) if s])[0]
                    for k, vs in adds.items()}
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            n = 0
            for a in (data.get("attributes") or []):
                name = str(a.get("name") or "")
                if name not in adds:
                    continue
                params = a.get("params") if isinstance(a.get("params"), dict) else None
                tgt = params if params is not None else a
                cur = [str(x) for x in (tgt.get("values") or [])]
                for v in adds[name]:
                    if v not in cur:
                        cur.append(v)
                        n += 1
                tgt["values"] = cur
            if n:
                # ★ 2026-10-14(B1): rules.json 是**定义层**文件, 写坏 → 16/17/18 全挂。
                #   原为 `open(path,"w")` 直写, 中断/异常会留半截 JSON。改走原子写。
                write_json_atomic(path, data)
            return n
        except Exception as exc:  # noqa: BLE001
            self.log(ctx, "警告: 追加受控值到 rules.json 失败(内存内已生效): %s" % exc)
            return 0

    def _rules_json_path(self, ctx: StepContext, *, writable: bool = False) -> str:
        """统一定位规则；有歧义时拒绝猜测。"""
        try:
            return resolve_rules_file(str(ctx.paths.cat_root), writable=writable) or ""
        except ValueError as exc:
            raise StepDataError(str(exc)) from exc

    def _vocab_status(self, ctx: StepContext) -> str:
        """读 `rules.json` 的 `vocab_status`（缺省返回空串 = 从未生成/未确认）。"""
        p = self._rules_json_path(ctx)
        if not p or not os.path.exists(p):
            return ""
        try:
            with open(p, encoding="utf-8-sig") as fh:
                return str((json.load(fh) or {}).get(VOCAB_STATUS_KEY) or "")
        except (OSError, ValueError):
            return ""

    def _set_vocab_status(self, ctx: StepContext, status: str) -> int:
        """把 `rules.json` 的 `vocab_status` 置为给定值。

        返回该词表覆盖的属性数（有非空 `params.values` 的属性个数）——
        让日志能说清"这次确认影响多少属性"，而不是只报一个状态字符串。
        **原子写**：先写 `.tmp` 再 `os.replace`，避免写一半留下坏 JSON。
        """
        p = self._rules_json_path(ctx, writable=True)
        if not p or not os.path.exists(p):
            return 0
        with open(p, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        data[VOCAB_STATUS_KEY] = str(status)
        n = 0
        for a in (data.get("attributes") or []):
            params = a.get("params") if isinstance(a.get("params"), dict) else {}
            if params.get("values") or a.get("values"):
                n += 1
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return n

    def _write_vocab_to_rules(self, cat_dir, cat_id, adopted: dict, ctx,
                              drop_by_attr: dict | None = None) -> None:
        """把生成的规范值写回 rules.json(新格式 params.values; 兼容旧格式顶层 values)。

        ★ **并集写入（只增不减）**（2026-10-10 修复）：原写法只在 `params.values` **为空**时写，
          于是**已有类目每轮为新值付 LLM 费、一个词都写不回去**（注释宣称"已有类目也要跑聚类"
          与实现矛盾；冷启动测不出，因为那时全部属性都为空）。
          现在：**已有值保留不动**（人工确认过的词表不会被 LLM 悄悄改掉）+ **新值追加**。
          **不自动删除**（合并/清理交「停②」的人工确认）。
          ⚠ 代价：词表会**单调增长**，改名场景下新旧并存 —— 属有意的设计取舍。
        ★ 写入前统一 `str().strip()` + 丢空串 + 去重（保持"字符串列表"契约）。
        """
        if not cat_dir or not cat_id:
            return
        path = os.path.join(cat_dir, cat_id + ".rules.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8-sig") as fh:
                rules = json.load(fh)
            # ★★ 修复（审核低1，2026-10-10）：`_write_vocab_to_rules` 不校验传入值类型 ——
            #   实测传 `['竹木', {'坏':1}, None, 3]` 会**原样落盘**（破坏"字符串列表"契约）。
            #   三条真实调用路径当前都做了 `str()`（所以不可达），但契约要写死在这里：
            #   **统一 `str().strip()` + 丢空串 + 去重（保序）**。
            _clean = {n: list(dict.fromkeys(
                s for s in (str(v).strip() for v in vs) if s)) for n, vs in adopted.items()}
            # ★ 2026-10-27: `drop_by_attr` = 被归并掉的原值（原值→别的归入）。
            #   并集写入必须把这批值**删掉**，否则词表永远只增不减：
            #   实测「小型除湿机」既在归并映射里→除湿机，又留在 params.values 里。
            _drop = {n: set(d or ()) for n, d in (drop_by_attr or {}).items()}
            for a in (rules.get("attributes") or []):
                name = str(a.get("name") or "")
                if name not in _clean:
                    continue
                params = a.get("params") if isinstance(a.get("params"), dict) else None
                if params is not None:
                    # ★★ **并集写入**（2026-10-10 审核修复）：原写法 `if not params.get("values")`
                    #   只在"受控值为空"时写 → 已有类目的新值**永远写不进去**。
                    #   改成**只增不减**（与 `_append_vocab_values` 同语义）：
                    #     · 已有值**保留不动** → 人工确认过的词表不会被 LLM 悄悄改掉
                    #     · 新值**追加** → 新原值/新属性才收敛得进来
                    #   合并/清理交给**停② 的人工确认**（Web 面板），不做自动删除。
                    cur = [str(x) for x in (params.get("values") or [])]
                    _d = _drop.get(name) or set()
                    if _d:
                        cur = [v for v in cur if v not in _d]   # ★ 去掉被归并掉的原值
                    add = [v for v in _clean[name] if v not in cur]
                    if add or not cur or _d:
                        params["values"] = cur + add
                elif "values" in a:
                    cur = [str(x) for x in (a.get("values") or [])]
                    _d = _drop.get(name) or set()
                    if _d:
                        cur = [v for v in cur if v not in _d]
                    add = [v for v in _clean[name] if v not in cur]
                    if add or not cur or _d:
                        a["values"] = cur + add
            # ★ 2026-10-14(B1): 同上 —— 定义层文件改原子写
            write_json_atomic(path, rules)
            self.log(ctx, "已写回规则词表: %s" % path)
        except Exception as exc:  # noqa: BLE001
            self.log(ctx, "⚠ 词表写回 rules.json 失败(忽略, 本次运行内存内仍生效): %s" % exc)

    def _write_numeric_declaration(self, cat_dir, cat_id, decls: dict, ctx) -> None:
        """把**数值型声明**写回 rules.json（改动清单 0-8/0-9）。

        `decls`：`{属性: {"unit":…, "quantity":…, "unit_factors":{…}, "subcols":[…]}}`
        写 `rule="numeric"` + `params.unit` + `params.unit_factors` + `params.subcols`，
        并**清掉 `params.values`**（数值型不参与受控值命中统计）。

        为什么必须单独写这个：数值型属性若只生成了"受控值词表"，就会变成
        `['55L','53L','108L','62L','50L']` 这种**原值枚举** —— 换个型号（如 `1.3L`）
        立刻变成"词表外"，永远归不了。`rule_numeric` + 按物理量的 `unit_factors`
        才能把 `1.3L`/`55 liters`/`1300ml` 收敛到同一个数值。
        """
        if not cat_dir or not cat_id or not decls:
            return
        path = os.path.join(cat_dir, cat_id + ".rules.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8-sig") as fh:
                doc = json.load(fh)
            for a in (doc.get("attributes") or []):
                name = str(a.get("name") or "")
                d = decls.get(name)
                if not d:
                    continue
                a["rule"] = "numeric"
                params = a.get("params") if isinstance(a.get("params"), dict) else {}
                params.pop("values", None)
                params["unit"] = d["unit"]
                from tools.attr_stability.units import quantity_of_unit
                params["quantity"] = d.get("quantity") or quantity_of_unit(d["unit"])
                params["_物理量"] = params["quantity"]
                params["unit_factors"] = d["unit_factors"]
                existing_cols = a.get("subcols") or params.get("subcols")
                numeric_cols = list(existing_cols or d["subcols"])
                if len(numeric_cols) == 1 and len(d["subcols"]) > 1:
                    numeric_cols += d["subcols"][1:]
                a["subcols"] = params["subcols"] = numeric_cols
                if d.get("bands"):
                    params["bands"] = d["bands"]
                a["params"] = params
            # ★ 2026-10-14(B1): 同上 —— 定义层文件改原子写
            write_json_atomic(path, doc)
            self.log(ctx, "已写回数值型声明(%d 个属性): %s" % (len(decls), path))
        except Exception as exc:  # noqa: BLE001
            self.log(ctx, "⚠ 数值型声明写回失败(忽略, 本次运行内存内仍生效): %s" % exc)

    def _review_vocab(self, ctx: StepContext, out_dir: str, res,
                      cat_dir: str | None = None, cat_id: str | None = None) -> None:
        """新词候选(vocab_candidates) → 独立审校员 AI 初审。

        ATTR_STAB_AUTO_ONBOARD 默认 false(半自动): 只写 `新词初审裁决.json` 交人工审阅后 --apply;
        置 true(全自动): AI 初审后**自动把「采纳」写回 rules.json**, 并留 `新词自动采纳记录.json` 供回溯。
        """
        cands = res.vocab_candidates or []
        if not cands:
            return
        try:
            from tools.attr_stability.onboard import apply_vocab_acceptance, build_vocab_review_prompt
        except ImportError:
            return
        prompt = build_vocab_review_prompt(cands, self._cat)
        try:
            raw = ctx.llm().chat_json(prompt, temperature=0.1, max_tokens=8000)
        except Exception as exc:  # noqa: BLE001
            self.log(ctx, "新词初审调用失败(忽略, 可手动 onboard_cli --prompt): %s" % exc)
            return
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            self.log(ctx, "新词初审返回格式异常, 跳过(可手动 onboard_cli --prompt)")
            return
        n_adopt = sum(1 for i in items if isinstance(i, dict)
                      and str(i.get("结论") or "") == "采纳")
        auto = self._auto_onboard(ctx)
        path = os.path.join(out_dir, "新词初审裁决.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"cat": self._cat,
                       "status": "auto_applied" if auto else "pending_review",
                       "候选数": len(cands), "items": items}, fh, ensure_ascii=False, indent=2)
        # ★ 词表外新词 → 并入「待确认清单」（人工一眼看完，用户口径 2026-10-08）
        try:
            self._write_pending_review(ctx, out_dir, cands=cands)
        except Exception as exc:  # noqa: BLE001 — 清单失败不影响交付
            self.log(ctx, "⚠ 待确认清单(新词)写入失败(忽略): %s" % exc)
        if not auto:
            self.log(ctx, "新词初审完成: %d 个候选 → AI 建议采纳 %d 个, 已写 %s"
                     "(人工审阅后执行: python tools/attr_stability/onboard_cli.py "
                     "--rules <rules.json> --apply %s)" % (len(cands), n_adopt, path, path))
            return
        # 全自动: 直接把 AI 采纳的词写回 rules.json
        rules_path = os.path.join(cat_dir, cat_id + ".rules.json") if (cat_dir and cat_id) else ""
        if not rules_path or not os.path.exists(rules_path):
            self.log(ctx, "自动写回跳过: 找不到 rules.json(仍留有裁决 %s 可手动 --apply)" % path)
            return
        out = apply_vocab_acceptance(rules_path, items)
        with open(os.path.join(out_dir, "新词自动采纳记录.json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        self.log(ctx, "新词自动采纳完成: %d 候选 → AI 采纳并写回 %d 个"
                 "(新增 %d / 重复 %d / 驳回 %d / 未匹配 %d)"
                 % (len(cands), n_adopt, len(out.get("已新增") or []),
                    len(out.get("跳过重复") or []), len(out.get("驳回") or []),
                    len(out.get("未匹配属性") or [])))

    def _write_raw(self, ctx: StepContext, out_dir: str, rows, res, vision: dict,
                   *, xlsx_name: str | None = None, write_stats: bool = True) -> None:
        """写 单遍原始 xlsx(原始 标准 值 + 视觉补充) + 唯一值统计.json(供步骤18 标准化)。

        `xlsx_name` / `write_stats`：剔除前的**全量表**只写 xlsx、不覆盖唯一值统计。
        """
        try:
            import openpyxl
        except ImportError:
            openpyxl = None
            self.log(ctx, "⚠ 未安装 openpyxl, 跳过 单遍原始 xlsx")
        row_by = {int(r.get("xl_row")) if str(r.get("xl_row") or "").isdigit() else i: r
                  for i, r in enumerate(rows, start=2)}
        deliv = {int(d.get("xl_row")): (d.get("attributes") or {})
                 for d in (res.deliverable or []) if d.get("xl_row") is not None}
        # 新 schema: attributes 平铺为「值键 + 来源键 + 依据键」, 列名只取值键(属性名)。
        # ★ 2026-10-14 修: 原来只过滤 `_来源` —— `_write_deliverables`(步骤18 交付表)两个后缀
        #   都过滤, 这里却漏了 `_依据`。冷启动真跑实测: 单遍原始.xlsx 176 行 × **145 列**,
        #   其中 33 个 `xxx_依据` 内部字段被当成交付列写出去(交付物表头契约被破坏)。
        attr_names = _deliverable_attr_names(res.deliverable)
        input_cols: list[str] = []
        for r in rows:
            for k in r:
                if k != "xl_row" and k not in input_cols:
                    input_cols.append(k)
        id_cols = ["父ASIN", "变体ASIN", "标题"]
        ordered = [c for c in id_cols if c in input_cols] + [c for c in input_cols if c not in id_cols]
        header = ordered + attr_names

        def raw_value(attrs, attr):
            # 新 schema: 值键=属性名, 直接是标量
            v = attrs.get(attr)
            return "" if v is None else str(v)

        if openpyxl is not None:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "属性标注"
            ws.append(header)
            for xl in sorted(set(row_by) | set(deliv)):
                r = row_by.get(xl, {})
                attrs = deliv.get(xl, {})
                ws.append([r.get(c, "") for c in ordered]
                          + [vision.get((xl, a), raw_value(attrs, a)) for a in attr_names])
            # ★ 2026-10-29 修 BUG: `_save_workbook_atomic` 原先**写在 for 循环里面**，
            #   于是每加一行就把整本工作簿「保存 + 回读校验」一次 = 463 次全量写盘。
            #   实测 Salt and Pepper（36 属性 / 69MB 交付）每次约 12 秒 → 整步要多花 ~90 分钟，
            #   日志不输出，表现为「步骤17 卡住」。移出循环后只保存一次。
            _save_workbook_atomic(wb, os.path.join(
                out_dir, xlsx_name or RAW_XLSX_TPL.format(cat=self._cat)))

        if not write_stats:
            self.log(ctx, "已写剔除前全量表: %s"
                     % (xlsx_name or RAW_XLSX_TPL.format(cat=self._cat)))
            return
        # 唯一值统计(供步骤18 词表生成/调粒度)
        from tools.attr_stability.unknowns import is_unknown
        stats: dict[str, dict[str, int]] = {}
        for key, d in (res.cells or {}).items():
            if not isinstance(d, dict) or d.get("kind") != "value":
                continue
            _, _, path = key.partition("|")
            if not path.startswith("attr."):
                continue
            attr = path[len("attr."):]
            counter = stats.setdefault(attr, {})
            for g in (d.get("values") or []):
                v = str((g or {}).get("value") or "").strip()
                # ★★ 修复（审核低危7）：原本地硬编码白名单 `("未明确","","None","不适用","未知")`
                #   **漏了 `未标注`**（`unknowns.py` 注释自己记着：`未标注` 曾因此被写进
                #   `dog_dryer.rules.json` 当合法受控值、覆盖率虚高到 56.3%）。改用权威 `is_unknown`。
                if v and not is_unknown(v):
                    counter[v] = counter.get(v, 0) + 1
        out_stats = {a: [{"值": v, "次数": n} for v, n in sorted(c.items(), key=lambda x: -x[1])]
                     for a, c in stats.items() if c}
        with open(os.path.join(out_dir, UNIQUE_STATS_NAME), "w", encoding="utf-8") as fh:
            json.dump({"cat": self._cat, "属性唯一值": out_stats}, fh, ensure_ascii=False, indent=2)
        self.log(ctx, "已写 单遍原始 xlsx + 唯一值统计.json(%d 属性有取值)" % len(out_stats))

    def _annotation_cache_context(self, ctx: StepContext) -> dict:
        """只记录影响判断的定义与模型配置，绝不记录密钥。"""
        rules_path = self._rules_json_path(ctx)
        rules = {}
        if rules_path:
            with open(rules_path, encoding="utf-8-sig") as fh:
                rules = json.load(fh)
            rules.pop(VOCAB_STATUS_KEY, None)  # 确认动作本身不改变标注口径
        return {
            "rules": rules, "spec": self._spec_attributes,
            "require_basis": str(ctx.config.get("ATTR_STAB_REQUIRE_BASIS", "true")),
            "model": str(ctx.config.get("LLM_MODEL", "")),
            "base_url": str(ctx.config.get("LLM_BASE_URL", "")),
            "vision_model": str(ctx.config.get("VISION_MODEL", "")),
            "vision_base_url": str(ctx.config.get("VISION_BASE_URL", "")),
        }

    @staticmethod
    def _rows_digest(rows) -> str:
        import hashlib
        payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _load_rows(self, ctx: StepContext, out_dir: str):
        """auto 核对上游摘要后续跑；snapshot 显式复放原快照。"""
        import hashlib
        import openpyxl

        mode = str(ctx.config.get("ATTR_STAB_INPUT_MODE", "auto")).strip().lower()
        if mode not in ("auto", "snapshot"):
            raise StepDataError("ATTR_STAB_INPUT_MODE 必须是 auto 或 snapshot")
        snap = os.path.join(out_dir, "input.json")
        if mode == "snapshot":
            if not os.path.isfile(snap):
                raise StepDataError("snapshot 模式缺少 input.json")
            self.log(ctx, "输入模式 snapshot: 显式复放原快照")
            return load_rows(snap)
        src = ctx.resolve_artifact(self._upstream_name())
        digest = hashlib.sha256()
        with open(src, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        source = {"path": os.path.abspath(src), "sha256": digest.hexdigest(),
                  "sheet": UPSTREAM_SPLIT_SHEET}
        if os.path.isfile(snap):
            try:
                with open(snap, encoding="utf-8-sig") as fh:
                    saved = json.load(fh)
                cached = saved.get("rows") if isinstance(saved, dict) else None
                if cached and saved.get("source") == source and (
                        saved.get("rows_sha256") == self._rows_digest(cached)):
                    self.log(ctx, "上游与输入快照摘要一致，复用 input.json")
                    return cached
            except (OSError, ValueError, TypeError):
                pass
            self.log(ctx, "上游已更新或快照无可信摘要，重新生成 input.json")
        wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
        try:
            if UPSTREAM_SPLIT_SHEET not in wb.sheetnames:
                raise StepDataError("上游缺少工作表 %s，拒绝读取其他工作表" % UPSTREAM_SPLIT_SHEET)
            it = wb[UPSTREAM_SPLIT_SHEET].iter_rows(values_only=True)
            hdr = [str(h or "").strip() for h in next(it, ())]
            if not hdr or len(hdr) != len(set(hdr)) or "xl_row" in hdr:
                raise StepDataError("上游表头为空、重复或包含保留列 xl_row")
            # ★ 2026-10-14(B3): 标注**必需列**缺失 → 硬失败。
            #   原来只查上面三项, 于是上游把 `图片链接`/`五点描述` 改名或漏列时,
            #   N 遍会把该属性整列填成「未明确」, 而调用方看到的是"成功"。
            _missing = [c for c in REQUIRED_INPUT_FIELDS if c not in hdr]
            if _missing:
                raise StepDataError(
                    "上游缺少标注必需列: %s（当前表头 %d 列）→ 拒绝继续: "
                    "否则这些维度会整列填『未明确』却报成功。请检查步骤14 的拆分表列名。"
                    % ("、".join(_missing), len(hdr)))
            rows = [{"xl_row": i,
                     **{hdr[j]: ("" if v is None else str(v))
                        for j, v in enumerate(r) if j < len(hdr)}}
                    for i, r in enumerate(it, start=2) if any(v is not None for v in r)]
        finally:
            wb.close()
        if "size" not in hdr:
            self.log(ctx, "⚠ 上游表无 size 列，跳过 size 原文交叉校验")
        os.makedirs(out_dir, exist_ok=True)
        tmp = snap + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"rows": rows, "source": source, "rows_sha256": self._rows_digest(rows)},
                      fh, ensure_ascii=False, indent=2)
        os.replace(tmp, snap)
        return rows

    def _spec_attr_names(self) -> list[str]:
        names = [str(a.get("name") or "").strip()
                 for a in (self._spec_attributes or []) if str(a.get("name") or "").strip()]
        return names

    def _build_attr_hint_block(self, ctx: StepContext, rules=None) -> str:
        """把步骤16 的属性说明 + **受控值词表**注入标注提示词(提升取值准确性)。

        优先用精简版 `hint_short`(步骤16 已总结), 无则兜底用完整 `hint`。

        ★ 2026-10-07 新增**受控值注入**（改动清单 §15 根因 1）：
        原实现只给 `hint_short`、**不给 `params.values`**，于是模型看不到词表，
        只能自由发挥 → 实测 `尺寸规格` 产出 1595 个自由串、`容量` 覆盖率仅 0.10
        （词表是 `['55L','53L',…]` 而模型答 `1.3L`/`55 liters` 等写法）。
        这与视觉提示词、step18 归类载荷是**同一个病根**：受控值词表没有贯穿到
        每个"问模型"的环节。这里补上（哨兵会被滤掉 —— 它们不是可选项）。

        两者都无的属性只列名字(词表约束靠 rules.json)。返回整段 markdown; 无属性返回 ""。
        """
        if not self._spec_attributes:
            return ""
        values_by_attr: dict[str, list[str]] = {}
        if rules is not None:
            try:
                from tools.attr_stability.unknowns import filter_unknown_words
                for n, s in (getattr(rules, "attrs", {}) or {}).items():
                    kept, _drop = filter_unknown_words(list(getattr(s, "values", []) or []))
                    if kept:
                        values_by_attr[n] = kept
            except Exception:  # noqa: BLE001
                values_by_attr = {}
        lines = []
        for a in self._spec_attributes:
            name = str(a.get("name") or "").strip()
            if not name:
                continue
            desc = str((a.get("hint") if a.get("use_full_hint") else a.get("hint_short"))
                       or a.get("hint") or a.get("hint_short") or "").strip()
            line = ("- %s：%s" % (name, desc)) if desc else ("- %s" % name)
            vals = values_by_attr.get(name) or []
            if vals:
                line += "\n    **受控值（取值必须逐字符来自这里）**：%s" % " ｜ ".join(vals)
            lines.append(line)
        if not lines:
            return ""
        head = (
            "## 本类目属性定义（判定关键词 / 典型取值 / 易混区分）\n"
            "典型取值只是示例；按原文证据和属性规则判断，不为匹配示例而猜测。"
            "受控值按规则使用；证据明确但无法准确归入时保留真实值及依据，交由词表校验和候选机制处理。"
            "各属性按自身对象、部件和口径判断，取值必须与依据自洽。\n"
        )
        return head + "\n".join(lines)

    @staticmethod
    def _row_image_urls(row: dict, img_col: str) -> list[str]:
        """取该行的商品图片 URL 列表（图廊在前，A+ 在另一列）。

        ★ 2026-10-07 修复（改动清单 §19）：**分隔符要兼顾 `,` 与 `;`**。
        上游 `step10_variants.py` 产出时用 `;` 拼接，而 `step14_traffic_split.py`
        统一成 **`, ` 拼接**（其日志明写「全图逗号拼接」）。本函数原先只按 `[|;\\n]` 切，
        于是拿到逗号串时**整串被当成 1 个 URL** → 图片根本没传进视觉 API
        （实测：Dog Dryer 176 行全部只解析出 1 张；`in_miss` 每调用仅 713 tokens 无图）。
        代价实测：修复后每行 7.12 张、视觉值活到交付 0.07% → **97.5%**，视觉花费 ×1.85。

        `startswith("http")` 守卫：逗号切分后若把 Amazon 尺寸参数（其字符类本身含 `,`，
        如 `._AC_SR320,320_.`）切坏，最多丢弃那张图，**不会把残渣传给 API**。
        （实测 2818 行 / 20652 个 URL 未出现 URL 内部逗号，属预防性守卫。）
        """
        raw = str(row.get(img_col) or "").strip().strip('"\'')
        urls = [u.strip().strip('"\'') for u in re.split(r"[|;,\n]+", raw) if u.strip()]
        # 去重 + 去 Amazon 缩略图尺寸参数(._XX_.) + 丢掉切坏的片段
        cleaned: list[str] = []
        seen: set[str] = set()
        for u in urls:
            if not u.startswith("http"):
                continue
            u = re.sub(r"\._[A-Za-z0-9,]+(?:_[A-Za-z0-9,]+)*_\.", ".", u)
            if u and u not in seen:
                seen.add(u)
                cleaned.append(u)
        return cleaned

    # 需要高清读图(读小字)的属性名线索: OCR 文字 / 雕刻文字 / 认证标记 / logo 等
    _TEXT_READING_HINTS = ("ocr", "文字", "text", "雕刻", "镌刻", "铭", "logo",
                           "认证", "标签", "印刷", "商标")

    @classmethod
    def _is_text_reading_attr(cls, name: str) -> bool:
        s = str(name or "").strip().lower()
        return any(h in s for h in cls._TEXT_READING_HINTS)

    @staticmethod
    def _classify_prompt_text(payload: dict) -> str:
        return ("你是亚马逊类目属性审校员。下面是需要做「语义归类」的词表外原值。\n"
                "任务与硬性要求见下, 目标值必须逐字符来自受控值词表, 或形如「新增: X」。\n\n"
                "【**最重要的一条：不许硬塞**】\n"
                "如果受控值词表里**没有任何一个值能正确表达这个原值**，"
                "**必须**写 `新增: X`（X 是你建议的新受控值）。\n"
                "❌ **绝对不要**为了「凑一个已有值」而选一个**语义不符**的值。\n"
                "   实测教训：某商品标题是 `Titanium Cutting Board`（纯钛），"
                "而 `产品类型` 词表里只有 `不锈钢砧板` 这一个金属类，"
                "模型就把它归成了 `不锈钢砧板` —— 这是**错的**，"
                "正确做法是 `新增: 钛金属砧板`。\n"
                "判断标准：问自己「如果我把这个受控值填进表格，业务方会不会认为填错了？」"
                "会 → 就写 `新增: X`。\n"
                "宁可报「词表缺口」让人工补，也不要产出**语义错误**的值。\n\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)
                + "\n\n严格输出 JSON: {\"归类\": {属性: {原值: 受控值或\"新增: X\"}}}")

    def _converge_standardize(self, ctx: StepContext, rules, res, rows=None) -> dict:
        """归并标准化: 机械收敛(受控词表) + 大模型语义归类(词表外值) + 数值异常值校验。

        `rows`：步骤17 的 input 行（用于数值异常的"原文证据"校验）。可选，
        缺省时跳过证据类校验（其余三类判据仍生效）。

        返回 {(xl_row, attr): 标准化后的值}; 失败/无产出时返回空(交付表回落到机械归一)。
        """
        try:
            from tools.attr_stability import attr_spec_from_modern
            from tools.attr_stability.catrules import load_category
            from tools.attr_stability.converge import (
                apply_classification, build_classify_payload, converge_mechanical,
                convergence_report,
            )
        except ImportError:
            return {}
        declared = {n: s.values for n, s in rules.attrs.items()}
        _r_dir, _r_id = self._resolve_rules(ctx)
        attrs_spec: dict = {}
        try:
            cf = load_category(_r_dir, _r_id) if _r_dir else None
            from tools.attr_stability.rule_pipeline import rule_configs
            for at in (rule_configs(cf.main, cells=res.cells) if cf else []):
                if not isinstance(at, dict) or not at.get("name"):
                    continue
                params = at.get("params") if isinstance(at.get("params"), dict) else {}
                spec = attr_spec_from_modern(at) if ("rule" in at or "params" in at) else None
                attrs_spec[str(at["name"])] = {
                    **params,
                    "rule": at.get("rule") or "enum",
                    "_rule_config": at,
                    "type": spec.type if spec else str(at.get("type") or "enum"),
                    "value_shape": spec.shape if spec else str(at.get("value_shape") or "single"),
                    "value_order": params.get("value_order") or params.get("values")
                                   or at.get("value_order"),
                    "value_separators": params.get("value_separators") or at.get("value_separators"),
                    # join_sep：多值重组时的连接符。材质类要用 `+`（`不锈钢+亚克力`），
                    # 其余用 `、`。原实现没把这一项传下去，于是材质也会被拼成 `不锈钢、亚克力`，
                    # 与本类目既有口径（rules.json 里材质一律 `+`）不一致。
                    "join_sep": params.get("join_sep") or at.get("join_sep"),
                    "unit": params.get("unit") or at.get("unit"),
                    # ★ 2026-10-07 修复（改动清单 §13 方案①）：
                    # `build_classify_payload` 读的是 `spec['values']`，而这里原先**没传**，
                    # 于是归类载荷里 25 个属性的「受控值词表」**全是空的** → 模型无法归类
                    # （只能答"新增: X"，而"新增"按设计不落地）→ 所有词表外的值全部掉落，
                    # 视觉补充 5947 格里只有 4 格活到交付。这里把词表补上。
                    "values": list(declared.get(str(at["name"])) or []),
                    # 数值类属性的异常值校验要用（改动清单 §17）
                    "bands": params.get("bands"),
                    "_物理量": params.get("_物理量"),
                    # ★ 2026-10-08（改动清单 §21）：确定性推导与分量收敛的声明。
                    #   不传下去的话 `apply_derive_and_converge` 会**静默什么都不做**
                    #   （声明写在 rules.json 里、代码却读不到）。本项目已两次踩同一个坑：
                    #   §13 的 `values` 漏传导致归类载荷词表全空，§18 的 bands 同理。
                    "derive_from": params.get("derive_from"),
                    "converge": params.get("converge"),
                }
        except Exception as exc:  # noqa: BLE001
            self.log(ctx, "⚠ 读取类目属性类型失败(按 enum/single 收敛): %s" % exc)

        # ★ 2026-10-08（§21）：上面 `at` 来自 `load_category()`（**类目配置**），
        #   它**不带** rules.json 的 `params.derive_from` / `params.converge` / `params.bands`
        #   → 声明写了也读不到，确定性推导会**静默空转**（实测：交付表里材质/厚度全是旧值）。
        #   这里从 **rules.json 原文**把这三项覆盖进去，保证「声明」与「代码」真正接通。
        #   本项目的同类坑已出现三次（§13 values 漏传、§18 bands、本次 converge），
        #   根因都是「声明在 A 文件、消费在 B 文件，中间没人核对」。
        if _r_dir and _r_id:
            try:
                _rp = os.path.join(_r_dir, _r_id + ".rules.json")
                if os.path.exists(_rp):
                    with open(_rp, encoding="utf-8-sig") as _fh:
                        _raw_rules = json.load(_fh)
                    _merged = []
                    for _a in (_raw_rules.get("attributes") or []):
                        _n = str(_a.get("name") or "")
                        _p = _a.get("params") if isinstance(_a.get("params"), dict) else {}
                        if _n not in attrs_spec:
                            continue
                        for _k in ("derive_from", "converge", "bands"):
                            if _p.get(_k):
                                attrs_spec[_n][_k] = _p[_k]
                                _merged.append("%s.%s" % (_n, _k))
                    if _merged:
                        self.log(ctx, "已接通 rules.json 声明: %s" % "、".join(_merged))
            except (OSError, ValueError) as _exc:
                self.log(ctx, "⚠ 读取 rules.json 声明失败(推导/收敛将不生效): %s" % _exc)

        results = converge_mechanical(res.cells, declared, attrs_spec=attrs_spec)

        # 大模型语义归类(词表外/语义分歧)
        classify = build_classify_payload(results, attrs_spec)
        _skipped = classify.get("_跳过(词表为空)") or []
        if _skipped:
            # ② 空词表守卫：不静默 —— 这些属性的词表外值**不会被归类**，
            #    交付表里会回落成原值/未明确。多数情况意味着 attrs_spec 漏了 values
            #    或该类目确实还没有受控词表。
            self.log(ctx, "⚠ 语义归类跳过 %d 个属性(受控值词表为空，其词表外值不会归类): %s"
                     % (len(_skipped),
                        json.dumps(_skipped, ensure_ascii=False)[:400]))
        mapping: dict = {}
        if [t for t in classify.get("待归类", []) if t.get("待归类原值")]:
            try:
                raw = ctx.llm().chat_json(self._classify_prompt_text(classify),
                                          temperature=0.2, max_tokens=8000)
                mapping = raw.get("归类") if isinstance(raw, dict) else raw
                if not isinstance(mapping, dict):
                    mapping = {}
            except Exception as exc:  # noqa: BLE001
                self.log(ctx, "语义归类调用失败(忽略, 待归类格保留原值): %s" % exc)
        applied = {}
        try:
            applied = apply_classification(results, mapping or {}, declared)
            mapping = applied.get("合法映射") or {}
        except Exception as exc:  # noqa: BLE001
            self.log(ctx, "语义归类结果校验异常(忽略): %s" % exc)
            mapping = {}

        # 落回: 主值优先; 待归类且大模型给了受控值的用大模型结果; 其余留空(交付表回落)
        #
        # ★ 2026-10-07 修复【Bug B，改动清单 §14】键类型必须与写入端一致：
        #   `ConvergenceResult.cells` 的键来自 `"<行号>|attr.<属性>".partition("|")`，
        #   所以 `cell['行号']` 是**字符串**（`'2'`）；而 `_write_deliverables` 用
        #   `std_map.get((xl, a), …)`，`xl` 是 **int**（`int(d['xl_row'])`）。
        #   两者不匹配 → `std_map` **整体永远查不中** → 交付表实际落回
        #   `res.deliverable`（step17 多数表决原值）经 `normalize_form` 的结果。
        #   实测：Dog Dryer 176 行 × 27 属性 = 4752 格，**100% 等于 deliverable 原值**
        #   → **机械收敛 + 语义归类 + 视觉补充全部空转**（还白花 LLM 钱）。
        #   这里把行号统一成 int（非数字键原样保留）。
        std: dict = {}

        def _rid(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return v

        for attr, cr in results.items():
            for _key, cell in cr.cells.items():        # `_key` 用不到（值取自 cell 内部）
                rid = _rid(cell.get("行号"))
                main = str(cell.get("主值") or "").strip()
                if main:
                    std[(rid, attr)] = main
                    continue
                candidates = cell.get("候选") or []
                raw_val = str(candidates[0]).strip() if candidates else ""
                if not raw_val:
                    continue
                attr_map = (mapping or {}).get(attr, {})
                if not isinstance(attr_map, dict):
                    attr_map = {}
                tgt = str(attr_map.get(raw_val, "") or "").strip()
                if tgt and not tgt.startswith("新增") and tgt not in ("未明确", "", "None"):
                    std[(rid, attr)] = tgt
        # ★ 第二趟收敛（2026-10-09 用户口径）：**组合值靠枚举追不上**。
        #   第一趟里 `不锈钢+小麦秸秆PP` 的成分 `小麦秸秆PP` 未命中 → 整串悬空、
        #   **成分**进了归类（见 converge.py 的 vocab_has_combo 分支）；
        #   LLM 判出 `小麦秸秆PP`→`塑料` 后，把这些归类结果当**同义表**再收敛一趟，
        #   就能重组出 `不锈钢+塑料` —— 而不是在交付表里留一个词表外组合值。
        #   只补第一趟没定下来的格，**不覆盖**已有结果。
        try:
            _al2 = {a: {str(k): str(v) for k, v in (m or {}).items()
                        if isinstance(v, str) and not v.startswith("新增")}
                    for a, m in (mapping or {}).items() if isinstance(m, dict)}
            _al2 = {a: m for a, m in _al2.items() if m}
            if _al2:
                _r2 = converge_mechanical(res.cells, declared, aliases=_al2,
                                          attrs_spec=attrs_spec)
                _n2 = 0
                for _a, _cr2 in (_r2 or {}).items():
                    for _key2, _c2 in (_cr2.cells or {}).items():
                        _rid2 = _rid(_c2.get("行号"))
                        _mv2 = str(_c2.get("主值") or "").strip()
                        if _mv2 and (_rid2, _a) not in std:
                            std[(_rid2, _a)] = _mv2
                            _n2 += 1
                if _n2:
                    self.log(ctx, "第二趟收敛(用归类结果当同义表): 补定 %d 格" % _n2)
        except Exception as exc:  # noqa: BLE001 — 第二趟失败不影响第一趟结果
            self.log(ctx, "第二趟收敛失败(忽略): %s" % exc)

        try:
            rep = convergence_report(results)
            self.log(ctx, "归并标准化: 总格 %s · 一致 %s · 已收敛 %s · 待归类 %s · 收敛率 %s"
                     % (rep.get("总格数"),
                        rep.get("一致（提名本就同一个受控值）"),
                        rep.get("已收敛（多提名→同一受控值）"),
                        rep.get("待归类（词表外或有语义分歧）"),
                        rep.get("收敛率")))
        except Exception:  # noqa: BLE001
            pass
        if applied.get("新增建议"):
            self.log(ctx, "新词建议(登记待人工审定, 不直接进词表): %s"
                     % json.dumps(applied["新增建议"], ensure_ascii=False)[:400])
        if applied.get("非法输出"):
            self.log(ctx, "⚠ 语义归类含非法输出(已丢弃): %s"
                     % json.dumps(applied["非法输出"], ensure_ascii=False)[:400])

        # ============ 数值类属性**异常值校验 + 修正**（改动清单 §17） ============
        # `rule_numeric` 只做"数值+单位换算+分档"，**不判断结果是否可信**。实测三类真异常
        # （Dog Dryer 标准化交付表）：`容量=55` 5 行 / `线长=2` 10 行 / `最大风速=72` 18 行
        # 在原文里**找不到任何对应数值** → 缺乏依据（多是上游把枚举成员判错了）。
        # 这里做四类校验；**能确定性修的当场修**，不能确定的**不许留假数** + 出重跑指令。
        try:
            from tools.attr_stability.num_check import (
                apply_fixes, build_retry_items, check_cells,
            )
            rows_by_id: dict = {}
            for i, r in enumerate(rows or [], start=2):
                xl = r.get("xl_row")
                try:
                    rows_by_id[int(xl) if xl is not None else i] = r
                except (TypeError, ValueError):
                    rows_by_id[i] = r
            numeric_attrs = [a for a, s in attrs_spec.items()
                             if str(s.get("type") or "").lower() == "numeric"]
            if numeric_attrs:
                nrep = check_cells(std, attrs_spec, rows_by_id=rows_by_id or None,
                                   numeric_attrs=numeric_attrs,
                                   with_evidence=bool(rows_by_id))
                std, nfix = apply_fixes(std, nrep)
                st = nrep.get("统计") or {}
                self.log(ctx, "数值异常校验: 数值属性 %d 个 · 问题 %d 处 %s · 已修 %d 处"
                         % (st.get("数值属性数", 0), st.get("问题总数", 0),
                            st.get("按类型", {}), len(nfix)))
                if nrep.get("可修"):
                    for a in nfix[:20]:
                        self.log(ctx, "   修正 行%s %s: %r → %r (%s)"
                                 % (a.get("行"), a.get("属性"), a.get("原值"),
                                    a.get("新值"), a.get("动作")))
                brief = build_retry_items(nrep)
                if brief:
                    bp = os.path.join(ctx.resolve_artifact("18_标准化"),
                                      "%s-数值异常重跑指令.md" % self._cat)
                    with open(bp, "w", encoding="utf-8") as fh:
                        fh.write(brief)
                    self.log(ctx, "已写数值异常重跑指令(%d 格): %s"
                             % (len(nrep.get("待重跑") or []), bp))
                # 落盘报告（供审计）
                rp2 = os.path.join(ctx.resolve_artifact("18_标准化"),
                                   "%s-数值异常校验.json" % self._cat)
                with open(rp2, "w", encoding="utf-8") as fh:
                    json.dump({"统计": st,
                               "属性": {a: (v.get("统计") or {})
                                        for a, v in (nrep.get("属性") or {}).items()},
                               "已修": nfix,
                               "待重跑": (nrep.get("待重跑") or [])[:500]},
                              fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001 — 校验失败不影响标准化主流程
            self.log(ctx, "⚠ 数值异常校验失败(忽略, 不影响交付): %s" % exc)

        # ============ 确定性推导 + 分量收敛（改动清单 §21，2026-10-08）============
        # 两个实测问题，都**不调用 LLM**、纯确定性解决：
        #   ① `厚度` 与 `尺寸_拆列.厚` 矛盾（148 行里 147 行不一致），且旧分档
        #      3-10mm / 15-20mm 无归属（70 个值里 22 个落洞）→ 由精确值确定性分档；
        #   ② `主体材质` 20 个取值太碎 → 按**分量**收敛（组合值保留，同族退化）。
        # 声明写在 rules.json 的 `params.derive_from` / `params.bands` / `params.converge`。
        try:
            from tools.attr_stability.derive import apply_derive_and_converge
            # `cells` 必须传：`std` 里**没有** `尺寸_拆列.*` 这类过程字段
            # （`converge_mechanical` 只处理规格书属性），推导的取值来源只能从 cells 读。
            std, dst = apply_derive_and_converge(std, attrs_spec, cells=res.cells)
            if dst.get("推导") or dst.get("收敛") or dst.get("推导跳过(无档可归)"):
                self.log(ctx, "确定性推导/收敛: 推导改档 %d 格 · 分量收敛 %d 格 · "
                              "来源为空跳过 %d · 无档可归 %d"
                         % (dst.get("推导", 0), dst.get("收敛", 0),
                            dst.get("推导跳过(来源空)", 0), dst.get("推导跳过(无档可归)", 0)))
        except Exception as exc:  # noqa: BLE001 — 失败不影响交付
            self.log(ctx, "⚠ 确定性推导/收敛失败(忽略): %s" % exc)

        # ============ 学习映射：**本轮新学到的 原值→受控值 沉淀下来，供下次复用** ============
        # 用户口径（2026-10-08）：枚举映射表**可以存在**，但必须由标准化**自动生成**、
        # 供下次同类目运行使用 —— 不是人手写。所以：
        #   · 本轮：LLM 归类的结果（`mapping`）→ 合并进 `<cat>.learned-map.json`
        #   · 下次：`apply_learned()` 先确定性套用，**这些值不再调 LLM**
        # 只沉淀「目标值在当前受控值词表内」的映射 —— 否则会把偶发的"新增: X"
        # 或已淘汰的值学进表，下次照抄即产出非法值（旧 `<cat>.dict.json` 的
        # `mapping` 正是因为这个原因被废弃，不能重蹈）。
        try:
            from tools.attr_stability.learned_map import (
                apply_learned, load_learned, merge_learned, save_learned,
            )
            _allowed = {n: list(s.get("values") or []) for n, s in attrs_spec.items()}
            # ★ 由**确定性推导**决定的属性（有 `derive_from`）跳过学习映射：
            #   否则学习表里 LLM 自己判的档会在推导之后覆盖回来
            #   （实测：厚度推导对了 57 行，又被学习表的旧档覆盖，一致性卡在 57/70）。
            _derived = {n for n, s in attrs_spec.items() if s.get("derive_from")}
            if _r_dir and _r_id:
                _lp = os.path.join(_r_dir, _r_id + ".learned-map.json")
                _old = load_learned(_r_dir, _r_id)
                _learned = {a: m for a, m in (_old or {}).items() if a not in _derived}
                # ① 先套用已学到的（确定性，省 LLM）
                std, _hit = apply_learned(std, _learned, cells=res.cells, allowed=_allowed)
                if _hit:
                    self.log(ctx, "学习映射命中 %d 格（复用上次沉淀的归类，不再调 LLM）" % _hit)
                # ② 沉淀本轮 LLM 归类结果
                _new = {}
                for _a, _m in (mapping or {}).items():
                    if isinstance(_m, dict):
                        _new[str(_a)] = {str(k): str(v) for k, v in _m.items()
                                         if isinstance(v, str) and not v.startswith("新增")}
                _merged = merge_learned(_old, _new, allowed=_allowed)
                if _merged != _old:
                    save_learned(_r_dir, _r_id, _merged, cat=self._cat)
                    _n = sum(len(v) for v in _merged.values())
                    self.log(ctx, "学习映射已更新: %s（累计 %d 条，下次同类目直接复用）"
                             % (os.path.basename(_lp), _n))
        except Exception as exc:  # noqa: BLE001 — 沉淀失败不影响交付
            self.log(ctx, "⚠ 学习映射沉淀失败(忽略): %s" % exc)

        return std

    @staticmethod
    def _vision_fewer_images(ctx, prompt, urls, **kw):
        """带图片数量降级的视觉调用：400 时依次用 全部 → 4 → 2 → 1 张重试。

        某些商品里混着接口不接受的图（实测 Salt and Pepper 27 行稳定 400），
        整单都会被拒；减图后多数还能拿到答案，比直接放弃划算。
        """
        tiers = [urls, urls[:4], urls[:2], urls[:1]]
        last = None
        for idx, tier in enumerate(tiers):
            if not tier:
                break
            try:
                return ctx.llm().vision(prompt, tier, **kw)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "400" not in msg and "Bad Request" not in msg:
                    raise
                last = exc
                if idx == 0:
                    ctx.log.info("视觉请求 400，降级为前 %d 张图重试"
                                 % (len(urls[:4]) if len(urls) > 4 else 1))
        raise last if last is not None else RuntimeError("视觉调用无可用图片")

    def _vision_gapfill(self, ctx: StepContext, res, rows,
                        rules=None) -> dict:
        """图片视觉补充: 仅对全部运行都判「未明确」的格, 用该行图片再问一次(不覆盖已有值)。

        无图片列 / VISION 未配置 / 无待补格 时安全跳过, 返回空 dict。

        `rules`：**必须传** —— 用于把每个属性的受控值注入视觉提示词
        （改动清单 §15 根因 1：不注入会导致模型答 `是`/`否`，与词表 `带X`/`不带X`
        口径不一致，实测 331 格无法归类）。

        并发 + 断点续跑(2026-09-23, 对齐文本标注):
          · 并发数 = ATTR_STAB_WORKERS(默认3, 与文本标注一致);
          · 每完成一行即把结果落盘 `视觉补充.checkpoint.json`, 中断重启读回已完成行, 只跑剩余;
          · 单行失败隔离(不炸整步) → 失败行按 1行/次 串行重试, 最多 3 轮(与缺行补跑一致)。
        """
        try:
            from tools.attr_stability.gapfill import (
                build_vision_prompt, find_gaps, merge_candidates, parse_vision_answer,
            )
        except ImportError:
            return {}
        if not rows:
            return {}
        img_col = None
        keys = [str(k).strip() for k in rows[0]]
        # 精确匹配优先(避免「图片」子串误命中「图片链接」), 其次子串匹配兜底。
        for cand in ("图片URL", "图片链接", "图片", "image_urls", "主图", "image"):
            if cand in keys:
                img_col = cand
                break
        if img_col is None:
            for cand in ("图片URL", "图片链接", "图片", "image_urls", "主图", "image"):
                if any(cand.lower() in k.lower() for k in keys):
                    img_col = cand
                    break
        if not img_col:
            self.log(ctx, "输入无图片列, 跳过视觉补充")
            return {}
        plan = find_gaps(res.cells, self._spec_attr_names())
        if not plan.items:
            return {}
        row_by: dict[int, dict] = {}
        for i, r in enumerate(rows, start=2):
            xl = r.get("xl_row")
            xl = int(xl) if xl is not None else i
            row_by[xl] = r

        # ---- 注入"受控值 + 判据"到视觉提示词（改动清单 §15 根因 1）----
        # 受控值取**权威来源 rules.json**，并滤掉哨兵（哨兵不是可选项，不该被列成候选）。
        _vvalues: dict[str, list[str]] = {}
        if rules is not None:
            try:
                from tools.attr_stability.unknowns import filter_unknown_words
                for _n, _s in (getattr(rules, "attrs", {}) or {}).items():
                    kept, _drop = filter_unknown_words(list(getattr(_s, "values", []) or []))
                    if kept:
                        _vvalues[_n] = kept
            except Exception as _exc:  # noqa: BLE001
                self.log(ctx, "视觉补充: 读取受控值失败(忽略, 提示词将不带词表): %s" % _exc)
        _vnotes: dict = {}
        try:
            from tools.attr_stability.gapfill import load_attr_notes
            _r_dir, _r_id = self._resolve_rules(ctx)
            if _r_dir:
                _vnotes = load_attr_notes(_r_dir, _r_id) or {}
        except Exception:  # noqa: BLE001
            _vnotes = {}
        if _vvalues:
            self.log(ctx, "视觉补充: 已注入 %d 个属性的受控值到提示词"
                     % len(_vvalues))

        # 用户选择低细节视觉补充；细节级别参与断点指纹，避免混用不同口径的结果。
        detail = "low"
        # ---- 断点续跑: 读回已完成的 gap 结果, 只跑剩余行 ----
        out_dir = ctx.resolve_artifact(OUT_DIR)
        ckpt_path = os.path.join(out_dir, "视觉补充.checkpoint.json")
        # 将实际发送的提示词也计入：模板代码变更不能复用旧视觉判断。
        vision_prompts = {}
        for item in plan.items:
            row = row_by.get(item["xl_row"])
            if row is not None:
                urls = self._row_image_urls(row, img_col)
                vision_prompts[str(item["xl_row"])] = build_vision_prompt(
                    row, item["待补属性"], n_images=len(urls),
                    notes=_vnotes, values_by_attr=_vvalues)
        ckpt_key = self._vision_input_key(
            plan, img_col, rows=rows,
            context={**self._annotation_cache_context(ctx), "detail": detail, "notes": _vnotes,
                     "values": _vvalues, "prompts": vision_prompts})
        done: dict[int, dict] = {}
        if os.path.exists(ckpt_path):
            try:
                with open(ckpt_path, encoding="utf-8") as fh:
                    o = json.load(fh)
                if (o.get("input_key") or "") == ckpt_key:
                    for it in (o.get("done") or []):
                        if isinstance(it, dict) and it.get("xl_row") is not None:
                            done[it["xl_row"]] = it
            except (OSError, ValueError):
                done = {}
        if done:
            self.log(ctx, "视觉补充断点续跑: 已读回 %d/%d 行, 只跑剩余"
                     % (len(done), len(plan.items)))

        # "本来就不需要补"的行(无图 / 无行 / 模型确认没的可补) → 只记下、**不重试**
        skipped: set = set()

        def _fill_one(it: dict) -> dict | None:
            """补一行: 成功返回 {xl_row, 追问, 来源}; 无需/无法补 → 记入 skipped 并返回 None。"""
            row = row_by.get(it["xl_row"])
            if not row:
                skipped.add(it["xl_row"])
                return None
            urls = self._row_image_urls(row, img_col)
            if not urls:
                skipped.add(it["xl_row"])     # 无图 → 视觉补不了, 重试无意义
                return None
            prompt = build_vision_prompt(row, it["待补属性"], n_images=len(urls),
                                         notes=_vnotes, values_by_attr=_vvalues)
            try:
                # ★ 2026-10-29: 某些行的某张图会让接口整单 400（实测 27 行）。
                #   遇到 400 就**逐级减少图片**重试，能救回来一部分。
                out = self._vision_fewer_images(
                    ctx, prompt, urls, temperature=0.2, max_tokens=4000,
                    detail=detail, max_images=None)
            except Exception as exc:  # noqa: BLE001 — 单行失败隔离, 不炸整步
                self.log(ctx, "视觉补充失败(行 %s): %s" % (it["xl_row"], exc))
                return None
            # 剥壳并按属性白名单过滤。内容丢失、空对象或空值不能证明图片无可补内容；
            # 它们应进入失败重试。明确返回「未明确」则是有效判断，不应重复请求。
            vals, srcs = parse_vision_answer(out, it["待补属性"])
            if not vals or not any(v is not None and str(v).strip() for v in vals.values()):
                shape = list(out)[:10] if isinstance(out, dict) else type(out).__name__
                self.log(ctx, "视觉补充 行 %s: 返回内容缺失或无法解析，待重试；待补=%s 响应顶层=%s"
                         % (it["xl_row"], it["待补属性"][:5], shape))
                return None
            return {"xl_row": it["xl_row"], "追问": vals, "来源": srcs}

        def _save_ckpt() -> None:
            try:
                os.makedirs(out_dir, exist_ok=True)
                tmp = ckpt_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"input_key": ckpt_key,
                               "done": [done[k] for k in sorted(done)]},
                              fh, ensure_ascii=False, indent=2)
                os.replace(tmp, ckpt_path)
            except OSError:
                pass

        workers = _cfg_num(ctx.config, "ATTR_STAB_WORKERS", 3, int)
        # ---- 主跑: 并发补剩余行(每完成一行写检查点) ----
        todo = [it for it in plan.items if it["xl_row"] not in done]
        if todo:
            n = len(todo)
            use_workers = workers if n > 1 else 1
            if use_workers > 1:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                t0 = __import__("time").time()
                finished = 0
                with ThreadPoolExecutor(max_workers=use_workers) as ex:
                    futs = {ex.submit(_fill_one, it): it for it in todo}
                    for fut in as_completed(futs):
                        it = futs[fut]
                        r = fut.result()
                        finished += 1
                        if r is not None:
                            done[it["xl_row"]] = r
                            _save_ckpt()
                        self.log(ctx, "   视觉补充 行 %d/%d 完成(累计 %d 行, 总耗时 %.0fs)"
                                 % (finished, n, len(done),
                                    __import__("time").time() - t0))
            else:
                for it in todo:
                    r = _fill_one(it)
                    if r is not None:
                        done[it["xl_row"]] = r
                        _save_ckpt()
        # ---- 失败行兜底: 只重试**真正的失败**; 无图/无行/无可补的行已在 skipped 里, 不重试 ----
        if skipped:
            self.log(ctx, "   视觉补充 %d 行无需补(无图/无行/模型确认没的可补), 跳过、不重试"
                     % len(skipped))
        missing = [it for it in plan.items
                   if it["xl_row"] not in done and it["xl_row"] not in skipped]
        round_n = 0
        while missing and round_n < 3:
            round_n += 1
            self.log(ctx, "   视觉补充 %d 行失败, 逐行重试第 %d 轮" % (len(missing), round_n))
            still = []
            for it in missing:
                r = _fill_one(it)
                if r is not None:
                    done[it["xl_row"]] = r
                    _save_ckpt()
                elif it["xl_row"] not in skipped:
                    still.append(it)
            missing = still
        if missing:
            self.log(ctx, "   视觉补充 %d 行重试后仍失败(跳过)" % len(missing))

        gap_results = [done[k] for k in sorted(done)]
        if not gap_results:
            return {}
        # 全部完成 → 清掉检查点
        if not missing:
            try:
                if os.path.exists(ckpt_path):
                    os.remove(ckpt_path)
            except OSError:
                pass
        merged = merge_candidates(res.cells, gap_results)
        filled: dict = {}
        filled_src: dict = {}
        for attr, recs in (merged.get("按属性") or {}).items():
            for r in recs:
                v = str(r.get("值") or "").strip()
                s = str(r.get("来源") or "").strip()
                if v:
                    filled[(r.get("行号"), attr)] = v
                    if s:
                        filled_src[(r.get("行号"), attr)] = s
        self.log(ctx, "视觉补充: 并入 %d 条(%d 属性)"
                 % (merged.get("并入") or 0, len(merged.get("按属性") or {})))
        # 来源也一并返回(新 schema: 值 + 来源)
        self._vision_sources = filled_src
        return filled

    @staticmethod
    def _vision_input_key(plan, img_col: str, *, rows=None, context=None) -> str:
        """视觉指纹包含实际行数据/图片链接及模型、属性与规则定义。"""
        import hashlib
        payload = json.dumps({"version": 2, "plan": plan.items, "image_column": img_col,
                              "rows": rows or [], "context": context or {}},
                             sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _norm_size_dim(v) -> str:
        """把**单个维度**的值规范成 `数值[，数值…]cm`。

        · 裸数字补单位（模型换算完忘了写单位 —— 实测 200 行里 长 49 行 / 宽 52 行这样）
        · 多件尺寸用 `，` 连接、**单位只写在末尾**（用户指定：`44.5，36.8，30.5cm`）
        · 其它长度单位换算成 cm（英寸→cm）
        · **解析不出就原样返回**，绝不猜、绝不清空（如「大号」这种没有数值的）
        """
        from tools.attr_stability.units import convert, parse_measure

        s = str(v or "").strip()
        if not s or s in ("未明确", "未标注", "不适用", "未知", "待确认", "None", "none", "缺"):
            # 哨兵不是尺寸值 → 留空。
            # 缺 单独立着也算留空：按提示词规则，「整列一块都没有该维度」应写 ""，
            # 缺 只用于**多值串里某一块**缺该维度（如 43.18，35.56，缺cm）。
            return ""
        parts = [p.strip() for p in re.split(r"[，,、;；/]+", s) if p.strip()]
        if not parts:
            return ""
        # 逐段规范化；`缺` 是**多值串里合法的占位**（某一块缺该维度），
        # 必须原样保留在串里，不能因为它而让整串退回原文（那样会漏掉单位）。
        tokens: list[str] = []
        for p in parts:
            if p == _SIZE_MISSING:
                tokens.append(_SIZE_MISSING)
                continue
            got = parse_measure(p)
            if got is None:
                if not re.fullmatch(r"-?\d+(?:\.\d+)?", p):
                    return s                    # 含非数值内容 → 原样保留
                tokens.append("%g" % round(float(p), 2))    # 裸数字：按 cm 处理
                continue
            val, q, u = got
            if q != "length":
                return s                        # 不是长度量 → 原样保留
            if str(u).strip().lower() == SIZE_SPLIT_UNIT:
                tokens.append("%g" % round(val, 2))
                continue
            c = convert(val, u, SIZE_SPLIT_UNIT)
            if c is None:
                return s                        # 换算不了 → 原样保留
            tokens.append("%g" % round(c, 2))
        if not any(t != _SIZE_MISSING for t in tokens):
            return ""                           # 整串都是「缺」→ 留空
        return "，".join(tokens) + SIZE_SPLIT_UNIT

    @classmethod
    def _extract_size_split(cls, res) -> tuple[dict, dict]:
        """从 `res.cells` 取 `尺寸_拆列.长/.宽/.高` 与 `尺寸_依据`。

        返回 `({xl_row: {维度: 规范值}}, {xl_row: 依据})`。
        取 `mode`（多数表决值）；`尺寸_依据` 的 mode 可能为空，则取第一个非空提名。

        ★ 键一律规范成 **int**（能转就转）：cells 的键是 `"2|尺寸_拆列.长"` 字符串，
          而交付表写盘时用的是 `row_by` 的 **int** 行号 —— 不统一就会两边都对不上，
          列建出来了但一格值都填不进去（实测踩过）。
        """
        out: dict = {}
        basis: dict = {}
        for key, d in (getattr(res, "cells", None) or {}).items():
            if not isinstance(d, dict):
                continue
            rid, _, path = str(key).partition("|")
            is_basis = path == SIZE_SPLIT_BASIS
            if not is_basis and not path.startswith(SIZE_SPLIT_PREFIX + "."):
                continue
            rk = int(rid) if str(rid).strip().isdigit() else rid
            vs = d.get("values") or []
            val = str(d.get("mode") or "").strip()
            if not val:                          # mode 空（无多数）→ 取第一个非空提名
                for cand in vs:
                    if isinstance(cand, dict) and str(cand.get("value") or "").strip():
                        val = str(cand["value"]).strip()
                        break
            if not val:
                continue
            if is_basis:
                basis[rk] = val
                continue
            dim = SIZE_SPLIT_DIM_ALIAS.get(path.split(".", 1)[1])
            if dim in SIZE_SPLIT_DIMS:
                norm = cls._norm_size_dim(val)
                if norm:
                    out.setdefault(rk, {})[dim] = norm
        # ★ 2026-10-26 用户口径: **没有 `尺寸_拆列` 时，从 `尺寸` 的值解析**。
        #   步骤17 现在禁止写 `尺寸_拆列`（拆列是步骤18 的事），所以正常跑出来的
        #   cells 里只有 `尺寸`，长/宽/高三列必须由这里填 —— 否则三列永远是空的。
        #   解析规则见 `tools/attr_stability/size_parse.py`：
        #     · 单件 `32x17x22cm`        → 长=32 宽=17 高=22
        #     · 套件 `小20x15 / 中25x18 / 大30x22` → 长=`20 / 25 / 30`（三值同列）
        #     · 单位统一换算成 cm
        #   有 `尺寸_拆列` 的行**不覆盖**（向后兼容历史数据）。
        try:
            from tools.attr_stability.size_parse import parse_size
            for row in (getattr(res, "deliverable", None) or []):
                rid = row.get("xl_row")
                if rid is None:
                    continue
                rk = int(rid) if str(rid).strip().isdigit() else rid
                if rk in out:                       # 已有拆列 → 不覆盖
                    continue
                at = row.get("attributes") or {}
                raw = str(at.get("尺寸") or "").strip()
                if not raw:
                    continue
                parsed = parse_size(raw)
                if not parsed.get("组数"):
                    continue
                dims = {d: str(parsed[d]) for d in SIZE_SPLIT_DIMS if parsed.get(d)}
                if dims:
                    out[rk] = dims
                    basis.setdefault(rk, str(at.get("尺寸_依据") or "").strip())
        except Exception:  # noqa: BLE001 — 解析失败不影响其他列
            pass
        return out, basis

    def _final_rows(self, res, std_map: dict | None) -> list[dict]:
        """把**最终交付值**投影成 `[{'xl_row', 'attributes'}]`，供跨属性校验用。

        为什么要它（2026-10-09 实测踩到的设计缺陷）：
          · `_register_cross_conflicts` 原来直接用 `res.deliverable`（**原始**多数表决值）
          · 而纠正步骤用的是 `std_map`（**规范化后**的值）
          两者值域不同 → 登记报的冲突（如 `主体材质=柚木`）在交付表里根本不存在
          （交付表里已是 `木制`）→ 一堆**假冲突**，而且纠正看到的又是另一批。
          **登记与纠正必须看同一份数据**，也就是**真正会写进交付表的值**。
        """
        std_map = std_map or {}
        attrs = {}
        # `getattr` 而不是 `res.deliverable`：`_Res` 这类**假对象**（单测）可能没有它，
        # 直接取属性会 `AttributeError` 把整组测试打挂（实测挂过 15 个）。
        for d in (getattr(res, "deliverable", None) or []):
            if not isinstance(d, dict):
                continue
            try:
                rid = int(d.get("xl_row"))
            except (TypeError, ValueError):
                continue
            attrs[rid] = dict(d.get("attributes") or {})
        out: list[dict] = []
        for rid in sorted(set(attrs) | {r for (r, _a) in std_map}):
            base = attrs.get(rid, {})
            row = dict(base)
            for (r2, a), v in std_map.items():
                if r2 == rid and str(v or "").strip():
                    row[a] = v
            out.append({"xl_row": rid, "attributes": row})
        return out

    def _register_cross_conflicts(self, ctx, out_dir: str, res,
                                 std_map: dict | None = None) -> int:
        """跨属性一致性：声明写在 rules.json 的 `cross_checks`。

        `std_map`：**最终交付值**。传了就按它校验（与纠正步骤同一份数据）；
        不传才回落到 `res.deliverable`（原始值，会有假冲突）。
        """
        try:
            import json as _json

            from tools.attr_stability.cross_checks import (
                check_rows, conflicts_report, load_cross_checks,
            )
        except ImportError:
            return 0
        cat_dir, cat_id = self._resolve_rules(ctx)
        decl: dict = {}
        if cat_dir and cat_id:
            rp = os.path.join(cat_dir, cat_id + ".rules.json")
            if os.path.exists(rp):
                try:
                    with open(rp, encoding="utf-8-sig") as fh:
                        decl = load_cross_checks(_json.load(fh))
                except (OSError, ValueError):
                    decl = {}
        if not decl:
            # ★★ 修复（审核中3，2026-10-10）：原来**没声明就早退不写** → 人分不清
            #   「没违例」和「根本没跑约束校验」（两者都得到"0 处"）。
            #   现在也写一份报告，明确标 `未声明约束`，与"有声明但零违例"区分开。
            try:
                with open(os.path.join(out_dir, CROSS_CHECK_NAME), "w", encoding="utf-8") as fh:
                    _json.dump({
                        "_说明": "**未声明跨属性约束**（rules.json 无 cross_checks）→ 本次**没有做**一致性校验",
                        "冲突总数": 0,
                        "模式": "未声明约束",
                    }, fh, ensure_ascii=False, indent=2)
            except OSError:
                pass
            return 0
        rows_for_check = (self._final_rows(res, std_map) if std_map is not None
                          else (res.deliverable or []))
        hits = check_rows(rows_for_check, decl)
        try:
            with open(os.path.join(out_dir, CROSS_CHECK_NAME), "w", encoding="utf-8") as fh:
                _json.dump(conflicts_report(hits), fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
        if hits:
            self.log(ctx, "⚠ 跨属性一致性冲突 %d 处（已登记到 %s，**不静默改值**）"
                     % (len(hits), CROSS_CHECK_NAME))
        return len(hits)

    # 材质类通用词(跨类目通用, 不是类目词表): 只有复合词里含这些词才算"更具体的材质"
    _MATERIAL_WORDS = (
        "steel", "metal", "plastic", "wood", "wooden", "leather", "fabric", "textile",
        "glass", "rubber", "silicone", "ceramic", "aluminum", "aluminium", "iron",
        "copper", "zinc", "nylon", "polyester", "cotton", "bamboo", "linen", "wool",
        "abs", "pp", "pc", "tpu", "pu", "eva", "stainless", "alloy", "titanium",
        "carbon", "fiber", "fibre", "mesh", "foam", "velvet", "canvas", "acrylic",
    )

    def _report_under_specified(self, ctx: StepContext, rules, res) -> None:
        """「依据比 value 更具体」自查（2026-10-22，③，**只报警不阻断**）。

        判据（**不依赖任何类目词**，换类目照样成立）:
          · 值很短（≤4 字，像「金属」「塑料」这种上位词）;
          · 且它的 `_依据` 里出现**含材质词的英文复合词**
            （如 `Alloy Steel` / `ABS Plastic` / `Stainless Steel`）。
        → 说明证据里有更具体的种类，值可能被降级成了上位词。

        ⚠ 第一版判据是"任意英文复合词"，实测 **938 行 / 44 属性全是误报**
        （`Pet Dryer`、`LED Lighting`、`Overheat Protection` 都被当成材质）。
        现在加了一道**通用材质词表**过滤（steel/plastic/wood/…，跨类目通用、不是类目专用词），
        只留真正的"具体材质"。

        为什么不做成阻断: 「不锈钢 ← Stainless Steel」这类**正确**的也会命中，
        它是"可能该细化"的提示，不是错误判定。
        """
        import re
        from collections import Counter

        # 只查 **vocab_policy=open** 的属性（即"具体优先、笼统兜底"的那批）。
        # 不这样做会把「是否带负离子=是 ← Equipped with」这类布尔题也报进来（实测 128 行噪音）。
        open_names = {str(n) for n, spec in (getattr(rules, "attrs", {}) or {}).items()
                      if str(getattr(spec, "vocab_policy", "") or "").lower() == "open"}

        phrase_rx = re.compile(r"\b([A-Za-z][A-Za-z\-]{1,}\s+[A-Za-z][A-Za-z\-]{1,})\b")
        # ⚠ 材质词必须用**词边界**匹配 —— 子串匹配会把 `equipped` 命中 `pp`、`app` 命中 `pp`
        #   （实测 128 行噪音里有 40+ 条是这么来的）。
        mat_rx = re.compile(r"\b(%s)\b" % "|".join(re.escape(w) for w in self._MATERIAL_WORDS), re.I)
        hits: Counter = Counter()
        samples: list = []
        for d in (res.deliverable or []):
            attrs = d.get("attributes") or {}
            for k, v in attrs.items():
                if str(k).endswith(("_依据", "_来源")):
                    continue
                if open_names and str(k) not in open_names:
                    continue
                val = str(v or "").strip()
                if not val or len(val) > 4:
                    continue
                basis = str(attrs.get(str(k) + "_依据") or "")
                if not basis:
                    continue
                found = ""
                for m in phrase_rx.finditer(basis):
                    phrase = m.group(1)
                    if mat_rx.search(phrase):
                        found = phrase
                        break
                if found:
                    hits[str(k)] += 1
                    if len(samples) < 6:
                        samples.append((d.get("xl_row"), str(k), val, found))
        if hits:
            self.log(ctx, "⚠ 上位词自查: %d 个属性存在「依据更具体但值较短」的行(共 %d 行)"
                     % (len(hits), sum(hits.values())))
            for k, n in hits.most_common(8):
                self.log(ctx, "   %s: %d 行" % (k, n))
            for xl, k, val, phrase in samples:
                self.log(ctx, "   例 xl_row=%s %s=%s ← 依据含「%s」" % (xl, k, val, phrase))
        else:
            self.log(ctx, "上位词自查: 未发现「依据更具体但值较短」的行")

    def _write_deliverables(self, ctx: StepContext, out_dir: str, rows, res,
                            std_map: dict | None = None, raw_too: bool = True) -> None:
        """写交付 xlsx: 单遍原始(raw_too=True) + 标准化交付(含 来源 sheet)。"""
        std_map = std_map or {}
        try:
            import openpyxl
            from tools.attr_stability.converge import normalize_form
        except ImportError:
            self.log(ctx, "⚠ 未安装 openpyxl, 跳过 xlsx 交付表")
            return
        row_by: dict[int, dict] = {}
        input_cols: list[str] = []
        from core.traffic_fields import PROCESS_ONLY_TRAFFIC_FIELDS
        for i, r in enumerate(rows, start=2):
            xl = r.get("xl_row")
            xl = int(xl) if xl is not None else i
            row_by[xl] = r
            for k in r:
                if k != "xl_row" and k not in PROCESS_ONLY_TRAFFIC_FIELDS and k not in input_cols:
                    input_cols.append(k)
        deliv = {int(d.get("xl_row")): (d.get("attributes") or {})
                 for d in (res.deliverable or []) if d.get("xl_row") is not None}
        # 新 schema: 列名**只取值键**(属性名) —— `_来源` 与 `_依据` 各有自己的 sheet
        attr_names: list[str] = []
        for d in (res.deliverable or []):
            for a in (d.get("attributes") or {}):
                # ★ 2026-10-09 修：新增 `_依据` 时只过滤了 `_来源` →
                #   `主体材质_依据` 之类**变成了主表的列**，还被 `是否*` 取值域检查
                #   误当成"是否类属性"报错（关卡抓到的）。两个后缀都要过滤。
                if a.endswith("_来源") or a.endswith("_依据"):
                    continue
                if a not in attr_names:
                    attr_names.append(a)
        # ---- 尺寸拆三列（长/宽/厚）：紧跟 `尺寸规格` 之后 ----
        size_map, size_basis = self._extract_size_split(res)
        cols: list[str] = list(attr_names)
        column_parent = {a: a for a in cols}
        from tools.attr_stability.rule_pipeline import delivery_columns
        for cfg in getattr(self, "_delivery_rule_configs", []):
            name = cfg["name"]
            position = cols.index(name) + 1 if name in cols else len(cols)
            for subcol in delivery_columns(cfg):
                if subcol not in cols:
                    cols.insert(position, subcol)
                    position += 1
                column_parent[subcol] = name
        if size_map:
            # ★ 2026-10-20: 锚点不再死绑「尺寸规格」——优先它, 否则取第一个名字含
            #   「尺寸」的属性(产品尺寸/商品尺寸/机身尺寸/外形尺寸…), 都没有就放最后。
            #   这样"类似商品尺寸的属性"都能挂上长/宽/高三列。
            anchor = len(cols)
            if SIZE_SPEC_ATTR in cols:
                anchor = cols.index(SIZE_SPEC_ATTR) + 1
            else:
                for _i, _n in enumerate(cols):
                    if "尺寸" in _n:
                        anchor = _i + 1
                        break
            cols[anchor:anchor] = list(SIZE_SPLIT_DIMS)
        id_cols = ["父ASIN", "变体ASIN", "标题"]
        ordered = [c for c in id_cols if c in input_cols] + [c for c in input_cols if c not in id_cols]
        input_headers = []
        taken = set(cols)
        renamed = {}
        for original in ordered:
            display = original
            if display in taken:
                display = "输入_" + original
                serial = 2
                while display in taken:
                    display = "输入_" + original + "_" + str(serial)
                    serial += 1
                renamed[original] = display
            taken.add(display)
            input_headers.append(display)
        if renamed:
            self.log(ctx, "交付输入列重名已区分(原值保留): %s" % renamed)
        header = input_headers + cols

        def raw_value(attrs, attr):
            # 新 schema: 值键=属性名, 直接是标量
            v = attrs.get(attr)
            return "" if v is None else str(v)

        def source_cell(attrs, attr):
            """来源跟踪: 直接读 属性_来源 键(新 schema 平铺)。"""
            return str(attrs.get(attr + "_来源") or "").strip()

        def size_of(xl, dim):
            """容忍 int/str 两种行号键（cells 是字符串键，写盘循环用 int）。"""
            d = size_map.get(xl)
            if d is None:
                d = size_map.get(str(xl))
            return (d or {}).get(dim, "")

        def basis_of(xl):
            v = size_basis.get(xl)
            return size_basis.get(str(xl), "") if v is None else v

        def write_sheet(path, standardize):
            wb = openpyxl.Workbook()
            def append_literal(sheet, values):
                sheet.append(values)
                for cell in sheet[sheet.max_row]:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        cell.data_type = "s"
            ws = wb.active
            ws.title = "属性标注"
            append_literal(ws, header)
            src_ws = wb.create_sheet("来源") if standardize else None
            bas_ws = wb.create_sheet("依据") if standardize else None
            if src_ws is not None:
                append_literal(src_ws, header)
            if bas_ws is not None:
                # ★ 「依据」sheet（2026-10-09）：逐属性原文依据。
                #   为什么单独一页：`来源` 只是标签（标题/五点描述），
                #   人工复核与事后校验真正要看的是**原文**（§3.3e）。
                append_literal(bas_ws, header)
            for xl in sorted(set(row_by) | set(deliv)):
                r = row_by.get(xl, {})
                attrs = deliv.get(xl, {})
                vals = []
                srcs = []
                basis = []
                for a in cols:
                    if a in SIZE_SPLIT_DIMS:
                        # 尺寸三列来自 `尺寸_拆列`（不是规格书属性），来源统一用 `尺寸_依据`
                        vals.append(size_of(xl, a))
                        srcs.append(basis_of(xl))
                        basis.append(basis_of(xl))
                        continue
                    parent = column_parent.get(a, a)
                    raw = raw_value(attrs, a)
                    if standardize:
                        v = std_map.get((xl, a), normalize_form(raw))
                    else:
                        v = raw
                    vals.append(v)
                    evidence = getattr(self, "_delivery_provenance", {}).get((xl, a), {})
                    srcs.append(evidence.get("来源", source_cell(attrs, parent)))
                    basis.append(evidence.get("依据", str(attrs.get(parent + "_依据") or "").strip()))
                append_literal(ws, [r.get(c, "") for c in ordered] + vals)
                if src_ws is not None:
                    append_literal(src_ws, [r.get(c, "") for c in ordered] + srcs)
                if bas_ws is not None:
                    append_literal(bas_ws, [r.get(c, "") for c in ordered] + basis)
            import tempfile
            fd, temporary = tempfile.mkstemp(suffix=".xlsx", dir=os.path.dirname(path))
            os.close(fd)
            try:
                wb.save(temporary)
                check = openpyxl.load_workbook(temporary, read_only=True)
                try:
                    if check["属性标注"].max_row != len(set(row_by) | set(deliv)) + 1:
                        raise StepDataError("交付文件行数校验失败")
                    if len(header) != len(set(header)):
                        raise StepDataError("交付文件存在重复表头")
                finally:
                    check.close()
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

        if raw_too:
            write_sheet(os.path.join(out_dir, RAW_XLSX_TPL.format(cat=self._cat)), standardize=False)
        write_sheet(os.path.join(out_dir, STD_XLSX_TPL.format(cat=self._cat)), standardize=True)
        self.log(ctx, "已写 标准化交付 xlsx(%d 行 × %d 属性%s)%s"
                 % (len(deliv), len(cols) - len(SIZE_SPLIT_DIMS),
                    " + 尺寸三列" if size_map else "", "" if raw_too else "(仅标准化)"))
        if size_map:
            self.log(ctx, "尺寸拆列: %d 行有长/宽/厚（单位 %s，多值用 `，`）"
                     % (len(size_map), SIZE_SPLIT_UNIT))
        # ★ 传 `std_map`：按**最终交付值**校验（与纠正步骤同一份数据）。
        #   不传会用 `res.deliverable` 原始值 → 报一堆交付表里根本不存在的假冲突。
        self._register_cross_conflicts(ctx, out_dir, res, std_map=std_map)

    def _build_cross_check_block(self, ctx: StepContext) -> str:
        """把**跨属性约束**注入标注提示词（2026-10-08，用户口径）。

        为什么要前移到步骤17（而不是只在步骤18 事后纠正）：
          · 模型标注时**手里就有原文与图片** —— 告诉它约束，它当场就能选对
            （把「实木砧板」填成「竹制砧板」），**冲突根本不产生**；
          · 事后（步骤18）纠正只能靠规则硬判或再问一次模型，更贵也更差。
        实测动机：Cutting Boards 有 28 行 `主体材质=竹木` 却 `产品类型=实木砧板`，
        而模型在步骤17 自报的 23 条冲突**全是同属性跨来源冲突**，一条跨属性都没有
        —— 因为它不知道存在这层约束。

        约束声明写在 rules.json 的 `cross_checks`（来源见步骤16）。
        读不到就返回 ""（不改变原有行为）。
        """
        try:
            import json as _json

            from tools.attr_stability.cross_checks import load_cross_checks
        except ImportError:
            return ""
        cat_dir, cat_id = self._resolve_rules(ctx)
        decl: dict = {}
        if cat_dir and cat_id:
            rp = os.path.join(cat_dir, cat_id + ".rules.json")
            if os.path.exists(rp):
                try:
                    with open(rp, encoding="utf-8-sig") as fh:
                        decl = load_cross_checks(_json.load(fh))
                except (OSError, ValueError):
                    decl = {}
        items = []
        for kind, rules_ in (decl or {}).items():
            for it in rules_:
                items.append((kind, it))
        if not items:
            return ""
        lines = ["## 本类目的跨属性约束（**必须同时满足**，标注时就要避开）", ""]
        for _kind, it in items:
            left, right = it.get("左") or {}, it.get("右") or {}
            def _side(c):
                bits = []
                if c.get("含"):
                    bits.append("含" + "、".join("「%s」" % x for x in c["含"]))
                if c.get("不得含"):
                    bits.append("不含" + "、".join("「%s」" % x for x in c["不得含"]))
                return "%s %s" % (c.get("属性"), " 且 ".join(bits))
            lines.append("- **%s** ⟹ %s" % (_side(left), _side(right)))
            if it.get("说明"):
                lines.append("    - 理由：%s" % it["说明"])
        lines += [
            "",
            "**怎么处理**：",
            "1. 在原文证据支持时，填写同时满足属性定义与约束的值；不得由一个属性猜测另一个属性。",
            "2. 若原文证据**确实**指向互相矛盾的两个值，**仍按证据填**（不许为了凑约束改证据），",
            "   但**必须在 `冲突` 里记一条**，格式：",
            '   `{"属性": "<被违反的属性>", "取值": "<你填的值>", "依据": "<原文里矛盾在哪>"}`',
            "",
        ]
        return "\n".join(lines)

    def _load_prompt(self, ctx: StepContext) -> str:
        rel = _cfg_str(ctx.config, "ATTR_STAB_PROMPT", "prompts/step17_general_core.md")
        for base in (str(ctx.paths.cat_root), _ROOT):
            p = os.path.join(base, rel.replace("/", os.sep))
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    return fh.read()
        return ""

    def _write_dry_run(self, ctx, out_dir, rules, rows):
        obj = {"dry_run": True, "category": self._cat, "rows": len(rows),
               "attributes": [a.name for a in rules.attrs.values()],
               "rules_source": rules.warnings or ["two-files"]}
        with open(os.path.join(out_dir, RESULT_NAME), "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        # dry-run 也要写 单遍原始 xlsx + 唯一值统计(空), 否则 missing_artifacts 会把步骤标 FAILED
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "属性标注"
            ws.append(["dry_run", "类目", "属性数"])
            ws.append([True, self._cat, len(obj["attributes"])])
            _save_workbook_atomic(wb, os.path.join(out_dir, RAW_XLSX_TPL.format(cat=self._cat)))
        except ImportError:
            self.log(ctx, "⚠ 未安装 openpyxl, dry-run 跳过 xlsx 占位")
        with open(os.path.join(out_dir, UNIQUE_STATS_NAME), "w", encoding="utf-8") as fh:
            json.dump({"cat": self._cat, "属性唯一值": {}}, fh, ensure_ascii=False, indent=2)
