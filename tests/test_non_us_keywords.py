# 单元测试: 非 US 站点的关键词口径(2026-09-15 用户需求)
# ---------------------------------------------------------------------------
# 需求原文: "针对除 US 站点外的其他站点, 无法使用 SCREEN_REQUIREMENT: 月搜索量>=3万,
#   站内同比>=-20%, Google同比>=-20%, CPC<3 这些条件去看关键词, 所以当调研除 US 站点外的
#   其他站点时不需要设置这些参数, 直接取搜索量前 5 的关键词就行; 长尾词的生成是用达标的
#   核心关键词生成, 在调研除 US 站点外的其他站点时, 长尾词的生成就用最大搜索量的核心关键词生成。"
#
# 落到代码上的三条口径:
#   ① is_us_site(): 站点判定(MARKET → SITE → CAT 地区码), 判不出时保守按 US(老配置不变);
#   ② pick_top_volume_candidates(): 站内月搜索量降序前 5, 缺搜索量的词直接排除;
#   ③ 步骤 9/10 的消费点: 非 US 站跳过开品门槛、达标核心=前 5、长尾基座=其中搜索量最高者,
#      且步骤10 的判定参考词与步骤9 用**同一批词**(2026-10-06 起直接同步「关键词清单」的
#      生效核心大词(长尾词不参与注入), 避免"搜索用前5、判定参考词为空"的错配)。
import json
import os

from core.category_config import (
    NON_US_QUALIFIED_TOP,
    is_us_site,
    pick_top_volume_candidates,
)
from core.config import Config
from core.paths import Paths
from steps.base import StepContext
from steps.step09_search_collect import Step09SearchCollect
from steps.step10_variants import Step10Variants


# ---------------------------------------------------------------- ① 站点判定
def test_is_us_site_by_market_domain():
    """MARKET 域名是首要判据(含带 www / 完整 URL 的写法)。"""
    assert is_us_site(market="amazon.com") is True
    assert is_us_site(market="www.amazon.com") is True
    assert is_us_site(market="https://www.amazon.com/") is True
    assert is_us_site(market="amazon.de") is False
    assert is_us_site(market="amazon.co.uk") is False
    assert is_us_site(market="amazon.fr") is False
    assert is_us_site(market="www.amazon.co.jp") is False
    # 注意: amazon.com.mx 是墨西哥站, 不能被 "startswith(amazon.com)" 误判成美国站
    assert is_us_site(market="amazon.com.mx") is False
    assert is_us_site(market="amazon.com.br") is False


def test_is_us_site_by_seller_sprite_site_name():
    """SITE 是卖家精灵站点名(中文); 只有"美国"算 US 口径。"""
    assert is_us_site(site="美国") is True
    assert is_us_site(site="德国") is False
    assert is_us_site(site="英国") is False
    assert is_us_site(site="de") is False          # 也容忍直接写地区码
    assert is_us_site(site="us") is True


def test_is_us_site_precedence_and_fallback():
    """优先级 MARKET > SITE > CAT 地区码; 三者都判不出时保守按 US(老配置行为不变)。"""
    assert is_us_site(site="美国", market="amazon.de") is False        # MARKET 优先
    assert is_us_site(site="德国", market="amazon.com") is True
    assert is_us_site(cat="Cutting Boards-DE-202609") is False         # 靠 CAT 后缀兜底
    assert is_us_site(cat="Cutting Boards-GB-202609") is False
    assert is_us_site(cat="Salt and Pepper Mill Sets-US-202608") is True
    assert is_us_site() is True                                        # 无从判断 → 按 US
    assert is_us_site(cat="salt-pepper-shaker-sets") is True


# ---------------------------------------------------------------- ② 选词: 搜索量前 5
def test_pick_top_volume_candidates_ranks_and_limits():
    """按站内月搜索量降序取前 5; 同量按词字典序(与步骤9 既有排序口径一致)。"""
    rows = [
        {"keyword": "b", "monthly_search_volume": 100},
        {"keyword": "a", "monthly_search_volume": 100},
        {"keyword": "c", "monthly_search_volume": 900},
        {"keyword": "d", "monthly_search_volume": 50},
        {"keyword": "e", "monthly_search_volume": 400},
        {"keyword": "f", "monthly_search_volume": 300},
        {"keyword": "g", "monthly_search_volume": 200},
    ]
    picked = pick_top_volume_candidates(rows)
    assert len(picked) == NON_US_QUALIFIED_TOP == 5
    assert picked == ["c", "e", "f", "g", "a"]        # 900,400,300,200,100(a 先于 b)


def test_pick_top_volume_candidates_drops_missing_volume():
    """缺搜索量(缺失/None/非数值)的词**直接排除**, 不占名额; 因此可能不足 5 个。"""
    rows = [
        {"keyword": "ok1", "monthly_search_volume": 10},
        {"keyword": "no1"},                                        # 缺字段
        {"keyword": "no2", "monthly_search_volume": None},
        {"keyword": "no3", "monthly_search_volume": "abc"},
        {"keyword": "no4", "monthly_search_volume": ""},
        {"keyword": "ok2", "monthly_search_volume": 20},
    ]
    assert pick_top_volume_candidates(rows) == ["ok2", "ok1"]


def test_pick_top_volume_candidates_edge_cases():
    """零量/负量仍是数值(参与排序); 空输入/非列表/重复词都安全。"""
    assert pick_top_volume_candidates([]) == []
    assert pick_top_volume_candidates(None) == []
    assert pick_top_volume_candidates([{"keyword": "x"}]) == []
    rows = [{"keyword": "dup", "monthly_search_volume": 5},
            {"keyword": "dup", "monthly_search_volume": 999},
            {"keyword": "zero", "monthly_search_volume": 0}]
    assert pick_top_volume_candidates(rows) == ["dup", "zero"]


# ---------------------------------------------------------------- ③ 步骤 9 / 10 的消费点
def _make_ctx(tmp_dir: str, **extra) -> StepContext:
    data = {"CAT": "t", "DATA_ROOT": tmp_dir, "DRY_RUN": True}
    data.update(extra)
    cfg = Config(data)
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths)


def _write_kw_data(ctx: StepContext, rows: list) -> None:
    with open(ctx.resolve_artifact(f"{ctx.config.cat}-amazon-keyword-data.json"),
              "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False)


def test_step09_non_us_core_is_top5_not_screen(ws_tmp):
    """非 US 站: 达标核心 = 站内月搜索量前 5(与 FR-07 判定无关)。"""
    ctx = _make_ctx(ws_tmp, SITE="德国", MARKET="amazon.de")
    _write_kw_data(ctx, [{"keyword": f"w{i}", "monthly_search_volume": i * 100}
                         for i in range(1, 8)])
    s = Step09SearchCollect()
    assert s._us_site(ctx) is False
    core = s._top_volume_candidates(ctx)
    assert core == ["w7", "w6", "w5", "w4", "w3"]


def test_step09_us_still_uses_screen(ws_tmp):
    """US 站: 仍按原口径(由 FR-07 判定), 不取"前5"。"""
    ctx = _make_ctx(ws_tmp, SITE="美国", MARKET="amazon.com")
    s = Step09SearchCollect()
    assert s._us_site(ctx) is True


def test_step10_non_us_injection_matches_step09_core(ws_tmp):
    """非 US 站: 步骤10 自动注入词 = 步骤9 那批"搜索量前 5", 两处必须同一批。"""
    ctx = _make_ctx(ws_tmp, SITE="法国", MARKET="amazon.fr")
    rows = [{"keyword": f"kw{i}", "monthly_search_volume": i * 10} for i in range(1, 8)]
    _write_kw_data(ctx, rows)
    # FR-07 结果故意给空(非 US 站不该依赖它) —— 若实现回退到 screen 就会返回 []
    with open(ctx.resolve_artifact(f"{ctx.config.cat}-screen-results.json"),
              "w", encoding="utf-8") as fh:
        json.dump([], fh, ensure_ascii=False)

    s10 = Step10Variants()
    injected = s10._final_keywords(ctx, ctx.config.cat)
    s9 = Step09SearchCollect()
    core = [w.lower() for w in s9._top_volume_candidates(ctx)]
    assert injected == core == ["kw7", "kw6", "kw5", "kw4", "kw3"]


def test_step10_us_injection_uses_screen_verdicts(ws_tmp):
    """US 站: 注入词仍 = FR-07 全部达标词(原行为不变)。"""
    ctx = _make_ctx(ws_tmp, SITE="美国", MARKET="amazon.com")
    _write_kw_data(ctx, [{"keyword": "a", "monthly_search_volume": 500000}])
    with open(ctx.resolve_artifact(f"{ctx.config.cat}-screen-results.json"),
              "w", encoding="utf-8") as fh:
        json.dump([{"keyword": "A", "verdict": "达标"},
                   {"keyword": "B", "verdict": "不达标"}], fh, ensure_ascii=False)
    out = Step10Variants()._final_keywords(ctx, ctx.config.cat)
    assert out == ["a"]                                # 只取 verdict=达标 的


def test_step10_follows_manual_core_list_on_non_us(ws_tmp):
    """人工核心清单优先级最高: 非 US 站也一样(用户手填的词不被"前5"覆盖)。"""
    ctx = _make_ctx(ws_tmp, SITE="德国", MARKET="amazon.de")
    _write_kw_data(ctx, [{"keyword": f"kw{i}", "monthly_search_volume": i} for i in range(1, 9)])
    with open(ctx.resolve_artifact(f"filter-{ctx.config.cat}-core-words.txt"),
              "w", encoding="utf-8") as fh:
        fh.write("manual one\nmanual two\n")
    out = Step10Variants()._final_keywords(ctx, ctx.config.cat)
    assert out == ["manual one", "manual two"]


# ---------------------------------------------------------------- ④ dry-run 替身完整性
def test_mock_page_supports_step09_calls():
    """MockPage 必须提供步骤9 会用到的全部方法(2026-09-15 补 wait_for_timeout)。

    背景: 步骤9 等结果卡渲染后会调 `page.wait_for_timeout(1500)`, 而 MockPage 缺该方法
    → **dry-run 跑步骤9 必抛 AttributeError**("dry-run 全流程冒烟"因此覆盖不到步骤9)。
    本次非 US 站验收时正是先撞上它才发现的, 故在此钉死。
    """
    from core.browser import MockPage

    page = MockPage(browser=None)          # 替身不依赖真实 browser
    assert isinstance(page.content(), str) and page.content()
    assert page.title() == "mock"
    page.wait_for_timeout(1500)            # 不应抛异常, 也不阻塞
