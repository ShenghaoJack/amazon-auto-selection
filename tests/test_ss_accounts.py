# 单元测试: 卖家精灵多账号配额协调(步骤11/12 导出配额耗尽自动换号)
from datetime import datetime

import pytest

from steps._ss_accounts import (
    account_plan,
    backup_accounts,
    hint_matches,
    parse_quota,
    window_open,
)


def test_parse_quota_variants():
    assert parse_quota("支持快速导出前2000条,也可以自定义勾选ASIN导出(当天剩余0次)说明:闲时可导出前3000条") == 0
    assert parse_quota("当天剩余 12 次") == 12
    assert parse_quota("今日剩余5次") == 5
    assert parse_quota("剩余100次") == 100
    assert parse_quota("可导出前3000条") == 3000
    assert parse_quota("无任何配额文案") is None
    assert parse_quota("") is None
    assert parse_quota(None) is None


def test_hint_matches():
    assert hint_matches("支持快速导出前2000条…当天剩余0次…闲时可导出前3000条")
    assert hint_matches("今天可导出前 3000 条")
    assert not hint_matches("普通提示文字")
    assert not hint_matches("")


def test_window_open_boundaries():
    # 默认窗口 20:00 ~ 次日 08:00
    assert window_open(None, datetime(2026, 9, 2, 19, 59)) is False
    assert window_open(None, datetime(2026, 9, 2, 20, 0)) is True
    assert window_open(None, datetime(2026, 9, 2, 23, 30)) is True
    assert window_open(None, datetime(2026, 9, 3, 0, 30)) is True   # 跨天凌晨仍在窗口
    assert window_open(None, datetime(2026, 9, 3, 7, 59)) is True
    assert window_open(None, datetime(2026, 9, 3, 8, 0)) is False   # 8 点窗口关闭
    assert window_open(None, datetime(2026, 9, 3, 12, 0)) is False


def test_window_open_same_start_end_all_day():
    """起止相同(00:00/00:00) = 无窗口限制, 备用账号全天可用。"""
    cfg = _Cfg(SS_QUOTA_WINDOW_START="00:00", SS_QUOTA_WINDOW_END="00:00")
    for h in (0, 6, 12, 18, 23):
        assert window_open(cfg, datetime(2026, 9, 2, h, 30)) is True, f"{h}:30 应全天可用"
    # 也可用其它相同值表达全天
    cfg2 = _Cfg(SS_QUOTA_WINDOW_START="20:00", SS_QUOTA_WINDOW_END="20:00")
    assert window_open(cfg2, datetime(2026, 9, 2, 10, 0)) is True


def test_window_open_custom_same_day_range():
    """同日窗口(如 08:00~20:00)按 [start, end) 判断。"""
    cfg = _Cfg(SS_QUOTA_WINDOW_START="08:00", SS_QUOTA_WINDOW_END="20:00")
    assert window_open(cfg, datetime(2026, 9, 2, 7, 59)) is False
    assert window_open(cfg, datetime(2026, 9, 2, 8, 0)) is True
    assert window_open(cfg, datetime(2026, 9, 2, 19, 59)) is True
    assert window_open(cfg, datetime(2026, 9, 2, 20, 0)) is False


class _Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_backup_accounts_dicts_and_defaults():
    cfg = _Cfg(
        SS_ACCOUNTS=[
            {"account": "b1@x.com", "password": "p1", "note": "备用甲"},
            {"account": "b2@x.com", "password": "p2"},
        ]
    )
    bks = backup_accounts(cfg)
    assert len(bks) == 2
    assert bks[0]["account"] == "b1@x.com" and bks[0]["index"] == 1
    assert bks[0]["note"] == "备用甲"
    assert bks[1]["index"] == 2 and bks[1]["note"] == "备用账号2"  # note 缺省


def test_backup_accounts_none_and_empty():
    assert backup_accounts(_Cfg()) == []
    assert backup_accounts(_Cfg(SS_ACCOUNTS=[])) == []
    assert backup_accounts(_Cfg(SS_ACCOUNTS=[{"account": "  ", "password": "p"}])) == []


def test_backup_accounts_string_form():
    cfg = _Cfg(SS_ACCOUNTS="b1@x.com:p1")
    bks = backup_accounts(cfg)
    assert len(bks) == 1 and bks[0]["account"] == "b1@x.com" and bks[0]["password"] == "p1"


def test_quota_exhausted_is_exception():
    from steps._ss_accounts import QuotaExhausted

    with pytest.raises(QuotaExhausted) as ei:
        raise QuotaExhausted("当天剩余0次")
    assert "当天剩余0次" in str(ei.value)


def test_account_plan_with_main_flag():
    """统一账号列表: is_main:true 为主(随时), 其余为备用(窗口后); 备用序号独立。"""
    cfg = _Cfg(
        SS_ACCOUNTS=[
            {"account": "main@x.com", "password": "mp", "note": "主", "is_main": True},
            {"account": "b1@x.com", "password": "p1", "note": "备用1"},
            {"account": "b2@x.com", "password": "p2", "is_main": False},
        ]
    )
    plan = account_plan(cfg)
    assert [a["kind"] for a in plan] == ["main", "backup", "backup"]
    assert plan[0]["account"] == "main@x.com"
    assert plan[1]["index"] == 1 and plan[1]["account"] == "b1@x.com"
    assert plan[2]["index"] == 2 and plan[2]["account"] == "b2@x.com"
    # backup_accounts 只返回非主
    bks = backup_accounts(cfg)
    assert [b["account"] for b in bks] == ["b1@x.com", "b2@x.com"]


def test_account_plan_fallback_to_ss_account():
    """无 is_main 主项时回退旧字段 SS_ACCOUNT 为主账号, 列表全部为备用。"""
    cfg = _Cfg(
        SS_ACCOUNT="legacy@x.com",
        SS_PASSWORD="lp",
        SS_ACCOUNTS=[{"account": "b1@x.com", "password": "p1"}],
    )
    plan = account_plan(cfg)
    assert plan[0]["kind"] == "main" and plan[0]["account"] == "legacy@x.com"
    assert plan[1]["kind"] == "backup" and plan[1]["account"] == "b1@x.com"


def test_account_plan_no_main_anywhere():
    """SS_ACCOUNTS 含 is_main 主项时不依赖旧字段; 全空时无账号。"""
    cfg = _Cfg(SS_ACCOUNTS=[])
    assert account_plan(cfg) == []
    cfg2 = _Cfg(SS_ACCOUNT="m@x.com", SS_PASSWORD="mp", SS_ACCOUNTS=[])
    assert [a["kind"] for a in account_plan(cfg2)] == ["main"]
    cfg3 = _Cfg(
        SS_ACCOUNT="ignored@x.com",
        SS_ACCOUNTS=[{"account": "m2@x.com", "password": "mp", "is_main": True}],
    )
    plan = account_plan(cfg3)
    assert plan[0]["account"] == "m2@x.com"  # is_main 主项优先于旧字段


def test_account_plan_active_override():
    """SS_ACTIVE_ACCOUNT 手动指定起始账号: 提到首位; 配额0仍自动换号(顺序保留在后)。"""
    base = _Cfg(
        SS_ACCOUNTS=[
            {"account": "b1@x.com", "password": "p1"},
            {"account": "b2@x.com", "password": "p2"},
        ],
        SS_ACCOUNT="main@x.com",
        SS_PASSWORD="mp",
    )
    # auto/空: 主优先
    assert account_plan(_Cfg(SS_ACTIVE_ACCOUNT="auto", **base))[0]["account"] == "main@x.com"
    # 指定 backup1: 备用1 优先, 主账号仍保留在计划中(耗尽后自动换)
    plan = account_plan(_Cfg(SS_ACTIVE_ACCOUNT="backup1", **base))
    assert [a["account"] for a in plan] == ["b1@x.com", "main@x.com", "b2@x.com"]
    # 指定账号字符串(忽略大小写)
    plan = account_plan(_Cfg(SS_ACTIVE_ACCOUNT="B2@X.COM", **base))
    assert plan[0]["account"] == "b2@x.com"
    # 指定 main
    plan = account_plan(_Cfg(SS_ACTIVE_ACCOUNT="main", **base))
    assert plan[0]["account"] == "main@x.com"
    # 未知指定: 保持默认顺序
    plan = account_plan(_Cfg(SS_ACTIVE_ACCOUNT="nobody@x.com", **base))
    assert plan[0]["account"] == "main@x.com"
