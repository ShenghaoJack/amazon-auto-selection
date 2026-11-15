# attr_stability —— 属性标注 / 标准化工具层（接入 auto-workflow-python 说明）

> 定位：`tools/attr_stability/` 是**步骤 17（属性标注）与步骤 18（标准化）共用的工具层**，
> 提供结构闸门、多遍一致性表决、图片视觉补充、定向追问、归类收敛与词表放权等能力。
>
> 步骤侧只做「编排 + 读写产物」，**所有可复用机制都下沉到本工具层**。
>
> 本目录**零第三方依赖**（只用标准库），可整目录拷进目标工程。

---

## 一、模块定位（已从单步骤收口升级为步骤 17 + 18 共用层）

当前流水线共 **20 个步骤（0–19）**。本工具层服务于其中两个相邻步骤：

| 步骤 | 目录名 | 步骤类 | 文件 | 职责 |
|---|---|---|---|---|
| 16 | `16_属性集提取与确认` | `Step16AttributeAnnotation` | `steps/step16_attribute_annotation.py` | 提取属性集（**不做逐行标注**），产 `<CAT>-attribute-spec.json` |
| **17** | **`17_属性标注`** | **`Step17AttrAnnotation`** | **`steps/step17_attr_annotation.py`** | N 遍补充标注 + 闸门 + 表决 + 图片视觉补充 |
| **18** | **`18_标准化`** | **`Step18Standardization`** | **`steps/step18_standardization.py`** | 词表生成/聚类 + 归并标准化 + 新词初审 |
| 19 | `17_备份与监控`（遗留名） | 备份与最终体检 | `steps/step17_backup_monitor.py`（number=19） | 全产物体检 + 预警 + 归档提示 |

> 步骤 16 只提取属性集、不再产 xlsx；步骤 17 只做标注（产单遍原始 + 稳定性结果）；**归并标准化已拆到步骤 18**。
> 三者职责边界清晰，不再出现「一个步骤既标注又标准化」的旧结构。

---

## 二、文件与职责

| 文件 | 职责 |
|---|---|
| `attr_stability.py` | 规则源读取、**结构闸门**（词表/结构键/来源名/交叉校验）、独异格率、一致性表决（并集/多数）、交付装配、重跑指令生成 |
| `attr_runner.py` | 生产者适配（`LLMClient` 直调 / 读文件）、一轮闭环编排、离线 CLI |
| `catrules.py` | 类目文件统一加载器（2 份 / 兼容旧 4 份） |
| `gapfill.py` | 定向追问（gap-fill）：只对「未明确」格换问法再问，产出候选值 + 依据（不覆盖原值） |
| `converge.py` | 归类与标准化（收敛层）：形式归一 + 受控词表机械收敛 + 语义归类载荷/词表校验 |
| `onboard.py` | 词表放权：新词**独立初审提示词** + 批量确认写回 `rules.json` |
| `std_rules.py` | 规则类型引擎：把「归类 + 标准化」拆成可复用规则类型，参数全部来自 `rules.json` |
| `spec.py` | 步骤 16 属性集产物纯函数（N 次抽取合并 + 同义归并提示词 + spec 读写 + rules.json 对齐/自动建骨架） |
| `onboard_cli.py` | 词表放权 CLI（批量确认入口，零依赖，不做 LLM 调用） |
| `README_接入说明.md` | 本文档 |

**对外导出**（`__init__.py`）覆盖标注侧（`gate_run` / `consistency` / `majority` / `deliverable_from_cells` / `build_retry_brief`）、
追问侧（`find_gaps` / `merge_candidates`）、收敛侧（`converge_mechanical` / `build_classify_payload` / `apply_classification`）、
词表侧（`build_vocab_review_prompt` / `apply_vocab_acceptance`）与生产者适配（`run_round` / `Producer` 系列）。

**硬约束（有自检）**：闸门源码里**不得出现任何类目词**，类目信息一律从 `categories/<cat>.json` 读取；
`lint_category_words(src)` 返回命中的词。

---

## 三、步骤 17 · 属性标注（`steps/step17_attr_annotation.py`）

- 步骤类：`Step17AttrAnnotation`，`number = 17`，`title = "属性标注"`。
- **只做标注**，归并标准化交给步骤 18。

### 上游

1. 步骤 16 已确认的属性集规格 `<CAT>-attribute-spec.json`（`status = confirmed`）；
2. 步骤 14 流量拆分输入 `14_流量拆分/流量拆分数据-需人工补充.xlsx`（sheet `AI已补充`）；
3. 类目规则 `*.rules.json`（缺则按已确认 spec 自动建最小骨架：属性 rule=enum、词表留空）。

### 流程

`ATTR_STAB_RUNS`（默认 5）遍补充标注 → **宽松结构闸门**（只记录不拦截，不 REJECT/不重跑）→
**一致性表决**（独异格率筛离群 + 并集）→ **图片视觉补充**（仅对「未明确」格用图片再问）。

### 产出 `17_属性标注/`

| 产物 | 说明 |
|---|---|
| `稳定性结果.json` | 含交付表 + **两套指标**（诚实指标 / 交付机制）+ `vocab_candidates` |
| `<CAT>-属性标注-单遍原始.xlsx` | 单遍原始标注（每属性一列） |
| `唯一值统计.json` | 各属性唯一值统计 |
| `input.json` | 输入快照（供闸门「与输入交叉校验」） |
| `视觉补充.json` | 图片视觉补充结果 |
| `runs/` | 逐遍原始运行 |
| `retry_briefs/` | （宽松模式下不再产生）被拒运行的指名到行重跑指令 |

> 两套指标必须分开汇报：`metrics_honest`（不剔除任何运行，用于衡量规则质量）与
> `metrics_delivery`（剔除语义离群后交付，用于产出交付物）——拿后者当成绩报是自欺。

### 韧性

- **单遍失败隔离**：宽松模式下结构问题不 REJECT、不重跑；仅某一遍 LLM 调用失败（超时/连接）才废这一遍，不炸整步。
- **输出截断自愈**：`finish_reason=length` → `LLMTruncatedError` → 自动拆半重试。

---

## 四、步骤 18 · 标准化（`steps/step18_standardization.py`）

- 步骤类：`Step18Standardization`，`number = 18`，`title = "标准化"`（继承 `Step17AttrAnnotation`）。

### 上游

步骤 17 的 `稳定性结果.json` + `input.json` + `视觉补充.json` + 类目规则 `*.rules.json`。

### 流程

1. **冷启动词表生成**（`ATTR_STAB_GEN_VOCAB=true` 时）：对「无词表」的 enum/multi 属性，
   取唯一值 → LLM 语义聚类 → 生成规范值写回 `rules.json`（无需人工预填词表）；
2. **归并标准化**：受控词表机械收敛 + 大模型语义归类；
3. **新词初审**：新词候选 → 独立审校员 AI 初审（半自动 / 全自动，`ATTR_STAB_AUTO_ONBOARD`）。

### 产出 `18_标准化/`

| 产物 | 说明 |
|---|---|
| `<CAT>-属性标注-标准化交付.xlsx` | **两个 sheet**：`属性标注` + `来源` |
| `词表聚类报告.json` | **原值 → 规范值** 归并映射，供人工审核 + Web secStd 弹窗展示 |
| `新词初审裁决.json` | 半自动模式的新词初审裁决 |
| `新词自动采纳记录.json` | 全自动模式的新词采纳记录 |

---

## 四点五、交付表列级精修（独立脚本，**不在步骤17/18 里跑**）

步骤17 的标注是按「属性说明 + 受控词表」整表跑的，个别列会出现**口径对但值不够精确**的情况。
这类列用独立脚本单独重提取，直接写回交付表；不改动步骤17/18 的任何代码。

### `thickness.py` + `scripts/rethickness.py` — 「厚度」精确 cm

`python scripts/rethickness.py --cat "<CAT>" [--apply]` → `17_属性标注/厚度-精确值.json`。

- **背景**：交付表「厚度」此前只有 `超薄(≤3mm)/标准(约10-15mm)/加厚(约20-45mm)/超厚(≥45mm)`
  四档区间（步骤16 的属性说明把"典型取值"写成了档位，步骤17 照档回，步骤18 按 enum 收敛）。
- **三层证据**：① 文本**显式单块厚度陈述**（`6mm thickness` / `0.31 inches thick`，约 280 行）直接采用；
  ② `详细参数` 的 `Product Dimensions ... Th` 与 `商品尺寸` 最小维（**Th 标签会错位**：实测
  `10"L x 0.6"W x 15"Th` 把 15" 标成厚度，故两者都要过物理合理性闸门）；
  ③ **图片视觉 + OCR** 读尺寸标注图/侧视图（其余 ~2200 行都过一遍）。
- **套装核实**：`套装数量 > 1` 时结构化尺寸那一维常是**整叠高度**（实测 12 件套写 `4.13"Th`
  而五点写明单块 `0.31"`）—— 图片确认是整叠且拿不到单块时**不把整叠值当厚度**，记 `未明确`
  并在「来源」写明整叠高度与件数。
- **单位**：一律换算成 **cm**（1 inch = 2.54 cm，1 mm = 0.1 cm），写法与步骤18 numeric 归一一致
  （`%g` + `cm`，如 `1.524cm`）。

### `material_faces.py` + `scripts/rematerial.py` — 「主体材质」两面核实

`python scripts/rematerial.py --cat "<CAT>" [--apply]` → `17_属性标注/主体材质-两面核实.json`。

- **口径（用户 2026-09-24）**：`A+B` **只表示砧板两个切面材质不同**；两面一样只写一个材质；
  **配件不算"面"**（塑料砧板垫 / 抽拉托盘 / 收纳架盒 / SAN 盖 / 硅胶包边 / 不锈钢钢条）不进主体材质；
  成套但**不同件**材质不同 → 只写主材质。
- **核实范围**：`是否双面可用 ∈ {双面可用, 双面不同材质}`（实测 1253 行）或材质列已含 `+`（108 行）。
  实测 108 个 `+` 里：73 行确为双面不同材质（保留），约 20 行是"本体+配件"、约 15 行是"成套不同件"
  （该去掉 `+`），另有 7 行标了"双面不同材质"却没写 `+`（该补上）。
- 材质名收敛到受控词表（`rules.json` 主体材质 values ∪ 交付表实际取值）。

### 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `THICKNESS_WORKERS` | `ATTR_STAB_WORKERS`(6) | 厚度视觉并发 |
| `THICKNESS_VISION_DETAIL` | high | 读尺寸小字用高清 |
| `THICKNESS_MAX_IMAGES` / `THICKNESS_APLUS_IMAGES` | 5 / 3 | 详情图 / A+ 图 张数上限 |
| `THICKNESS_TEXT_BATCH` | 8 | 厚度**文本复核**批量条数(纯文字 LLM, 带原文核验) |
| `MATERIAL_WORKERS` | `ATTR_STAB_WORKERS`(6) | 主体材质核实并发 |
| `MATERIAL_VISION_DETAIL` | high | 同上 |
| `MATERIAL_MAX_IMAGES` / `MATERIAL_APLUS_IMAGES` | 4 / 2 | 详情图 / A+ 图 张数上限 |
| `REPAIR_WORKERS` / `REPAIR_BATCH` | `ATTR_STAB_WORKERS`(6) / 8 | 列级修复的并发 / 批大小 |
| `REPAIR_VISION_DETAIL` / `REPAIR_MAX_IMAGES` / `REPAIR_APLUS_IMAGES` | high / 4 / 2 | 列级修复的视觉参数 |

> 都带**行级断点**(`*.checkpoint.json`, 每完成一行即落盘, 中断重跑只补剩余)。

### `text_llm.py` — 批量文本 LLM 马达

`run_batches(ctx, items, build_prompt, parse/accept/key_of, batch, workers, ckpt_path, plan_key)`:
切批 → 并发调 `chat_json` → 逐批解析(一批里缺几行只丢那几行) → 整批失败同批重发 → 再失败降为
2 行/批、1 行/批 → 断点续跑。配 `quote_in_text(quote, texts)` 做**原文引用核验**:
模型给的"原文片段"必须真的出现在该行资料里(归一化大小写/全角标点后按字面比), 把幻觉挡在门外 ——
这也是敢让"文本复核"优先于"结构化尺寸"的底气。

### `attr_repair.py` + `scripts/repair_attrs.py` — 材质归一 / 类型对齐 / 空白补全

`python scripts/repair_attrs.py --cat "<CAT>" [--apply] [--redecide] [--size-detail] [--report]`
→ `17_属性标注/属性修复-材质与空白.json`。

- **材质归一**(`MATERIAL_MAP`, 全量含 `+` 单元格): 木种→`木制`、PP/塑料/小麦秸秆PP/亚克力/树脂/TPU→`塑料`、
  304不锈钢→`不锈钢`、竹木→`竹制`、钛金属→`钛`、钢化玻璃→`玻璃`、大理石/花岗岩→`石材`。
- **材质真伪逐行核实**: 文本 LLM(原文核验) → 判不出才走图片视觉。**族一致时保留原写法** ——
  核实模型常只抄 `Material:` 一个字段, 会把 `钛+塑料` 压成 `钛`, 把"两面不同材质"丢掉。
- **产品类型对齐**(`align_type`): 按已核实材质给类型(钛→钛金属砧板 / 竹制→竹制砧板 / 木制→实木砧板 …);
  `拼盘托盘`、`带标识分类砧板` 是**形态名**, 不动; 硅胶属塑料族, 保持 `塑料砧板`/`塑料软砧板垫` 形态。
- **只补真空白**(不含「未明确」): 主体材质 / 产品类型 / 套装数量 / 尺寸规格;
  多件套只给了一个尺寸时**逐件细化**成 `大号:40.6x28.4x0.94 cm,中号:…`。
- `--redecide` 用产物里留档的**原始文本/视觉结论**重跑定案(不调模型), 调口径时零成本。

### `size_thickness_sync.py` + `scripts/sync_thickness_size.py` — 厚度 ↔ 尺寸规格·高 对齐

`python scripts/sync_thickness_size.py --cat "<CAT>" [--apply] [--vision-fill]`

- **口径**(用户 2026-09-24 第三轮): 「厚度」要与 `尺寸规格` 的第三维(高/厚)**对齐**;
  多件套**各件厚度相同 → 只写一个值**, **不同 → 逐件写** `大号:0.5cm,小号:0.4cm`(标签与尺寸规格逐段对应);
  **没有厚度写 `缺`**(取代「未明确」); 两列冲突时**以核实到的单块厚度为准, 同时修正尺寸规格的高**。
- **整叠判定**: `高 ≈ 件数 × 单块厚度`(容差 25%) → 判为该维是整叠, 换成单块厚度。
  实测 5 件套 `50.80x35.56x10.16 cm`(单块 2.03)、2 件套 `大号:38.1x27.94x4.06,小号:…x4.06`(单块 2.03)、
  12 件套 `30.5x20.3x7.6 cm`(五点写明单块 6mm)。
- **回写方式**: 只替换 `尺寸规格` 里第三个数那个 token, **长宽/标签/单位/空格原样保留**;
  数值真的不同才改(纯格式差异如 `2.0`/`2` 不动, 避免无意义 churn)。
- `--vision-fill`: 对仍为「缺」的行, 用**只问厚度、不给结构化尺寸**的图片提示词再试一次(避免被锚定)。

### `vision_json.py` — 视觉返回剥壳

模型有时不直接回我们要的 JSON, 而是回一层包装(`{"type":"json_object","response":{…}}`
或 `{"type":"json_object","content":"<json 字符串>"}`); 直接 `.get()` 会**静默读不到字段**。
`unwrap_vision(rec, want)` 逐层剥到含目标字段的那一层(实测「主体材质」核实有 208 条命中、
13 行因此被误判成"视觉与现状一致")。

---

## 五、配置项（按步骤归属）

| 键 | 默认 | 步骤 | 说明 |
|---|---|---|---|
| `ATTR_MODE` | auto | 16 | 属性提取模式 auto/manual |
| ~~`ATTR_SPEC_RUNS`~~ | 3（写死） | 16 | 2026-11-11 起不再是配置项：每价格段固定抽 3 轮 |
| `ATTR_KEYS` / `ATTR_DESC` | "" | 16 | 人工补充属性 / 说明 |
| `ATTR_STAB_RUNS` | 5 | 17 | 跑几遍做表决（<2 则 SKIP） |
| `ATTR_STAB_CHUNK` | 12 | 17 | 单次调用送几行 |
| `ATTR_STAB_MAX_ALONE` | 10.0 | 17 | 独异格率阈值(%)，超过不进交付 |
| `ATTR_STAB_MAX_RETRY` | 2 | 17 | （宽松模式下失效）被闸门拒后最多重跑几轮 |
| ~~`ATTR_STAB_MIN_RATE`~~ | 0（写死） | 17 | 2026-11-11 起不再是配置项：一致率提示门槛固定 0，只提示不阻断 |
| `ATTR_STAB_PROMPT` | prompts/23_llm_only_pipeline.md | 17 | 标注提示词路径 |
| `ATTR_STAB_GEN_VOCAB` | false | 18 | 冷启动词表自动生成开关 |
| `ATTR_STAB_VOCAB_GRANULARITY` | 50 | 18 | 合并粒度：0=最粗合并 / 100=最细细分 |
| `ATTR_STAB_AUTO_ONBOARD` | false | 18 | 新词全自动写回开关（false=半自动交人工审） |

> 注：`ATTR_STAB_VOCAB_MIN_FREQ` 已**删除 / 内化**（固定所有唯一值都进聚类）。

---

## 六、关键机制（新增，需写进对应步骤文档）

1. **冷启动词表自动生成**：步骤 18 对无词表 enum/multi 属性自动聚类生成规范值，无需人工预填词表。
2. **词表聚类归并报告**：`18_标准化/词表聚类报告.json`，含「原值 → 规范值」映射，日志 + Web secStd 弹窗展示，供人工审核。
3. **std-vocab Web 面板**（secStd，Step18 Standardize）：每属性显示唯一值 + 规范值输入 + 粒度滑块 + 归并展示 + 重新生成 + 保存。
4. **输出截断自愈**：`finish_reason=length` → `LLMTruncatedError` → 自动拆半重试。
5. **单遍失败隔离 + 消除重复产出**：`run_round` 单遍失败标 REJECT 重跑，不再重复调 LLM（省 token）。
6. **视觉通道**：读全部图片（不限 4 张），含 OCR/文字类属性自动高清（`detail`/`max_images`）。
