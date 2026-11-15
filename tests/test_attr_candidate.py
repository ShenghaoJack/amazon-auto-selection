# -*- coding: utf-8 -*-
"""步骤16「候选属性」在 Web 面板的显示与决定（2026-10-12 用户口径）。

要解决的问题
===============================================================================
步骤16 的 N 次抽取里，`runs < ATTR_SPEC_MIN_RUNS`（默认 2）的属性**被挡在 spec 之外**
（避免属性集膨胀），只写进 `16_产品属性标注/属性集待确认清单.json`。
而 **Web 里没有任何入口能看它**（`webui/` 曾对这个文件名零引用）→
**一个真正值得加的新维度会被无声漏掉**。

规则（用户确认）
===============================================================================
  ① **显示**：候选常驻显示在步骤16 面板（不再只躺在 json 里）
  ② **采纳** → 写 `ATTR_KEYS`（步骤16 无条件追加 → 下轮必然进属性集）
              **并**从 `ATTR_EXCLUDE` 移除（否则两边冲突，步骤16 按"不要"处理）
  ③ **忽略** → 写 `ATTR_EXCLUDE`（下轮 AI 再提议也照样丢弃，且进 `_excluded_proposals.json` 留痕）
  ④ **已决的自动消失**：名字已在 `ATTR_KEYS` / `ATTR_EXCLUDE` 里 → 不再列为候选
     （**不动 step16 的产物文件** —— 下一轮它自己会重写）
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CAT = "Demo Cat-US-202609"
PENDING = "属性集待确认清单.json"


def _load_server():
    spec = importlib.util.spec_from_file_location("webui_server_attrcand", ROOT / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(ws_tmp):
    s = _load_server()
    s.PROJECT_ROOT = Path(ws_tmp)
    return s


def _mk(ws_tmp, extra_cfg: str = "") -> Path:
    root = Path(ws_tmp)
    d = root / "data" / CAT
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(
        f'CAT: "{CAT}"\nDATA_ROOT: "{(root / "data").as_posix()}"\n' + extra_cfg,
        encoding="utf-8")
    return d


def _handler(server):
    return server.Handler.__new__(server.Handler)


def _file_q() -> dict:
    """POST 体用 `config_file`（GET 才用 `file` 查询参数）。"""
    return {"config_file": f"data/{CAT}/config.yaml"}


def _write_pending(ws_tmp, items: list[dict], threshold: str = "runs >= 2 才自动采纳") -> Path:
    d = Path(ws_tmp) / "data" / CAT / "16_产品属性标注"
    d.mkdir(parents=True, exist_ok=True)
    p = d / PENDING
    p.write_text(json.dumps({"_说明": "…", "cat": CAT, "阈值": threshold,
                             "generated_at": "2026-10-12T11:00:00", "items": items},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _cfg_text(ws_tmp) -> str:
    return (Path(ws_tmp) / "data" / CAT / "config.yaml").read_text(encoding="utf-8")


def _write_spec(ws_tmp, names: list[str]) -> Path:
    """写一份最小 spec（方案 C 起，"采纳"直写 spec，所以测试要先有 spec）。"""
    p = Path(ws_tmp) / "data" / CAT / f"{CAT}-attribute-spec.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"cat": CAT, "status": "confirmed",
         "attributes": [{"name": n, "hint": "", "source": "llm",
                         "modified": False, "runs": 3} for n in names]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _spec_names(ws_tmp) -> list[str]:
    p = Path(ws_tmp) / "data" / CAT / f"{CAT}-attribute-spec.json"
    o = json.loads(p.read_text(encoding="utf-8"))
    return [str(a.get("name")) for a in (o.get("attributes") or [])]


# ---------------------------------------------------------------- ① 显示
def test_candidates_read_from_step16_output(server, ws_tmp):
    """★ 候选能从步骤16 的产物里读出来（带 runs 与 AI 说明）。"""
    _mk(ws_tmp)
    _write_pending(ws_tmp, [
        {"属性": "品牌", "出现次数": 1, "说明": "只在一次抽取里出现"},
        {"属性": "木材树种", "出现次数": 1, "说明": "可能与主体材质重叠"},
    ])
    got = _handler(server)._candidates_list(f"data/{CAT}/config.yaml")
    assert [c["name"] for c in got] == ["品牌", "木材树种"]
    assert got[0]["runs"] == 1
    assert "只在一次抽取里出现" in got[0]["hint"]


def test_candidates_missing_file_is_empty(server, ws_tmp):
    """没跑过步骤16 / 没触发阈值 → 空列表（UI 会整块隐藏）。"""
    _mk(ws_tmp)
    assert _handler(server)._candidates_list(f"data/{CAT}/config.yaml") == []


def test_candidates_corrupt_file_is_empty(server, ws_tmp):
    """产物坏了不该炸。"""
    _mk(ws_tmp)
    _write_pending(ws_tmp, []).write_text("{ 坏 json", encoding="utf-8")
    assert _handler(server)._candidates_list(f"data/{CAT}/config.yaml") == []


# ---------------------------------------------------------------- ④ 已决的消失
def test_candidates_hide_decided(server, ws_tmp):
    """★ 已在 **spec**（采纳过）或 `ATTR_EXCLUDE`（忽略过）的名字**不再列为候选**。

    方案 C（2026-10-12）起"采纳"的落点是 **spec**（不再写 `ATTR_KEYS`），所以判定也按 spec。
    """
    _mk(ws_tmp, extra_cfg='ATTR_EXCLUDE: "木材树种"\n')
    _write_spec(ws_tmp, ["品牌"])
    _write_pending(ws_tmp, [
        {"属性": "品牌", "出现次数": 1},
        {"属性": "木材树种", "出现次数": 1},
        {"属性": "雕刻文字", "出现次数": 1},          # 还没决 → 应留下
    ])
    got = _handler(server)._candidates_list(f"data/{CAT}/config.yaml")
    assert [c["name"] for c in got] == ["雕刻文字"]


def test_candidates_skips_blank_names(server, ws_tmp):
    _mk(ws_tmp)
    _write_pending(ws_tmp, [{"属性": "  ", "出现次数": 1}, {"属性": "品牌", "出现次数": 1}])
    assert [c["name"] for c in _handler(server)._candidates_list(f"data/{CAT}/config.yaml")] == ["品牌"]


# ---------------------------------------------------------------- ② 采纳
def test_candidate_adopt_writes_into_spec(server, ws_tmp):
    """★ 采纳 → **直接加进 spec**（方案 C：spec 是唯一真值，不再走 `ATTR_KEYS`）。"""
    _mk(ws_tmp)
    _write_spec(ws_tmp, ["产品类型"])
    h = _handler(server)
    r = h._api_attr_candidate({**_file_q(), "name": "雕刻文字", "action": "adopt"})
    assert r["ok"] and r["action"] == "adopt"
    assert _spec_names(ws_tmp) == ["产品类型", "雕刻文字"]
    # 加进 spec 后状态必须回落待确认（人工动过属性集 → 停① 重确认）
    o = json.loads((Path(ws_tmp) / "data" / CAT / f"{CAT}-attribute-spec.json")
                   .read_text(encoding="utf-8"))
    assert o["status"] == "pending_confirm"


def test_candidate_adopt_also_removes_from_exclude(server, ws_tmp):
    """★ 采纳时若它同时在「不要清单」里 → **必须移出**。

    `ATTR_EXCLUDE` 是**唯一的删除通道**：两边同时有的话下一步会被重新丢掉
    → 用户点了"采纳"却没生效（且只表现为一条日志）。
    """
    _mk(ws_tmp, extra_cfg='ATTR_EXCLUDE: "雕刻文字;形状"\n')
    _write_spec(ws_tmp, ["产品类型"])
    h = _handler(server)
    r = h._api_attr_candidate({**_file_q(), "name": "雕刻文字", "action": "adopt"})
    assert r["note"], "应当提示「已同时移出不要清单」"
    assert "雕刻文字" in _spec_names(ws_tmp)
    txt = _cfg_text(ws_tmp)
    assert "形状" in txt, "别的排除条目不能被抹掉"
    for ln in txt.splitlines():
        if ln.startswith("ATTR_EXCLUDE"):
            assert "雕刻文字" not in ln, ln


def test_candidate_adopt_makes_it_disappear(server, ws_tmp):
    """采纳后**立刻**从候选里消失（响应里带回新的候选清单）。"""
    _mk(ws_tmp)
    _write_spec(ws_tmp, ["产品类型"])
    _write_pending(ws_tmp, [{"属性": "雕刻文字", "出现次数": 1},
                            {"属性": "品牌", "出现次数": 1}])
    h = _handler(server)
    r = h._api_attr_candidate({**_file_q(), "name": "雕刻文字", "action": "adopt"})
    assert [c["name"] for c in r["candidates"]] == ["品牌"]


def test_candidate_adopt_without_spec_is_rejected(server, ws_tmp):
    """还没跑过步骤16（没有 spec）→ 采纳要**明确报错**，不能静默什么都不做。"""
    _mk(ws_tmp)
    h = _handler(server)
    with pytest.raises(ValueError):
        h._api_attr_candidate({**_file_q(), "name": "雕刻文字", "action": "adopt"})


# ---------------------------------------------------------------- ③ 忽略
def test_candidate_ignore_writes_exclude(server, ws_tmp):
    """★ 忽略 → 写 `ATTR_EXCLUDE`（下轮 AI 再提议也照样被丢弃）。"""
    _mk(ws_tmp)
    h = _handler(server)
    r = h._api_attr_candidate({**_file_q(), "name": "品牌", "action": "ignore"})
    assert r["ok"] and r["action"] == "ignore"
    txt = _cfg_text(ws_tmp)
    assert "ATTR_EXCLUDE" in txt and "品牌" in txt
    assert [c["name"] for c in r["candidates"]] == []


# ---------------------------------------------------------------- 参数校验
def test_candidate_rejects_bad_action(server, ws_tmp):
    _mk(ws_tmp)
    h = _handler(server)
    with pytest.raises(ValueError):
        h._api_attr_candidate({**_file_q(), "name": "品牌", "action": "delete"})
    with pytest.raises(ValueError):
        h._api_attr_candidate({**_file_q(), "name": "  ", "action": "adopt"})
