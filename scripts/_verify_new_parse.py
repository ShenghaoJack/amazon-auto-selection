# -*- coding: utf-8 -*-
import sys, os, re
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.http_client import parse_product_html

# 重新抓一次页面(本地快速)并解析
from core.config import Config
from core.http_client import HttpFetcher

cfg = Config.load("config.cb.yaml")
http = HttpFetcher(cfg, cookie=str(cfg.get("COOKIE", "") or "") or None)
resp = http.get("https://www.amazon.com/dp/B03A125F64")
html = getattr(resp, "text", "") or ""
print("页面长度:", len(html))
title, bullets = parse_product_html(html)
print("标题:", title[:80])
print(f"五点条数: {len(bullets)}")
for i, b in enumerate(bullets, 1):
    print(f"  [{i}] {b[:130]}")
bsr = [b for b in bullets if re.search(r"#\s?[\d,]+ in |See Top 100", b)]
print("BSR 混入:", len(bsr))
