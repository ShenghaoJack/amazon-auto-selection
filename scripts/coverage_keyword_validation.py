#!/usr/bin/env python3
"""coverage_keyword_validation.py — 覆盖率六维打分 + 角色分类验证(对照 skill 流程)。

把 skill 的"榜单语料证据化选词"逻辑落成本地脚本, 在保留商品(标题+五点)上
跑确定性覆盖率回测, 验证: 稳定抽出的核心词与现有 LLM 候选差多少。

六维指标(skill Analysis contract 1):
  标题覆盖率 title_cov      = 标题含词组的去重商品数 / 总商品数
  全文覆盖率 fulltext_cov   = 标题或五点含词组的去重商品数 / 总商品数
  提及次数 mentions         = 全文出现次数合计
  BS 覆盖率 bs_cov          = BS 榜商品内命中率
  NR 覆盖率 nr_cov          = NR 榜商品内命中率
  NR-BS 差 nr_bs_diff       = nr_cov - bs_cov(百分点)
角色分类(skill Analysis contract 3):
  广域根词(发现词) / 品类泛词 / 精准功能(属性/功能) / 完整意图(场景/人群) / 商标竞品品牌

用法:
  python scripts/coverage_keyword_validation.py --cat <CAT> [--extra-cat <CAT2>] [--top N]
输出:
  data/<CAT>/<CAT>-coverage-keyword-validation.md      验证报告
  data/<CAT>/<CAT>-coverage-keyword-candidates.csv     全候选打分表
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, OrderedDict
from datetime import date
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

# ---------------- 词汇表 ----------------
CORE_NOUNS = {
    "salt", "pepper", "shaker", "shakers", "mill", "mills",
    "grinder", "grinders", "spice", "spices", "seasoning", "seasonings",
}
MINED_CORE = {
    "salt", "pepper", "shaker", "shakers", "mill", "mills",
    "grinder", "grinders", "spice", "spices",
}
# 完整意图: 场景 / 人群
SCENARIO_WORDS = {
    "kitchen", "travel", "camping", "camp", "picnic", "bbq", "barbecue",
    "outdoor", "restaurant", "home", "office", "party", "table", "dining",
    "gift", "gifts", "holiday", "cooking", "grill",
}
PERSONA_WORDS = {
    "senior", "seniors", "chef", "chefs", "kids", "child", "children",
    "adult", "adults", "traveler", "travelers", "camper", "campers",
    "elderly", "arthritis", "weak hands", "left handed",
}
# 精准功能: 属性 / 材质 / 功能
FUNC_WORDS = {
    "glass", "ceramic", "stainless", "steel", "wooden", "wood", "bamboo",
    "acrylic", "plastic", "porcelain", "stoneware", "mini", "small", "tiny",
    "vintage", "farmhouse", "rustic", "modern", "electric", "manual",
    "adjustable", "automatic", "battery", "portable", "heavy", "large",
    "led", "magnetic", "cork", "lid", "lids", "pour", "refill", "refillable",
    "insulated", "silicone", "walnut", "acacia", "marble", "countertop",
    "compact", "one handed", "dispenser",
}
BRAND_WORDS = [
    "oxo", "peugeot", "le creuset", "cuisinart", "kitchenaid", "zwilling",
    "zassenhaus", "cole mason", "hario", "morton", "mccormick", "ebaco",
    "generic", "tramontina", "joseph joseph", "wusthof",
]
# 结构性虚词(根词判定时剔除)
STRUCT_STOPS = {"and", "or", "the", "a", "an", "set", "sets", "with", "of"}
TRAIL_STOPS = {
    "with", "and", "or", "for", "of", "the", "a", "an", "to", "in", "on",
    "by", "at", "from",
}
# 词组收尾名词(决定 n-gram 是否为"可独立成词"的搜索短语)
SHAPE_NOUNS = {
    "shaker", "shakers", "mill", "mills", "grinder", "grinders",
    "set", "sets", "holder", "jar", "bottle", "dispenser", "container",
}

# 锚定根词(即使支持数低也必须保留在考虑范围, skill: 大类根词不因精准度低而丢弃)
ROOT_ANCHORS = [
    "salt and pepper shaker",
    "salt and pepper shakers",
    "salt and pepper shakers set",
    "salt shaker",
    "salt shakers",
    "pepper shaker",
    "pepper shakers",
    "salt and pepper",
    "salt and pepper mill",
    "salt and pepper grinder",
]


def smart_norm(text: str) -> str:
    """skill 风格标准化: 小写 / & -> and / 非字母数字转空格 / 合并空白。"""
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def naive_norm(text: str) -> str:
    """step04 现用标准化(不带 & -> and), 用于对比 & 造成的覆盖率损失。"""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def read_jsonl_robust(path: Path) -> list[dict]:
    """整文件 raw_decode 逐条解析, 容忍字符串内含换行。"""
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    decoder = json.JSONDecoder()
    idx, n, out = 0, len(raw), []
    while idx < n:
        while idx < n and raw[idx] in " \r\n\t":
            idx += 1
        if idx >= n:
            break
        try:
            obj, idx = decoder.raw_decode(raw, idx)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            out.append(obj)
    return out


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))


def read_lines(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if ln.strip()]


def word_ngrams(words: list[str], n_min: int = 2, n_max: int = 8) -> list[str]:
    out = []
    for n in range(n_min, min(n_max, len(words)) + 1):
        for i in range(len(words) - n + 1):
            out.append(" ".join(words[i:i + n]))
    return out


def trim_phrase(phrase: str) -> str:
    """两端剥除虚词(可剥到 1 词, 由 _keep_phrase 再判定多词)。"""
    words = phrase.split()
    while words and words[0] in TRAIL_STOPS:
        words = words[1:]
    while words and words[-1] in TRAIL_STOPS:
        words = words[:-1]
    return " ".join(words)


def classify_role(phrase: str) -> str:
    """四角色 + 商标标记(skill Analysis contract 3 的本地启发式)。"""
    words = phrase.split()
    for brand in BRAND_WORDS:
        if re.search(rf"\b{re.escape(brand)}\b", phrase):
            return "商标/竞品品牌(剔除)"
    if any(w in SCENARIO_WORDS or w in PERSONA_WORDS for w in words):
        return "完整意图(场景/人群)"
    if any(w in FUNC_WORDS for w in words):
        return "精准功能(属性/功能)"
    core = [w for w in words if w not in STRUCT_STOPS]
    if core and all(w in CORE_NOUNS for w in core):
        return "广域根词(发现词)"
    return "品类泛词(泛化)"


def mine_pool(records: list[dict], smart: bool) -> dict[str, dict]:
    """确定性候选池: 标题 n-gram(商品支持>=2) ∪ 五点 n-gram(商品支持>=2)。

    返回 {phrase: {"title_products": set, "full_products": set, "mentions": int}}
    其中 mentions 为全文出现次数合计(仅用于已进入池的词)。
    """
    title_products: dict[str, set] = OrderedDict()
    bullet_products: dict[str, set] = OrderedDict()
    mentions: Counter = Counter()
    for i, rec in enumerate(records):
        title = smart_norm(str(rec.get("title") or ""))
        bullets_raw = rec.get("bullets") or rec.get("五点") or []
        if isinstance(bullets_raw, str):
            bullets_raw = [bullets_raw]
        bullets = [smart_norm(str(b)) for b in bullets_raw if str(b).strip()]

        seen_title = set()
        for ng in word_ngrams(re.findall(r"[a-z0-9]+", title)):
            ng = trim_phrase(ng)
            if _keep_phrase(ng):
                if ng not in seen_title:
                    title_products.setdefault(ng, set()).add(i)
                    seen_title.add(ng)
                mentions[ng] += title.count(ng)
        seen_bullet = set()
        for b in bullets:
            for ng in word_ngrams(re.findall(r"[a-z0-9]+", b)):
                ng = trim_phrase(ng)
                if _keep_phrase(ng) and ng not in seen_bullet:
                    bullet_products.setdefault(ng, set()).add(i)
                    seen_bullet.add(ng)
                mentions[ng] += b.count(ng)
    pool: dict[str, dict] = {}
    for phrase, prods in title_products.items():
        if len(prods) >= 2:
            pool[phrase] = {"title_products": prods, "full_products": set(prods), "mentions": mentions[phrase]}
    for phrase, prods in bullet_products.items():
        if len(prods) >= 2 and phrase not in pool:
            pool[phrase] = {"title_products": set(), "full_products": set(prods), "mentions": mentions[phrase]}
    return pool


def _keep_phrase(phrase: str) -> bool:
    """词池过滤: 多词 / 收尾名词或完整意图形态 / 含类目核心名词 / 非品牌词。

    - 收尾名词形态: ...shaker(s)/mill(s)/grinder(s)/set(s)/holder/... (可独立成搜索词)
    - 完整意图形态: 含 'for' 且以场景/人群词收尾(如 '... for kitchen')
    - 根词锚定: 'salt and pepper' 等由锚定列表单独注入, 此处不拦
    """
    words = phrase.split()
    if len(words) < 2:
        return False
    if not any(w in MINED_CORE for w in words):
        return False
    for brand in BRAND_WORDS:
        if re.search(rf"\b{re.escape(brand)}\b", phrase):
            return False
    last = words[-1]
    if last in SHAPE_NOUNS:
        return True
    if "for" in words and (last in SCENARIO_WORDS or last in PERSONA_WORDS):
        return True
    return False


def score_phrase(
    phrase: str,
    records: list[dict],
    texts_smart: list[tuple[str, str]],
    texts_naive: list[tuple[str, str]],
    bs_set: set,
    nr_set: set,
) -> dict:
    """计算六维指标; texts_*: (title_norm, full_norm) 平行列表。"""
    n = max(len(records), 1)
    asins = [str(r.get("request_asin") or "") for r in records]
    bs_total = sum(1 for a in asins if a in bs_set)
    nr_total = sum(1 for a in asins if a in nr_set)

    title_hits: list[int] = []
    full_hits: list[int] = []
    mentions = 0
    naive_title_hits = 0
    for i, (t, f) in enumerate(texts_smart):
        if phrase in t:
            title_hits.append(i)
        if phrase in f:
            full_hits.append(i)
        mentions += f.count(phrase)
    for (t, _) in texts_naive:
        if phrase in t:
            naive_title_hits += 1

    bs_hits = sum(1 for i in full_hits if asins[i] in bs_set)
    nr_hits = sum(1 for i in full_hits if asins[i] in nr_set)
    return {
        "phrase": phrase,
        "role": classify_role(phrase),
        "title_support": len(title_hits),
        "title_cov": len(title_hits) / n,
        "naive_title_cov": naive_title_hits / n,
        "fulltext_support": len(full_hits),
        "fulltext_cov": len(full_hits) / n,
        "mentions": mentions,
        "bs_cov": (bs_hits / bs_total) if bs_total else None,
        "nr_cov": (nr_hits / nr_total) if nr_total else None,
        "nr_bs_diff": ((nr_hits / nr_total) - (bs_hits / bs_total)) if (bs_total and nr_total) else None,
        "bs_total": bs_total,
        "nr_total": nr_total,
    }


def fmt_pct(v) -> str:
    if v is None:
        return ""
    return f"{v:.1%}"


def fmt_pp(v) -> str:
    if v is None:
        return ""
    return f"{v:+.1%}"


def jaccard(a: set, b: set) -> float:
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def load_volumes(cat_dir: Path) -> dict[str, dict]:
    path = cat_dir / f"{cat_dir.name}-amazon-keyword-data.json"
    if not path.exists():
        return {}
    data = read_json(path)
    out = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            kw = smart_norm(str(item.get("keyword") or ""))
            if kw:
                out[kw] = {
                    "vol": item.get("monthly_search_volume"),
                    "cpc": item.get("cpc"),
                    "mom_yoy": item.get("mom_yoy"),
                }
    return out


def load_core_words(cat_dir: Path) -> list[str]:
    path = cat_dir / f"{cat_dir.name}-keywords.txt"
    if not path.exists():
        return []
    lines = read_lines(path)
    in_core = False
    core = []
    for ln in lines:
        if ln.startswith("[核心大词]"):
            in_core = True
            continue
        if ln.startswith("[") and in_core:
            break
        if in_core and ln:
            core.append(smart_norm(ln))
    return core


def load_bsnr(cat_dir: Path) -> tuple[set, set]:
    path = cat_dir / f"{cat_dir.name}-bestseller-newrelease.json"
    if not path.exists():
        return set(), set()
    data = read_json(path)
    bs = {str(a).strip() for a in (data.get("bs") or []) if str(a).strip()}
    nr = {str(a).strip() for a in (data.get("nr") or []) if str(a).strip()}
    return bs, nr


def build_records(cat_dir: Path) -> list[dict]:
    path = cat_dir / f"{cat_dir.name}-dual-details-filtered.jsonl"
    if not path.exists():
        sys.exit(f"[error] 未找到过滤后榜单详情: {path}")
    records = [r for r in read_jsonl_robust(path) if r.get("title") or r.get("bullets")]
    if not records:
        sys.exit(f"[error] 无有效记录: {path}")
    return records


def analyze_cat(cat: str, data_dir: Path, top: int) -> dict:
    cat_dir = data_dir / cat
    records = build_records(cat_dir)
    bs_set, nr_set = load_bsnr(cat_dir)
    existing = [smart_norm(p) for p in read_lines(cat_dir / f"{cat}-keywords-candidates.txt")]
    core = load_core_words(cat_dir)
    volumes = load_volumes(cat_dir)

    texts_smart = []
    texts_naive = []
    for r in records:
        title = str(r.get("title") or "")
        bullets_raw = r.get("bullets") or r.get("五点") or []
        if isinstance(bullets_raw, str):
            bullets_raw = [bullets_raw]
        body = " ".join(str(b) for b in bullets_raw if str(b).strip())
        full = f"{title} {body}" if body else title
        texts_smart.append((smart_norm(title), smart_norm(full)))
        texts_naive.append((naive_norm(title), naive_norm(full)))

    # 确定性挖掘
    mined = mine_pool(records, smart=True)
    mined_phrases = list(mined.keys())

    # 全池打分: 挖掘 + 根词锚定 + 现有候选 + 核心词
    all_phrases = list(OrderedDict.fromkeys(mined_phrases + ROOT_ANCHORS + existing + core))
    full_scored = {
        c["phrase"]: c
        for c in [score_phrase(p, records, texts_smart, texts_naive, bs_set, nr_set) for p in all_phrases]
    }
    # 挖掘支持信息(用于标注"是否可被确定性挖掘命中")
    for p, info in mined.items():
        if p in full_scored:
            full_scored[p]["mined_title_support"] = len(info["title_products"])
            full_scored[p]["mined_full_support"] = len(info["full_products"])
    for p in all_phrases:
        full_scored[p].setdefault("mined_title_support", 0)
        full_scored[p].setdefault("mined_full_support", 0)

    # 确定性 Top: 挖掘命中的词 + 有标题证据的根词锚定, 按指标排序
    anchor_set = set(ROOT_ANCHORS)
    det_pool = [
        full_scored[p]
        for p in all_phrases
        if p in mined or (p in anchor_set and full_scored[p]["title_support"] >= 1)
    ]
    det_pool.sort(key=lambda c: (-c["title_cov"], -c["fulltext_cov"], -c["mentions"], len(c["phrase"])))
    det_top = det_pool[:top]
    det_top_set = {c["phrase"] for c in det_top}

    for p in existing:
        c = full_scored[p]
        c["in_existing"] = True
        c["in_det_top"] = p in det_top_set
        c["in_core"] = p in set(core)
        c["vol"] = (volumes.get(p) or {}).get("vol")
    for c in det_top:
        c.setdefault("in_existing", c["phrase"] in set(existing))
        c.setdefault("in_core", c["phrase"] in set(core))
        c.setdefault("in_det_top", True)

    existing_scored = [full_scored[p] for p in existing if p in full_scored]
    missing_in_det = [p for p in existing if p not in det_top_set]
    det_new = [c for c in det_top if not c.get("in_existing")]

    bs_same_nr = bool(bs_set) and bs_set == nr_set
    if bs_same_nr:
        # 两榜无区分度: BS/NR/NR-BS 如实记数据不足(空白), 不虚构
        for c in full_scored.values():
            c["bs_cov"] = c["nr_cov"] = c["nr_bs_diff"] = None

    return {
        "cat": cat,
        "records": records,
        "texts_smart": texts_smart,
        "bs_set": bs_set,
        "nr_set": nr_set,
        "bs_same_nr": bs_same_nr,
        "existing": existing,
        "core": core,
        "volumes": volumes,
        "det_top": det_top,
        "det_top_set": det_top_set,
        "full_scored": full_scored,
        "existing_scored": existing_scored,
        "missing_in_det": missing_in_det,
        "det_new": det_new,
    }


def write_csv(cat: str, data_dir: Path, full_scored: dict) -> None:
    path = data_dir / cat / f"{cat}-coverage-keyword-candidates.csv"
    rows = sorted(full_scored.values(), key=lambda c: (-c["title_cov"], -c["fulltext_cov"], -c["mentions"]))
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "词组", "角色", "标题覆盖率", "全文覆盖率", "标题命中数", "全文命中数",
            "提及次数", "BS覆盖率", "NR覆盖率", "NR-BS差", "现有候选", "确定性Top", "核心词",
            "naive标题覆盖率(&损失)", "月搜索量",
        ])
        for c in rows:
            w.writerow([
                c["phrase"], c["role"], fmt_pct(c["title_cov"]), fmt_pct(c["fulltext_cov"]),
                c["title_support"], c["fulltext_support"], c["mentions"],
                fmt_pct(c["bs_cov"]), fmt_pct(c["nr_cov"]), fmt_pp(c["nr_bs_diff"]),
                "是" if c.get("in_existing") else "",
                "是" if c.get("in_det_top") else "",
                "是" if c.get("in_core") else "",
                fmt_pct(c["naive_title_cov"]),
                c.get("vol") if c.get("vol") is not None else "",
            ])
    return path


def write_md(res: dict, top: int, extra_res: dict | None = None) -> Path:
    cat = res["cat"]
    data_dir = res["_data_dir"]
    records = res["records"]
    n = len(records)
    lines: list[str] = []
    add = lines.append
    add(f"# {cat} 覆盖率六维打分 + 角色分类验证报告")
    add("")
    add(f"- 生成日期: {date.today().isoformat()}")
    add(f"- 语料: FR-03 过滤后榜单详情 {n} 条(标题+五点)")
    add(f"- 确定性 Top 取前 {top}(对齐 CANDIDATE_LIMIT)")
    add("")

    add("## 1. 语料与分组")
    add("")
    if res["bs_same_nr"]:
        add(f"- ⚠️ FR-00 双榜产物中 bs 与 nr 集合完全相同({len(res['bs_set'])} == {len(res['nr_set'])}), "
            "本环境无法区分两榜成员 -> **NR-BS 差无可计算信息, 按 skill 规则记录为数据不足, 不虚构**。")
    else:
        add(f"- FR-00 双榜: BS {len(res['bs_set'])} / NR {len(res['nr_set'])} 独立集合, 正常计算 BS/NR 覆盖率。")
    def _bullets_of(r):
        b = r.get("bullets") or r.get("五点") or []
        return b if isinstance(b, list) else [b]

    raw_amp_titles = sum(1 for r in records if "&" in str(r.get("title") or ""))
    raw_amp_bullets = sum(1 for r in records if any("&" in str(x) for x in _bullets_of(r)))
    add(f"- 标准化: 原文标题含 '&' 的商品 {raw_amp_titles}/{n}, 五点含 '&' 的 {raw_amp_bullets}/{n}。"
        "step04 现用 naive 标准化会把 & 直接删成空格, 导致 'salt & pepper shakers' 无法命中 'salt and pepper shakers';"
        "本脚本按 skill 先做 &->and 再统计(naive 与 smart 的命中差见 §3 naive 列)。")
    add("")

    add("## 2. 确定性挖掘 Top-%d(按标题覆盖率排序)" % top)
    add("")
    add("| 排名 | 词组 | 角色 | 标题覆盖率 | 全文覆盖率 | 标题/全文命中 | 提及 | BS覆盖率 | NR覆盖率 | NR-BS差 |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for i, c in enumerate(res["det_top"], 1):
        mark = "★" if c.get("in_core") else ("◎" if c.get("in_existing") else "")
        add(f"| {i} | {c['phrase']}{mark} | {c['role']} | {fmt_pct(c['title_cov'])} | {fmt_pct(c['fulltext_cov'])} "
            f"| {c['title_support']}/{c['fulltext_support']} | {c['mentions']} | {fmt_pct(c['bs_cov'])} | "
            f"{fmt_pct(c['nr_cov'])} | {fmt_pp(c['nr_bs_diff'])} |")
    add("")
    add("> ★=当前核心大词 ◎=现有 LLM 候选")
    add("")

    add("## 3. 现有 LLM 候选逐词回测")
    add("")
    add("| 词组 | 角色 | 标题覆盖率 | naive标题覆盖率(&损失) | 全文覆盖率 | 提及 | 确定性Top | 可被确定性挖掘 | 月搜索量 |")
    add("|---|---|---|---|---|---|---|---|---|")
    core_set = set(res["core"])
    for c in sorted(res["existing_scored"], key=lambda x: (-x["title_cov"], -x["fulltext_cov"])):
        mined_ok = "是" if c.get("mined_title_support", 0) >= 2 or c.get("mined_full_support", 0) >= 2 else "否(证据不足)"
        vol = c.get("vol")
        add(f"| {c['phrase']}{'★' if c['phrase'] in core_set else ''} | {c['role']} | {fmt_pct(c['title_cov'])} "
            f"| {fmt_pct(c['naive_title_cov'])} | {fmt_pct(c['fulltext_cov'])} | {c['mentions']} | "
            f"{'是' if c.get('in_det_top') else '否'} | {mined_ok} | {vol if vol is not None else ''} |")
    add("")

    add("## 4. 对比结论")
    add("")
    existing_set = set(res["existing"])
    add(f"- 现有 LLM 候选 {len(existing_set)} 个, 确定性 Top{top} 与现有候选交集: "
        f"**{len(existing_set & res['det_top_set'])} 个** "
        f"(Jaccard={jaccard(existing_set, res['det_top_set']):.2f})")
    add(f"- 确定性命中但现有候选缺失({len(res['det_new'])} 个): "
        + ("、".join(f"`{c['phrase']}`({fmt_pct(c['title_cov'])})" for c in res["det_new"]) or "无"))
    missing = res["missing_in_det"]
    if missing:
        add(f"- 现有候选但未进确定性 Top{top}({len(missing)} 个): ")
        for p in sorted(missing):
            c = res["full_scored"][p]
            mined_ok = c.get("mined_title_support", 0) >= 2 or c.get("mined_full_support", 0) >= 2
            why = "可被挖掘, 覆盖率排名靠后" if mined_ok else "挖掘证据不足(命中商品<2)"
            add(f"  - `{p}` 标题覆盖率 {fmt_pct(c['title_cov'])} 全文 {fmt_pct(c['fulltext_cov'])} "
                f"提及 {c['mentions']} —— {why}")
    add("")
    add("## 5. 与最终核心大词对照(挖词量 Top5)")
    add("")
    add(f"- 当前核心大词(共 {len(res['core'])}): " + "、".join(f"`{w}`" for w in res["core"]))
    for w in res["core"]:
        c = res["full_scored"].get(w)
        if not c:
            add(f"- `{w}`: 不在全池(无标题/五点证据)")
            continue
        rank = next((i for i, x in enumerate(res["det_top"], 1) if x["phrase"] == w), None)
        rank_txt = f"确定性第 {rank} 名" if rank else "未进确定性 Top"
        add(f"- `{w}` 标题覆盖率 {fmt_pct(c['title_cov'])} 全文 {fmt_pct(c['fulltext_cov'])} "
            f"提及 {c['mentions']} —— {rank_txt}")
    add("")

    # 跨轮稳定性对比
    if extra_res:
        add("## 6. 跨轮稳定性对比(与另一轮的 LLM 候选比较)")
        add("")
        e1, e2 = set(res["existing"]), set(extra_res["existing"])
        d1, d2 = res["det_top_set"], extra_res["det_top_set"]
        add(f"| 对比 | {res['cat']} 与 {extra_res['cat']} Jaccard |")
        add("|---|---|")
        add(f"| LLM 候选(现实现状) | {jaccard(e1, e2):.2f} ({len(e1 & e2)}/{len(e1 | e2)}) |")
        add(f"| 确定性挖掘 Top{top}(本方案) | {jaccard(d1, d2):.2f} ({len(d1 & d2)}/{len(d1 | d2)}) |")
        add(f"| 确定性 Top{top} vs 本类 LLM 候选 | {jaccard(d1, e1):.2f} |")
        add("")
        add(f"- 两轮 LLM 候选差异词: 仅 {res['cat']} 有 = "
            + ("、".join(f"`{w}`" for w in sorted(e1 - e2)) or "无")
            + "; 仅 " + extra_res["cat"] + " 有 = "
            + ("、".join(f"`{w}`" for w in sorted(e2 - e1)) or "无"))
        add(f"- 两轮确定性 Top{top} 差异词: 仅 {res['cat']} 有 = "
            + ("、".join(f"`{w}`" for w in sorted(d1 - d2)) or "无")
            + "; 仅 " + extra_res["cat"] + " 有 = "
            + ("、".join(f"`{w}`" for w in sorted(d2 - d1)) or "无"))
        add("")

    add("## 7. 方法说明与限制")
    add("")
    add("- 覆盖率是 **listing 市场语言证据代理**, 不是亚马逊官方月搜索量(skill 要求明确标注)。")
    add("- 候选池=确定性 n-gram 挖掘(标题/五点商品支持≥2)+ 现有候选 + 根词锚定; 全部统一按六维打分。")
    add("- 根词锚定规则: 大类根词即使标题命中<2 也保留在考虑范围, 不因长尾意图更精准而丢弃。")
    add("- 词义身份保持: 只做字符级标准化(&->and), 不做同义替换; 'salt or pepper' 不并入 'salt and pepper'。")
    add("- 角色分类为本地启发式(词表驱动), 与 LLM 的 generic/functional/intent 三分法不一一对应。")
    add("- 提及次数为全文出现次数合计; 商品支持数去重口径。")
    add("- 当 bs==nr 时 BS/NR/NR-BS 无区分度, 该维度如实记数据不足, 不参与结论。")
    path = data_dir / cat / f"{cat}-coverage-keyword-validation.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cat", required=True, help="主类目, 如 salt-pepper-shaker-sets-v2")
    ap.add_argument("--extra-cat", default="", help="跨轮对比类目(如 salt-pepper-shaker-sets)")
    ap.add_argument("--data-dir", default="data", help="数据根目录")
    ap.add_argument("--top", type=int, default=20, help="确定性 Top N(默认 20, 对齐 CANDIDATE_LIMIT)")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    res = analyze_cat(args.cat, data_dir, args.top)
    res["_data_dir"] = data_dir
    extra = None
    if args.extra_cat:
        extra = analyze_cat(args.extra_cat, data_dir, args.top)
        extra["_data_dir"] = data_dir

    md = write_md(res, args.top, extra)
    csv_path = write_csv(args.cat, data_dir, res["full_scored"])
    print(f"[ok] report: {md}")
    print(f"[ok] csv   : {csv_path}")
    print(f"[ok] det-top{args.top} vs existing: "
          f"intersect={len(set(res['existing']) & res['det_top_set'])}, "
          f"existing={len(res['existing'])}, det={len(res['det_top_set'])}")
    if extra:
        print(f"[ok] cross-run LLM jaccard={jaccard(set(res['existing']), set(extra['existing'])):.3f} | "
              f"det top{args.top} jaccard={jaccard(res['det_top_set'], extra['det_top_set']):.3f}")


if __name__ == "__main__":
    main()
