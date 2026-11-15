# -*- coding: utf-8 -*-
"""批量「文本 LLM」抽取的马达(2026-09-24)。

为什么单独抽出来
================
`厚度` / `主体材质` / `产品类型` / `套装数量` / `尺寸规格` 这几列的问题都是同一类:
**步骤17 是"按属性说明整表跑"的, 个别列的取值口径不对或漏读**, 需要按列重新用
「文字 + 图片视觉」过一遍。而"文字"这一层用**批量文本 LLM** 做, 比正则穷举可靠得多
(实测 `0.8-inch profile`、`each board is 0.8 inch thick` 这类写法靠正则越补越漏)。

本模块只负责: 切批 → 并发调 `chat_json` → 解析 → 失败隔离 + 断点续跑。
**怎么问、怎么校验**由调用方给(`build_prompt` / `parse` / `accept`)。

关键设计
========
· **逐批解析**: 一批里某几行缺失只丢那几行, 不炸整批;
· **同批重试**: 整批失败(超时/限流/解析不了) → 同批重发一次, 再失败按 2 行/批拆;
· **引用可核**(`accept` 回调): 要求模型给出**原文片段**, 调用方用 `quote_in_text` 核验
  它确实出现在该行资料里 —— 把"模型幻觉"挡在门外, 这也是敢让文本层优先于结构化尺寸的底气。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

__all__ = ["run_batches", "normalize_for_match", "quote_in_text"]

_WS = re.compile(r'\s+')
# 全角标点 → 半角(引用核对时两边都归一, 免得模型把 `,` 抄成 `，` 就判不中)
_PUNCT = str.maketrans({
    "，": ",", "。": ".", "：": ":", "；": ";", "（": "(", "）": ")",
    "“": '"', "”": '"', "‘": "'", "’": "'", "、": ",", "－": "-", "—": "-",
    "–": "-", "\u00a0": " ",
})


def normalize_for_match(s: Any) -> str:
    """归一化文本用于**引用核对**: 小写 + 全角标点转半角 + 空白压缩。"""
    t = str(s or "").translate(_PUNCT).lower()
    return _WS.sub(" ", t).strip()


def quote_in_text(quote: Any, texts: Iterable[Any], min_len: int = 6) -> bool:
    """模型给的 `原文` 是否真的出现在该行资料里(用于挡幻觉)。

    · 太短(<`min_len`)的片段不算 —— 否则 `1mm` 这种随手就能凑出来, 起不到核验作用;
    · 两边都做 `normalize_for_match`, 只比字面, 不做模糊匹配。
    """
    q = normalize_for_match(quote)
    if len(q) < min_len:
        return False
    for t in texts:
        if q in normalize_for_match(t):
            return True
    return False


def _extract_rows(raw: Any) -> list[dict]:
    """从模型返回里取出行列表(容忍 {"rows":[…]} / {"结果":[…]} / 直接是 list)。"""
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for k in ("rows", "结果", "items", "data", "list", "输出"):
            v = raw.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                return [dict(v, **{"xl_row": kk}) if "xl_row" not in v else v
                        for kk, v in v.items() if isinstance(v, dict)]
        # {行号: {...}} 形态
        if raw and all(isinstance(v, dict) for v in raw.values()) and \
                all(str(k).isdigit() for k in raw):
            return [dict(v, xl_row=k) for k, v in raw.items()]
    return []


def run_batches(
    ctx,
    items: Sequence[Any],
    build_prompt: Callable[[Sequence[Any]], str],
    *,
    parse: Optional[Callable[[Any, Sequence[Any]], list[dict]]] = None,
    accept: Optional[Callable[[Mapping[str, Any]], bool]] = None,
    key_of: Callable[[Any], Any] = lambda it: it,
    batch: int = 8,
    workers: int = 6,
    max_tokens: int = 8000,
    temperature: float = 0.2,
    log: Optional[Callable[[str], None]] = None,
    ckpt_path: str = "",
    plan_key: str = "",
    label: str = "文本抽取",
) -> dict:
    """把 `items` 切成 `batch` 条一批, 并发问 LLM, 返回 `{key: rec}`。

    `parse(raw, batch_items)`: 默认按 `xl_row` 对齐(见 `_extract_rows`)。
    `accept(rec)`: 返回 False 的记录被丢弃(如"引用核验没过")。
    """
    _log = log or (lambda m: None)
    if not items:
        return {}
    # ---- 断点
    done: dict = {}
    if ckpt_path and os.path.exists(ckpt_path):
        try:
            o = json.load(open(ckpt_path, encoding="utf-8"))
            if (o.get("input_key") or "") == plan_key:
                done = {k: v for k, v in (o.get("done") or {}).items()}
        except (OSError, ValueError):
            done = {}
    if done:
        _log("%s断点续跑: 已读回 %d 条" % (label, len(done)))

    todo = [it for it in items if str(key_of(it)) not in done]

    def _save() -> None:
        if not ckpt_path:
            return
        try:
            os.makedirs(os.path.dirname(ckpt_path) or ".", exist_ok=True)
            tmp = ckpt_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"input_key": plan_key, "done": done}, fh, ensure_ascii=False)
            os.replace(tmp, ckpt_path)
        except OSError:
            pass

    def _parse(raw: Any, b: Sequence[Any]) -> list[dict]:
        if parse is not None:
            return parse(raw, b) or []
        recs = _extract_rows(raw)
        out = []
        for rec in recs:
            out.append({str(k): v for k, v in rec.items()})
        return out

    def _one(b: Sequence[Any]) -> dict:
        prompt = build_prompt(b)
        try:
            raw = ctx.llm().chat_json(prompt, temperature=temperature, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            _log("%s调用失败(批 %d 条): %s" % (label, len(b), str(exc)[:120]))
            return {}
        got = {}
        for rec in _parse(raw, b):
            k = rec.get("xl_row", rec.get("行号", rec.get("key")))
            if k is None:
                continue
            k = str(k)
            if accept is not None and not accept(rec):
                continue
            got[k] = rec
        return got

    def _run(batches: Sequence[Sequence[Any]]) -> None:
        nonlocal todo
        use = max(1, min(workers, len(batches)))
        if use == 1:
            for b in batches:
                got = _one(b)
                for k, v in got.items():
                    done[k] = v
                _save()
            return
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=use) as ex:
            futs = {ex.submit(_one, b): b for b in batches}
            fin = 0
            for fut in as_completed(futs):
                b = futs[fut]
                got = fut.result()
                for k, v in got.items():
                    done[k] = v
                fin += 1
                if fin % 10 == 0 or fin == len(batches):
                    _log("%s 批 %d/%d 完成(累计 %d 条)" % (label, fin, len(batches), len(done)))
                _save()

    batches = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    _run(batches)
    # ---- 未回的条目: 同批重发 → 拆 2 行/批 → 逐行
    for size in (batch, 2, 1):
        miss = [it for it in items if str(key_of(it)) not in done]
        if not miss:
            break
        if size >= batch:
            _log("%s %d 条未回, 同批重发一次" % (label, len(miss)))
        else:
            _log("%s %d 条未回, 降为 %d 行/批重试" % (label, len(miss), size))
        _run([miss[i:i + size] for i in range(0, len(miss), size)])
    left = [it for it in items if str(key_of(it)) not in done]
    if left:
        _log("%s %d 条最终未回(跳过)" % (label, len(left)))
    if ckpt_path and not left and os.path.exists(ckpt_path):
        try:
            os.remove(ckpt_path)
        except OSError:
            pass
    return done
