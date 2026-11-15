# -*- coding: utf-8 -*-
import sys, json
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd

detail = pd.read_excel("data/Cutting-Boards/Cutting-Boards-父项变体明细.xlsx", sheet_name="变体明细", dtype=str)
d = detail.copy()
for c in ["父ASIN", "变体ASIN", "来源ASIN"]:
    d[c] = d[c].astype(str).str.strip()

print("== 明细基本信息 ==")
print("行数:", len(d))
# 变体自身被抓? 来源ASIN == 变体ASIN(自身记录) 占比
self_rec = (d["来源ASIN"] == d["变体ASIN"])
print(f"来源ASIN==变体ASIN(变体自身被抓): {int(self_rec.sum())}/{len(d)} ({self_rec.mean()*100:.0f}%)")
print(f"来源ASIN==父ASIN(内容来自父体记录): {int((d['来源ASIN']==d['父ASIN']).sum())}")

print("\n== 标题/五点是否'变体级'(同父体变体间是否一致) ==")
fam = d.groupby("父ASIN")
same_title = same_bullet = 0
multi_t = multi_b = 0
for pa, g in fam:
    if len(g) <= 1:
        continue
    uniq_t = g["标题"].fillna("").nunique()
    uniq_b = g["五点描述"].fillna("").nunique()
    if uniq_t == 1:
        same_title += 1
    else:
        multi_t += 1
    if uniq_b == 1:
        same_bullet += 1
    else:
        multi_b += 1
print(f"多变体父体数: {d['父ASIN'].nunique()} | 标题: 父内变体全一致 {same_title} 个父体, 不一致 {multi_t}")
print(f"                                           五点: 父内变体全一致 {same_bullet} 个父体, 不一致 {multi_b}")

print("\n== 图片链接/主图 ==")
img = d["图片链接"].fillna("")
print(f"图片链接非空: {(img!='').sum()}/{len(d)}")
multi_img = img.str.contains(",|;", regex=True)
print(f"含多图(,或;分隔): {int(multi_img.sum())}")
main = d["主图"].fillna("") if "主图" in d.columns else pd.Series("", index=d.index)
print(f"主图列存在: {'主图' in d.columns}")
if "主图" in d.columns:
    # 主图是否为 图片链接 第一个(按 , ; 切)
    def first_img(x):
        x = str(x)
        for sep in (",", ";"):
            if sep in x:
                return x.split(sep)[0].strip()
        return x
    exp = img.apply(first_img)
    mism = (main != exp) & (img != "")
    print(f"主图≠图片链接首图的行: {int(mism.sum())}")
    print("主图样例:", main.iloc[0][:90])
    print("图片链接样例:", img.iloc[0][:120])

print("\n== 样例: 一个多变体父体(标题/五点/图 各变体对照) ==")
pa = d["父ASIN"].value_counts().index[0] if len(d) else None
sub = d[d["父ASIN"] == pa]
if len(sub):
    print(f"父体 {pa} ({len(sub)} 个变体):")
    for _, r in sub.head(6).iterrows():
        print(f"  变体{r['变体ASIN']} 来源{r['来源ASIN']} | 标题头: {str(r['标题'])[:50]}")
        print(f"    五点头: {str(r['五点描述'])[:60]}")
        print(f"    图: {str(r['图片链接'])[:60]}")
