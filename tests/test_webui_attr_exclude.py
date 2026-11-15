# -*- coding: utf-8 -*-
"""Web「不要清单」端点的**写回不丢数据**（2026-10-12 修复）。

抓到的真 bug
===============================================================================
`_api_attr_exclude` / `_excluded_list` / `_api_attr_candidate` 都用
`cfg = self._status_cfg(...)` 然后 `cfg.get("ATTR_EXCLUDE")` ——
而 **`_status_cfg` 只返回 `{CAT, DATA_ROOT}`**（见 `server.py:4247` 的实现），
**根本不含 `ATTR_*`** → 读到的一律是**空** → 再 `";".join(cur)` 写回：

  · **加一条** → 把**原有整份清单抹掉**，只剩新加的那条
  · **移除一条** → 清单变成空（全都"恢复"了）
  · **强制加回** → `ATTR_KEYS` 只剩这一个名字
  · **「已排除属性」UI** → 运行层条目**永远显示不出来**（人以为删除没生效）

而 UI 上看起来"只是加了一条" —— **静默数据丢失**，最危险的那类。
现在统一走 `_cat_cfg_values()`（读**配置文件本身**）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CAT = "Demo Cat-US-202609"


def _load_server():
    spec = importlib.util.spec_from_file_location("webui_server_attrex", ROOT / "webui" / "server.py")
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


def _body(**kw) -> dict:
    return {"config_file": f"data/{CAT}/config.yaml", **kw}


def _cfgs(ws_tmp) -> dict:
    """把 config.yaml 解析成 {键: 原始值字符串}（不依赖 yaml 库）。"""
    out: dict[str, str] = {}
    for ln in (Path(ws_tmp) / "data" / CAT / "config.yaml").read_text(encoding="utf-8").splitlines():
        if ":" in ln and not ln.startswith(" "):
            k, v = ln.split(":", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def _names(v: str) -> list[str]:
    return [x.strip() for x in str(v or "").replace("；", ";").replace(",", ";").split(";") if x.strip()]


# ---------------------------------------------------------------- 加：不能抹掉旧的
def test_exclude_add_preserves_existing(server, ws_tmp):
    """★ 加一条**不能抹掉原有**（这是被修掉的数据丢失 bug）。"""
    _mk(ws_tmp, extra_cfg='ATTR_EXCLUDE: "厚度;形状"\n')
    h = _handler(server)
    h._api_attr_exclude(_body(action="add", name="品牌"))
    got = _names(_cfgs(ws_tmp)["ATTR_EXCLUDE"])
    assert set(got) == {"厚度", "形状", "品牌"}, got


def test_exclude_remove_preserves_others(server, ws_tmp):
    """★ 移除一条**不能把别的也清了**。"""
    _mk(ws_tmp, extra_cfg='ATTR_EXCLUDE: "厚度;形状;品牌"\n')
    h = _handler(server)
    h._api_attr_exclude(_body(action="remove", name="形状"))
    got = _names(_cfgs(ws_tmp)["ATTR_EXCLUDE"])
    assert set(got) == {"厚度", "品牌"}, got


def test_exclude_force_add_writes_into_spec(server, ws_tmp):
    """★ 「强制加回」= 移出不要清单 **并直接加进 spec**（方案 C，2026-10-12）。

    以前是写 `ATTR_KEYS` 靠步骤16"再加回来"；现在 spec 是唯一真值、且步骤16 **只增不减**
    → 直写 spec 即可。这条同时守住"**不能抹掉 spec 里已有的属性**"。
    """
    _mk(ws_tmp, extra_cfg='ATTR_EXCLUDE: "厚度"\n')
    _sp = Path(ws_tmp) / "data" / CAT / f"{CAT}-attribute-spec.json"
    _sp.write_text(json.dumps(
        {"cat": CAT, "status": "confirmed",
         "attributes": [{"name": "是否双面可用", "hint": "", "source": "llm",
                         "modified": False, "runs": 3}]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    h = _handler(server)
    r = h._api_attr_exclude(_body(action="remove", name="厚度", force=True))
    names = [str(a.get("name")) for a in json.loads(
        _sp.read_text(encoding="utf-8")).get("attributes") or []]
    assert names == ["是否双面可用", "厚度"], names
    assert "已加进属性集" in str(r.get("forced") or ""), r.get("forced")
    # 别的排除条目不能被抹掉
    assert set(_names(_cfgs(ws_tmp).get("ATTR_EXCLUDE", ""))) == set(), _cfgs(ws_tmp)


# ---------------------------------------------------------------- 读：UI 要看得见
def test_excluded_list_shows_config_level(server, ws_tmp):
    """★ 「已排除属性」UI 必须显示**运行层**（config.yaml）的条目。

    原 bug：这一段的来源**永远为空** → 人删了属性，面板上却看不到它 → 以为没生效。
    """
    _mk(ws_tmp, extra_cfg='ATTR_EXCLUDE: "厚度;形状"\n')
    h = _handler(server)
    got = h._excluded_list(f"data/{CAT}/config.yaml")
    names = {x["属性"] for x in got}
    assert names == {"厚度", "形状"}, names
    assert all(x["来源"] == "本市场" and x["范围"] == "config.yaml" for x in got)


def test_excluded_list_empty_when_none(server, ws_tmp):
    _mk(ws_tmp)
    assert _handler(server)._excluded_list(f"data/{CAT}/config.yaml") == []


# ---------------------------------------------------------------- 读：解析辅助本身
def test_cat_cfg_values_really_reads_business_keys(server, ws_tmp):
    """★ `_cat_cfg_values` 必须真的读到业务键（`_status_cfg` 读不到，这就是根因）。"""
    _mk(ws_tmp, extra_cfg='ATTR_EXCLUDE: "厚度"\nATTR_KEYS: "品牌"\n')
    h = _handler(server)
    vals = h._cat_cfg_values(f"data/{CAT}/config.yaml")
    assert str(vals.get("ATTR_EXCLUDE")) == "厚度"
    assert str(vals.get("ATTR_KEYS")) == "品牌"
    # 反证：`_status_cfg` 确实没有这些键（否则上面的 bug 不成立）
    st = h._status_cfg(f"data/{CAT}/config.yaml")
    assert "ATTR_EXCLUDE" not in st and "ATTR_KEYS" not in st
