# 单元测试: WebUI 构建版本握手(2026-10-08)
#
# 机制: `webui/server.py::WEBUI_VERSION` 由 `/api/bootstrap` 返给前端, 前端拿它跟
# `webui/index.html::WUI_EXPECT` 比对 —— 不一致就标红并弹"旧构建, 请重启服务"。
# 所以**改 server.py / index.html 后必须两处一起 bump**, 只改一处会让页面**每次加载**
# 都误报"请重启服务"(或反过来: 改了服务端却忘了 bump, 用户永远得不到提示)。
# 这条用例把两个常量锁在一起。
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _server_version() -> str:
    spec = importlib.util.spec_from_file_location("_wui_srv_ver", ROOT / "webui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.WEBUI_VERSION)


def test_webui_version_matches_index_expect():
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'const\s+WUI_EXPECT\s*=\s*"([^"]+)"', html)
    assert m, "index.html 里找不到 WUI_EXPECT"
    assert m.group(1) == _server_version(), (
        f"版本号不一致: server.py WEBUI_VERSION={_server_version()!r} "
        f"vs index.html WUI_EXPECT={m.group(1)!r} —— 必须两处一起 bump"
    )


def test_version_looks_like_a_date_tag():
    """版本号是 `YYYY-MM-DD.NN` 形态(便于一眼看出构建日期)。"""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d{2}", _server_version()), _server_version()
