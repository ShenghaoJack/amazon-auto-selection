"""跨步骤共享的长尾词规则(FR-04 生成 → FR-09 抓取)。

为什么要单独一个模块: 长尾的"维度 → 组装模板"与"五维实体台账"是**跨步骤契约** ——
FR-04 抽实体并组装长尾(基座=核心根词), FR-09 要用**搜索量最高的达标词**作基座重新组装
5 条抓取词(达标词只有 FR-07 之后才知道, 所以不能在 FR-04 里组装)。规则若在两个步骤里
各写一份, 迟早跑偏 —— 统一放这里, 两边 import。

台账格式(FR-04 写、FR-09 读, 纯文本可人工核对):
    # 注释行(# 开头, 解析时忽略)
    [场景.实体]
    1<TAB>kitchen<TAB>商品数=42<TAB>出现=55<TAB>来源=词表
    [场景.长尾词]
    # 兜底 0 条
    dog food storage for kitchen
"""
from __future__ import annotations

import re

# 五维度: dim -> (组装模式, 该维度抽什么)
#   for    = "<基座> for <实体>"(场景/人群)
#   prefix = "<实体> <基座>"(功能)
#   suffix = "<基座> <实体>"; 实体末词是容器/结构件名词时改用 "<基座> with <实体>"(属性/痛点)
DIM_RULES: dict[str, tuple[str, str]] = {
    "场景": ("for", "使用场景/地点(如 kitchen/travel/office/outdoor)"),
    "人群": ("for", "使用人群或对象(如 seniors/kids/beginners/pets)"),
    "功能": ("prefix", "功能/工作方式(如 electric/adjustable/collapsible/portable)"),
    "属性": ("suffix", "材质/规格/结构属性(如 stainless steel/large capacity/airtight lid)"),
    "痛点": ("suffix", "要解决的痛点(如 easy to clean/no mess/quiet/leak proof)"),
}

# suffix 模式下改用 "<基座> with <实体>" 的实体末词(容器/配件/结构件/功能件名词)
ATTR_NOUN_HEADS = {
    "lid", "lids", "cup", "burr", "filter", "handle", "handles", "scoop", "bag",
    "cover", "tray", "mat", "strap", "base", "ring", "seal", "pump", "nozzle",
    "container", "holder", "stand", "dispenser", "hook", "clips", "clip", "cap",
    "spout", "brush", "pouch", "caddy", "bin", "basket", "drawer", "shelf",
    "lock", "locks", "valve", "sensor", "remote", "gasket", "hinge", "cord",
    "mode", "modes", "groove", "timer", "panel", "display", "indicator",
}

# 纯"产品零部件"名词: 作为**单词实体**出现在 功能/属性 维度时剔除 ——
# 这类词(blade/bowl/motor…)在榜单文本里频率很高, 但拼出来的长尾是"配件搜索"
# (实战 Food Processors: "blade for food processor"/"vegetable chopper blade" 会搜到刀片而非整机)。
PART_NOUNS = {
    "blade", "blades", "bowl", "bowls", "motor", "motors", "jar", "jars",
    "brush", "mat", "cover", "pouch", "basket", "bin", "drawer", "shelf",
    "spout", "nozzle", "cord", "hinge", "gasket", "pump", "hook", "clip",
    "clips", "ring", "base", "stand", "holder", "caddy", "tray", "cap",
}

# 痛点维度: 出现这些词即视为"形容词性痛点" -> "<基座> <实体>"; 否则名词性痛点 -> "<基座> for <实体>"
PAIN_ADJ_TOKENS = {
    "easy", "non", "no", "without", "anti", "quiet", "low", "silent", "free",
    "friendly", "safe", "sturdy", "durable", "compact", "light", "toxic",
    "lasting", "saving", "proof", "resistant", "leakproof", "spillproof",
    "odorless", "odor", "smell", "mess", "spill",
}

# 长尾每条最大词数
LONGTAIL_MAX_WORDS = 8

# 形容词/分词尾(判定"功能/痛点"维度的实体是否为修饰语; 也用于痛点组装选模板)
ADJ_TAIL_RE = re.compile(
    r"(?:ic|al|ble|less|ing|ed|ful|ive|proof|free|duty|operated|mounted|standing"
    r"|release|button|hand|hands)$")

_STRIP_LEAD = re.compile(r"^(for|in|at|on)\s+")


def normalize(text: str) -> str:
    """与 FR-04 的本地规范化一致: 小写 / & -> and / 标点转空格 / 合并空白。"""
    text = str(text or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_token(token: str) -> str:
    """轻量复数归一(sets→set / purifiers→purifier / berries→berry), 用于词对齐。"""
    t = str(token or "").strip().lower()
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        return t[:-1]
    return t


def entity_in_base(entity: str, base: str) -> bool:
    """实体(按复数归一的词)是否已全部出现在基座词里。

    用于避免同义重复: base=cat water fountain 时, 人群维度的 cat/cats 再拼一次就是
    "cat water fountain for cat"; FR-04(基座=核心根词)与 FR-09(基座=达标词)都要判。
    """
    base_tokens = {norm_token(t) for t in str(base or "").split()}
    ent_tokens = [norm_token(t) for t in str(entity or "").split()]
    return bool(ent_tokens) and all(t in base_tokens for t in ent_tokens)


def assemble_entity(dim: str, entity: str, base: str) -> str:
    """把"维度实体"与"基座词"组装成长尾短语(按 DIM_RULES 的模板)。

    痛点维度按实体形态选模板(实战修正): 结构件名词 -> "with <实体>"(with safety lock),
    形容词 -> "<基座> <形容词>"(quiet), 名词性痛点 -> "<基座> for <实体>"(for pet dander)。
    """
    mode = DIM_RULES.get(dim, ("suffix", ""))[0]
    ent = str(entity or "").strip()
    if not ent or not base:
        return ""
    if mode == "for":
        ent = _STRIP_LEAD.sub("", ent)
        return normalize(f"{base} for {ent}")
    if mode == "prefix":
        last = ent.split()[-1] if ent.split() else ""
        if last in ATTR_NOUN_HEADS:            # 名词性功能件 -> "<基座> with <实体>"
            return normalize(f"{base} with {ent}")
        return normalize(f"{ent} {base}")
    last = ent.split()[-1] if ent.split() else ""
    if dim == "痛点":
        toks = ent.split()
        if last in ATTR_NOUN_HEADS:
            return normalize(f"{base} with {ent}")
        if any(t in PAIN_ADJ_TOKENS for t in toks):
            # 单词形容词 -> 前置("quiet air purifier"/"durable cutting board"), 短语 -> 后置("easy to clean")
            if len(toks) == 1:
                return normalize(f"{ent} {base}")
            return normalize(f"{base} {ent}")
        if ADJ_TAIL_RE.search(last) and not (len(toks) == 1 and last.endswith("ing")):
            return normalize(f"{base} {ent}")
        return normalize(f"{base} for {ent}")
    if last in ATTR_NOUN_HEADS and not ent.startswith("with "):
        return normalize(f"{base} with {ent}")
    return normalize(f"{base} {ent}")


def entity_section(dim: str, kind: str) -> str:
    """台账分区标题: kind = 实体 / 长尾词。"""
    return f"[{dim}.{kind}]"


def _to_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_entity_artifact(lines: list[str]) -> dict[str, dict]:
    """解析 FR-04 五维实体台账 -> {dim: {"entities": [{entity,records,occ,source}], "longtails": [...]}}。

    容错: 未知分区/脏行跳过; 缺维度则缺键(调用方按空处理)。
    """
    report: dict[str, dict] = {}
    dim = kind = ""
    for raw in lines or []:
        s = str(raw).strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            body = s[1:-1]
            name, _, tail = body.partition(".")
            dim, kind = name.strip(), (tail.strip() or "实体")
            report.setdefault(dim, {"entities": [], "longtails": []})
            continue
        if not dim:
            continue
        if kind == "长尾词":
            report[dim]["longtails"].append(s)
            continue
        parts = raw.split("\t")
        if len(parts) < 2 or not parts[1].strip():
            continue
        meta: dict[str, str] = {}
        for seg in parts[2:]:
            key, sep, val = seg.partition("=")
            if sep:
                meta[key.strip()] = val.strip()
        report[dim]["entities"].append({
            "entity": parts[1].strip(),
            "records": _to_int(meta.get("商品数")),
            "occ": _to_int(meta.get("出现")),
            "source": meta.get("来源", ""),
        })
    return report


def top_entities(report: dict, dim: str, n: int = 1) -> list[str]:
    """取某维度频率最高的前 n 个实体(台账已按频率降序, 这里只截断)。"""
    rows = (report.get(dim) or {}).get("entities") or []
    return [r["entity"] for r in rows[:max(0, n)] if r.get("entity")]


def crawl_words(report: dict, base: str, reject=None,
                per_dim: int = 1) -> list[tuple[str, str, str]]:
    """FR-09 抓取词: 每维取频率最高的**可用**实体 × 基座词 -> [(维度, 实体, 抓取词)]。

    "可用" = ①不与基座重复(entity_in_base, 避免 "cat water fountain for cat")
             ②组装结果非空 ③未命中 reject(配件/耗材词) ④与已选抓取词不重复。
    某个实体不可用时**顺位取该维下一个**实体(而不是直接放弃该维度)。
    """
    out: list[tuple[str, str, str]] = []
    if not base:
        return out
    for dim in DIM_RULES:
        rows = (report.get(dim) or {}).get("entities") or []
        picked = 0
        for row in rows:
            ent = str(row.get("entity") or "")
            if not ent or entity_in_base(ent, base):
                continue
            word = assemble_entity(dim, ent, base)
            if not word or (reject is not None and reject(word)):
                continue
            if any(word == w for _, _, w in out):
                continue
            out.append((dim, ent, word))
            picked += 1
            if picked >= max(1, per_dim):
                break
    return out
