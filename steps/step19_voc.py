# -*- coding: utf-8 -*-
"""步骤19 VOC 评论分析(FR-19)。

2026-11-02：采集结束后按最终去重清单、归属变体ASIN汇集评论提取多方面好评点/痛点；
先做功能大类/属性/具体体验分类及独立目录同义归并，
区分实际体验、带条件体验、泛评、预期、转述及担忧，再归并候选Need、生成有范围边界的Open和待验证方案。
新增产物位于2_需求机会链/，含Need/Open分类及逐条评论回贴表；整个步骤目录递归归档。
原14表工作簿仍为单主标签兼容视图，不等同于多方面统计。
2026-11-03：不同父体并发采集；方面提取、分类/语义复核、Need字段关联、解法核查、Open生成/复核
按独立任务有界并发。保留Pangolinfo优先→卖家精灵兜底、原始回执与成功缓存、失败补试及阶段续跑。
采集去重后再提取，保证同变体评论分组完整；共享Need目录归并及最终导出仍按依赖统一执行。

做什么(四步流水线, 编排在 `tools/voc/pipeline.py`):
  1. **取父ASIN**: 优先步骤14 的流量拆分表(`AI已补充` 表, 按父体月销量合计排序);
     缺表则回退步骤10 的 `<CAT>-parents.txt`;
  2. **采集评论**: Pangolinfo MCP 主源(`get_amazon_reviews`), 按 **VOC_PAGES_PER_PARENT**
     (默认 5 页/父体)以 `VOC_STAR_MIX` 模板分摊到 critical(1–3★)/four_star/five_star;
     父体自身无评论时依次探测其子体(Pangolinfo 实测: 父体 totalReviews=0, 评论挂在子体上);
     Pangolinfo 积分耗尽 → 自动切卖家精灵 `review` 兜底; 每请求原始响应落 JSONL 可断点续跑;
  3. **去重**: 按 Amazon 原生 `reviewId` 去重(卖家精灵无 ID → 用 作者|标题|正文 兜底键,
     并做日期容差校验); 合并来源、累加出现次数, Helpful 绝不累加;
  4. **出报告**: LLM 逐条标注(属性/证据角色/问题/严重度/安全等级/场景) → 标签归一 →
     生成 skill 契约的 **V3 14 表工作簿** → 用 skill 校验脚本做 `--strict-v3` 校验。

产物(均落在 `data/<CAT>/19_VOC报告/`, 随编排器自动归档到 `data/archive/<CAT>-<ts>/19_VOC报告/`):
  <CAT>-VOC报告.xlsx / 分析明细.jsonl / 评分汇总.csv / 去重审计.csv / 采集审计.csv / VOC流水线摘要.json

开关与参数(见 CONFIG_GUIDE.md):
  VOC_ENABLED / VOC_PAGES_PER_PARENT / VOC_STAR_MIX /
  VOC_FETCH_WORKERS / VOC_LLM_BATCH / VOC_LLM_WORKERS / VOC_VALIDATE_STRICT

**已移出配置的键**(2026-10-12 用户口径):
  · `VOC_SOURCE_PRIORITY` —— **写死在代码** (就 Pangolinfo → 卖家精灵两个源, 没有第三种方式);
  · `VOC_SITE` —— **由 `SITE` 自适应** (`tools/voc/pipeline.voc_site_of`)。
    ⚠ 原来它是独立的一份拷贝, 用 Web「新建类目」`copy_from` 时会被**一起复制** →
    实测英/德站的类目里仍是 `amz_us` → **一直在抓美国站评论且没有任何提示**。

Pangolinfo 凭据(**不在 config.yaml**, 2026-10-12 起):
  · URL —— **写死在代码** (`tools/voc/pipeline.PANGOLIN_MCP_URL`, 固定端点非秘密);
  · KEY —— **环境变量** `PANGOLIN_MCP_KEY` (服务器部署用) → 回落本机 `~/.codex/config.toml`。

父体范围: **全量写死** —— 流量拆分表(或 <CAT>-parents.txt)里的每个父 ASIN 都抓,
不设数量上限(2026-10-09 用户口径: "要跑肯定是全量跑"); 积分不设熔断 —— 额度耗尽由数据源兜底: Pangolinfo → 卖家精灵。
"""
from __future__ import annotations

import json
import os

from core.errors import StepDataError
from steps.base import BaseStep, StepContext
from tools.voc import pipeline as voc_pipeline

STEP_DIR = "19_VOC报告"
REPORT_SUFFIX = "-VOC报告.xlsx"
SUMMARY_NAME = "VOC流水线摘要.json"


class Step19Voc(BaseStep):
    number = 19
    title = "VOC评论分析"
    description = ("FR-19: 按父ASIN抓取 1–5 星评论(Pangolinfo 主 / 卖家精灵兜底) → reviewId 去重 → "
                   "LLM 归类 → 生成 V3 14 表 VOC 工作簿并做严格校验")

    # ---------------- 上游校验
    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        # 输入二选一: 步骤14 流量拆分表(优先) 或 步骤10 父体清单
        split = ctx.resolve_artifact(os.path.join("14_流量拆分", "流量拆分数据-需人工补充.xlsx"))
        parents = ctx.resolve_artifact("%s-parents.txt" % self._cat)
        self._traffic = split if os.path.exists(split) else None
        if not self._traffic and not os.path.exists(parents):
            raise StepDataError(
                "缺少 VOC 输入: 既无「14_流量拆分/流量拆分数据-需人工补充.xlsx」, "
                "也无「%s-parents.txt」(请先跑步骤10 或步骤14)" % self._cat)

    # ---------------- 产物声明(随编排器归档)
    @property
    def artifacts(self) -> list[str]:
        # 注意: 文件名后缀必须**内联**写在 f-string 里 —— scripts/check_contract.py 从源码提取
        # (`{self._cat}`→CAT 后)的文件名做跨步骤契约比对, 写成 REPORT_SUFFIX 变量会提取不到而误报。
        return [f"{STEP_DIR}/{self._cat}-VOC报告.xlsx"]

    @property
    def archive_dirs(self) -> list[str]:
        return [STEP_DIR]

    @property
    def extra_archive_files(self) -> list[str]:
        # 中间产物(可能因预算/额度提前停止而缺失) → 不参与 DONE 校验, 只随步骤归档
        return ["%s/%s" % (STEP_DIR, n) for n in
                ("分析明细.jsonl", "评分汇总.csv", "去重审计.csv", "采集审计.csv", SUMMARY_NAME,
                 "2_需求机会链/chain.json", "2_需求机会链/需求机会链摘要.json", "2_需求机会链/分类结果.json",
                 "2_需求机会链/属性分类汇总.csv", "2_需求机会链/功能大类.csv",
                 "2_需求机会链/逐条评论分类.csv", "2_需求机会链/已有解法与解决程度.csv",
                 "2_需求机会链/步骤18功能属性证据.csv", "2_需求机会链/已查商品.csv",
                 "2_需求机会链/归属待核记录.csv", "2_需求机会链/Open需求准入.csv",
                 "2_需求机会链/步骤18关联来源.json", "2_需求机会链/阶段断点.json",
                 "2_需求机会链/实体与变体关联明细.csv", "2_需求机会链/待验证方案.csv",
                 "2_需求机会链/Need跨变体归并组.csv", "2_需求机会链/评论准入.json",
                 "2_需求机会链/变体Need完整机会链.csv",
                 "ASIN缺失评论明细.csv", "ASIN跨源补抓任务.json", "ASIN跨源补抓结果.json",
                 "1_采集原始数据/ASIN重定向记录.json",
                 "2_需求机会链/GitHub大类参考.json",
                 "2_需求机会链/评论准入排除记录.csv", "2_需求机会链/排除评论原文.json")]

    # ---------------- 开关
    def skip_when(self, ctx: StepContext) -> bool:
        """跳过条件(任一): VOC_ENABLED=false; 或 dry_run 模式。

        dry_run 必须跳过: 该步依赖**真实** MCP 评论接口与 LLM 逐条标注
        (mock 下拿不到评论, 流水线会把本步判 FAILED, 破坏整体 dry-run 冒烟)。
        """
        if not bool(ctx.config.get("VOC_ENABLED", True)):
            self.log(ctx, "VOC_ENABLED=false → 跳过 VOC 步骤(不消耗积分)")
            return True
        if ctx.dry_run:
            self.log(ctx, "dry_run 模式 → 跳过 VOC 步骤(无法用 mock 产出真实评论分析)")
            return True
        return False

    # ---------------- 产物质量校验(断点续跑时用)
    def validate_artifacts(self, ctx: StepContext) -> None:
        """工作簿存在/非空 + 摘要必须存在且显示有唯一评论; 不合格抛错让编排器重跑。"""
        cat = ctx.config.cat
        path = ctx.resolve_artifact(os.path.join(STEP_DIR, "%s%s" % (cat, REPORT_SUFFIX)))
        if not os.path.exists(path) or os.path.getsize(path) < 10_000:
            raise StepDataError("VOC 工作簿缺失或过小: %s" % path)
        summary_path = ctx.resolve_artifact(os.path.join(STEP_DIR, SUMMARY_NAME))
        if not os.path.exists(summary_path):
            raise StepDataError("VOC 摘要缺失, 视为未完成: %s" % summary_path)
        try:
            with open(summary_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise StepDataError("VOC 摘要不可解析: %s" % exc) from exc
        if not data.get("unique_reviews"):
            raise StepDataError("VOC 摘要显示 0 条唯一评论, 视为未完成")
        if data.get("collection_pending_parents"):
            raise StepDataError("VOC还有MCP采集任务待补试，不能视为完成，请续跑步骤19")
        if data.get("annotation_failed_reviews"):
            raise StepDataError("VOC还有模型标注任务待补试，不能视为完成，请续跑步骤19")
        if data.get("asin_recovery", {}).get("pending_tasks"):
            raise StepDataError("VOC还有ASIN缺失跨源补抓任务待补试，请续跑步骤19")
        if ctx.config.get("VOC_OPPORTUNITY_ENABLED", True):
            chain_dir = os.path.join(ctx.paths.cat_root, STEP_DIR, "2_需求机会链")
            try:
                with open(os.path.join(chain_dir, "需求机会链摘要.json"), encoding="utf-8") as fh:
                    chain_summary = json.load(fh)
                with open(os.path.join(chain_dir, "chain.json"), encoding="utf-8") as fh:
                    chain = json.load(fh)
            except (OSError, ValueError) as exc:
                raise StepDataError("需求机会链缺失/不可解析，请重跑步骤19") from exc
            if (chain_summary.get("status") != "complete" or not chain.get("reviews")
                    or len(chain.get("reviews", [])) != data.get("unique_reviews")):
                raise StepDataError("需求机会链未完成或与VOC评论数量不一致，请重跑步骤19")
            from tools.voc.classification import VERSION as TAXONOMY_VERSION
            classification = chain.get("classification", {})
            if (classification.get("status") != "complete" or classification.get("version") != TAXONOMY_VERSION
                    or classification.get("classified_aspects") != len(chain.get("aspects", []))
                    or chain_summary.get("classification") != classification):
                raise StepDataError("VOC统一分类缺失、版本过期或覆盖不全，请重跑步骤19")
            from tools.voc.opportunity import find_span_conflicts
            from tools.voc.opportunity import EXTRACTION_VERSION
            if chain.get("extraction", {}).get("version") != EXTRACTION_VERSION:
                raise StepDataError("VOC尚未按变体ASIN分组提取，请重跑步骤19")
            from tools.voc.relation_tags import VERSION as RELATION_VERSION
            relation = chain.get("relation_classification", {})
            if (relation.get("version") != RELATION_VERSION or relation.get("status") != "complete"
                    or relation.get("classified_needs") != len(chain.get("needs", []))
                    or relation.get("classified_opens") != len(chain.get("opportunities", []))):
                raise StepDataError("Need/Open分类缺失或不完整，请重跑步骤19")
            if find_span_conflicts(chain.get("aspects", [])):
                raise StepDataError("VOC正负原句交叠，需重新拆分精确片段，请重跑步骤19")
            from tools.voc.solutions import VERSION as SOLUTION_VERSION, source_stamp
            solution = chain.get("solution_check", {})
            if (solution.get("version") != SOLUTION_VERSION or solution.get("status") != "complete"
                    or solution.get("mapped_needs") != len(chain.get("needs", []))
                    or solution.get("checked_solutions") != len(chain.get("solutions", []))
                    or chain_summary.get("solution_check") != solution):
                raise StepDataError("步骤18关联核查缺失、过期或不完整，请重跑步骤19")
            from tools.voc.identity import VERSION as IDENTITY_VERSION, ADMISSION_VERSION, validate_identities
            identity = chain.get("identity_association", {})
            admission = chain.get("admission_summary", {})
            if (identity.get("version") != IDENTITY_VERSION or identity.get("status") != "complete"
                    or chain_summary.get("identity_association") != identity
                    or admission.get("version") != ADMISSION_VERSION
                    or admission.get("included_reviews") != len(chain["reviews"])
                    or admission.get("source") != solution.get("source")
                    or chain_summary.get("admission_summary") != admission):
                raise StepDataError("变体/父ASIN/品牌关联或步骤18准入记录缺失、过期，请重跑步骤19")
            expected = {"aspects": len(chain.get("aspects", [])), "needs": len(chain.get("needs", [])),
                        "solutions": len(chain.get("solutions", [])), "opens": len(chain.get("opportunities", [])),
                        "proposals": len(chain.get("proposals", [])), "links": len(chain.get("variant_links", []))}
            if any(identity.get(k) != v for k, v in expected.items()):
                raise StepDataError("实体与变体关联覆盖不完整，请重跑步骤19")
            try:
                validate_identities(chain)
            except (ValueError, KeyError, TypeError) as exc:
                raise StepDataError("实体与变体关联校验失败：%s" % exc) from exc
            try:
                current_source = source_stamp(ctx.paths.cat_root, cat)
                if current_source.get("version"):
                    from tools.attr_stability.step18_workspace import Workspace
                    workspace = Workspace(ctx.paths.cat_root)
                    workspace._fresh(workspace._current(), current_source["version"])
                if solution.get("source") != current_source:
                    raise ValueError("步骤18交付版本或内容已变化")
            except (OSError, ValueError) as exc:
                raise StepDataError("步骤18交付已失效或尚未导出，需重新核查：%s" % exc) from exc

    # ---------------- 主流程
    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        out_dir = ctx.paths.step_dir(19)          # 自动创建 data/<CAT>/19_VOC报告/
        pages = int(ctx.config.get("VOC_PAGES_PER_PARENT", 5) or 5)
        # ★ 数据源顺序**写死**（2026-10-12 用户口径：就 Pangolinfo → 卖家精灵两个源，
        #   没有第三种方式；留配置键只会让人以为还有别的花样）。
        from tools.voc.pipeline import DEFAULT_SOURCE_PRIORITY
        priority = DEFAULT_SOURCE_PRIORITY
        strict = bool(ctx.config.get("VOC_VALIDATE_STRICT", True))
        self.log(ctx, "开始 FR-19 VOC 评论分析: CAT=%s; 页数/父体=%d; 父体范围=全量; 源顺序=%s"
                 % (cat, pages, priority or "默认"))
        self.log(ctx, "产物目录: %s(随步骤自动归档到 data/archive/<CAT>-<时间戳>/%s/)" % (out_dir, STEP_DIR))

        summary = voc_pipeline.run_voc(
            cat_dir=ctx.paths.cat_root.as_posix() if hasattr(ctx.paths.cat_root, "as_posix")
            else str(ctx.paths.cat_root),
            config=ctx.config,
            log=lambda m: self.log(ctx, m),
            traffic_xlsx=self._traffic,
            out_dir=out_dir,
            max_parents=None,          # 全量: 流量拆分表里有多少父体就抓多少(用户口径)
            pages_per_parent=pages,
            source_priority=priority or None,
            validate=strict,
            meter=ctx.usage(),
        )

        self.log(ctx, "采集: 父体 %s 个 / %s 页 / %s 积分; 去重后唯一评论 %s 条"
                 % (summary.get("parents"), summary.get("pages"), summary.get("points"),
                    summary.get("unique_reviews")))
        self.log(ctx, "报告: 问题 %s 类 / 正向驱动 %s 类; 校验=%s"
                 % (summary.get("issues"), summary.get("drivers"), summary.get("validation_status")))
        if summary.get("stopped_reason"):
            self.log(ctx, "⚠ 提前停止: %s" % summary["stopped_reason"])
        if not os.path.exists(os.path.join(out_dir, "%s%s" % (cat, REPORT_SUFFIX))):
            raise StepDataError("VOC 工作簿未生成: %s" % os.path.join(out_dir, "%s%s" % (cat, REPORT_SUFFIX)))
