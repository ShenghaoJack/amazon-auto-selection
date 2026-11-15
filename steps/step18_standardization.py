# -*- coding: utf-8 -*-
"""步骤18：导出面板确认的私有JSON有效版本，保留上游标注及规则。

面板支持多值归并、按要求重判、数值/跨列复核、历史回滚。交付包含来源、
依据、原始行号及版本记录。尚未使用新版面板的CLI任务保留原标准化入口。
"""
from __future__ import annotations

import json
import os

from core.errors import StepDataError, WaitingForHuman
from steps.base import StepContext
from steps.step17_attr_annotation import (
    RESULT_NAME,
    STD_XLSX_TPL,
    VOCAB_CONFIRMED,
    Step17AttrAnnotation,
)

STEP17_OUT_DIR = "17_属性标注"
STD_OUT_DIR = "18_标准化"


class Step18Standardization(Step17AttrAnnotation):
    number = 18
    title = "标准化"
    description = ("FR-18: 只读步骤17标注，多选值归并、自定义重判、异常与跨列复核；"
                   "预览确认、JSON版本及回滚，导出含来源与依据的标准化交付 xlsx")

    @property
    def artifacts(self) -> list[str]:
        cat = self._cat or ""
        return [f"{STD_OUT_DIR}/{STD_XLSX_TPL.format(cat=cat)}"]

    # Only this new Step18-owned directory participates in the panel reset/archive.
    archive_dirs = ["18_标准化/版本数据"]

    def _panel_workspace(self, ctx):
        from pathlib import Path
        from tools.attr_stability.step18_workspace import Workspace
        spec = Path(ctx.resolve_artifact(f"{self._cat}-attribute-spec.json"))
        root = Path(ctx.paths.cat_root)
        return Workspace(root, spec_path=spec, rules_path=self._rules_json_path(ctx))

    # Legacy Step18-owned outputs only. extra_archive_files also determines reset
    # deletion, so Step17's vocabulary reports/history/pending list must stay out.
    @property
    def extra_archive_files(self) -> list[str]:
        cat = self._cat or ""
        return [
            f"{STD_OUT_DIR}/新词初审裁决.json",
            f"{STD_OUT_DIR}/新词自动采纳记录.json",
            # ↓ 2026-10-14 补: 实测被漏归档的校验/冲突类产物
            f"{STD_OUT_DIR}/交付值校验.json",
            f"{STD_OUT_DIR}/规则执行与输出校验.json",
            f"{STD_OUT_DIR}/跨属性冲突.json",
            f"{STD_OUT_DIR}/跨属性冲突纠正.json",
            f"{STD_OUT_DIR}/{cat}-数值异常校验.json",
            f"{STD_OUT_DIR}/{cat}-数值异常重跑指令.md",
        ]

    def validate_upstream(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        if not self._cat:
            raise ValueError("配置缺少 CAT")
        step17_dir = ctx.resolve_artifact(STEP17_OUT_DIR)
        if not os.path.exists(os.path.join(step17_dir, RESULT_NAME)):
            raise StepDataError("缺少步骤17 的 %s/%s(步骤17 未完成?)" % (STEP17_OUT_DIR, RESULT_NAME))
        if not os.path.exists(os.path.join(step17_dir, "input.json")):
            raise StepDataError("缺少步骤17 的 input.json(步骤17 未完成?)")
        sp = ctx.resolve_artifact(f"{self._cat}-attribute-spec.json")
        if not os.path.exists(sp):
            raise StepDataError("缺少属性集规格 %s(步骤16 未完成?)" % (self._cat + "-attribute-spec.json"))
        try:
            with open(sp, encoding="utf-8-sig") as fh:
                spec = json.load(fh)
            self._spec_attributes = [a for a in (spec.get("attributes") or [])
                                     if isinstance(a, dict) and str(a.get("name") or "").strip()]
        except Exception:
            self._spec_attributes = []
        # ★★ 词表确认闸门（2026-10-09，方案 A 的「停②」）
        #   链路：16 提取属性 →[停①]确认属性集 → 17 标注 **+ 产词表** →[停②]确认词表 → 18 应用词表
        #   为什么要有它：词表是**业务口径**（哪些值算同一个），模型聚出来的只是初稿，
        #   必须让人过一眼。步骤17 结束时置 `pending_confirm`，人工在 Web
        #   「标准化词表」面板调整并**保存**（保存即确认）之后才放行。
        #   与「属性集规格未确认」那一套**同构** —— 项目里已有的模式，不新造机制。
        # ★★ 修复（审核中-5，2026-10-10）：缺 `rules.json` 是**上游产物缺失**（步骤16/17 没完成），
        #   不该被报成「等人工确认词表」（rc 3）—— 那会把人引到 Web 面板找一个不存在的确认动作。
        #   对齐步骤17 对缺 spec 的处理（`step17:143` 抛 `StepDataError` = rc 2）。
        if not (self._rules_json_path(ctx) and os.path.exists(self._rules_json_path(ctx))):
            raise StepDataError("缺少 rules.json(步骤16 未完成?) —— 这是上游产物缺失，"
                                "不是等待人工确认")
        workspace = self._panel_workspace(ctx)
        if workspace.pointer.exists():
            current = workspace.open()
            workspace._fresh(current)
            from tools.attr_stability.step18_workspace import read_json
            exported = workspace.store / "export.json"
            if not exported.exists() or read_json(exported).get("version") != current["version"]:
                raise WaitingForHuman("步骤18当前版本尚未确认导出", hint="在新面板确认处理结果并导出交付，再继续流水线")
            self._verify_result_contract(ctx)
            return
        _vs = self._vocab_status(ctx)
        if _vs != VOCAB_CONFIRMED:
            # ★ 「停②」：不是失败，是**等人工确认词表**（2026-10-09 方案 A）。
            raise WaitingForHuman(
                "标准化词表未确认(当前状态: %s)" % (_vs or "缺失"),
                hint="在 Web 的「标准化词表」面板调整并保存（保存即确认）后，"
                     "重跑本命令即从本步续跑")
        self._verify_result_contract(ctx)

    def run(self, ctx: StepContext) -> None:
        self._cat = ctx.config.cat
        step17_dir = ctx.resolve_artifact(STEP17_OUT_DIR)
        workspace = self._panel_workspace(ctx)
        if workspace.pointer.exists():
            current = workspace.open()
            workspace.export(current["version"])
            self.log(ctx, "已输出步骤18面板确认的有效版本 " + current["version"])
            return

        # ① 读步骤17 产物 → 轻量 res 命名空间
        with open(os.path.join(step17_dir, RESULT_NAME), encoding="utf-8") as fh:
            data = json.load(fh)
        res = type("Res", (), {
            "cells": data.get("cells") or {},
            "deliverable": data.get("deliverable") or [],
            "vocab_candidates": data.get("vocab_candidates") or [],
        })()

        # ①b 空交付硬拦（2026-09-29）
        # 步骤17 的多数表决要求 ≥2 份运行过闸门；不足时 run_round 提前返回空交付
        # （deliverable=[] / cells={}）。此处必须拒绝：否则下面会照常走
        # 冷启动词表生成→归并标准化→写交付表，把**上游原始表**当成"标准化交付"
        # 写出去（78 个输入列、0 个属性列），流水线还报"全部步骤完成"。
        # 实测：Salt and Pepper 2026-09-29（a1 末批 LLM 超时被弃 + a2 过闸门失败）。
        # 放在 makedirs 之前，避免为一次注定失败的运行留下空目录。
        if not res.deliverable:
            raise StepDataError(
                "步骤17 交付为空(0 行 / 0 属性)：多数表决需要 ≥2 份运行过闸门，本次未满足。"
                "拒绝写出空交付表，请先重跑步骤17。（%s）"
                % os.path.join(step17_dir, RESULT_NAME))
        if self._vocab_status(ctx) != VOCAB_CONFIRMED:
            raise WaitingForHuman("标准化词表未确认", hint="请保存确认词表后再运行步骤18")
        self._verify_result_contract(ctx)

        out_dir = ctx.resolve_artifact(STD_OUT_DIR)
        os.makedirs(out_dir, exist_ok=True)

        # ② 输入行(步骤17 的 input.json 快照)
        from tools.attr_stability import load_rows
        rows = load_rows(os.path.join(step17_dir, "input.json"))

        # ③ 视觉补充(步骤17 的 视觉补充.json: 值 + 来源)
        vision: dict = {}
        vision_src: dict = {}
        vp = os.path.join(step17_dir, "视觉补充.json")
        if os.path.exists(vp):
            with open(vp, encoding="utf-8") as fh:
                vd = json.load(fh)
            for k, v in (vd.get("filled") or {}).items():
                rid, _, attr = k.partition("|")
                try:
                    vision[(int(rid), attr)] = v
                except ValueError:
                    vision[(rid, attr)] = v
            for k, v in (vd.get("sources") or {}).items():
                rid, _, attr = k.partition("|")
                try:
                    vision_src[(int(rid), attr)] = v
                except ValueError:
                    vision_src[(rid, attr)] = v

        # ④ 类目规则
        cat_dir, cat_id = self._resolve_rules(ctx, writable=True)
        if cat_dir is None:
            raise StepDataError("找不到类目规则文件(*.rules.json), 请确认 data/<CAT>/ 下有 <snake>.rules.json")
        from tools.attr_stability import RuleSource
        rules = RuleSource.load(cat_dir, cat_id)

        # ★ 2026-10-27 用户口径: **产品类型词表即"合法集合"** —— 用户在词表面板里删掉的产品类型,
        #   对应的整行（所有属性）都不再进入交付。这样"删词表"就等于"删数据行", 无需硬编码。
        _pspec = rules.attrs.get("产品类型")
        _pvals = {str(v).strip() for v in (getattr(_pspec, "values", []) or [])
                  if str(v).strip()} if _pspec is not None else set()
        if _pvals:
            _ptype_of = {d.get("xl_row"): str(((d.get("attributes") or {}).get("产品类型") or "")).strip()
                         for d in (res.deliverable or [])}
            # ★ 2026-10-28 用户口径: 步骤18 **不做整行剔除**。
            #   词表是"标准化用的推荐集合"，不是行过滤器；标注出来的真实值一律保留，
            #   只把不在词表里的值记进日志（人工想删数据就在 Excel/上游处理）。
            _outside = {rid: pt for rid, pt in _ptype_of.items() if pt and pt not in _pvals}
            if _outside:
                _cnt = {}
                for _pt in _outside.values():
                    _cnt[_pt] = _cnt.get(_pt, 0) + 1
                self.log(ctx, "ℹ 产品类型有 %d 行不在词表内（**保留，不剔除**）: %s"
                         % (len(_outside),
                            "、".join("%s×%d" % (k, v) for k, v in
                                      sorted(_cnt.items(), key=lambda kv: -kv[1]))))
            if not res.deliverable:
                raise StepDataError("没有可交付行，请检查标注结果")

        from tools.attr_stability.rule_pipeline import rule_configs
        with open(os.path.join(cat_dir, cat_id + ".rules.json"), encoding="utf-8-sig") as fh:
            definitions = json.load(fh)
        try:
            delivery_configs = rule_configs(definitions, cells=res.cells)
        except ValueError as exc:
            raise StepDataError("标准化规则声明无效: %s" % exc) from exc
        # ★ 2026-10-22 新增: **单位一致性复查**。步骤16 的 hint 已要求数值类属性锁定单一单位,
        #   这里在标准化时再核一遍: 同一数值属性出现多种单位(inch / cm / mm / lb / kg …)
        #   → 报警, 提示回步骤16 改 hint。只报警不阻断(避免把已有流程卡死)。
        try:
            self._check_unit_mix(ctx, definitions, res.deliverable)
        except Exception as exc:  # noqa: BLE001
            self.log(ctx, "⚠ 单位一致性检查失败(忽略): %s" % exc)

        # 概览日志
        attr_names = []
        for d in (res.deliverable or []):
            for a in (d.get("attributes") or {}):
                if a not in attr_names:
                    attr_names.append(a)
        self.log(ctx, "标准化开始: 读入步骤17 原始标注 %d 行 × %d 属性"
                 "(词表外候选 %d 个, 视觉补充 %d 格)"
                 % (len(rows), len(attr_names), len(res.vocab_candidates or []), len(vision)))

        # ⑤ 归并标准化 + 标准化交付 + 新词初审
        # ★ 2026-10-09：`_generate_vocab` **已移到步骤17 末尾**（方案 A 的「停②」）——
        #   原先这里自己生成词表，导致"步骤17 跑完时没有新词表可调"，
        #   用户要的"停下来调词表再继续"根本落不了地。
        #   现在步骤18 **只应用**已确认的词表；未确认会被 `validate_upstream` 拒绝。
        # 视觉补充的值**并进 cells** 作为额外提名, 与文本标注一起走机械收敛+语义归类,
        # 而不是绕过标准化直接塞进结果(否则视觉补的原文片段不会被收敛成受控值)。
        # 来源也注入 cells(attr.<属性>_来源 格), 供交付表来源列。
        self._merge_vision_into_cells(res, vision, vision_src)
        std_map = self._converge_standardize(ctx, rules, res, rows)
        self.log(ctx, "归并标准化收口: %d 格有标准化值(含视觉补充 %d 格)" % (len(std_map), len(vision)))
        # ★ 校验 + 纠正 跨属性约束违例（用户口径 2026-10-09）：
        #   「既然有冲突，应该再加个校验和纠正的步骤」——
        #   此前只写 `跨属性冲突.json` 登记，冲突行**原样进交付表**。
        #   必须放在 `_write_deliverables` **之前**，否则改的是已经写出去的交付表。
        try:
            _spec_for_corr = {}
            try:
                from tools.attr_stability import attr_spec_from_modern
                _spec_for_corr = attr_spec_from_modern(rules) or {}
            except Exception:  # noqa: BLE001
                _spec_for_corr = {n: {"values": list(s.values or [])}
                                  for n, s in rules.attrs.items()}
            # ★★ `load_cross_checks` 要求**真正的 Mapping**（dict），而这里的 `rules`
            #    是 `RuleSource` **对象** → `isinstance(x, Mapping)` 为假 →
            #    **静默返回空** → 纠正步骤一次都没跑过（实测：没有
            #    `跨属性冲突纠正.json`，步骤18 只有 2 次调用）。
            #    所以必须把 **rules.json 的原始 dict** 传进去。
            _rules_raw = {}
            try:
                _rp = os.path.join(cat_dir, cat_id + ".rules.json") if (cat_dir and cat_id) else ""
                if _rp and os.path.exists(_rp):
                    with open(_rp, encoding="utf-8-sig") as _fh:
                        _rules_raw = json.load(_fh)
            except (OSError, ValueError) as exc:
                self.log(ctx, "⚠ 读 rules.json 原始声明失败(跨属性纠正跳过): %s" % exc)
            if not _rules_raw:
                self.log(ctx, "⚠ 拿不到 rules.json 原始声明 → 跨属性纠正跳过"
                              "（冲突仍会被登记）")
            else:
                _corr = self._correct_cross_conflicts(ctx, _rules_raw, res, rows, std_map,
                                                      attrs_spec=_spec_for_corr)
                if _corr:
                    self.log(ctx, "跨属性冲突纠正: 改正 %d 处 · 待人工 %d 处"
                             % (len(_corr.get("已纠正") or []), len(_corr.get("待人工") or [])))
        except Exception as exc:  # noqa: BLE001 — 纠正失败不影响交付（冲突仍会被登记）
            self.log(ctx, "⚠ 跨属性冲突纠正失败(忽略, 冲突照旧登记): %s" % exc)
        # 纠正可能合法追加受控值，重读同一份规则后做交付前终检。
        rules = RuleSource.load(cat_dir, cat_id)
        with open(os.path.join(cat_dir, cat_id + ".rules.json"), encoding="utf-8-sig") as fh:
            definitions = json.load(fh)
        validation = self._validate_delivery_values(rules, res, std_map, definitions=definitions)
        from tools.attr_stability.rule_pipeline import project_columns
        delivery_configs = rule_configs(definitions, cells=res.cells)
        execution, self._delivery_provenance = project_columns(delivery_configs, res, rows, std_map)
        self._delivery_rule_configs = delivery_configs
        with open(os.path.join(out_dir, "规则执行与输出校验.json"), "w", encoding="utf-8") as fh:
            json.dump({"已执行格数": len(execution), "问题格数": sum(bool(r["问题"]) for r in execution),
                       "记录": execution}, fh, ensure_ascii=False, indent=2)
        validation["规则执行格数"] = len(execution)
        validation["拆分输出列"] = {c["name"]: c["subcols"] for c in delivery_configs}
        with open(os.path.join(out_dir, "交付值校验.json"), "w", encoding="utf-8") as fh:
            json.dump(validation, fh, ensure_ascii=False, indent=2)
        self.log(ctx, "交付值终检: 校验 %d 格，词表外值 %d 格（已保留原值），无词表属性 %s"
                 % (validation["已校验格数"], len(validation["词表外保留"]),
                    validation["无词表属性"]))
        self._write_deliverables(ctx, out_dir, rows, res, std_map, raw_too=False)
        self._review_vocab(ctx, out_dir, res, cat_dir, cat_id)

    @staticmethod
    def _check_unit_mix(ctx, definitions: dict, deliverable: list) -> None:
        """数值类属性的**单位一致性复查**（2026-10-22）。

        为什么需要: 步骤16 的 hint 现在要求"数值属性锁定单一单位"(尺寸 cm / 功率 W / 噪音 dB …),
        但历史 spec 里可能仍然混着写(实测 `尺寸规格` 的举例混用 inch / mm / cm)。
        这里在标准化阶段再核一遍: 同一数值属性出现多种单位 → 报警, 提示回步骤16 改 hint。

        只报警、**不阻断** —— 单位换算靠 rules.json 的 `unit_factors`, 混单位不一定会出错值。
        实现要点: 用**单位白名单**(不是"数字后面跟什么都算"), 否则 `43x33cm` 里的分隔符 `x`
        会被误当成单位(实测踩过)。
        """
        import re
        from collections import Counter, defaultdict

        # 单位白名单 → 归一化写法（长词优先，避免 `inch` 被 `in` 吃掉）
        alias = [
            ("fl oz", "fl oz"), ("fl.\u00a0oz", "fl oz"),
            ("inch", "inch"), ("inches", "inch"), ("厘米", "cm"), ("毫米", "mm"), ("英寸", "inch"),
            ("英尺", "ft"), ("公斤", "kg"), ("千克", "kg"), ("磅", "lb"), ("分贝", "dB"),
            ("毫升", "mL"), ("升", "L"), ("瓦", "W"), ("克", "g"),
            ("mm", "mm"), ("cm", "cm"), ("kg", "kg"), ("lbs", "lb"), ("lb", "lb"),
            ("kw", "kW"), ("hp", "HP"), ("db", "dB"), ("ml", "mL"), ("ft", "ft"),
            ("mm", "mm"), ("in", "inch"), ("m", "m"), ("g", "g"), ("w", "W"), ("l", "L"),
            ('"', "inch"), ("\u2033", "inch"), ("\u2032", "ft"),
        ]
        alt = "|".join(re.escape(k) for k, _ in sorted(alias, key=lambda kv: -len(kv[0])))
        unit_rx = re.compile(r"(\d+(?:\.\d+)?)\s*(%s)" % alt, re.I)
        norm = dict(alias)
        numeric = {str(a.get("name") or ""): a
                   for a in (definitions.get("attributes") or []) if isinstance(a, dict)}
        by_attr: dict[str, Counter] = defaultdict(Counter)
        for d in (deliverable or []):
            for name, val in (d.get("attributes") or {}).items():
                if str(name).endswith(("_依据", "_来源")):
                    continue
                spec = numeric.get(str(name)) or {}
                if str(spec.get("rule") or "").lower() != "numeric":
                    continue
                for m in unit_rx.finditer(str(val or "")):
                    raw = m.group(2).strip()
                    u = norm.get(raw) or norm.get(raw.lower())
                    if u:
                        by_attr[str(name)][u] += 1
        mixed = {k: v for k, v in by_attr.items() if len(v) > 1}
        if mixed:
            ctx.log.warning("⚠ 单位不统一: %d 个数值属性混用了多种单位（建议回步骤16 在 hint 里锁定单一单位）"
                            % len(mixed))
            for name, units in list(mixed.items())[:8]:
                ctx.log.warning("   %s: %s" % (name, "、".join("%s×%d" % (u, n) for u, n in units.most_common())))
        else:
            ctx.log.info("单位一致性检查通过: %d 个数值属性均为单一单位" % len(by_attr))

    def _validate_delivery_values(self, rules, res, std: dict, *, definitions=None) -> dict:
        """阻止标准化失败时原始词表外值经回退进入正式交付。"""
        from tools.attr_stability.converge import controlled_value_valid
        from tools.attr_stability.unknowns import is_unknown

        checked = 0
        rejected = []
        empty = set()
        attr_defs = {a["name"]: a for a in (definitions or {}).get("attributes", [])
                     if isinstance(a, dict) and a.get("name")}
        for row in self._final_rows(res, std):
            rid = row["xl_row"]
            for attr, spec in rules.attrs.items():
                from tools.attr_stability.rule_pipeline import SPECIAL_RULES
                if attr_defs.get(attr, {}).get("rule") in SPECIAL_RULES:
                    continue  # 专用类型由其执行器和输出校验检查，而非按普通枚举解释。
                if spec.type in ("numeric", "open", "ocr") or spec.vocab_policy == "open":
                    continue  # 这些类型不使用受控词表，不能谎称已经过枚举校验
                if not spec.values:
                    empty.add(attr)
                    continue
                value = (row.get("attributes") or {}).get(attr, "")
                definition = attr_defs.get(attr, {})
                params = definition.get("params") or {}
                separators = params.get("value_separators") or definition.get("value_separators")
                separators = list(separators or ("、", ",", ";", "+"))
                if params.get("join_sep"):
                    separators.append(params["join_sep"])
                checked += 1
                if is_unknown(value):
                    std[(rid, attr)] = "未明确"
                elif not controlled_value_valid(value, spec.values, multi=spec.shape == "multi",
                                                 separators=separators):
                    # ★ 2026-10-28 用户口径: 词表外的值**保留原值**，不再改写成「未明确」。
                    #   词表是推荐集合；标注出来的真实值不该为了"合词表"被抹掉。
                    rejected.append({"行": rid, "属性": attr, "原值": value,
                                     "原因": "不在当前受控值词表内", "处理": "保留原值"})
        return {"已校验格数": checked, "词表外保留": rejected,
                "无词表属性": sorted(empty),
                "说明": "词表外值按原值保留；数值与开放文本不在本次枚举终检范围内"}

    @staticmethod
    def _merge_vision_into_cells(res, vision: dict, vision_src: dict | None = None) -> None:
        """把视觉补充的格注入 cells(作为额外提名, 来源标记 vision), 供统一收敛。

        cells 是步骤17 union_cells 产物: {"<行>|attr.<属性>": {"values":[{value, by}]}}
        (新 schema: 值格路径 = attr.<属性>, 来源格 = attr.<属性>_来源)。
        视觉补充补的格通常是"文本全判未明确"的格, 其 cells 里 values 都是 未明确;
        把视觉候选值作为新提名追加进去, converge_mechanical 会把它当普通提名一起查词表收敛。
        视觉来源也注入 attr.<属性>_来源 格(free 类, 供交付表来源列)。
        """
        vision_src = vision_src or {}
        for (rid, attr), v in vision.items():
            key = "%s|attr.%s" % (rid, attr)
            cell = res.cells.get(key)
            if not isinstance(cell, dict):
                # 文本侧根本没有这个格(极端情况) → 新建一个只含视觉提名的 cell
                res.cells[key] = {"kind": "value", "agree": True, "mode": v,
                                  "values": [{"value": v, "by": ["vision"]}],
                                  "n_votes": 1, "needs_review": False,
                                  "no_majority": False}
            else:
                values = cell.get("values")
                if not isinstance(values, list):
                    values = []
                # 视觉值作为新提名追加(来源标记 vision); 去重(同值不重复加)
                if not any(g.get("value") == v for g in values if isinstance(g, dict)):
                    values.append({"value": v, "by": ["vision"]})
                cell["values"] = values
                cell["n_votes"] = len(values)
            # 视觉来源 → attr.<属性>_来源 格(free 类)
            s = vision_src.get((rid, attr))
            if s:
                skey = "%s|attr.%s_来源" % (rid, attr)
                scell = res.cells.get(skey)
                if not isinstance(scell, dict):
                    res.cells[skey] = {"kind": "free", "agree": True, "mode": s,
                                       "values": [{"value": s, "by": ["vision"]}],
                                       "n_votes": 1, "needs_review": False,
                                       "no_majority": False}
                elif not any(g.get("value") == s for g in (scell.get("values") or [])
                             if isinstance(g, dict)):
                    scell.setdefault("values", []).append({"value": s, "by": ["vision"]})
            # ★ 2026-10-28 用户口径: 视觉补的值也要有**依据**，否则审计时看不出是模型推断的。
            #   视觉补充本身没有逐格依据，这里统一写成「图片视觉推断」，来源已有单独字段。
            bkey = "%s|attr.%s_依据" % (rid, attr)
            bcell = res.cells.get(bkey)
            _basis = ("图片视觉推断（看图判断，非原文摘录）")
            if not isinstance(bcell, dict):
                res.cells[bkey] = {"kind": "free", "agree": True, "mode": _basis,
                                   "values": [{"value": _basis, "by": ["vision"]}],
                                   "n_votes": 1, "needs_review": False,
                                   "no_majority": False}
            elif not str(bcell.get("mode") or "").strip():
                bcell["mode"] = _basis
                bcell.setdefault("values", []).append({"value": _basis, "by": ["vision"]})
