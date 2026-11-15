# 单元测试: 步骤10 并行抓取(FETCH_WORKERS 多线程, 失败重试, 断点)
import contextlib
import csv
import json
import os
from unittest import mock

from core.config import Config
from core.http_client import HttpResponse
from core.paths import Paths
from core.io_utils import append_jsonl, read_jsonl
from steps.base import StepContext
from steps.step10_variants import (
    _TERMINAL_STATUSES,
    Step10Variants,
    _is_terminal,
)


class _FakeHttp:
    """按调用次序返回 (status_class, text) 的假 HttpFetcher(只实现 _fetch_one 用到的 get)。"""

    def __init__(self, responses: list[tuple[str, str]]):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url: str, retries: int = 1) -> HttpResponse:
        self.calls += 1
        status_class, text = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return HttpResponse(200 if status_class == "ok" else 503, text, url, True, status_class)


def _ctx(tmp_dir: str, workers: int = 3, stall_rounds: int = 3) -> StepContext:
    cfg = Config(
        {
            "CAT": "t",
            "DATA_ROOT": tmp_dir,
            "DRY_RUN": True,
            "FETCH_WORKERS": workers,
            "FETCH_ROUND_PAUSE": 0,
            "FETCH_STALL_ROUNDS": stall_rounds,
        }
    )
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths)


def test_parallel_fetch_all(ws_tmp):
    ctx = _ctx(ws_tmp)
    asins = [f"B0TEST{i:04d}" for i in range(1, 13)]
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    records = s._fetch_all(ctx, asins, out)
    assert len(records) == 12
    assert all(r.get("ok") for r in records)


def test_fetch_all_skips_done_retries_failed(ws_tmp):
    ctx = _ctx(ws_tmp)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    # 预置: 1 成功 + 1 失败
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"request_asin": "B0F3768982", "ok": True}) + "\n")
        fh.write(json.dumps({"request_asin": "B00860F9BF", "ok": False}) + "\n")
    records = s._fetch_all(ctx, ["B0F3768982", "B00860F9BF", "B071D99E75"], out)
    by_asin = {r["request_asin"]: r for r in records}
    # 成功记录保留; 失败记录重试(新抓取应 ok=True); 新 ASIN 抓取
    assert by_asin["B0F3768982"]["ok"] is True
    assert by_asin["B00860F9BF"]["ok"] is True
    assert by_asin["B071D99E75"]["ok"] is True


def test_single_worker_path(ws_tmp):
    ctx = _ctx(ws_tmp, workers=1)
    asins = ["B06D2E1A16", "B0602F3E4F"]
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    records = s._fetch_all(ctx, asins, out)
    assert len(records) == 2


def test_multi_round_retry_until_success(ws_tmp):
    """失败记录自动重试: 第1轮失败 -> 第2轮成功, 全部 ok 停止。"""
    ctx = _ctx(ws_tmp)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()

    real_fetch_one = s._fetch_one
    call = {"n": 0}
    fail_asin = "B0FAILRETRY"

    def flaky(step_ctx, http, asin, attempts=4):
        if asin == fail_asin and call["n"] == 0:
            call["n"] += 1
            return s._failed_record(asin, "bot_blocked", s.now())
        return real_fetch_one(step_ctx, http, asin, attempts=attempts)

    with mock.patch.object(s, "_fetch_one", side_effect=flaky):
        records = s._fetch_all(ctx, [fail_asin, "B0CFFBEC96"], out)
    by_asin = {r["request_asin"]: r for r in records}
    assert by_asin[fail_asin]["ok"] is True
    assert by_asin["B0CFFBEC96"]["ok"] is True


def test_multi_round_stops_at_fixed_retry_rounds(ws_tmp):
    """2026-09-09 定稿: 直接请求阶段固定 = 首轮 + FETCH_STALL_ROUNDS 轮重试(默认3)
    → 共 4 轮后停止(不再因失败数无下降提前停, 也不再无限重试); dry-run 不触发浏览器。"""
    ctx = _ctx(ws_tmp)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()

    def always_fail(step_ctx, http, asin):
        return s._failed_record(asin, "bot_blocked", s.now())

    with mock.patch.object(s, "_fetch_one", side_effect=always_fail), \
         mock.patch.object(s, "_fetch_round", wraps=s._fetch_round) as rnd:
        records = s._fetch_all(ctx, ["B0PERSIST01", "B0PERSIST02"], out)
        # 首轮 + 3 轮固定重试 = 共 4 轮后停止
        assert rnd.call_count == 4
    assert len(records) == 2
    assert all(not r.get("ok") for r in records)


def test_multi_round_fixed_retry_rounds_count(ws_tmp):
    """固定重试轮数可配置: FETCH_STALL_ROUNDS=2 -> 首轮 + 2 轮 = 3 轮上限;
    每轮失败集合即使都在变化(看似有改善), 也只在固定轮数内重试, 之后停止。"""
    ctx = _ctx(ws_tmp, stall_rounds=2)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    round_no = {"n": 0}

    def fake_round(step_ctx, pending, path):
        round_no["n"] += 1
        n = round_no["n"]
        # 每轮失败 ASIN 名字不同(失败集合变化)
        return [s._failed_record(f"B0FAIL{n:04d}", "bot_blocked", s.now())]

    with mock.patch.object(s, "_fetch_round", side_effect=fake_round):
        s._fetch_all(ctx, ["B0START0001"], out)
    assert round_no["n"] == 3  # 首轮 + 2 轮固定重试


def test_multi_round_success_within_fixed_rounds(ws_tmp):
    """固定重试轮数内成功即提前结束: 前3轮失败、第4轮成功(默认 stall_rounds=3
    → 上限首轮+3轮=4), 全部 ok 后停止, 不再多跑。"""
    ctx = _ctx(ws_tmp)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    real_fetch_one = s._fetch_one
    call = {"n": 0}
    fail_asin = "B0FLAKY0001"

    def flaky(step_ctx, http, asin, attempts=4):
        if asin == fail_asin and call["n"] < 3:  # 前3轮失败, 第4轮成功
            call["n"] += 1
            return s._failed_record(asin, "bot_blocked", s.now())
        return real_fetch_one(step_ctx, http, asin, attempts=attempts)

    with mock.patch.object(s, "_fetch_one", side_effect=flaky), \
         mock.patch.object(s, "_fetch_round", wraps=s._fetch_round) as rnd:
        records = s._fetch_all(ctx, [fail_asin, "B0CFFBEC96"], out)
    assert rnd.call_count == 4  # 首轮 + 3 轮内达成全部成功
    by_asin = {r["request_asin"]: r for r in records}
    assert by_asin[fail_asin]["ok"] is True


def test_long_interval_applied_every_n(ws_tmp):
    """每 FETCH_LONG_EVERY 条应触发一次长间隔冷却(对齐实测稳定方案)。"""
    cfg = Config(
        {
            "CAT": "t",
            "DATA_ROOT": ws_tmp,
            "DRY_RUN": True,
            "FETCH_WORKERS": 1,  # 单线程便于计数
            "FETCH_LONG_EVERY": 2,
            "FETCH_LONG_MIN": 8,
            "FETCH_LONG_MAX": 15,
        }
    )
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    ctx = StepContext(cfg, paths)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    asins = ["B0B7366999", "B066602102", "B0BE298C67", "B008CF9BC8", "B01F109163"]
    with mock.patch("time.sleep") as sleep:
        s._fetch_all(ctx, asins, out)
    long_sleeps = [c.args[0] for c in sleep.call_args_list if c.args[0] > 5]
    # 每 2 条一次长间隔: 第2、4 条后触发(5 条 -> 2 次长间隔)
    assert len(long_sleeps) == 2
    for v in long_sleeps:
        assert 8 <= v <= 15


def test_build_detail_df_unique_variants():
    """2026-09-08 定稿: 明细 = 每个保留 ASIN 自己一行(自身标题/五点/全图), 不再展开家族。"""
    from steps.step10_variants import Step10Variants

    s = Step10Variants()
    kept = [
        {"request_asin": "V1", "parent_asin": "P1", "variant_asins": ["V1", "V2"],
         "variant_labels": {}, "images": ["img1"], "title": "A", "bullets": ["b1"],
         "ok": True, "status": "ok", "fetched_at": "t"},
        {"request_asin": "V2", "parent_asin": "P1", "variant_asins": ["V2"],
         "variant_labels": {}, "images": ["img2"], "title": "B", "bullets": ["b2"],
         "ok": True, "status": "ok", "fetched_at": "t"},
        {"request_asin": "X1", "parent_asin": "P2", "variant_asins": ["V2", "X2"],
         "variant_labels": {}, "images": ["img3"], "title": "C", "bullets": [],
         "ok": True, "status": "ok", "fetched_at": "t"},
        {"request_asin": "X2", "parent_asin": "P2", "variant_asins": ["X2"],
         "variant_labels": {}, "images": ["img4"], "title": "D", "bullets": [],
         "ok": True, "status": "ok", "fetched_at": "t"},
    ]
    df = s._build_detail_df(kept)
    # 每行 = 一个保留 ASIN 自身(不按家族展开, 不重复)
    assert df["变体ASIN"].nunique() == len(df) == 4
    assert set(df["变体ASIN"]) == {"V1", "V2", "X1", "X2"}
    # 2026-09-08(用户): 列名与列数与原来一致(含 来源ASIN/文本来源);
    # 内容仍是各 ASIN 自己页面的: 来源ASIN=变体自身, 文本来源恒为"自有"(无继承)
    # 2026-09-20(用户): 新增 `A+图片链接` 列(A+ 内容图单独成列, 不与图廊混)
    assert list(df.columns) == ["来源ASIN", "父ASIN", "变体ASIN", "文本来源", "图片链接",
                                "A+图片链接",
                                "标题", "五点描述", "是否成功", "状态", "抓取时间"]
    v2 = df[df["变体ASIN"] == "V2"].iloc[0]
    assert v2["来源ASIN"] == "V2" and v2["文本来源"] == "自有"
    assert v2["父ASIN"] == "P1"
    assert v2["标题"] == "B" and v2["图片链接"] == "img2" and v2["五点描述"] == "b2"
    # A+ 列在无 A+ 图时为空串(不是 NaN)
    assert v2["A+图片链接"] == ""


def test_build_detail_df_only_candidate_rows():
    """明细只输出候选(有自身成功记录的 ASIN); 仅出现在家族列表里、无自身记录的
    子变体(V9/V8)不出行——由全量变体抓取补齐后才进候选(2026-09-08 无继承)。"""
    from steps.step10_variants import Step10Variants

    s = Step10Variants()
    kept = [
        {"request_asin": "P3", "parent_asin": "", "variant_asins": ["P3", "V9", "V8"],
         "variant_labels": {}, "images": ["img5"], "title": "E", "bullets": [],
         "ok": True, "status": "ok", "fetched_at": "t"},
        {"request_asin": "V9", "parent_asin": "P3", "variant_asins": ["V9"],
         "variant_labels": {}, "images": ["img6"], "title": "G", "bullets": ["b9"],
         "ok": True, "status": "ok", "fetched_at": "t"},
    ]
    df = s._build_detail_df(kept)
    assert set(df["变体ASIN"]) == {"P3", "V9"}      # V8 无自身记录 => 不在候选/不出行
    v9 = df[df["变体ASIN"] == "V9"].iloc[0]
    assert v9["父ASIN"] == "P3" and v9["标题"] == "G"
    p3 = df[df["变体ASIN"] == "P3"].iloc[0]
    assert p3["父ASIN"] == "P3"                     # 无 parent_asin 时=自身


def test_merge_own_records_request_preferred_ok_only():
    """候选集: 只收成功记录、按 ASIN 去重、请求记录优先; 补抓失败(ok=false)不入候选。"""
    from steps.step10_variants import Step10Variants

    s = Step10Variants()
    req = [
        {"request_asin": "A1", "ok": True, "title": "request-title"},
        {"request_asin": "BAD", "ok": False, "status": "parse_failed"},
    ]
    vt = [
        {"request_asin": "A1", "ok": True, "title": "vt-title"},   # 同 ASIN 重复 -> 取请求记录
        {"request_asin": "C2", "ok": True, "title": "child-title"},
        {"request_asin": "F1", "ok": False, "status": "bot_blocked"},  # 失败 -> 剔除
    ]
    out = s._merge_own_records(req, vt)
    assert [r["request_asin"] for r in out] == ["A1", "C2"]
    assert out[0]["title"] == "request-title"
    assert all(r.get("ok") for r in out)


def _run_with_fixed_failures(ws_tmp, fail_status):
    """给定失败状态跑 _fetch_all: 终态(not_found)1 轮; 可重试失败按固定重试轮数(默认
    首轮+3轮=4)停止(dry-run 不触发浏览器补抓)。"""
    ctx = _ctx(ws_tmp)  # FETCH_STALL_ROUNDS=3 -> 上限 4 轮
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    real_fetch_one = s._fetch_one
    fail_asin = "B0FINAL0001"

    def fixed(step_ctx, http, asin, attempts=4):
        if asin == fail_asin:
            return s._failed_record(asin, fail_status, s.now())
        return real_fetch_one(step_ctx, http, asin, attempts=attempts)

    with mock.patch.object(s, "_fetch_one", side_effect=fixed), \
         mock.patch.object(s, "_fetch_round", wraps=s._fetch_round) as rnd:
        records = s._fetch_all(ctx, [fail_asin, "B0CFFBEC96"], out)
    by_asin = {r["request_asin"]: r for r in records}
    return rnd.call_count, by_asin, fail_asin


def test_parse_failed_retryable_then_stop(ws_tmp):
    """parse_failed 为可重试失败: 固定重试轮数(默认首轮+3轮=4)后仍失败停止;
    dry-run 不触发浏览器补抓, 失败记录保留审计。"""
    calls, by_asin, fail_asin = _run_with_fixed_failures(ws_tmp, "parse_failed")
    assert calls == 4                       # 首轮 + 3 轮固定重试
    assert by_asin[fail_asin]["ok"] is False
    assert by_asin[fail_asin]["status"] == "parse_failed"  # 记录保留审计
    assert by_asin["B0CFFBEC96"]["ok"] is True


def test_not_found_permanent_skip(ws_tmp):
    """not_found/Page Not Found 为终态, 永久跳过不重试(1 轮)。"""
    calls, by_asin, fail_asin = _run_with_fixed_failures(ws_tmp, "not_found")
    assert calls == 1
    assert by_asin[fail_asin]["ok"] is False
    assert by_asin[fail_asin]["status"] == "not_found"
    assert by_asin["B0CFFBEC96"]["ok"] is True


def test_final_keywords_auto_from_screen_qualified(ws_tmp):
    """步骤10 达标关键词注入源(自动模式) = FR-07 对全部候选判定的"达标"词全集
    (screen-results.json verdict=达标; step04 核心=全部候选, 达标不限个数)。
    不再用 search-asins.json 键/keywords.txt 全量(避免漏 grinder 达标词/混入未达标长尾)。"""
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    screen = [
        {"keyword": "salt and pepper", "verdict": "达标"},
        {"keyword": "pepper shaker", "verdict": "不达标"},
        {"keyword": "pepper grinder", "verdict": "达标"},
        {"keyword": "electric salt and pepper grinder set", "verdict": "达标"},
        {"keyword": "camping pepper shaker", "verdict": "数据不足"},
    ]
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump(screen, fh)
    s = Step10Variants()
    out = s._final_keywords(ctx, "t")
    assert out == ["salt and pepper", "pepper grinder", "electric salt and pepper grinder set"]


def test_final_keywords_follows_manual_core_words(ws_tmp):
    """存在人工核心清单 filter-<CAT>-core-words.txt 时, 判定参考词用清单词。

    (2026-10-06: 步骤10 词源与「关键词清单」同步, 不再单独维护注入词清单)。
    """
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    screen = [{"keyword": "salt and pepper", "verdict": "达标"},
              {"keyword": "pepper grinder", "verdict": "达标"}]
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump(screen, fh)
    inject = os.path.join(root, "filter-t-core-words.txt")
    with open(inject, "w", encoding="utf-8") as fh:
        fh.write("pepper grinder\n自定义词 custom set\n")
    s = Step10Variants()
    out = s._final_keywords(ctx, "t")
    # 人工核心清单优先(即使 screen 也有 pepper grinder); 手填自定义词保留
    assert out == ["pepper grinder", "自定义词 custom set"]


def test_final_keywords_missing_screen_empty(ws_tmp):
    ctx = _ctx(ws_tmp)
    s = Step10Variants()
    assert s._final_keywords(ctx, "t") == []


def test_final_keywords_excludes_longtail_words(ws_tmp):
    """2026-10-06 用户口径: 注入源**只取核心大词**, 长尾词不参与注入。

    长尾词只用于步骤9 抓取与报告; 判定参考词 = 关键词清单的核心大词列。
    """
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump([{"keyword": "cutting board", "verdict": "达标"},
                   {"keyword": "knife", "verdict": "不达标"}], fh)
    with open(os.path.join(root, "t-keywords.txt"), "w", encoding="utf-8") as fh:
        fh.write("[核心大词]\ncutting board\nknife\n[长尾词]\n"
                 "cutting board for kitchen\ntravel cutting board\n")
    out = Step10Variants()._final_keywords(ctx, "t")
    # 只注入 FR-07 达标的核心大词; 长尾词一个都不进
    assert out == ["cutting board"]


def test_final_keywords_manual_longtail_does_not_inject(ws_tmp):
    """人工长尾清单**不影响**注入源: 核心=自动FR-07, 长尾清单被忽略。"""
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump([{"keyword": "cutting board", "verdict": "达标"}], fh)
    with open(os.path.join(root, "filter-t-longtail-words.txt"), "w", encoding="utf-8") as fh:
        fh.write("my own long tail\n")
    out = Step10Variants()._final_keywords(ctx, "t")
    assert out == ["cutting board"]


def test_final_keywords_manual_core_list_is_the_inject_source(ws_tmp):
    """核心大词列 = 步骤10 的注入词选择表(原「步骤10 注入词」面板功能并入此处)。

    面板里手填的词即便 FR-07 判不达标/无判定, 也照样注入 —— 与旧注入词清单同原理。
    """
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump([{"keyword": "a", "verdict": "达标"},
                   {"keyword": "b", "verdict": "不达标"}], fh)
    with open(os.path.join(root, "t-keywords.txt"), "w", encoding="utf-8") as fh:
        fh.write("[核心大词]\na\nb\n[长尾词]\ntail one\n")
    s = Step10Variants()
    assert s._final_keywords(ctx, "t") == ["a"]          # 自动态: 只标"达标"的
    with open(os.path.join(root, "filter-t-core-words.txt"), "w", encoding="utf-8") as fh:
        fh.write("b\n手填新词\n")
    # 人工态: 面板里选的词原样生效(不看 FR-07), 长尾词仍不注入
    assert s._final_keywords(ctx, "t") == ["b", "手填新词"]



def test_step10_filter_records_uses_judge_batch_ladder(ws_tmp):
    """步骤10 第二遍过滤走 judge_batch(共享阶梯重试): LLM 首轮超时 -> 集中重发成功,
    记录正常保留且 reason 无兜底标记; BATCH_FILTER 作为批大小传入。"""
    from core.errors import LLMError
    from steps.step10_variants import Step10Variants

    ctx = _ctx(ws_tmp)
    ctx.config.set("BATCH_FILTER", 5)
    ctx.config.set("LLM_TIMEOUT", 15)
    records = [
        {"request_asin": f"B0TEST{i:04d}", "ok": True, "title": f"t{i}", "bullets": []}
        for i in range(3)
    ]
    fp = {
        "category": "t", "requirement": "r",
        "related_title": ["board"], "hard_exclude": [],
        "related_bullets": [], "accessories": [], "consumables": [],
    }
    s = Step10Variants()
    calls = {"n": 0}

    def flaky(prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMError("LLM 请求超时(>15s, 服务端无响应/挂起)")
        return {"judgments": [
            {"index": 0, "keep": True, "reason": "命中相关词"},
            {"index": 1, "keep": True, "reason": "命中相关词"},
            {"index": 2, "keep": False, "reason": "标题命中硬排除词"},
        ]}

    with mock.patch.object(
        ctx, "llm", return_value=mock.MagicMock(chat_json=mock.MagicMock(side_effect=flaky))
    ):
        kept, dropped = s._filter_records(ctx, records, fp)
    # 首轮超时(3条整批) -> 集中重发一轮成功
    assert calls["n"] == 2
    assert len(kept) == 2
    assert len(dropped) == 1
    assert all("兜底保留" not in r.get("reason", "") for r in kept + dropped)


def test_step10_run_inject_logs_and_saves_words(ws_tmp):
    """步骤10 run() 注入段: 日志完整列出注入词, 并把实际注入词清单落盘
    为 <CAT>-injected-keywords.txt(供人工核对过滤口径)。"""
    from steps.step10_variants import Step10Variants

    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    # 输入清单
    with open(os.path.join(root, "t-search-asins-all.txt"), "w", encoding="utf-8") as fh:
        fh.write("B0B5E1E13B\nB0TEST0002\n")
    # FR-02 过滤提示词(无 达标关键词)
    fp = {
        "category": "t", "requirement": "r",
        "related_title": ["board", "cutting board"], "hard_exclude": [],
        "related_bullets": [], "accessories": [], "consumables": [],
        "rules": [], "output_format": {},
    }
    with open(os.path.join(root, "filter-t-prompt.json"), "w", encoding="utf-8") as fh:
        json.dump(fp, fh, ensure_ascii=False)
    # FR-07 达标词源(自动)
    screen = [
        {"keyword": "cutting board", "verdict": "达标"},
        {"keyword": "wooden cutting board", "verdict": "达标"},
        {"keyword": "knife", "verdict": "不达标"},
    ]
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump(screen, fh)
    s = Step10Variants()
    s._cat = "t"
    s._input_path = os.path.join(root, "t-search-asins-all.txt")
    logs: list[str] = []
    records = [
        {"request_asin": "B0B5E1E13B", "ok": True, "title": "cutting board", "bullets": []},
        {"request_asin": "B0915D1B8F", "ok": True, "title": "board", "bullets": []},
    ]
    with mock.patch.object(s, "_fetch_all", return_value=records), \
         mock.patch.object(s, "_write_outputs", return_value=None), \
         mock.patch.object(ctx, "llm", return_value=mock.MagicMock(chat_json=mock.MagicMock(
             return_value={"judgments": [
                 {"index": 0, "keep": True, "reason": "命中相关词"},
                 {"index": 1, "keep": True, "reason": "命中相关词"},
             ]}))), \
         mock.patch.object(s, "log", side_effect=lambda ctx, text: logs.append(text)):
        s.run(ctx)
    joined = "\n".join(logs)
    assert "实际注入词" in joined
    assert "cutting board" in joined and "wooden cutting board" in joined
    saved = os.path.join(root, "t-injected-keywords.txt")
    assert os.path.exists(saved)
    words = [ln.strip() for ln in open(saved, encoding="utf-8") if ln.strip()]
    assert "cutting board" in words and "wooden cutting board" in words


def _low_rate_stats(n=20):
    """构造低一致率复核结果(一致率 60%, 8 条不一致: 原保留复核剔除)。"""
    disagree = [
        {"来源标识": f"B0DISC{i:04d}", "复核判定": "剔除", "原判定": "保留"}
        for i in range(8)
    ]
    return {
        "total": n, "sample": n, "agree": n - 8, "disagree": 8,
        "rate": (n - 8) / n, "rows": [], "disagree_rows": disagree,
        "out_path": None, "disagree_path": None,
    }


def test_step10_review_gate_blocks_on_keep_original(ws_tmp):
    """keep_original 策略 + 复核一致率低于门槛 → 步骤10 阻断(抛 StepDataError)。"""
    from core.errors import StepDataError
    from steps.step10_variants import Step10Variants

    ctx = _ctx(ws_tmp)
    ctx.config.set("REVIEW_AUTO", True)
    ctx.config.set("REVIEW_LIMIT", 20)
    ctx.config.set("REVIEW_MIN_RATE", 90)
    ctx.config.set("REVIEW_POLICY", "keep_original")
    ctx.config.set("DRY_RUN", False)  # 真实模式才阻断
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-search-asins-all.txt"), "w", encoding="utf-8") as fh:
        fh.write("B0B5E1E13B\n")
    with open(os.path.join(root, "filter-t-prompt.json"), "w", encoding="utf-8") as fh:
        json.dump({"category": "t", "requirement": "r", "related_title": ["board"],
                   "hard_exclude": [], "related_bullets": [], "accessories": [],
                   "consumables": [], "rules": [], "output_format": {}}, fh, ensure_ascii=False)
    s = Step10Variants()
    s._cat = "t"
    s._input_path = os.path.join(root, "t-search-asins-all.txt")
    ok = [{"request_asin": f"B0DISC{i:04d}", "ok": True, "title": "x", "bullets": []} for i in range(8)]
    ok += [{"request_asin": f"B0OK{i:04d}", "ok": True, "title": "board", "bullets": []} for i in range(12)]
    import steps.step10_variants as mod
    with mock.patch.object(s, "_fetch_all", return_value=ok), \
         mock.patch.object(s, "_write_outputs", return_value=None), \
         mock.patch.object(s, "_final_keywords", return_value=[]), \
         mock.patch.object(mod, "review_filter_results", return_value=_low_rate_stats()), \
         mock.patch.object(ctx, "llm", return_value=mock.MagicMock(chat_json=mock.MagicMock(
             return_value={"judgments": []}))), \
         mock.patch.object(mod, "apply_review_policy", return_value=0):
        try:
            s.run(ctx)
            raise AssertionError("keep_original 低一致率应阻断步骤10")
        except StepDataError:
            pass  # 预期阻断


def test_step10_review_gate_passes_on_follow_review(ws_tmp):
    """follow_review(自动改判)策略: 即使复核原始一致率低于门槛也**不阻断**
    (不一致项已按复核改判消解, 产物已重写; 门槛只约束 keep_original 人工场景)。"""
    from steps.step10_variants import Step10Variants

    ctx = _ctx(ws_tmp)
    ctx.config.set("REVIEW_AUTO", True)
    ctx.config.set("REVIEW_LIMIT", 20)
    ctx.config.set("REVIEW_MIN_RATE", 90)
    ctx.config.set("REVIEW_POLICY", "follow_review")
    ctx.config.set("DRY_RUN", False)
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-search-asins-all.txt"), "w", encoding="utf-8") as fh:
        fh.write("B0B5E1E13B\n")
    with open(os.path.join(root, "filter-t-prompt.json"), "w", encoding="utf-8") as fh:
        json.dump({"category": "t", "requirement": "r", "related_title": ["board"],
                   "hard_exclude": [], "related_bullets": [], "accessories": [],
                   "consumables": [], "rules": [], "output_format": {}}, fh, ensure_ascii=False)
    s = Step10Variants()
    s._cat = "t"
    s._input_path = os.path.join(root, "t-search-asins-all.txt")
    ok = [{"request_asin": f"B0DISC{i:04d}", "ok": True, "title": "x", "bullets": []} for i in range(8)]
    ok += [{"request_asin": f"B0OK{i:04d}", "ok": True, "title": "board", "bullets": []} for i in range(12)]
    import steps.step10_variants as mod
    with mock.patch.object(s, "_fetch_all", return_value=ok), \
         mock.patch.object(s, "_write_outputs", return_value=None), \
         mock.patch.object(s, "_final_keywords", return_value=[]), \
         mock.patch.object(mod, "review_filter_results", return_value=_low_rate_stats()), \
         mock.patch.object(ctx, "llm", return_value=mock.MagicMock(chat_json=mock.MagicMock(
             return_value={"judgments": []}))), \
         mock.patch.object(mod, "apply_review_policy", return_value=8):
        s.run(ctx)  # follow_review: 不应抛 StepDataError
    logs = []  # 不触发异常即通过


def test_step12_is_product_asin_filter():
    """步骤12 非标准 ASIN 过滤(方案B): 纯数字 ISBN 型(如 0593334469=书名ASIN)
    卖家精灵无法查询, 应被 _is_product_asin 剔除; 标准 B0/字母开头 10 位保留。"""
    from steps.step12_competitor_submit import Step12CompetitorSubmit

    ok = ["B080BFF86E", "B039302F19", "B0FE8539C0", "A1B2C3D4E5", "B048B9D8B7"]
    bad = ["0593334469", "1234567890", "B0SHORT1", "", "B0", "ABCDEFGHIJK", "B0!X1Y2Z3"]
    f = Step12CompetitorSubmit._is_product_asin
    assert all(f(a) for a in ok), "标准 ASIN 应保留"
    assert not any(f(a) for a in bad), "ISBN/格式异常应剔除"
    # 0593334469 应剔除(关键回归: 书名 ASIN)
    assert f("0593334469") is False


def test_step13_earliest_time_grace_minute_precision(ws_tmp):
    """步骤13 时间窗口过滤: 平台导出日志只有分钟精度, 同批任务创建时刻可比本地
    '最早提交'显示早 1 分钟(如任务 17:04 vs 记录 17:05:00)——加 90s 容差后
    同批任务不被误杀; **上界**: 更晚的其他批次/其他类目新任务(如 17:30)也被挡,
    不会误勾; 真正的历史旧任务(隔天/隔小时)仍被挡。"""
    from datetime import datetime, timedelta
    from steps.step13_competitor_download import Step13CompetitorDownload

    earliest = datetime(2026, 9, 4, 17, 5, 0)   # 本地最早提交(含秒)
    latest = datetime(2026, 9, 4, 17, 7, 15)    # 本地最晚提交
    grace = timedelta(seconds=90)
    s = Step13CompetitorDownload()
    # 窗口 = [17:03:30, 17:08:45]
    lo = earliest - grace
    hi = latest + grace
    # 同批任务: 17:04 / 17:05 / 17:06 → 都应在窗口内(不误杀)
    for hhmm in (datetime(2026, 9, 4, 17, 4, 0), datetime(2026, 9, 4, 17, 5, 0),
                 datetime(2026, 9, 4, 17, 6, 0)):
        assert lo <= hhmm <= hi, f"{hhmm} 同批任务不应被误杀"
    # 真正旧任务: 9/3 凌晨 → 应被挡(低于下界)
    assert datetime(2026, 9, 3, 0, 30, 0) < lo
    # 其他批次新任务: 当天稍晚 17:30 → 应被挡(高于上界)
    assert datetime(2026, 9, 4, 17, 30, 0) > hi
    # _parse_time 支持页面格式 "2026-09-04 17:04"(分钟精度) 与 "2026-09-04 17:05:00"
    assert s._parse_time("2026-09-04 17:04") == datetime(2026, 9, 4, 17, 4, 0)
    assert s._parse_time("2026-09-04 17:05:00") == earliest
    assert s._parse_time("2026-09-03 00:30") == datetime(2026, 9, 3, 0, 30, 0)
    # _latest_time: 取组里最晚提交(截分钟)
    groups = [
        {"time": "2026-09-04 17:05:00"},
        {"time": "2026-09-04 17:05:58"},
        {"time": "2026-09-04 17:07:15"},
    ]
    assert s._latest_time(groups) == datetime(2026, 9, 4, 17, 7, 0)


# ---------------------------------------------------------------- 2026-09-09 定稿
# 固定冷却(每轮一样) + 固定重试轮(FETCH_STALL_ROUNDS) + 残余全转浏览器 + 浏览器3次舍弃

def test_fixed_round_pause_same_each_round(ws_tmp):
    """轮间冷却固定且每轮一样(用户选定: 等待时间设置成一样的, 不再递增退避):
    FETCH_ROUND_PAUSE=30 时第 2/3/4 轮前都 sleep 30s。"""
    ctx = _ctx(ws_tmp, stall_rounds=3)
    ctx.config.set("FETCH_ROUND_PAUSE", 30)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    round_no = {"n": 0}

    def fake_round(step_ctx, pending, path):
        round_no["n"] += 1
        return [s._failed_record(f"B0FIX{round_no['n']:04d}", "bot_blocked", s.now())]

    sleeps: list[float] = []
    with mock.patch.object(s, "_fetch_round", side_effect=fake_round), \
         mock.patch("time.sleep", side_effect=lambda sec: sleeps.append(sec)):
        s._fetch_all(ctx, ["B0START0001"], out)
    assert round_no["n"] == 4                    # 首轮 + 3 轮固定重试 -> 停
    assert sleeps == [30.0, 30.0, 30.0]          # 每轮冷却一样(不递增)


def test_fixed_pause_default_60_when_not_configured(ws_tmp):
    """未显式配置 FETCH_ROUND_PAUSE 时用写死默认 60s(每轮一样)。"""
    from core.config import Config
    from core.paths import Paths

    cfg = Config(
        {
            "CAT": "t",
            "DATA_ROOT": ws_tmp,
            "DRY_RUN": True,
            "FETCH_STALL_ROUNDS": 2,          # 上限 = 首轮+2 轮 = 3
            # 不设 FETCH_ROUND_PAUSE -> 走 DEFAULTS/写死默认 60
        }
    )
    ctx2 = StepContext(cfg, Paths(cfg))
    os.makedirs(ctx2.paths.cat_root, exist_ok=True)
    s = Step10Variants()
    round_no = {"n": 0}

    def fake_round(step_ctx, pending, path):
        round_no["n"] += 1
        return [s._failed_record(f"B0D60{round_no['n']:04d}", "bot_blocked", s.now())]

    sleeps: list[float] = []
    with mock.patch.object(s, "_fetch_round", side_effect=fake_round), \
         mock.patch("time.sleep", side_effect=lambda sec: sleeps.append(sec)):
        s._fetch_all(ctx2, ["B0START0001"],
                     os.path.join(ctx2.paths.cat_root, "t-results.jsonl"))
    assert round_no["n"] == 3
    assert sleeps == [60.0, 60.0]                # 默认 60s, 每轮一样


def test_browser_fallback_one_try_then_discard(ws_tmp):
    """2026-09-09 定稿(用户改=1): 浏览器补抓每 ASIN 最多试 1 次; 失败即 ok=False
    舍弃该变体(留审计记录, 不进候选); 不因 URL 形态多而多试。"""
    ctx = _ctx(ws_tmp)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    gotos: list[str] = []
    urls = ["u1", "u2", "u3", "u4", "u5"]        # 候选形态多于 1 个也应只试 1 次

    class FakePage:
        def content(self):
            return "<html>not a valid product page</html>"

    class FakeBrowser:
        def new_page(self):
            return FakePage()

        def goto(self, page, url):
            gotos.append(url)
            raise RuntimeError("page load failed")

    # 替换 _build_ok_record: 内容无效也按失败处理, 避免落到真解析
    def bad_record(*args, **kwargs):
        return s._failed_record(args[1], "failed", s.now())

    with mock.patch.object(s, "_build_ok_record", side_effect=bad_record), \
         mock.patch.object(ctx, "browser", return_value=__import__("contextlib").nullcontext(FakeBrowser())), \
         mock.patch("steps.step10_variants.url_candidates", return_value=urls):
        recs = s._browser_fallback(ctx, ["B01F771797"])
    assert len(gotos) == 1                        # 只试 1 次
    assert gotos == ["u1"]
    assert recs[0]["request_asin"] == "B01F771797"
    assert recs[0]["ok"] is False
    assert "1" in str(recs[0].get("error", ""))   # 标记舍弃原因


def test_browser_fallback_results_are_written_back(ws_tmp, monkeypatch):
    """2026-09-16 修 bug: 浏览器补抓成功的记录**必须真正写回台账**。

    事故现场(`Chopping Boards-UK-202608` 实测): 首轮直连 66% 被风控(bot_blocked, 298/883),
    残余 579 个转本机浏览器补抓 —— 日志逐条打印 `浏览器补抓 X -> ok`(**904 次里成功 903 次**),
    但台账仍是 304/579 一条没变, 补抓白跑 1.5 小时, 步骤10 质量检查因此报
    "894 未解决 / 需复核"。**根因**: 这些 ASIN 在台账里**本来就有失败行**, 而
    `append_jsonl(dedup_key=…)` 的语义是"已存在的同一主体**跳过**"(keep-first, 从不覆盖)
    → 补抓结果被整批静默丢弃。**修法**: 与 `_fetch_round` 同款 —— 先剪除旧失败行再写入。
    """
    cfg = Config({
        "CAT": "t", "DATA_ROOT": ws_tmp, "DRY_RUN": False,
        "FETCH_ROUND_PAUSE": 0, "FETCH_STALL_ROUNDS": 1, "FETCH_WORKERS": 1,
    })
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    ctx = StepContext(cfg, paths)
    out = os.path.join(paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    asin = "B057553496"
    # 台账预置: 直连被风控的失败行(补抓前就存在的那一行)
    append_jsonl(out, [s._failed_record(asin, "bot_blocked", s.now())],
                 dedup_key="request_asin")
    assert read_jsonl(out)[0]["ok"] is False

    # 直连重试轮仍失败(不改台账) → 走浏览器补抓
    monkeypatch.setattr(s, "_fetch_round", lambda c, pend, p: read_jsonl(p))
    ok_rec = {"request_asin": asin, "ok": True, "status": "ok", "source": "local_chrome",
              "title": "T", "bullets": ["b"], "fetched_at": "2026-09-16 00:00:00"}
    monkeypatch.setattr(s, "_browser_fallback", lambda c, asins: [ok_rec])

    recs = s._fetch_all(ctx, [asin], out)
    # ① 没有重复行(剪除生效, 不是"追加了一条")  ② 台账里就是补抓的 ok 记录
    assert [r.get("request_asin") for r in recs] == [asin], f"台账出现重复/错行: {recs}"
    assert recs[0]["ok"] is True, "补抓成功记录没写回台账(旧 bug: 被 dedup keep-first 丢弃)"
    assert recs[0]["source"] == "local_chrome"


def test_browser_fallback_without_records_keeps_ledger(ws_tmp, monkeypatch):
    """补抓一条记录都没返回时, **不能**把旧失败行剪掉(否则这些 ASIN 会从台账消失)。"""
    cfg = Config({
        "CAT": "t", "DATA_ROOT": ws_tmp, "DRY_RUN": False,
        "FETCH_ROUND_PAUSE": 0, "FETCH_STALL_ROUNDS": 1, "FETCH_WORKERS": 1,
    })
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    ctx = StepContext(cfg, paths)
    out = os.path.join(paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    asin = "B05524910A"
    append_jsonl(out, [s._failed_record(asin, "bot_blocked", s.now())],
                 dedup_key="request_asin")
    monkeypatch.setattr(s, "_fetch_round", lambda c, pend, p: read_jsonl(p))
    monkeypatch.setattr(s, "_browser_fallback", lambda c, asins, *, headed=False: [])

    recs = s._fetch_all(ctx, [asin], out)
    assert len(recs) == 1
    assert recs[0]["request_asin"] == asin and recs[0]["ok"] is False


def _fake_product_html() -> str:
    """构造一个能过步骤10 全部解析门槛的假商品页。

    门槛(见 `_build_ok_record`): HTML 长度 ≥ 50000、**父 ASIN 必须是合法 10 位**、变体数 1..100、
    页面判定非 blocked/unexpected_page —— 所以这里带 twister 变体映射并补足长度。
    ⚠️ ASIN 一律 10 位: 写成 11 位会被 `normalize_asin` 判"父 ASIN 格式无效"→ parse_failed。
    """
    return """<html><head><title>Test Chopping Board : Amazon.co.uk</title></head><body>
<span id="productTitle">Test Chopping Board XL</span>
<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/main_full.jpg" src="https://m.media-amazon.com/images/I/main_thumb.jpg">
<div id="feature-bullets"><ul><li><span class="a-list-item"><span>Durable</span></span></li></ul></div>
<script>P.register('twister-js-init-dpx-data', function(){ var dataToReturn = {"parentAsin":"B076215605","currentAsin":"B0EFD58058","dimensionToAsinMap":{"Black":"B0CD4B1A23","Silver":"B0674211A9"}}; });</script>
<script>jQuery.parseJSON('{"colorToAsin":{"Black":"B0CD4B1A23","Silver":"B0674211A9"}}');</script>
<div id="altImages"><img class="imageThumbnail" src="https://m.media-amazon.com/images/I/g1._AC_US40_.jpg"></div>
<!--%s-->
</body></html>""" % ("x" * 52000)


def _bf_ctx(ws_tmp, **over):
    """浏览器补抓相关用例的 ctx: 必须非 dry-run(补抓只在真实模式跑)。"""
    data = {
        "CAT": "t", "DATA_ROOT": ws_tmp, "DRY_RUN": False, "MARKET": "amazon.co.uk",
        "FETCH_ROUND_PAUSE": 0, "FETCH_STALL_ROUNDS": 1, "FETCH_WORKERS": 1,
        "BROWSER_ROUND_PAUSE": 0, "BROWSER_FALLBACK_ROUNDS": 1,
    }
    data.update(over)
    cfg = Config(data)
    paths = Paths(cfg)
    os.makedirs(paths.cat_root, exist_ok=True)
    return StepContext(cfg, paths)


def test_browser_fallback_really_writes_ok_records_to_disk(ws_tmp, monkeypatch):
    """端到端(真解析器 + 真台账文件): 补抓成功记录**必须真的落盘**。

    这条是 2026-09-16「903 条补抓成果被静默丢弃」的直接回归 —— 用**真实的**
    `_browser_fallback`(只把浏览器本体换成假的: goto 不联网、content 返回合法商品页),
    从 `_fetch_all` 一路跑到写文件, 再读回磁盘断言, 而不是打桩返回值。
    """
    ctx = _bf_ctx(ws_tmp)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    asins = ["B0EFD58058", "B078B0AE44"]
    # 台账预置: 直连被风控的失败行(补抓前就存在的那两行)
    append_jsonl(out, [s._failed_record(a, "bot_blocked", s.now()) for a in asins],
                 dedup_key="request_asin")

    class FakePage:
        def content(self):
            return _fake_product_html()

    class FakeBrowser:
        def new_page(self):
            return FakePage()

        def goto(self, page, url):
            pass

    monkeypatch.setattr(s, "_fetch_round", lambda c, pend, p: read_jsonl(p))
    monkeypatch.setattr(ctx, "browser", lambda *a, **k: contextlib.nullcontext(FakeBrowser()))
    monkeypatch.setattr("steps.step10_variants.time.sleep", lambda *_: None)

    s._fetch_all(ctx, asins, out)

    on_disk = read_jsonl(out)
    assert len(on_disk) == len(asins), f"台账行数不对(应有 {len(asins)} 行, 无重复): {on_disk}"
    assert {r["request_asin"] for r in on_disk} == set(asins)
    assert all(r["ok"] for r in on_disk), f"补抓成功记录没有落盘: {on_disk}"
    assert {r["source"] for r in on_disk} == {"local_chrome"}


def test_browser_fallback_runs_another_round_for_leftovers(ws_tmp, monkeypatch):
    """用户 2026-09-16 要求: 补抓后**还有失败的就再来一轮补抓**, 每轮结果都写回台账。"""
    ctx = _bf_ctx(ws_tmp, BROWSER_FALLBACK_ROUNDS=2)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    asins = ["B057553496", "B05524910A", "B02CC9EA29"]
    append_jsonl(out, [s._failed_record(a, "bot_blocked", s.now()) for a in asins],
                 dedup_key="request_asin")

    rounds: list[list[str]] = []

    def ok_rec(asin):
        return {"request_asin": asin, "ok": True, "status": "ok",
                "source": "local_chrome", "title": "T", "bullets": [], "fetched_at": "t"}

    def fake_fallback(c, pend):
        rounds.append(sorted(pend))
        if len(rounds) == 1:            # 第 1 轮只成功 1 个
            return [ok_rec(pend[0])] + [s._failed_record(a, "blocked", s.now())
                                        for a in pend[1:]]
        return [ok_rec(a) for a in pend]  # 第 2 轮全部成功

    monkeypatch.setattr(s, "_fetch_round", lambda c, pend, p: read_jsonl(p))
    monkeypatch.setattr(s, "_browser_fallback", fake_fallback)
    monkeypatch.setattr("steps.step10_variants.time.sleep", lambda *_: None)

    s._fetch_all(ctx, asins, out)

    assert len(rounds) == 2, f"应当在第 2 轮再补一次, 实际 {len(rounds)} 轮"
    assert rounds[0] == asins
    assert rounds[1] == sorted(asins[1:]), "第 2 轮只应补仍未解决的 ASIN"
    on_disk = read_jsonl(out)
    assert len(on_disk) == len(asins), f"多轮补抓产生了重复/丢失行: {on_disk}"
    assert all(r["ok"] for r in on_disk), f"两轮补抓后仍应有全部成功记录: {on_disk}"


def test_browser_fallback_rounds_stop_early_when_resolved(ws_tmp, monkeypatch):
    """第 1 轮就全部解决 → 不再空跑第 2 轮。"""
    ctx = _bf_ctx(ws_tmp, BROWSER_FALLBACK_ROUNDS=3)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    asin = "B022AF9BCC"
    append_jsonl(out, [s._failed_record(asin, "bot_blocked", s.now())],
                 dedup_key="request_asin")
    rounds: list[list[str]] = []

    def fake_fallback(c, pend):
        rounds.append(sorted(pend))
        return [{"request_asin": a, "ok": True, "status": "ok", "source": "local_chrome"}
                for a in pend]

    monkeypatch.setattr(s, "_fetch_round", lambda c, pend, p: read_jsonl(p))
    monkeypatch.setattr(s, "_browser_fallback", fake_fallback)

    s._fetch_all(ctx, [asin], out)
    assert len(rounds) == 1, "全部解决后不应再补抓"
    assert read_jsonl(out)[0]["ok"] is True


def test_fallback_off_never_uses_browser(ws_tmp, monkeypatch):
    """`FALLBACK: off` = 完全不用浏览器(用户 2026-09-16 选定): 只直连, 抓不到就如实记失败。"""
    ctx = _bf_ctx(ws_tmp, FALLBACK="off")
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    asin = "B05BC69449"
    append_jsonl(out, [s._failed_record(asin, "bot_blocked", s.now())],
                 dedup_key="request_asin")
    monkeypatch.setattr(s, "_fetch_round", lambda c, pend, p: read_jsonl(p))
    called: list = []
    monkeypatch.setattr(s, "_browser_fallback", lambda c, asins: called.append(asins) or [])

    recs = s._fetch_all(ctx, [asin], out)
    assert called == [], f"FALLBACK=off 时不该启动浏览器补抓, 实际调用 {called}"
    assert len(recs) == 1 and recs[0]["ok"] is False
    assert recs[0]["status"] == "bot_blocked"


def test_internal_retry_defaults_when_config_has_no_keys(ws_tmp, monkeypatch):
    """内化默认(用户"太多参数了, 我只需要个兜底的就行"): 配置里只有 `FALLBACK` 时,
    直连 = 首轮 + 1 轮重试, 之后浏览器补抓 = 无头 2 轮 + **有头兜底 1 轮**(2026-09-29 起)。"""
    ctx = _bf_ctx(ws_tmp, FALLBACK="auto")
    for k in ("FETCH_STALL_ROUNDS", "FETCH_ROUND_PAUSE", "FETCH_ASIN_TRIES",
              "REQUEST_RETRY", "BROWSER_FALLBACK_ROUNDS", "BROWSER_ROUND_PAUSE",
              "BROWSER_HEADED_ROUNDS"):
        try:
            del ctx.config.data[k]
        except (KeyError, TypeError):
            pass
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    asin = "B06A7571D6"
    append_jsonl(out, [s._failed_record(asin, "bot_blocked", s.now())],
                 dedup_key="request_asin")

    round_calls: list = []

    def fake_round(c, pend, path):
        round_calls.append(list(pend))
        return read_jsonl(path)

    fb_calls: list = []

    def fake_fb(c, asins, *, headed=False):
        fb_calls.append((list(asins), headed))
        return [s._failed_record(a, "blocked", s.now()) for a in asins]

    monkeypatch.setattr(s, "_fetch_round", fake_round)
    monkeypatch.setattr(s, "_browser_fallback", fake_fb)
    monkeypatch.setattr("steps.step10_variants.time.sleep", lambda *_: None)

    s._fetch_all(ctx, [asin], out)
    assert len(round_calls) == 2, f"直连阶段应为 首轮 + 1 轮重试(共 2 轮), 实际 {len(round_calls)} 轮"
    assert [h for _a, h in fb_calls] == [False, False, True], \
        f"浏览器补抓应为 无头 2 轮 + 有头兜底 1 轮, 实际 {fb_calls}"
    assert all(a == [asin] for a, _h in fb_calls), "每轮都应只针对未解决的 ASIN"


def test_headed_fallback_disabled_by_zero(ws_tmp, monkeypatch):
    """`BROWSER_HEADED_ROUNDS: 0` = 关闭有头兜底(只想跑无头、不弹窗)。"""
    ctx = _bf_ctx(ws_tmp, BROWSER_FALLBACK_ROUNDS=1, BROWSER_HEADED_ROUNDS=0)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    asin = "B04C6C0545"
    append_jsonl(out, [s._failed_record(asin, "bot_blocked", s.now())],
                 dedup_key="request_asin")
    heads: list[bool] = []

    def fake_fb(c, pend, *, headed=False):
        heads.append(headed)
        return [s._failed_record(a, "blocked", s.now()) for a in pend]

    monkeypatch.setattr(s, "_fetch_round", lambda c, pend, p: read_jsonl(p))
    monkeypatch.setattr(s, "_browser_fallback", fake_fb)
    monkeypatch.setattr("steps.step10_variants.time.sleep", lambda *_: None)

    s._fetch_all(ctx, [asin], out)
    assert heads == [False], f"关闭有头兜底后不应再有 headed 轮: {heads}"


def test_headed_fallback_skipped_when_headless_resolves(ws_tmp, monkeypatch):
    """无头轮已全部解决 → 不弹有头窗口(不打扰用户)。"""
    ctx = _bf_ctx(ws_tmp, BROWSER_FALLBACK_ROUNDS=2)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    asin = "B0860ABA55"
    append_jsonl(out, [s._failed_record(asin, "bot_blocked", s.now())],
                 dedup_key="request_asin")
    heads: list[bool] = []

    def fake_fb(c, pend, *, headed=False):
        heads.append(headed)
        return [{"request_asin": a, "ok": True, "status": "ok",
                 "source": "local_chrome"} for a in pend]

    monkeypatch.setattr(s, "_fetch_round", lambda c, pend, p: read_jsonl(p))
    monkeypatch.setattr(s, "_browser_fallback", fake_fb)

    s._fetch_all(ctx, [asin], out)
    assert heads == [False], f"无头已解决就不该再有有头轮: {heads}"
    assert read_jsonl(out)[0]["ok"] is True


def test_fallback_off_never_opens_headed_window(ws_tmp, monkeypatch):
    """`FALLBACK: off` 连无头都不用 → 有头兜底也不能弹窗。"""
    ctx = _bf_ctx(ws_tmp, FALLBACK="off")
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    append_jsonl(out, [s._failed_record("B0C1AB44E2", "bot_blocked", s.now())],
                 dedup_key="request_asin")
    calls: list = []
    monkeypatch.setattr(s, "_fetch_round", lambda c, pend, p: read_jsonl(p))
    monkeypatch.setattr(s, "_browser_fallback",
                        lambda c, a, *, headed=False: calls.append((a, headed)) or [])

    s._fetch_all(ctx, ["B0C1AB44E2"], out)
    assert calls == [], f"FALLBACK=off 不应调用浏览器(含 headed): {calls}"


def test_browser_fallback_headed_really_opens_visible_window(ws_tmp):
    """有头轮必须真的以 `headed=True` 打开浏览器 —— 不是只改日志文案。"""
    ctx = _ctx(ws_tmp)
    s = Step10Variants()
    seen: list[dict] = []

    class FakePage:
        def content(self):
            return "<html>not a product page</html>"

    class FakeBrowser:
        def new_page(self):
            return FakePage()

        def goto(self, page, url):
            raise RuntimeError("blocked")

    def fake_browser(*args, **kwargs):
        seen.append({"args": args, "kwargs": kwargs})
        return contextlib.nullcontext(FakeBrowser())

    with mock.patch.object(ctx, "browser", side_effect=fake_browser), \
         mock.patch("steps.step10_variants.time.sleep", lambda *_: None):
        recs = s._browser_fallback(ctx, ["B07E96C1F3"], headed=True)

    assert seen, "没打开浏览器"
    assert seen[0]["kwargs"].get("headed") is True, f"没有以 headed=True 打开: {seen[0]}"
    assert recs[0]["ok"] is False and recs[0]["source"] == "local_chrome"


def test_headed_fallback_runs_after_headless_rounds(ws_tmp, monkeypatch):
    """用户 2026-09-29 要求: "如果无头的无效再使用有头的" —— 无头轮用尽后仍有未解决,
    再用**有头(可见窗口)**补一轮, 结果照样写回台账。"""
    ctx = _bf_ctx(ws_tmp, BROWSER_FALLBACK_ROUNDS=2, BROWSER_HEADED_ROUNDS=1)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    asins = ["B060B75784", "B02BB69A05"]
    append_jsonl(out, [s._failed_record(a, "bot_blocked", s.now()) for a in asins],
                 dedup_key="request_asin")
    calls: list[tuple[list[str], bool]] = []

    def fake_fb(c, pend, *, headed=False):
        calls.append((sorted(pend), headed))
        if not headed:                      # 无头两轮全失败(模拟被识别)
            return [s._failed_record(a, "blocked", s.now()) for a in pend]
        return [{"request_asin": a, "ok": True, "status": "ok",
                 "source": "local_chrome"} for a in pend]

    monkeypatch.setattr(s, "_fetch_round", lambda c, pend, p: read_jsonl(p))
    monkeypatch.setattr(s, "_browser_fallback", fake_fb)
    monkeypatch.setattr("steps.step10_variants.time.sleep", lambda *_: None)

    s._fetch_all(ctx, asins, out)

    assert [h for _a, h in calls] == [False, False, True], \
        f"应为 无头 2 轮 + 有头 1 轮, 实际 {calls}"
    assert all(a == asins for a, _h in calls), "每轮都应针对全部未解决 ASIN"
    on_disk = read_jsonl(out)
    assert len(on_disk) == len(asins) and all(r["ok"] for r in on_disk), \
        f"有头轮成功后应写回台账且无重复行: {on_disk}"


def test_headed_fallback_only_takes_unresolved_asins(ws_tmp, monkeypatch):
    """有头轮只补"到那一刻仍未解决"的 ASIN(无头已救回的绝不重复打开)。"""
    ctx = _bf_ctx(ws_tmp, BROWSER_FALLBACK_ROUNDS=1, BROWSER_HEADED_ROUNDS=1)
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    s = Step10Variants()
    asins = ["B08D8CB02D", "B06F34C4CE"]
    append_jsonl(out, [s._failed_record(a, "bot_blocked", s.now()) for a in asins],
                 dedup_key="request_asin")
    headed_asins: list[list[str]] = []

    def fake_fb(c, pend, *, headed=False):
        if not headed:                      # 无头轮: 第 1 个成功, 第 2 个仍失败
            return [{"request_asin": pend[0], "ok": True, "status": "ok",
                     "source": "local_chrome"}] + [
                s._failed_record(a, "blocked", s.now()) for a in pend[1:]]
        headed_asins.append(list(pend))
        return [{"request_asin": a, "ok": True, "status": "ok",
                 "source": "local_chrome"} for a in pend]

    monkeypatch.setattr(s, "_fetch_round", lambda c, pend, p: read_jsonl(p))
    monkeypatch.setattr(s, "_browser_fallback", fake_fb)

    s._fetch_all(ctx, asins, out)
    assert headed_asins == [[asins[1]]], f"有头轮只应补未解决的那个: {headed_asins}"


def test_browser_fallback_failure_keeps_page_evidence(ws_tmp):
    """补抓失败必须留下**页面取证**(2026-09-29): 事故里 311 条失败记录只有一句通用 error,

    `html_length`/`final_url`/`title` 全是 None → 连"是验证码页、登录页还是超时"都查不出来。
    """
    ctx = _ctx(ws_tmp)
    s = Step10Variants()
    captcha = ('<html><head><title>Amazon.com</title></head>'
               "<body>Enter the characters you see below (captcha) automated access</body></html>")

    class FakePage:
        url = "https://www.amazon.com/dp/B090926B0F"

        def content(self):
            return captcha

    class FakeBrowser:
        def new_page(self):
            return FakePage()

        def goto(self, page, url):
            pass

    # 页面能打开但内容不是商品页 → 走"内容无效"分支(与真实验证码页一致)
    with mock.patch.object(ctx, "browser", return_value=contextlib.nullcontext(FakeBrowser())), \
         mock.patch("steps.step10_variants.time.sleep", lambda *_: None), \
         mock.patch.object(s, "_build_ok_record",
                           side_effect=lambda *a, **k: s._failed_record(a[1], "failed", s.now())):
        recs = s._browser_fallback(ctx, ["B090926B0F"])

    rec = recs[0]
    assert rec["ok"] is False and rec["source"] == "local_chrome"
    assert rec["blocked_kind"] == "captcha", f"应识别出验证码页: {rec}"
    assert rec["html_length"] == len(captcha)
    assert rec["page_title"] == "Amazon.com"
    assert rec["final_url"] == "https://www.amazon.com/dp/B090926B0F"
    assert rec["title"] == "", "取证标题绝不能写进业务字段 title(免得验证码页标题被当商品名)"


def test_browser_fallback_exception_keeps_error_detail(ws_tmp):
    """打开就抛异常(net::ERR_ 类) → 失败记录带 `blocked_kind=error` 与可读的 error_detail。"""
    ctx = _ctx(ws_tmp)
    s = Step10Variants()

    class FakePage:
        def content(self):
            return ""

    class FakeBrowser:
        def new_page(self):
            return FakePage()

        def goto(self, page, url):
            raise RuntimeError("net::ERR_TIMED_OUT at https://www.amazon.com/dp/x")

    with mock.patch.object(ctx, "browser", return_value=contextlib.nullcontext(FakeBrowser())), \
         mock.patch("steps.step10_variants.time.sleep", lambda *_: None):
        recs = s._browser_fallback(ctx, ["B0B8E1F095"])

    rec = recs[0]
    assert rec["ok"] is False
    assert rec["blocked_kind"] == "error"
    assert "ERR_TIMED_OUT" in rec["error_detail"]
    assert rec["final_url"], "至少留下请求用的 URL"
    assert rec["html_length"] == 0


def test_browser_session_failure_marks_all_asins(ws_tmp):
    """连浏览器会话都起不来 → 每个 ASIN 的失败记录都标 `session_error`(便于与页面级失败区分)。"""
    ctx = _ctx(ws_tmp)
    s = Step10Variants()
    with mock.patch.object(ctx, "browser", side_effect=RuntimeError("chrome 起不来")):
        recs = s._browser_fallback(ctx, ["B086D2A0D3", "B06F6A8C9C"])
    assert len(recs) == 2
    assert all(r["blocked_kind"] == "session_error" for r in recs)
    assert all("chrome 起不来" in r["error_detail"] for r in recs)


def test_page_evidence_kinds_and_title_extraction():
    """`_page_evidence` 的分类与标题提取(纯函数, 不碰浏览器)。"""
    ev = Step10Variants._page_evidence("", "https://x/dp/A")
    assert ev["blocked_kind"] == "empty" and ev["html_length"] == 0 and ev["final_url"] == "https://x/dp/A"
    assert Step10Variants._page_evidence(
        "<html>Sign in to continue /ap/signin</html>", "u")["blocked_kind"] == "signin"
    assert Step10Variants._page_evidence(
        "<html>verify you are human</html>", "u")["blocked_kind"] == "captcha"
    assert Step10Variants._page_evidence(
        "<html><body>hello</body></html>", "u")["blocked_kind"] == "content"
    ev2 = Step10Variants._page_evidence(
        "<HTML><HEAD><TITLE>\n  Spam   Page \n</TITLE></HEAD><body>x</body></HTML>", "u")
    assert ev2["page_title"] == "Spam Page", "标题要压缩空白"


def test_browser_fallback_success_stops_early(ws_tmp):
    """浏览器补抓首次成功即停, 不浪费。"""
    ctx = _ctx(ws_tmp)
    s = Step10Variants()
    gotos: list[str] = []
    urls = ["u1", "u2", "u3"]

    class FakePage:
        def content(self):
            return "<html>ok page</html>"

    class FakeBrowser:
        def new_page(self):
            return FakePage()

        def goto(self, page, url):
            gotos.append(url)

    ok_rec = {"request_asin": "B0E7D57814", "ok": True, "status": "ok",
              "title": "T", "bullets": ["b"], "images": ["i"], "fetched_at": "t"}

    def good_record(*args, **kwargs):
        return ok_rec if len(gotos) == 1 else None

    with mock.patch.object(s, "_build_ok_record", side_effect=good_record), \
         mock.patch.object(ctx, "browser", return_value=__import__("contextlib").nullcontext(FakeBrowser())), \
         mock.patch("steps.step10_variants.url_candidates", return_value=urls):
        recs = s._browser_fallback(ctx, ["B0E7D57814"])
    assert len(gotos) == 1                         # 第一次即成功, 提前停
    assert recs[0]["ok"] is True


# ============================================================ 变体数越界 = 终态
# 2026-09-16 用户实测(Chopping Boards-DE-202608): 台账里 4 条 B0E6B6B877/B0BD0F9477/
# B0E7DDBE1C/B016838F2E 的失败原因其实是"变体数 725/599/650/650 不在 1..100" —— 页面
# **已成功取回**, 只是被本程序上限拒收。原实现把它降级成瘦 failed 记录: `variant_count`
# /`html_length`/`map_counts` 等取证字段全丢、日志与看板看不出与网络失败的区别, 而且每次
# 续跑都会当普通失败重新抓一遍。下面 6 条锁死修复后的口径。


def _fake_variants_html(n: int, asin: str = "B0EFD58058") -> str:
    """构造一个含 n 个变体的假商品页(ASIN 全 10 位, 走真实解析器与门槛)。

    `currentAsin` 必须等于**被请求的 ASIN**: 解析器的 unexpected_page 判定是
    "标题里没有本站域名 **且** 页面里找不到该 ASIN" → 少了它会被判非本站商品页(blocked)。
    """
    pairs = ",".join('"v%d":"B0V%07d"' % (i, i) for i in range(n))
    return """<html><head><title>Test Chopping Board : Amazon.co.uk</title></head><body>
<span id="productTitle">Test Chopping Board XL</span>
<img id="landingImage" data-old-hires="https://m.media-amazon.com/images/I/main_full.jpg">
<div id="feature-bullets"><ul><li><span class="a-list-item"><span>Durable</span></span></li></ul></div>
<script>P.register('twister-js-init-dpx-data', function(){ var dataToReturn = {"parentAsin":"B076215605","currentAsin":"%s","dimensionToAsinMap":{%s}}; });</script>
<!--%s-->
</body></html>""" % (asin, pairs, "x" * 52000)


def test_too_many_variants_reject_is_terminal_with_evidence(ws_tmp):
    """变体数越界 → 状态 skipped_too_many_variants(终态)且保留取证字段。"""
    ctx = _bf_ctx(ws_tmp)                          # 必须非 dry-run: `_build_ok_record` 虽不判
    s = Step10Variants()                           # dry-run, 但这里与 `_fetch_one` 用例保持同口径
    rec = s._build_ok_record(ctx, "B0CE68802E", _fake_variants_html(650, "B0CE68802E"),
                             s.now(), "u", "u", "")
    assert rec["ok"] is False
    assert rec["status"] == "skipped_too_many_variants"
    assert _is_terminal(rec["status"]) is True
    assert rec["status"] in _TERMINAL_STATUSES
    # 解析器会**把请求 ASIN 本身也算进变体**(650 个映射项 → 651 条), 这里断言真实口径
    assert rec["variant_count"] == 651
    assert len(rec["variant_asins"]) == 651
    assert rec["html_length"] >= 50000
    assert rec["parent_asin"] == "B076215605"
    assert "651" in rec["error"] and "终态跳过" in rec["error"]


def test_fetch_one_keeps_rich_reject_instead_of_generic_failed(ws_tmp):
    """单次尝试即被门限拒收时, `_fetch_one` 必须返回**富记录**(不被"多次尝试失败"覆盖)。

    ⚠️ 必须非 dry-run: dry-run 下 `_fetch_one` 走 mock 分支(写死 ok), 不会真解析。
    """
    ctx = _bf_ctx(ws_tmp)
    s = Step10Variants()
    http = _FakeHttp([("ok", _fake_variants_html(650, "B079522F3A"))])
    rec = s._fetch_one(ctx, http, "B079522F3A", attempts=1)
    assert rec["status"] == "skipped_too_many_variants"
    assert rec["variant_count"] == 651
    assert not str(rec["error"]).startswith("多次尝试失败")
    assert rec["source"] == "direct_http" and rec["source_url"]
    assert http.calls == 1


def test_fetch_one_later_network_error_still_wins(ws_tmp):
    """第 1 次拒收、第 2 次被打断(blocked) → 仍按网络失败记(不把拒收当最终结论)。"""
    ctx = _bf_ctx(ws_tmp)
    s = Step10Variants()
    http = _FakeHttp([("ok", _fake_variants_html(650, "B052C4C8DD")), ("bot_blocked", "<html>captcha</html>")])
    rec = s._fetch_one(ctx, http, "B052C4C8DD", attempts=2)
    assert rec["status"] == "failed"
    assert "多次尝试失败" in rec["error"] and "bot_blocked" in rec["error"]
    assert http.calls == 2


def test_too_many_variants_terminal_only_one_round(ws_tmp):
    """终态: 变体数越界不参与固定重试轮(只跑 1 轮), 也不进浏览器补抓。"""
    calls, by_asin, fail_asin = _run_with_fixed_failures(ws_tmp, "skipped_too_many_variants")
    assert calls == 1
    assert by_asin[fail_asin]["ok"] is False
    assert by_asin[fail_asin]["status"] == "skipped_too_many_variants"
    assert by_asin["B0CFFBEC96"]["ok"] is True


def test_round_summary_lists_too_many_variants_asins(ws_tmp):
    """轮次汇总: 变体数越界单独计数, 且日志里直接列出 ASIN(用户要求可视)。"""
    ctx = _ctx(ws_tmp)
    s = Step10Variants()
    out = os.path.join(ctx.paths.cat_root, "t-results.jsonl")
    rows = [
        {"request_asin": "B0CFFBEC96", "ok": True, "status": "ok"},
        {"request_asin": "B04CF9457E", "ok": False, "status": "skipped_too_many_variants"},
        {"request_asin": "B062781C51", "ok": False, "status": "skipped_page_not_found"},
        {"request_asin": "B06D7326B1", "ok": False, "status": "failed"},
    ]
    with open(out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    logs: list[str] = []
    with mock.patch.object(s, "log", side_effect=lambda c, text: logs.append(text)):
        s._log_round_summary(ctx, "第 1 轮完成", out)
    joined = "\n".join(logs)
    assert "成功 1 / 失败 1" in joined                # 越界与 PNF 都不算"失败"
    assert "变体数越界终态 1" in joined
    assert "PageNotFound终态 1" in joined
    assert "终态变体数越界(>100, 永久跳过): B04CF9457E" in joined
    assert "B06D7326B1" in joined                    # 真失败仍列在失败清单


def test_quality_check_excludes_too_many_from_unresolved(ws_tmp):
    """质量检查.json: 变体数越界进 skipped_too_many_variants_asins, **不**计入未解决;
    输入ASIN映射.csv 的状态列写明"已跳过(变体数超上限)"。"""
    ctx = _bf_ctx(ws_tmp)                          # 审计只写非 dry-run
    s = Step10Variants()
    ok_rec = s._build_ok_record(ctx, "B0EFD58058", _fake_product_html(), s.now(), "u", "u", "")
    assert ok_rec["status"] == "ok"
    too_many = s._build_ok_record(ctx, "B094496CE2", _fake_variants_html(650, "B094496CE2"),
                                  s.now(), "u", "u", "")
    failed = s._failed_record("B00860F9BF", "failed", s.now())
    s._write_variation_audit(ctx, [ok_rec, too_many, failed])

    prefix = ctx.resolve_artifact(f"{ctx.config.cat}-变体抓取")
    with open(prefix + "-质量检查.json", encoding="utf-8") as fh:
        quality = json.load(fh)
    assert quality["skipped_too_many_variants_asins"] == ["B094496CE2"]
    assert "B094496CE2" not in quality["unresolved_asins"]
    assert quality["unresolved_asins"] == ["B00860F9BF"]
    assert quality["summary"]["skipped_too_many_variants_count"] == 1
    assert quality["summary"]["skipped_count"] == 1          # 终态总数(仅这一条)
    assert quality["summary"]["residual_count"] == 1         # 只剩真失败

    with open(prefix + "-输入ASIN映射.csv", encoding="utf-8-sig") as fh:
        mapping = list(csv.reader(fh))
    status_by_asin = {row[0]: row[4] for row in mapping[1:]}
    assert status_by_asin["B094496CE2"] == "已跳过(变体数超上限)"
    assert status_by_asin["B00860F9BF"] == "失败"
    assert status_by_asin["B0EFD58058"] == "成功"


def test_final_keywords_includes_inject_only_words(ws_tmp):
    """仅注入词(`[仅注入]` 分区)进步骤10 的判定参考词 —— 这就是"能添加"的落地。

    自动态: 注入 = FR-07 达标自动核心词 + 仅注入词。
    """
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump([{"keyword": "auto one", "verdict": "达标"}], fh)
    with open(os.path.join(root, "filter-t-core-words.txt"), "w", encoding="utf-8") as fh:
        fh.write("[核心大词]\n[仅注入]\ninject only\n另一个仅注入词\n")
    out = Step10Variants()._final_keywords(ctx, "t")
    assert out == ["auto one", "inject only", "另一个仅注入词"]


def test_final_keywords_manual_core_plus_inject_only(ws_tmp):
    """人工核心清单 + 仅注入词 同时生效(核心在抓取里, 仅注入只在判定里)。"""
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump([{"keyword": "auto one", "verdict": "达标"}], fh)
    with open(os.path.join(root, "filter-t-core-words.txt"), "w", encoding="utf-8") as fh:
        fh.write("[核心大词]\nmy core\n[仅注入]\ninject only\n")
    out = Step10Variants()._final_keywords(ctx, "t")
    assert out == ["my core", "inject only"]


def test_final_keywords_inject_only_survives_legacy_file(ws_tmp):
    """旧格式(无分区头)的文件仍按核心大词读 -> 行为与改动前一致(零回归)。"""
    ctx = _ctx(ws_tmp)
    root = ctx.paths.cat_root
    with open(os.path.join(root, "t-screen-results.json"), "w", encoding="utf-8") as fh:
        json.dump([{"keyword": "auto one", "verdict": "达标"}], fh)
    with open(os.path.join(root, "filter-t-core-words.txt"), "w", encoding="utf-8") as fh:
        fh.write("legacy core\n")
    out = Step10Variants()._final_keywords(ctx, "t")
    assert out == ["legacy core"]
