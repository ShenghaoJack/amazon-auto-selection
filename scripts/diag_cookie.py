# -*- coding: utf-8 -*-
"""诊断: 抓取检测页, 打印关键标记命中情况。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.http_client import HttpFetcher, classify_failure
from core.cookie_check import analyze_page

cfg = Config.load("config.yaml")
fetcher = HttpFetcher(cfg, cookie=str(cfg.get("COOKIE", "")) or None)
url = "https://amazon.com/dp/B03A9C46E9"
resp = fetcher.get(url, retries=1)
low = resp.text.lower()

print("HTTP 状态:", resp.status)
print("status_class:", resp.status_class)
print("页面大小:", len(resp.text))
print()
markers = {
    "productTitle 出现次数": low.count("producttitle"),
    'id="productTitle"': 'id="producttitle"' in low,
    "robot 标记(sorry...)": "sorry, we just need to make sure you're not a robot" in low,
    "captcha": "captcha" in low,
    "robot check": "robot check" in low,
    "something went wrong": "something went wrong" in low,
    "we encountered an error": "we encountered an error" in low,
    "api-gateway": "api-gateway" in low,
    "dogs of amazon": "dogs of amazon" in low,
}
for k, v in markers.items():
    print(f"  {k}: {v}")
print()
print("classify_failure 结果:", classify_failure(resp.status, resp.text))
print("analyze_page 结果:", analyze_page(resp.text))
print()
print("页面前 500 字符:")
print(resp.text[:500])
