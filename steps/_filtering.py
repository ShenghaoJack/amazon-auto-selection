"""FR-02.1 统一过滤判定约定(抽象, 类目无关)。

FR-02 生成一份类目过滤提示词; FR-03/FR-10 共用同一份提示词对记录逐条判定。

判定规则(相关词优先, 核心):
① 标题或卖点命中"相关词" -> 保留
② 标题命中"硬排除词" -> 剔除
③ 卖点命中"排除词" -> 剔除
④ 标题命中"配件/耗材词" -> 剔除
⑤ 其余默认保留

容错(2026-09-08 用户选定阶梯方案, 默认 batch_size=10): 批判定健康路径快;
失败/漏判降档重试——整批失败送下一档(集中重发同批 -> 拆 2 条/批 -> 1 条/批),
**批内漏判缺条(JSON 缺 index)不默认保留, 只送缺的那几条下档**;
1 条/批逐条独立重试(默认3次), 多次仍失败按兜底策略(默认保留并标"待复核", 可配剔除)。
batch_size<=1 = 全逐条(显式配置)。
"""
from __future__ import annotations

import random
from typing import Any

from core.catname import display_cat
from core.errors import LLMError
from core.llm import LLMClient


def build_filter_prompt(category: str, requirement: str, five_categories: dict) -> dict:
    """组装类目过滤提示词(FR-02 输出/FR-02.1 结构)。

    category 存入前先清洗为展示名(去掉末尾 -v2/-202609/-US 等后缀, '-' 转空格),
    供 FR-03/FR-10 逐条判定的提示词使用(文件名/目录仍用原始 CAT)。
    产物只含 5 类过滤词 + "产品"(本类目产品定义, 生成侧给出 product_scope 时),
    **不含"达标关键词"字段**——达标关键词 是步骤10 注入最终关键词时才出现的概念
    (见 enrich_prompt_with_keywords); 步骤3 判定用 related_title 兜底。
    """
    product = str(five_categories.get("product_scope") or "").strip()
    related = five_categories.get("related_title", [])
    return {
        "category": display_cat(category),
        "requirement": requirement,
        "产品": product,
        "hard_exclude": five_categories.get("hard_exclude", []),
        "related_title": related,
        "related_bullets": five_categories.get("related_bullets", []),
        "accessories": five_categories.get("accessories", []),
        "consumables": five_categories.get("consumables", []),
        "rules": [
            "① 标题或卖点命中相关词 -> 保留",
            "② 标题命中硬排除词 -> 剔除",
            "③ 卖点命中排除词 -> 剔除",
            "④ 标题命中配件/耗材词 -> 剔除",
            "⑤ 其余默认保留",
        ],
        "output_format": {
            "每条记录输出": {"keep": "true/false", "reason": "原因"},
            "整体输出": {"judgments": "[{index, keep, reason}, ...]"},
        },
    }


def enrich_prompt_with_keywords(filter_prompt: dict | None, keywords: list[str]) -> dict | None:
    """步骤10 用"当前最终关键词(核心+长尾)"增强过滤提示词后再判定。

    背景: FR-02(步骤2)生成提示词时, 关键词体系(步骤4/5/7)尚未产出;
    步骤10 过滤/复核时把它们注入为"达标关键词"(related_title), 并从
    硬排除/配件/耗材等冲突列表移除, 使过滤与最终选词口径一致。
    """
    if not filter_prompt:
        return filter_prompt
    fp = dict(filter_prompt)
    words = [str(k).strip().lower() for k in (keywords or []) if str(k).strip()]
    if not words:
        return fp
    qualify = list(fp.get("达标关键词") or fp.get("related_title") or [])
    added = 0
    for w in words:
        if w not in qualify:
            qualify.append(w)
            added += 1
    fp["达标关键词"] = qualify
    fp["related_title"] = qualify
    for key in ("hard_exclude", "accessories", "consumables"):
        lst = fp.get(key) or []
        new_lst = [x for x in lst if x not in words]
        if len(new_lst) != len(lst):
            fp[key] = new_lst
    return fp


def build_judgment_prompt(filter_prompt: dict, records: list[dict]) -> str:
    """构造单批判定提示词(记录批 + 过滤提示词一次发给 LLM)。"""
    lines = []
    for i, rec in enumerate(records):
        title = str(rec.get("title", "")).strip()
        bullets = rec.get("bullets") or rec.get("五点") or []
        if isinstance(bullets, list):
            bullets_text = " | ".join(str(b) for b in bullets)
        else:
            bullets_text = str(bullets)
        lines.append(f"{i}. 标题: {title}\n   卖点: {bullets_text}")
    record_block = "\n".join(lines)
    fp = filter_prompt or {}
    product = str(fp.get("产品") or "").strip()
    qualify = fp.get("达标关键词") or fp.get("related_title", [])
    # 达标关键词 仅步骤10 注入后存在; 步骤2/3 阶段只有 related_title(相关标题词)
    label = "达标关键词/相关标题词" if fp.get("达标关键词") is not None else "相关标题词"
    prompt = f"""你是亚马逊类目选品过滤判定助手。

类目: {fp.get('category', '')}
{('本类目产品: ' + product) if product else ''}
过滤要求: {fp.get('requirement', '')}

判定参考词:
硬排除词: {fp.get('hard_exclude', [])}
{label}: {qualify}
相关卖点词: {fp.get('related_bullets', [])}
配件词: {fp.get('accessories', [])}
耗材词: {fp.get('consumables', [])}

判定规则(2026-09-08 修订: 先按"本类目产品"定义判整机, 相关词只作参考信号, 不再"命中即保留/其余默认保留"):
1. 先判定是否属于本类目**整机**(严格按"本类目产品"定义; 关键词/达标词命中仅是参考信号, 不能单独作为保留依据)
2. 标题或卖点命中"硬排除词", 或属于非本类目整机(狗用品/碗盘瓶/家用饮水设备(water cooler/boiler/milk can/非宠物 dispenser)/配件耗材/礼品卡等, 参考硬排除词+配件词+耗材词与"本类目产品"定义) -> 剔除
3. 标题命中"配件/耗材词" -> 剔除
4. 其余: 能按定义确认属于本类目整机 -> 保留; **无法确认(拿不准) -> 剔除, 原因注明存疑点**(不再默认保留)
5. 每条必须给出原因(命中的参考信号/排除信号/判定依据)

对下面每条记录判定(保留/剔除+原因):
{record_block}

严格输出 JSON(不要输出任何其他文字):
{{"judgments": [{{"index": 序号, "keep": true/false, "reason": "原因"}}, ...]}}
"""
    return prompt


def parse_judgments(raw: Any, batch_size: int) -> dict[int, tuple[bool, str]]:
    """把 LLM 判定结果解析为 {批次内序号: (keep, reason)}。缺失默认保留。"""
    result: dict[int, tuple[bool, str]] = {}
    if isinstance(raw, dict):
        raw = raw.get("judgments", [])
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < batch_size:
            # 2026-09-13: keep 需显式容错解析 —— 原 bool(item.get("keep", True)) 会把**字符串** "false"
            # 判成 True(应剔除的被保留, 且 reason 还写着剔除原因, 自相矛盾), 把 null 判成 False(应保留
            # 的被误杀)。模型长批判定时把布尔写成字符串/写 null 是常见漂移, 一旦命中即静默污染保留集合
            # 与 <CAT>-filtered-dual.txt / variants.txt 等下游清单。
            raw_keep = item.get("keep", True)
            if raw_keep is None:
                keep = True                      # 缺 key / null 与既有语义一致: 默认保留
            elif isinstance(raw_keep, str):
                keep = raw_keep.strip().lower() not in (
                    "false", "0", "no", "n", "none", "null", "否", "剔除", "不保留",
                )
            else:
                keep = bool(raw_keep)
            reason = str(item.get("reason", "") or "").strip()
            result[idx] = (keep, reason)
    return result


def judge_batch(
    llm: LLMClient,
    filter_prompt: dict,
    records: list[dict],
    batch_size: int = 10,
    log=None,
    max_tries: int = 3,
    fallback_keep: bool = True,
    workers: int = 1,
) -> tuple[list[dict], list[dict]]:
    """执行 LLM 判定, 返回 (保留记录, 剔除记录), 顺序与 records 一致。

    判定执行(2026-09-08 用户选定方案, 默认 batch_size=10):
    * **批判定(健康路径快)**: 正常批(每批 batch_size 条)整批发 LLM;
      任何一档 **漏判缺条(JSON 里缺 index)都不再默认保留**, 而是送下一档重判;
      整批 LLM 失败(超时/服务端错误)同样送下一档。
    * **降档阶梯(只处理失败尾巴)**: 首轮过滤 -> 集中重发(同批大小)
      -> 拆 2 条/批 -> **1 条/批**: 每条记录独立单条 prompt(temp0),
      失败/解析缺失**不默认保留**, 该条独立重试 max_tries 次(默认 3, 间隔 0.5s);
      多次仍失败才走兜底策略: fallback_keep=True(默认) -> 保留并打标
      "判定多次失败, 兜底保留(不误杀), 待复核"(不误杀, 交复核审计);
      fallback_keep=False -> 剔除宁缺毋滥。
    * batch_size<=1(显式配置): 直接从 1 条/批逐条判定(同上逻辑, 无批判定段)。

    并发(2026-09 提速, 仅健康路径): workers>1 时, **首轮过滤/集中重发**两档的
    批内并发(每批仍整批发 LLM, 批间无依赖)。档与档之间**仍串行**——必须等上一档
    跑完收集失败尾巴, 才能进下一档; 拆小/1条/批/最终兜底也保持串行(失败尾巴量小,
    且逐条有 0.5s 节奏控制)。并发批次里单批失败/漏判照样收集进下一档, 兜底不变。

    保留记录附带 reason。
    batch_size: 每批判定条数(默认 10 = 阶梯起点; 1 = 全逐条)。
    max_tries: 1条/批阶段的单条最大重试次数。
    fallback_keep: 单条多次重试仍失败时的兜底方向(True=保留标"待复核" / False=剔除)。
    log: 可选日志函数 fn(text) —— 每批/阶段进度与失败可见。
    workers: 首轮/集中重发两档的并发批数(1=串行; >1 且批数>1 才并发)。
    """
    if not filter_prompt:
        kept = [dict(r, keep=True, reason="过滤提示词缺失, 默认保留") for r in records]
        return kept, []
    total = len(records)
    out: dict[int, dict] = {}

    def _judge_one(gidx: int, tag: str) -> bool:
        """单条独立判定: 重试 max_tries 次; 成功写 out 返回 True, 否则兜底写 out 返回 False。"""
        rec = records[gidx]
        keep: bool = fallback_keep
        reason = ""
        ok_flag = False
        for attempt in range(1, max_tries + 1):
            try:
                prompt = build_judgment_prompt(filter_prompt, [rec])
                raw = llm.chat_json(prompt, temperature=0.0, max_tokens=2000)
                judgment = parse_judgments(raw, 1)
                if 0 in judgment:
                    keep, reason = judgment[0]
                    ok_flag = True
                    break
                reason = f"第{attempt}次解析缺失(judgments 无 index=0)"
            except LLMError as exc:
                reason = str(exc)[:150]
            if attempt < max_tries:
                import time as _time
                _time.sleep(0.5)
        if ok_flag:
            out[gidx] = dict(rec, keep=keep, reason=reason or ("保留" if keep else "剔除"))
            return True
        out[gidx] = dict(
            rec,
            keep=keep,
            reason=(reason or "").rstrip("。") + (
                "；判定多次失败, 兜底保留(不误杀), 待复核" if fallback_keep
                else "；判定多次失败, 兜底剔除(宁缺勿滥)"
            ),
        )
        if log:
            log(f"{tag} {gidx + 1}/{total} FAIL: {out[gidx]['reason']}")
        return False

    # ---- 显式逐条模式(batch_size<=1) ----
    if batch_size <= 1:
        fail_cnt = 0
        for gidx in range(total):
            if not _judge_one(gidx, "逐条判定"):
                fail_cnt += 1
            if log and (gidx + 1) % 50 == 0:
                log(f"逐条判定进度: {gidx + 1}/{total} "
                    f"(保留 {sum(1 for i in range(gidx + 1) if out[i]['keep'])})")
        if log and (fail_cnt or total % 50):
            log(f"逐条判定完成: {total} 条, 重试后仍失败 {fail_cnt} 条"
                f"({('兜底保留待复核' if fallback_keep else '兜底剔除')})")
        kept = [out[i] for i in range(total) if out[i]["keep"]]
        dropped = [out[i] for i in range(total) if not out[i]["keep"]]
        return kept, dropped

    # ---- 阶梯批处理模式(batch_size>1, 2026-09-08 用户方案) ----
    # 档位: 首轮过滤(batch_size) -> 集中重发(同批大小) -> 拆 2 条/批 -> 1 条/批逐条重试
    # 任一档: 整批 LLM 失败送下档; 批内漏判缺条只送缺的那几条下档(不默认保留)
    def _judge_batch_unit(batch_idx: list[int]) -> tuple[dict, str, float]:
        """判一批(整批发 LLM, 温度0)。返回 (judgment, fail_reason, 耗时)。不写 out。"""
        batch = [records[i] for i in batch_idx]
        prompt = build_judgment_prompt(filter_prompt, batch)
        t0 = _now_ts()
        try:
            # 温度0(2026-09): 与关键词抽取一致, 降低"同题两判不一致"(原判定 vs 复核)的假噪声
            raw = llm.chat_json(prompt, temperature=0.0, max_tokens=8000)
            return parse_judgments(raw, len(batch)), "", _now_ts() - t0
        except LLMError as exc:
            return {}, str(exc)[:150], _now_ts() - t0

    def _collect_batch(batch_idx: list[int], judgment: dict, fail_reason: str,
                       elapsed: float, pname: str, bno: int, n_batch: int) -> list[int]:
        """把单批结果写入 out; 返回本批中需送下一档的 gidx 列表(失败/漏判, 不兜底)。"""
        still: list[int] = []
        if fail_reason:
            # 整批失败: 收集到下一档重发(集中重发/拆小), 不兜底
            still.extend(batch_idx)
            if log:
                log(f"{pname}批 {bno}/{n_batch}: LLM 失败({fail_reason}), "
                    f"本批 {len(batch_idx)} 条移至下一档")
            return still
        n_keep = sum(1 for k, v in judgment.items() if v[0])
        n_drop = len(judgment) - n_keep
        missing_n = len(batch_idx) - len(judgment)
        for k, gidx in enumerate(batch_idx):
            if k in judgment:
                keep, reason = judgment[k]
                out[gidx] = dict(records[gidx], keep=keep,
                                 reason=reason or ("保留" if keep else "剔除"))
            else:
                still.append(gidx)   # 漏判缺条送下一档, 不默认保留
        if log:
            extra = f" 漏判 {missing_n} 条送下档(不默认保留)" if missing_n else ""
            log(f"{pname}批 {bno}/{n_batch} 完成: 本批 {len(batch_idx)} 条, "
                f"保留 {n_keep} 剔除 {n_drop}{extra}(耗时 {elapsed:.0f}s)")
        return still

    phases = [("首轮过滤", batch_size), ("集中重发", batch_size), ("拆小(2条/批)", 2)]
    pending = list(range(total))
    for pname, size in phases:
        if not pending:
            break
        still: list[int] = []
        batches = [pending[start:start + size] for start in range(0, len(pending), size)]
        n_batch = len(batches)
        # 并发只作用于 首轮/集中重发 两个批判定档(批间无依赖); 拆小档量小仍串行。
        use_workers = workers if (pname != "拆小(2条/批)" and workers > 1 and n_batch > 1) else 1
        if use_workers > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            ex = ThreadPoolExecutor(max_workers=use_workers)
            try:
                futs = {ex.submit(_judge_batch_unit, b): b for b in batches}
                done = 0
                for fut in as_completed(futs):
                    judgment, fail_reason, elapsed = fut.result()
                    done += 1
                    still.extend(_collect_batch(futs[fut], judgment, fail_reason,
                                                elapsed, pname, done, n_batch))
            except Exception:
                ex.shutdown(wait=False, cancel_futures=True)
                raise
            ex.shutdown(wait=True)
        else:
            for bno, batch_idx in enumerate(batches, 1):
                judgment, fail_reason, elapsed = _judge_batch_unit(batch_idx)
                still.extend(_collect_batch(batch_idx, judgment, fail_reason,
                                            elapsed, pname, bno, n_batch))
        pending = still
        if pending and log:
            who = "首轮" if pname == "首轮过滤" else pname
            log(f"{who}后仍有 {len(pending)} 条待重试, 进入下一档")
    # 最后一档: 1 条/批逐条独立重试(max_tries), 仍失败按 fallback_keep 兜底
    if pending:
        if log:
            log(f"{len(pending)} 条进入 1 条/批逐条重试(每条最多 {max_tries} 次)")
        fail_cnt = 0
        for gidx in pending:
            if not _judge_one(gidx, "逐条重试"):
                fail_cnt += 1
        if log and fail_cnt:
            log(f"逐条重试完成: {len(pending)} 条, 仍失败 {fail_cnt} 条"
                f"({('兜底保留待复核' if fallback_keep else '兜底剔除')})")
    # 防御: 理论上所有记录都已被判定/兜底
    for i in range(total):
        if i not in out:
            out[i] = dict(records[i], keep=fallback_keep,
                          reason="LLM多次重试仍失败, 兜底保留(不误杀), 待复核" if fallback_keep
                                 else "LLM多次重试仍失败, 兜底剔除(宁缺勿滥)")
    kept = [out[i] for i in range(total) if out[i]["keep"]]
    dropped = [out[i] for i in range(total) if not out[i]["keep"]]
    return kept, dropped


def _now_ts() -> float:
    import time as _time
    return _time.time()


def review_filter_results(
    llm: LLMClient,
    market: str,
    records: list[dict],
    filter_prompt: dict | None,
    limit: int = 0,
    seed: int = 42,
    out_path: str | None = None,
    disagree_path: str | None = None,
    log=None,
    batch_size: int = 10,
    workers: int = 1,
) -> dict:
    """对过滤结果**重新判定一遍**, 与原判定逐条对比, 输出不一致项与复核审计 CSV。

    用途: FR-10 步骤完成后自动附带抽查(REVIEW_AUTO)或手动复核脚本共用。
    原理: 用同一份过滤提示词重新走一遍 judge_batch, 两次判定结果不同
          (不一致项)即为误判风险最高的记录, 优先人工复核。

    参数:
      llm: LLMClient(步骤内传 ctx.llm(), dry-run 自动走 Mock)
      market: 亚马逊市场(拼商品链接, 如 amazon.com)
      records: 全部 ok 记录(含原 keep 标志)
      filter_prompt: FR-02 类目过滤提示词
      limit: 0=全量复核; N=抽样 N 条(保留/剔除按比例混合)
      seed: 抽样随机种子(可复现)
      out_path: 复核审计 CSV 输出路径(None=不写文件)
      disagree_path: 不一致项单独清单 CSV 路径(None=不写; 无不一致时不写)
      batch_size: 复核判定每批条数(默认 10 = 阶梯起点, 与 judge_batch 一致)
      workers: 复核判定并发批数(透传 judge_batch; 1=串行)
    返回: {total, sample, agree, disagree, rate, rows, disagree_rows, out_path, disagree_path}
    """
    original = {r.get("request_asin"): bool(r.get("keep", True)) for r in records}
    if limit > 0:
        rng = random.Random(seed)
        sample = rng.sample(records, min(limit, len(records)))
    else:
        sample = records
    if not sample:
        return {
            "total": 0, "sample": 0, "agree": 0, "disagree": 0,
            "rate": 1.0, "rows": [], "disagree_rows": [],
            "out_path": out_path, "disagree_path": disagree_path,
        }
    kept, dropped = judge_batch(llm, filter_prompt, sample, batch_size=batch_size,
                                log=log, workers=workers)
    rows: list[dict] = []
    disagree: list[dict] = []
    for rec in kept + dropped:
        asin = rec.get("request_asin", "")
        new_keep = bool(rec.get("keep", True))
        orig_keep = original.get(asin, True)
        row = {
            "来源标识": asin,
            "标题": rec.get("title", ""),
            "复核判定": "保留" if new_keep else "剔除",
            "原判定": "保留" if orig_keep else "剔除",
            "是否一致": "一致" if new_keep == orig_keep else "**不一致**",
            "原因": rec.get("reason", ""),
            "链接": f"https://{market}/dp/{asin}" if market else "",
        }
        rows.append(row)
        if new_keep != orig_keep:
            disagree.append(row)
    if out_path:
        import csv

        with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["来源标识", "标题", "复核判定", "原判定", "是否一致", "原因", "链接"],
            )
            writer.writeheader()
            writer.writerows(rows)
    if disagree_path and disagree:
        import csv

        with open(disagree_path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["来源标识", "标题", "复核判定", "原判定", "是否一致", "原因", "链接"],
            )
            writer.writeheader()
            writer.writerows(disagree)
    total = len(rows)
    agree = total - len(disagree)
    return {
        "total": total,
        "sample": len(sample),
        "agree": agree,
        "disagree": len(disagree),
        "rate": agree / total if total else 1.0,
        "rows": rows,
        "disagree_rows": disagree,
        "out_path": out_path,
        "disagree_path": disagree_path,
    }


def apply_review_policy(records: list[dict], disagree_rows: list[dict], policy: str) -> int:
    """把复核改判策略应用到不一致记录(就地修改 records 的 keep/reason)。

    policy(小写):
      keep_original    - 保持原判定(只提示不处理, 默认)
      follow_review    - 跟随复核判定(复核保留→保留, 复核剔除→剔除)
      drop_on_disagree - 不一致一律剔除(保守: 宁缺毋滥)

    返回: 实际改判条数。
    """
    policy = (policy or "keep_original").strip().lower()
    if policy == "keep_original" or not disagree_rows:
        return 0
    by_asin = {r.get("request_asin"): r for r in records}
    changed = 0
    for row in disagree_rows:
        rec = by_asin.get(row.get("来源标识"))
        if rec is None:
            continue
        review_keep = row.get("复核判定") == "保留"
        if policy == "follow_review":
            if bool(rec.get("keep", True)) != review_keep:
                rec["keep"] = review_keep
                rec["reason"] = row.get("原因") or rec.get("reason", "")
                changed += 1
        elif policy == "drop_on_disagree":
            if bool(rec.get("keep", True)):
                rec["keep"] = False
                rec["reason"] = (str(rec.get("reason") or "") or "未命中相关词") + "[复核不一致, 策略默认剔除]"
                changed += 1
    return changed
