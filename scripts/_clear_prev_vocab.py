# -*- coding: utf-8 -*-
"""清空「上一轮」留下的受控值词表，让步骤18 用本轮全量数据重新聚类。

清三处：
  ① rules.json  attributes[].params.values   ← 步骤18 归并标准化真正使用的词表
  ② rules.json  detail.vocabularies          ← 旧 schema 遗留的正则词表（代码无读者）
  ③ ledger.json current_controlled_values / deprecated_values ← 上一轮的词表快照
     （revisions 历史保留 —— 那是台账的意义；若要连历史一起删，删整个文件即可）

保留：top_keys / allow_keys / rule / type / 其余全部字段。
"""
from __future__ import annotations

import json
import os
import shutil

BASE = os.path.join("data", "Salt and Pepper Mill Sets-US-202608")
RULES = os.path.join(BASE, "salt_pepper_mill.rules.json")
LEDGER = os.path.join(BASE, "salt_pepper_mill.ledger.json")

shutil.copy2(RULES, RULES + ".bak-before-vocab-reset")
shutil.copy2(LEDGER, LEDGER + ".bak-before-vocab-reset")
print("已备份 rules.json / ledger.json -> *.bak-before-vocab-reset")

# ---------- ① + ②
d = json.load(open(RULES, encoding="utf-8"))
cleared = []
for a in d.get("attributes") or []:
    p = a.get("params")
    if not isinstance(p, dict):
        continue
    v = p.get("values") or []
    if v:
        cleared.append((a.get("name"), len(v)))
        p["values"] = []
# detail.vocabularies：旧 schema 遗留（material/color/scene/... 带正则），代码里无读者
det = d.get("detail")
if isinstance(det, dict) and isinstance(det.get("vocabularies"), dict):
    old = {k: (len(x) if hasattr(x, "__len__") else 1)
           for k, x in det["vocabularies"].items() if k != "_说明"}
    det["vocabularies"] = {"_说明": "2026-09-29 已清空：旧 schema 遗留正则词表，代码无读者；"
                                    "本轮受控值由步骤18 从当前全量数据重新聚类生成。原内容见 "
                                    "salt_pepper_mill.rules.json.bak-before-vocab-reset"}
    print("② detail.vocabularies 已清空（原 %d 组：%s）" % (len(old), old))
json.dump(d, open(RULES, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("① attributes[].params.values 已清空 %d 个属性：" % len(cleared))
for n, k in cleared:
    print("     %-14s 原 %d 个值" % (n, k))

# ---------- ③
lg = json.load(open(LEDGER, encoding="utf-8"))
for k in ("current_controlled_values", "deprecated_values"):
    if isinstance(lg.get(k), dict) and lg[k]:
        print("③ ledger.%s 清空（原 %d 个属性）" % (k, len(lg[k])))
        lg[k] = {}
lg["_清空说明"] = ("2026-09-29 按用户要求清空 current_controlled_values / deprecated_values："
                  "那是上一轮（含旧 schema 英文 slug）的词表快照。revisions 历史保留。"
                  "原内容见 salt_pepper_mill.ledger.json.bak-before-vocab-reset")
json.dump(lg, open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# ---------- 复核
d2 = json.load(open(RULES, encoding="utf-8"))
left = [(a["name"], len((a.get("params") or {}).get("values") or []))
        for a in d2["attributes"]]
print("\n复核 rules.json：仍有词表的属性 =", [x for x in left if x[1]] or "无 ✅")
print("   top_keys =", d2["top_keys"], " allow_keys =", d2["allow_keys"])
lg2 = json.load(open(LEDGER, encoding="utf-8"))
print("复核 ledger：current_controlled_values=%d 项, deprecated=%d 项, revisions=%d 条"
      % (len(lg2["current_controlled_values"]), len(lg2["deprecated_values"]),
         len(lg2.get("revisions") or [])))
