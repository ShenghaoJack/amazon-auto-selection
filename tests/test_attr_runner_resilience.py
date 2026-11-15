# -*- coding: utf-8 -*-
"""run_round 韧性回归: 单份运行的 LLM 产出失败不炸整步(参考 steps/_filtering.judge_batch)。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.attr_stability import AttrSpec, RuleSource  # noqa: E402
from tools.attr_stability.attr_runner import run_round  # noqa: E402


def test_fill_missing_only_adds_requested_missing_keys(tmp_path):
    from tools.attr_stability.attr_runner import CallableProducer
    row = _row()
    row['attributes'].pop('供电方式_依据')
    def fn(prompt):
        return {'rows': [{'xl_row': 1, 'attributes': {'供电方式': 'USB充电',
                 '供电方式_来源': '图片', '供电方式_依据': '原文证据', '其他': '意外改动'}}]}
    prod = CallableProducer(fn=fn, rows=[row], out_dir=str(tmp_path), prefix='a',
                            prompt_text='P', save_raw=True)
    result = prod.fill_missing({1: row}, {1: ['供电方式']}, 'a1')
    assert result[1]['attributes'] == {'供电方式': '电池供电', '供电方式_来源': '标题'}


def test_fill_missing_reads_source_and_repairs_only_supported_empty_basis(tmp_path):
    from tools.attr_stability.attr_runner import CallableProducer
    original = {'xl_row': 1, '标题': 'battery powered bowl', 'SKU': 'Color: Black'}
    output = _row()
    output['attributes']['供电方式_依据'] = ''
    prompts = []
    def fn(prompt):
        prompts.append(prompt)
        return {'rows': [{'xl_row': 1, 'attributes': {'供电方式': '电池供电',
                '供电方式_依据': '标题：battery powered'}}]}
    prod = CallableProducer(fn, [original], str(tmp_path), 'a', 'P',
                            input_fields=['xl_row', '标题', 'SKU'])
    result = prod.fill_missing({1: output}, {1: ['供电方式']}, 'a1')
    assert 'battery powered bowl' in prompts[0] and 'Color: Black' in prompts[0]
    assert result[1]['attributes']['供电方式_依据'] == '标题：battery powered'
    assert result[1]['attributes']['供电方式'] == '电池供电'


def test_fill_missing_rejects_unrelated_or_unsupported_basis(tmp_path):
    from tools.attr_stability.attr_runner import CallableProducer
    original = {'xl_row': 1, '标题': 'battery powered bowl'}
    output = _row()
    output['attributes']['供电方式_依据'] = ''
    for returned in ({'供电方式': 'USB充电', '供电方式_依据': '标题：battery powered'},
                     {'供电方式': '电池供电', '供电方式_依据': '标题：made up'}):
        prod = CallableProducer(lambda _: {'rows': [{'xl_row': 1, 'attributes': returned}]},
                                [original], str(tmp_path), 'a', 'P', input_fields=['xl_row', '标题'])
        result = prod.fill_missing({1: output}, {1: ['供电方式']})
        assert result[1]['attributes']['供电方式'] == '未明确'
        assert '待复核' in result[1]['attributes']['供电方式_依据']


def test_fill_missing_marks_original_as_unknown_only_when_model_cannot_support_it(tmp_path):
    from tools.attr_stability.attr_runner import CallableProducer
    original = {'xl_row': 1, '标题': 'bowl without power details'}
    output = _row()
    output['attributes']['供电方式_依据'] = ''
    def fn(prompt):
        assert '电池供电' in prompt and 'bowl without power details' in prompt
        return {'rows': [{'xl_row': 1, 'attributes': {'供电方式': '未明确', '供电方式_依据': '原文无明确说明'}}]}
    prod = CallableProducer(fn, [original], str(tmp_path), 'a', 'P', input_fields=['xl_row', '标题'])
    result = prod.fill_missing({1: output}, {1: ['供电方式']})
    assert result[1]['attributes']['供电方式'] == '未明确'
    assert result[1]['attributes']['供电方式_来源'] == ''
    assert '待复核' in result[1]['attributes']['供电方式_依据']


def test_repair_rows_uses_original_input_instead_of_output_row(tmp_path):
    from tools.attr_stability.attr_runner import CallableProducer
    original = {'xl_row': 1, '标题': 'original product title'}
    seen = []
    def fn(prompt):
        seen.append(prompt)
        return {'rows': [{**_row(), 'xl_row': 1}]}
    prod = CallableProducer(fn, [original], str(tmp_path), 'a', 'P', input_fields=['xl_row', '标题'])
    prod.repair_rows({1: _row()}, {1: ['漏属性']})
    assert 'original product title' in seen[0]


def test_round_repairs_empty_basis_without_overwriting_judgement(tmp_path):
    from tools.attr_stability.attr_runner import CallableProducer
    source = {'xl_row': 1, '标题': 'battery powered bowl'}
    bad = _row()
    bad['attributes']['供电方式_依据'] = ''
    def fn(prompt):
        assert 'battery powered bowl' in prompt
        return {'rows': [{'xl_row': 1, 'attributes': {'供电方式': '电池供电',
                 '供电方式_依据': '标题：battery powered'}}]}
    prod = CallableProducer(fn, [source], str(tmp_path), 'a', 'P', input_fields=['xl_row', '标题'])
    for tag in ('a1', 'a2'):
        prod._save_ckpt(prod._raw_path(tag), prod._input_key(), {1: bad})
    result = run_round(prod, _rules(), n_runs=2, max_retry=1, require_basis=True,
                       strict_gate=False, require_complete=True, log=lambda _: None)
    assert result.passed == ['a1', 'a2']
    assert result.gates['a1'].required_errors == 0


def test_unrepaired_missing_fields_do_not_trigger_full_run(tmp_path):
    from tools.attr_stability.attr_runner import CallableProducer
    calls = []
    row = _row()
    row['attributes'].pop('供电方式_依据')
    def fn(prompt):
        calls.append(prompt)
        return {'rows': [{'xl_row': 1, 'attributes': {'供电方式': 'USB充电'}}]}
    prod = CallableProducer(fn=fn, rows=[row], out_dir=str(tmp_path), prefix='a',
                            prompt_text='P', save_raw=True)
    prod._save_ckpt(prod._raw_path('a1'), prod._input_key(), {1: row})
    prod._save_ckpt(prod._raw_path('a2'), prod._input_key(), {1: _row()})
    logs = []
    result = run_round(prod, _rules(), n_runs=2, max_retry=1, log=logs.append,
                       require_basis=True)
    assert result.gates['a1'].verdict == 'REJECT'
    assert result.gates['a2'].verdict == 'PASS'
    assert len(calls) == 2
    assert all('只需补这些属性' in p for p in calls)
    assert any('不整份重跑' in line for line in logs)


def _rules() -> RuleSource:
    rs = RuleSource()
    rs.attrs = {"供电方式": AttrSpec(name="供电方式", values=["电池供电", "USB充电"])}
    return rs


def _row() -> dict:
    return {
        "xl_row": 1,
        "category_check": {"判定": "属于本类目"},
        # ★ 2026-10-09：新 schema 要求每个属性都给 `_依据` 原文（提示词 §3.3e），
        #   闸门硬拦漏依据 → 不补的话这里每份运行都 REJECT，断言 `PASS` 会假失败。
        "attributes": {"供电方式": "电池供电", "供电方式_来源": "标题",
                       "供电方式_依据": "标题：battery powered"},
        "尺寸_拆列": {"长": "", "宽": "", "高": ""}, "尺寸_依据": "",
        "suitable_scene_规范串": "", "词表外词": [],
        "pack_count_数值": None, "跨列纠错": [], "冲突": [],
    }


def test_model_row_normalization_keeps_attributes_wrapper_and_evidence():
    from tools.attr_stability.attr_runner import _normalize_got_row

    original = _row()
    assert _normalize_got_row(original) == original
    nested = {"row_id": "1", "category_check": original["category_check"],
              "attributes": {"供电方式": {"value": "电池供电", "source": "标题",
                                        "basis": "battery powered"}}}
    actual = _normalize_got_row(nested)
    assert actual["xl_row"] == "1"
    assert actual["attributes"] == {"供电方式": "电池供电", "供电方式_来源": "标题",
                                    "供电方式_依据": "battery powered"}
    assert "供电方式" not in actual
    assert nested["attributes"]["供电方式"]["value"] == "电池供电"


def test_run_round_isolates_producer_failure():
    """一份运行的产出抛异常(模拟 LLM 超时) → 只废这一份, 标 REJECT, 其余照常。"""
    class _FailProducer:
        def produce(self, run_tag, retry_brief=None):
            if run_tag == "a2":
                raise RuntimeError("LLM 超时(模拟)")
            return {1: _row()}

    logs: list[str] = []
    res = run_round(_FailProducer(), _rules(), n_runs=3, log=logs.append)

    assert res.gates["a2"].verdict == "REJECT"
    assert res.gates["a1"].verdict == "PASS"
    assert res.gates["a3"].verdict == "PASS"
    assert res.passed == ["a1", "a3"]                 # 失败的那份不进 passed
    assert any("产出失败" in l and "a2" in l for l in logs)


def test_run_round_does_not_reproduce_passed_runs():
    """稳定性阶段复用已产出结果, 不重复调 produce(不重复花 token)。"""
    calls: list[str] = []

    class _CountingProducer:
        def produce(self, run_tag, retry_brief=None):
            calls.append(run_tag)
            return {1: _row()}

    res = run_round(_CountingProducer(), _rules(), n_runs=2, log=lambda *_: None)
    assert res.passed == ["a1", "a2"]
    assert calls == ["a1", "a2"]                     # 每份只产出一次, 无第二次重产


def test_is_truncation_detects_both_signals():
    """截断识别: 专用异常类名 LLMTruncatedError 或 finish_reason=length 文案。"""
    from tools.attr_stability.attr_runner import CallableProducer

    class LLMTruncatedError(Exception):
        pass

    assert CallableProducer._is_truncation(LLMTruncatedError("x"))
    assert CallableProducer._is_truncation(RuntimeError("输出超长被截断(finish_reason=length, ...)"))
    assert not CallableProducer._is_truncation(RuntimeError("connection refused"))


def test_producer_splits_chunk_on_truncation():
    """输出被截断(finish_reason=length)时, 自动把 chunk 拆半重试, 不丢行。"""
    import re
    from tools.attr_stability.attr_runner import CallableProducer

    calls: list[int] = []

    def fn(prompt):
        n = prompt.count('"xl_row"')
        calls.append(n)
        if n > 4:  # 模拟: 超过 4 行输出就截断
            raise RuntimeError("输出超长被截断(finish_reason=length, max_tokens=16000 已到上限)")
        ids = [int(m) for m in re.findall(r'"xl_row":\s*(\d+)', prompt)]
        return {"rows": [{"xl_row": i} for i in ids]}

    rows = [{"xl_row": i} for i in range(1, 9)]  # 8 行, chunk=8
    prod = CallableProducer(fn=fn, rows=rows, out_dir="/nonexistent", prefix="a",
                            prompt_text="P", chunk=8, save_raw=False)
    result = prod.produce("a1")

    assert set(result) == set(range(1, 9))          # 所有行都被产出
    assert calls[0] == 8                             # 首调 8 行 → 截断
    assert all(n <= 4 for n in calls[1:])            # 拆半后每批 <= 4 行成功


def test_producer_retries_missing_rows():
    """主跑漏行(LLM 只回部分行) → 缺的行按 1 行/次补跑, 不静默丢行。"""
    import re
    from tools.attr_stability.attr_runner import CallableProducer

    def fn(prompt):
        n = prompt.count('"xl_row"')
        ids = [int(m) for m in re.findall(r'"xl_row":\s*(\d+)', prompt)]
        if n > 6:  # 主跑(12行/批)故意只回前 6 行, 模拟漏行
            return {"rows": [{"xl_row": i} for i in ids[:6]]}
        return {"rows": [{"xl_row": i} for i in ids]}  # 补跑(1行/次)回全部

    rows = [{"xl_row": i} for i in range(1, 13)]  # 12 行, chunk=12
    prod = CallableProducer(fn=fn, rows=rows, out_dir="/nonexistent", prefix="a",
                            prompt_text="P", chunk=12, save_raw=False, workers=1)
    result = prod.produce("a1")

    assert set(result) == set(range(1, 13))  # 缺的 6 行被补跑补回


def test_producer_resumes_from_checkpoint(tmp_path):
    """中断后重启: 检查点已产出的行被读回, 只跑剩余的行(不重复调已产出的)。"""
    import re
    from tools.attr_stability.attr_runner import CallableProducer

    calls: list[set] = []  # 记录每次调用收到哪些 xl_row(用于断言不重复产出)

    def fn(prompt):
        ids = {int(m) for m in re.findall(r'"xl_row":\s*(\d+)', prompt)}
        calls.append(ids)
        return {"rows": [{"xl_row": i, "v": "x%d" % i} for i in sorted(ids)]}

    rows = [{"xl_row": i} for i in range(1, 9)]  # 8 行, chunk=4 → 2 批
    out_dir = str(tmp_path)
    prod = CallableProducer(fn=fn, rows=rows, out_dir=out_dir, prefix="a",
                            prompt_text="P", chunk=4, save_raw=True, workers=1)

    # 模拟"只跑了第一批就中断": 手工构造一个只含前 4 行的检查点(raw 文件尚不存在)
    ckpt = prod._ckpt_path("a1")
    import json as _json
    _json.dump({"input_key": prod._input_key(),
                "rows": [{"xl_row": i, "v": "x%d" % i} for i in range(1, 5)]},
               open(ckpt, "w", encoding="utf-8"))

    result2 = prod.produce("a1")
    assert set(result2) == set(range(1, 9))
    # 续跑只重跑剩余 4 行(一批), 前 4 行不再重复调
    assert len(calls) == 1
    assert calls[0] == {5, 6, 7, 8}


def test_producer_reuses_completed_run(tmp_path):
    """已完整产出的运行: 重启时直接复用 raw 文件, 不再调 fn。"""
    import re
    from tools.attr_stability.attr_runner import CallableProducer

    calls: list[set] = []

    def fn(prompt):
        ids = {int(m) for m in re.findall(r'"xl_row":\s*(\d+)', prompt)}
        calls.append(ids)
        return {"rows": [{"xl_row": i} for i in sorted(ids)]}

    rows = [{"xl_row": i} for i in range(1, 5)]
    out_dir = str(tmp_path)
    prod = CallableProducer(fn=fn, rows=rows, out_dir=out_dir, prefix="a",
                            prompt_text="P", chunk=2, save_raw=True, workers=1)
    prod.produce("a1")
    assert len(calls) == 2

    calls.clear()
    result = prod.produce("a1")            # 重启同一 run_tag
    assert set(result) == set(range(1, 5))
    assert calls == []                     # 完整产出 → 不重复调 fn


def test_repair_uses_reduced_chunk_and_persists_before_interruption(tmp_path):
    import json
    import re
    import pytest
    from tools.attr_stability.attr_runner import CallableProducer

    calls = []
    rows = [{**_row(), "xl_row": i} for i in range(1, 14)]
    old = {r["xl_row"]: {**r, "attributes": {}} for r in rows}

    def fn(prompt):
        ids = [int(i) for i in re.findall(r'"xl_row":\s*(\d+)', prompt)]
        calls.append(ids)
        if len(calls) == 2:
            raise KeyboardInterrupt("模拟进程中断")
        return {"rows": [{"row_id": str(i), "attributes": _row()["attributes"]}
                         for i in ids]}

    prod = CallableProducer(fn, rows, str(tmp_path), "a", "P", chunk=12, workers=1)
    prod.chunk = 6
    with pytest.raises(KeyboardInterrupt):
        prod.repair_rows(old, {i: ["漏属性"] for i in old}, run_tag="a1")
    assert list(map(len, calls)) == [6, 6]
    saved = json.loads((tmp_path / "a1.json").read_text(encoding="utf-8"))
    assert len(saved["rows"]) == 13
    assert sum(bool(r["attributes"]) for r in saved["rows"]) == 6

    prod.fn = lambda _: pytest.fail("续跑读取修复结果不应调用模型")
    resumed = prod.produce("a1")
    assert sum(bool(r["attributes"]) for r in resumed.values()) == 6
    assert not list(tmp_path.glob("*.tmp"))


def test_repair_recursively_splits_truncated_output(tmp_path):
    import re
    from tools.attr_stability.attr_runner import CallableProducer

    sizes = []
    def fn(prompt):
        ids = [int(i) for i in re.findall(r'"xl_row":\s*(\d+)', prompt)]
        sizes.append(len(ids))
        if len(ids) > 2:
            raise RuntimeError("finish_reason=length")
        return {"rows": [{"xl_row": i, "attributes": _row()["attributes"]} for i in ids]}

    rows = [{**_row(), "xl_row": i} for i in range(1, 7)]
    prod = CallableProducer(fn, rows, str(tmp_path), "a", "P", chunk=6, workers=1)
    result = prod.repair_rows({r["xl_row"]: r for r in rows},
                              {i: ["漏属性"] for i in range(1, 7)}, "a1")
    assert len(result) == 6 and sizes[0] == 6
    assert prod.produce("a1") == result


def test_run_round_repaired_cache_passes_without_repeated_model_calls(tmp_path):
    import re
    from tools.attr_stability.attr_runner import CallableProducer

    calls = []
    def fn(prompt):
        ids = [int(i) for i in re.findall(r'"xl_row":\s*(\d+)', prompt)]
        calls.append(ids)
        return {"rows": [{**_row(), "xl_row": i} for i in ids]}

    rows = [{**_row(), "xl_row": i} for i in range(1, 14)]
    prod = CallableProducer(fn, rows, str(tmp_path), "a", "P", chunk=12, workers=2)
    bad = {r["xl_row"]: {**r, "attributes": {}} for r in rows}
    for tag in ("a1", "a2"):
        prod._save_ckpt(prod._raw_path(tag), prod._input_key(), bad)
    first = run_round(prod, _rules(), n_runs=2, log=lambda *_: None)
    assert first.passed == ["a1", "a2"]
    assert calls and max(map(len, calls)) <= 6
    calls.clear()
    second = run_round(prod, _rules(), n_runs=2, log=lambda *_: None)
    assert second.passed == ["a1", "a2"] and not calls


def test_atomic_save_failure_preserves_previous_run_and_cleans_temp(tmp_path, monkeypatch):
    import pytest
    from tools.attr_stability.attr_runner import CallableProducer

    target = tmp_path / "a1.json"
    target.write_text("previous complete data", encoding="utf-8")
    def fail_replace(*_):
        raise PermissionError("模拟替换失败")
    monkeypatch.setattr("os.replace", fail_replace)
    with pytest.raises(PermissionError):
        CallableProducer._save_ckpt(str(target), "key", {1: _row()})
    assert target.read_text(encoding="utf-8") == "previous complete data"
    assert not list(tmp_path.glob("*.tmp"))


def test_producer_keeps_checkpoint_when_still_missing(tmp_path):
    """补跑 3 轮后仍缺行: 检查点保留(不删), 供下次续跑; raw 不完整不被当成品。"""
    import re
    from tools.attr_stability.attr_runner import CallableProducer

    def fn(prompt):
        ids = [int(m) for m in re.findall(r'"xl_row":\s*(\d+)', prompt)]
        # 只回 xl_row < 3 的行, 3/4 无论主跑还是逐行补跑都缺
        return {"rows": [{"xl_row": i} for i in ids if i < 3]}

    rows = [{"xl_row": i} for i in range(1, 5)]
    out_dir = str(tmp_path)
    prod = CallableProducer(fn=fn, rows=rows, out_dir=out_dir, prefix="a",
                            prompt_text="P", chunk=4, save_raw=True, workers=1)
    result = prod.produce("a1")
    assert set(result) == {1, 2}           # 3/4 始终缺
    ckpt = prod._ckpt_path("a1")
    import os as _os
    assert _os.path.exists(ckpt)           # 仍缺行 → 检查点保留

    # 重启: raw 不完整不被复用, 检查点能读回已产出的 2 行(从缺的 2 行继续补)
    calls2 = []
    prod2 = CallableProducer(
        fn=lambda p: (calls2.append(p) or {"rows": []}),
        rows=rows, out_dir=out_dir, prefix="a", prompt_text="P",
        chunk=4, save_raw=True, workers=1)
    result2 = prod2.produce("a1")
    assert set(result2) == {1, 2}          # 仍是 1/2(3/4 依旧缺)
    assert len(calls2) >= 1                # 重启后确实又补跑了(没有静默当成品跳过)
