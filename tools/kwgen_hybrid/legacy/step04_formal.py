"""FR-04 生成关键词(LLM∪确定性挖掘抽取 + 覆盖率回测 + 五维度长尾)。

输入: FR-03 过滤后榜单详情 <CAT>-dual-details-filtered.jsonl(必选)
      + FR-02 产品口径/排除词 filter-<CAT>-prompt.json(可选, 缺失回退通用口径)
      + 配置(站点/月份/密钥) + tools/kwgen_hybrid/registered_roots.json(登记根词)
输出: <CAT>-keywords-candidates.txt(大词候选, 最多 CANDIDATE_LIMIT 个, 默认 20; 每行一个词组)
      <CAT>-keywords.txt(分区: [核心大词] = 全部核心候选 + [长尾词] = 每维 LONGTAIL_PER_DIM 条)
      <CAT>-keywords-entities.txt(五维高频实体台账: 实体/商品数/出现次数/来源/对应长尾词)
      + <CAT>-kwgen-summary.json / <CAT>-kwgen-candidates.csv(融合版审计件, 不入下游消费)

4.1 大词候选: **LLM 抽取(温度0) ∪ 确定性挖掘 ∪ 根词** 取并集(2026-09 稳定化:
      解决跨轮 LLM 抽取不稳定, 如 grinder 词一轮有一轮无)
      -> 确定性挖掘 = 标题/五点 n-gram(商品支持≥2, 产品名词收尾, 品牌剔除)
      -> 正则校验多词短语(排除孤立单根词) -> 品牌黑名单剔除
      -> 全覆盖率回测(标题/全文/提及/BS/NR) -> 按标题覆盖率降序取前 CANDIDATE_LIMIT
      (无语料证据的 LLM 词让位给有证据词; LLM 失败时确定性挖掘+模板仍可产出)
4.2 核心大词(2026-09 变更): **不再固定 5 个** —— [核心大词] 分区写入**全部大词候选**
      (覆盖率降序; 根词规则"乙"把最有代表性的类目根词提到首位作长尾基座)。
      **达标筛选不限个数**: 由 FR-05 挖词数据(搜索量/站内同比/Google指数/CPC)与 FR-07
      逐词判定完成 —— 凡 搜索量>3万 且 站内/Google 趋势与 CPC 达标 的候选均为
      "达标核心大词", step08 报告核心分析 / step09 搜索收集 / step10 达标关键词注入
      均以该达标全集为准。
4.3 长尾词(2026-09-13 改版): 五维度(场景/人群/功能/属性/痛点), 每维:
      ① 候选实体 = 本地维度词表 ∪ LLM 从**标题+五点**抽出的实体;
      ② 每个候选实体在**全部标题+五点**上确定性计数(记录数/出现次数), 只保留真实出现的,
         按频率降序取前 LONGTAIL_PER_DIM(默认 5)个 —— 频率是"有多少个商品提到"优先;
      ③ 组装 = 维度模板 × 基座词(核心根词), 再由 LLM 润色成自然搜索短语(≤8 词,
         必须同时含基座词与该实体, 不合规即回落到模板草稿);
      ④ 不足时用维度兜底模板补足, 保证每维条数固定(默认 25 条 = 5 维 × 5)。

容错: LLM 失败 -> 确定性挖掘(∪并集) + 本地模板兜底, 保证 dry-run 与失败时也能产出
      恰好 CANDIDATE_LIMIT(候选) + LONGTAIL_PER_DIM×5(长尾)。
"""
# =====================================================================
# LEGACY(旧正式版步骤4): 已被 steps/step04_keywords.py(融合版) 替换。
# 本文件仅保留旧实现(文案提取+覆盖率回测+模板长尾)供历史对照,
# 以及 tests/_legacy/ 下 3 个旧测试的回归目标。正式流程不再引用本文件。
# =====================================================================
from __future__ import annotations

import re
from collections import Counter

from core import (
    LLMError,
    StepDataError,
    atomic_write_text,
    ensure_non_empty,
    read_json,
    read_jsonl,
    require_file,
    warn_missing,
    write_txt_lines,
)
from core.catname import display_cat
from steps._keywords import (
    ADJ_TAIL_RE,
    ATTR_NOUN_HEADS,
    DIM_RULES,
    LONGTAIL_MAX_WORDS,
    PART_NOUNS,
    assemble_entity,
    entity_in_base,
    entity_section,
    norm_token,
    normalize as _normalize_keyword,
)
from steps.base import BaseStep, StepContext

# 品牌词黑名单(剔除含品牌的候选, 需求 FR-04)
_BRAND_BLACKLIST = [
    "oxo",
    "peugeot",
    "le creuset",
    "cuisinart",
    "kitchenaid",
    "zwilling",
    "zassenhaus",
    "cole mason",
    "hario",
    "morphy richards",
]

# 大词候选本地兜底模板(类目无关, 以类目根词参数化)
# 首个模板即类目根词本身, 保证标题存在时至少有一个候选能命中
_CAND_FALLBACK_TEMPLATES = [
    "{r}",
    "{r} set",
    "electric {r}",
    "{r} for kitchen",
    "adjustable {r}",
    "{r} stainless steel",
    "manual {r}",
    "{r} for travel",
    "portable {r}",
    "heavy duty {r}",
    "{r} with ceramic burr",
    "{r} for camping",
    "wooden {r}",
    "{r} battery operated",
    "{r} for home",
    "{r} for outdoor",
    "{r} for gifts",
    "compact {r}",
    "{r} with lid",
    "{r} large capacity",
]

# ---------- 长尾五维度(2026-09-13: 四维"场景/人群功能/属性/痛点" -> 五维) ----------
# 每个维度的实体都**从 FR-03 的标题+五点里按频率提取**(不再靠 LLM 凭空生成一整条长尾):
#   dim -> (组装模式, 该维度要抽什么); 组装模式见 steps/_keywords.py(与 FR-09 共用同一套规则)
_DIM_RULES = DIM_RULES

# 各维度候选实体词表(= 候选**生成器**, 不是白名单):
# 只有**在标题/五点里真实出现**的候选才会被计数保留(见 _extract_entities),
# 因此词表没覆盖到的表达由 LLM 补充, 词表里没出现的词自然被淘汰。
_DIM_LEXICON = {
    "场景": {
        "kitchen", "home", "travel", "camping", "outdoor", "office", "restaurant",
        "car", "party", "picnic", "bathroom", "bedroom", "garage", "yard", "garden",
        "pantry", "counter", "gym", "hotel", "dorm", "apartment", "bakery", "cafe",
        "bar", "farm", "workshop", "nursery", "balcony", "patio", "bbq", "grill",
        "small kitchen", "rv", "boat", "school", "classroom", "living room", "on the go",
        "cabinet", "shelf", "island", "laundry", "hallway", "closet", "basement",
        "window", "windowsill", "van", "truck", "tent", "daycare", "salon", "clinic",
        "campsite", "cabin", "beach", "pool", "porch", "deck", "loft", "studio",
        "dining", "dining room", "table", "buffet", "food prep", "meal prep",
    },
    "人群": {
        "kids", "kid", "children", "child", "baby", "toddler", "seniors", "senior",
        "elderly", "adults", "men", "women", "family", "families", "beginners",
        "beginner", "chef", "chefs", "students", "travelers", "campers", "hikers",
        "pets", "dogs", "cats", "dog", "cat", "professional", "arthritic",
        "left handed", "busy people", "pet owners", "dog owners", "cat owners",
    },
    "功能": {
        "electric", "manual", "automatic", "adjustable", "portable", "foldable",
        "collapsible", "rechargeable", "cordless", "wireless", "battery operated",
        "multifunctional", "multipurpose", "heavy duty", "non slip", "stackable",
        "nesting", "rotating", "removable", "washable", "reusable", "free standing",
        "wall mounted", "handheld", "hands free", "self cleaning", "quick release",
        "one hand", "lockable", "detachable", "interactive", "programmable", "smart",
        "rolling", "swivel", "magnetic", "push button",
    },
    "属性": {
        "stainless steel", "ceramic", "wooden", "wood", "bamboo", "plastic",
        "silicone", "glass", "acrylic", "cast iron", "aluminum", "marble", "large",
        "large capacity", "extra large", "small", "mini", "compact", "slim", "wide",
        "tall", "round", "square", "rectangular", "clear", "transparent", "matte",
        "bpa free", "food grade", "dishwasher safe", "airtight", "waterproof",
        "rust proof", "leak proof", "scratch resistant", "with lid", "with handle",
        "with scoop", "with measuring cup", "with wheels", "with lock", "with filter",
        "with tray", "with strap", "adjustable height", "stainless", "portable size",
    },
    "痛点": {
        "easy to clean", "no mess", "mess free", "quiet", "noise free", "low noise",
        "spill proof", "odor free", "smell proof", "rust free", "dent resistant",
        "sturdy", "durable", "long lasting", "space saving", "easy to use",
        "easy to carry", "easy to store", "easy to fill", "easy to remove",
        "pain free", "fatigue free", "non toxic", "no spill", "tangle free",
        "anti slip", "no slip", "no leakage", "safety lock",
    },
}

# suffix 模式下改用 "<基座> with <实体>" 的实体末词(容器/配件/结构件名词; 与 FR-09 共用)
_ATTR_NOUN_HEADS = ATTR_NOUN_HEADS

# 维度实体的形态校验: 防止 LLM 把动词/名词短语塞进不合适的维度
# (实战 Food Processors: 功能维度收到 "chop"(37品)/"press"(21品)/"stainless steel blades"(24品)
#  → 组装出 "chop food processor"、"press food processor" 这类不通顺的长尾)
_ADJ_TAIL_RE = ADJ_TAIL_RE
# 单字(单词)实体的维度白名单: 场景维度也要求词表命中, 避免 "bags"/"cabinets" 这类非地点名词
_SINGLE_WORD_OK_DIMS = {"人群", "属性"}
# 长尾基座词: 根词结尾的场景介词短语(如 "air purifier for home" 的 " for home")在组装时剥掉
_LONGTAIL_BASE_TAIL_RE = re.compile(r"\s+(?:for|in|at|on)\s+[a-z0-9]+(?:\s+[a-z0-9]+){0,2}$")

# 长尾各维度本地兜底词表(实体不足时用通用模板补足, 保证每维条数固定)
_DIM_FALLBACK = {
    "场景": [
        "for kitchen",
        "for home",
        "for travel",
        "for camping",
        "for outdoor",
        "for office",
        "for restaurant",
        "for gifts",
    ],
    "人群": [
        "for seniors",
        "for kids",
        "for beginners",
        "for families",
        "for adults",
        "for pets",
        "for small kitchens",
        "for busy people",
    ],
    "功能": [
        "electric",
        "manual",
        "adjustable",
        "portable",
        "heavy duty",
        "battery operated",
        "cordless",
        "multifunctional",
    ],
    "属性": [
        "stainless steel",
        "ceramic",
        "wooden",
        "large capacity",
        "compact",
        "with lid",
        "bpa free",
        "dishwasher safe",
    ],
    "痛点": [
        "easy to clean",
        "no mess",
        "quiet",
        "sturdy",
        "space saving",
        "easy to use",
        "leak proof",
        "durable",
    ],
}

# 长尾每条最大词数(需求 2026-09-13: 长尾词 ≤8 个英文单词; 与 steps/_keywords.py 共用)
_LONGTAIL_MAX_WORDS = LONGTAIL_MAX_WORDS
# 每维向 LLM 索取/保留的候选实体上限(默认; 实际按 LONGTAIL_PER_DIM 走)
_LONGTAIL_PER_DIM = 5

# ---------- 动态产品名词(2026-09-13: 取代原硬编码产品名词表) ----------
# 原实现是手工表 {"shaker","mill","grinder","board","container","dispenser",...}, 写死于
# 研磨器/厨房类目, 后果:
#   ① 类目真词(processor/chopper/fountain/purifier)不在表内 → 确定性挖掘收不到它们;
#   ② _select_core 用同一张表挑根词, 把"标题里最热的产品词"顶掉 —— 实战 Food Processors
#      被选成 `processor meat grinder`(表里有 grinder), 而该类目达标词是 food chopper/
#      vegetable chopper, 两者无任何共同词头 → step09"长尾必须含达标词产品名词头"把 20 条
#      长尾全剔 → 相关长尾 0(丢约 30% 候选池)。
# 现做法: 每个类目**从自己的榜单标题**动态提取产品名词(见 _product_nouns)。
_NOUN_MIN_PRODUCTS = 2        # 至少被这么多个商品的标题用作末词
_NOUN_MIN_COVERAGE = 0.25     # 标题覆盖率下限
# 标题"末词 + 其前导分隔符": 分组1 非空 = 该末词是 Amazon 变体名(如 ", Blue" / "- 108oz")
_LAST_TOKEN_RE = re.compile(r"([,;|/\-]\s*)?([a-z0-9]+)[\s.,!)]*$")


def _norm_token(token: str) -> str:
    """轻量复数归一(sets→set / purifiers→purifier / berries→berry), 用于类目名与标题词对齐。"""
    return norm_token(token)


def _product_nouns(titles: list[str]) -> set[str]:
    """从**榜单标题**动态提取产品名词(取代原硬编码 _MINE_PRODUCT_ENDS)。

    判据(需全部满足):
      ① 作为标题末词出现 ≥ `_NOUN_MIN_PRODUCTS` 个商品(产品名通常收尾);
      ② **变体标签率 < 50%** —— 末词紧跟 , ; | / - 的视为变体名(颜色/尺寸), 不计入;
         (实测: black/white/blue/gray/108oz 这类词的变体标签率 50%~100%; 而
          chopper/processor/container/fountain/board 等真产品名词一律 0%)
      ③ 标题覆盖率 ≥ `_NOUN_MIN_COVERAGE`(实测: 真产品名词 ≥26%, 修饰/配件词 ≤37%);
      ④ 不在 虚词表/配料表/场景人群表, 且不是品牌词。
    提取为空(标题异常/过少)时返回空集, 由 `_mine_keep` 退化为"末词非虚词/场景/配料词"。
    """
    end_cnt: Counter = Counter()
    tag_cnt: Counter = Counter()
    total = 0
    for raw in titles:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        total += 1
        m = _LAST_TOKEN_RE.search(text)
        if not m:
            continue
        sep, last = m.group(1), m.group(2)
        if len(last) < 3 or last.isdigit():
            continue
        if last in _MINE_TRAIL_STOPS or last in _MINE_INGREDIENTS or last in _MINE_INTENT_WORDS:
            continue
        end_cnt[last] += 1
        if sep:
            tag_cnt[last] += 1
    if not total:
        return set()
    nouns: set[str] = set()
    for word, cnt in end_cnt.items():
        if cnt < _NOUN_MIN_PRODUCTS:
            continue
        if tag_cnt.get(word, 0) / cnt >= 0.5:          # 变体标签(颜色/尺寸)排除
            continue
        if cnt / total < _NOUN_MIN_COVERAGE:
            continue
        if any(re.search(rf"\b{re.escape(b)}\b", word) for b in _BRAND_BLACKLIST):
            continue
        nouns.add(word)
    return nouns

# ---------- 根词规则(乙: 类目名实词覆盖优先) ----------
_ROOT_NGRAM_MIN = 2           # 候选短语词数下限
_ROOT_NGRAM_MAX = 5           # 候选短语词数上限
_ROOT_NGRAM_MIN_SUPPORT = 3   # 候选短语至少被这么多个商品标题用到
_ROOT_MIN_COVERAGE = 0.25     # 候选短语覆盖率下限
# 配料/食材词: 可组成 "salt and pepper" 式根词, 但不宜作为 n-gram 收尾
_MINE_INGREDIENTS = {
    "salt", "pepper", "spice", "spices", "sugar", "herb", "herbs",
    "seasoning", "seasonings", "oil", "vinegar",
}
# 场景/人群词(→intent 角色)
_MINE_INTENT_WORDS = {
    "kitchen", "travel", "camping", "camp", "picnic", "bbq", "barbecue",
    "outdoor", "restaurant", "home", "office", "party", "table", "dining",
    "gift", "gifts", "holiday", "cooking", "grill",
    "senior", "seniors", "chef", "chefs", "kids", "child", "children",
    "adult", "adults", "traveler", "travelers", "camper", "campers",
    "elderly", "arthritis",
}
# 属性/功能词(→functional 角色)
_MINE_FUNC_WORDS = {
    "glass", "ceramic", "stainless", "steel", "wooden", "wood", "bamboo",
    "acrylic", "plastic", "porcelain", "stoneware", "mini", "small", "tiny",
    "vintage", "farmhouse", "rustic", "modern", "electric", "manual",
    "adjustable", "automatic", "battery", "portable", "heavy", "large",
    "led", "magnetic", "cork", "lid", "lids", "pour", "refill",
    "refillable", "insulated", "silicone", "walnut", "acacia", "marble",
    "countertop", "compact", "premium", "durable",
}
# n-gram 两端剥除的虚词
_MINE_TRAIL_STOPS = {
    "with", "and", "or", "for", "of", "the", "a", "an", "to", "in", "on",
    "by", "at", "from",
}

# 榜单详情文本送入 LLM 的最大长度(字符)
_TEXT_CAP = 20000


class LegacyStep04Formal(BaseStep):
    number = 4
    title = "生成关键词"
    description = ("FR-04(旧正式版, 已被融合版替换): 文案 n-gram 挖掘+LLM 抽取+根词, "
                   "覆盖率回测排序, 实体×根词润色+模板兜底长尾。仅供历史对照与 tests/_legacy/ 回归。")

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        details = ctx.resolve_artifact(f"{self._cat}-dual-details-filtered.jsonl")
        require_file(details, "FR-03 过滤后榜单详情")
        ensure_non_empty(details, "FR-03 过滤后榜单详情")

    @property
    def artifacts(self) -> list[str]:
        return [
            f"{self._cat}-keywords-candidates.txt",
            f"{self._cat}-keywords.txt",
            f"{self._cat}-keywords-entities.txt",
        ]

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        self.log(ctx, f"开始生成关键词: CAT={cat}")
        out_cand = ctx.resolve_artifact(self.artifacts[0])
        out_kw = ctx.resolve_artifact(self.artifacts[1])
        out_ent = ctx.resolve_artifact(self.artifacts[2])

        records = read_jsonl(ctx.resolve_artifact(f"{cat}-dual-details-filtered.jsonl"))
        if not records:
            raise StepDataError("过滤后榜单详情无有效记录, 无法生成关键词")
        titles = [self._normalize_text(str(r.get("title", ""))) for r in records]
        fulltexts = [self._fulltext_text(r) for r in records]
        text = self._build_text(records)
        self.log(ctx, f"读取过滤后榜单详情 {len(records)} 条")

        root_word = self._top_bigram(titles) or cat.replace("-", " ").strip()
        bs_set, nr_set = self._load_bsnr(ctx, cat)

        candidates = self._extract_candidates(
            ctx, cat, root_word, text, records, titles, fulltexts, bs_set, nr_set
        )
        self.log(ctx, f"大词候选 {len(candidates)} 个(按覆盖率降序)")
        write_txt_lines(out_cand, [c["phrase"] for c in candidates])
        self.log(ctx, f"大词候选已写入: {out_cand}")

        core = self._select_core(ctx, cat, candidates, titles)
        root = core[0]
        self.log(ctx, f"核心大词 {len(core)} 个, 根词: {root}")
        for i_, w in enumerate(core, 1):
            self.log(ctx, f"  核心大词[{i_}]: {w}")

        per_dim = self._longtail_per_dim(ctx)
        longtail, report = self._generate_longtail(ctx, cat, root, fulltexts, set(core), per_dim)
        self.log(ctx, f"长尾词 {len(longtail)} 个(五维度 × {per_dim})")

        content = "[核心大词]\n" + "\n".join(core) + "\n[长尾词]\n" + "\n".join(longtail) + "\n"
        atomic_write_text(out_kw, content)
        self.log(ctx, f"关键词文件已写入: {out_kw}")

        atomic_write_text(out_ent, self._entity_report_text(cat, root, len(records), per_dim, report))
        self.log(ctx, f"五维实体台账已写入: {out_ent}")

    # ---------- 本地文本处理 ----------

    def _normalize_text(self, text: str) -> str:
        """本地规范化: 小写 / & -> and / 标点转空格 / 合并空白。

        2026-09 变更: `&` 先转成 and 再清标点, 否则 "salt & pepper shakers"
        会被清成 "salt pepper shakers", 永远命中不了 "salt and pepper shakers",
        系统性低估 shaker 家族覆盖率。
        """
        text = (text or "").lower().replace("&", " and ")
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _build_text(self, records: list[dict]) -> str:
        parts = []
        for r in records:
            title = self._normalize_text(str(r.get("title", "")))
            bullets = r.get("bullets") or r.get("五点") or []
            if isinstance(bullets, list):
                bullet_text = " ".join(self._normalize_text(str(b)) for b in bullets)
            else:
                bullet_text = self._normalize_text(str(bullets))
            if title:
                parts.append(title)
            if bullet_text:
                parts.append(bullet_text)
        blob = " ".join(parts)
        return blob[:_TEXT_CAP]

    def _fulltext_text(self, record: dict) -> str:
        """单条记录的规范化全文(标题 + 五点), 用于全文覆盖率/提及次数回测。"""
        title = self._normalize_text(str(record.get("title", "")))
        bullets = record.get("bullets") or record.get("五点") or []
        if isinstance(bullets, list):
            body = " ".join(self._normalize_text(str(b)) for b in bullets if str(b).strip())
        else:
            body = self._normalize_text(str(bullets))
        return f"{title} {body}".strip()

    def _top_bigram(self, titles: list[str]) -> str:
        """取标题中最常出现的相邻双词作为类目根词猜测。"""
        counter: Counter = Counter()
        for t in titles:
            words = re.findall(r"[a-z0-9]+", t)
            for i in range(len(words) - 1):
                counter[" ".join(words[i:i + 2])] += 1
        if not counter:
            return ""
        return counter.most_common(1)[0][0]

    def _valid_phrase(self, phrase: str) -> bool:
        """多词短语校验: 含空格、至少两个词、非品牌词。"""
        if not phrase or " " not in phrase:
            return False
        if len(re.findall(r"[a-z0-9]+", phrase)) < 2:
            return False
        for brand in _BRAND_BLACKLIST:
            if re.search(rf"\b{re.escape(brand)}\b", phrase):
                return False
        return True

    # ---------- 4.1 大词候选 ----------

    def _extract_candidates(
        self,
        ctx: StepContext,
        cat: str,
        root_word: str,
        text: str,
        records: list[dict],
        titles: list[str],
        fulltexts: list[str],
        bs_set: set,
        nr_set: set,
    ) -> list[dict]:
        """提取大词候选(2026-09 稳定化): **LLM(温度0) ∪ 确定性挖掘 ∪ 根词** 并集。

        1. 确定性挖掘(标题/五点 n-gram, 商品支持≥2) —— 稳定可复算, 跨轮不变;
        2. 类目根词补充(双配料词如 salt and pepper / 干净根词) —— 根词不因精准度低丢弃;
        3. LLM 抽取(温度0) —— 同词时覆盖挖掘角色, 补充无语料证据但有真实搜索习惯的词。
        统一按覆盖率回测排序取前 CANDIDATE_LIMIT: 无语料证据的 LLM 词让位给有证据词。
        LLM 失败: 确定性挖掘 + 模板兜底仍可产出(不会因模型波动而空)。
        """
        limit = max(1, int(ctx.config.get("CANDIDATE_LIMIT", 14) or 14))
        presence, roots = self._root_supplement(cat, root_word)
        nouns = _product_nouns(titles)
        self.log(ctx, f"动态产品名词 {len(nouns)} 个: " + ("、".join(sorted(nouns)[:15]) or "(空, 已退化为不限制末词)"))

        # ① 确定性挖掘 + 根词(先入池, 角色由启发式给出)
        pool: dict[str, dict] = {}
        try:
            mined = self._mine_pool(records, presence, nouns)
        except Exception as exc:  # 挖掘兜底不应让步骤失败
            self.log(ctx, f"确定性挖掘失败, 跳过: {exc}")
            mined = []
        for phrase in mined:
            pool[phrase] = {"phrase": phrase, "role": self._mine_role(phrase)}
        for phrase in roots:
            pool.setdefault(phrase, {"phrase": phrase, "role": "generic"})
        self.log(ctx, f"确定性挖掘 {len(mined)} 个 + 根词 {len(roots)} 个")

        # ③ LLM 抽取(温度0); 失败回退本地模板补足
        llm_failed = False
        try:
            raw = self._call_candidates_llm(ctx, cat, root_word, text, limit)
        except LLMError as exc:
            llm_failed = True
            self.log(ctx, f"大词候选 LLM 调用失败(温度0), 使用确定性挖掘+模板: {exc}")
            raw = []
        for c in raw:
            pool[c["phrase"]] = c  # 同词保留 LLM 角色(generic/functional/intent)
        if llm_failed and len(pool) < limit:
            # 仅 LLM 失败时用模板补足(与旧行为一致: LLM 成功不做模板填充)
            for tpl in _CAND_FALLBACK_TEMPLATES:
                phrase = self._normalize_text(tpl.format(r=root_word))
                if phrase and self._valid_phrase(phrase) and phrase not in pool:
                    pool[phrase] = {"phrase": phrase, "role": "generic"}
                if len(pool) >= limit:
                    break

        scored = self._score_candidates(list(pool.values()), records, titles, fulltexts, bs_set, nr_set)
        scored.sort(
            key=lambda c: (-c["title_cov"], -c["fulltext_cov"], -c["mentions"], len(c["phrase"]))
        )
        kept = scored[:limit]
        for c in kept:
            self.log(
                ctx,
                f"候选: {c['phrase']} 覆盖率={c['title_cov']:.2%} 全文={c['fulltext_cov']:.2%} "
                f"提及={c['mentions']} BS={c['bs_cov']:.2%} NR={c['nr_cov']:.2%} "
                f"差={c['nr_bs_diff']:+.2%} 角色={c['role']}",
            )
        self.log(
            ctx,
            f"大词候选共 {len(kept)} 个(LLM∪确定性挖掘∪根词, 按覆盖率降序取前 {limit})",
        )
        return kept

    # ---------- 确定性挖掘(① 结构兜底) ----------

    def _root_supplement(self, cat: str, root_word: str) -> tuple[set, list[str]]:
        """从类目名/根词推导: 词池准入词表 presence + 根词补充列表 roots。"""
        cat_tokens = [
            t
            for t in re.findall(r"[a-z0-9]+", cat.lower())
            if t not in _MINE_TRAIL_STOPS and len(t) > 1 and not re.search(r"\d", t)
        ]
        root_tokens = [
            t
            for t in re.findall(r"[a-z0-9]+", root_word or "")
            if t not in _MINE_TRAIL_STOPS and len(t) > 1 and not re.search(r"\d", t)
        ]
        presence: set = set()
        for t in cat_tokens + root_tokens:
            presence.add(t)
            if t.endswith("s") and len(t) > 3:
                presence.add(t[:-1])  # boards -> board
            else:
                presence.add(t + "s")  # shaker -> shakers
        roots: list[str] = []
        # 干净的根词本身(top_bigram 猜测, 首尾无虚词才可用)
        words = (root_word or "").split()
        if (
            len(words) >= 2
            and words[0] not in _MINE_TRAIL_STOPS
            and words[-1] not in _MINE_TRAIL_STOPS
            and self._valid_phrase(root_word)
        ):
            roots.append(root_word)
        # "salt and pepper" 式双配料根词(类目名含 ≥2 个配料词时)
        ings = [t for t in cat_tokens if t in _MINE_INGREDIENTS]
        if len(ings) >= 2:
            cand_root = f"{ings[0]} and {ings[1]}"
            if cand_root not in roots and self._valid_phrase(cand_root):
                roots.append(cand_root)
        return presence, roots

    def _trim_mine_phrase(self, phrase: str) -> str:
        """两端剥除虚词(可剥到 1 词, 多词性由调用方判定)。"""
        words = phrase.split()
        while words and words[0] in _MINE_TRAIL_STOPS:
            words = words[1:]
        while words and words[-1] in _MINE_TRAIL_STOPS:
            words = words[:-1]
        return " ".join(words)

    def _mine_keep(self, phrase: str, presence: set, nouns: set | None = None) -> bool:
        """挖掘词保留判定: 多词 / 非品牌 / 含类目词 / **末词是动态产品名词**(或完整意图形态)。"""
        words = phrase.split()
        if len(words) < 2:
            return False
        for brand in _BRAND_BLACKLIST:
            if re.search(rf"\b{re.escape(brand)}\b", phrase):
                return False
        if not any(w in presence for w in words):
            return False
        last = words[-1]
        if last in _MINE_INGREDIENTS:
            return False
        if last in _MINE_TRAIL_STOPS:
            return False
        if nouns:
            # 动态产品名词集非空 → 白名单式(与该类目标题里"作末词的产品名词"对齐)
            if last in nouns or _norm_token(last) in nouns:
                return True
        else:
            # 动态集为空(标题过少/异常): 退化为"末词不是虚词/场景/配料词"即可
            if last not in _MINE_INTENT_WORDS:
                return True
        if "for" in words and last in _MINE_INTENT_WORDS:
            return True
        return False

    def _mine_role(self, phrase: str) -> str:
        """挖掘词角色启发式 -> pipeline 三角色(generic/functional/intent)。"""
        words = phrase.split()
        if any(w in _MINE_INTENT_WORDS for w in words):
            return "intent"
        if any(w in _MINE_FUNC_WORDS for w in words):
            return "functional"
        return "generic"

    def _mine_ngrams(self, text: str) -> list[str]:
        """单条规范化文本的 2..8 词 n-gram(去两端虚词, 去空)。"""
        words = re.findall(r"[a-z0-9]+", text)
        out = []
        for n in range(2, min(8, len(words)) + 1):
            for i in range(len(words) - n + 1):
                trimmed = self._trim_mine_phrase(" ".join(words[i:i + n]))
                if trimmed and " " in trimmed:
                    out.append(trimmed)
        return out

    def _mine_pool(self, records: list[dict], presence: set,
                   nouns: set | None = None) -> list[str]:
        """确定性候选池: 标题 n-gram ∪ 五点 n-gram, 商品支持≥2(去重后返回)。"""
        t_seen: dict[str, set] = {}
        b_seen: dict[str, set] = {}
        for idx, rec in enumerate(records):
            title = self._normalize_text(str(rec.get("title") or ""))
            bullets = rec.get("bullets") or rec.get("五点") or []
            if isinstance(bullets, str):
                bullets = [bullets]
            t_local: set = set()
            for ng in self._mine_ngrams(title):
                if ng not in t_local and self._mine_keep(ng, presence, nouns):
                    t_local.add(ng)
                    t_seen.setdefault(ng, set()).add(idx)
            b_local: set = set()
            for bullet in bullets:
                bn = self._normalize_text(str(bullet))
                for ng in self._mine_ngrams(bn):
                    if ng not in b_local and self._mine_keep(ng, presence, nouns):
                        b_local.add(ng)
                        b_seen.setdefault(ng, set()).add(idx)
        pool: list[str] = []
        for phrase, prods in t_seen.items():
            if len(prods) >= 2:
                pool.append(phrase)
        for phrase, prods in b_seen.items():
            if phrase not in t_seen and len(prods) >= 2:
                pool.append(phrase)
        return pool

    def _call_candidates_llm(self, ctx: StepContext, cat: str, root_word: str, text: str, limit: int) -> list[dict]:
        """LLM 抽取候选: **温度0 单轮确定性输出**(2026-09 稳定化)。

        原多温(0.3/0.5)双轮抽取是为提高单次抽取覆盖面, 但也造成跨轮结果
        漂移(grinder 词一轮有一轮无); 温度0 保证同一文本每次输出一致,
        漏词由确定性挖掘(∪并集)补足, 不再靠抽两次碰运气。
        """
        prompt = f"""你是亚马逊类目大词候选抽取助手。
类目: {display_cat(cat)}
类目根词: {root_word}
以下是从该类目榜单商品提取的标题与五点描述文本(来源样本):
{text}
请从以上文本中**尽可能全面地**抽取该类目买家可能搜索的关键词组(宁多勿漏), 最多 {limit} 个。
抽取范围包括但不限于:
1. 标题中的完整核心词组(如 "salt and pepper shakers")
2. 五点卖点中的功能/属性/场景/人群表达(如 electric, adjustable, for kitchen, stainless steel)
3. 结合你对亚马逊搜索习惯的理解, 补全可能的搜索变体(如加 set/kit/electric/manual 等修饰词)
4. **同一核心词请同时考虑 带/不带 修饰变体**(如 "electric salt and pepper grinder" 与 "electric salt and pepper grinder set" 是两个不同的真实搜索词, 都要输出)
按流量角色分类:
- generic: 泛词(类目通用短语)
- functional: 功能词(含功能/用途/属性描述)
- intent: 完整意图(含场景/人群/痛点)
要求:
1. 必须是多词短语(含空格), 禁止输出孤立单根词(salt/pepper/grinder 等)
2. 不输出品牌词
3. 数量尽量接近 {limit} 个, 不要遗漏任何可能的搜索词组
严格输出 JSON(不要输出其他任何文字):
{{"candidates": [{{"phrase": "词组", "role": "generic|functional|intent"}}, ...]}}
"""
        raw = ctx.llm().chat_json(prompt, temperature=0.0, max_tokens=4000)
        return self._parse_candidates(raw)

    def _parse_candidates(self, raw) -> list[dict]:
        out = []
        if not isinstance(raw, dict):
            return out
        for item in raw.get("candidates") or []:
            if not isinstance(item, dict):
                continue
            phrase = self._normalize_text(str(item.get("phrase") or ""))
            role = str(item.get("role") or "generic").strip().lower()
            if role not in ("generic", "functional", "intent"):
                role = "generic"
            if self._valid_phrase(phrase):
                out.append({"phrase": phrase, "role": role})
        return out

    def _load_bsnr(self, ctx: StepContext, cat: str) -> tuple[set, set]:
        bs_set: set = set()
        nr_set: set = set()
        path = ctx.resolve_artifact(f"{cat}-bestseller-newrelease.json")
        if warn_missing(path, "FR-00 双榜 ASIN(覆盖率回测)"):
            data = read_json(path)
            if isinstance(data, dict):
                bs_set = {str(a).strip() for a in (data.get("bs") or []) if str(a).strip()}
                nr_set = {str(a).strip() for a in (data.get("nr") or []) if str(a).strip()}
        return bs_set, nr_set

    def _score_candidates(
        self,
        candidates: list[dict],
        records: list[dict],
        titles: list[str],
        fulltexts: list[str],
        bs_set: set,
        nr_set: set,
    ) -> list[dict]:
        """覆盖率回测(2026-09 扩展为六维): 标题覆盖率/全文覆盖率/提及次数 + BS/NR。

        - title_cov: 标题含词组的商品占比(主排序依据)
        - fulltext_cov: 标题或五点含词组的商品占比
        - mentions: 全文出现次数合计
        - bs_cov/nr_cov: BS/NR 榜成员内全文命中率; nr_bs_diff = NR - BS
        """
        total = max(len(records), 1)
        asins = [str(r.get("request_asin", "")).strip() for r in records]
        bs_total = max(sum(1 for a in asins if a in bs_set), 1)
        nr_total = max(sum(1 for a in asins if a in nr_set), 1)
        scored = []
        for cand in candidates:
            phrase = cand["phrase"]
            t_hits = [i for i, t in enumerate(titles) if phrase in t]
            f_hits = [i for i, f in enumerate(fulltexts) if phrase in f]
            mentions = sum(f.count(phrase) for f in fulltexts)
            bs_hits = sum(1 for i in f_hits if asins[i] in bs_set)
            nr_hits = sum(1 for i in f_hits if asins[i] in nr_set)
            scored.append(
                {
                    "phrase": phrase,
                    "role": cand["role"],
                    "title_cov": len(t_hits) / total,
                    "fulltext_cov": len(f_hits) / total,
                    "mentions": mentions,
                    "bs_cov": bs_hits / bs_total,
                    "nr_cov": nr_hits / nr_total,
                    "nr_bs_diff": nr_hits / nr_total - bs_hits / bs_total,
                }
            )
        return scored

    # ---------- 4.2 核心大词 ----------

    def _select_core(self, ctx: StepContext, cat: str, candidates: list[dict],
                     titles: list[str] | None = None) -> list[str]:
        """核心大词 = **全部大词候选**(2026-09 变更: 不再固定 5 个/top5)。

        候选已按覆盖率回测降序(见 _extract_candidates); 全部写入 [核心大词] 分区,
        让 FR-07 筛选(搜索量>3万 + 站内/Google 趋势 + CPC)对**每一个候选**判定,
        达标全集不限个数, 供 step08 报告 / step09 搜索 / step10 注入使用。

        **根词规则(乙, 2026-09-13 取代硬编码产品名词表)**: 取"覆盖**类目名实词**最多 →
        覆盖率最高 → 词数更长"的标题高频短语作根词(见 `_pick_root`), 把它提到首位。
        理由: 类目名是人选的、代表这个类目到底是什么产品; 而原实现用一张写死的研磨器
        名词表去挑, 会把类目真词(processor/chopper/fountain)跳过、把 `processor meat grinder`
        这类(表里有 grinder)顶上去, 与达标词毫无交集 → step09 长尾全被剔(实战 0 条)。
        候选集合本身**不裁剪**(达标判定仍由 FR-07 对全部候选执行)。
        """
        phrases = [c["phrase"] for c in candidates]
        if not phrases:
            raise StepDataError("无可用的核心大词候选, 无法生成关键词")
        core = list(phrases)
        root = self._pick_root(cat, titles or [])
        if root and root in core and core[0] != root:
            core.remove(root)
            core.insert(0, root)
        return core

    def _cat_words(self, cat: str) -> set:
        """类目名实词(去纯数字/单字符/虚词, 轻量复数归一)。"""
        words = set()
        for tok in re.findall(r"[a-z0-9]+", str(cat or "").lower()):
            if len(tok) < 2 or tok.isdigit() or tok in _MINE_TRAIL_STOPS:
                continue
            words.add(_norm_token(tok))
        return words

    def _title_ngrams(self, titles: list[str]) -> dict[str, int]:
        """标题 n-gram(词数 _ROOT_NGRAM_MIN..MAX) -> 覆盖商品数(≥ _ROOT_NGRAM_MIN_SUPPORT)。"""
        counts: Counter = Counter()
        for raw in titles:
            words = re.findall(r"[a-z0-9]+", str(raw or "").lower())
            for n in range(_ROOT_NGRAM_MIN, _ROOT_NGRAM_MAX + 1):
                if len(words) < n:
                    break
                for gram in {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}:
                    counts[" ".join(gram)] += 1
        return {p: c for p, c in counts.items() if c >= _ROOT_NGRAM_MIN_SUPPORT}

    def _pick_root(self, cat: str, titles: list[str]) -> str:
        """根词规则(乙): 覆盖类目名实词最多 → 覆盖率最高 → 词数更长; 无匹配回落最高频双词。"""
        grams = self._title_ngrams(titles)
        total = sum(1 for t in titles if str(t or "").strip())
        cat_words = self._cat_words(cat)
        best_key, best_phrase = None, ""
        for phrase, cnt in grams.items():
            if not total:
                break
            cov = cnt / total
            if cov < _ROOT_MIN_COVERAGE:
                continue
            matched = len({_norm_token(w) for w in phrase.split()} & cat_words)
            if matched == 0:
                continue
            key = (matched, cov, len(phrase.split()))
            if best_key is None or key > best_key:
                best_key, best_phrase = key, phrase
        if best_phrase:
            return best_phrase
        return self._top_bigram_clean(titles)

    def _top_bigram_clean(self, titles: list[str]) -> str:
        """兜底根词: 最高频相邻双词, 但**两端不得是虚词**(避免 `for home`/`and pepper` 这类碎片)。"""
        counter: Counter = Counter()
        for raw in titles:
            words = re.findall(r"[a-z0-9]+", str(raw or "").lower())
            for i in range(len(words) - 1):
                if words[i] in _MINE_TRAIL_STOPS or words[i + 1] in _MINE_TRAIL_STOPS:
                    continue
                counter[" ".join(words[i:i + 2])] += 1
        if counter:
            return counter.most_common(1)[0][0]
        return self._top_bigram(titles)

    # ---------- 4.3 长尾词 ----------

    def _longtail_per_dim(self, ctx: StepContext) -> int:
        """每维长尾条数: 配置 `LONGTAIL_PER_DIM` 优先; 其次兼容旧键 `LONGTAIL_COUNT // 维度数`。"""
        raw = ctx.config.get("LONGTAIL_PER_DIM", None)
        if raw is not None:
            try:
                n = int(raw)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                return n
            self.log(ctx, f"LONGTAIL_PER_DIM={raw!r} 非法, 回落默认 {_LONGTAIL_PER_DIM}")
        try:
            total = int(ctx.config.get("LONGTAIL_COUNT", 0) or 0)
        except (TypeError, ValueError):
            total = 0
        if total > 0:
            n = max(1, total // len(_DIM_RULES))
            self.log(ctx, f"未配置 LONGTAIL_PER_DIM, 按旧键 LONGTAIL_COUNT={total} 折算每维 {n} 条")
            return n
        return _LONGTAIL_PER_DIM

    def _generate_longtail(self, ctx: StepContext, cat: str, root: str,
                           texts: list[str], used: set,
                           per_dim: int | None = None) -> tuple[list[str], dict]:
        """五维度长尾: 每维"标题+五点里频率最高的实体" × 基座词。

        返回 (长尾词列表, 实体台账 {dim: {"entities": [...], "longtails": [...], "fallback": n}})。
        """
        per_dim = per_dim or self._longtail_per_dim(ctx)
        # 2026-09-09: 长尾词应围绕 达标词/核心产品根词; 生成时用 FR-02 的 配件/耗材/硬排除 词
        # 直接把"air ionizer 4 pack"这类非主营/配件长尾剔除, 避免进入候选与搜索清单。
        skip_terms = _load_skip_terms(ctx, cat)
        base = self._longtail_base(root)
        if base != root:
            self.log(ctx, f"长尾基座词: {base}(根词 {root} 去掉结尾场景短语, 避免 'for X for Y' 重复)")
        all_words: list[str] = []
        report: dict = {}
        used_entities: set[str] = set()
        for dim, (mode, rule) in _DIM_RULES.items():
            rows = self._extract_entities(ctx, cat, dim, rule, texts, per_dim, used_entities, skip_terms)
            self.log(ctx, f"长尾词[{dim}] 高频实体 {len(rows)}/{per_dim} 个: "
                          + (" | ".join(f"{r['entity']}({r['records']}品/{r['occ']}次/{r['source']})" for r in rows) or "(无)"))
            drafts: list[tuple[str, str]] = []
            for r in rows:
                if self._entity_in_text(r["entity"], base):
                    # 实体已在基座里(如基座含 cat -> cat/cats) -> 拼出来是同义重复, 跳过取下一个
                    self.log(ctx, f"  长尾词[{dim}] 跳过与基座重复的实体: {r['entity']}")
                    continue
                word = self._assemble_entity(dim, r["entity"], base)
                if not word or word in used or self._is_accessory(word, skip_terms):
                    continue
                if len(word.split()) > _LONGTAIL_MAX_WORDS:
                    continue
                drafts.append((r["entity"], word))
            words = self._polish_longtail(ctx, cat, dim, base, drafts, skip_terms, used)
            picked: list[str] = []
            for ent, word in words:
                if word and word not in used and not self._is_accessory(word, skip_terms):
                    used.add(word)
                    used_entities.add(ent)
                    picked.append(word)
            real = len(picked)
            filled = self._fill_longtail(picked, dim, base, used, per_dim, skip_terms)
            all_words.extend(filled)
            report[dim] = {"entities": rows, "longtails": filled, "fallback": len(filled) - real}
            self.log(ctx, f"长尾词[{dim}] {len(filled)} 个(实体派生 {real} + 兜底 {len(filled) - real})")
            self.log(ctx, f"  长尾词[{dim}]: " + " | ".join(filled))
        expected = per_dim * len(_DIM_RULES)
        if len(all_words) != expected:
            raise StepDataError(f"长尾词数量异常: {len(all_words)} != {expected}(每维 {per_dim} × {len(_DIM_RULES)} 维度)")
        return all_words, report

    # ---------- 4.3.1 五维实体提取(频率优先) ----------

    def _entity_prompt(self, cat: str, dim: str, rule: str, text: str, top: int) -> str:
        return f"""你是亚马逊类目文本实体抽取助手({dim}维度)。
类目: {display_cat(cat)}
要从下面的"标题 + 五点描述"文本里, 抽取属于【{dim}】维度的候选实体 —— 即: {rule}
要求:
1. 只输出在该文本里**真实出现**的英文词或短语(1~4 个词, 小写, 不要标点), 不要编造
2. 按你观察到的出现频率从高到低排序, 最多 {top * 3} 个
3. 不要品牌词 / 型号 / 颜色尺寸变体 / 配件耗材(/换芯/替换件/收纳袋等)
4. 同一实体只出现一次, 不要同义反复(如同时给 kids 和 children, 只留文本里更常见的一个)
严格输出 JSON(不要输出其他任何文字):
{{"entities": [{{"text": "kitchen", "note": "多条 listing 都提到"}}, ...]}}
文本:
{text}
"""

    def _llm_entities(self, ctx: StepContext, cat: str, dim: str, rule: str,
                      text: str, top: int) -> list[str]:
        """LLM 抽取该维度候选实体(失败返回空表, 不影响主流程)。"""
        try:
            raw = ctx.llm().chat_json(
                self._entity_prompt(cat, dim, rule, text, top),
                temperature=0.0,
                max_tokens=2000,
            )
        except LLMError as exc:
            self.log(ctx, f"长尾词[{dim}] LLM 实体抽取失败, 仅用本地词表: {exc}")
            return []
        out: list[str] = []
        if not isinstance(raw, dict):
            return out
        for item in raw.get("entities") or []:
            if isinstance(item, dict):
                txt = item.get("text") or item.get("entity") or item.get("keyword") or ""
            else:
                txt = item
            ent = self._normalize_text(str(txt))
            if ent and ent not in out:
                out.append(ent)
        return out

    def _count_entity(self, entity: str, texts: list[str]) -> tuple[int, int]:
        """实体在全部标题+五点里的 (含它的记录数, 总出现次数)。末尾词允许复数变化。"""
        words = entity.split()
        if not words:
            return 0, 0
        pat = r"\s+".join(re.escape(w) for w in words)
        last = words[-1]
        if not last.endswith("s"):
            pat += r"(?:s|es)?"
        regex = re.compile(r"(?<![a-z0-9])" + pat + r"(?![a-z0-9])")
        records = occurrences = 0
        for t in texts:
            n = len(regex.findall(t or ""))
            if n:
                records += 1
                occurrences += n
        return records, occurrences

    def _entity_allowed(self, dim: str, ent: str) -> bool:
        """维度实体的形态校验(仅靠词表不够: LLM 会塞进动词/名词短语)。

        功能: 必须是词表里的功能词, 或以形容词/分词尾(-ic/-al/-ble/-less/-ing/-ed/-ful/-ive,
              duty/operated/mounted/free/proof…)结尾 —— 挡掉 "chop"/"press"/"stainless steel blades"。
        痛点: 词表词, 或多词短语, 或形容词尾单字(leakproof/odorless…) —— 挡掉过泛的 "safe"。
        场景: 单字必须是词表里的地点词(挡掉 "bags"/"cabinets" 这类非地点名词)。
        人群/属性: 不限形态(本身就以名词为主)。
        """
        lexicon = _DIM_LEXICON.get(dim, set())
        ent = str(ent or "").strip()
        if not ent:
            return False
        if ent in lexicon or _norm_token(ent) in {_norm_token(t) for t in lexicon}:
            return True
        if dim == "功能":
            if ent in PART_NOUNS:
                return False
            return bool(_ADJ_TAIL_RE.search(ent))
        if dim == "属性":
            # 纯零部件名词(blade/bowl/motor…)在 属性 维度只收多词短语(如 "airtight lid"),
            # 单词形式会拼出配件搜索(实战 "blade for food processor")
            return not (len(ent.split()) == 1 and ent in PART_NOUNS)
        if dim == "痛点":
            return len(ent.split()) >= 2 or bool(_ADJ_TAIL_RE.search(ent))
        if dim not in _SINGLE_WORD_OK_DIMS and len(ent.split()) < 2:
            return False
        return True

    def _longtail_base(self, root: str) -> str:
        """长尾基座词 = 根词剥掉结尾的场景介词短语(`air purifier for home` -> `air purifier`)。

        为什么: 场景维度组装时会再拼 "for <场景>", 基座自带 "for home" 就会出现
        "air purifier for home for home"/"…for home for bedroom" 这种重复短语(实战已出现)。
        剥掉后仍需剩 ≥2 词, 否则保持原样(如 `board for kitchen` 只剩 board, 不剥)。
        """
        base = str(root or "").strip()
        stripped = _LONGTAIL_BASE_TAIL_RE.sub("", base)
        if stripped and len(stripped.split()) >= 2:
            return stripped
        return base

    @staticmethod
    def _entity_in_text(ent: str, text: str) -> bool:
        """实体(按复数归一的词)是否已全部出现在基座词里 —— 跳过重复实体(如 base 含 cat -> 跳过 cat/cats)。"""
        return entity_in_base(ent, text)

    def _extract_entities(self, ctx: StepContext, cat: str, dim: str, rule: str,
                          texts: list[str], top: int, used_entities: set,
                          skip_terms: set) -> list[dict]:
        """该维度候选实体 = 本地词表 ∪ LLM 抽取 -> 在文本上确定性计数 -> 频率降序取前 top。"""
        candidates: dict[str, str] = {}
        for term in sorted(_DIM_LEXICON.get(dim, ())):
            ent = self._normalize_text(term)
            if ent:
                candidates.setdefault(ent, "词表")
        blob = " ".join(texts)[:_TEXT_CAP]
        for ent in self._llm_entities(ctx, cat, dim, rule, blob, top):
            candidates.setdefault(ent, "LLM")
        rows: list[dict] = []
        for ent, source in candidates.items():
            if len(ent) < 3 or ent in used_entities:
                continue
            if len(ent.split()) > 4 or ent in _MINE_TRAIL_STOPS:
                continue
            if not self._entity_allowed(dim, ent):
                continue
            if any(re.search(rf"\b{re.escape(b)}\b", ent) for b in _BRAND_BLACKLIST):
                continue
            if self._is_accessory(ent, skip_terms):
                continue
            records, occurrences = self._count_entity(ent, texts)
            if records <= 0:                    # 文本里没出现 -> 无证据, 淘汰
                continue
            rows.append({"entity": ent, "records": records, "occ": occurrences, "source": source})
        # 频率优先(提到它的商品数 -> 出现次数); 同频时**更长/更具体的短语优先**, 再按字典序稳定排序
        rows.sort(key=lambda r: (-r["records"], -r["occ"], -len(r["entity"].split()), r["entity"]))
        picked: list[dict] = []
        for row in rows:
            tokens = {_norm_token(t) for t in row["entity"].split()}
            if any(tokens <= {_norm_token(t) for t in p["entity"].split()}
                   or {_norm_token(t) for t in p["entity"].split()} <= tokens
                   for p in picked):        # 同维近义重复(如 stainless/stainless steel、kid/kids)只留一个
                continue
            picked.append(row)
            if len(picked) >= top:
                break
        return picked

    def _assemble_entity(self, dim: str, entity: str, base: str) -> str:
        """实体 → 长尾草稿(维度模板在 steps/_keywords.py, 与 FR-09 抓取词组装同一套规则)。"""
        return assemble_entity(dim, entity, base)

    def _polish_prompt(self, cat: str, dim: str, base: str, drafts: list[tuple[str, str]]) -> str:
        items = "\n".join(f"{i}. 实体: {ent} | 草稿: {word}" for i, (ent, word) in enumerate(drafts, 1))
        return f"""你是亚马逊搜索词润色助手({dim}维度)。
类目: {display_cat(cat)}
基座词: {base}
把下面每条草稿改写成更符合亚马逊真实搜索习惯的长尾搜索词:
{items}
硬性要求:
1. 每条必须**原样包含**基座词 "{base}" 和它对应的**实体词**(不要换成同义词)
2. **只做必要的语序/连接词调整, 不要新增草稿里没有的词**(不得加 kitchen/use/small/container
   这类填充词), 词数不得比草稿多 2 个以上
3. 每条 ≤ {_LONGTAIL_MAX_WORDS} 个英文单词, 小写, 无标点, 无品牌词, 无配件/耗材词
4. 与草稿**一一对应**且顺序不变, 恰好 {len(drafts)} 条
5. 若草稿读起来重复(基座已含同一场景/人群词), 改成不重复的同义表达
   (如 "air purifier for home for bedroom" -> "air purifier for bedroom for home")
严格输出 JSON(不要输出其他任何文字):
{{"longtail": [{{"keyword": "..."}}, ...]}}
"""

    def _polish_longtail(self, ctx: StepContext, cat: str, dim: str, base: str,
                         drafts: list[tuple[str, str]], skip_terms: set,
                         used: set) -> list[tuple[str, str]]:
        """LLM 润色模板草稿; 任一环节不合规(缺基座/缺实体/超词数/配件词/重复)即回落草稿。"""
        if not drafts:
            return []
        polished: list[str] = []
        try:
            raw = ctx.llm().chat_json(
                self._polish_prompt(cat, dim, base, drafts),
                temperature=0.0,
                max_tokens=1500,
            )
            if isinstance(raw, dict):
                for item in raw.get("longtail") or []:
                    txt = item.get("keyword") if isinstance(item, dict) else item
                    polished.append(self._normalize_text(str(txt or "")))
        except LLMError as exc:
            self.log(ctx, f"长尾词[{dim}] LLM 润色失败, 使用模板草稿: {exc}")
        out: list[tuple[str, str]] = []
        seen = set(used)
        for i, (ent, draft) in enumerate(drafts):
            cand = polished[i] if i < len(polished) else ""
            word = draft
            if cand and base in cand and ent in cand and len(cand.split()) <= _LONGTAIL_MAX_WORDS \
                    and not self._is_accessory(cand, skip_terms) and cand not in seen \
                    and not (dim == "人群" and f"for {ent}" not in cand) \
                    and not (dim == "功能" and not cand.startswith(ent)):
                # 人群: 必须保持 "for <人群>" 语序(否则 LLM 会把 "food processor for baby"
                #       改成 "baby food processor" —— 语义变成另一个品类);
                # 功能: 必须保持 "<功能> <产品>" 语序(否则 "fan speed air purifier"
                #       会被改成不通顺的 "air purifier fan speed")
                word = cand
            out.append((ent, word))
            seen.add(word)
        return out

    def _is_accessory(self, word: str, skip_terms: set) -> bool:
        """长尾词是否命中 配件/耗材/硬排除 词。"""
        if not skip_terms:
            return False
        wl = word.lower()
        wnorm = wl.replace(" ", "")
        for t in skip_terms:
            if not t:
                continue
            if t in wl or t.replace(" ", "") in wnorm:
                return True
        return False

    def _fill_longtail(self, words: list[str], dim: str, root: str, used: set, per_dim: int,
                       skip_terms: set | None = None) -> list[str]:
        skip_terms = skip_terms or set()
        result = list(words)
        for suffix in _DIM_FALLBACK[dim]:
            if len(result) >= per_dim:
                break
            word = self._assemble_entity(dim, suffix, root)
            if word and word not in used and not self._is_accessory(word, skip_terms):
                used.add(word)
                result.append(word)
        return result

    def _entity_report_text(self, cat: str, root: str, total_records: int,
                            per_dim: int, report: dict) -> str:
        """五维实体台账(人工可读 + FR-09 机读): 实体 + 商品数/出现次数 + 来源 + 派生的长尾词。

        分区格式 `[维度.实体]` / `[维度.长尾词]` 由 steps/_keywords.py 统一约定,
        FR-09 用 parse_entity_artifact() 读它, 拿每维最高频实体 × 搜索量最高的达标词组装抓取词。
        """
        lines = [
            f"# FR-04 五维实体台账 —— {cat}",
            f"# 基座词(核心根词): {root}",
            f"# 语料: FR-03 过滤后榜单详情 {total_records} 条(标题+五点描述)",
            f"# 每维取频率最高的 {per_dim} 个实体组装长尾; 商品数=提到该实体的记录数(优先级), 出现=总次数",
            f"# 来源: 词表=本地维度词表命中, LLM=模型从语料抽出的候选(同样经过语料计数验证)",
            "# 本文件是 FR-09 组装抓取词的输入(每维最高频实体 × 搜索量最高的达标词), 勿手工删分区",
        ]
        for dim in _DIM_RULES:
            info = report.get(dim) or {}
            lines.append(entity_section(dim, "实体"))
            rows = info.get("entities") or []
            if not rows:
                lines.append("# (无: 语料里没有该维度的可计数实体, 长尾全部由通用模板兜底)")
            for i, r in enumerate(rows, 1):
                lines.append(f"{i}\t{r['entity']}\t商品数={r['records']}\t出现={r['occ']}\t来源={r['source']}")
            lines.append(entity_section(dim, "长尾词"))
            lines.append(f"# 兜底 {info.get('fallback', 0)} 条")
            for w in info.get("longtails") or []:
                lines.append(w)
        return "\n".join(lines) + "\n"


def _to_float(value) -> float | None:
    """转 float, 缺失或不可解析返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_skip_terms(ctx, cat: str) -> set:
    """从 FR-02 过滤提示词取 配件/耗材/硬排除 词(长尾生成时剔除, 防"air ionizer 4 pack"类配件长尾混入)。"""
    fp = read_json(ctx.resolve_artifact(f"filter-{cat}-prompt.json"), default={})
    terms: set[str] = set()
    for k in ("accessories", "consumables", "hard_exclude"):
        for t in (fp.get(k) or []):
            s = str(t).strip().lower()
            if s:
                terms.add(s)
    return terms
