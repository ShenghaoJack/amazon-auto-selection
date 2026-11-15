# -*- coding: utf-8 -*-
"""全量重跑「产品类型 → 适用颗粒尺寸（mm）」31 列：每行一次调用（文本 + 图片）。
结果落 JSON，供复核后再写回 xlsx。

用法: python scripts/_reannotate_31cols.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import openpyxl

sys.path.insert(0, os.getcwd())
from core.config import Config   # noqa: E402
from core.llm import LLMClient   # noqa: E402

DESK = r"C:\Users\Jack_New\Desktop\亚马逊选品类目分类\类目分组\Automatic Cat Feeders"
FN = "Automatic Cat Feeders-最终销量拆分结果-产品属性提炼版-单位统一_已填充About.xlsx"
P = os.path.join(DESK, FN)
CFG = os.path.join("data", "Air Purifiers for Home Pets-US-202608", "config.yaml")
OUT = os.path.join("scripts", "_reannotate_result_all.json")
I0, I1 = "产品类型", "适用颗粒尺寸（mm）"
BIN = ["APP远程控制", "摄像头", "夜视", "双向语音", "移动/声音提醒", "语音录制/餐食呼唤",
       "定时定量", "慢食模式", "保鲜密封", "干燥剂盒/防潮", "防卡粮/防堵", "低粮提醒",
       "堵粮检测", "备用供电", "可拆洗/易清洁", "湿粮适用", "冷藏/冰袋保鲜",
       "宠物识别", "多宠管理"]

PROMPT_HEAD = """你是宠物喂食器类目的属性标注员。下面是**同一行商品**的资料：商品标题、详细参数、产品卖点（可能被截断），以及该变体的商品图片（若干张，按顺序编号）。

请依据这些证据，重新判定下列属性。**铁律**：

1. **只填有明确证据的**（文本明写 或 图片上能看清/读到）。没有证据一律填 `"未明确"`。
   —— 绝不要把「文本没提到」推断成 `"否"`：本表的二值属性只有 `"是"` 和 `"未明确"` 两种取值。
2. 图片证据优先看：规格参数面板、卖点图、包装图上的文字。
3. 数值类只填数字（不要单位、不要范围符号之外的字符）；读不到填 `"未明确"`。
4. 输出**严格 JSON**，键名照抄，不要增删键，不要解释。

各属性的取值要求：
%s

输出格式：
{"%s": "<值>", "依据": "<本条判定的证据出处，40字内，例如 '图3规格面板: 1-10 Meals Per Day；标题: 2.4GHz WiFi'>"}
"""
SPEC = """- 产品类型：程序定时干粮喂食器 / 联网智能干粮喂食器 / 重力式食水套装 / 摄像智能喂食器 / 湿粮/冷藏喂食器 / 宠物识别喂食器 / 重力式喂食器（有 WiFi/App 一律归「联网智能干粮喂食器」；WiFi 喂食器+重力水碗二合一也归联网智能）
- 喂食机制：定量出粮 / 重力式 / 识别门控 / 旋转餐盘
- 容量（L）：数字（加仑→×3.78541，杯→×0.236588；食水套装只取**食物**容量）
- 每日最大餐数（餐/天）：整数（`1-10 Meals Per Day` 取 10）
- 每餐份数范围（份/餐）：整数（取范围最大值；`≤50` 取 50）
- 单份克重（g/份）：数字（`8~10g per portion` 取 10）
- 联网方式（GHz）：Wi-Fi 2.4 GHz / Wi-Fi 5 GHz / Wi-Fi 2.4/5 GHz / Wi-Fi（频段未明确）/ 未明确（**蓝牙不算联网方式**；本列请照抄这里的写法，含连字符）
- APP远程控制 / 摄像头 / 夜视 / 双向语音 / 移动/声音提醒 / 语音录制/餐食呼唤 / 定时定量 / 慢食模式 / 保鲜密封 / 干燥剂盒/防潮 / 防卡粮/防堵 / 低粮提醒 / 堵粮检测 / 备用供电 / 可拆洗/易清洁 / 湿粮适用 / 冷藏/冰袋保鲜 / 宠物识别 / 多宠管理：是 / 未明确
- 摄像头分辨率（原标称）：1080P / 2K / 4K / 3MP / 未明确
- 语音录制时长（秒）：数字
- 食碗材质：不锈钢 / 塑料 / 未明确
- 食碗/宠物位数量：单碗 / 双碗/双宠位 / 未明确
- 适用颗粒尺寸（mm）：最小–最大（如 2–15）/ 未明确"""


def build_prompt(keys):
    return PROMPT_HEAD % (SPEC, '": "…", "'.join(keys))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    wb = openpyxl.load_workbook(P, read_only=True, data_only=True)
    ws = wb["Sheet1"]
    it = ws.iter_rows(values_only=True)
    hdr = [str(x) for x in next(it)]
    data = [(i + 2, r) for i, r in enumerate(it)]
    wb.close()
    C = {h: i for i, h in enumerate(hdr)}
    i0, i1 = hdr.index(I0), hdr.index(I1)
    KEYS = hdr[i0:i1 + 1]
    assert set(BIN) <= set(KEYS), "二值列缺失: %s" % (set(BIN) - set(KEYS))
    print("属性列 %d 个；行 %d；并发 %d" % (len(KEYS), len(data), args.workers))

    def txt(r, k, cap=900):
        v = r[C[k]] if k in C else None
        s = "" if v is None else str(v)
        return s[:cap]

    def urls(r):
        out = []
        for col in ("图片链接", "图片证据URL"):
            if col in C:
                for u in ("" if r[C[col]] is None else str(r[C[col]])).replace(" | ", ",").split(","):
                    u = u.strip()
                    if u.startswith("http") and u not in out:
                        out.append(u)
        return out          # 全部图片，不设上限（2026-09-30 修正：此前误设为 6 张）

    targets = data if not args.limit else data[:args.limit]
    cfg = Config.load(CFG)
    llm = LLMClient(cfg)
    prompt = build_prompt(KEYS)
    t0 = time.time()

    def one(item):
        n, r = item
        body = ("【商品标题】%s\n【详细参数】%s\n【产品卖点】%s"
                % (txt(r, "商品标题", 300), txt(r, "详细参数", 700), txt(r, "产品卖点", 900)))
        try:
            out = llm.vision(prompt + "\n" + body, urls(r), temperature=0.1,
                             max_tokens=2000, detail="low", max_images=None)
            if isinstance(out, dict) and isinstance(out.get("response"), dict):
                out = out["response"]
        except Exception as exc:  # noqa: BLE001
            return {"row": n, "asin": str(r[C["变体 ASIN"]]).strip(), "error": str(exc)[:120]}
        got = {k: ("" if (out or {}).get(k) is None else str((out or {}).get(k)).strip())
               for k in KEYS}
        # 归一：模型常写 ASCII 连字符 Wi-Fi，本表用 U+2011 Wi‑Fi；不归一会产生第 5 种取值
        net = got.get("联网方式（GHz）", "")
        if net:
            got["联网方式（GHz）"] = (net.replace("Wi-Fi", "Wi‑Fi").replace("WiFi", "Wi‑Fi")
                                     .replace("wi-fi", "Wi‑Fi"))
        return {"row": n, "asin": str(r[C["变体 ASIN"]]).strip(), "new": got,
                "why": str((out or {}).get("依据") or "")[:200],
                "old": {k: ("" if r[C[k]] is None else str(r[C[k]]).strip()) for k in KEYS}}

    res = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(one, it_): it_[0] for it_ in targets}
        for k, f in enumerate(as_completed(futs), 1):
            res.append(f.result())
            if k % 10 == 0 or k == len(targets):
                print("  %d/%d  用时 %.0fs" % (k, len(targets), time.time() - t0), flush=True)
    res.sort(key=lambda x: x["row"])
    json.dump({"keys": KEYS, "n": len(res), "results": res},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ---- 统计：新填 / 改动
    fill = chg = err = 0
    per = {k: 0 for k in KEYS}
    for x in res:
        if x.get("error"):
            err += 1
            continue
        for k in KEYS:
            o, nw = x["old"].get(k, ""), x["new"].get(k, "")
            if o in ("", "未明确") and nw and nw != "未明确":
                fill += 1
                per[k] += 1
            elif o and o != "未明确" and nw and nw != "未明确" and o != nw:
                chg += 1
    print("\n新填格 = %d   与现值冲突 = %d   调用失败 = %d" % (fill, chg, err))
    print("按列新填数（前 20）:", dict(sorted(per.items(), key=lambda x: -x[1])[:20]))
    try:
        print("用量:", json.dumps(llm.meter().totals(), ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass
    print("结果已写 ->", OUT)


if __name__ == "__main__":
    main()
