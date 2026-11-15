# 单元测试: FR-09 抓取词(2026-09-13 口径) —— 每维频率最高实体 × 搜索量最高的达标词
#
# 背景: 旧口径是"达标核心词 + 按相关度取最多 5 个长尾词", 且长尾还必须含核心大词的
# "产品名词头"(硬门槛) —— Food Processors 实战里根词(processor meat grinder)与达标词
# (food chopper/vegetable chopper)无共同词头 → 20 条长尾全被剔 → 相关长尾 0。
# 新口径: 基座 = 搜索量最高的**达标**词; 每维取 FR-04 台账里频率最高的实体组装 1 条, 共 5 条;
# 组装出的抓取词天然与达标词同头, 不再需要词头门槛(该门槛已降级为排序权重, 见 ③)。
import json
import os

from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step09_search_collect import Step09SearchCollect

CAT = "t"
BASE_ARTIFACT = (
    "# FR-04 五维实体台账 —— t\n"
    "[场景.实体]\n"
    "1\tkitchen\t商品数=42\t出现=55\t来源=词表\n"
    "2\ttravel\t商品数=18\t出现=20\t来源=LLM\n"
    "[场景.长尾词]\n"
    "# 兜底 0 条\n"
    "dog food storage for kitchen\n"
    "[人群.实体]\n"
    "1\tfor seniors\t商品数=12\t出现=14\t来源=词表\n"
    "[人群.长尾词]\n"
    "# 兜底 0 条\n"
    "dog food storage for seniors\n"
    "[功能.实体]\n"
    "1\telectric\t商品数=9\t出现=11\t来源=词表\n"
    "[功能.长尾词]\n"
    "# 兜底 0 条\n"
    "electric dog food storage\n"
    "[属性.实体]\n"
    "1\tairtight lid\t商品数=7\t出现=8\t来源=LLM\n"
    "[属性.长尾词]\n"
    "# 兜底 0 条\n"
    "dog food storage with airtight lid\n"
    "[痛点.实体]\n"
    "1\teasy to clean\t商品数=31\t出现=40\t来源=词表\n"
    "[痛点.长尾词]\n"
    "# 兜底 0 条\n"
    "dog food storage easy to clean\n"
)


def _ctx(tmp_dir: str, extra: dict | None = None) -> StepContext:
    data = {"CAT": CAT, "DATA_ROOT": tmp_dir, "DRY_RUN": True}
    data.update(extra or {})
    cfg = Config(data)
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths)


def _write(ctx: StepContext, name: str, payload) -> None:
    path = ctx.resolve_artifact(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(payload, str):
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)


def _keyword_data(rows) -> list:
    return [{"keyword": kw, "monthly_search_volume": vol} for kw, vol in rows]


# ---------- 基座 = 搜索量最高的达标词 ----------

def test_top_volume_core_picks_highest_volume(ws_tmp):
    ctx = _ctx(ws_tmp)
    _write(ctx, f"{CAT}-amazon-keyword-data.json",
           _keyword_data([("food storage", 69859), ("dog food storage container", 12100),
                          ("dog food storage", 45000)]))
    s = Step09SearchCollect()
    base = s._top_volume_core(ctx, ["dog food storage", "dog food storage container"])
    assert base == "dog food storage"


class _Log:
    """假 logger: base.log() 调的是 ctx.log.info(...)。"""

    def __init__(self):
        self.msgs: list[str] = []

    def info(self, msg):
        self.msgs.append(str(msg))


def test_top_volume_core_missing_data_ranks_last_and_logs(ws_tmp):
    ctx = _ctx(ws_tmp)
    _write(ctx, f"{CAT}-amazon-keyword-data.json", _keyword_data([("a b", 1000)]))
    logs = _Log()
    ctx.log = logs
    s = Step09SearchCollect()
    assert s._top_volume_core(ctx, ["no data word", "a b"]) == "a b"
    assert any("缺搜索量数据" in m for m in logs.msgs)


def test_top_volume_core_empty_input(ws_tmp):
    ctx = _ctx(ws_tmp)
    assert Step09SearchCollect()._top_volume_core(ctx, []) == ""


# ---------- 抓取词: 每维 1 条 × 基座 ----------

def test_crawl_keywords_one_per_dimension(ws_tmp):
    ctx = _ctx(ws_tmp)
    _write(ctx, f"{CAT}-keywords-entities.txt", BASE_ARTIFACT)
    _write(ctx, f"{CAT}-amazon-keyword-data.json",
           _keyword_data([("dog food storage", 45000), ("dog food storage bin", 9000)]))
    s = Step09SearchCollect()
    words = s._crawl_keywords(ctx, ["dog food storage bin", "dog food storage"], set())
    assert words == [
        "dog food storage for kitchen",       # 场景: for <实体>(顺带剥掉实体的 for 前缀)
        "dog food storage for seniors",       # 人群
        "electric dog food storage",          # 功能: <实体> <基座>
        "dog food storage with airtight lid",  # 属性: 容器/结构件名词 -> with
        "dog food storage easy to clean",     # 痛点: 短语 -> 后置
    ]


def test_crawl_keywords_skips_missing_dimension_and_accessory(ws_tmp):
    ctx = _ctx(ws_tmp)
    text = ("[场景.实体]\n1\tkitchen\n"
            "[属性.实体]\n1\treplacement filter\n")
    _write(ctx, f"{CAT}-keywords-entities.txt", text)
    _write(ctx, f"{CAT}-amazon-keyword-data.json", _keyword_data([("dog food storage", 45000)]))
    s = Step09SearchCollect()
    words = s._crawl_keywords(ctx, ["dog food storage"], {"replacement"})
    assert words == ["dog food storage for kitchen"]     # 配件实体被剔除, 缺维度跳过


def test_crawl_keywords_skips_entity_already_in_base(ws_tmp):
    """基座已含该实体(基座 cat water fountain + 实体 cat) -> 顺位取下一个实体, 不拼出重复短语。"""
    ctx = _ctx(ws_tmp)
    _write(ctx, f"{CAT}-keywords-entities.txt",
           "[人群.实体]\n1\tcat\t商品数=63\t出现=466\t来源=词表\n"
           "2\tpets\t商品数=47\t出现=92\t来源=词表\n")
    _write(ctx, f"{CAT}-amazon-keyword-data.json",
           _keyword_data([("cat water fountain", 60000)]))
    s = Step09SearchCollect()
    words = s._crawl_keywords(ctx, ["cat water fountain"], set())
    assert words == ["cat water fountain for pets"]


def test_crawl_keywords_without_artifact_returns_empty(ws_tmp):
    ctx = _ctx(ws_tmp)
    _write(ctx, f"{CAT}-amazon-keyword-data.json", _keyword_data([("dog food storage", 45000)]))
    assert Step09SearchCollect()._crawl_keywords(ctx, ["dog food storage"], set()) == []


# ---------- 收集清单: 达标词全集 + 抓取词 ----------

def test_build_collect_list_core_plus_crawl_words(ws_tmp):
    ctx = _ctx(ws_tmp)
    _write(ctx, f"{CAT}-keywords-entities.txt", BASE_ARTIFACT)
    _write(ctx, f"{CAT}-amazon-keyword-data.json",
           _keyword_data([("dog food storage", 45000)]))
    _write(ctx, f"{CAT}-filter-prompt.json", {"accessories": [], "consumables": [],
                                              "hard_exclude": []})
    s = Step09SearchCollect()
    words = s._build_collect_list(ctx, ["dog food storage"], ["unused longtail"])
    assert words[0] == "dog food storage"                 # 达标核心词全部保留且在最前
    assert len(words) == 1 + 5
    assert "dog food storage for kitchen" in words
    assert "unused longtail" not in words                 # 新口径不再按相关度挑长尾


def test_build_collect_list_falls_back_to_relevance_when_no_artifact(ws_tmp):
    """实体台账缺失(FR-04 未重跑) -> 回退旧口径: 按相关度取长尾(词头只作排序权重)。"""
    ctx = _ctx(ws_tmp)
    _write(ctx, f"{CAT}-amazon-keyword-data.json", _keyword_data([("dog food storage", 45000)]))
    _write(ctx, f"{CAT}-filter-prompt.json", {"accessories": [], "consumables": [],
                                              "hard_exclude": []})
    s = Step09SearchCollect()
    longs = ["dog food storage for kitchen", "dog food storage for seniors",
             "dog food storage for kids", "dog food storage for travel",
             "dog food storage easy to clean", "dog food storage quiet"]
    words = s._build_collect_list(ctx, ["dog food storage"], longs)
    assert words[0] == "dog food storage"
    assert set(words[1:]) <= set(longs)
    assert len(words) == 1 + 5
