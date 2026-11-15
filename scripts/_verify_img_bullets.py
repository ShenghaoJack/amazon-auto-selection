# -*- coding: utf-8 -*-
"""临时验证: 图片链接(全图拼接)与五点描述(HTML清洗)是否在产物中更正成功。"""
import sys, json, os

sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

ROOT = r"D:\deepseek工作区\auto-workflow-python\data\salt-pepper-shaker-sets"


def show_sheet(path, sheet, label, img_cols=("图片链接",), bullet_cols=("五点描述",)):
    print("=" * 70)
    print(f"FILE : {os.path.basename(path)}  SHEET: {sheet}  [{label}]")
    if not os.path.exists(path):
        print("  !! 文件不存在:", path)
        return
    df = pd.read_excel(path, sheet_name=sheet, dtype=str)
    cols = list(df.columns)
    print("  列数:", len(cols))
    print("  列名:", " | ".join(cols))
    # 找列
    img_col = next((c for c in img_cols if c in cols), None)
    bul_col = next((c for c in bullet_cols if c in cols), None)
    bad = []
    for c in (img_col, bul_col):
        if c:
            s = df[c].fillna("")
            nonempty = s[s != ""]
            print(f"  列[{c}] 非空 {len(nonempty)}/{len(df)}")
            if len(nonempty):
                sample = nonempty.iloc[0]
                print(f"    样例: {str(sample)[:220]}")
                n_semi = nonempty.str.contains(";").sum()
                n_comma = nonempty.str.contains(", ").sum()
                if c == img_col:
                    print(f"    含';'多图行: {n_semi}   含', '多图行: {n_comma}")
                if c == bul_col:
                    has_html = nonempty.str.contains("<[a-zA-Z/]", regex=True).sum()
                    print(f"    残留HTML标签行: {has_html}")
    print()


def main():
    # 1) 步骤10 明细
    detail = os.path.join(ROOT, "salt-pepper-shaker-sets-父项变体明细.xlsx")
    xl = pd.ExcelFile(detail)
    print("== 父项变体明细 sheets:", xl.sheet_names)
    for sh in xl.sheet_names:
        show_sheet(detail, sh, "step10")

    # 2) 步骤14 输出
    out14 = os.path.join(ROOT, "14_流量拆分", "流量拆分数据-需人工补充.xlsx")
    xl14 = pd.ExcelFile(out14)
    print("== 步骤14 输出 sheets:", xl14.sheet_names)
    for sh in xl14.sheet_names:
        show_sheet(out14, sh, "step14")

    # 3) results.jsonl 源数据: 单条记录结构(images 数量 / bullets 是否含 HTML)
    jl = os.path.join(ROOT, "10_父项变体", "salt-pepper-shaker-sets-results.jsonl")
    if not os.path.exists(jl):
        jl = os.path.join(ROOT, "salt-pepper-shaker-sets-results.jsonl")
    print("=" * 70)
    print("RESULTS.JSONL:", jl, "存在:", os.path.exists(jl))
    if os.path.exists(jl):
        img_counts, bul_html, n = {}, 0, 0
        with open(jl, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                n += 1
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                imgs = r.get("images") or r.get("gallery") or []
                img_counts[len(imgs)] = img_counts.get(len(imgs), 0) + 1
                for b in (r.get("bullets") or []):
                    if "<" in str(b):
                        bul_html += 1
                        break
        print(f"  记录总数: {n}")
        print(f"  单记录图片数分布: {dict(sorted(img_counts.items()))}")
        print(f"  含HTML的bullets记录数: {bul_html}")


if __name__ == "__main__":
    main()
