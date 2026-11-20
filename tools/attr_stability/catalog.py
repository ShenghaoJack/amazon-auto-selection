# -*- coding: utf-8 -*-
"""沉淀的**类目级共享层 + 市场标记**（方案甲第 1 步，2026-10-09 用户口径）。

要解决的问题
===============================================================================
现在 `data/<CAT>/` 是**扁平**的，而 `CAT` 里带了市场+年月
（`Cutting Boards-US-202608`）。于是：

    Cutting Boards-US-202608    ← 定义层（属性集/词表/判据/映射）+ 运行层 混在一起
    Chopping Boards-UK-202608   ← 同一个商品类目的另一市场 → **沉淀零共享**

**同一个类目在不同市场/不同月份，全都从零开始** —— 用户要的"越跑越准"断了。

而"共享"和"不污染"是一对矛盾：美国站的口径不一定适用于德国站。
用户的口径是：**共享沉淀，但每条沉淀都标上它来自哪个市场** —— 读的时候按市场过滤，
这样既不重复劳动，又不会把一个市场的特殊口径灌给别人。

目标布局
===============================================================================
    data/
      _catalog/Cutting Boards/          ← 定义层（跨市场共享，每条带市场标记）
        _catalog.json                   ← 类目身份 + 用过哪些市场
        confirmed-attribute-spec.json   ← 当前唯一已确认属性集（含来源运行）
        cutting_boards.rules.json       ← 每个取值/口径/约束带市场标记
        learned-map.json
        词表聚类报告.json
      Cutting Boards-US-202608/         ← 运行层（**现状布局不变**）
      Chopping Boards-UK-202608/        ← 同样指向 _catalog/Cutting Boards

本模块**只做两件事**（第 1 步，不改变现有行为）
===============================================================================
  ① **路径解析**：`resolve_definition_dir()` —— 有 `_catalog/<类目>/` 就用它，
     **没有就退回运行目录**（向后兼容 → 现有 20 个目录行为完全不变）。
  ② **市场标记**：读/写/过滤。**没有标记的项一律视为"全球"** →
     所以对现有数据（没有任何标记）过滤后结果**逐字不变**。

⚠ 第 1 步**不做**：写回打标签、`_override.json`、Web 面板、约束过检回落。
   那些是第 2 步（会改变写回位置），要等第 1 步验证通过。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "CATALOG_DIR", "GLOBAL_MARKET", "MARKET_KEY", "VALUES_MARKET_KEY", "ATTR_POOL_FILE",
    "parse_cat", "catalog_dir_of", "resolve_definition_dir", "catalog_meta",
    "market_of", "is_visible", "filter_values", "filter_items",
    "values_market_map", "tag_values",
    "attr_pool_path", "load_attr_pool", "append_attr_pool",
    "CONFIRMED_SPEC_FILE", "CONFIRMED_RULES_FILE", "CONFIRMED_SPEC_POLICY",
    "resolve_spec_path", "resolve_rules_path", "resolve_learned_map_path",
    "resolve_definition_file",
    "parse_name_list",
]

# 定义层目录名（在 data_root 下）
CATALOG_DIR = "_catalog"
CONFIRMED_SPEC_FILE = "confirmed-attribute-spec.json"
CONFIRMED_RULES_FILE = "confirmed.rules.json"
CONFIRMED_SPEC_POLICY = "confirmed_only"
# 「全球」标记：没有标记 = 全球可见
GLOBAL_MARKET = "*"
# 通用市场标记键
MARKET_KEY = "_市场"
# `params.values` 的旁挂市场映射（**不能改 values 的类型** —— 十几个消费者按字符串读）
VALUES_MARKET_KEY = "_values_market"

# `Cutting Boards-US-202608` / `Chopping Boards-DE-202608`
_MONTH_RE = re.compile(r"^(?P<body>.+)-(?P<ym>\d{6})$")
_MARKET_RE = re.compile(r"^(?P<body>.+)-(?P<mk>[A-Z]{2})$")


def parse_cat(cat: Any) -> dict[str, str]:
    """把 `类目-市场-年月` 拆开。**认不出就整串当类目**（绝不猜）。

    实测的目录名形态：
      · `Cutting Boards-US-202608`    → 类目/Cutting Boards · 市场/US · 年月/202608
      · `Chopping Boards-UK-202608`   → 同上（**不同市场、同一个商品类目**）
      · `Air Purifiers for Home Pets-US-202608`
      · `salt and pepper mill sets`   → 无市场无年月 → 类目=全名
      · `your-category-US-YYYYMM`     → `YYYYMM` 不是 6 位数字 → 类目=全名
    """
    s = str(cat or "").strip()
    recent = bool(re.search(r"-RECENT30$", s, flags=re.I))
    if recent:
        s = re.sub(r"-RECENT30$", "", s, flags=re.I)
        s = re.sub(r"-(20\d{4})\d{2}$", r"-\1", s)
    out = {"类目": s, "市场": "", "年月": ""}
    if not s:
        return out
    m = _MONTH_RE.match(s)
    if not m:
        return out                                   # 没有 -YYYYMM → 整串当类目
    body, ym = m.group("body"), m.group("ym")
    mk = ""
    m2 = _MARKET_RE.match(body)
    if m2:
        body, mk = m2.group("body"), m2.group("mk")
    if not body.strip():
        return {"类目": s, "市场": "", "年月": ""}      # 拆到空 → 放弃
    out.update({"类目": body + (" [最近30天]" if recent else ""), "市场": mk, "年月": ym})
    return out


def catalog_dir_of(data_root: Any, 类目: str) -> Path:
    """定义层目录：`<data_root>/_catalog/<类目>/`（**不建目录**）。"""
    return Path(data_root) / CATALOG_DIR / str(类目)


def resolve_definition_dir(data_root: Any, cat: Any) -> Path:
    """**定义层目录解析**（第 1 步的核心，向后兼容）。

    规则（**顺序不能变**）：
      1. 若 `<data_root>/_catalog/<该类目的类目名>/` **已存在** → 用它（共享层）
      2. 否则 → **退回 `<data_root>/<cat>/`**（现状）

    ★ 第 2 条是**向后兼容的关键**：现有 20 个目录都还没有 `_catalog/`，
      第 1 步不迁移、不建目录 → 所有读写**仍然落在原处**，行为逐字不变。
    """
    dr = Path(data_root)
    p = parse_cat(cat)
    cand = catalog_dir_of(dr, p["类目"])
    if cand.is_dir():
        return cand
    return dr / str(cat)


def catalog_meta(data_root: Any, 类目: str) -> dict:
    """读 `_catalog/<类目>/_catalog.json`（类目身份 + 用过的市场清单）。

    不存在 → 返回一个**最小骨架**（不落盘）。第 1 步不写它。
    """
    f = catalog_dir_of(data_root, 类目) / "_catalog.json"
    base = {"类目": str(类目), "市场": [], "运行": []}
    if not f.is_file():
        return base
    try:
        o = json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return base
    if not isinstance(o, dict):
        return base
    # ⚠ 不要用**键白名单**：我第一版写成 `{k: v for k, v in o.items() if k in ("类目","市场",...)}`，
    #   结果 `排除属性` 这类**后来才加的键被静默丢掉**（测试立刻抓到：全类目级排除不生效）。
    #   `_catalog.json` 是本模块自己的元数据文件，整份返回、缺省值用骨架兜底即可。
    base.update(o)
    return base


# ---------------------------------------------------------------- 市场标记
def market_of(item: Any) -> list[str]:
    """取一个**条目**的市场标记；没有/空 → `[GLOBAL_MARKET]`（全球）。"""
    if isinstance(item, Mapping):
        v = item.get(MARKET_KEY)
    else:
        v = item
    if v is None or v == "":
        return [GLOBAL_MARKET]
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return [GLOBAL_MARKET]
    out = [str(x).strip() for x in v if str(x).strip()]
    return out or [GLOBAL_MARKET]


def is_visible(markets: Any, market: Any) -> bool:
    """`markets` 标记的条目，对 `market` 是否可见。

    ★ **缺省语义是"全球"**：没标记 / 空 / `None` / 含 `*` → **对所有市场可见**。
      这条是第 1 步"行为不变"的依据：现有数据没有任何标记 → 全部可见。
    `market` 为空（该类目没市场概念）→ 也一律可见（不能因为有标记就把没市场的类目挡掉）。

    ⚠ 踩过的坑（探针抓到的）：第一版写成
      `[str(x).strip() for x in (markets if isinstance(...) else [markets])]`
      → `markets=None` 时 `str(None)` = `"None"` → 被当成"有个叫 None 的标记"
      → `is_visible(None, 'US')` 变 **False** → 连带 `filter_values(值, None, 市场)`
      返回 **空列表** → **整个词表会被清空**。所以必须先单独判 `None` / 逐项滤 `None`。
    """
    if markets is None:
        return True
    src = markets if isinstance(markets, (list, tuple)) else [markets]
    mk = [str(x).strip() for x in src if x is not None and str(x).strip()]
    if not mk or GLOBAL_MARKET in mk:
        return True
    cur = str(market or "").strip()
    if not cur:
        return True
    return cur in mk


def filter_items(items: Sequence[Any], market: Any, *,
                 tag_of=None) -> list[Any]:
    """按市场过滤条目（`tag_of(item)` 取标记，缺省 `item[MARKET_KEY]`）。"""
    fn = tag_of or market_of
    return [it for it in (items or []) if is_visible(fn(it), market)]


def filter_values(values: Sequence[str], tags: Mapping[str, Any] | None,
                  market: Any) -> list[str]:
    """按市场过滤 `params.values`（**向后兼容的关键函数**）。

    `values` 是**字符串列表**（十几个消费者按字符串读，**不能改成对象**），
    市场标记放在旁挂映射 `params._values_market` 里：
        {"values": ["竹制", "钛金属"], "_values_market": {"钛金属": ["US"]}}
    → 在 `DE` 市场过滤后 = `["竹制"]`（竹制无标记=全球；钛金属只给 US）。

    **纯函数**（不读文件，只吃三个入参），方便单测。
    ⚠ **没有 tags 时逐字返回原列表** —— 这是第 1 步"行为不变"的依据。
    """
    t = tags if isinstance(tags, Mapping) else {}
    return [str(v) for v in (values or []) if is_visible(t.get(str(v)), market)]


def values_market_map(params: Any) -> dict[str, list[str]]:
    """从 `params` 里取 `_values_market` 旁挂映射（非法/缺失 → `{}`）。"""
    if not isinstance(params, Mapping):
        return {}
    m = params.get(VALUES_MARKET_KEY)
    if not isinstance(m, Mapping):
        return {}
    return {str(k): market_of(v) for k, v in m.items()}


def tag_values(params: dict, mapping: Mapping[str, Any]) -> dict:
    """把 `{值: [市场]}` 写进 `params._values_market`（**不碰 values**，只增改、可删全球标记）。

    · 标了**具体市场** → 写进旁挂映射
    · 标了**全球**（`*`）→ **删掉**该值的标记（缺省即全球，写 `["*"]` 只会制造噪音）
    返回写入后的 `params`（原地改）。
    """
    if not isinstance(params, dict) or not mapping:
        return params
    cur = params.get(VALUES_MARKET_KEY)
    cur = dict(cur) if isinstance(cur, Mapping) else {}
    for k, v in mapping.items():
        mks = market_of(v)
        # 全球不写（缺省即全球）—— 写 `["*"]` 会让文件里全是噪音
        if mks == [GLOBAL_MARKET]:
            cur.pop(str(k), None)
        else:
            cur[str(k)] = mks
    if cur:
        params[VALUES_MARKET_KEY] = cur
    else:
        params.pop(VALUES_MARKET_KEY, None)
    return params


# ---------------------------------------------------------------- 属性池
ATTR_POOL_FILE = "属性池.json"


def attr_pool_path(data_root: Any, 类目: str) -> Path:
    """`_catalog/<类目>/属性池.json` —— **多轮属性集的累积池**。"""
    return catalog_dir_of(data_root, 类目) / ATTR_POOL_FILE


def _round_markets(r: Mapping) -> list[str]:
    """取一轮的市场标记。

    ⚠ 踩过的坑（探针抓到）：**写入用 `"市场"`、读取用 `MARKET_KEY`(`"_市场"`)** →
      `r.get("_市场")` 永远 None → `is_visible(None, m)` = True → **市场过滤静默失效**
      （实测 `US`/`DE` 都看到全部轮次）。所以两个键**都认**，且以 `MARKET_KEY` 为准。
    """
    if MARKET_KEY in r:
        return market_of(r.get(MARKET_KEY))
    return market_of(r.get("市场")) if r.get("市场") else [GLOBAL_MARKET]


def load_attr_pool(data_root: Any, 类目: str, market: Any = None) -> list[dict]:
    """读属性池，按市场过滤后返回**每一轮的属性集**。

    **返回项的字段形状**（2026-10-10 补文档，此前只有实现）：
      · `市场`  —— **`、` 拼接的市场串**；**未标市场（全球 `*`）返回空串 `""`**
                   （表示"对所有市场可见"，不是缺失）。⚠ 多市场轮是 `"US、DE"`，
                   与"全球"的空串**在字符串上不可区分** —— 本字段只作展示/审计用，
                   **生产消费者（`step16._pool_merge`）只用 `属性`**。
      · `时间`  —— 写入时的 ISO 时间戳字符串
      · `属性`  —— `[{"name": …, "hint": …}]`

    ⚠ **只保留最近 `keep`（默认 30）轮**（`append_attr_pool` 的滑动窗口）：
      所以 `runs`（由调用方对返回轮次做并集得到）实际是「**最近 30 轮内的**命中轮数」，
      不是"历史总命中数"。老轮次被截断后 `runs` 会下降 —— 曾经"稳定"的属性可能
      降级成"偶然"被转人工（这是**有意**的"近期证据优先"，但**口径要知道**）。

    **为什么要有池子**（用户口径 2026-10-10：「可以把这三轮的提取的属性都给模型去判断和聚类」）：
    单轮冷启动抽出的属性集**每轮都不一样**（实测：同一份数据四轮，`品牌` 只有一轮抽到）。
    把多轮攒起来 → `runs`（命中轮数）就成了**稳定性信号**：
    多轮都抽到 = 稳；只一轮抽到 = 偶然（`品牌` 就是 `runs=1/4`）。
    **这比在代码里写死"品牌不要"更好** —— 规则会误伤真正有区分度的类目，而数据统计不会。

    `market` 给了就**只取对该市场可见的轮次**（各轮带市场标记，见方案甲）；不给则全取。
    """
    f = attr_pool_path(data_root, 类目)
    if not f.is_file():
        return []
    try:
        o = json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    rounds = o.get("轮次") if isinstance(o, dict) else None
    if not isinstance(rounds, list):
        return []
    out = []
    for r in rounds:
        if not isinstance(r, Mapping):
            continue
        attrs = r.get("属性")
        if not isinstance(attrs, list) or not attrs:
            continue
        if market is not None and not is_visible(_round_markets(r), market):
            continue
        # ★★ 修复（审核中1，2026-10-10）：原来 `str(r.get("市场") or "")` 读的键是 `"市场"`，
        #   但 `append_attr_pool` 写的是 `MARKET_KEY`(`_市场`) → 返回的市场**永远为空**。
        #   改用 `_round_markets(r)`（两个键都认，以 `_市场` 为准）。
        _mk = _round_markets(r)
        out.append({"市场": "、".join(m for m in _mk if m != GLOBAL_MARKET),
                    "时间": str(r.get("时间") or ""),
                    "属性": [{"name": str(a.get("name")), "hint": str(a.get("hint") or "")}
                             for a in attrs if isinstance(a, Mapping) and a.get("name")]})
    return out


def append_attr_pool(data_root: Any, 类目: str, attrs: Sequence[Mapping[str, Any]],
                     market: Any = None, when: str = "", keep: int = 30) -> Path:
    """把**这一轮的属性集**追加进池子（带市场标记），返回池子路径。

    · **同市场同属性集不重复追加** —— 重跑同一份数据不该把 `runs` 灌高（否则阈值失真）。
    · 只留最近 `keep` 轮（防文件无限长）。
    · `keep<=0` 表示不限制。
    """
    path = attr_pool_path(data_root, 类目)
    path.parent.mkdir(parents=True, exist_ok=True)
    old: dict = {}
    try:
        if path.is_file():
            o = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(o, dict):
                old = o
    except (OSError, ValueError):
        old = {}
    rounds = old.get("轮次") if isinstance(old.get("轮次"), list) else []
    # 市场标记一律存**规范化后的列表**（`["US"]` / `["*"]`）：
    #   ⚠ 键名必须与模块约定一致（`MARKET_KEY` = `_市场`）—— 我第一版这里写成中文 `"市场"`，
    #     测试立刻抓到读写的键对不上（`_round_markets` 两个键都认，以本键为准）。
    #   ⚠ 也不要用逗号拼串再解析：`market_of("US,DE")` 会当成**一个**市场（不拆），比较必不相等。
    mks = market_of(market) if market is not None else [GLOBAL_MARKET]
    item = {MARKET_KEY: mks, "时间": str(when or ""),
            "属性": [{"name": str(a.get("name")), "hint": str(a.get("hint") or "")}
                     for a in (attrs or []) if isinstance(a, Mapping) and a.get("name")]}

    def _sig(x: Mapping) -> str:
        return "|".join(sorted(str(a.get("name")) for a in (x.get("属性") or [])))

    if item["属性"] and not any(_sig(r) == _sig(item) and _round_markets(r) == mks
                                for r in rounds if isinstance(r, Mapping)):
        rounds.append(item)
    if keep and keep > 0:
        rounds = rounds[-keep:]
    path.write_text(json.dumps({
        "_说明": ("**多轮属性集累积池**：每跑一轮步骤16 追加一份（带市场标记）。"
                  "步骤16 读它 → 并集算 `runs`（命中轮数）→ `runs` 低的是偶然属性，"
                  "转人工确认；再由模型做同义归并。"
                  "**不要手工编辑** —— 它只是历史留痕，删了会让 `runs` 失真。"),
        "轮次": rounds,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# 历史属性池函数仅为旧脚本/数据迁移保留；步骤16不再调用。
# 当前共享基准仅在人工确认时由 spec_io.commit_attr_spec 发布。

# ---------------------------------------------------------------- 定义层文件解析
# ★★ 修复（审核格式#2，2026-10-10）：这里原来的注释写"共享层用**规范名**…**为什么必须规范名**"，
#   但 `resolve_spec_path` 的实现是**刻意不改文件名**（靠后缀 glob 共享，因为那个带 CAT 的
#   固定名被代码里 60+ 处引用，改名要动它们全部）—— **注释与实现正好相反**。
#   现在改成与实现一致，并删掉那三个从没被用上的 `*_CANON` 死常量。
#   共享层**保留原名**；靠后缀查找让同类目其他市场也能找到。


def _confirmed_shared_spec(path: Path, directory: Path) -> bool:
    """共享候选必须是目录内的非空已确认属性集；草稿/旧属性池不是继承基准。"""
    if path.resolve().parent != directory.resolve() or not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return (isinstance(data, dict) and data.get("status") == "confirmed"
            and isinstance(data.get("attributes"), list) and bool(data["attributes"]))


def resolve_spec_path(data_root: Any, cat: Any) -> Path:
    """本运行草稿优先；共享层仅继承已确认基准，启用新策略后不回退历史副本。"""
    dr = Path(data_root)
    own = dr / str(cat) / ("%s-attribute-spec.json" % cat)
    if own.is_file():
        return own
    category = parse_cat(cat)["类目"]
    cd = catalog_dir_of(dr, category)
    meta = catalog_meta(dr, category)
    declared = str(meta.get("属性集") or "").strip()
    if declared and _confirmed_shared_spec(cd / declared, cd):
        return cd / declared
    if meta.get("属性集策略") == CONFIRMED_SPEC_POLICY:
        return own
    exact = cd / ("%s-attribute-spec.json" % cat)
    if _confirmed_shared_spec(exact, cd):
        return exact
    candidates = [p for p in sorted(cd.glob("*-attribute-spec.json"))
                  if _confirmed_shared_spec(p, cd)]
    return candidates[0] if len(candidates) == 1 else own


def resolve_rules_path(data_root: Any, cat: Any, rules_name: str = "") -> Path:
    """规则文件路径。**自己的目录第一优先**（用户口径「肯定是自己的目录」）。

    解析顺序：① 自己目录里的（给的 `rules_name` 或唯一的 `*.rules.json`）
              ② `_catalog.json` 的 `规则` 显式声明
              ③ 共享层里本 CAT 的名字
              ④ 共享层里**恰好只有一份**候选 → 无歧义继承；多份**不猜**
    """
    dr = Path(data_root)
    run = dr / str(cat)
    if rules_name and (run / rules_name).is_file():
        return run / rules_name                                   # ① 自己的目录
    found = sorted(run.glob("*.rules.json")) if run.is_dir() else []
    if not rules_name and len(found) == 1:
        return found[0]
    p = parse_cat(cat)
    cd = catalog_dir_of(dr, p["类目"])
    if cd.is_dir():
        meta = catalog_meta(dr, p["类目"])
        declared = str(meta.get("规则") or "").strip()
        if meta.get("属性集策略") == CONFIRMED_SPEC_POLICY and not declared:
            return run / (rules_name or (p["类目"] + ".rules.json"))
        if declared and (cd / declared).is_file():
            return cd / declared                                  # ② 显式声明
        if rules_name and (cd / rules_name).is_file():
            return cd / rules_name                                # ③ 本市场在共享层的那份
        others = sorted(x for x in cd.glob("*.rules.json") if x.is_file())
        if len(others) == 1:
            return others[0]                                      # ④ 唯一候选
    # ★★ 修复（审核中3，2026-10-10）：原来 `return found[0] if found else ...` ——
    #   run 目录里**多份** `*.rules.json` 时会**静默取字母序第一个**（猜），与文档
    #   "多份不猜"和 `find_rules_file` 的"多份返回 None"口径不一致。
    #   现在**多份/找不到一律返回默认路径（可能不存在）**，由调用方 `is_file()` 判断 —— 不猜。
    return run / ("%s.rules.json" % (rules_name or p["类目"]))


def resolve_learned_map_path(data_root: Any, cat: Any) -> Path:
    """学习映射文件路径（**自己的目录第一优先**，与 spec/rules 同口径）。"""
    dr = Path(data_root)
    p = parse_cat(cat)
    own = dr / str(cat) / ("%s.learned-map.json" % cat)
    if own.is_file():
        return own
    cd = catalog_dir_of(dr, p["类目"])
    if cd.is_dir():
        exact = cd / ("%s.learned-map.json" % cat)
        if exact.is_file():
            return exact
        others = sorted(x for x in cd.glob("*.learned-map.json") if x.is_file())
        if len(others) == 1:
            return others[0]
    return own


def resolve_definition_file(cat_root: Any, name: Any) -> Path:
    """**接线用的统一入口**：把一个「类目根目录下的文件名」按沉淀规则解析成绝对路径。

    这是 `core/paths.py` / `steps/base.py` / `spec.find_rules_file` 共用的那一个函数 ——
    在**一处**做好，所有消费者就都会"两个抽屉都看一眼"。

    ★★ **只对定义层文件名生效**：
        · `*-attribute-spec.json`   → `resolve_spec_path`
        · `*.rules.json`            → `resolve_rules_path`
        · `*.learned-map.json`      → `resolve_learned_map_path`
        · `18_标准化/词表聚类报告.json` → 共享层同名文件优先
        · **其他任何名字** → 原样返回 `cat_root / name`
      → 所以交付表 / 稳定性结果 / 输入 json 等**行为逐字不变**（这是"不改 60 个调用点"的关键）。

    `data_root` 与 `cat` 由 `cat_root` **反推**（`cat_root.parent` / `cat_root.name`）——
      调用方不必改签名。
    """
    cr = Path(cat_root)
    n = str(name or "").replace("\\", "/")
    cat, dr = cr.name, cr.parent
    if n.endswith("-attribute-spec.json"):
        return resolve_spec_path(dr, cat)
    if n.endswith(".rules.json"):
        return resolve_rules_path(dr, cat, n)
    if n.endswith(".learned-map.json"):
        return resolve_learned_map_path(dr, cat)
    if n.endswith("词表聚类报告.json"):
        p = parse_cat(cat)
        shared = catalog_dir_of(dr, p["类目"]) / n
        return shared if shared.is_file() else (cr / n)
    return cr / n


# ---------------------------------------------------------------- 名单解析
def parse_name_list(raw: Any) -> list[str]:
    """把「名字清单」解析成去重后的字符串列表 —— **支持 str / list / dict 三种写法**。

    ★★ 为什么要有它（审核中9，2026-10-10）：`ATTR_EXCLUDE` 同一段解析逻辑被复制了 4 遍，
      而且只支持 str/list。用户按 `ATTR_DESC` 的风格写成 **dict**（如 `{"厚度": "US"}`，
      本意"厚度只排除美国站"）时，`str(dict)` → `"{'厚度': 'US'}"` → 被当成一个名字
      → **排除静默失效**（正是此功能要防的）。
      这里统一口径：
        · str   → 按 `；`/`;`/`,` 切分
        · list  → 逐项（项可以是 str，或带 `属性`/`name` 键的 dict）
        · dict  → **取 keys**（名字在 key 上，value 是市场等元信息）
      **去重、保序、去空**。
    """
    out: list[str] = []

    def _add(x: Any) -> None:
        if isinstance(x, Mapping):
            x = x.get("属性") or x.get("name")
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)

    if raw is None or raw == "":
        return out
    if isinstance(raw, Mapping):
        for k in raw.keys():
            _add(k)
    elif isinstance(raw, (list, tuple)):
        for x in raw:
            _add(x)
    else:
        for part in str(raw).replace("；", ";").replace(",", ";").split(";"):
            _add(part)
    return out

