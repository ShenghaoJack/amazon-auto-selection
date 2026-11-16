# -*- coding: utf-8 -*-
import sys, os, re
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.http_client import HttpFetcher

cfg = Config.load("config.cb.yaml")
cookie = str(cfg.get("COOKIE", "") or "") or None
http = HttpFetcher(cfg, cookie=cookie)
resp = http.get("https://www.amazon.com/dp/B03A125F64")
print("状态:", getattr(resp, "status_class", "?"), "| 长度:", len(getattr(resp, "text", "") or ""))
html = getattr(resp, "text", "") or ""

for key in ("About this item", "feature-bullets", "productOverview", "detailBullets", "popped", "productDescription"):
    idxs = [m.start() for m in re.finditer(re.escape(key), html)][:3]
    print(f"\n[{key}] 出现 {len(idxs)} 次 at {idxs}")
    for i in idxs[:1]:
        seg = html[max(0, i - 200): i + 600]
        print("   上下文:", re.sub(r"\s+", " ", seg)[:700])

# 若 feature-bullets 存在, 看其 ul/li 结构
i = html.find("feature-bullets")
if i != -1:
    seg = html[i:i + 2500]
    print("\n== feature-bullets 区域(前 2500 字符, 截断打印) ==")
    print(re.sub(r"\s+", " ", seg)[:2200])
