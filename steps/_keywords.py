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


def load_verified_longtail_csv(csv_path) -> list[dict]:
    """读取 <CAT>-kwgen-candidates.csv 中已验证(is_longtail=True, relation=same_product)的长尾词。"""
    import csv
    from pathlib import Path

    out: list[dict] = []
    if not csv_path:
        return out
    p = Path(csv_path)
    if not p.is_file():
        return out
    try:
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                dim = str(row.get("dimension") or "").strip()
                word = str(row.get("keyword") or "").strip()
                is_lt = str(row.get("is_longtail") or "").strip().lower() in ("true", "1")
                if not (dim and word and is_lt and row.get("relation") == "same_product"):
                    continue
                try:
                    vol = float(str(row.get("searches") or 0).replace(",", "") or 0.0)
                except (TypeError, ValueError):
                    vol = 0.0
                out.append({"dimension": dim, "keyword": word, "searches": vol})
    except Exception:  # noqa: BLE001
        return []
    return out


_REL_STOP_WORDS = {
    "for", "with", "and", "of", "the", "a", "an", "to", "in", "on", "by", "at",
    "from", "or", "as", "is", "are", "be", "best", "new", "cheap", "sale",
    "clearance", "deals", "deal", "top", "rated", "review", "reviews", "kit",
    "2023", "2024", "2025", "2026", "2027",
}


def _stem_token(tok: str) -> str:
    t = str(tok or "").strip().lower()
    if len(t) <= 2 or t in _REL_STOP_WORDS:
        return ""
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def _content_tokens(phrase: str) -> set[str]:
    raw = re.findall(r"[^\W_]+", str(phrase or "").lower(), re.UNICODE)
    out = {_stem_token(t) for t in raw}
    out.discard("")
    return out


def select_verified_first_longtails(
    report: dict | None,
    base: str,
    qualified_core: list[str] | None = None,
    verified_rows: list[dict] | None = None,
    auto_tail: list[str] | None = None,
    core_candidates: list[str] | set[str] | None = None,
    auto_core: list[str] | set[str] | None = None,
    reject=None,
    limit: int = 5,
) -> list[tuple[str, str, str]]:
    """方案A(全选已验证优先 + 核心词增删动态关联): 返回最多 limit 条 [(维度, 来源说明, 抓取词)]。

    优先级与核心词关联规则:
      0. 核心词相关性过滤与加权:
         - 若用户删除了部分核心词，凡命中「被删核心词独有细分词根」(如删掉 espresso machine 后的 espresso)
           或仅匹配被删核心词而不匹配当前保留核心词的长尾词，一律自动排除;
         - 若用户新增了核心词，优先提权匹配新增核心词的已验证长尾词;
         - 所有入选长尾词必须与当前生效核心词(`qualified_core`)具备词根相关性。
      1. 五维已验证长尾词: 按 场景/人群/功能/属性/痛点 顺序，每维优先选 1 条与当前核心词相关的已验证长尾词;
      2. 已验证长尾词跨维补齐: 若不足 limit 条，从剩余与当前核心词相关的已验证长尾词(含 `其他 · 已验证`)
         按「新增核心词关联度 > 核心词匹配度 > 真实搜索量」降序补齐至 limit 条;
      3. 新增核心词专属兜底 & 未验证实体组词兜底:
         - 若用户手动新增了核心词且已验证池中无任何词覆盖该新增核心词，自动用该新增核心词组装 1 条五维实体长尾词;
         - 若已验证相关长尾词总数不足 limit 条，回退使用当前核心词基座 `crawl_words(report, base)` 补齐。
    """
    report = report or {}
    max_items = max(1, int(limit or 5))
    active_cores: list[str] = []
    for w in (qualified_core or []):
        s = str(w or "").strip().lower()
        if s and s not in active_cores:
            active_cores.append(s)
    if not active_cores and base:
        b_clean = str(base).strip().lower()
        if b_clean:
            active_cores.append(b_clean)
    active_set = set(active_cores)

    excluded: set[str] = {
        str(w).strip().lower()
        for w in list(qualified_core or []) + list(core_candidates or [])
        if str(w).strip()
    }
    seen: set[str] = set(excluded)

    # 计算活跃核心词词根、被删核心词词根、共享类目词根及新增核心词
    active_toks_map: dict[str, set[str]] = {c: _content_tokens(c) for c in active_cores}
    all_active_toks: set[str] = set().union(*active_toks_map.values()) if active_toks_map else set()

    ref_pool: list[str] = []
    for w in list(core_candidates or []) + list(auto_core or []) + active_cores:
        s = str(w or "").strip().lower()
        if s and s not in ref_pool:
            ref_pool.append(s)

    tok_doc_freq: dict[str, int] = {}
    for c in ref_pool:
        for t in _content_tokens(c):
            tok_doc_freq[t] = tok_doc_freq.get(t, 0) + 1
    if len(ref_pool) >= 4:
        shared_threshold = max(2, int(len(ref_pool) * 0.35))
    else:
        shared_threshold = 2 if len(ref_pool) >= 2 else 1
    shared_root_toks: set[str] = {
        t for t, freq in tok_doc_freq.items() if freq >= shared_threshold
    }

    removed_cores = [c for c in ref_pool if c not in active_set]
    removed_toks_map: dict[str, set[str]] = {c: _content_tokens(c) for c in removed_cores}
    removed_only_toks: set[str] = (
        (set().union(*removed_toks_map.values()) if removed_toks_map else set()) - all_active_toks
    )
    forbidden_subtype_toks: set[str] = removed_only_toks - shared_root_toks

    baseline_set: set[str] = {
        str(w).strip().lower()
        for w in (auto_core if auto_core is not None else (core_candidates or []))
        if str(w).strip()
    }
    added_cores: list[str] = [c for c in active_cores if baseline_set and c not in baseline_set]
    added_set = set(added_cores)

    def _core_relevance(word: str) -> tuple[bool, int, str]:
        """判断长尾词是否与当前核心词相关且不命中被删核心词，返回 (是否可用, 关联分, 匹配的核心词)。"""
        if not active_cores:
            return True, 1, ""
        w_toks = _content_tokens(word)
        if not w_toks:
            return False, 0, ""
        # 1) 命中被删核心词的独有细分词根(如删掉 espresso machine 后的 espresso) -> 直接排除
        if w_toks & forbidden_subtype_toks:
            return False, 0, ""
        # 2) 命中被删核心词的共享词根(如删掉 coffee machine 仅留 coffee maker 后的 machine)，
        #    除非该长尾词同时完整包含某条当前保留核心词的全部词根，否则排除
        if (w_toks & removed_only_toks) and not any(
            c_toks and c_toks <= w_toks for c_toks in active_toks_map.values()
        ):
            return False, 0, ""
        # 3) 必须与至少一条当前生效核心词正相关
        best_score = 0
        matched_core = ""
        for c in active_cores:
            c_toks = active_toks_map.get(c) or set()
            if not c_toks:
                continue
            c_sub = c_toks - shared_root_toks
            bonus = 2 if c in added_set else 0
            score = 0
            if c_toks <= w_toks:
                score = 3 + bonus
            elif c_sub:
                if c_sub <= w_toks and len(w_toks & c_toks) >= max(1, len(c_toks) - 1):
                    score = 2 + bonus
            else:
                head_tok = max(c_toks, key=len)
                if head_tok in w_toks and len(w_toks & c_toks) >= max(1, len(c_toks) - 1):
                    score = 1 + bonus
            if score > best_score:
                best_score = score
                matched_core = c
        return (best_score > 0), best_score, matched_core

    def _can_pick(word: str) -> bool:
        w = str(word or "").strip()
        if not w:
            return False
        wl = w.lower()
        if wl in seen:
            return False
        if reject is not None and reject(w):
            return False
        ok, _, _ = _core_relevance(w)
        return ok

    vol_map: dict[str, float] = {}
    for r in verified_rows or []:
        kw = str(r.get("keyword") or "").strip().lower()
        if kw:
            try:
                vol_map[kw] = max(vol_map.get(kw, 0.0), float(r.get("searches") or 0.0))
            except (TypeError, ValueError):
                pass

    # 构建各维度已验证词表(台账 `[维度.长尾词]` 优先，再合并 CSV 同维度已验证长尾词)
    dim_verified: dict[str, list[str]] = {d: [] for d in DIM_RULES}
    for d in DIM_RULES:
        for w in (report.get(d) or {}).get("longtails") or []:
            ws = str(w or "").strip()
            if ws and ws.lower() not in {x.lower() for x in dim_verified[d]}:
                dim_verified[d].append(ws)
    for r in verified_rows or []:
        d = str(r.get("dimension") or "").strip()
        ws = str(r.get("keyword") or "").strip()
        if d in dim_verified and ws and ws.lower() not in {x.lower() for x in dim_verified[d]}:
            dim_verified[d].append(ws)

    picked: list[tuple[str, str, str]] = []

    # ① 第一优先级: 五维已验证长尾词，每维按(核心词关联分, 搜索量)降序先取 1 条
    for d in DIM_RULES:
        candidates_in_dim: list[tuple[int, float, int, str]] = []
        for idx_w, w in enumerate(dim_verified.get(d, [])):
            if _can_pick(w):
                _, rel_score, _ = _core_relevance(w)
                candidates_in_dim.append((rel_score, vol_map.get(w.lower(), 0.0), idx_w, w))
        if candidates_in_dim:
            candidates_in_dim.sort(key=lambda item: (-item[0], -item[1], item[2]))
            _, _, _, best_w = candidates_in_dim[0]
            vol = int(vol_map.get(best_w.lower(), 0))
            src = f"已验证长尾词[{d}]" + (f" 月搜={vol}" if vol > 0 else "")
            picked.append((d, src, best_w))
            seen.add(best_w.lower())
        if len(picked) >= max_items:
            break

    # ② 第二优先级: 剩余全部已验证长尾词(含 `其他 · 已验证` 及各维剩余已验证词)按(关联分, 搜索量)降序补齐
    pool_candidates: list[tuple[int, float, int, str, str]] = []
    pool_seen: set[str] = set()
    idx = 0
    for r in verified_rows or []:
        d = str(r.get("dimension") or "").strip() or "其他"
        w = str(r.get("keyword") or "").strip()
        wl = w.lower()
        if w and wl not in pool_seen and _can_pick(w):
            pool_seen.add(wl)
            _, rel_score, _ = _core_relevance(w)
            pool_candidates.append((rel_score, vol_map.get(wl, 0.0), idx, d, w))
            idx += 1
    for d in DIM_RULES:
        for w in dim_verified.get(d, []):
            wl = w.lower()
            if w and wl not in pool_seen and _can_pick(w):
                pool_seen.add(wl)
                _, rel_score, _ = _core_relevance(w)
                pool_candidates.append((rel_score, vol_map.get(wl, -1.0), idx, d, w))
                idx += 1
    for w in auto_tail or []:
        ws = str(w or "").strip()
        wl = ws.lower()
        if ws and wl not in pool_seen and _can_pick(ws):
            pool_seen.add(wl)
            _, rel_score, _ = _core_relevance(ws)
            pool_candidates.append((rel_score, vol_map.get(wl, -2.0), idx, "已验证", ws))
            idx += 1

    pool_candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
    for _, vol, _, d, w in pool_candidates:
        if len(picked) >= max_items:
            break
        if _can_pick(w):
            v_int = int(vol) if vol > 0 else 0
            src = f"已验证长尾词补位[{d}]" + (f" 月搜={v_int}" if v_int > 0 else "")
            picked.append((d, src, w))
            seen.add(w.lower())

    # ②-b 若用户手动新增了核心词(added_cores)，且已选长尾词中尚未覆盖该新增核心词，
    #      优先从已验证池或实体组词中为新增核心词配齐相关长尾词
    if added_cores and report:
        for c_add in added_cores[:2]:
            c_add_toks = active_toks_map.get(c_add) or set()
            if not c_add_toks:
                continue
            already_covered = any(c_add_toks <= _content_tokens(w) for _, _, w in picked)
            if already_covered:
                continue
            add_pairs = crawl_words(report, c_add, reject=reject, per_dim=1)
            for d, ent, w in add_pairs:
                if _can_pick(w):
                    rows = (report.get(d) or {}).get("entities") or []
                    records = next((r.get("records", 0) for r in rows if r.get("entity") == ent), 0)
                    entry = (d, f"新增核心词关联组词(实体 {ent} {records}品 × 新增核心 {c_add})", w)
                    if len(picked) < max_items:
                        picked.append(entry)
                    elif max_items >= 2:
                        picked[-1] = entry
                    seen.add(w.lower())
                    break

    # ③ 第三优先级: 仅当与当前核心词相关的已验证长尾词不足 limit 时，才用当前核心词的五维实体组词兜底
    if len(picked) < max_items and report:
        base_candidates = [base] if base else []
        for c in active_cores:
            if c not in base_candidates:
                base_candidates.append(c)
        for b_word in base_candidates:
            if len(picked) >= max_items or not b_word:
                break
            covered_dims = {d for d, _, _ in picked}
            raw_pairs = crawl_words(report, b_word, reject=reject, per_dim=1)
            ordered_pairs = (
                [p for p in raw_pairs if p[0] not in covered_dims]
                + [p for p in raw_pairs if p[0] in covered_dims]
            )
            for d, ent, w in ordered_pairs:
                if len(picked) >= max_items:
                    break
                if _can_pick(w):
                    rows = (report.get(d) or {}).get("entities") or []
                    records = next((r.get("records", 0) for r in rows if r.get("entity") == ent), 0)
                    picked.append((d, f"未验证实体组词兜底(实体 {ent} {records}品 × 基座 {b_word})", w))
                    seen.add(w.lower())

    return picked

