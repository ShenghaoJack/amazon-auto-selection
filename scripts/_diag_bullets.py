# -*- coding: utf-8 -*-
"""排查: AI已补充表五点列与明细五点列的关系 + 实体残留来源。"""
import sys, os, re
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

ROOT = r"D:\deepseek工作区\auto-workflow-python\data\salt-pepper-shaker-sets"
detail = pd.read_excel(os.path.join(ROOT, "salt-pepper-shaker-sets-父项变体明细.xlsx"), sheet_name="变体明细", dtype=str)
ai = pd.read_excel(os.path.join(ROOT, "14_流量拆分", "流量拆分数据-需人工补充.xlsx"), sheet_name="AI已补充", dtype=str)

d5 = detail.set_index("变体ASIN")["五点描述"].fillna("")
a5 = ai[["变体ASIN", "五点描述"]].copy()
a5["五点描述"] = a5["五点描述"].fillna("")

# 1) 明细五点列的实体残留统计
def entity_hits(s):
    return s.str.contains("&[a-zA-Z]+;", regex=True).sum()

print("== step10 明细 五点列 ==")
print("  非空:", (d5 != "").sum(), "/", len(d5), " 含命名实体字面:", entity_hits(d5))
for i in d5[d5.str.contains("&[a-zA-Z]+;", regex=True)].head(3).items():
    print("  残留样例:", i[1][:180])

print()
print("== AI已补充 五点列 vs 明细 ==")
print("  AI非空:", (a5["五点描述"] != "").sum(), " 含实体:", entity_hits(a5["五点描述"]))
# 逐行对比
match_same = match_diff = only_ai = only_detail = 0
diff_samples = []
for _, r in a5.iterrows():
    va = str(r["变体ASIN"]).strip()
    av = r["五点描述"]
    dv = d5.get(va, "")
    if av != "" and dv != "":
        if av == dv:
            match_same += 1
        else:
            match_diff += 1
            if len(diff_samples) < 3:
                diff_samples.append((va, dv[:120], av[:160]))
    elif av != "" and dv == "":
        only_ai += 1
    elif av == "" and dv != "":
        only_detail += 1
print(f"  两者都非空且相同: {match_same}  不同: {match_diff}  仅AI有: {only_ai}  AI空但明细有: {only_detail}")
for va, dv, av in diff_samples:
    print(f"  DIFF {va}:\n    明细: {dv}\n    AI  : {av}")
print()
# AI 为空但明细非空的 ASIN 样例(疑似被清空)
emptied = a5[(a5["五点描述"] == "") & (a5["变体ASIN"].map(lambda x: d5.get(str(x).strip(), "") != ""))]
print("  AI空但明细非空的行数:", len(emptied))
for va in emptied["变体ASIN"].head(5):
    print("    e.g.", va, "->", d5.get(str(va).strip(), "")[:80])
