"""FR-04 生成关键词(融合版: 登记根词 + LLM 发现 + 词库验证 + 语义审核)。

输入: FR-03 过滤后榜单详情 <CAT>-dual-details-filtered.jsonl(必选)
      + FR-02 产品口径/排除词 filter-<CAT>-prompt.json(可选, 缺失回退通用口径)
      + 配置(站点/月份/密钥) + tools/kwgen_hybrid/registered_roots.json(登记根词)
输出: <CAT>-keywords-candidates.txt(核心候选, 最多 CANDIDATE_LIMIT 个默认 20; 每行一个词)
      <CAT>-keywords.txt(分区: [核心大词] = 全部核心候选 + [长尾词] = 每维 LONGTAIL_PER_DIM 条)
      <CAT>-keywords-entities.txt(五维高频实体台账: 实体/商品数/出现次数/来源/对应长尾词)
      + <CAT>-kwgen-summary.json / <CAT>-kwgen-candidates.csv(融合版审计件, 不入下游消费)

流程: 登记根词(按类目+站点, 不绑月份) + LLM 提取(产品范围/多语言根词/候选/短实体)
      → 卖家精灵 keyword_miner 逐根词召回(默认 3 页) → LLM 候选+Unicode 重叠短语补漏
      → keywordList 精准验证(只留正搜索量) → LLM 语义审核(本类目/邻类目/配件/品牌/五维)
      → 核心词按搜索量排序 → 长尾从真实词按语义选(不改写、不硬凑、不足不补泛词)。

输出与旧正式版**完全同构**: 复用同一套写盘与实体台账渲染(见 _entity_report_text),
只把"词从哪来"换成融合引擎 → 步骤5(读 candidates.txt)/6/8/9(读 keywords.txt+entities.txt)零改动。

旧正式版实现已隔离到 tools/kwgen_hybrid/legacy/step04_formal.py(不再被正式流程引用)。
"""
from __future__ import annotations

from core import (
    StepDataError,
    atomic_write_text,
    ensure_non_empty,
    require_file,
    write_txt_lines,
)
from core.catname import display_cat
from steps._keywords import DIM_RULES, entity_section
from steps.base import BaseStep, StepContext

_DIM_RULES = DIM_RULES


class Step04Keywords(BaseStep):
    number = 4
    title = "生成关键词"
    description = ("FR-04(融合版): 登记根词+LLM 发现根词 → 卖家精灵词库召回(每根词最多 3 页) → "
                   "keywordList 精准验证(只留正搜索量) → LLM 语义审核(本类目/邻类目/配件/品牌/五维) "
                   "→ 核心词按搜索量排序 + 长尾从真实词按语义选。输出格式与旧版完全同构。")

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        details = ctx.resolve_artifact(f"{self._cat}-dual-details-filtered.jsonl")
        require_file(details, "FR-03 过滤后榜单详情")
        ensure_non_empty(details, "FR-03 过滤后榜单详情")

    @property
    def artifacts(self) -> list[str]:
        return [
            f"{self._cat}-keywords-candidates.txt",
            f"{self._cat}-keywords.txt",
            f"{self._cat}-keywords-entities.txt",
        ]

    @property
    def extra_archive_files(self) -> list[str]:
        # 融合版审计件: 逐词搜索量/语义/依据(candidates.csv) + 运行摘要(summary.json)。
        # 与正式三件套同名不冲突, 随步骤4 一起归档。
        return [f"{self._cat}-kwgen-summary.json", f"{self._cat}-kwgen-candidates.csv"]

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        self.log(ctx, f"开始生成关键词(融合版: 登记根词+LLM+词库验证+语义审核): CAT={cat}")
        from tools.kwgen_hybrid import glue
        from tools.kwgen_hybrid.engine import DIMS

        if ctx.dry_run:
            raise StepDataError("融合版步骤4 需要真实 LLM 与卖家精灵 MCP, 不支持 DRY_RUN(离线验证走单测)")
        if not ctx.config.get("LLM_API_KEY") or not ctx.config.get("SS_MCP_KEY"):
            raise StepDataError("融合版步骤4 需要 LLM_API_KEY 与 SS_MCP_KEY(环境变量优先)")

        out_cand = ctx.resolve_artifact(self.artifacts[0])
        out_kw = ctx.resolve_artifact(self.artifacts[1])
        out_ent = ctx.resolve_artifact(self.artifacts[2])

        import hashlib
        import json as _json
        from pathlib import Path as _Path

        directory = _Path(ctx.paths.cat_root)
        raw, cfg = glue.read_config(directory / "config.yaml")
        site = glue.resolve_site(raw, cat)
        month = cfg.trend_month
        registry_path = _Path(glue.__file__).resolve().parent / "registered_roots.json"
        registered, _reg_info = glue.load_registered_roots(registry_path, cat, site)

        src = _Path(ctx.resolve_artifact(f"{cat}-dual-details-filtered.jsonl"))
        records = [_json.loads(ln) for ln in src.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
        if not records:
            raise StepDataError("过滤后榜单详情无有效记录, 无法生成关键词")
        brands = [r["brand"] for r in records if isinstance(r.get("brand"), str)]
        policy = glue.load_product_policy(directory, cat)
        exclusions = glue.collect_exclusions(directory, cat)
        self.log(ctx, f"读取过滤后榜单详情 {len(records)} 条 | 站点={site} 目标月={month} | 登记根词={len(registered)}")

        llm_cache = glue.CachedCalls(directory / "_kwgen_llm_cache.json", {
            "version": "hybrid-v4-category-generic", "model": cfg.get("LLM_MODEL"),
            "endpoint_hash": hashlib.sha256(str(cfg.get("LLM_BASE_URL")).encode()).hexdigest()})
        from core.mcp import MCPClient, normalize_ss_mcp_url
        miner_cache = glue.CachedCalls(directory / "_kwgen_mcp_cache.json", {
            "version": "keyword_miner-v2",
            "endpoint_hash": hashlib.sha256(normalize_ss_mcp_url(str(cfg.get("SS_MCP_URL"))).encode()).hexdigest()})
        mcp = MCPClient(cfg)
        llm_client = glue.ExperimentLLM(cfg, meter=ctx.usage())
        pipeline = glue.HybridPipeline(
            glue.LiveLLM(llm_client, llm_cache), glue.LiveMiner(mcp, miner_cache),
            root_limit=10, max_pages=3, judge_batch=30, max_pool=1200, hint_limit=12,
            weekly=glue.WeeklyEvidence(mcp))
        try:
            result = pipeline.run(display_cat(cat), site, month, records,
                                  candidate_limit=int(cfg.get("CANDIDATE_LIMIT", 20)),
                                  per_dim=int(cfg.get("LONGTAIL_PER_DIM", 5)),
                                  product_policy=policy, brands=brands, registered_roots=registered)
        finally:
            mcp.close()

        # ★ 输出与旧版**完全同构**: 复用旧版的写盘与实体台账渲染, 只换"词从哪来"。
        #   三件套格式不变 → 步骤5(读 candidates.txt)/6/8/9(读 keywords.txt + entities.txt)零改动。
        core_words = [r["keyword"] for r in result["core"]]
        fallback = result["summary"].get("weekly_fallback")
        if fallback:
            metric = {"complete_weeks": "完整周搜索量合计", "recent_four_weeks": "近4周搜索量合计", "previous_month": "历史月搜索量"}.get(fallback["kind"], "兜底搜索量")
            caveat = "不作为目标月月搜索量" 
            self.log(ctx, f"目标月 {month} 月数据为空：使用 {fallback['source']} {metric} "
                          f"{fallback['period_start']}~{fallback['period_end']}，"
                          f"召回月份 {fallback['recall_month']}，完整数据 {fallback['complete_count']}/{fallback['candidate_count']} 词；{caveat}。")
        longtail_words = [r["keyword"] for dim in DIMS for r in result["longtail"].get(dim, [])]
        root = core_words[0] if core_words else display_cat(cat)
        per_dim = int(cfg.get("LONGTAIL_PER_DIM", 5))
        report = {}
        for dim in DIMS:
            report[dim] = {
                "entities": [{"entity": e["entity"], "records": e["records"], "occ": e["occ"], "source": "LLM"}
                             for e in result["entities"].get(dim, [])],
                "longtails": [r["keyword"] for r in result["longtail"].get(dim, [])],
                "fallback": 0,
            }

        # Quality gate precedes publication of any inputs consumed by steps 5–10.
        result["summary"]["call_diagnostics"] = [
            event for cache in (llm_cache, miner_cache) for event in cache.events
            if event.get("error_type") or event.get("schema_incomplete")]
        atomic_write_text(ctx.resolve_artifact(f"{cat}-kwgen-summary.json"),
                          _json.dumps(result["summary"], ensure_ascii=False, indent=2) + "\n")
        self._write_kwgen_csv(ctx, cat, result)
        summary = result["summary"]
        if summary["status"] != "ok":
            raise StepDataError(f"融合版步骤4 状态={summary['status']}，核心 {len(core_words)}，"
                                f"待审 {summary['pending_review']}，错误 {summary['errors']}，"
                                f"详见 {cat}-kwgen-summary.json")

        write_txt_lines(out_cand, core_words)
        self.log(ctx, f"大词候选 {len(core_words)} 个(词库验证+语义审核, 按搜索量降序)已写入: {out_cand}")
        content = "[核心大词]\n" + "\n".join(core_words) + "\n[长尾词]\n" + "\n".join(longtail_words) + "\n"
        atomic_write_text(out_kw, content)
        self.log(ctx, f"关键词文件已写入: {out_kw} (核心 {len(core_words)} + 长尾 {len(longtail_words)})")
        atomic_write_text(out_ent, self._entity_report_text(cat, root, len(records), per_dim, report))
        self.log(ctx, f"五维实体台账已写入: {out_ent}")

        self.log(ctx, f"融合版完成: 核心 {len(core_words)} / 长尾 {len(longtail_words)} / "
                      f"池 {result['summary'].get('pool_count')} / 正搜索量 {result['summary'].get('positive_verified_count')}")

    def _write_kwgen_csv(self, ctx: StepContext, cat: str, result: dict) -> None:
        """融合版逐词审计表(搜索量/语言/维度/关系/来源/依据), 供人工复核。"""
        import csv as _csv
        import os as _os
        path = ctx.resolve_artifact(f"{cat}-kwgen-candidates.csv")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
            fields = ("keyword", "searches", "kind", "source", "period_start", "period_end", "weeks", "recall_month", "language", "dimension", "is_longtail",
                      "relation", "sources", "reason")
            w = _csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for row in result.get("candidates", []) + result.get("adjacent", []):
                w.writerow(dict(row, sources=" | ".join(row.get("sources", []))))
        _os.replace(tmp, path)

    def _entity_report_text(self, cat: str, root: str, total_records: int, per_dim: int, report: dict) -> str:
        """五维实体台账(人工可读 + FR-09 机读): 实体 + 商品数/出现次数 + 来源 + 派生的长尾词。

        分区格式 `[维度.实体]` / `[维度.长尾词]` 由 steps/_keywords.py 统一约定,
        FR-09 用 parse_entity_artifact() 读它, 拿每维最高频实体 × 搜索量最高的达标词组装抓取词。
        """
        lines = [
            f"# FR-04 五维实体台账 —— {cat}",
            f"# 基座词(核心根词): {root}",
            f"# 语料: FR-03 过滤后榜单详情 {total_records} 条(标题+五点描述)",
            f"# 每维取频率最高的 {per_dim} 个实体组装长尾; 商品数=提到该实体的记录数(优先级), 出现=总次数",
            "# 来源: LLM=融合版语义审核后经语料计数验证的实体",
            "# 本文件是 FR-09 组装抓取词的输入(每维最高频实体 × 搜索量最高的达标词), 勿手工删分区",
        ]
        for dim in _DIM_RULES:
            info = report.get(dim) or {}
            lines.append(entity_section(dim, "实体"))
            rows = info.get("entities") or []
            if not rows:
                lines.append("# (无: 语料里没有该维度的可计数实体, 长尾不足不补泛词)")
            for i, r in enumerate(rows, 1):
                lines.append(f"{i}\t{r['entity']}\t商品数={r['records']}\t出现={r['occ']}\t来源={r['source']}")
            lines.append(entity_section(dim, "长尾词"))
            lines.append(f"# 兜底 {info.get('fallback', 0)} 条")
            for w in info.get("longtails") or []:
                lines.append(w)
        return "\n".join(lines) + "\n"
