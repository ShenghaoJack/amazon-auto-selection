# -*- coding: utf-8 -*-
import sys, json, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd

rows = []
for line in open("data/Cutting-Boards/Cutting-Boards-results.jsonl", encoding="utf-8-sig"):
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    if not r.get("ok"):
        continue
    rows.append(
        {
            "request_asin": r.get("request_asin"),
            "parent_asin": r.get("parent_asin") or "",
            "变体数(页面)": len(r.get("variant_asins") or []),
            "标题": r.get("title") or "",
            "五点条数": len(r.get("bullets") or []),
            "五点描述": " | ".join(str(b) for b in (r.get("bullets") or [])),
            "图片数": len(r.get("images") or []),
            "图片链接": "; ".join(str(u) for u in (r.get("images") or [])),
            "抓取时间": r.get("fetched_at") or "",
        }
    )
df = pd.DataFrame(rows)
out = "data/Cutting-Boards/步骤10重抓-已抓取预览.xlsx"
with pd.ExcelWriter(out, engine="openpyxl") as w:
    df.to_excel(w, sheet_name="已抓取", index=False)
print("已生成:", os.path.abspath(out))
print("行数:", len(df), "| 列:", list(df.columns))
