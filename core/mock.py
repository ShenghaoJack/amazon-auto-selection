"""dry-run 模式模拟数据源。

当配置 DRY_RUN=true 时, LLM/MCP/浏览器/HTTP 客户端均使用本模块的模拟实现,
使 0-19 全流程可以在离线环境跑通, 便于逐步骤审查逻辑与产物结构。
"""
from __future__ import annotations

import json
import os
import random
import re
import string
import time
from datetime import datetime, timedelta

import pandas as pd

# 固定随机种子, 保证 dry-run 冒烟结果确定(开品门槛/筛选判定不随运行波动)
random.seed(2026)

_ASIN_POOL: list[str] = []


def _rand_asin() -> str:
    return "B0" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def make_asins(n: int) -> list[str]:
    """生成 n 个稳定的模拟 ASIN(进程内去重)。"""
    global _ASIN_POOL
    while len(_ASIN_POOL) < n:
        a = _rand_asin()
        if a not in _ASIN_POOL:
            _ASIN_POOL.append(a)
    return list(_ASIN_POOL[:n])


def _month_series(month: str, base: float) -> list[dict]:
    year = int(month[:4])
    start = datetime(year, int(month[4:6]), 1) if len(month) == 6 else datetime.strptime(month, "%Y-%m")
    rows = []
    for i in range(11, -1, -1):
        d = start.replace(day=1) - timedelta(days=30 * i)
        idx = 11 - i
        rows.append(
            {
                "month": d.strftime("%Y-%m"),
                "search_volume": int(base * (1 + 0.05 * ((idx % 3) - 1))),
                "purchase_volume": int(base * 0.3),
                "purchase_rate": 0.3,
                "yoy": random.uniform(-10, 15),
            }
        )
    return rows


class MockLLM:
    """模拟 LLM: 根据提示词特征返回各类结构化 JSON。"""

    def chat(self, prompt: str) -> str:
        return json.dumps(self.chat_json(prompt), ensure_ascii=False)

    def chat_json(self, prompt: str):
        p = prompt
        if "对下面每条记录判定" in p:
            indices = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. 标题:", p, re.M)]
            n = max(indices) + 1 if indices else 1
            judgments = []
            for i in range(n):
                if i % 5 == 4:
                    judgments.append({"index": i, "keep": False, "reason": "标题命中硬排除词"})
                else:
                    judgments.append({"index": i, "keep": True, "reason": "命中相关词"})
            return {"judgments": judgments}
        if "过滤" in p and "硬排除" in p:
            return {
                "hard_exclude": ["replacement", "parts only", "refurbished"],
                "related_title": ["pepper grinder", "salt mill", "spice grinder"],
                "related_bullets": ["adjustable grind", "ceramic burr", "stainless steel"],
                "accessories": ["replacement spring", "salt mill parts"],
                "consumables": ["peppercorns", "salt refill"],
            }
        if "候选" in p and "generic" in p:
            words = [
                "salt and pepper grinder set",
                "electric salt grinder",
                "pepper mill for kitchen",
                "stainless steel pepper mill",
                "pepper grinder with ceramic burr",
                "adjustable salt grinder",
                "pepper mill set",
                "pepper grinder set",
                "electric pepper grinder",
                "battery operated grinder",
            ]
            return {"candidates": [{"phrase": w, "role": random.choice(["generic", "functional", "intent"])} for w in words]}
        if "实体抽取" in p:
            # 2026-09-13: FR-04 长尾改为"从标题+五点抽实体再计数", 这里给出候选实体
            # (真实管线中每个候选都会被语料计数验证, 不出现的会被淘汰 -> 自动回落到模板兜底)
            dims = {
                "场景": ["kitchen", "home", "travel", "camping", "outdoor"],
                "人群": ["seniors", "kids", "beginners", "families", "adults"],
                "功能": ["electric", "manual", "adjustable", "portable", "heavy duty"],
                "属性": ["stainless steel", "ceramic", "wooden", "large capacity", "with lid"],
                "痛点": ["easy to clean", "no mess", "quiet", "sturdy", "durable"],
            }
            dim_name = "场景"
            for d in dims:
                if d in p:
                    dim_name = d
                    break
            return {"entities": [{"text": w, "note": "mock"} for w in dims[dim_name]]}
        if "润色" in p:
            # 回显草稿("草稿: xxx" 行), 覆盖"草稿已足够自然 -> 原样返回"这条路径
            drafts = re.findall(r"草稿:\s*(.+?)\s*$", p, re.M)
            return {"longtail": [{"keyword": w} for w in drafts]}
        if "提取" in p and "属性" in p:
            # 2026-09-17: 属性名改**中文**(与真实提示词口径一致: 只给 name + hint, 列名=name)
            return {
                "attributes": [
                    {"name": "产品类型", "hint": "如胡椒研磨器 / 手动研磨器 / 电动研磨器"},
                    {"name": "材质", "hint": "不锈钢 / 陶瓷 / 亚克力 / 玻璃"},
                    {"name": "研磨方式", "hint": "手动 / 电动 / 手动电动两用"},
                    {"name": "供电方式", "hint": "电池 / USB充电 / 手动(无供电)"},
                    {"name": "容量", "hint": "30 ml / 50 ml / 100 ml"},
                    {"name": "功能", "hint": "研磨 / 撒粉 / 研磨+收纳"},
                    {"name": "使用场景", "hint": "厨房 / 餐桌 / 烧烤 / 旅行"},
                ]
            }
        if "筛选脚本" in p:
            return {"script": _SCREEN_TEMPLATE, "description": "通用筛选模板"}
        if "属性" in p and "标注" in p:
            return {
                "产品类型": {"value": "胡椒研磨器", "source": "标题"},
                "材质": {"value": "不锈钢", "source": "卖点"},
                "颜色": {"value": "银色", "source": "卖点"},
                "图片OCR文字": {"value": "Pepper Grinder 120V", "source": "图片"},
            }
        return {"ok": True, "note": "mock response"}

    def vision(self, prompt: str, image_urls: list[str]):
        return {
            "颜色": {"value": "黑色"},
            "材质": {"value": "不锈钢"},
            "ocr_text": {"value": "Pepper Grinder"},
        }


_SCREEN_TEMPLATE = '''\
def screen(row):
    volume = row.get("monthly_search_volume") or 0
    mom = row.get("mom_yoy")
    google = row.get("google_yoy")
    cpc = row.get("cpc")
    if volume < 30000 or mom is None or google is None or cpc is None:
        return ("数据不足", "缺数据")
    if volume < 30000 or mom < -20 or google < -20 or cpc >= 3:
        return ("不达标", "未达筛选标准")
    return ("达标", "全部通过")
'''


class MockMCP:
    """模拟卖家精灵 MCP 三接口。"""

    def keyword_trend(self, keyword: str, month: str) -> dict:
        base = 30000 + (hash(keyword) % 40000)
        rows = _month_series(month, base)
        target = None
        for r in rows:
            if r["month"].startswith(month[:7]):
                target = r
        target = target or rows[-1]
        return {
            "keyword": keyword,
            "month": month,
            "search_volume": target["search_volume"],
            "mom_yoy": 5 + (hash(keyword) % 25),
            "purchase_volume": target["purchase_volume"],
            "purchase_rate": target["purchase_rate"],
            "mom_change": 2 + (hash(keyword) % 18),
            "growth_3m": 1 + (hash(keyword) % 25),
            "monthly_series": rows,
            "status": "ok",
        }

    def aba_weekly_bid(self, keyword: str, month: str) -> dict:
        return {
            "keyword": keyword,
            "week": month + "-last-saturday",
            "cpc": round(0.8 + (hash(keyword) % 100) / 100.0, 2),
            "cpc_min": round(0.5 + (hash(keyword) % 60) / 100.0, 2),
            "cpc_max": round(1.8 + (hash(keyword) % 120) / 100.0, 2),
            "weekly_search_volume": 5000 + (hash(keyword) % 35000),
            "search_rank": 1 + (hash(keyword) % 200),
            "status": "ok",
        }

    def google_trend(self, keyword: str, years: int = 5) -> dict:
        series = {}
        now = datetime.now()
        # i=0 为最近月; 指数随月份递减(近高远低), 保证同比稳定为正, 满足筛选标准
        for i in range(years * 12):
            d = now - timedelta(days=30 * i)
            series[d.strftime("%Y-%m")] = 80 - i
        return {"keyword": keyword, "google_monthly_index": series, "status": "ok"}


class MockHttp:
    """模拟 HTTP 直连详情页。

    标题按 ASIN 哈希从模板池取, 使不同 ASIN 命中不同候选词组,
    保证大词候选覆盖率回测能产生多个候选(供开品门槛通过)。
    """

    _TITLES = [
        "Salt and Pepper Grinder Set Stainless Steel Electric",
        "Electric Salt Grinder with Ceramic Burr",
        "Pepper Mill for Kitchen Stainless Steel",
        "Stainless Steel Pepper Mill with Adjustable Grind",
        "Pepper Grinder with Ceramic Burr Battery Operated",
        "Adjustable Salt Grinder Pepper Mill Set",
    ]

    def get(self, url: str):
        asin = "UNKNOWN"
        m = re.search(r"/dp/([A-Z0-9]{10})", url)
        if m:
            asin = m.group(1)
        title = self._TITLES[hash(asin) % len(self._TITLES)]
        price = 15 + (hash(asin) % 40)
        html = f'''<html><body>
<span id="productTitle">{title}</span>
<span id="priceblock_ourprice">${price}.99</span>
<a id="bylineInfo">MockBrand Store</a>
<div id="feature-bullets">
<ul>
<li><span class="a-list-item"><span>Adjustable coarse to fine grind</span></span></li>
<li><span class="a-list-item"><span>Ceramic burr mechanism</span></span></li>
<li><span class="a-list-item"><span>Battery operated electric grinder</span></span></li>
<li><span class="a-list-item"><span>Easy to clean and refill</span></span></li>
</ul>
</div>
</body></html>'''
        return _FakeResp(html)


class _FakeResp:
    def __init__(self, text: str):
        self.status = 200
        self.text = text
        self.url = ""
        self.ok = True
        self.status_class = "ok"


class MockSellersprite:
    """模拟卖家精灵浏览器页面操作(步骤 11/12/13)。"""

    def __init__(self, ctx):
        self.ctx = ctx

    def check_login(self) -> bool:
        return True

    def login(self, account: str, password: str) -> bool:
        return True

    def reverse_traffic_export(self, asins: list[str], site: str, month: str) -> str:
        """按父体生成模拟流量来源导出(每个变体一行, 便于步骤14流量占比拆分)。"""
        rows = []
        variant_map = self._load_variants()
        for i, parent in enumerate(asins):
            variants = variant_map.get(parent) or [parent]
            for j, va in enumerate(variants):
                rows.append(
                    {
                        "父ASIN": parent,
                        "变体ASIN": va,
                        "流量词": f"pepper grinder {i}-{j}",
                        "流量词数": 10 + j,
                        "月销量": 200 + i * 10,
                        "评分": 4.5,
                    }
                )
        return _write_xlsx(self.ctx.paths.step_dir(11), f"mock-traffic-{time.time()}.xlsx", rows)

    def competitor_submit(self, asins: list[str], site: str) -> dict:
        return {"task_name": f"mock-task-{asins[0][:8]}", "group": 0}

    def export_log_ready(self) -> list[dict]:
        return [{"task": "mock-task-1", "status": "已完成", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}]

    def download_export(self, task_names: list[str], target_dir: str) -> list[str]:
        """生成含真实清单 ASIN 的模拟竞品导出文件, 保证步骤13清单校验保留。"""
        files = []
        variants = self._load_variant_list()
        for name in task_names:
            row_asins = variants[:3] or ["B03B15D79C"]
            rows = []
            for a in row_asins:
                rows.append(
                    {
                        "ASIN": a,
                        "父ASIN": variants[0] if variants else a,
                        "月销量": 300,
                        "评分": 4.5,
                        "商品标题": f"Mock Product {a}",
                        "配送方式": "FBA",
                        "上架时间": "2023-01-15",
                    }
                )
            path = _write_xlsx(target_dir, f"{name}.xlsx", rows)
            files.append(path)
        return files

    def _load_variant_list(self) -> list[str]:
        try:
            cat = self.ctx.config.cat
            path = os.path.join(self.ctx.paths.cat_root, f"{cat}-variants.txt")
            with open(path, encoding="utf-8-sig") as fh:
                return [ln.strip() for ln in fh if ln.strip()]
        except OSError:
            return []

    def _load_variants(self) -> dict:
        """读取 results.jsonl 得到 父ASIN -> [变体ASIN] 映射。"""
        result = {}
        try:
            cat = self.ctx.config.cat
            path = os.path.join(self.ctx.paths.cat_root, f"{cat}-results.jsonl")
            with open(path, encoding="utf-8-sig") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    parent = rec.get("parent_asin") or rec.get("request_asin")
                    variants = rec.get("variant_asins") or []
                    if parent:
                        result.setdefault(parent, []).extend(variants)
        except OSError:
            pass
        return result


def _write_xlsx(directory: str, name: str, rows: list[dict]) -> str:
    import os
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    df = pd.DataFrame(rows)
    df.to_excel(path, index=False)
    return path
