"""FR-02 生成过滤提示词(LLM)。

输入: 配置(类目名/过滤要求/LLM接口) + 通用过滤提示词模板
输出: 类目过滤提示词 filter-<类目名>-prompt.json
      (含 5 类过滤词参考: 硬排除/相关标题/相关卖点/配件/耗材, 供 FR-03/FR-10 共用)

逻辑: 构造生成提示词 -> LLM 生成 5 类过滤词(非思考+JSON 模式) -> 容错解析
      -> 组装为类目过滤提示词(FR-02.1 结构) -> 写 JSON
容错: LLM 失败 -> 提示词留空(判定时全保留不误杀); 多次完整尝试
"""
from __future__ import annotations

import os

from core.catname import display_cat
from core.io_utils import write_json
from steps._filtering import build_filter_prompt
from steps.base import BaseStep, StepContext


class Step02FilterPrompt(BaseStep):
    number = 2
    title = "生成过滤提示词"
    description = "FR-02: LLM 生成 5 类过滤词并组装为类目过滤提示词"

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")

    @property
    def artifacts(self) -> list[str]:
        return [f"filter-{self._cat}-prompt.json"]

    def run(self, ctx: StepContext) -> None:
        cat = ctx.config.cat
        requirement = ctx.config.get("FILTER_REQUIREMENT", "排除与本产品无关的")
        self.log(ctx, f"开始生成过滤提示词: CAT={cat} 要求={requirement}")

        five = self._generate_five_categories(ctx, cat, requirement)

        if not five:
            self.log(ctx, "LLM 生成失败, 输出空过滤提示词(判定时全保留不误杀)")
            fp = build_filter_prompt(cat, requirement, {})
        else:
            fp = build_filter_prompt(cat, requirement, five)
            self.log(ctx, f"5类词: 硬排除{len(fp['hard_exclude'])} 相关标题{len(fp['related_title'])} "
                          f"相关卖点{len(fp['related_bullets'])} 配件{len(fp['accessories'])} 耗材{len(fp['consumables'])}")
            if five.get("product_scope"):
                self.log(ctx, f"产品定义: {five['product_scope']}")

        path = ctx.resolve_artifact(self.artifacts[0])
        write_json(path, fp)
        self.log(ctx, f"过滤提示词已写入: {path}")

    def _generate_five_categories(self, ctx: StepContext, cat: str, requirement: str) -> dict:
        """调用 LLM 生成 产品定义 + 5 类过滤词, 多轮重试保证生成成功。"""
        display = display_cat(cat)   # 提示词用展示名(去掉 -v2/-202609/-US 等后缀)
        titles = self._sample_titles(ctx, cat)   # 真实在售商品标题样本(判断类目范围用)
        sample_block = "\n".join(titles) if titles else "(榜单详情缺失, 无商品样本)"
        prompt = f"""你是亚马逊类目选品过滤词生成助手。

本产品类型(类目): {display}
过滤要求: {requirement}

以下为本类目**榜单在售商品标题样本**(用于判断本类目实际包含哪些产品类型与买家常用词):
{sample_block}

请输出:
1. product_scope: 用一句话定义**本类目产品**(明确"包含哪些产品类型, 不含什么", 例如
   "盐与胡椒调味瓶(shaker)套装, 含研磨器/胡椒碾(grinder/mill)及电动版本; 不含调味品原料本身、配件替换件与耗材");
2. 5 类过滤判定参考词:
   1. hard_exclude: 硬排除词(标题命中即与本类目无关的词, 如其他产品类型)
   2. related_title: 相关标题词(标题含这些词即判定为本类目相关, **须覆盖本类目所有核心产品家族**)
   3. related_bullets: 相关卖点词(卖点含这些词即相关)
   4. accessories: 配件词(如替换件/配件, 标题命中即剔除)
   5. consumables: 耗材词(如耗材/补充装, 标题命中即剔除)

要求: 每类 5-10 个英文词组(小写); **本类目核心产品(见 product_scope)绝不能放进 hard_exclude**, 只放在 related_title。严格输出 JSON(不要输出其他任何文字):
{{"product_scope": "...", "hard_exclude": [...], "related_title": [...], "related_bullets": [...], "accessories": [...], "consumables": [...]}}
"""
        llm = ctx.llm()
        last_err = ""
        for attempt in range(3):
            try:
                raw = llm.chat_json(prompt, temperature=0.2, max_tokens=4000)
                if isinstance(raw, dict):
                    five = {
                        "hard_exclude": _norm_list(raw.get("hard_exclude")),
                        "related_title": _norm_list(raw.get("related_title")),
                        "related_bullets": _norm_list(raw.get("related_bullets")),
                        "accessories": _norm_list(raw.get("accessories")),
                        "consumables": _norm_list(raw.get("consumables")),
                    }
                    five["product_scope"] = str(raw.get("product_scope") or "").strip()
                    return five
            except Exception as exc:
                last_err = str(exc)
                self.log(ctx, f"第{attempt+1}次生成失败: {last_err}")
        self.log(ctx, f"LLM 生成最终失败: {last_err}")
        return {}

    def _sample_titles(self, ctx: StepContext, cat: str, limit: int = 12) -> list[str]:
        """读 FR-01 榜单详情, 取前 limit 条成功记录的真实标题(可选上下文, 缺失不阻断)。"""
        out: list[str] = []
        path = ctx.resolve_artifact(f"{cat}-dual-details.jsonl")
        if not os.path.exists(path):
            return out
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        import json as _json
                        rec = _json.loads(ln)
                    except Exception:
                        continue
                    if rec.get("ok") and str(rec.get("title") or "").strip():
                        out.append(str(rec["title"]).strip())
                        if len(out) >= limit:
                            break
        except OSError:
            return out
        return out


def _norm_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip().lower() for v in value if str(v).strip()]
