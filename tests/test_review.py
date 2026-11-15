# 单元测试: 过滤结果复核 review_filter_results(抽样/对比/审计 CSV) + 改判策略
import os

from core.config import Config
from core.paths import Paths
from steps._filtering import apply_review_policy, review_filter_results
from steps.base import StepContext


def _ctx(tmp_dir: str) -> StepContext:
    cfg = Config(
        {
            "CAT": "t",
            "DATA_ROOT": tmp_dir,
            "DRY_RUN": True,
            "MARKET": "amazon.com",
        }
    )
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths)


def _records(n: int = 8) -> list[dict]:
    return [
        {
            "request_asin": f"B0T{i:04d}",
            "title": f"product {i}",
            "ok": True,
            "keep": i % 2 == 0,
        }
        for i in range(n)
    ]


def test_review_full_writes_csv(ws_tmp):
    """全量复核: 全部记录参与, 写出含表头的审计 CSV。"""
    ctx = _ctx(ws_tmp)
    out = os.path.join(ctx.paths.cat_root, "t-复核-过滤审计.csv")
    stats = review_filter_results(
        ctx.llm(), "amazon.com", _records(), {"category": "c"},
        limit=0, out_path=out,
    )
    assert stats["total"] == 8
    assert stats["sample"] == 8
    assert stats["agree"] + stats["disagree"] == 8
    assert 0.0 <= stats["rate"] <= 1.0
    assert os.path.exists(out)
    with open(out, encoding="utf-8-sig") as fh:
        lines = fh.read().strip().splitlines()
    assert len(lines) == 9  # 表头 + 8 行


def test_review_sampling(ws_tmp):
    """抽查模式: 只复核 limit 条。"""
    ctx = _ctx(ws_tmp)
    stats = review_filter_results(
        ctx.llm(), "amazon.com", _records(), {"category": "c"},
        limit=3, seed=1, out_path=None,
    )
    assert stats["sample"] == 3
    assert stats["total"] == 3


def test_review_sampling_deterministic(ws_tmp):
    """同一种子抽查结果可复现。"""
    ctx = _ctx(ws_tmp)
    a = review_filter_results(
        ctx.llm(), "amazon.com", _records(), {"category": "c"}, limit=4, seed=7
    )
    b = review_filter_results(
        ctx.llm(), "amazon.com", _records(), {"category": "c"}, limit=4, seed=7
    )
    assert {r["来源标识"] for r in a["rows"]} == {r["来源标识"] for r in b["rows"]}


def test_review_empty_records(ws_tmp):
    """无记录时不写文件, 一致率=1。"""
    ctx = _ctx(ws_tmp)
    out = os.path.join(ctx.paths.cat_root, "t-复核-过滤审计.csv")
    stats = review_filter_results(
        ctx.llm(), "amazon.com", [], {"category": "c"}, limit=0, out_path=out,
    )
    assert stats["total"] == 0
    assert stats["rate"] == 1.0
    assert not os.path.exists(out)


def test_review_disagree_list_file(ws_tmp):
    """有不一致项时写出单独清单文件; 无不一致时不写。"""
    ctx = _ctx(ws_tmp)
    dout = os.path.join(ctx.paths.cat_root, "t-复核-不一致项.csv")
    stats = review_filter_results(
        ctx.llm(), "amazon.com", _records(), {"category": "c"},
        limit=0, out_path=None, disagree_path=dout,
    )
    if stats["disagree"] > 0:
        assert os.path.exists(dout)
        with open(dout, encoding="utf-8-sig") as fh:
            lines = fh.read().strip().splitlines()
        assert len(lines) == stats["disagree"] + 1  # 表头 + 不一致条数
    else:
        assert not os.path.exists(dout)


def _rec(asin: str, keep: bool) -> dict:
    return {"request_asin": asin, "title": asin, "ok": True, "keep": keep}


def test_policy_keep_original_no_change():
    """keep_original: 只提示不处理, 不改任何记录。"""
    records = [_rec("B0A", True), _rec("B0B", False)]
    disagree = [
        {"来源标识": "B0A", "复核判定": "剔除", "原判定": "保留"},
    ]
    assert apply_review_policy(records, disagree, "keep_original") == 0
    assert records[0]["keep"] is True


def test_policy_follow_review():
    """follow_review: 不一致项跟随复核判定。"""
    records = [_rec("B0A", True), _rec("B0B", False)]
    disagree = [
        {"来源标识": "B0A", "复核判定": "剔除", "原判定": "保留", "原因": "标题含配件词"},
        {"来源标识": "B0B", "复核判定": "保留", "原判定": "剔除", "原因": "命中相关词"},
    ]
    changed = apply_review_policy(records, disagree, "follow_review")
    assert changed == 2
    assert records[0]["keep"] is False
    assert records[0]["reason"] == "标题含配件词"
    assert records[1]["keep"] is True
    assert records[1]["reason"] == "命中相关词"


def test_policy_drop_on_disagree():
    """drop_on_disagree: 不一致一律剔除(仅翻转保留→剔除)。"""
    records = [_rec("B0A", True), _rec("B0B", False)]
    disagree = [
        {"来源标识": "B0A", "复核判定": "剔除", "原判定": "保留"},
        {"来源标识": "B0B", "复核判定": "保留", "原判定": "剔除"},
    ]
    changed = apply_review_policy(records, disagree, "drop_on_disagree")
    assert changed == 1  # B0A 被剔除; B0B 本来就是剔除
    assert records[0]["keep"] is False
    assert "复核不一致" in records[0]["reason"]
    assert records[1]["keep"] is False
