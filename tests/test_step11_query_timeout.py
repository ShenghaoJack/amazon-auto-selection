# 单元测试: FR-11 查询等待超时解析(QUERY_WAIT_TIMEOUT 与 DOWNLOAD_TIMEOUT 拆分)
#
# 背景(2026-09-11): 原实现"等查询结果"与"等下载文件"共用 DOWNLOAD_TIMEOUT(默认120s),
# 具体历史月份查询(2026-08, 20 个父体)三次 120s 全部超时 → 拆出独立键, 只放宽查询等待。
from steps.step11_traffic_source import _query_wait_timeout


def test_defaults_to_download_timeout():
    assert _query_wait_timeout({}, 120) == 120
    assert _query_wait_timeout({"DOWNLOAD_TIMEOUT": 200}, 200) == 200


def test_explicit_query_wait_wins():
    assert _query_wait_timeout({"QUERY_WAIT_TIMEOUT": 300}, 120) == 300
    assert _query_wait_timeout({"QUERY_WAIT_TIMEOUT": "300"}, 120) == 300


def test_bad_or_zero_falls_back():
    assert _query_wait_timeout({"QUERY_WAIT_TIMEOUT": 0}, 120) == 120
    assert _query_wait_timeout({"QUERY_WAIT_TIMEOUT": ""}, 120) == 120
    assert _query_wait_timeout({"QUERY_WAIT_TIMEOUT": None}, 120) == 120
    assert _query_wait_timeout({"QUERY_WAIT_TIMEOUT": "abc"}, 120) == 120


def test_clamped_to_minimum():
    assert _query_wait_timeout({"QUERY_WAIT_TIMEOUT": 5}, 120) == 10
    assert _query_wait_timeout({"QUERY_WAIT_TIMEOUT": -1}, 120) == 120
