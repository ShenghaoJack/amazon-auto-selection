# 单元测试: 属性集规格 Web API(2026-09-21, Phase 1)
# ---------------------------------------------------------------------------
# GET  /api/attr-spec           读取步骤16 属性集规格(不存在则 exists=False + 空清单)
# POST /api/attr-spec/save      保存人工编辑(增/删/改名/改说明), 状态回落 pending_confirm
# POST /api/attr-spec/confirm   人工确认(status → confirmed), 进入步骤17 的前置
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CAT = "Demo Cat-US-202609"


def _load_server():
    spec = importlib.util.spec_from_file_location("webui_server_attrspec", ROOT / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(ws_tmp):
    s = _load_server()
    s.PROJECT_ROOT = Path(ws_tmp)
    return s


def _mk(ws_tmp, cat: str = CAT) -> Path:
    root = Path(ws_tmp)
    d = root / "data" / cat
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(
        f'CAT: "{cat}"\nDATA_ROOT: "{(root / "data").as_posix()}"\n', encoding="utf-8")
    return d


def _spec_path(ws_tmp, cat: str = CAT) -> Path:
    return Path(ws_tmp) / "data" / cat / f"{cat}-attribute-spec.json"


def _handler(server):
    return server.Handler.__new__(server.Handler)


def _file_q(cat: str = CAT) -> dict:
    return {"file": [f"data/{cat}/config.yaml"]}


def _write_spec(ws_tmp, attrs: list[dict], status: str = "pending_confirm"):
    p = _spec_path(ws_tmp)
    p.write_text(json.dumps({"cat": CAT, "status": status, "attributes": attrs},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def test_get_attr_spec_empty(server, ws_tmp):
    _mk(ws_tmp)
    h = _handler(server)
    out = h._api_attr_spec(_file_q())
    assert out["ok"] is True and out["cat"] == CAT
    assert out["exists"] is False and out["status"] == "" and out["attributes"] == []


def test_get_attr_spec_with_file(server, ws_tmp):
    _mk(ws_tmp)
    _write_spec(ws_tmp, [
        {"name": "材质", "hint": "不锈钢/陶瓷", "source": "llm", "modified": False, "runs": 3},
        {"name": "容量", "hint": "", "source": "manual", "modified": True, "runs": 1},
    ])
    h = _handler(server)
    out = h._api_attr_spec(_file_q())
    assert out["exists"] is True and out["status"] == "pending_confirm"
    assert [a["name"] for a in out["attributes"]] == ["材质", "容量"]
    assert out["attributes"][0]["source"] == "llm" and out["attributes"][0]["runs"] == 3
    assert out["attributes"][0]["modified"] is False
    assert out["attributes"][1]["modified"] is True


def test_save_creates_pending_spec(server, ws_tmp):
    _mk(ws_tmp)
    h = _handler(server)
    out = h._api_attr_spec_save({"config_file": f"data/{CAT}/config.yaml",
                                 "attributes": [{"name": "材质", "hint": "不锈钢/陶瓷"},
                                                {"name": "容量", "hint": ""}]})
    assert out["ok"] is True and out["count"] == 2 and out["status"] == "pending_confirm"
    spec = json.loads(_spec_path(ws_tmp).read_text(encoding="utf-8"))
    assert spec["cat"] == CAT and spec["status"] == "pending_confirm"
    by_name = {a["name"]: a for a in spec["attributes"]}
    assert by_name["材质"]["source"] == "manual" and by_name["材质"]["modified"] is True
    assert by_name["材质"]["hint"] == "不锈钢/陶瓷"


def test_save_preserves_provenance_and_marks_modified(server, ws_tmp):
    _mk(ws_tmp)
    _write_spec(ws_tmp, [
        {"name": "材质", "hint": "旧说明", "source": "llm", "modified": False, "runs": 3},
        {"name": "产品类型", "hint": "原始", "source": "llm", "modified": False, "runs": 3},
    ])
    h = _handler(server)
    # 改「材质」的 hint, 删掉「产品类型」, 新增「容量」
    h._api_attr_spec_save({"config_file": f"data/{CAT}/config.yaml",
                           "expected_revision": h._api_attr_spec(_file_q())["revision"],
                           "attributes": [{"name": "材质", "hint": "新说明"},
                                          {"name": "容量", "hint": "30/50/100ml"}]})
    spec = json.loads(_spec_path(ws_tmp).read_text(encoding="utf-8"))
    by_name = {a["name"]: a for a in spec["attributes"]}
    assert set(by_name) == {"材质", "容量"}
    assert by_name["材质"]["source"] == "llm" and by_name["材质"]["runs"] == 3   # 保留出处
    assert by_name["材质"]["modified"] is True                                    # hint 变了
    assert by_name["容量"]["source"] == "manual" and by_name["容量"]["modified"] is True


def test_save_rejects_empty_and_duplicate_names(server, ws_tmp):
    _mk(ws_tmp)
    h = _handler(server)
    with pytest.raises(ValueError):
        h._api_attr_spec_save({"config_file": f"data/{CAT}/config.yaml",
                               "attributes": [{"name": "  ", "hint": "x"}]})
    with pytest.raises(ValueError):
        h._api_attr_spec_save({"config_file": f"data/{CAT}/config.yaml",
                               "attributes": [{"name": "材质"}, {"name": "材质"}]})
    with pytest.raises(ValueError):
        h._api_attr_spec_save({"config_file": f"data/{CAT}/config.yaml",
                               "attributes": "不是列表"})


def test_confirm_sets_confirmed(server, ws_tmp):
    _mk(ws_tmp)
    _write_spec(ws_tmp, [{"name": "材质", "hint": "x", "source": "llm", "modified": False, "runs": 3}])
    h = _handler(server)
    out = h._api_attr_spec_confirm({"config_file": f"data/{CAT}/config.yaml",
                                    "expected_revision": h._api_attr_spec(_file_q())["revision"]})
    assert out["ok"] is True and out["status"] == "confirmed"
    assert json.loads(_spec_path(ws_tmp).read_text(encoding="utf-8"))["status"] == "confirmed"


def test_confirm_requires_existing_and_nonempty(server, ws_tmp):
    _mk(ws_tmp)
    h = _handler(server)
    with pytest.raises(ValueError):
        h._api_attr_spec_confirm({"config_file": f"data/{CAT}/config.yaml"})   # 尚无 spec
    _write_spec(ws_tmp, [])
    with pytest.raises(ValueError):
        h._api_attr_spec_confirm({"config_file": f"data/{CAT}/config.yaml"})   # 空属性集


def test_save_after_confirm_resets_to_pending(server, ws_tmp):
    _mk(ws_tmp)
    _write_spec(ws_tmp, [{"name": "材质", "hint": "x", "source": "llm", "modified": False, "runs": 3}],
                status="confirmed")
    h = _handler(server)
    h._api_attr_spec_save({"config_file": f"data/{CAT}/config.yaml",
                           "expected_revision": h._api_attr_spec(_file_q())["revision"],
                           "attributes": [{"name": "材质", "hint": "改"}]})
    assert json.loads(_spec_path(ws_tmp).read_text(encoding="utf-8"))["status"] == "pending_confirm"


def test_attr_spec_requires_cat(server, ws_tmp):
    # 配置缺 CAT(如根模板占位) → 拒绝(不写到占位目录)
    root = Path(ws_tmp)
    (root / "config.yaml").write_text('CAT: ""\n', encoding="utf-8")
    h = _handler(server)
    with pytest.raises(ValueError):
        h._api_attr_spec({"file": ["config.yaml"]})
    with pytest.raises(ValueError):
        h._api_attr_spec_save({"config_file": "config.yaml", "attributes": [{"name": "x"}]})
