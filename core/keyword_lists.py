"""关键词清单三态(自动 / 人工 / 停止) —— 核心大词 + 长尾词。

背景(2026-09-29 用户口径): 核心大词与长尾词原先只有 FR-04 自动生成的
`<CAT>-keywords.txt`, 人工无法干预; 当时"步骤10 注入词"已经有一套人工清单模式
(`filter-<CAT>-inject-words.txt`: 有清单=人工, 删除=回自动)。本模块先借用了那套做法,
随后(**2026-10-06 用户口径**)关系反转: **步骤10 的判定参考词改为直接同步本模块的
"生效核心大词"**(长尾词只用于步骤9 抓取与报告, 不参与注入)**, 独立的注入词清单与面板已下线 —— 于是"关键词清单面板 /
步骤9 搜索 / 步骤10 判定参考词"三处永远同一批词, 只需在一处维护。三态如下:

  auto   自动 : FR-07 判定的达标核心词(US)/站内搜索量前 N(非 US) + FR-04 长尾词;
                开品门槛 = 达标核心词数 >= GATE_MIN_QUALIFIED(默认 2) —— 与旧行为完全一致
  manual 人工 : 用人工清单词。可以在达标词里删掉不想要的, 也可以加不达标词或手填新词;
                **不再看 FR-07 判定**; 开品门槛 = 最终生效核心词数 >= GATE_MIN_QUALIFIED
                (不够就中止并提示: 补词 / 下调门槛 / 改回自动)
  stop   停止 : 人工决定中止 —— 步骤9 直接阻断(不抓数据), 后续步骤自然不跑

文件(都在类目目录, 与 FR-04 产物同级):
  filter-<CAT>-core-words.txt      人工核心大词(存在且非空 => 人工态);
                                   可含 `[仅注入]` 分区(见下), 两个分区共存于同一文件
  filter-<CAT>-longtail-words.txt  人工长尾词(独立生效, 不参与门槛)
  filter-<CAT>-keyword-stop.txt    停止标记(存在 => 停止态; **不需要填理由**)
  <CAT>-used-keywords.txt          本轮实际生效留痕(来源 + 分项统计 + 门槛判定)

`[仅注入]` 分区(2026-10-06 用户口径 —— "注入词只能和核心词同步吗, 不能添加吗"):
  步骤10 的注入词默认 = 生效核心大词。但有时想**多加几个只作判定参考、不去抓取**的词
  (例如某个近义词想让它参与"是不是本类目整机"的判断, 又不想多花 5 页搜索),
  就把它们写进同一文件的 `[仅注入]` 分区:
      [核心大词]
      pepper grinder
      [仅注入]
      手填的判断参考词
  于是: 步骤9 抓取 = 核心大词(不含仅注入); 步骤10 注入 = 核心大词 + 仅注入词。
  仅注入词**不进门槛判定**(门槛只看核心词数), 也不受三态影响 —— 自动态下同样生效。
  兼容: 没有分区头的旧文件(纯词表)全部按核心大词读。

优先级: stop > manual > auto。

为什么门槛按"最终生效词数"判(而不是继续按 FR-07 达标数判):
  人工态的意义就是"我来定搜哪些词"。若仍按达标数判, 使用者手填的新词没有 FR-07 判定
  -> 永远凑不够 -> 人工兜底失效。改按最终词数判后: 人工删减/新增都能生效, 同时门槛
  仍守着"至少要有 N 个词", 不会因为一份空清单就把流水线放过去。

站点差异(保持既有行为, 不因本模块改变): **非 US 站自动态**门槛不生效(有词即可继续),
  因为那套阈值是按美国站数据特征定的; 但**人工态一律按门槛判**(US/非 US 同一把尺子),
  口径不一致时以"人工自己定的词"为准更容易解释。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MODE_AUTO = "auto"
MODE_MANUAL = "manual"
MODE_STOP = "stop"

MODE_LABELS = {MODE_AUTO: "自动", MODE_MANUAL: "人工清单", MODE_STOP: "人工停止"}

# 人工清单数量上限(误粘贴保护; 面板也按这个值校验)
MAX_CORE = 50
MAX_LONGTAIL = 50
MAX_INJECT = 50                          # `[仅注入]` 分区上限
MAX_NOINJECT = 50                        # `[不注入]` 分区上限

# `filter-<CAT>-core-words.txt` 的分区头(2026-10-06 起; 无头的旧文件按核心大词读)
CORE_SECTION = "[核心大词]"
INJECT_SECTION = "[仅注入]"
# ★ `[不注入]`(2026-10-12 用户口径 A): **从步骤10 的注入里扣掉**这些词。
#   语义: 该词**仍然**是核心大词、**仍然**被步骤9 抓取, 只是**不再参与步骤10 的判定**。
#   为什么需要它: `inject_words = 核心大词 + 仅注入词` 是**只加不减**的 —— 光把达标核心词
#   预填进「仅注入」再删掉是**没有效果**的(它还在核心词里)。要"删得掉"就必须有这个减法清单。
NOINJECT_SECTION = "[不注入]"
_INJECT_KEYS = ("仅注入", "inject")       # 分区头判定关键字(大小写/中英都不敏感)
_NOINJECT_KEYS = ("不注入", "noinject", "no-inject", "exclude")

_USED_HEADER = "本轮实际生效关键词(由步骤6/8/9 写入, 供人工核对口径)"


def core_manual_name(cat: str) -> str:
    return f"filter-{cat}-core-words.txt"


def longtail_manual_name(cat: str) -> str:
    return f"filter-{cat}-longtail-words.txt"


def stop_name(cat: str) -> str:
    return f"filter-{cat}-keyword-stop.txt"


def used_name(cat: str) -> str:
    return f"{cat}-used-keywords.txt"


def _p(cat_dir, name: str) -> Path:
    return Path(cat_dir) / name


def read_words(path) -> list[str]:
    """逐行读词: 去空白 / 小写 / 去重 / 跳过 `[分区]` 行与 `#` 注释。读不到返回空列表。"""
    out: list[str] = []
    seen: set[str] = set()
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except Exception:                                   # noqa: BLE001 — 读不到按"无清单"处理
        return out
    for ln in text.splitlines():
        s = ln.strip().lower()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def write_words(path, words) -> list[str]:
    """写人工清单(去重/小写/去空)。返回实际写入的词。空列表 => 等价删除文件(回自动)。"""
    clean: list[str] = []
    seen: set[str] = set()
    for w in words or []:
        s = str(w).strip().lower()
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not clean:
        remove_file(p)
        return []
    p.write_text("\n".join(clean) + "\n", encoding="utf-8")
    return clean


def remove_file(path) -> bool:
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _norm_words(words) -> list[str]:
    """去空/小写/去重(保序)。"""
    out: list[str] = []
    seen: set[str] = set()
    for w in words or []:
        s = str(w).strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def read_core_sections3(path) -> tuple[list[str], list[str], list[str]]:
    """读核心清单文件 -> `(核心大词, 仅注入词, 不注入词)`。

    兼容四种写法:
      * `[核心大词]` + `[仅注入]` + `[不注入]`(新格式);
      * 只有前两个之一(2026-10-06 起的格式);
      * **无任何分区头的纯词表**(2026-10-06 之前保存的文件) -> 全部算核心大词。
    未知分区头之后的行也算核心大词(等价旧 `read_words` 的"除 `[..]` 行外全读"), 避免丢词。

    ⚠ `read_core_sections()`(两元组)是本函数的前两个返回值 —— **只在这里解析一次**,
      两处各写一套解析就会漂移。
    """
    core: list[str] = []
    inject: list[str] = []
    noinject: list[str] = []
    target = core                              # 分区头之前的行 -> 核心大词(旧格式)
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except Exception:                          # noqa: BLE001 — 读不到按"无清单"
        return core, inject, noinject
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            head = s[1:-1].strip().lower()
            if any(k in head for k in _NOINJECT_KEYS):
                target = noinject
            elif any(k in head for k in _INJECT_KEYS):
                target = inject
            else:
                target = core
            continue
        w = s.lower()
        if w not in target:
            target.append(w)
    return core, inject, noinject


def read_core_sections(path) -> tuple[list[str], list[str]]:
    """读核心清单文件 -> `(核心大词, 仅注入词)`（两元组，`[不注入]` 由 `read_core_sections3` 取）。"""
    core, inject, _noinject = read_core_sections3(path)
    return core, inject


def write_core_sections(path, core, inject) -> tuple[list[str], list[str]]:
    """写核心清单文件（**两分区签名保持不变**；`[不注入]` 见 `write_core_sections3`）。

    ⚠ 与读侧对称：**新功能走 `_3` 版本**，旧签名返回值不变 —— 免得打断既有调用方
      （`set_list` / 测试都用 `c, i = write_core_sections(...)`）。
    """
    c, i, _x = write_core_sections3(path, core, inject, None)
    return c, i


def write_core_sections3(path, core, inject, noinject=None) -> tuple[list[str], list[str], list[str]]:
    """写核心清单文件(三分区)。返回 `(实际写入的核心词, 仅注入词, 不注入词)`。

    * 仅注入词里与核心词重复的会被剔除(避免同一词被算两次);
    * **不注入词可以就是核心词**(它的语义正是"从注入里扣掉某个核心词"), 所以**不剔除**,
      但会与仅注入词去重(一个词不能同时"只注入"又"不注入" —— 不注入优先);
    * 三个分区都为空 => **删除文件**(回自动态);
    * 只写非空分区。
    """
    c = _norm_words(core)
    cset = set(c)
    x = _norm_words(noinject)                  # 不注入: 允许是核心词
    xset = set(x)
    i = [w for w in _norm_words(inject) if w not in cset and w not in xset]
    p = Path(path)
    if not c and not i and not x:
        remove_file(p)
        return [], [], []
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [CORE_SECTION] + c
    if i:
        lines += [INJECT_SECTION] + i
    if x:
        lines += [NOINJECT_SECTION] + x
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return c, i, x


def read_sections(path) -> dict[str, list[str]]:
    """通用分区解析: `{分区名: [词]}`(分区名保留原样, 词小写去重)。

    用于读 FR-04 的 `<CAT>-keywords.txt`([核心大词]/[长尾词]) 与
    `<CAT>-keywords-entities.txt`([场景.长尾词] 等五维分区, 供面板分组展示)。
    """
    out: dict[str, list[str]] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except Exception:                                   # noqa: BLE001
        return out
    section = ""
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s
            out.setdefault(section, [])
            continue
        if not section:
            continue
        w = s.lower()
        if w not in out[section]:
            out[section].append(w)
    return out


def read_keywords_file(path) -> tuple[list[str], list[str]]:
    """解析 FR-04 产物 `<CAT>-keywords.txt` -> ([核心大词], [长尾词])。"""
    sec = read_sections(path)
    return list(sec.get("[核心大词]") or []), list(sec.get("[长尾词]") or [])


def longtail_dimensions(cat_dir, cat: str) -> dict[str, list[str]]:
    """从五维实体台账取 `{维度: [该维派生的长尾词]}`(面板按维分组展示用)。"""
    sec = read_sections(_p(cat_dir, f"{cat}-keywords-entities.txt"))
    out: dict[str, list[str]] = {}
    for name, words in sec.items():
        if not (name.startswith("[") and name.endswith("]")):
            continue
        inner = name[1:-1]
        if "." not in inner:
            continue
        dim, kind = inner.split(".", 1)
        if kind == "长尾词":
            out[dim] = list(words)
    return out


def manual_first(cat_dir, cat: str, kind: str, auto_words) -> tuple[list[str], str]:
    """只做"人工优先"这一层, 返回 `(生效词, 来源标签)`。

    供 FR-06 趋势 / FR-08 报告这类**只读消费端**使用(它们不参与门槛判定, 只需与
    步骤9 用同一批词); 需要完整门槛口径时请用 `resolve()`。
    """
    name = core_manual_name(cat) if kind == "core" else longtail_manual_name(cat)
    # ★★ 修复（2026-10-12）：`kind == "core"` **必须分区感知**。
    #   原来一律用 `read_words()`，而它**跳过 `[..]` 行、把文件里所有词都读出来** →
    #   一个只带 `[仅注入]` 分区的文件，在这里会被当成"人工核心大词"。
    #   后果：`resolve()`（**步骤9 走这条**）是分区感知的（`[核心大词]` 为空 → 核心词走**自动**），
    #   而本函数（**步骤6 趋势走这条**）却把"仅注入词"当核心词 → **两步的词不一致**：
    #   步骤9 抓自动词、步骤6 却拿仅注入词去分析趋势。
    #   `read_core_sections` 对"无分区头的旧纯词表"同样全部算核心大词 → **向后兼容**。
    if kind == "core":
        words, _inject = read_core_sections(_p(cat_dir, name))
    else:
        words = read_words(_p(cat_dir, name))
    if words:
        return words, "人工清单"
    return list(auto_words or []), "自动"


@dataclass
class Plan:
    """一次运行最终生效的关键词方案(核心 + 长尾)与门槛判定。"""

    mode: str = MODE_AUTO
    core: list[str] = field(default_factory=list)
    longtail: list[str] = field(default_factory=list)
    inject_only: list[str] = field(default_factory=list)
    # ★ `[不注入]`(2026-10-12 用户口径 A): **从步骤10 的注入里扣掉**这些词。
    #   它们是/可以是核心大词 —— 仍被抓取、仍算核心, 只是不参与判定。
    no_inject: list[str] = field(default_factory=list)
    auto_core: list[str] = field(default_factory=list)
    auto_longtail: list[str] = field(default_factory=list)
    manual_core: list[str] = field(default_factory=list)
    manual_longtail: list[str] = field(default_factory=list)
    screen_verdict: dict[str, str] = field(default_factory=dict)
    gate: int = 2
    gate_applies: bool = True
    stopped: bool = False

    # ---------------------------------------------------------------- 查询
    @property
    def mode_label(self) -> str:
        """三态中文名: 自动 / 人工清单 / 人工停止。"""
        return MODE_LABELS.get(self.mode, self.mode)

    @property
    def is_manual(self) -> bool:
        return self.mode == MODE_MANUAL

    @property
    def qualified(self) -> int:
        """最终生效核心词里被 FR-07 判"达标"的个数(人工手填词通常为 0)。"""
        return sum(1 for w in self.core if self.screen_verdict.get(w) == "达标")

    @property
    def no_verdict(self) -> int:
        """最终生效核心词里**没有 FR-07 判定**的个数(人工新增词属这一类)。"""
        return sum(1 for w in self.core if w not in self.screen_verdict)

    @property
    def gate_ok(self) -> bool:
        """门槛是否通过。

        - 人工态: 一律 `最终核心词数 >= 阈值`(US/非 US 一致)
        - 自动态: 按站点 —— 适用站点(=US)同上; 不适用站点(非 US)只要非空即可
        """
        n = len(self.core)
        if not self.is_manual and not self.gate_applies:
            return n > 0
        return n >= max(1, int(self.gate or 1))

    @property
    def longtail_manual_active(self) -> bool:
        return bool(self.manual_longtail)

    @property
    def inject_words(self) -> list[str]:
        """**步骤10 的注入词** = 生效核心大词 + `[仅注入]` 词 − `[不注入]` 词（去重保序）。

        核心大词同时供步骤9 抓取; 仅注入词**只注入不抓取**、也不进门槛判定。
        **不注入词**（2026-10-12 用户口径 A）：仍然是被抓取的核心大词，但**不参与步骤10 判定**
        —— 这就是"在面板上把某个达标核心词从注入里删掉"的落地方式。
        （没有这一项时公式退化为"只加不减"，删不掉任何核心词，面板上删了也不生效。）
        """
        skip = set(self.no_inject)
        out = [w for w in self.core if w not in skip]
        for w in self.inject_only:
            if w not in out and w not in skip:
                out.append(w)
        return out

    def inject_text(self) -> str:
        """步骤10 注入口径一行(日志/面板同口径)。"""
        return (f"步骤10 注入词 {len(self.inject_words)} 个 = "
                f"核心大词 {len(self.core)} 个 + 仅注入 {len(self.inject_only)} 个"
                + (f" − 不注入 {len(self.no_inject)} 个" if self.no_inject else "")
                + ("" if self.inject_only else "(未设 [仅注入] 分区)"))

    # ---------------------------------------------------------------- 文本
    def source_text(self) -> str:
        """分来源口径: `核心词 20 个(来源=人工清单; FR-07 达标 15 / 无判定 5)`。"""
        src = MODE_LABELS.get(self.mode, self.mode)
        return (f"核心词 {len(self.core)} 个(来源={src}; "
                f"FR-07 达标 {self.qualified} / 无判定 {self.no_verdict})")

    def gate_text(self) -> str:
        """门槛判定一行(日志/报告/面板三处同口径)。"""
        n = len(self.core)
        gate = max(1, int(self.gate or 1))
        if self.stopped:
            return "人工停止标记存在 → ❌ 中止步骤9(本轮不抓取搜索数据)"
        if self.is_manual:
            head = "人工清单生效"
        elif not self.gate_applies:
            return f"非美国站 → 不适用门槛; {self.source_text()} → " + ("✅继续" if n else "❌停止")
        else:
            head = "自动口径"
        ok = self.gate_ok
        return f"{head} → 核心词 {n} 个 {'≥' if ok else '<'} 阈值 {gate} → {'✅继续' if ok else '❌停止'}"

    def longtail_text(self) -> str:
        src = "人工清单" if self.longtail_manual_active else "自动(FR-04)"
        return f"长尾词 {len(self.longtail)} 个(来源={src})"


# -------------------------------------------------------------------- 解析

def resolve(cat_dir, cat: str, *, auto_core, auto_longtail,
            screen_verdict: dict | None = None, gate: int = 2,
            gate_applies: bool = True) -> Plan:
    """按三态文件解析出最终方案。任何文件读取失败都不抛异常(退化为自动态)。"""
    manual_core, inject_only, no_inject = read_core_sections3(_p(cat_dir, core_manual_name(cat)))
    manual_longtail = read_words(_p(cat_dir, longtail_manual_name(cat)))
    stopped = _p(cat_dir, stop_name(cat)).exists()
    if stopped:
        mode = MODE_STOP
    elif manual_core:
        mode = MODE_MANUAL
    else:
        mode = MODE_AUTO
    plan = Plan(
        mode=mode,
        core=list(manual_core) if mode == MODE_MANUAL else list(auto_core or []),
        longtail=list(manual_longtail) if manual_longtail else list(auto_longtail or []),
        inject_only=list(inject_only),
        no_inject=list(no_inject),
        auto_core=list(auto_core or []),
        auto_longtail=list(auto_longtail or []),
        manual_core=manual_core,
        manual_longtail=manual_longtail,
        screen_verdict=dict(screen_verdict or {}),
        gate=int(gate or 1),
        gate_applies=bool(gate_applies),
        stopped=stopped,
    )
    return plan


def write_used(cat_dir, cat: str, plan: Plan, *, site: str = "") -> str:
    """把本轮实际生效的词写成留痕文件(失败只返回空串, 不抛异常)。

    这份文件的价值: 事后能回答"这一轮到底搜了哪些词、是人工还是自动选的、
    门槛当时是怎么判的" —— 与步骤10 的 `<CAT>-injected-keywords.txt` 同类作用。
    """
    try:
        lines = [
            f"# {_USED_HEADER}",
            f"# 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            + (f"  站点: {site}" if site else ""),
            f"# {plan.source_text()}",
            f"# {plan.longtail_text()}",
            f"# {plan.inject_text()}",
            f"# 门槛判定: {plan.gate_text()}",
            "[核心大词]",
        ]
        lines += plan.core or ["(无)"]
        if plan.inject_only:
            lines += ["[仅注入]"]
            lines += plan.inject_only
        lines += ["[长尾词]"]
        lines += plan.longtail or ["(无)"]
        path = _p(cat_dir, used_name(cat))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)
    except Exception:                                   # noqa: BLE001 — 留痕不得阻断流程
        return ""


# -------------------------------------------------------------------- 写入(webui)

def set_list(cat_dir, cat: str, kind: str, mode: str, words=None) -> dict:
    """保存人工清单。`mode='auto'` 清掉该清单(回自动态)。

    kind:
      'core'     -> `filter-<CAT>-core-words.txt` 的 `[核心大词]` 分区(核心大词, 步骤9 抓取 + 步骤10 注入)
      'inject'   -> **同一文件**的 `[仅注入]` 分区(只给步骤10 注入, 不抓取、不进门槛)
      'noinject' -> **同一文件**的 `[不注入]` 分区(仍抓取/仍是核心, 但**不参与步骤10 判定**)
      'longtail' -> `filter-<CAT>-longtail-words.txt`(长尾词, 只用于步骤9 抓取/报告)

    core / inject / noinject 是**同一文件的三个分区**, 保存其中一个时其余原样保留
    (所以"清空核心大词"不会顺手删掉仅注入词, 反之亦然); 三个分区都空了才删文件。
    """
    if kind not in ("core", "inject", "noinject", "longtail"):
        return {"ok": False, "error": "list 只能是 core / inject / noinject / longtail"}
    is_auto = str(mode or MODE_AUTO).strip().lower() == MODE_AUTO

    # ---- 核心大词 / 仅注入词 / 不注入词(同一文件的三个分区) ----
    if kind in ("core", "inject", "noinject"):
        name = core_manual_name(cat)
        path = _p(cat_dir, name)
        cur_core, cur_inject, cur_noinject = read_core_sections3(path)
        limit = {"core": MAX_CORE, "inject": MAX_INJECT, "noinject": MAX_NOINJECT}[kind]
        label = {"core": "核心大词", "inject": "仅注入词", "noinject": "不注入词"}[kind]
        if is_auto:
            new_core, new_inject, new_noinject = cur_core, cur_inject, cur_noinject
            if kind == "core":
                new_core = []
            elif kind == "inject":
                new_inject = []
            else:
                new_noinject = []
        else:
            uniq = _norm_words(words)
            if len(uniq) > limit:
                return {"ok": False, "error": f"{label}最多 {limit} 个, 当前 {len(uniq)} 个"}
            new_core, new_inject, new_noinject = cur_core, cur_inject, cur_noinject
            if kind == "core":
                new_core = uniq
            elif kind == "inject":
                new_inject = uniq
            else:
                new_noinject = uniq
        c, i, x = write_core_sections3(path, new_core, new_inject, new_noinject)
        saved = {"core": c, "inject": i, "noinject": x}[kind]
        auto_now = not saved
        return {"ok": True, "mode": MODE_AUTO if auto_now else MODE_MANUAL,
                "file": name, "saved": saved,
                "core_saved": c, "inject_saved": i, "noinject_saved": x}

    # ---- 长尾词(独立文件) ----
    name = longtail_manual_name(cat)
    path = _p(cat_dir, name)
    if is_auto:
        remove_file(path)
        return {"ok": True, "mode": MODE_AUTO, "file": name, "saved": []}
    uniq = _norm_words(words)
    if len(uniq) > MAX_LONGTAIL:
        return {"ok": False, "error": f"长尾词最多 {MAX_LONGTAIL} 个, 当前 {len(uniq)} 个"}
    saved = write_words(path, uniq)
    return {"ok": True, "mode": MODE_MANUAL if saved else MODE_AUTO,
            "file": name, "saved": saved}


def set_stop(cat_dir, cat: str, stopped: bool) -> dict:
    """设置/清除"人工停止"标记。标记内容只写一行时间戳(不需要填理由)。"""
    path = _p(cat_dir, stop_name(cat))
    if not stopped:
        removed = remove_file(path)
        return {"ok": True, "stopped": False, "file": path.name, "removed": removed}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"写停止标记失败: {exc}"}
    return {"ok": True, "stopped": True, "file": path.name}


def status(cat_dir, cat: str) -> dict:
    """当前三态与文件存在性(webui 面板用)。"""
    return {
        "core_manual_file": core_manual_name(cat),
        "longtail_manual_file": longtail_manual_name(cat),
        "stop_file": stop_name(cat),
        "used_file": used_name(cat),
        "core_manual_exists": _p(cat_dir, core_manual_name(cat)).exists(),
        "longtail_manual_exists": _p(cat_dir, longtail_manual_name(cat)).exists(),
        "stopped": _p(cat_dir, stop_name(cat)).exists(),
        "max_core": MAX_CORE,
        "max_longtail": MAX_LONGTAIL,
        "max_inject": MAX_INJECT,
        "inject_section": INJECT_SECTION,
    }


def used_head(cat_dir, cat: str, limit: int = 6) -> list[str]:
    """读留痕文件的头部注释(面板显示"上一轮实际生效口径")。"""
    try:
        lines = Path(_p(cat_dir, used_name(cat))).read_text(
            encoding="utf-8-sig", errors="replace").splitlines()
    except Exception:                                   # noqa: BLE001
        return []
    return [ln.lstrip("# ").strip() for ln in lines[:limit] if ln.startswith("#")]
