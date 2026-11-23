# -*- coding: utf-8 -*-
"""一轮闭环编排：产出（可多次运行）→ 结构闸门 → 只重跑被拒的 → 稳定性筛选 → 交付

两种生产者（同一个接口，便于离线复现与项目内直调）
================================================================================
  FileProducer      从磁盘读取已有的运行文件（离线 / 由外部 agent 产出）
  CallableProducer  把任意 `fn(prompt) -> dict` 包成生产者；
                    在 auto-workflow-python 里绑定 `ctx.llm().chat_json` 即可直调模型

闭环
================================================================================
  1. 产出 N 份运行
  2. 结构闸门：逐份判定 PASS/REJECT
  3. 对被拒的运行生成**指名到行**的重跑指令，重新产出（只重跑被拒的）
  4. 再闸门（可迭代 max_retry 次）
  5. 稳定性筛选：独异格率超过阈值的运行不纳入交付
  6. 多数表决 → 交付表 + 两套指标（诚实指标 / 交付机制）

断点续跑（CallableProducer, 步骤内部）
===============================================================================
  每跑完一批即把已产出行落盘 `.run_tag.checkpoint.json`(输入指纹校验), 中断后
  重启只补跑剩余行; 整份运行完整产出后复用 `run_tag.json`, 不再重复调 LLM。
  指纹含输入行 + 重跑 brief, 输入或指令变化即失效, 防止把旧进度续到新输入上。

用法（离线，本仓库）
================================================================================
  python -m attr_stability.attr_runner --prefix r4 \\
      --cat-dir categories --cat salt_pepper_mill \\
      --input results/llm_only/input.json \\
      --out-dir results/llm_only --out outputs/attr_stability_r4.json

用法（项目内直调，在 step 或脚本里）
================================================================================
  from attr_stability import CallableProducer, run_round, RuleSource

  rules = RuleSource.load(cat_dir)              # 从类目配置读，非硬编码
  prod  = CallableProducer(
      fn=lambda prompt: ctx.llm().chat_json(prompt, max_tokens=16000),
      rows=rows,                                # list[dict]，含 xl_row
      out_dir=ctx.paths.cat_root / "17_属性标注",
      prefix="a", prompt_text=open(prompt_path, encoding="utf-8").read())
  result = run_round(prod, rules, n_runs=5, max_alone=10.0, log=ctx.log.info)
  result.write(ctx.paths.cat_root / "17_属性标注" / "稳定性结果.json")
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

try:                                   # 包内导入
    from .attr_stability import (
        ERR_QUALITY, GateIssue, GateResult, RuleSource, alone_rates,
        build_retry_brief, consistency, deliverable_from_cells, gate_run,
        majority, union_cells, review_queue, process_union,
    )
except ImportError:                    # 直接当脚本跑
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from attr_stability import (       # type: ignore
        ERR_QUALITY, GateIssue, GateResult, RuleSource, alone_rates,
        build_retry_brief, consistency, deliverable_from_cells, gate_run,
        majority, union_cells, review_queue, process_union,
    )

__all__ = [
    "Producer", "FileProducer", "CallableProducer",
    "RoundResult", "run_round", "load_rows", "load_run_files",
    "DEFAULT_CHUNK",
]

# 单次调用送多少行。太大易被输出截断；太小则调用次数多。
# 本项目实测：12 行 × 25 属性 ≈ 80 KB 输出，安全。
DEFAULT_CHUNK = 12


# ================================================================ 生产者
class Producer:
    """生产者接口：产出一份运行并返回 `{xl_row: 行数据}`。"""

    def produce(
        self,
        run_tag: str,
        retry_brief: Optional[str] = None,
    ) -> dict[int, Mapping[str, Any]]:
        raise NotImplementedError


class FileProducer(Producer):
    """离线生产者：运行文件已存在（由外部 agent 计划产出）。

    重跑时**不做任何事**——文件由外部覆盖后再跑一遍 `run_round` 即可。
    这样离线流程与项目内直调共用同一套闸门/一致性逻辑。
    """

    def __init__(self, out_dir: str, prefix: str):
        self.out_dir = out_dir
        self.prefix = prefix

    def path(self, run_tag: str) -> str:
        return os.path.join(self.out_dir, "%s.json" % run_tag)

    def produce(self, run_tag: str, retry_brief: Optional[str] = None):
        p = self.path(run_tag)
        if not os.path.exists(p):
            raise FileNotFoundError("运行文件不存在：%s" % p)
        with open(p, encoding="utf-8-sig") as fh:
            o = json.load(fh)
        rows = o if isinstance(o, list) else o.get("rows", [])
        return {r.get("xl_row"): r for r in rows if isinstance(r, Mapping)}


def _normalize_got_row(row):
    """将行号和属性对象归一为步骤17的契约格式。

    2026-10-29 实测 Salt and Pepper，模型有时回：
        {"row_id": "2", "attributes": {"是否带漏斗": {"value": "否",
                                                     "source": "五点描述", "basis": "…"}}}
    契约要求行内保留 attributes 对象，其内部使用属性、来源和依据的平铺键。

    不归一的话 `xl_row` 取不到 → 整批报「非法或额外行号」→ 白跑一批。
    """
    if not isinstance(row, Mapping):
        return row
    out = dict(row)
    if "xl_row" not in out:
        for k in ("row_id", "rowId", "row", "id", "行号"):
            if k in out and str(out[k]).strip():
                out["xl_row"] = out.pop(k)
                break
    attrs = out.get("attributes")
    if isinstance(attrs, Mapping):
        flat = {str(name): val for name, val in attrs.items()
                if not isinstance(val, Mapping) or "value" not in val}
        for name, val in attrs.items():
            name = str(name)
            if isinstance(val, Mapping) and "value" in val:
                flat[name] = val["value"]
                if "source" in val:
                    flat.setdefault(name + "_来源", val["source"])
                if "basis" in val or "依据" in val:
                    flat.setdefault(name + "_依据", val.get("basis") or val.get("依据") or "")
        out["attributes"] = flat
    return out


def quarantine_unknown_attributes(rows, known):
    """从工作副本隔离未定义键，返回逐行原样记录；不修改磁盘原始产物。"""
    isolated = []
    for rid, row in rows.items():
        at = row.get("attributes") if isinstance(row, dict) else None
        if not isinstance(at, dict):
            continue
        extra = {}
        for key in list(at):
            name = str(key)
            for suffix in ("_来源", "_依据"):
                if name.endswith(suffix):
                    name = name[:-len(suffix)]
                    break
            if name not in known:
                extra[key] = at.pop(key)
        if extra:
            isolated.append({"xl_row": rid, "attributes": extra})
    return isolated


def fill_missing_sources(rows, input_rows, known):
    """只用原文中的连续依据补来源；无法唯一定位时保留空来源并登记。"""
    try:
        from .unknowns import is_unknown
    except ImportError:
        from unknowns import is_unknown
    fields = ("标题", "亮点", "五点描述", "详细参数", "SKU")
    filled, review = 0, []
    for rid, row in rows.items():
        attrs = row.get("attributes") if isinstance(row, Mapping) else None
        original = input_rows.get(rid) if input_rows else None
        if not isinstance(attrs, dict):
            continue
        for name in known:
            v = attrs.get(name)
            if (v is None or (is_unknown(v) and str(v).strip() != "不适用")
                    or str(attrs.get(name + "_来源") or "").strip()):
                continue
            basis = str(attrs.get(name + "_依据") or "").strip()
            match = re.match(r"^(标题|亮点|五点描述|详细参数|SKU)\s*[：:]\s*", basis, re.I)
            claimed = match.group(1) if match else ""
            quote = basis[match.end():].strip() if match else basis
            # 忽略空白和大小写，不拆句、不采用模糊或短词匹配。
            norm = lambda s: re.sub(r"\s+", " ", str(s or "")).strip().casefold()
            needle = norm(quote)
            matches = [f for f in fields if len(needle) >= 4 and isinstance(original, Mapping)
                       and needle in norm(original.get(f))]
            if claimed:
                target = next((f for f in matches if f.casefold() == claimed.casefold()), "")
                if not target and len(matches) == 1:
                    target = matches[0]
            else:
                target = matches[0] if len(matches) == 1 else ""
            if target:
                attrs[name + "_来源"] = target
                filled += 1
            else:
                review.append({"xl_row": rid, "attribute": name, "basis": basis,
                               "reason": "来源无法唯一匹配当前商品原文，需复核"})
    return filled, review


class CallableProducer(Producer):
    """把 `fn(prompt) -> dict|list` 包成生产者。

    fn 在 auto-workflow-python 里应绑定为 `ctx.llm().chat_json`
    （该客户端已强制非思考模式、JSON 模式、截断自动提上限、失败换温度重试）。
    也可传任何自定义函数，便于测试（可注入 mock）。
    """

    def __init__(
        self,
        fn: Callable[[str], Any],
        rows: Sequence[Mapping[str, Any]],
        out_dir: str,
        prefix: str,
        prompt_text: str,
        *,
        chunk: int = DEFAULT_CHUNK,
        max_tokens: int = 16000,
        save_raw: bool = True,
        log=None,
        workers: int = 3,
        input_fields: Optional[Sequence[str]] = None,
        cache_context: Optional[Mapping[str, Any]] = None,
        compact_attributes: Optional[Sequence[str]] = None,
        compact_prompt_text: Optional[str] = None,
        adjudication_hints: Optional[Mapping[str, str]] = None,
    ):
        self.fn = fn
        self.rows = list(rows)
        self.out_dir = out_dir
        self.prefix = prefix
        self.prompt_text = prompt_text
        self.compact_prompt_text = compact_prompt_text
        self.adjudication_hints = dict(adjudication_hints or {})
        self.chunk = max(1, int(chunk))
        self.max_tokens = int(max_tokens)
        self.save_raw = save_raw
        self.log = log
        self.workers = max(1, int(workers))
        # 输入字段白名单: 只把标注真正需要的字段塞进 prompt, 砍掉无关字段省 token。
        # None = 整行全传(旧行为, 测试/兼容用); 给定则只传这些字段(缺的字段用空串占位)。
        self.input_fields = [str(f) for f in input_fields] if input_fields else None
        self.cache_context = dict(cache_context or {})
        names = [str(n).strip() for n in (compact_attributes or []) if str(n).strip()]
        if len(names) != len(set(names)):
            raise ValueError("压缩属性名不能重复")
        self.compact_attributes = names
        self._compact_codes = {"p%d" % (i + 1): name for i, name in enumerate(names)}

    # ------------------------------------------------------------ 提示词
    def build_prompt(self, chunk_rows: Sequence[Mapping[str, Any]],
                     retry_brief: Optional[str], *, compact: bool = True) -> str:
        if self.input_fields is not None:
            rows_out = [
                {f: r.get(f, "") for f in self.input_fields}
                for r in chunk_rows
            ]
        else:
            rows_out = list(chunk_rows)
        base_prompt = self.compact_prompt_text if compact and self._compact_codes and self.compact_prompt_text is not None else self.prompt_text
        parts = [base_prompt.rstrip(), ""]
        if compact and self._compact_codes:
            parts += [
                "## 本次输出格式覆盖说明（仅改变编码，判断规则和证据要求不变）",
                "本次 attributes 不使用属性名、_来源、_依据三个平铺键。"
                "下面每个编号对应一个属性，每行每个编号必须且只能输出一次，"
                "其值必须是严格三元素数组 [属性值, 来源, 依据]，顺序不可变。"
                "保留真实依据和来源，未明确也必须给依据；不要省略或猜测。"
                "xl_row、category_check及其他行级字段仍按原要求输出。",
                "属性编号映射：" + json.dumps(self._compact_codes, ensure_ascii=False, separators=(",", ":")),
                '格式示例：{"rows":[{"xl_row":2,"category_check":{"判定":"属于本类目","信号":[]},'
                '"attributes":{"p1":["未明确","","原文未提及该属性"]}}]}。'
                "示例仅展示一个属性，实际必须覆盖映射中的全部编号。", "",
            ]
        parts += ["## 输入（本次要标注的行）", "",
                 "```json",
                 json.dumps({"rows": rows_out}, ensure_ascii=False, separators=(",", ":")),
                 "```", ""]
        if retry_brief:
            parts += ["## 上一轮未通过结构闸门，本次必须修正", "", retry_brief, ""]
        return "\n".join(parts)

    def _decode_compact_rows(self, rows):
        """解码三元组；无效编号或结构触发回退，缺失属性仍由既有闸门检查。"""
        if not self._compact_codes:
            return rows
        decoded = []
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("attributes"), Mapping):
                raise ValueError("压缩标注缺少属性对象")
            attrs = row["attributes"]
            flat = {}
            for key, value in attrs.items():
                code = str(key)
                if code in self._compact_codes:
                    if not isinstance(value, list) or len(value) != 3:
                        raise ValueError("压缩标注的属性必须包含值、来源和依据")
                    name = self._compact_codes[code]
                    for dest, item in zip((name, name + "_来源", name + "_依据"), value):
                        if dest in flat:
                            raise ValueError("压缩标注出现重复属性")
                        flat[dest] = item
                else:
                    if re.match(r"^p\d+(?:_|$)", code):
                        raise ValueError("压缩标注出现未知编号")
                    if code in self.compact_attributes and isinstance(value, list):
                        raise ValueError("压缩标注使用了属性名而非编号")
                    if code in flat:
                        raise ValueError("压缩标注出现重复属性")
                    flat[code] = value
            decoded.append({**dict(row), "attributes": flat})
        return decoded

    # ------------------------------------------------------------ 补漏
    def fill_missing(self, rows, missing_map, run_tag: str = "fill"):
        """**只补跑每行缺失的属性**（2026-10-23 方案 A）。

        背景: 实测 a2 只因为 `1 行漏了「是否带滤网」` 就被闸门判 REJECT → 整份作废。
        整份重跑（12 元 / 15 分钟）代价太大，而缺的往往只是 1 行 1 个属性。

        做法: 对每个有缺口的行**单独发一次请求**，提示词里明确"只补这些属性，
        其它属性不要输出、也不要把已有值改写"，然后把结果 merge 回去。

        missing_map: `{xl_row: [缺失属性名, ...]}`。返回补过之后的 `{xl_row: row}`。
        """
        out = dict(rows)
        input_rows = {str(r.get("xl_row")): r for r in self.rows if isinstance(r, Mapping)}
        # 将相同缺口的行合并；没有返回或格式异常的行仍走原逐行补漏。
        groups: dict[tuple, list] = {}
        for rid, missing in (missing_map or {}).items():
            if missing and isinstance(out.get(rid), Mapping) and str(rid) in input_rows:
                groups.setdefault(tuple(sorted(set(missing))), []).append(rid)
        prefetched = {}
        for missing, rids in groups.items():
            for start in range(0, len(rids), min(self.chunk, 8)):
                part = rids[start:start + min(self.chunk, 8)]
                if len(part) < 2:
                    continue
                payloads, verify = [], {}
                for rid in part:
                    orig = input_rows[str(rid)]
                    payloads.append({f: orig.get(f, "") for f in self.input_fields} if self.input_fields else dict(orig))
                    payloads[-1]["xl_row"] = rid
                    at = out[rid].get("attributes") or {}
                    verify[str(rid)] = {n: at[n] for n in missing if n in at and not str(at.get(n + "_依据") or "").strip()}
                prompt = "\n".join([self.prompt_text.rstrip(), "", "## 定向批量补漏",
                    "每行只补以下属性：" + "、".join(missing),
                    "每行独立判断，不得借用其他商品证据；对已有值，仅有原文支持时保留并摘录依据，否则返回未明确。",
                    "已有值：" + json.dumps(verify, ensure_ascii=False, separators=(",", ":")),
                    "返回JSON：{\"rows\":[{\"xl_row\":行号,\"attributes\":{属性名:值, 属性名_来源:来源, 属性名_依据:原文}}]}；不得输出其他属性。",
                    "## 输入（本次要标注的行）", "```json", json.dumps({"rows": payloads}, ensure_ascii=False, separators=(",", ":")), "```"])
                try:
                    got = self.fn(prompt)
                    result = got if isinstance(got, list) else (got or {}).get("rows", [])
                    if not isinstance(result, list):
                        continue
                    counts = {}
                    for item in result:
                        if isinstance(item, Mapping):
                            key = str(item.get("xl_row", item.get("row_id", "")))
                            counts[key] = counts.get(key, 0) + 1
                    for item in result:
                        if isinstance(item, Mapping):
                            key = str(item.get("xl_row", item.get("row_id", "")))
                            if key in {str(r) for r in part} and counts[key] == 1 and isinstance(item.get("attributes"), Mapping):
                                original_id = next(r for r in part if str(r) == key)
                                prior = out[original_id].get("attributes") or {}
                                returned = item["attributes"]
                                required = {n + suffix for n in missing for suffix in ("", "_来源", "_依据")
                                            if n + suffix not in prior or (suffix in ("", "_依据") and not str(prior.get(n + suffix) or "").strip())}
                                required.update(n for n in missing if n in prior and not str(prior.get(n + "_依据") or "").strip())
                                if required <= set(returned):
                                    prefetched[key] = item
                except Exception as exc:
                    if self.log:
                        self.log("      补漏 %s 批量失败，回退逐行: %s" % (run_tag, str(exc)[:90]))
        dirty = 0
        for rid, missing in (missing_map or {}).items():
            row = out.get(rid)
            if not isinstance(row, Mapping) or not missing:
                continue
            original = input_rows.get(str(rid))
            if not isinstance(original, Mapping):
                if self.log:
                    self.log("      补漏 %s 行 %s 找不到原始输入，保留原结果" % (run_tag, rid))
                continue
            if self.input_fields is not None:
                payload = {f: original.get(f, "") for f in self.input_fields}
            else:
                payload = {k: v for k, v in original.items() if k != "attributes"}
            payload = {"xl_row": rid, **payload}
            existing = dict(row.get("attributes") or {})
            verify = {name: existing[name] for name in missing
                      if name in existing and not str(existing.get(name + "_依据") or "").strip()}
            prompt = "\n".join([
                self.prompt_text.rstrip(), "",
                "## 本次**只需补这些属性**（其它属性不要输出、也不要把已有值改写）", "",
                "、".join(str(x) for x in missing), "",
                "以下已有值缺少依据，只作为待核判断。对每个已有值，若原文有明确支持，保持该值并给出单段连续原文摘录；"
                "若无法明确支持该值，返回值‘未明确’，不要猜测。不要修改其他属性。",
                json.dumps(verify, ensure_ascii=False), "",
                "## 输入（本次要标注的行）", "",
                "```json",
                json.dumps({"rows": [payload]}, ensure_ascii=False, indent=2),
                "```", "",
            ])
            try:
                cached = prefetched.get(str(rid))
                got = {"rows": [cached]} if cached is not None else self.fn(prompt)
            except Exception as exc:  # noqa: BLE001 — 补漏失败不炸整份运行
                if self.log:
                    self.log("      补漏 %s 行 %s 失败(跳过): %s" % (run_tag, rid, str(exc)[:90]))
                continue
            got_rows = got if isinstance(got, list) else (got or {}).get("rows", [])
            if not isinstance(got_rows, list) or not got_rows:
                continue
            g0 = next((_normalize_got_row(r) for r in got_rows
                       if isinstance(r, Mapping) and str(r.get("xl_row", r.get("row_id", ""))) == str(rid)), {})
            at = dict(row.get("attributes") or {})
            returned = g0.get("attributes") or {}
            if not isinstance(returned, Mapping):
                continue
            # 只补原来缺失的键；即使模型额外返回其他字段，也不能覆盖已有判断。
            allowed = {name + suffix for name in missing for suffix in ("", "_来源", "_依据")}
            added = {k: v for k, v in returned.items() if k in allowed and k not in at}
            for name in missing:
                basis = name + "_依据"
                if name in at and (at[name] is None or (isinstance(at[name], str) and not at[name].strip())):
                    if not g0:
                        continue  # 未收到本行响应，不把调用错误当成证据不足
                    proposed = returned.get(name)
                    proposed = str(proposed).strip() if proposed is not None else ""
                    evidence = str(returned.get(basis) or "").strip()
                    excerpt = re.sub(r"^(?:标题|亮点|五点描述|详细参数|SKU)\s*[：:]\s*", "", evidence, flags=re.I).strip()
                    supported = len(excerpt) >= 4 and any(excerpt.casefold() in str(original.get(f) or "").casefold()
                                                          for f in ("标题", "亮点", "五点描述", "详细参数", "SKU"))
                    if proposed and (proposed in ("未明确", "未知", "待确认") or supported):
                        added[name] = "未明确" if proposed in ("未知", "待确认") else proposed
                        added[name + "_来源"] = str(returned.get(name + "_来源") or "")
                        added[basis] = evidence or "补跑后证据不足，未明确"
                    else:
                        added[name] = "未明确"
                        added[name + "_来源"] = ""
                        added[basis] = "补跑未得到有原文支持的有效值，未明确"
                    continue
                if name not in at or str(at.get(basis) or "").strip():
                    continue
                # Preserve the existing judgement; accept a missing/empty basis
                # only if the model repeats that judgement and quotes the input.
                added.pop(basis, None)
                evidence = str(returned.get(basis) or "").strip()
                excerpt = re.sub(r"^(?:标题|亮点|五点描述|详细参数|SKU|图片OCR)\s*[：:]\s*", "", evidence, flags=re.I).strip()
                fields = ("标题", "亮点", "五点描述", "详细参数", "SKU")
                supported = bool(excerpt) and any(excerpt.casefold() in str(original.get(f) or "").casefold() for f in fields)
                if returned.get(name) == at[name] and supported:
                    added[basis] = evidence
                elif (isinstance(returned.get(name), str) and returned[name].strip()
                      and any(str(original.get(f) or "").strip() for f in fields)):
                    # A real rejudgement was returned but did not verify the old
                    # value with an exact quote. Do not let an unsupported value
                    # pass the gate; callers retain the prior result in backup.
                    added[name] = "未明确"
                    added[name + "_来源"] = ""
                    added[basis] = "原始资料未找到足以确认原值的明确依据，待复核"
                    if self.log:
                        self.log("      补漏 %s 行 %s 属性 %s：原值 %s 缺证，重判为未明确"
                                 % (run_tag, rid, name, str(at[name])[:80]))
            if not added:
                continue
            at.update(added)
            out[rid] = {**dict(row), "attributes": at}
            dirty += 1
            if self.save_raw and dirty % 20 == 0:
                self._save_ckpt(self._raw_path(run_tag), self._input_key(), out)
            if self.log:
                self.log("      补漏 %s 行 %s: 补回 %d 个缺失字段" % (run_tag, rid, len(added)))
        if self.save_raw and dirty % 20:
            self._save_ckpt(self._raw_path(run_tag), self._input_key(), out)
        return out

    # ------------------------------------------------------------ 定向修复
    def repair_rows(self, rows, errors_by_row, run_tag: str = "rep", batch: Optional[int] = None):
        """对**闸门报错的行**做定向修复（2026-10-25 用户口径："能重跑的修复，修好放行"）。

        与 `fill_missing` 的分工:
          · `fill_missing` 只补"属性名缺失"（漏属性）;
          · `repair_rows`  修**所有**闸门报错 —— 未定义属性（`尺寸_拆列`/`X_dup`）、
            词表外值、漏 `_依据`/`_来源`、依据为空 等。

        做法: 把出错的行按当前 chunk 分批重新标注，提示词里逐行列出"错在哪、怎么改"；
        并发数沿用 workers，每批完成后原子保存，重启后由闸门筛出剩余错误行。
        返回的整行结果**替换**原行（不是追加）—— 因为错键要先删掉。

        errors_by_row: `{xl_row: [错误说明, ...]}`。返回修复后的 `{xl_row: row}`。
        """
        out = dict(rows)
        input_rows = {str(r.get("xl_row")): r for r in self.rows if isinstance(r, Mapping)}
        todo = [rid for rid in (errors_by_row or {}) if rid in out]
        batch = max(1, int(self.chunk if batch is None else batch))
        parts = [todo[i:i + batch] for i in range(0, len(todo), batch)]

        def repair_part(part):
            payload, hints = [], []
            for rid in part:
                row = input_rows.get(str(rid))
                if not isinstance(row, Mapping):
                    if self.log:
                        self.log("      定向修复 %s 行 %s 找不到原始输入，保留原结果" % (run_tag, rid))
                    continue
                if self.input_fields is not None:
                    one = {f: row.get(f, "") for f in self.input_fields}
                else:
                    one = {k: v for k, v in row.items() if k != "attributes"}
                payload.append({"xl_row": rid, **one})
                hints.append("xl_row=%s 的错误：%s" % (
                    rid, "；".join(str(e) for e in (errors_by_row.get(rid) or [])[:6])))
            prompt = "\n".join([
                self.prompt_text.rstrip(), "",
                "## 本次是**定向修复**：下面这些行上一版没过结构闸门，请按列出的错误重做", "",
                "\n".join("- " + h for h in hints), "",
                "## 修复要求（务必逐条满足）", "",
                "1. **输出完整的一行**（所有属性 + 每个属性的 `_依据` 与 `_来源`），"
                "不要只输出出错的那几个；",
                "2. **删掉**上面列出的未定义键（如 `尺寸_拆列` → 改成写 `尺寸` 本身）；",
                "3. 词表外的值按「典型取值」改写成受控值；确无受控值的，保留原文并确保键名正确；",
                "4. `_来源` 只能是 标题/亮点/五点描述/详细参数/SKU/图片OCR/图片视觉 之一或空串；",
                "5. `_依据` 必须是原文摘录，**不能为空**。", "",
                "## 输入（本次要修复的行）", "",
                "```json",
                json.dumps({"rows": payload}, ensure_ascii=False, indent=2),
                "```", "",
            ])
            try:
                got = self.fn(prompt)
            except Exception as exc:  # noqa: BLE001 — 修复失败保住原值，不炸整份
                if self._is_truncation(exc) and len(part) > 1:
                    mid = len(part) // 2
                    return {**repair_part(part[:mid]), **repair_part(part[mid:])}
                if self.log:
                    self.log("      定向修复 %s 行 %s 失败(保留原值): %s"
                             % (run_tag, part, str(exc)[:90]))
                return {}
            got_rows = got if isinstance(got, list) else (got or {}).get("rows", [])
            if not isinstance(got_rows, list):
                return {}
            repaired = {}
            for r in got_rows:
                r = _normalize_got_row(r)
                if not isinstance(r, Mapping):
                    continue
                try:
                    rid = int(r.get("xl_row"))
                except (TypeError, ValueError):
                    continue
                if rid not in part:
                    continue
                at = r.get("attributes")
                if not isinstance(at, Mapping) or not at:
                    continue
                repaired[rid] = {**dict(out[rid]), "attributes": dict(at)}
            return repaired

        def commit(part, repaired, done):
            out.update(repaired)
            if repaired and self.save_raw:
                # 完整 raw 随每批修复更新；重启先复用它，再由闸门筛出尚未修好的行。
                self._save_ckpt(self._raw_path(run_tag), self._input_key(), out)
            if self.log:
                self.log("      定向修复 %s 批 %d/%d: %d 行重建（原错误行 %d；批上限 %d；已保存 %s）"
                         % (run_tag, done, len(parts), len(repaired), len(part), batch,
                            "是" if repaired and self.save_raw else "否"))

        if self.workers <= 1 or len(parts) <= 1:
            for done, part in enumerate(parts, 1):
                commit(part, repair_part(part), done)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                pending = {pool.submit(repair_part, part): part for part in parts}
                for done, future in enumerate(as_completed(pending), 1):
                    commit(pending[future], future.result(), done)
        return out

    # ------------------------------------------------------------ 产出
    @staticmethod
    def _is_truncation(exc: Exception) -> bool:
        """识别"输出被截断"专用错误(不 import core, 按异常类名 + finish_reason 文案)。

        ★ 2026-10-29: 把「无法解析 LLM JSON」也算作截断 —— 实测输出撞 max_tokens 时
          抛的是 parse error 而不是 LLMTruncatedError，于是**自动拆批没触发**，
          整批作废 → 整遍 REJECT → 步骤17 失败（Salt and Pepper 实测 44 分钟白跑）。
          归到截断后，走两半递归重试，小批更容易返回合法 JSON。
        """
        name = exc.__class__.__name__
        msg = str(exc)
        return (name == "LLMTruncatedError" or "finish_reason=length" in msg
                or "无法解析 LLM JSON" in msg)

    def _produce_chunk(self, chunk, run_tag: str,
                       retry_brief: Optional[str]) -> dict[int, Mapping[str, Any]]:
        """产出一个 chunk; 输出被截断时**自动拆成两半递归重试**(直到单行或成功)。"""
        if not chunk:
            return {}
        prompt = self.build_prompt(chunk, retry_brief)
        try:
            got = self.fn(prompt)
        except Exception as exc:  # noqa: BLE001 — 截断才拆批, 其余原样抛出
            if self._is_truncation(exc) and len(chunk) > 1:
                mid = len(chunk) // 2
                out: dict[int, Mapping[str, Any]] = {}
                out.update(self._produce_chunk(chunk[:mid], run_tag, retry_brief))
                out.update(self._produce_chunk(chunk[mid:], run_tag, retry_brief))
                return out
            raise
        got_rows = got if isinstance(got, list) else (got or {}).get("rows", [])
        if not isinstance(got_rows, list):
            raise ValueError("生产者返回结构异常：%s" % type(got).__name__)
        if self._compact_codes:
            try:
                got_rows = self._decode_compact_rows(got_rows)
            except ValueError as exc:
                if self.log:
                    self.log("   %s 压缩输出格式无效，回退普通格式重试: %s" % (run_tag, exc))
                fallback = self.fn(self.build_prompt(chunk, retry_brief, compact=False))
                got_rows = fallback if isinstance(fallback, list) else (fallback or {}).get("rows", [])
                if not isinstance(got_rows, list):
                    raise ValueError("普通格式回退返回结构异常")
        got_rows = [_normalize_got_row(r) for r in got_rows]
        expected = {r["xl_row"] for r in chunk}
        out = {}
        for row in got_rows:
            if not isinstance(row, Mapping):
                raise ValueError("生产者返回行不是对象")
            rid = row.get("xl_row")
            if isinstance(rid, str) and rid.isdigit():
                rid = int(rid)
            if not isinstance(rid, int) or isinstance(rid, bool) or rid not in expected:
                raise ValueError("生产者返回非法或额外行号: %r" % row.get("xl_row"))
            if rid in out:
                raise ValueError("生产者返回重复行号: %s" % rid)
            out[rid] = {**row, "xl_row": rid}
        return out

    def _produce_chunks(self, chunk_list, run_tag: str,
                        retry_brief: Optional[str],
                        base: Optional[Mapping[int, Mapping[str, Any]]] = None,
                        ckpt: Optional[tuple[str, str]] = None,
                        ) -> dict[int, Mapping[str, Any]]:
        """并发产出 chunk 列表(每个元素是一批行), 返回 {xl_row: row}。单批失败即抛出。

        base / ckpt: 断点续跑用。base 是本轮之前已恢复的行; ckpt=(path, key) 时,
        每完成一批就把 base ∪ 本轮产出写回检查点文件, 中断后重启可从检查点续跑。
        """
        import time as _time
        from concurrent.futures import ThreadPoolExecutor, as_completed
        out: dict[int, Mapping[str, Any]] = {}
        base = base or {}
        ckpt_path, ckpt_key = ckpt if ckpt else (None, None)

        def _maybe_ckpt() -> None:
            if ckpt_path:
                self._save_ckpt(ckpt_path, ckpt_key, {**base, **out})

        def _timed_chunk(c):
            """跑一批并单独计时(只算该批真正执行耗时, 不含并发排队)。"""
            t_batch = _time.time()
            got = self._produce_chunk(c, run_tag, retry_brief)
            return got, _time.time() - t_batch

        def _emit(done: int, n: int, batch_cost: float, total_cost: float) -> None:
            if self.log:
                self.log("   %s 批 %d/%d 完成(累计 %d 行, 本批 %.1fs, 总耗时 %.0fs)"
                         % (run_tag, done, n, len(base) + len(out),
                            batch_cost, total_cost))

        n = len(chunk_list)
        workers = self.workers if n > 1 else 1
        t0 = _time.time()
        if workers <= 1 or n <= 1:
            done = 0
            for c in chunk_list:
                got, batch_cost = _timed_chunk(c)
                out.update(got)
                done += 1
                _emit(done, n, batch_cost, _time.time() - t0)
                _maybe_ckpt()
            return out
        done = 0
        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            futs = [ex.submit(_timed_chunk, c) for c in chunk_list]
            for fut in as_completed(futs):
                got, batch_cost = fut.result()
                out.update(got)
                done += 1
                _emit(done, n, batch_cost, _time.time() - t0)
                _maybe_ckpt()
        except Exception:
            ex.shutdown(wait=False, cancel_futures=True)
            raise
        ex.shutdown(wait=True)
        if self.log:
            self.log("   %s 全部 %d 批完成, 共 %d 行"
                     % (run_tag, n, len(base) + len(out)))
        return out

    # ------------------------------------------------------------ 断点续跑(检查点)
    def _input_key(self, brief: Optional[str] = None) -> str:
        """输入行指纹: 输入变化(行数/内容/行号)即失效, 防止用旧检查点续到新输入上。

        传入 brief 时把重跑指令一并计入指纹, 避免把「上一份 brief 下产出的半成品」
        续到「新 brief」的补跑里(重跑指令每次可能不同)。
        """
        import hashlib
        payload_data = {"version": 2, "rows": self.rows,
                              "prompt": self.prompt_text, "fields": self.input_fields,
                              "context": self.cache_context}
        if self.compact_attributes:
            payload_data["compact_attributes_v1"] = self.compact_attributes
            payload_data["compact_prompt_text"] = self.compact_prompt_text
        payload = json.dumps(payload_data, sort_keys=True, ensure_ascii=False, default=str)
        if brief:
            payload += "\n" + brief
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def _ckpt_path(self, run_tag: str) -> str:
        """检查点路径。用 . 前缀避开 load_run_files 的 `a*.json` 通配, 不被当运行文件。"""
        return os.path.join(self.out_dir, ".%s.checkpoint.json" % run_tag)

    @staticmethod
    def _save_ckpt(path: str, key: str,
                   collected: Mapping[int, Mapping[str, Any]]) -> None:
        """原子写检查点: 先写临时文件再替换, 中断不留半截文件。"""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                                   dir=os.path.dirname(os.path.abspath(path)))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"input_key": key,
                           "rows": [collected[k] for k in sorted(collected)]},
                          fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    @staticmethod
    def _load_ckpt(path: str, key: str) -> dict[int, Mapping[str, Any]]:
        """读回检查点; 输入指纹不匹配或文件损坏/不存在 → 返回空(从头跑)。"""
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                o = json.load(fh)
        except (OSError, ValueError):
            return {}
        if (o.get("input_key") or "") != key:
            return {}
        rows = o.get("rows") or []
        return {r.get("xl_row"): r for r in rows
                if isinstance(r, Mapping) and r.get("xl_row") is not None}

    @staticmethod
    def _rm_ckpt(path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def _raw_path(self, run_tag: str) -> str:
        return os.path.join(self.out_dir, "%s.json" % run_tag)

    def _load_completed(self, path: str, key: str,
                        ) -> Optional[dict[int, Mapping[str, Any]]]:
        """读回一份**完整**产出(含 input_key 校验); 缺行/键不匹配/损坏 → None。"""
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                o = json.load(fh)
        except (OSError, ValueError):
            return None
        if (o.get("input_key") or "") != key:
            return None
        rows = o.get("rows") or []
        got = {r.get("xl_row"): r for r in rows
               if isinstance(r, Mapping) and r.get("xl_row") is not None}
        expected = {r.get("xl_row") for r in self.rows}
        return got if set(got) == expected and len(rows) == len(got) else None

    def produce(self, run_tag: str, retry_brief: Optional[str] = None):
        collected: dict[int, Mapping[str, Any]] = {}
        ckpt_path = self._ckpt_path(run_tag)
        raw_path = self._raw_path(run_tag)
        raw_key = self._input_key()          # 成品(raw)指纹: 只认输入, 不认 brief
        ckpt_key = self._input_key(retry_brief)  # 检查点指纹: 把 brief 一并计入
        # ① 主跑(非重跑)且上一轮已完整产出 → 直接复用, 不再重复调 LLM。
        if self.save_raw and retry_brief is None:
            done = self._load_completed(raw_path, raw_key)
            if done is not None:
                if self.log:
                    self.log("   %s 已有完整产出 %d/%d 行, 跳过(断点续跑)"
                             % (run_tag, len(done), len(self.rows)))
                return done
        # ② 断点续跑: 读回上次中断前已产出的行, 只跑剩下的。
        if self.save_raw:
            resumed = self._load_ckpt(ckpt_path, ckpt_key)
            expected = {r.get("xl_row") for r in self.rows}
            if not set(resumed).issubset(expected):
                resumed = {}
            if resumed:
                collected.update(resumed)
                if self.log:
                    self.log("   %s 断点续跑: 已读回 %d/%d 行, 只跑剩余 %d 行"
                             % (run_tag, len(collected), len(self.rows),
                                len(self.rows) - len(collected)))
        todo = [r for r in self.rows if r.get("xl_row") not in collected]
        if todo:
            n_chunks = (len(todo) + self.chunk - 1) // self.chunk
            chunks = [todo[ci * self.chunk:(ci + 1) * self.chunk] for ci in range(n_chunks)]
            use_workers = self.workers if n_chunks > 1 else 1
            if self.log:
                self.log("   %s 共 %d 批, 并发 %d, 开始产出" % (run_tag, n_chunks, use_workers))
            collected.update(self._produce_chunks(
                chunks, run_tag, retry_brief,
                base=collected, ckpt=(ckpt_path, ckpt_key) if self.save_raw else None))
            if self.log:
                self.log("   %s 主跑完成: %d/%d 行" % (run_tag, len(collected), len(self.rows)))
        # 缺行检查: 主跑漏掉的行按 1 行/次补跑, 最多补 3 轮(单行也可能偶发漏, 多补几轮兜底)
        missing = [r for r in self.rows if r.get("xl_row") not in collected]
        round_n = 0
        while missing and round_n < 3:
            round_n += 1
            if self.log:
                self.log("   %s 缺 %d 行, 逐行补跑第 %d 轮" % (run_tag, len(missing), round_n))
            collected.update(self._produce_chunks(
                [[r] for r in missing], run_tag, retry_brief,
                base=collected, ckpt=(ckpt_path, ckpt_key) if self.save_raw else None))
            missing = [r for r in self.rows if r.get("xl_row") not in collected]
        if missing and self.log:
            self.log("   %s 补跑 %d 轮后仍缺 %d 行" % (run_tag, round_n, len(missing)))
        if self.save_raw:
            os.makedirs(self.out_dir, exist_ok=True)
            self._save_ckpt(raw_path, raw_key, collected)
            if missing:
                # 仍缺行 → 保留检查点, 供下次续跑补完; raw 文件此时也不完整, 不会被 _load_completed 误当成品。
                if self.log:
                    self.log("   %s 仍缺 %d 行, 检查点已保留待续跑" % (run_tag, len(missing)))
            else:
                # 全部产出(含缺行兜底)完成 → 清掉检查点, 下次不再续跑旧进度。
                self._rm_ckpt(ckpt_path)
        if self.log:
            self.log("   %s 全部完成: %d/%d 行" % (run_tag, len(collected), len(self.rows)))
        return collected


# ================================================================ 输入读取
def load_rows(path: str) -> list[Mapping[str, Any]]:
    """读输入行。支持 `{"rows":[…]}` 或裸数组。"""
    with open(path, encoding="utf-8-sig") as fh:
        o = json.load(fh)
    rows = o if isinstance(o, list) else o.get("rows", [])
    if not isinstance(rows, list) or not rows:
        raise ValueError("输入无有效行：%s" % path)
    return rows


def load_run_files(out_dir: str, prefix: str) -> dict[str, dict[int, Mapping[str, Any]]]:
    """读一批运行文件（供离线闸门/一致性使用）。跳过非运行文件。"""
    import glob
    runs: dict[str, dict[int, Mapping[str, Any]]] = {}
    skip = ("input", "retry_brief")
    for p in sorted(glob.glob(os.path.join(out_dir, "%s*.json" % prefix))):
        tag = os.path.basename(p)[:-5]
        if tag in skip or tag.startswith(("drift_metrics", "gate_", "final_")):
            continue
        try:
            with open(p, encoding="utf-8-sig") as fh:
                o = json.load(fh)
        except Exception:
            continue
        rows = o if isinstance(o, list) else o.get("rows", [])
        if not isinstance(rows, list) or not rows:
            continue
        runs[tag] = {r.get("xl_row"): r for r in rows if isinstance(r, Mapping)}
    return runs


# ================================================================ 结果
@dataclass
class RoundResult:
    gates: dict[str, GateResult] = field(default_factory=dict)
    passed: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    retried: list[str] = field(default_factory=list)
    alone_rate: dict[str, float] = field(default_factory=dict)
    used_runs: list[str] = field(default_factory=list)
    metrics_honest: dict[str, Any] = field(default_factory=dict)   # 不剔除任何运行
    metrics_delivery: dict[str, Any] = field(default_factory=dict)  # 剔除语义离群后
    deliverable: list[dict[str, Any]] = field(default_factory=list)
    cells: dict[str, dict[str, Any]] = field(default_factory=dict)
    cells_majority: dict[str, dict[str, Any]] = field(default_factory=dict)
    needs_review: list[dict[str, Any]] = field(default_factory=list)
    # 2026-09-21 方案 A/B：非 closed 属性里出现的**词表外新词候选**
    #   [{"属性","词","出现次数","行号":[..],"policy","依据样本":[..]}]
    vocab_candidates: list[dict[str, Any]] = field(default_factory=list)
    quarantined_attributes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    source_review: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    conflict_adjudication: dict[str, Any] = field(default_factory=dict)
    """`needs_review`：交付值字段里出现不同提名的格（按提名数降序）——
    这就是"补充"的产物，多跑几份不是为得到更多格，而是把**口径分歧暴露出来**交人裁定。"""
    process_union: dict[str, list[Any]] = field(default_factory=dict)
    """`process_union`：过程记录字段（`冲突`/`跨列纠错`）的并集——
    各运行遇到的矛盾合并去重后的完整清单，交人工浏览（不参与逐格裁定）。"""

    def summary(self) -> str:
        L = []
        L.append("结构闸门：%d PASS / %d REJECT%s"
                 % (len(self.passed), len(self.rejected),
                    ("；重跑 %d 次" % len(self.retried)) if self.retried else ""))
        if self.rejected:
            L.append("  仍被拒：%s" % "、".join(self.rejected))
        if self.alone_rate:
            L.append("独异格率：%s"
                     % "  ".join("%s=%.2f%%" % (t, self.alone_rate[t])
                                 for t in sorted(self.alone_rate)))
        if self.used_runs:
            L.append("交付所用运行：%s" % "、".join(self.used_runs))
        for label, m in (("① 诚实指标", self.metrics_honest),
                         ("② 交付机制", self.metrics_delivery)):
            c = (m or {}).get("combined") or {}
            if c.get("rate") is not None:
                L.append("  %s（不剔除）合计 %.2f%%" % (label, c["rate"])
                         if label.startswith("①") else
                         "  %s（剔离群）合计 %.2f%%" % (label, c["rate"]))
        return "\n".join(L)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gates": {t: {"rows": g.rows, "errors": g.errors,
                          "warnings": g.warnings, "verdict": g.verdict,
                          "data_errors": g.data_errors,
                          "required_errors": g.required_errors,
                          "delivery_accepted": t in self.passed,
                          "delivery_status": "已纳入交付" if t in self.passed else "需修复或未纳入",
                          "nonblocking_errors": g.errors - g.required_errors,
                          "required_rows": len({i.xl_row for i in g.issues if i.level == "ERR" and i.required and i.xl_row is not None}),
                          "schema_errors": g.schema_errors,
                          "rows_with_err": g.rows_with_err,
                          "err_summary": g.err_summary,
                          "issues": [{"level": i.level, "xl_row": i.xl_row,
                                      "field": i.field, "why": i.why,
                                      "kind": i.kind, "required": i.required}
                                     for i in g.issues]}
                      for t, g in self.gates.items()},
            "passed": self.passed, "rejected": self.rejected,
            "retried": self.retried, "alone_rate": self.alone_rate,
            "used_runs": self.used_runs,
            "metrics_honest": self.metrics_honest,
            "metrics_delivery": self.metrics_delivery,
            "deliverable": self.deliverable,
            "needs_review": self.needs_review,
            "process_union": self.process_union,
            "cells": self.cells,
            "vocab_candidates": self.vocab_candidates,
            "quarantined_attributes": self.quarantined_attributes,
            "source_review": self.source_review,
            "conflict_adjudication": self.conflict_adjudication,
        }

    def write(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        return path


# ================================================================ 编排
def _normalize_attr_keys(rows, log=None, valid_names=None) -> int:
    """把 LLM **造出来的键名**确定性归一成 spec 的合法键（2026-10-23）。

    实测造的键（步骤17 a1/a2 的 required 错误来源）:
      `产品类型_来源2` / `产品类型_依据2` / `产品类型_dup` / `产品类型_dup_来源`
      `风速_来源_dup` / `温度_来源_依据` / `是否无线_依据补充` / `功率_功率`
      `重量_重量_依据` / `产品类型_dup_ignore` / `配件_依据`（名写短了）

    归一规则（全部确定性，**不依赖 LLM 遵守提示词**）:
      1. `X_来源_依据`                        → `X_依据`
      2. `X_来源<N>` / `X_依据<N>`            → `X_来源` / `X_依据`（多值用 `+` 合并）
      3. `X_来源补充` / `X_依据dup` 等         → `X_来源` / `X_依据`
      4. `X_来源_dup` / `X_依据_补充` 等       → `X_来源` / `X_依据`
      5. `X_dup_来源` / `X_ignore_依据` 等     → `X_来源` / `X_依据`
      6. `X_dup` / `X_补充` / `X_2` / `X_ignore` → `X`
      7. `X_来源_来源` / `X_依据_依据`         → `X_来源` / `X_依据`
      8. `X_X_依据`（两段同名 + 后缀）         → `X_依据`
      9. `X_X`（两段同名）                     → `X`
      10. 属性名写短了（`配件` ⊂ `配件数量`）→ 模糊匹配到 spec 的唯一长名

    valid_names: spec 里的合法属性名集合（用于第 10 条模糊匹配）。
    返回修复的键数。
    """
    import re as _re
    _K = r"(来源|依据)"
    _NOISE = r"(?:dup|extra|ignore|补充|补充说明|说明|2|3|4)"
    # (正则, 替换函数) —— 顺序敏感: 先处理"带后缀的双段", 再处理单段
    RULES = (
        (_re.compile(r"^(.+?)_来源_依据$"), lambda m: m.group(1) + "_依据"),
        (_re.compile(r"^(.+?)_(.+?)_%s$" % _K), None),          # 占位: X_Y_依据 (同名才处理)
        (_re.compile(r"^(.+?)_%s(\d+)$" % _K), lambda m: m.group(1) + "_" + m.group(2)),
        (_re.compile(r"^(.+?)_%s%s$" % (_K, _NOISE), _re.I), lambda m: m.group(1) + "_" + m.group(2)),
        (_re.compile(r"^(.+?)_%s_%s$" % (_K, _NOISE), _re.I), lambda m: m.group(1) + "_" + m.group(2)),
        (_re.compile(r"^(.+?)_%s_%s$" % (_NOISE, _K), _re.I), lambda m: m.group(1) + "_" + m.group(2)),
        (_re.compile(r"^(.+?)_%s$" % _NOISE, _re.I), lambda m: m.group(1)),
        (_re.compile(r"^(.+?)_%s_%s$" % (_K, _K)), lambda m: m.group(1) + "_" + m.group(2)),
    )
    names = {str(n).strip() for n in (valid_names or []) if str(n).strip()}

    def _fuzzy_long(short: str) -> str:
        """属性名写短了（`配件` ⊂ `配件数量`）时，映射到**唯一**的合法长名。"""
        if not names or not short or short in names:
            return short
        cands = [n for n in names if short in n]
        return cands[0] if len(cands) == 1 else short

    def _base_of(nk: str) -> str:
        for suf in ("_来源", "_依据"):
            if nk.endswith(suf):
                return nk[: -len(suf)]
        return nk

    fixed = 0
    for _xl, row in (rows or {}).items():
        if not isinstance(row, dict):
            continue
        at = row.get("attributes")
        if not isinstance(at, dict):
            continue
        new: dict = {}
        for k, v in at.items():
            key = str(k).strip()
            nk = key
            # 反复归一直到稳定（`产品类型_dup_ignore` 需要剥两层后缀）
            for _ in range(4):
                prev_nk = nk
                for rx, fn in RULES:
                    m = rx.match(nk)
                    if not m:
                        continue
                    if fn is None:
                        if m.group(1) == m.group(2):
                            nk = m.group(1) + "_" + m.group(3)
                        break
                    nk = fn(m)
                    break
                if nk == prev_nk:
                    segs = nk.split("_")
                    if len(segs) == 2 and segs[0] == segs[1]:
                        nk = segs[0]
                if nk == prev_nk:
                    break
            # 模糊属性名: `配件_依据` → `配件数量_依据`
            if names:
                base = _base_of(nk)
                long_base = _fuzzy_long(base)
                if long_base != base:
                    nk = long_base + nk[len(base):]
            if nk == key:
                new[key] = v
                continue
            fixed += 1
            if nk in new:
                prev = str(new[nk] or "")
                cur = str(v or "")
                if cur and cur not in prev:
                    new[nk] = (prev + "+" + cur) if prev else cur
            else:
                new[nk] = v
            if log:
                log("     键名归一: %s → %s" % (key, nk))
        row["attributes"] = new
    return fixed


def _ensure_source_basis_keys(rows, rules, log=None) -> int:
    """确定性补齐 `_来源`/`_依据` 键（2026-10-26 用户口径：没有来源和依据也要结构完整）。

    - 每个 spec 属性缺 `X_来源` → 补空串（没有来源就留空，闸门允许空串）。
    - 值为 `未明确`（证据不足）且缺/空 `X_依据` → 补「未明确（无证据）」。
    - 值是真实取值却缺 `X_依据` → **不补**（真实值必须有真依据，仍交闸门拦/定向修复）。
    - 值键本身缺失（漏属性）→ 不在这里补（那是截断/闸门要管的信号）。
    返回补齐的键数。
    """
    names = {str(n).strip() for n in (getattr(rules, "attrs", None) or {}) if str(n).strip()}
    filled = 0
    for _xl, row in (rows or {}).items():
        if not isinstance(row, dict):
            continue
        at = row.get("attributes")
        if not isinstance(at, dict):
            continue
        for name in names:
            if name not in at:          # 值键缺失 = 漏属性，不在这里补
                continue
            src_key = name + "_来源"
            if src_key not in at:
                at[src_key] = ""
                filled += 1
            basis_key = name + "_依据"
            if (basis_key not in at) or (not str(at.get(basis_key) or "").strip()):
                if str(at.get(name) or "").strip() in ("未明确", ""):
                    at[basis_key] = "未明确（无证据）"
                    filled += 1
    return filled


# `_来源` 常见别名 → 合法来源名（2026-10-27 用户口径：确定性归一，不重喂 LLM）
_SOURCE_ALIASES = {
    "标题": "标题", "title": "标题", "商品标题": "标题",
    "亮点": "亮点", "标题亮点": "亮点", "标题下亮点": "亮点",
    "五点描述": "五点描述", "五点": "五点描述", "要点": "五点描述",
    "bullet": "五点描述", "bullets": "五点描述", "五点图": "五点描述",
    "详细参数": "详细参数", "参数": "详细参数", "详情": "详细参数",
    "detail": "详细参数", "details": "详细参数", "spec": "详细参数", "specs": "详细参数",
    "sku": "SKU", "货号": "SKU",
    "图片ocr": "图片OCR", "ocr": "图片OCR", "图片文字": "图片OCR", "图片识别": "图片OCR", "图片": "图片OCR",
    "图片视觉": "图片视觉", "视觉": "图片视觉", "看图": "图片视觉",
}


def _normalize_sources(value) -> str:
    """把 `_来源` 的常见别名确定性归一成合法来源；未知的丢弃；全丢则空串。

    多来源分隔符兼容 `+` / `；` / `;`，输出保留原分隔符（没写就用 `+`）。
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    sep = "；" if "；" in value else "+"
    out: list[str] = []
    for p in re.split(r"[+；;]", value):
        k = p.strip().lower()
        if not k:
            continue
        canon = _SOURCE_ALIASES.get(k)
        if canon and canon not in out:
            out.append(canon)
    return sep.join(out)


def _normalize_attr_values(rows, rules, log=None) -> int:
    """确定性修复四个值级问题（2026-10-26 用户口径：闸门只记不修的，这里确定性补掉）。

    1. 空值写法归一: `null/None/无/—/-/N/A/na` → 空串（这些不该用来表示空）。
    2. `category_check.判定` 非法枚举 → 回退「属于本类目」（保守保留，不误删）。
    3. 辅助数组非数组 → 包成数组（`冲突`/`跨列纠错` 单对象→`[obj]`；`词表外词` 单串→`[str]`；其余→`[]`）。
    4. `_来源` 别名归一 + 未知清空（2026-10-27 用户口径：确定性归一，不重喂 LLM）。
    返回修复的格数。
    """
    _NEG_EMPTY = {"null", "None", "无", "—", "-", "N/A", "na"}
    _verdict_ok = set(getattr(rules, "verdict_values", None) or {"属于本类目", "不属于本类目"})
    _default_verdict = "属于本类目" if "属于本类目" in _verdict_ok else next(iter(sorted(_verdict_ok)))
    fixed = 0
    for _xl, row in (rows or {}).items():
        if not isinstance(row, dict):
            continue
        # 1) 空值写法归一（只动值键，不动 _来源/_依据）
        at = row.get("attributes")
        if isinstance(at, dict):
            for k in list(at.keys()):
                if isinstance(k, str) and k.endswith(("_来源", "_依据")):
                    continue
                v = at.get(k)
                if isinstance(v, str) and v.strip() in _NEG_EMPTY:
                    at[k] = ""
                    fixed += 1
            # 4) `_来源` 别名归一 + 未知清空
            for k in list(at.keys()):
                if isinstance(k, str) and k.endswith("_来源"):
                    old = at.get(k)
                    old_s = "" if old is None else str(old).strip()
                    new = _normalize_sources(old_s)
                    if new != old_s:
                        at[k] = new
                        fixed += 1
        # 2) category_check.判定 非法枚举 → 回退默认
        cc = row.get("category_check")
        if isinstance(cc, dict):
            j = str(cc.get("判定") or "").strip()
            if j and j not in _verdict_ok:
                cc["判定"] = _default_verdict
                fixed += 1
        # 3) 辅助数组非数组 → 包成数组
        for key in ("冲突", "跨列纠错", "词表外词"):
            v = row.get(key)
            if isinstance(v, list):
                continue
            if key == "词表外词":
                row[key] = [v] if isinstance(v, str) and v.strip() else []
            else:
                row[key] = [v] if isinstance(v, dict) else []
            fixed += 1
    return fixed


def _quality_errors_by_row(gate) -> dict:
    """只取「词表越界 / 来源标签非法」的行级错误（kind=quality），供质量修复用。

    与 `_errors_by_row` 的区别：那一个取**所有**报错（含完整性）；这一个只取
    质量类错误（词表越界、来源标签非法），用于"越界/非法再喂 LLM 确认"的定向修复。
    """
    out: dict = {}
    for it in (getattr(gate, "issues", None) or []):
        rid = getattr(it, "xl_row", None)
        why = str(getattr(it, "why", "") or "")
        if rid is None or not why:
            continue
        if getattr(it, "kind", None) == ERR_QUALITY:
            out.setdefault(rid, []).append(why)
    return out


def run_round(
    producer: Producer,
    rules: RuleSource,
    *,
    tags: Optional[Sequence[str]] = None,
    n_runs: int = 5,
    input_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    max_alone: Optional[float] = 10.0,
    max_retry: int = 2,
    brief_dir: Optional[str] = None,
    output_template: str = "",
    input_file: str = "",
    strict_gate: bool = True,
    require_complete: bool = False,
    require_basis: Optional[bool] = None,
    resolve_conflicts: bool = False,
    log: Callable[[str], None] = print,
) -> RoundResult:
    """跑完一整轮闭环。

    producer.produce 与 producer.path 中，运行标识形如 `a1`/`a2`…（避免与
    `r4_1` 之类既有命名混淆，命名为 `tag`）。tags 可显式指定。

    strict_gate=False（步骤17 用户诉求「只把属性值跑出来，不被结构限制影响」）：
    默认只记录结构问题；require_complete=True 时，缺行/漏属性/漏来源/漏依据
    仍须补跑或剔除，其余格式与语义问题保持宽松。
    """
    tags = list(tags or ["a%d" % (i + 1) for i in range(n_runs)])
    inp = ({r.get("xl_row"): r for r in input_rows} if input_rows else None)
    res = RoundResult()

    # 方案 A/B：跨运行聚合"新词候选"（属性+词 为键，累计出现次数/行号/依据样本）
    _vc: dict[tuple, dict] = {}

    def _collect_vocab(items):
        for it in items:
            k = (it.get("属性"), it.get("词"))
            e = _vc.setdefault(k, {"属性": it.get("属性"), "词": it.get("词"),
                                   "出现次数": 0, "行号": set(),
                                   "policy": it.get("policy"), "依据样本": []})
            e["出现次数"] += 1
            if it.get("行号") is not None:
                e["行号"].add(it["行号"])
            ev = it.get("依据")
            if ev and ev not in e["依据样本"] and len(e["依据样本"]) < 3:
                e["依据样本"].append(ev)

    # ---- 1/2 产出 + 闸门
    # 单份运行失败隔离(参考 steps/_filtering.judge_batch 的"失败不炸"约定):
    # 一次 LLM 调用失败(超时/连接/429/503)只废掉这一份运行 → 标 REJECT 进重跑档,
    # 不让异常穿透到步骤层把整步干掉; 其余 N-1 份照常产出。
    produced: dict[str, dict[int, Mapping[str, Any]]] = {}

    def _errors_by_row(gate) -> dict:
        """把闸门 issues 按行号聚合，供 `repair_rows` 用（2026-10-25）。"""
        out: dict = {}
        for it in (getattr(gate, "issues", None) or []):
            rid = getattr(it, "xl_row", None)
            why = str(getattr(it, "why", "") or "")
            if rid is None or not why:
                continue
            out.setdefault(rid, []).append(why)
        return out

    def _accepted(t):
        return t in produced and (
            res.gates[t].verdict == "PASS" if strict_gate
            else not (require_complete and res.gates[t].required_errors))

    def _normalize(t: str, rows):
        # ★ 2026-10-23: 产出一落地就做**键名归一**（造键是闸门 required 错误的头号来源）。
        #   放在这里而不是闸门之后 —— 闸门是按"合法键"判的, 归一必须在它之前。
        #   `valid_names` 用于第 10 条模糊匹配（`配件_依据` → `配件数量_依据`）。
        try:
            _known = {str(n) for n in (getattr(rules, "attrs", None) or {})}
            _nf = _normalize_attr_keys(rows, log=log, valid_names=_known)
            if _nf:
                log("   %-6s 键名归一: 修复 %d 个自造键（如 X_来源2 → X_来源）" % (t, _nf))
            isolated = quarantine_unknown_attributes(rows, _known)
            if isolated:
                res.quarantined_attributes.setdefault(t, []).extend(isolated)
                log("   %-6s 隔离未定义属性: %d 行（原始结果保留，可审查）" % (t, len(isolated)))
            _nv = _normalize_attr_values(rows, rules, log=log)
            if _nv:
                log("   %-6s 值归一: 修复 %d 处（空值写法/category枚举/辅助数组）" % (t, _nv))
            _nk = _ensure_source_basis_keys(rows, rules, log=log)
            if _nk:
                log("   %-6s 补键: 补齐 %d 个来源/依据键（缺来源→空串, 未明确缺依据→说明）" % (t, _nk))
            fixed_sources, source_review = fill_missing_sources(rows, inp, _known)
            res.source_review[t] = source_review
            if fixed_sources or source_review:
                log("   %-6s 依据定位来源: 补齐 %d 格，无法唯一定位 %d 格待复核" % (t, fixed_sources, len(source_review)))
        except Exception as exc:  # noqa: BLE001
            log("   %-6s ⚠ 键名归一失败(忽略): %s" % (t, str(exc)[:120]))
        # ★ 2026-10-25: **不再在这里逐行补漏**（`fill_missing`）—— 实测 Cat Fountains
        #   607 行里有 408 行有缺口 → 408 次单行调用，花了 ~13 分钟、占了总费用的 1/3。
        #   现在由**闸门之后**的 `repair_rows`（12 行/批）统一处理：它既补漏属性，
        #   也修"未定义属性 / 词表外值 / 漏依据"等所有 required 错误，而且批量。
        return rows

    def _produce(t: str, brief=None):
        try:
            rows = producer.produce(t, retry_brief=brief)
        except Exception as exc:  # noqa: BLE001 — 隔离单份运行失败, 不炸整步
            return None, str(exc)[:200]
        return _normalize(t, rows), None

    log("【1】产出 %d 份运行并过结构闸门" % len(tags))
    for t in tags:
        rows, err = _produce(t)
        if err:
            log("   %-6s 产出失败(%s) → 标 REJECT, 进入重跑档" % (t, err))
            res.gates[t] = GateResult(tag=t, rows=0, errors=1,
                                      issues=[GateIssue("ERR", None, "(产出)", err, required=True)])
            continue
        produced[t] = rows
        nv: list = []
        g = gate_run([rows[k] for k in sorted(rows)], rules, inp, tag=t,
                     new_vocab=nv, require_basis=require_basis)
        _collect_vocab(nv)
        res.gates[t] = g
        accepted = _accepted(t)
        log("   %-6s %4d 行  必修 %d 条 / %d 行 · 其他问题 %d 条 · 提醒 %d 条 → %s"
            % (t, g.rows, g.required_errors,
               len({i.xl_row for i in g.issues if i.level == "ERR" and i.required and i.xl_row is not None}),
               g.errors - g.required_errors, g.warnings, "已纳入交付" if accepted else "需修复或未纳入"))

    # ---- 3/4 只重跑被拒的（最多 max_retry 轮）
    # 定向修复每轮内最多循环几次（2026-10-26 用户口径：先多轮定向修复，仍残留 required 才整份重跑）
    _REPAIR_MAX_ROUNDS = 2
    for attempt in range(1, max_retry + 1):
        bad = [t for t in tags if not _accepted(t)] if (strict_gate or require_complete) else []
        if not bad:
            break
        log("【%d】重跑被拒的 %d 个：%s" % (2 + attempt, len(bad), "、".join(bad)))
        for t in bad:
            # 漏属性可能是静默截断或模型漏写，先把 chunk 减半降低输出负担。
            #   实测 Dog Dryer: 51 属性 × 12 行/批 → 单次输出 ~15,952 tokens（正好撞 max_tokens），
            #   LLM 写到「尺寸」时 token 用尽 → 24 行整列缺失（连来源/依据键一起没有）。
            #   这种"整列缺失"不是 LLM 判断错，**原样重跑还会再截断**，必须缩小批次。
            #   已有的 `_produce_chunk` 只在"抛 LLMTruncatedError"时拆批；而静默截断不抛异常，
            #   所以这里用闸门的"漏属性"信号补上这个触发点。
            if isinstance(producer, CallableProducer):
                g0 = res.gates.get(t)
                miss = [i for i in ((g0.issues if g0 else []) or [])
                        if "漏属性" in str(getattr(i, "why", "") or "")]
                if miss:
                    old_chunk = int(getattr(producer, "chunk", 0) or 0)
                    if old_chunk > 1:
                        new_chunk = max(1, old_chunk // 2)
                        producer.chunk = new_chunk
                        log("   %-6s 漏属性 %d 条(疑似输出截断) → chunk %d → %d 后重跑"
                            % (t, len(miss), old_chunk, new_chunk))
            # ★ 2026-10-25 用户口径: **先定向修复出错的行**（修好就放行），而不是整份重跑。
            #   整份重跑 607 行 = 12 元 / 20 分钟；定向修复只重做"报错的那几十行"。
            # ★ 2026-10-26 用户口径: 定向修复**循环多轮**（每轮只重做报错行），
            #   连续 _REPAIR_MAX_ROUNDS 轮都还残留 required 错误时才整份重跑兜底。
            _repaired = False
            if isinstance(producer, CallableProducer) and hasattr(producer, "repair_rows"):
                for _r in range(_REPAIR_MAX_ROUNDS):
                    if _accepted(t):
                        _repaired = True
                        break
                    _errs = _errors_by_row(res.gates.get(t))
                    if not _errs:
                        break        # 没有行级错误可修（run 级错误交给整份重跑）
                    log("   %-6s 定向修复第 %d 轮: %d 行报错 → 只重做这些行（不整份重跑）"
                        % (t, _r + 1, len(_errs)))
                    try:
                        # 针对缺失键只补缺口，保留同一行其他已有字段。
                        # 其他类型错误不能靠整行重建安全修复，留待明确报错。
                        import ast
                        import re
                        missing_map = {}
                        for rid, issues in _errs.items():
                            names = set()
                            for why in issues:
                                match = re.search(r"(?:漏(?:属性|来源键|依据键)|依据键为空|属性值为空)\s*(\[[^\]]*\])", why)
                                if not match:
                                    continue
                                try:
                                    keys = ast.literal_eval(match.group(1))
                                except (ValueError, SyntaxError):
                                    continue
                                if not isinstance(keys, list):
                                    continue
                                for key in keys:
                                    name = str(key)
                                    for suffix in ("_来源", "_依据"):
                                        if name.endswith(suffix):
                                            name = name[:-len(suffix)]
                                            break
                                    if name in rules.attrs:
                                        names.add(name)
                            if names:
                                missing_map[rid] = sorted(names)
                        if not missing_map:
                            log("   %-6s 没有可安全补齐的缺失字段，保留原结果并报告剩余错误" % t)
                            break
                        produced[t] = producer.fill_missing(produced[t], missing_map, run_tag=t)
                        _normalize(t, produced[t])
                        _nv2: list = []
                        _g2 = gate_run([produced[t][k] for k in sorted(produced[t])], rules, inp,
                                       tag=t, new_vocab=_nv2, require_basis=require_basis)
                        _collect_vocab(_nv2)
                        res.gates[t] = _g2
                        log("   %-6s 定向修复后 %-40s → %s"
                            % (t, _g2.err_summary or "ERR 0", _g2.verdict))
                    except Exception as exc:  # noqa: BLE001
                        log("   %-6s ⚠ 定向修复失败(继续整份重跑): %s" % (t, str(exc)[:100]))
                        break
                if _accepted(t):
                    _repaired = True
            if _repaired:
                continue        # 定向修复修好了 → 不再整份重跑
            if t in produced and isinstance(producer, CallableProducer):
                log("   %-6s 仍有结构错误，保留已保存结果；只修错误部分，不整份重跑" % t)
                continue
            brief = build_retry_brief(
                t, res.gates[t],
                output_file=(output_template % {"tag": t}) if output_template
                else "%s.json" % t,
                input_file=input_file)
            if brief_dir:
                os.makedirs(brief_dir, exist_ok=True)
                bp = os.path.join(brief_dir, "retry_brief_%s.md" % t)
                with open(bp, "w", encoding="utf-8") as fh:
                    fh.write(brief)
                log("   已写重跑指令 %s" % bp)
            rows, err = _produce(t, brief)
            res.retried.append(t)
            if err:
                log("   %-6s 重跑产出失败(%s) → 仍 REJECT" % (t, err))
                res.gates[t] = GateResult(tag=t, rows=0, errors=1,
                                          issues=[GateIssue("ERR", None, "(产出)", err, required=True)])
                continue
            produced[t] = rows
            nv: list = []
            g = gate_run([rows[k] for k in sorted(rows)], rules, inp, tag=t,
                         new_vocab=nv, require_basis=require_basis)
            _collect_vocab(nv)
            res.gates[t] = g
            log("   %-6s 重跑后 %-40s → %s"
                % (t, g.err_summary or "ERR 0", g.verdict))

    # ---- 质量错误**兜底**：确定性归一 + 闸门放行之后，仍残留的零星越界/非法 → 重喂 LLM ----
    # 2026-10-27 用户口径: 重喂大模型是「特殊情况」的**兜底行为** ——
    #   · 未知来源 → 已在 `_normalize_attr_values` 里别名归一(映射不到则清空)，不在此重喂;
    #   · 数值/组合/自由文本的词表越界 → 已在闸门直接放行，不在此重喂;
    #   · 剩下的零星越界(材质/香型/颜色等) / 其他特殊非法 → 才重喂 LLM 确认,
    #     最多 2 次，仍不过则记入日志、**不拦交付**。
    _QUALITY_MAX_RETRY = 2
    if isinstance(producer, CallableProducer) and hasattr(producer, "repair_rows"):
        for t in list(produced.keys()):
            for attempt in range(_QUALITY_MAX_RETRY):
                errs = _quality_errors_by_row(res.gates.get(t))
                if not errs:
                    break
                log("   %-6s 质量问题 %d 行，保留原值并记录，避免重写整行" % (t, len(errs)))
                break
            _left = _quality_errors_by_row(res.gates.get(t))
            if _left:
                log("   %-6s 质量兜底结束: 仍剩 %d 行词表越界/来源标签非法（记入闸门日志，不拦交付）"
                    % (t, len(_left)))

    res.passed = [t for t in tags if _accepted(t)]
    res.rejected = [t for t in tags if not _accepted(t)]
    if len(res.passed) < 2:
        log("⚠ 通过闸门的运行不足 2 个，无法交付。")
        return res

    # ---- 5/6 稳定性筛选 + 多数表决（**只统计通过闸门的运行**）
    runs: dict[str, dict[int, Mapping[str, Any]]] = {}
    for t in res.passed:
        if isinstance(producer, FileProducer):
            rows = producer_produce_safe(producer, t)   # 文件可能被外部覆盖, 重读
        else:
            rows = produced[t]   # 复用已产出结果, 不重复调 LLM(省 token, 也避免二次失败)
        runs[t] = rows

    res.alone_rate = {t: round(v, 4) for t, v in alone_rates(runs, res.passed).items()}
    used = [t for t in res.passed
            if max_alone is None or res.alone_rate[t] <= max_alone]
    if len(used) < 2:
        log("⚠ 稳定性筛选后不足 2 个运行，改用全部通过闸门的运行。")
        used = list(res.passed)
    res.used_runs = used

    log("【%d】独异格率（>%.1f%% 视为语义离群，不纳入交付）"
        % (3 + max_retry, max_alone or 0))
    for t in res.passed:
        log("   %-6s %6.2f%%%s" % (t, res.alone_rate[t],
                                   "   ← 语义离群" if t not in used else ""))

    res.metrics_honest = consistency(runs, res.passed)
    res.metrics_delivery = consistency(runs, used)
    # **并集口径用于交付**：多运行的目的是「补充属性值」——一次标注提不全，
    # 多跑几份把漏掉的收进来。取并集才不会丢少数提名；
    # 多数表决（cells_majority）只保留为**稳定性指标**，不再用于交付。
    res.cells = union_cells(runs, used)
    if resolve_conflicts and isinstance(producer, CallableProducer) and len(used) == 2:
        from .conflict_adjudication import adjudicate
        try:
            res.conflict_adjudication = adjudicate(res.cells, runs, producer, rules, log)
            log("冲突第三次判断：待判 %(target_cells)d 格，已完成 %(completed_cells)d 格，达成多数 %(resolved_cells)d 格"
                % res.conflict_adjudication)
        except Exception as exc:
            res.conflict_adjudication = {"error": str(exc)}
            log("⚠ 冲突复核未完成，未裁定的分歧保留待复核: %s" % str(exc)[:160])
    res.cells_majority = majority(runs, used)
    rows_ids = sorted(set().union(*[set(r) for r in runs.values()]))
    res.deliverable = deliverable_from_cells(res.cells, rows_ids)
    res.needs_review = review_queue(res.cells)
    res.process_union = process_union(res.cells)
    res.vocab_candidates = [
        {**e, "行号": sorted(e["行号"])} for e in _vc.values()
    ]
    res.vocab_candidates.sort(key=lambda c: -c["出现次数"])

    log("【交付】")
    h = res.metrics_honest["combined"]["rate"]
    d = res.metrics_delivery["combined"]["rate"]
    log("   ① 诚实指标（全部 %d 个通过闸门的运行）合计 %.2f%%" % (len(res.passed), h))
    log("   ② 交付机制（剔除离群后 %d 个：%s）合计 %.2f%%"
        % (len(used), "、".join(used), d))
    log("   ⚠ ① 是指标、② 是机制，报告时必须分开写，不可互相替代")
    dv = res.metrics_delivery.get("value") or {}
    if dv.get("rate") is not None:
        log("   交付值字段一致率（②口径）%.2f%%（%d/%d）"
            % (dv["rate"], dv["ok"], dv["n"]))

    # 召回量化：并集 vs 多数表决 —— 用**同一口径**比（"给出真值"的格数），
    # 不能拿"并集有明确多数"去比"多数表决要求全体一致"，那是两回事。
    _EMPTY = {"未明确", "不适用", "未知", "待确认", "", "None", "nan"}

    def _real(v):
        if isinstance(v, list):
            return len(v) > 0
        if v is None:
            return False
        if isinstance(v, bool):
            return True
        return str(v).strip() not in _EMPTY

    n_union = n_maj = 0
    for key, d in res.cells.items():
        if d["kind"] != "value":
            continue
        if any(_real(g["value"]) for g in d["values"]):
            n_union += 1
        if d["agree"] and _real(d["mode"]):
            n_maj += 1
    log("【召回】并集：%d 格有真值 ／ 要求全体一致：%d 格 → 多收 %d 格（+%.1f%%）"
        % (n_union, n_maj, n_union - n_maj,
           100.0 * (n_union - n_maj) / max(n_maj, 1)))
    log("【待裁定】交付值字段有 %d 格出现不同提名（其他 %d 格是过程记录，取并集交人工浏览）"
        % (len(res.needs_review), len(res.process_union)))
    if res.vocab_candidates:
        log("【新词候选】非 closed 属性出现 %d 个词表外词（见 vocab_candidates）"
            % len(res.vocab_candidates))
        for c in res.vocab_candidates[:8]:
            log("    %-16s「%s」×%d 行%s（%s）"
                % (c["属性"], c["词"], c["出现次数"],
                   c["行号"][:6], c.get("policy") or "closed"))
    for r in res.needs_review[:6]:
        log("   行%-5s %-22s %s"
            % (r["行号"], r["字段"],
               " ／ ".join(str(g["值"])[:22] for g in r["提名"])))
    if res.metrics_delivery.get("drift_by_field"):
        log("   残余分歧最多字段：%s"
            % "、".join("%s(%d)" % (k, v) for k, v
                        in list(res.metrics_delivery["drift_by_field"].items())[:8]))
    return res


def producer_produce_safe(producer: FileProducer, tag: str):
    """FileProducer 重读文件（重跑后内容会变，必须重读而不是缓存）。"""
    return producer.produce(tag)


# ================================================================ CLI（离线）
def main() -> None:
    ap = argparse.ArgumentParser(description="属性标注：结构闸门 + 多运行一致性")
    ap.add_argument("--cat-dir", default="categories", help="类目配置目录")
    ap.add_argument("--cat", default=None, help="类目 ID（省略则取目录下唯一一个）")
    ap.add_argument("--input", default="", help="输入行 json（用于交叉校验与交付行集合）")
    ap.add_argument("--out-dir", default="results/llm_only", help="运行文件所在目录")
    ap.add_argument("--prefix", default="r4", help="运行文件前缀")
    ap.add_argument("--tags", default="", help="显式指定运行标识，逗号分隔")
    ap.add_argument("--max-alone", type=float, default=10.0,
                    help="独异格率阈值（%）超过则不纳入交付；<=0 表示不过滤")
    ap.add_argument("--out", default="", help="结果 json 输出路径")
    ap.add_argument("--check-only", action="store_true", help="只跑闸门，不做一致性")
    a = ap.parse_args()

    rules = RuleSource.load(a.cat_dir, a.cat)
    lint = rules.lint()
    if lint:
        print("⚠ 类目配置自检发现问题：")
        for x in lint:
            print("   - %s" % x)
    rows = load_rows(a.input) if a.input else None
    inp = {r.get("xl_row"): r for r in rows} if rows else None

    runs = load_run_files(a.out_dir, a.prefix)
    if not runs:
        print("未找到运行文件：%s/%s*.json" % (a.out_dir, a.prefix))
        return
    tags = [t for t in a.tags.split(",") if t] if a.tags else sorted(runs)

    print("=" * 78)
    print("结构闸门 —— 类目 %s（属性 %d，明细类名 %d，全部从配置读取）"
          % (a.cat or "(唯一)", len(rules.attrs), len(rules.detail_classes)))
    print("=" * 78)
    gates: dict[str, GateResult] = {}
    print("%-10s %5s %6s %6s %8s" % ("运行", "行数", "ERR", "WARN", "判定"))
    print("-" * 78)
    for t in tags:
        g = gate_run([runs[t][k] for k in sorted(runs[t])], rules, inp, tag=t)
        gates[t] = g
        print("%-10s %5d %6d %6d %8s" % (t, g.rows, g.errors, g.warnings, g.verdict))
    passed = [t for t in tags if gates[t].verdict == "PASS"]
    rejected = [t for t in tags if gates[t].verdict == "REJECT"]
    print("-" * 78)
    print("通过 %d：%s" % (len(passed), "、".join(passed) or "无"))
    print("拒绝 %d：%s" % (len(rejected), "、".join(rejected) or "无"))
    for t in rejected:
        print()
        print("【%s 违规明细】" % t)
        for i in gates[t].err_issues[:20]:
            print("   行%-5s %-24s %s" % (i.xl_row, i.field, i.why[:70]))
    if a.check_only or len(passed) < 2:
        return

    ar = alone_rates(runs, passed)
    used = [t for t in passed if a.max_alone <= 0 or ar[t] <= a.max_alone]
    if len(used) < 2:
        used = list(passed)
    print()
    print("=" * 78)
    print("一致性（只用通过闸门的 %d 个运行）" % len(passed))
    print("=" * 78)
    print("独异格率：%s" % "  ".join("%s=%.2f%%" % (t, ar[t]) for t in passed))
    print("纳入交付：%s%s" % ("、".join(used),
                          "" if len(used) == len(passed) else "（其余为语义离群）"))
    for label, sub in (("① 诚实指标（不剔除）", passed),
                       ("② 交付机制（剔离群）", used)):
        m = consistency(runs, sub)
        c = m["combined"]
        v = m["value"]
        print("  %-20s 合计 %6.2f%%   交付值字段 %6.2f%%  (%d/%d)"
              % (label, c["rate"], v["rate"], v["ok"], v["n"]))
    m = consistency(runs, used)
    if m.get("drift_by_field"):
        print("  残余分歧最多字段：%s"
              % "、".join("%s(%d)" % (k, v) for k, v
                          in list(m["drift_by_field"].items())[:10]))

    # ---- 并集口径（交付用）+ 待裁定队列（多运行的真正产物）
    cells = union_cells(runs, used)
    q = review_queue(cells)
    pu = process_union(cells)
    _EMPTY = {"未明确", "不适用", "未知", "待确认", "", "None", "nan"}

    def _real(v):
        if isinstance(v, list):
            return len(v) > 0
        if v is None:
            return False
        if isinstance(v, bool):
            return True
        return str(v).strip() not in _EMPTY

    nu = sum(1 for d in cells.values()
             if d["kind"] == "value" and any(_real(g["value"]) for g in d["values"]))
    nm = sum(1 for d in cells.values()
             if d["kind"] == "value" and d["agree"] and _real(d["mode"]))
    # 关键基线：并集 vs **最好单次** —— 这才是"多跑几份到底多补了多少"
    per_run = []
    for t in used:
        n = 0
        for key, d in cells.items():
            if d["kind"] != "value":
                continue
            by = [g for g in d["values"] if t in g["by"]]
            if by and _real(by[0]["value"]):
                n += 1
        per_run.append((t, n))
    best_t, best_n = max(per_run, key=lambda x: x[1]) if per_run else ('—', 0)
    print()
    print("【召回】交付值字段有真值的格数：")
    print("       各单次：%s" % "  ".join("%s=%d" % (t, n) for t, n in per_run))
    print("       最好单次 %s=%d ／ 并集 **%d** → **多补 %d 格（+%.1f%%）**"
          % (best_t, best_n, nu, nu - best_n, 100.0 * (nu - best_n) / max(best_n, 1)))
    print("       （注意：不要拿「全体一致=%d」当召回基线 —— 那是「多数表决会丢多少」，"
          "与「多补多少」是两回事）" % nm)
    print("【待裁定】受控值字段（`标准`）%d 格出现不同提名 —— 这才是人该看的口径分歧"
          % len(q))
    print("       其中疑似仅写法差异 %d 格；过程记录 %d 个字段取并集交人工浏览"
          % (sum(1 for r in q if r["疑似仅写法差异"]), len(pu)))
    for r in q[:10]:
        print("   行%-5s %-24s %s"
              % (r["行号"], r["字段"],
                 " ／ ".join(str(g["值"])[:22] for g in r["提名"])))

    if a.out:
        out = {
            "gates": {t: {"errors": gates[t].errors, "warnings": gates[t].warnings,
                          "verdict": gates[t].verdict} for t in tags},
            "passed": passed, "rejected": rejected,
            "alone_rate": {t: round(ar[t], 4) for t in passed},
            "used_runs": used,
            "metrics_honest": consistency(runs, passed),
            "metrics_delivery": m,
            "deliverable": deliverable_from_cells(
                cells, sorted(set().union(*[set(runs[t]) for t in used]))),
            "needs_review": q,
            "process_union": pu,
            "cells": cells,
        }
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print()
        print("结果已写入 %s" % a.out)


if __name__ == "__main__":
    main()
