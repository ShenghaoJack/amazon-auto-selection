# -*- coding: utf-8 -*-
"""启动卖家精灵后台 Chrome(CDP 模式): 持久 Profile + 远程调试端口。

用途: 步骤 11/12/13 的卖家精灵浏览器自动化以"CDP 直连"方式复用本 Chrome,
不弹新窗口、会话持久(登录一次永久复用)。启动后保持本 Chrome 运行即可
(可最小化), 配置 config.yaml 的 SS_CDP_URL 指向本端口。

用法:
  python -X utf8 scripts/start_ss_chrome.py [--port 9222] [--url https://www.sellersprite.com/v2/welcome]

首次启动: 在打开的 Chrome 里完成卖家精灵登录(之后会话保存在 ss-profile,
后续启动不再需要登录)。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.browser import find_chrome_path, launch_ss_chrome
from core.config import Config
from core.paths import Paths


def main() -> int:
    ap = argparse.ArgumentParser(description="启动卖家精灵后台 Chrome(CDP 模式)")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument(
        "--config", default="config.yaml",
        help="该类目的配置文件(默认根 config.yaml)。**多类目并行时用它指定类目** —— "
             "profile 按类目隔离, 不同类目各自登录自己的卖家精灵账号",
    )
    ap.add_argument(
        "--url", default="https://www.sellersprite.com/v2/welcome",
        help="启动后打开的页面",
    )
    args = ap.parse_args()

    cfg_path = args.config
    if not os.path.exists(cfg_path):
        print(f"配置文件不存在: {cfg_path}(用 --config 指定, 如 "
              f"--config \"data/<类目>/config.yaml\")")
        return 2
    cfg = Config.load(cfg_path)
    profile = str(Paths(cfg).profile_ss())
    chrome = find_chrome_path()
    print(f"配置: {cfg_path}  (CAT={cfg.cat})")
    print(f"Chrome: {chrome}")
    print(f"Profile: {profile}")
    print(f"CDP 端口: {args.port}")
    ok = launch_ss_chrome(cfg, args.port, args.url)
    if not ok:
        print("启动命令发送失败, 请检查 Chrome 路径或权限")
        return 1
    print("已启动。请完成/确认卖家精灵登录, 保持窗口运行(可最小化)。")
    print("提示: 断点/中断后再运行流水线时, 若本 Chrome 未启动, 系统会自动拉起(无需手动执行本脚本)。")
    print(f"然后在该类目 config.yaml 设置 SS_CDP_URL: http://127.0.0.1:{args.port}  "
          f"(注意用 127.0.0.1 而非 localhost: 调试端口仅绑 IPv4)")
    print("多类目并行: **每个类目用不同端口**(如 A=9222 / B=9223) 并在该 Chrome 里登录"
          "**该类目自己的卖家精灵账号** —— 这样一个类目的导出配额与另一个互不影响, "
          "步骤 0/11/12/13 也不会互相等待。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
