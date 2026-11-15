"""FR-03 榜单详情过滤(分批 LLM 判定)。

输入: FR-01 榜单详情(仅 ok=True 行, 每行 {request_asin, title, bullets, ...})
      + FR-02 类目过滤提示词 filter-<CAT>-prompt.json
输出: 过滤后的榜单详情明细
      <CAT>-dual-details-filtered.jsonl(保留行, 原字段 + keep:true + reason)
      <CAT>-filtered-dual.txt(每行一个保留 ASIN)

逻辑: 按 BATCH_FILTER(默认 10 = 批判定起点, 失败/漏判降档重试) -> 复用
      steps._filtering.judge_batch -> LLM 判定(相关词优先规则) -> 保留行写明细
容错: 提示词缺失 -> 全保留(不误删, reason="过滤提示词缺失, 默认保留");
      整批失败/漏判缺条 -> 降档: 集中重发(同批) -> 拆 2 条/批 -> 1 条/批逐条
      (每条独立重试 FILTER_JUDGE_TRIES 次, 默认 3); 多次仍失败 ->
      FILTER_JUDGE_FALLBACK 兜底(默认 keep: 保留标"待复核", 不误杀)
"""
from __future__ import annotations

import json

from core import (
    atomic_write_text,
    read_json,
    read_jsonl,
    require_file,
    warn_missing,
    write_txt_lines,
)
from steps._filtering import judge_batch
from steps.base import BaseStep, StepContext


class Step03FilterDetails(BaseStep):
    number = 3
    title = "榜单详情过滤"
    description = "FR-03: 分批 LLM 判定剔除不符合类目行, 输出过滤后榜单详情明细"

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        details = ctx.resolve_artifact(f"{self._cat}-dual-details.jsonl")
        require_file(details, "FR-01 榜单详情")

    @property
    def artifacts(self) -> list[str]:
        return [
            f"{self._cat}-dual-details-filtered.jsonl",
            f"{self._cat}-filtered-dual.txt",
        ]

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        self.log(ctx, f"开始榜单详情过滤: CAT={cat}")

        details_path = ctx.resolve_artifact(f"{cat}-dual-details.jsonl")
        filter_path = ctx.resolve_artifact(f"filter-{cat}-prompt.json")
        out_jsonl = ctx.resolve_artifact(self.artifacts[0])
        out_txt = ctx.resolve_artifact(self.artifacts[1])

        records = [r for r in read_jsonl(details_path) if r.get("ok")]
        self.log(ctx, f"读取详情记录 {len(records)} 条(仅 ok=True)")

        if not records:
            self.log(ctx, "无 ok 记录, 写出空过滤产物")
            atomic_write_text(out_jsonl, "")
            write_txt_lines(out_txt, [])
            return

        filter_prompt = None
        if warn_missing(filter_path, "FR-02 类目过滤提示词"):
            filter_prompt = read_json(filter_path)
            if not filter_prompt:
                self.log(ctx, "过滤提示词为空, 判定时全保留(不误杀)")

        batch_size = max(1, int(ctx.config.get("BATCH_FILTER", 10) or 10))
        tries = max(1, int(ctx.config.get("FILTER_JUDGE_TRIES", 3) or 3))
        fallback_keep = str(ctx.config.get("FILTER_JUDGE_FALLBACK", "keep")
                            or "keep").strip().lower() != "drop"
        workers = max(1, int(ctx.config.get("FILTER_JUDGE_WORKERS", 3) or 3))
        kept, dropped = judge_batch(ctx.llm(), filter_prompt, records, batch_size,
                                    log=lambda text: self.log(ctx, text),
                                    max_tries=tries, fallback_keep=fallback_keep,
                                    workers=workers)
        self.log(ctx, f"过滤完成: 保留 {len(kept)} 条, 剔除 {len(dropped)} 条"
                      f"(BATCH_FILTER={batch_size} 判定模式="
                      f"{'逐条' if batch_size <= 1 else '阶梯批(失败降档至1条/批)'} "
                      f"并发{workers} 重试{tries}次 失败兜底={'保留待复核' if fallback_keep else '剔除'})")

        if kept and all(r.get("reason") == "保留" for r in kept):
            self.log(ctx, "告警: LLM 判定全部失败, 已按全保留兜底(不误杀)")

        lines = [json.dumps(r, ensure_ascii=False) for r in kept]
        atomic_write_text(out_jsonl, "\n".join(lines) + "\n" if lines else "")
        asins = [str(r.get("request_asin", "")).strip() for r in kept]
        asins = [a for a in asins if a]
        write_txt_lines(out_txt, asins)
        self.log(ctx, f"过滤明细已写入: {out_jsonl}")
        self.log(ctx, f"保留 ASIN 已写入: {out_txt}")
