# 单元测试: CDP Chrome 自动启动(断点/中断后再运行, 未启动自动拉起)
from unittest import mock

from core import browser as br
from core.browser import cdp_port_from_url, ensure_ss_chrome, _cdp_alive


class _Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_cdp_port_from_url():
    assert cdp_port_from_url("http://127.0.0.1:9222") == 9222
    assert cdp_port_from_url("http://localhost:9223/") == 9223
    assert cdp_port_from_url("") == 9222
    assert cdp_port_from_url("http://127.0.0.1") == 9222  # 无端口缺省


def test_ensure_no_cdp_url_noop():
    """未配置 SS_CDP_URL(有头兜底模式): 返回 True, 不尝试启动。"""
    with mock.patch.object(br, "launch_ss_chrome") as launch:
        assert ensure_ss_chrome(_Cfg()) is True
        launch.assert_not_called()


def test_ensure_alive_no_launch():
    """CDP 已就绪: 直接 True, 不重复启动。"""
    cfg = _Cfg(SS_CDP_URL="http://127.0.0.1:9222")
    with mock.patch.object(br, "_cdp_alive", return_value=True) as alive, \
         mock.patch.object(br, "launch_ss_chrome") as launch:
        assert ensure_ss_chrome(cfg) is True
        launch.assert_not_called()
        assert alive.call_count >= 1


def test_ensure_launches_when_dead():
    """CDP 未启动: 自动拉起 Chrome, 等端口就绪后返回 True。"""
    cfg = _Cfg(SS_CDP_URL="http://127.0.0.1:9222")
    alive_states = iter([False, False, True])  # 第1次探测 dead → 启动 → 第3次就绪

    def fake_alive(url, timeout=2.0):
        return next(alive_states, True)

    with mock.patch.object(br, "_cdp_alive", side_effect=fake_alive), \
         mock.patch.object(br, "launch_ss_chrome", return_value=True) as launch, \
         mock.patch("time.sleep"):
        assert ensure_ss_chrome(cfg) is True
        launch.assert_called_once()


def test_ensure_timeout_returns_false():
    """自动启动后一直未就绪(如同 Profile 被占用): 超时返回 False(调用方明确报错)。"""
    cfg = _Cfg(SS_CDP_URL="http://127.0.0.1:9222")
    with mock.patch.object(br, "_cdp_alive", return_value=False), \
         mock.patch.object(br, "launch_ss_chrome", return_value=True) as launch, \
         mock.patch("time.sleep"):
        assert ensure_ss_chrome(cfg, timeout=3.0) is False
        launch.assert_called_once()


def test_cdp_alive_false_when_no_server():
    """探测到未监听端口应快速返回 False(不抛异常)。"""
    assert _cdp_alive("http://127.0.0.1:1") is False
