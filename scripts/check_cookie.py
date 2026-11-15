"""COOKIE 有效性检测脚本(命令行入口, 复用 core.cookie_check)。

用法: python -X utf8 scripts/check_cookie.py [ASIN] [--config config.yaml]
退出码: 0=有效 1=未配置/其他 2=失效或网络异常
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.cookie_check import PROBE_ASIN, analyze_page, check_cookie, diagnose_cookie


def main() -> int:
    args = sys.argv[1:]
    asin = args[0] if args and not args[0].startswith("--") else PROBE_ASIN
    cfg_path = "config.yaml"
    if "--config" in args:
        cfg_path = args[args.index("--config") + 1]
    cfg = Config.load(cfg_path)
    cookie = str(cfg.get("COOKIE", "") or "")
    print(f"测试 ASIN: {asin}")
    print(f"市场: {cfg.market}")
    print(f"COOKIE 长度: {len(cookie)} 字符" + (f"(首个键: {cookie.split(';')[0].split('=')[0]})" if cookie else "(未配置)"))
    print("--- 发起请求(可能需几秒) ---")
    result = check_cookie(cfg, asin)
    print(f"检测状态: {result['status']}")
    print(f"结论: {result['message']}")
    if result["status"] == "ok":
        print("[退出码 0] COOKIE 有效 ✅")
        return 0
    if result["status"] == "no_cookie":
        print("[退出码 1] 未配置 COOKIE")
        return 1
    print("[退出码 2] COOKIE 不可用 ❌")
    # 自动定性(2026-09-29): 多打 2~3 个请求, 直接告诉用户"是 COOKIE 失效还是 IP/客户端形态被标记"
    try:
        diag = diagnose_cookie(cfg, result, asin=asin)
        print("--- 自动定性(带/不带 COOKIE + 其它站点对照) ---")
        for p in diag.get("probes") or []:
            print(f"  {p['site']:<14} {'带COOKIE' if p['with_cookie'] else '匿名':<8} "
                  f"class={p['status']:<12} kind={p['kind']:<9} len={p['len']}")
        print(f"定性结论[{diag.get('verdict')}]: {diag.get('message')}")
    except Exception as exc:  # noqa: BLE001 — 定性失败不影响原结论
        print(f"(自动定性不可用: {type(exc).__name__}: {exc})")
    print("      建议: 若定性为 ip_or_client_marked → 换网络/等冷却或改用浏览器兜底(改 COOKIE 无用);"
          " 若为 cookie_suspect/cookie_expired → 重新登录该站点并整条替换 COOKIE")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
