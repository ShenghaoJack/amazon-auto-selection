"""FR-09 搜索收集(浏览器自动化)。

输入: 达标核心大词(FR-07 对全部候选判定为"达标"的词, 个数不限) + 长尾词 + 筛选结果
      + 双榜清单(并入)
输出: <CAT>-search-asins.json({keyword: [ASIN...]}) + <CAT>-search-asins-all.txt(全量去重 ASIN)
附档: <CAT>-search-verify.json({keyword: [{asin, title}]}, 断点可续) 
      + <CAT>-search-verify.csv(关键词×ASIN×标题×规则判定, 人工审计)

正确性(2026-09-08 增强, 回答"收集的是否正确的 ASIN"):
1. **只解析搜索结果主列表容器**(data-component-type="s-search-result" 的结果卡;
   ASIN 取卡内 /dp/ 标题链接优先、卡标签 data-asin 兜底——有的卡 ASIN 只在标题链接里),
   不再全文扫 /dp/ 链接——页尾"与此商品相关/看了又看/最佳卖家"等推荐模块、
   以及广告容器(sp-sponsored-result)一律不收;
   容器结构漂移(主列表解析为空)时回退旧全文过滤并**日志告警**(可能混入相关模块)。
2. **逐条留标题快照**(结果卡内标题文本——h2 文本 / /dp/ 标题锚点 / h2 aria-label 多路兜底)
   → <CAT>-search-verify.json 持久化(断点续爬不丢)。
3. **规则审计**(零额外请求): 标题与关键词做本地分词匹配, 全命中=相关,
   未命中/无快照=待人工; 结果写 <CAT>-search-verify.csv 并汇总日志。

逻辑: 关键词三态(2026-09-29) -> 开品门槛 -> 收集清单 = 核心词(全部)
      + 抓取词(五维度各取频率最高实体 × 搜索量最高的核心词, 最多 5 条; 见 _crawl_keywords)
      -> 浏览器逐词搜索, 滚动加载后解析自然位结果卡(ASIN+标题) -> 并入双榜清单 -> 去重 -> 截图

关键词三态(2026-09-29 用户口径, 详见 core/keyword_lists.py):
  自动 = FR-07 达标核心词(US)/站内搜索量前 N(非 US), 门槛口径与旧版完全一致;
  人工 = 看板「关键词清单」面板填的清单(可删达标词/加不达标词/手填新词), 门槛按最终词数判;
  停止 = 存在停止标记 -> 本步骤直接中止, 不抓数据。

站点分流(2026-09-15 用户口径): **非 US 站**不套 SCREEN_REQUIREMENT(美国站特征的阈值),
"达标核心大词" = 站内月搜索量前 NON_US_QUALIFIED_TOP(5) 个候选词(缺搜索量的排除),
且不由开品门槛阻断; 抓取词基座 = 这 5 个里搜索量最高的那一个(长尾只用最大搜索量核心词)。
US 站与无法判定站点的老配置保持原行为。

容错: 断点续爬(已完成词跳过); 单词失败重试; 两个产物必须始终写出
      实体台账缺失(FR-04 未重跑)时回退旧口径: 按相关度取最多 5 个长尾词
"""
from __future__ import annotations

import csv
import os
import re
import time
from html import unescape
from urllib.parse import quote_plus

from core.category_config import (
    NON_US_QUALIFIED_TOP,
    is_us_site as _is_us_site,
    pick_top_volume_candidates,
)
from core.errors import StepFailedError
from core.io_utils import read_json, read_txt_lines, write_json, write_txt_lines
from core.keyword_lists import (
    resolve as resolve_keywords,
    stop_name,
    write_used as write_used_keywords,
)
from core.validation import require_file, warn_missing
from steps._keywords import DIM_RULES, crawl_words, parse_entity_artifact
from steps.base import BaseStep, StepContext

_DP_ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})")
_NOISE_PREFIXES = ("best", "cheap", "new")
_SPONSOR_WINDOW = 600
# 2026-09-08: 结果主列表卡用 data-component-type="s-search-result" 定位(广告卡是
# sp-sponsored-result、推荐模块无该标记 -> 天然排除); ASIN 取自: 卡内标题链接/任意
# /dp/ 链接优先(有的卡 data-asin 不在该标签上、只在标题链接里), 标签内 data-asin 兜底
_RESULT_CARD_ATTR = re.compile(r'data-component-type="s-search-result"', re.I)
_NEXT_CARD_RE = re.compile(r'data-component-type="(?:s-search-result|sp-sponsored-result)"', re.I)
# 2026-09-08 广告排除(真实页面实测: 新布局把广告卡也标成 s-search-result,
# 判别 = 卡标签类含 AdHolder / 卡内 sponsored-ads 点击链接 / 可见 Sponsored 标签)
_AD_MARK_RE = re.compile(
    r"AdHolder|sponsored-ads\.amazon\.com|aria-label=\"[^\"]*Sponsored|>Sponsored<", re.I)
_STRIP_SCRIPT_RE = re.compile(r"<script[^>]*>[\s\S]*?</script>", re.I)
_STRIP_STYLE_RE = re.compile(r"<style[^>]*>[\s\S]*?</style>", re.I)
_STRIP_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I)
_H2_ARIA_RE = re.compile(r'<h2[^>]*\baria-label="([^"]*)"', re.I)
_DP_ANCHOR_RE = re.compile(
    r'<a[^>]*href="[^"]*/dp/[A-Z0-9]{10}(?:[^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
_DP_IN_SEG_RE = re.compile(r"/dp/([A-Z0-9]{10})")
_ASIN_ATTR_RE = re.compile(r'\bdata-asin="([A-Z0-9]{10})"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_STOP_TERMS = {"for", "with", "and", "of", "the", "a", "an", "to", "in", "on", "by", "kit"}


class Step09SearchCollect(BaseStep):
    number = 9
    title = "搜索收集"
    description = "FR-09: 浏览器逐词搜索自然位 ASIN(结果卡解析+标题快照+规则校验审计), 断点续爬, 并入双榜清单"

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        require_file(ctx.resolve_artifact(f"{self._cat}-screen-results.json"), "筛选结果(FR-07)")
        require_file(ctx.resolve_artifact(f"{self._cat}-keywords.txt"), "关键词文件(FR-04)")
        warn_missing(ctx.resolve_artifact(f"{self._cat}-bestseller-newrelease-all.txt"), "双榜清单(并入)")

    @property
    def artifacts(self) -> list[str]:
        return [f"{self._cat}-search-asins.json", f"{self._cat}-search-asins-all.txt"]

    @property
    def extra_archive_files(self) -> list[str]:
        """搜索核对**附档**(2026-09-15 补声明, 用户在看板上问起过这两个文件):

        本步骤还会产出两份附档(见模块 docstring):
          * `<CAT>-search-verify.json` = `{keyword: {asin: title}}` 断点续爬的持久化(重跑不丢进度);
          * `<CAT>-search-verify.csv`  = 关键词×ASIN×标题×规则判定, **人工审计**用。
        2026-09-29 追加第三份: `<CAT>-used-keywords.txt` = 本轮**实际生效**的核心词/长尾词
        与来源(自动/人工清单/人工停止)、门槛判定原文 —— 归档是交付物, 这份口径留痕
        理应随步骤9 一起进快照(与步骤10 的 `-injected-keywords.txt` 同款用法)。

        原先它们没进任何步骤声明 → 看板"点击步骤卡片看产物"只能把它们归到"未归属产物"
        (且显示不出大小/时间)。声明为 extra_archive_files 后: 归到 STEP 09, 并随步骤一起归档
        (该 API 的既有语义: 存在才同步, 不参与 DONE 校验/产物缺失判定)。
        """
        return [f"{self._cat}-search-verify.json", f"{self._cat}-search-verify.csv",
                f"{self._cat}-used-keywords.txt"]

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        self.log(ctx, f"开始搜索收集: CAT={cat}")
        gate = int(ctx.config.get("GATE_MIN_QUALIFIED", 2))
        screen = read_json(ctx.resolve_artifact(f"{cat}-screen-results.json"), default=[])
        if not isinstance(screen, list):
            screen = []
        screen_map = _norm_screen(screen)
        _, longtail_words = parse_keywords_file(
            read_txt_lines(ctx.resolve_artifact(f"{cat}-keywords.txt"))
        )

        # 达标核心大词的取法按**站点**分流(2026-09-15 用户口径):
        #  - US 站: **FR-07 对全部候选判定的"达标"词全集**(2026-09 变更: 不再限制在
        #    keywords.txt [核心大词] 固定 5 个内 —— step04 已把全部大词候选写入核心分区;
        #    此处直接以 screen 全量达标为准, 个数不限, 由筛选结果决定);
        #  - 非 US 站: SCREEN_REQUIREMENT 那套阈值(月搜索量/站内同比/Google同比/CPC)是
        #    按美国站数据特征定的, 其它站点普遍取不到同等数据 → 硬套必然全判"数据不足";
        #    故改为**直接取站内月搜索量前 5 个**候选词(缺搜索量的排除), 且不由开品门槛阻断。
        us_site = self._us_site(ctx)
        if us_site:
            auto_core = [w for w in screen_map if screen_map[w] == "达标"]
            self.log(ctx, "站点=美国站 → 自动口径使用 FR-07 筛选结果作达标核心词")
        else:
            auto_core = self._top_volume_candidates(ctx)
            self.log(ctx, f"站点=非美国站 → 按用户口径跳过 SCREEN_REQUIREMENT 筛选, "
                          f"自动口径取站内月搜索量前 {NON_US_QUALIFIED_TOP} 个候选词作达标核心")

        # 关键词三态(2026-09-29 用户口径): 自动 / 人工清单 / 人工停止。
        #   有人工清单 => 用清单词(可删达标词、可加不达标词、可手填新词), 门槛按**最终词数**判;
        #   无人工清单 => 与旧行为完全一致(US 按 FR-07 达标数判门槛, 非 US 门槛不生效);
        #   有停止标记 => 本步骤直接中止(不抓任何数据), 后续步骤自然不会跑。
        plan = resolve_keywords(
            ctx.paths.cat_root, cat,
            auto_core=auto_core, auto_longtail=longtail_words,
            screen_verdict=screen_map, gate=gate, gate_applies=us_site,
        )
        write_used_keywords(ctx.paths.cat_root, cat, plan,
                            site=str(ctx.config.get("SITE", "") or ""))
        self.log(ctx, plan.source_text())
        self.log(ctx, plan.longtail_text())
        self.log(ctx, plan.gate_text())
        self._ensure_keyword_plan(ctx, cat, plan, screen_map)
        qualified_core = list(plan.core)
        longtail_words = list(plan.longtail)
        self.log(ctx, f"最终核心词({len(qualified_core)}): " + "、".join(qualified_core))

        collect_words = self._build_collect_list(ctx, qualified_core, longtail_words,
                                                 manual_tail=plan.longtail_manual_active)
        related_n = max(0, len(collect_words) - len(qualified_core))
        self.log(ctx, f"收集词: 核心 {len(qualified_core)} 个 + 抓取词 {related_n} 个")
        self.log(ctx, f"  抓取词({related_n}): "
                      + "、".join(w for w in collect_words if w not in qualified_core))
        self.log(ctx, f"  收集词清单({len(collect_words)}): " + "、".join(collect_words))

        json_path = ctx.resolve_artifact(self.artifacts[0])
        verify_path = ctx.resolve_artifact(f"{cat}-search-verify.json")
        collected = _load_existing(json_path)
        verified = _load_verified(verify_path)
        todo = [w for w in collect_words if w not in collected]
        # 搜索页数可配置: 核心大词 CORE_SEARCH_PAGES(默认5页) / 长尾词 LONGTAIL_SEARCH_PAGES(默认4页)
        core_pages = max(1, int(ctx.config.get("CORE_SEARCH_PAGES", 5) or 5))
        tail_pages = max(1, int(ctx.config.get("LONGTAIL_SEARCH_PAGES", 4) or 4))
        self.log(ctx, f"搜索页数: 核心大词 {core_pages} 页 / 长尾词 {tail_pages} 页")
        if todo:
            self.log(ctx, f"待搜索词 {len(todo)} 个, 断点续爬跳过 {len(collect_words) - len(todo)} 个")
            self._search_words(ctx, todo, qualified_core, collected, verified, json_path,
                               verify_path, core_pages, tail_pages)
        else:
            self.log(ctx, "全部收集词已完成(断点续爬跳过)")

        merged = self._merge_all(ctx, collected)
        txt_path = ctx.resolve_artifact(self.artifacts[1])
        write_txt_lines(txt_path, sorted(merged))
        write_json(json_path, collected)
        write_json(verify_path, verified)
        self.log(ctx, f"搜索收集完成: {len(collected)} 个词, 全量去重 {len(merged)} 个 ASIN, "
                      f"已写 {json_path} / {txt_path}")
        self._verify_summary(ctx, collected, verified)

    def _us_site(self, ctx: StepContext) -> bool:
        """当前配置是否美国站(默认口径)。非 US 站走"搜索量前 5 + 单基座长尾"。"""
        return _is_us_site(
            site=str(ctx.config.get("SITE", "") or ""),
            market=str(ctx.config.market or ""),
            cat=str(ctx.config.cat or ""),
        )

    def _ensure_keyword_plan(self, ctx: StepContext, cat: str, plan, screen_map: dict) -> None:
        """三态/开品门槛闸门 —— 不通过就抛 StepFailedError(单独成方法便于单测, 不必起浏览器)。

        判定顺序: 人工停止 > 无可用核心词 > 门槛未过。
        """
        if plan.stopped:
            raise StepFailedError(
                f"人工停止: 存在停止标记 {stop_name(cat)}, 按人工决定中止(本轮不抓取搜索数据); "
                "如需继续请在看板「关键词清单」面板清除停止标记"
            )
        if not plan.core:
            raise StepFailedError(
                "没有可用的核心词(自动口径取不到带站内月搜索量的候选词, 人工清单也为空): "
                f"请检查 FR-05 挖词产物 "
                f"{ctx.resolve_artifact(f'{cat}-amazon-keyword-data.json')} 是否存在且 "
                "monthly_search_volume 有值, 或在看板「关键词清单」面板填人工清单; 中止搜索收集"
            )
        if not plan.gate_ok:
            detail = ""
            if not plan.is_manual:
                other = [f"{w}({screen_map[w]})" for w in screen_map if w not in plan.core]
                detail = f"; 其余候选词: {'、'.join(other) or '(无)'}"
            raise StepFailedError(
                f"开品门槛未过: {plan.source_text()}; {plan.gate_text()}{detail} → 中止搜索收集; "
                "处理办法: ① 在看板「关键词清单」面板补词 ② 下调门槛 GATE_MIN_QUALIFIED "
                "③ 清除人工清单改回自动口径"
            )

    def _top_volume_candidates(self, ctx: StepContext) -> list[str]:
        """非 US 站口径的"达标核心": 站内月搜索量前 NON_US_QUALIFIED_TOP 个候选词。"""
        data = read_json(ctx.resolve_artifact(f"{ctx.config.cat}-amazon-keyword-data.json"),
                         default=[])
        picked = pick_top_volume_candidates(data, NON_US_QUALIFIED_TOP)
        total = len(data) if isinstance(data, list) else 0
        if len(picked) < min(NON_US_QUALIFIED_TOP, total):
            self.log(ctx, f"非美国站: 候选 {total} 个, 其中带搜索量的仅 {len(picked)} 个"
                          f"(缺搜索量的已按用户口径排除)")
        return picked

    def _build_collect_list(self, ctx: StepContext, qualified_core: list[str],
                            longtail_words: list[str], manual_tail: bool = False) -> list[str]:
        # 2026-09-09: 用 FR-02 过滤提示词的 配件/耗材/硬排除 词, 剔除"非主营/配件"长尾,
        # 避免把 air ionizer 4 pack 这类配件词当相关词收进搜索。
        cat = ctx.config.cat
        bad = _accessory_terms_from(
            read_json(ctx.resolve_artifact(f"{cat}-filter-prompt.json"), default={}))
        result = list(qualified_core)
        if manual_tail:
            # 人工长尾清单(2026-09-29): 用户指定什么就抓什么 —— 跳过五维实体组装,
            # 也不按 配件/耗材/硬排除 过滤(人工说了算), 但命中的词要提示出来。
            hits = [w for w in longtail_words if _matches_accessory(w, bad)]
            self.log(ctx, f"长尾词来源=人工清单 → 抓取词直接用清单词 {len(longtail_words)} 个"
                          f"(跳过五维实体组装)")
            if hits:
                self.log(ctx, f"提示: 人工长尾里 {len(hits)} 个命中 配件/耗材/硬排除 词"
                              f"(人工清单不过滤, 仅提示): " + "、".join(hits[:5])
                              + ("…" if len(hits) > 5 else ""))
            crawl = list(longtail_words)
        else:
            crawl = self._crawl_keywords(ctx, qualified_core, bad)
            if not crawl:
                # 台账缺失/无可用实体(老数据目录/FR-04 未重跑) -> 回退旧口径: 按相关度取长尾
                self.log(ctx, "五维实体台账缺失或无可用实体, 回退: 按相关度取最多 5 个长尾词作抓取词")
                crawl = _top_related_longtail(qualified_core, longtail_words, 5, bad,
                                              log=lambda m: self.log(ctx, m))
        for w in crawl:
            if w not in result:
                result.append(w)
        return result

    def _crawl_keywords(self, ctx: StepContext, qualified_core: list[str], bad: set,
                        report_lines: list[str] | None = None) -> list[str]:
        """抓取词(2026-09-13 口径): 五维度中**每维频率最高的实体** × **搜索量最高的达标词**(基座)。

        实体与频率来自 FR-04 台账 <CAT>-keywords-entities.txt; 基座必须是达标词 ——
        这样组装出的抓取词天然与达标词同头, 不会像旧口径那样被"产品名词头"门槛全剔。
        **非 US 站(2026-09-15 用户口径)**: 达标核心本身已是"搜索量前 5", 基座取其中
        搜索量最高的那一个 —— 即"长尾词只用最大搜索量的核心关键词生成", 因此 5 条
        抓取词全部围绕这同一个基座(不会分散到其它核心词上)。
        report_lines: 仅供预览/测试直接传入台账内容(缺省从产物文件读)。
        """
        base = self._top_volume_core(ctx, qualified_core)
        if not base:
            return []
        if not self._us_site(ctx):
            self.log(ctx, f"非美国站: 长尾词基座 = 搜索量最高的核心关键词「{base}」"
                          f"(该站口径下只用这一个基座)")
        if report_lines is None:
            path = ctx.resolve_artifact(f"{ctx.config.cat}-keywords-entities.txt")
            if not os.path.exists(path):
                self.log(ctx, f"实体台账不存在(FR-04 未重跑?): {path}")
                return []
            report_lines = read_txt_lines(path)
        report = parse_entity_artifact(report_lines)
        pairs = crawl_words(report, base, reject=lambda w: _matches_accessory(w, bad))
        for dim, ent, word in pairs:
            rows = (report.get(dim) or {}).get("entities") or []
            records = next((r.get("records", 0) for r in rows if r.get("entity") == ent), 0)
            self.log(ctx, f"抓取词[{dim}]: {word}  (实体 {ent} {records}品 × 基座 {base})")
        missing = [d for d in DIM_RULES if d not in {p[0] for p in pairs}]
        if missing:
            self.log(ctx, f"抓取词: 以下维度没有可用实体(台账为空/实体与基座重复/命中配件词): "
                          + "、".join(missing))
        return [w for _, _, w in pairs]

    def _top_volume_core(self, ctx: StepContext, qualified_core: list[str]) -> str:
        """达标核心大词里**站内月搜索量最高**的一个(作抓取词基座); 无数据时按原序取第一个。"""
        data = read_json(ctx.resolve_artifact(f"{ctx.config.cat}-amazon-keyword-data.json"),
                         default=[])
        volume: dict[str, float] = {}
        for rec in data if isinstance(data, list) else []:
            if not isinstance(rec, dict):
                continue
            kw = str(rec.get("keyword") or "").strip()
            vol = rec.get("monthly_search_volume")
            if kw and isinstance(vol, (int, float)):
                volume[kw] = float(vol)
        if not qualified_core:
            return ""
        missing = [w for w in qualified_core if w not in volume]
        if missing:
            self.log(ctx, f"达标核心词缺搜索量数据 {len(missing)} 个(按 0 参与排序): "
                          + "、".join(missing[:5]) + ("…" if len(missing) > 5 else ""))
        ranked = sorted(qualified_core, key=lambda w: (-volume.get(w, 0.0), w))
        base = ranked[0]
        self.log(ctx, f"抓取词基座(搜索量最高的达标词): {base}"
                      f"(站内月搜索量 {int(volume.get(base, 0))})")
        return base

    def _search_words(self, ctx: StepContext, words: list[str], qualified_core: list[str],
                      collected: dict, verified: dict, json_path: str, verify_path: str,
                      core_pages: int, tail_pages: int) -> None:
        market = ctx.config.market
        retries = max(1, int(ctx.config.get("REQUEST_RETRY", 2)))
        screenshot_dir = ctx.paths.screenshot_dir()
        with ctx.browser("amazon") as browser:
            page = browser.new_page()
            for word in words:
                pages = core_pages if word in qualified_core else tail_pages
                asins, titles = self._search_one(ctx, browser, page, market, word, pages, retries)
                collected[word] = asins
                write_json(json_path, collected)
                # 标题快照持久化(断点续爬不丢; 同词重搜时整体刷新)
                verified[word] = titles
                write_json(verify_path, verified)
                self._screenshot(browser, page, screenshot_dir, word)
                self.log(ctx, f"词 [{word}] 收集 {len(asins)} 个自然位 ASIN(主列表解析)")
                if not ctx.dry_run:
                    time.sleep(float(ctx.config.get("REQUEST_MIN_INTERVAL", 1.5)))

    def _search_one(self, ctx: StepContext, browser, page, market: str,
                    word: str, pages: int, retries: int) -> tuple[list[str], dict]:
        """搜索 word 共 pages 页, 返回 (去重 ASIN 列表, {asin: 标题快照})。

        只取搜索结果主列表结果卡(见 _extract_result_items); 主列表解析为空时
        回退旧全文 /dp/ 过滤并告警(可能混入推荐/广告, 标题留空待人工)。
        """
        last_err = ""
        for attempt in range(1, retries + 1):
            try:
                items: list[dict] = []
                for page_no in range(1, pages + 1):
                    url = f"https://{market}/s?k={quote_plus(word)}"
                    if page_no > 1:
                        url += f"&page={page_no}"
                    browser.goto(page, url)
                    browser.scroll_to_bottom(page, times=2, delay=1.0)
                    # 2026-09-09: Amazon 新搜索结果页部分卡片由 JS 客户端注入(空壳占位,
                    # 仅有 data-asin + 固定高度), 需等标题/链接渲染完成再解析, 否则标题快照易缺失。
                    try:
                        page.wait_for_selector(
                            "div[data-component-type='s-search-result'] h2, "
                            "div[data-component-type='s-search-result'] a[href*='/dp/'], "
                            "div[data-component-type='s-search-result'] .a-color-base, "
                            "div[data-component-type='s-search-result'] .a-link-normal",
                            timeout=8000)
                    except Exception:
                        page.wait_for_timeout(1500)
                    try:
                        browser.scroll_to_bottom(page, times=1, delay=0.8)
                    except Exception:
                        pass
                    page_items = _extract_result_items(page.content())
                    if not page_items:
                        legacy = _extract_natural_asins(page.content())
                        if legacy:
                            if not ctx.dry_run:
                                self.log(ctx, f"词 [{word}] 第{page_no}页未解析到结果主列表"
                                              f"(页面结构漂移?), 回退全文 /dp/ 过滤 {len(legacy)} 个"
                                              f"(可能混入推荐/广告模块, 标题留空待人工)")
                            page_items = [{"asin": a, "title": ""} for a in legacy]
                    items.extend(page_items)
                asins: list[str] = []
                titles: dict[str, str] = {}
                for it in items:
                    asin = it["asin"]
                    if asin not in titles:
                        titles[asin] = it["title"]
                        asins.append(asin)
                miss = sum(1 for t in titles.values() if not t)
                if miss and not ctx.dry_run:
                    self.log(ctx, f"词 [{word}] 标题快照缺失 {miss}/{len(titles)} 个"
                                  f"(h2/锚点/aria-label 均未取到), 对应行将标'待人工'")
                return asins, titles
            except Exception as exc:
                last_err = str(exc)
                self.log(ctx, f"词 [{word}] 第 {attempt} 次搜索失败: {last_err}")
        raise StepFailedError(f"搜索收集失败: 词 [{word}] 重试 {retries} 次仍失败: {last_err}")

    def _verify_summary(self, ctx: StepContext, collected: dict, verified: dict) -> None:
        """收集正确性审计: 逐 词×ASIN 生成 标题×规则判定 清单 CSV + 汇总日志(零额外请求)。"""
        rows: list[dict] = []
        for word, asins in collected.items():
            titles = verified.get(word, {}) or {}
            for asin in asins:
                title = titles.get(asin, "")
                if not title:
                    verdict = "无标题快照(旧数据/解析缺失)"
                    reason = "标题缺失, 人工确认"
                else:
                    verdict, reason = _verify_relevance(word, title)
                rows.append({"关键词": word, "ASIN": asin, "标题": title,
                             "判定": verdict, "依据": reason})
        if not rows:
            return
        csv_path = ctx.resolve_artifact(f"{ctx.config.cat}-search-verify.csv")
        _write_verify_csv(csv_path, rows)
        ok_n = sum(1 for r in rows if r["判定"] == "相关")
        low_n = len(rows) - ok_n
        self.log(ctx, f"搜索校验: 共 {len(rows)} 条(词×ASIN), 标题规则命中 {ok_n} 条, "
                      f"待人工确认 {low_n} 条 -> {csv_path}")
        if low_n:
            sample = [r["ASIN"] for r in rows if r["判定"] != "相关"][:10]
            self.log(ctx, f"  待人工确认前 {len(sample)} 个 ASIN: " + "、".join(sample)
                          + ("…" if low_n > len(sample) else ""))

    def _screenshot(self, browser, page, screenshot_dir: str, word: str) -> None:
        safe = re.sub(r"[^\w\-]+", "_", word).strip("_") or "keyword"
        browser.screenshot(page, os.path.join(screenshot_dir, f"{safe}.png"))

    def _merge_all(self, ctx: StepContext, collected: dict) -> list[str]:
        merged: list[str] = []
        for asins in collected.values():
            merged.extend(asins)
        all_path = ctx.resolve_artifact(f"{ctx.config.cat}-bestseller-newrelease-all.txt")
        if os.path.exists(all_path):
            merged.extend(read_txt_lines(all_path))
        else:
            self.log(ctx, "双榜清单缺失, 跳过并入(仅搜索 ASIN)")
        return _dedup(merged)


def parse_keywords_file(lines: list[str]) -> tuple[list[str], list[str]]:
    """解析分区关键词文件: [核心大词](=全部大词候选, 2026-09 不再固定 5 个) + [长尾词] 20 行。"""
    core: list[str] = []
    longtail: list[str] = []
    section = ""
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s
            continue
        if section == "[核心大词]":
            core.append(s)
        elif section == "[长尾词]":
            longtail.append(s)
    return core, longtail


def _norm_screen(records: list[dict]) -> dict:
    out = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        kw = str(rec.get("keyword", "")).strip()
        if kw:
            out[kw] = str(rec.get("verdict", "") or "").strip()
    return out


def _is_noise_word(word: str) -> bool:
    s = str(word).strip().lower()
    if not s:
        return True
    return s.split()[0] in _NOISE_PREFIXES


def _longtail_relevance(long_word: str, core_words: list[str]) -> float:
    lw = long_word.lower()
    l_tokens = {t for t in lw.split() if len(t) > 2}
    score = 0.0
    for core in core_words:
        c = core.lower().strip()
        if not c:
            continue
        if c in lw:
            score += 3.0
        c_tokens = {t for t in c.split() if len(t) > 2}
        score += float(len(c_tokens & l_tokens))
    return score


def _accessory_terms_from(prompt: dict) -> set:
    """从 FR-02 过滤提示词取 配件/耗材/硬排除 词(命中即判定非主营/剔除)。"""
    terms: set[str] = set()
    for k in ("accessories", "consumables", "hard_exclude"):
        for t in (prompt.get(k) or []):
            s = str(t).strip().lower()
            if s:
                terms.add(s)
    return terms


def _matches_accessory(word: str, terms: set) -> bool:
    """长尾词是否命中 配件/耗材/硬排除 词。"""
    if not terms:
        return False
    wl = word.lower()
    wnorm = wl.replace(" ", "")
    for t in terms:
        if not t:
            continue
        if t in wl or t.replace(" ", "") in wnorm:
            return True
    return False


def _core_product_heads(core_words: list[str]) -> set:
    """核心大词的**产品名词头**(取最长实词, 如 "air purifiers for home" → "purifiers")。

    2026-09-10 修正: 不能取末尾词——末尾常是 home/for 等场景/虚词, 会把
    "ionizers for home" 误判成围绕主营产品; 产品名词通常比修饰词长(purifier > home)。
    """
    heads: set[str] = set()
    for c in core_words:
        toks = [t for t in c.lower().split() if len(t) >= 4 and t not in _STOP_TERMS]
        if not toks:
            toks = [t for t in c.lower().split() if t]
        if toks:
            heads.add(max(toks, key=len))
    return heads


def _contains_core_product(word: str, heads: set) -> bool:
    """长尾词是否含主营产品名词头(与核心词共头, 容忍复数/派生)。"""
    if not heads:
        return True
    toks = [t for t in word.lower().split() if len(t) >= 4]
    for t in toks:
        for h in heads:
            if h in t or t[:5] == h[:5]:
                return True
    return False


def _top_related_longtail(core_words: list[str], longtail_words: list[str], top_n: int,
                          bad_terms: set | None = None, log=None) -> list[str]:
    """按相关度打分取前 top_n 个长尾词; 剔除 ①噪声词(best/cheap/new 开头) ②命中配件/耗材/硬排除。

    2026-09-13 变更(③): "必须含主营产品名词头"由**硬门槛**改为**排序权重** —— 硬门槛在实战中
    会把 20 条长尾全剔(Food Processors 的根词与达标词无共同词头 → 相关长尾 0), 而长尾是否有价值
    由后续搜索的标题快照规则校验兜底。一条都不含词头时打 WARN(提示 FR-04 根词可能与达标词脱节)。
    bad_terms: 配件/耗材/硬排除词; log: 可选日志回调(用于 WARN)。
    """
    bad_terms = bad_terms or set()
    heads = _core_product_heads(core_words)
    scored = []
    head_hits = 0
    for idx, w in enumerate(longtail_words):
        if _is_noise_word(w):
            continue
        if _matches_accessory(w, bad_terms):
            continue
        hit = _contains_core_product(w, heads)
        if hit:
            head_hits += 1
        scored.append((0 if hit else 1, -_longtail_relevance(w, core_words), idx, w))
    if heads and not head_hits and log:
        log(f"长尾词 {len(scored)} 条全部不含核心大词产品名词头 {sorted(heads)}"
            f"(已降级为排序权重, 仍按相关度选取) —— 请检查 FR-04 根词/长尾是否与达标词脱节")
    scored.sort()
    return [w for *_, w in scored[:top_n]]


def _extract_natural_asins(html: str) -> list[str]:
    """提取 /dp/ASIN 链接, 跳过链接附近含 Sponsored 广告标记的条目(旧全文兜底)。"""
    html = html or ""
    asins: list[str] = []
    for m in _DP_ASIN_RE.finditer(html):
        start = max(0, m.start() - _SPONSOR_WINDOW)
        window = html[start : m.end() + _SPONSOR_WINDOW]
        if "Sponsored" in window or "sponsored" in window.lower():
            continue
        asin = m.group(1)
        if asin not in asins:
            asins.append(asin)
    return asins


def _extract_result_items(html: str) -> list[dict]:
    """2026-09-08: 只解析搜索结果主列表结果卡, 返回 [{"asin", "title"}...]。

    定位 = data-component-type="s-search-result"(广告卡 sp-sponsored-result /
    页尾推荐模块无该标记 -> 天然排除, 不再全文扫 /dp/)。
    ASIN 提取(2026-09-08 二次修, 用户指出"有的 ASIN 只在标题链接里"):
    **卡内第一个 /dp/ 链接(标题/主图链接)优先**, 结果卡标签内的 data-asin 兜底——
    不要求两者在同一个标签上, 真实页面结构漂移不漏收。
    标题取卡内 h2 文本 -> /dp/ 锚点文本 -> h2 aria-label(多路兜底, 空则标题留空待人工)。
    """
    html = html or ""
    # 2026-09-08: 先剥掉 script/style/注释——页面内嵌 JSON/脚本里大量
    # "data-component-type"、"Sponsored" 等字符串会干扰卡片分段与广告判定
    html = _STRIP_SCRIPT_RE.sub(" ", html)
    html = _STRIP_STYLE_RE.sub(" ", html)
    html = _STRIP_COMMENT_RE.sub(" ", html)
    items: list[dict] = []
    info: dict[str, dict] = {}     # asin -> {ad, title}(同一卡可能有重复 DOM 副本)
    order: list[str] = []
    for m in _RESULT_CARD_ATTR.finditer(html):
        pos = m.start()
        # 定位承载该属性的 <div ...> 开标签: 上一个已闭合标签(>)之后最近的 <div
        tag_asin = ""
        tag_end = pos
        tag = ""
        prev_gt = html.rfind(">", 0, pos)
        tag_start = html.rfind("<div", prev_gt + 1, pos)
        if tag_start >= 0:
            gt = html.find(">", pos)
            if gt >= 0:
                tag_end = gt + 1
                tag = html[tag_start : gt + 1]
                ma = _ASIN_ATTR_RE.search(tag)
                tag_asin = ma.group(1) if ma else ""
        # 卡片段: 本卡开标签结束 -> 下一张卡(含广告卡)开始
        seg_end = len(html)
        nxt = _NEXT_CARD_RE.search(html, pos + len('data-component-type="s-search-result"'))
        if nxt:
            seg_end = nxt.start()
        seg = html[tag_end:seg_end]
        md = _DP_IN_SEG_RE.search(seg)
        asin = md.group(1) if md else tag_asin
        if not asin:
            continue
        # 广告排除(2026-09-08 真实页面实测): 卡标签类含 AdHolder(新布局广告卡
        # 也带 s-search-result) / 卡内 sponsored-ads 点击链接 / 可见 Sponsored 标签
        ad = bool(_AD_MARK_RE.search(tag + seg[:2500]))
        rec = info.get(asin)
        if rec is None:
            rec = {"ad": False, "title": ""}
            info[asin] = rec
            order.append(asin)
        if ad:
            rec["ad"] = True
        if not rec["title"]:
            rec["title"] = _extract_title(seg)
    for asin in order:
        if not info[asin]["ad"]:
            items.append({"asin": asin, "title": info[asin]["title"]})
    return items


def _extract_title(segment: str) -> str:
    """取结果卡片段里的商品标题文本(2026-09-09 拓宽兜底, 适配客户端渲染/结构漂移)。

    候选依次取**第一个非空**: ①卡内 h2 文本 → ②卡内 /dp/ 锚点内部文本(≥3字)
    → ③/dp/ 锚点 aria-label → ④h2 aria-label → ⑤a.a-link-normal 内文本
    → ⑥span.a-size-base-plus/a-size-mini 文本 → ⑦span.a-color-base 文本
    → ⑧span.a-text-normal 文本 → ⑨img alt。全空返回 ""(标"无标题快照/待人工")。
    """
    seg = (segment or "")[:6000]

    def _clean(body: str) -> str:
        text = _TAG_RE.sub(" ", body or "")
        text = unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:300]

    m = _H2_RE.search(seg)
    if m:
        t = _clean(m.group(1))
        if t:
            return t
    for m in _DP_ANCHOR_RE.finditer(seg):
        t = _clean(m.group(1))
        if t and len(t) >= 3:
            return t
    for m in re.finditer(
            r'<a[^>]*href="[^"]*/dp/[^"]*"[^>]*aria-label="([^"]+)"', seg, re.I | re.S):
        t = unescape(m.group(1)).strip()
        if t:
            return t[:300]
    m = _H2_ARIA_RE.search(seg)
    if m:
        t = unescape(m.group(1)).strip()
        if t:
            return t[:300]
    for m in re.finditer(
            r'<a[^>]*class="[^"]*a-link-normal[^"]*"[^>]*>(.*?)</a>', seg, re.S):
        t = _clean(m.group(1))
        if t and len(t) >= 3:
            return t
    for m in re.finditer(
            r'<span[^>]*class="[^"]*a-size-(?:base-plus|mini)[^"]*"[^>]*>(.*?)</span>',
            seg, re.S):
        t = _clean(m.group(1))
        if t and len(t) >= 3:
            return t
    for m in re.finditer(
            r'<span[^>]*class="[^"]*a-color-base[^"]*"[^>]*>(.*?)</span>', seg, re.S):
        t = _clean(m.group(1))
        if t and len(t) >= 3:
            return t
    for m in re.finditer(
            r'<span[^>]*class="[^"]*a-text-normal[^"]*"[^>]*>(.*?)</span>', seg, re.S):
        t = _clean(m.group(1))
        if t and len(t) >= 3:
            return t
    for m in re.finditer(r'<img[^>]*alt="([^"]+)"', seg, re.I):
        t = unescape(m.group(1)).strip()
        if t and len(t) >= 3:
            return t[:300]
    return ""


def _verify_relevance(word: str, title: str) -> tuple[str, str]:
    """标题×关键词本地规则校验(零额外请求): 返回 (判定, 依据)。

    相关 = 标题含整词短语, 或标题含关键词**全部内容词**(排除 for/with/of 等虚词);
    任一内容词缺失 -> 待人工(规则未命中, 可能是同义/复数写法差异, 不自动剔除)。
    """
    w = re.sub(r"\s+", " ", str(word).strip().lower())
    t = re.sub(r"\s+", " ", str(title).strip().lower())
    if not w or not t:
        return "无标题快照(旧数据/解析缺失)", "标题缺失, 人工确认"
    if w in t:
        return "相关", f"标题含整词短语 [{word}]"
    terms = [x for x in w.split() if len(x) > 2 and x not in _STOP_TERMS]
    if not terms:
        return "相关", "关键词过短/全虚词, 无法规则校验, 默认相关"
    hit = [x for x in terms if x in t]
    miss = [x for x in terms if x not in t]
    if not miss:
        return "相关", "标题命中关键词全部内容词: " + "、".join(hit)
    return "待人工(规则未命中)", f"命中 {hit or '(无)'}, 缺失 {miss}"


def _load_verified(path: str) -> dict:
    """载入标题快照: {keyword: {asin: title}}(兼容旧格式 list[{asin,title}] / list[str])。"""
    data = read_json(path, default={})
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for word, entries in data.items():
        titles: dict[str, str] = {}
        if isinstance(entries, dict):       # {asin: title}
            titles = {str(k): str(v or "") for k, v in entries.items()}
        elif isinstance(entries, list):
            for e in entries:
                if isinstance(e, dict) and e.get("asin"):
                    titles[str(e["asin"])] = str(e.get("title", "") or "")
                elif isinstance(e, str):
                    titles[e] = ""
        out[str(word)] = titles
    return out


def _write_verify_csv(path: str, rows: list[dict]) -> None:
    """写搜索校验审计 CSV(utf-8-sig, Excel 友好): 关键词/ASIN/标题/判定/依据。"""
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=["关键词", "ASIN", "标题", "判定", "依据"])
        writer.writeheader()
        writer.writerows(rows)


def _dedup(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _load_existing(path: str) -> dict:
    data = read_json(path, default={})
    if not isinstance(data, dict):
        data = {}
    return {str(k): (v if isinstance(v, list) else []) for k, v in data.items()}
