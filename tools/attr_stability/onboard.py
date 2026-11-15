# -*- coding: utf-8 -*-
"""词表放权（方案 B）：新词独立初审提示词 + 批量确认写回。

与 `gapfill`/`converge` 同属「标注之外、只做判断/收口」的配套层。

边界（与整个项目一致）：
  · 采纳/驳回是**判断** → 交给 LLM（且必须是**独立**角色，见 `build_vocab_review_prompt`）；
  · 把采纳的词写进 rules.json、去重、汇报差异是**机械** → 代码做（`apply_vocab_acceptance`）；
  · 本模块**不直接改受控值语义**，只把人工/机器确认的结果落到 rules.json 的
    `attributes[].values` 里；rules.md 需另行重渲染（见 prompt-engineering 的
    `d104_render_category_rules.py`）。
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


def build_vocab_review_prompt(candidates: Sequence[Mapping[str, Any]], cat: str = "") -> str:
    """把「新词候选」拼成一次**独立初审**提示词。

    candidates：run_round 产出的 `vocab_candidates`（见 attr_runner.RoundResult），
      每项含 `属性 / 词 / 出现次数 / 行号 / policy / 依据样本`。

    为什么强调"独立"：项目硬约束是"独立验证必须与被验证对象机制不同"——
    让写答案的模型再审自己刚冒出的新词，会自我洗白；这里要求它站在审校员立场。
    """
    lines = []
    for c in candidates:
        rows = c.get("行号") or []
        ev = " | ".join(str(x) for x in (c.get("依据样本") or [])[:3]) or "(无依据样本)"
        lines.append(
            "- 属性「%s」: 新词「%s」, 出现 %d 次, 行号 %s, policy=%s, 依据样本: %s"
            % (c.get("属性"), c.get("词"), int(c.get("出现次数") or 0),
               ",".join(str(r) for r in list(rows)[:6]) or "-",
               c.get("policy") or "closed", str(ev)[:80])
        )
    block = "\n".join(lines)
    return (
        "你是**独立**于标注器的类目词表审校员（你与「写答案」的模型是两个角色，请独立判断）。\n\n"
        "任务：下面这些词是标注时遇到、但**不在受控词表**里的词。请逐个判定是否应**采纳为新的受控值**。\n\n"
        "判据：\n"
        "  · 采纳：它确实是本类目商品真实存在、且能与其它值区分开的取值"
        "（不是笔误、不是编造、不是已有受控值的同义写法）；\n"
        "  · 驳回：错别字 / 单位·格式问题 / 与词表内某受控值同义 / 凭空编造。\n\n"
        "对每个词输出：{\"属性\": \"...\", \"词\": \"...\", \"结论\": \"采纳|驳回\", "
        "\"规范化写法\": \"采纳时给出应写进词表的中文写法\", \"理由\": \"≤30字\"}。\n\n"
        "类目：%s\n\n"
        "待审新词：\n%s\n\n"
        "严格输出 JSON：{\"items\": [ ... ]}\n" % (cat, block)
    )


def _vocab_history_block(history: Sequence[Mapping[str, Any]] | None) -> str:
    """多轮迭代: 拼「L1 全部口径建议 + L2 最近 5 轮完整结果」两段。

    · L1 = 每一轮的口径建议（累积意图，全部要遵守；元素短，成本低，**永不丢**）
    · L2 = 最近 5 轮的 规范值 + 归并（完整结果，供 LLM 在此基础上修正；
           更早轮次不带完整归并，避免 token 膨胀）
    """
    rows = [r for r in (history or []) if isinstance(r, Mapping)]
    if not rows:
        return ""
    out = ""
    hints = [(str(r.get("口径建议") or "").strip(), r.get("轮次")) for r in rows]
    hints = [(h, n) for h, n in hints if h]
    if hints:
        # ★ 2026-10-27: 原来是「累积意图，全部要遵守」——导致 AI 把旧口径套到新要求上。
        #   改成「只作背景，冲突以最新一条为准」。
        out += ("\n【历史口径建议（按轮次，越后越新；**只作背景参考**，"
                "与用户最新一条要求冲突时**以最新一条为准**，不要自行沿用旧口径）】\n")
        for h, n in hints:
            out += "- 第%s轮: %s\n" % (n if n is not None else "?", h[:200])
    recent = rows[-5:]
    if recent:
        out += "\n【最近 %d 轮结果（供对照，本次在此基础上修正）】\n" % len(recent)
        for r in recent:
            n = r.get("轮次", "?")
            vals = [str(v).strip() for v in (r.get("规范值") or []) if str(v).strip()]
            out += "  第%s轮 规范值: %s\n" % (n, "、".join(vals[:40]))
            merged = [m for m in (r.get("归并") or [])
                      if isinstance(m, Mapping) and m.get("原值") and m.get("归入")]
            if merged:
                mt = "、".join("%s→%s" % (m["原值"], m["归入"]) for m in merged[:40])
                out += "  第%s轮 归并: %s\n" % (n, mt)
    return out


def build_vocab_chat_prompt(attr: str, values: Sequence[tuple[str, int]],
                            messages: Sequence[Mapping[str, Any]] | None = None,
                            cur_spec: Sequence[str] | None = None,
                            history: Sequence[Mapping[str, Any]] | None = None,
                            other_attrs: Mapping[str, Sequence[str]] | None = None,
                            sample_rows: Sequence[Mapping[str, Any]] | None = None,
                            deliv_dist: Mapping[str, int] | None = None) -> str:
    """**对话式**词表收敛提示词（2026-10-22 新增）。

    与 `build_vocab_cluster_prompt` 的区别：
      · 那个是"一次性给建议 → 出结果"；
      · 这个是"**多轮问答**"——用户说一句、AI 回一段解释 + 更新后的词表，
        用户可以继续追问（"为什么把 X 并进去了？""把 Y 拆开"）。

    `messages`: [{"角色": "用户"|"AI", "内容": "..."}]（按时间顺序，已截断到最近 N 条）
    `cur_spec`: 当前已生成的规范值（让 AI 知道"现在长什么样"，便于增量修改）
    `history` : 历史轮次（口径建议 + 结果），作为背景约束
    """
    shown = "、".join("%s×%d" % (str(v)[:20], n) for v, n in values[:80])
    cur_block = ""
    spec = [str(v).strip() for v in (cur_spec or []) if str(v).strip()]
    if spec:
        cur_block = "\n【当前规范值（本次请在此基础上调整，不要从零重来）】\n%s\n" % "、".join(spec)
    # 对话历史（只取最近 10 条，避免 token 膨胀）
    convo = [m for m in (messages or []) if isinstance(m, Mapping) and str(m.get("内容") or "").strip()]
    convo_block = ""
    if convo:
        convo_block = "\n【对话历史（越后越新）】\n"
        for m in convo[-10:]:
            role = "用户" if str(m.get("角色") or "") == "用户" else "AI"
            convo_block += "%s：%s\n" % (role, str(m.get("内容") or "").strip()[:400])
    # 其他属性的词表（供 AI 判断"某个值该不该归到本属性"）
    other_block = ""
    if other_attrs:
        _lines = []
        for _nm, _vs in list(other_attrs.items())[:40]:
            _vs = [str(x).strip() for x in (_vs or []) if str(x).strip()]
            _lines.append("  - %s：%s" % (_nm, "、".join(x[:24] for x in _vs[:8]) if _vs else "(无词表)"))
        other_block = ("\n【其他属性的词表（供参考：判断某个值是不是该归到别的属性）】\n"
                       + "\n".join(_lines) + "\n")
    # 原始商品资料样本（标题/五点/参数）——让 AI 看到原文证据
    rows_block = ""
    if sample_rows:
        _parts = []
        for _i, _r in enumerate(list(sample_rows)[:3], 1):
            _parts.append("【样本 %d】标题：%s\n  五点：%s\n  参数：%s" % (
                _i, str(_r.get("标题") or ""),
                str(_r.get("五点描述") or ""),
                str(_r.get("详细参数") or "")))
        rows_block = ("\n【原始商品资料样本（供参考，判断取值语义）】\n"
                      + "\n\n".join(_parts) + "\n")
    # ★ 2026-10-27: 当前**交付值**分布（逐行落地的值，含「未明确」）
    #   唯一值统计里不含「未明确」，AI 会误以为没有可重判的行 → 必须单独给。
    deliv_block = ""
    if deliv_dist:
        _dl = ["  - %s ×%d" % (str(k)[:24], int(n))
               for k, n in sorted(deliv_dist.items(), key=lambda kv: -kv[1])[:30]]
        deliv_block = ("\n【当前交付值分布（逐行落地的实际取值，含「未明确」）】\n"
                       + "\n".join(_dl) + "\n")
    return (
        "你是类目属性词表收敛助手。属性「%s」在一批商品里**实际观察到**的取值如下"
        "(已去重, 附出现次数):\n%s%s%s%s%s%s\n"
        "【你的任务】\n"
        "1. 认真理解用户**最新一条消息**里的要求（可能是「合并某几个值」「拆开某个值」"
        "「给某个值改名」「保留原样」等），"
        "在【当前规范值】基础上做**最小必要调整**；\n"
        "   ★★ **最新一条优先**：历史轮次 / 历史口径 / 上一轮的命名维度**只作背景**，"
        "与最新一条要求冲突时**一律以最新一条为准**；"
        "禁止把之前说过的口径（例如更早说过的「按技术命名」「按功能分类」）自行套到这一条上；"
        "用户这次只改哪几个值，就只改哪几个值，**不要顺带改动别的一批值**；\n"
        "2. 用**一段自然语言**回复用户：说明你改了什么、为什么这么改、"
        "如果用户的要求有歧义或和观察值冲突，**直接指出来并给出建议**（不要闷头瞎改）；\n"
        "   ★★ 「回复」必须**自包含**：用户看到的只有这段文字。"
        "当用户说「列出来 / 现在有哪些 / 展示给我看」时，"
        "必须在「回复」里**把清单和对照写全**（例如逐条写「小型除湿机、家用除湿机、…」），"
        "禁止只写「下面列出」「见清单」然后指望结构化字段——用户看不到结构化字段；\n"
        "3. 同时给出更新后的完整「规范值」和「归并」映射（逐条列出每个观察值的归属）。\n"
        "4. ★ 只有当**用户最新这一条消息明确要求改变命名维度**时（例如把「按大小/用途分类」"
        "改成「按技术/原理分类」），才按新维度重新设计规范值；"
        "**这条不能由历史轮次触发** —— 历史里说过的「按技术命名」不等于这一轮也要这么做；"
        "本轮的默认行为是：用户让改哪几个值就只改哪几个值；\n"
        "【硬约束】\n"
        "· 粒度以能覆盖绝大多数取值为准，不要为低频写法单独造类别；\n"
        "· 拿不准的写法并入最接近的类别，实在没法归的不要硬造；\n"
        "· ★ **禁止拆分多值**：观察值里用「、/＋/+」连接的多值（如 `地下室、浴室、卧室`）"
        "必须**整体保留为一个值**，绝不能拆成单值；归并时多值只能整体归到另一个**同样完整**的值；\n"
        "· 规范值必须字面出现在「归并」的「归入」里。\n\n"
        "严格输出 JSON:\n"
        "{\"回复\": \"给用户看的一段话\", \"动作\": \"converge|reannotate\", "
        "\"读取方式\": \"text|vision|both\", \"筛选值\": \"\", "
        "\"规范值\": [\"...\"], \"归并\": [{\"原值\": \"...\", \"归入\": \"...\"}], "
        "\"多属性\": {\"另一个属性\": [\"...\"]}}\n"
        "【三个新增字段由你判断，不要让用户选】\n"
        "· 「动作」：只调整词表写法 → `converge`；"
        "需要**回到原文重新判定每一行**（现有标注明显不准、或要换命名维度/要更具体的值）→ `reannotate`；\n"
        "  ★★ 用户消息里出现「重新判定 / 重判 / 重新标注 / 逐行判定 / 按文本重新判 / "
        "去掉某个值 / 某个值不对」这类要求时，**必须选 `reannotate`**，"
        "并在 `筛选值` 里填上他点名的值；**不要只回一段分析就结束**——"
        "分析只是一半，真正改行值要靠 reannotate 执行；\n"
        "· 「读取方式」：动作=reannotate 时读什么 —— `text`（默认）/ `vision`（外形/结构必须看图）/ `both`；\n"
        "  ★★ 用户消息里出现「图片 / 看图 / 照片 / 外观 / 外形 / 结构」这些词时，"
        "**必须选 `vision`（或 `both`）**，绝不能选 `text`——用户已经明确要求看图了；\n"
        "· 「筛选值」：只想重判某些取值的行时填空串以外的这些值，**多个值用「、」分隔**"
        "（例：用户说「压缩机和半导体制冷的那些」→ `压缩机、半导体制冷`）；"
        "不限定时填空串。★ 用户点名了几个值，就**全部写进来**，不要只写第一个；\n"
        "· ★ 「多属性」（**跨属性 / 多属性一起处理**）：本属性与别的属性相关时（例：产品类型 ↔ 材质 ↔ 是否带水泵 ↔ 排水方式），"
        "如果用户的要求需要**同时改几个属性**，就把它们都放进 `多属性`（键=属性名，值=该属性的新规范值列表）；"
        "只改当前属性时不要输出该字段（或输出空对象）。\n"
        % (attr, shown + ("…" if len(values) > 80 else ""),
           other_block, rows_block, deliv_block, cur_block,
           convo_block + (_vocab_history_block(history) or ""))
    )


def build_vocab_cluster_prompt(attr: str, values: Sequence[tuple[str, int]],
                               hint: str = "",
                               history: Sequence[Mapping[str, Any]] | None = None) -> str:
    """把「一个属性」的唯一值(附出现次数)拼成词表聚类提示词。

    `hint`：**人工口径建议**（自然语言），例如
      「把具体树种（柚木/相思木/胡桃木…）合并成「木制」」。
    2026-10-08 用户口径：删掉数字粒度阈值 `granularity`（0~100 说不清想怎么合），
    改为人工写建议、LLM 照做。空字符串表示没给建议。
    `history`：多轮迭代的历史轮次（每条含 轮次/口径建议/规范值/归并），
    由 `_vocab_history_block` 拼成「L1 全部建议 + L2 最近 5 轮结果」。
    """
    shown = "、".join("%s×%d" % (str(v)[:20], n) for v, n in values[:80])
    hint_block = ""
    if str(hint or "").strip():
        hint_block = ("\n【**人工口径建议（优先级最高，必须照做）**】\n%s\n" % str(hint).strip())
    history_block = _vocab_history_block(history)
    return (
        "你是类目属性词表生成助手。下面是属性「%s」在一批商品资料里**实际观察到**的取值清单(已去重, 附出现次数)。\n"
        "请按语义把同义/同类的写法归并成**规范类别**, 每个类别给一个规范写法。\n"
        "**粒度**：以能覆盖绝大多数取值为准，不要为低频写法单独造类别。\n"
        "★ **禁止拆分多值**：观察值里用「、/＋/+」连接的多值（如 `地下室、浴室、卧室`）"
        "必须**整体保留为一个值**，绝不能拆成单值（不要输出 地下室、浴室、卧室 三个）；"
        "归并时多值只能整体归到另一个**同样完整**的值，不能拆开再归。\n"
        "%s"
        "%s"
        "拿不准的写法并入最接近的类别, 实在没法归的不要硬造类别。\n\n"
        "待聚类取值: %s%s\n\n"
        "严格输出 JSON: {\"属性\": \"%s\", \"规范值\": [\"...\"], "
        "\"归并\": [{\"原值\": \"...\", \"归入\": \"...\"}]}\n"
        "「归并」必须逐条列出上面每个观察到的原始取值, 标注它归入哪个规范值"
        "(原值本身就是规范值时归入=原值)。\n"
        % (attr, hint_block, history_block, shown, "…" if len(values) > 80 else "", attr)
    )


def parse_vocab_cluster(raw: Any, attr: str) -> list[str]:
    """从聚类模型的 JSON 里取「规范值」列表(单属性)。"""
    if not isinstance(raw, dict):
        return []
    vals = raw.get("规范值")
    if not isinstance(vals, list):
        return []
    return [str(v).strip() for v in vals if str(v).strip()]


def parse_vocab_cluster_map(raw: Any, attr: str) -> list[dict]:
    """从聚类模型的 JSON 里取「原值 → 规范值」归并映射(单属性), 返回 [{原值, 归入}]。"""
    if not isinstance(raw, dict):
        return []
    out: list[dict] = []
    for e in (raw.get("归并") or []):
        if not isinstance(e, dict):
            continue
        src = str(e.get("原值") or "").strip()
        dst = str(e.get("归入") or "").strip()
        if src and dst:
            out.append({"原值": src, "归入": dst})
    return out


def apply_vocab_acceptance(
    rules_json_path: str,
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """把「采纳」的新词批量并入 rules.json 的 `attributes[].values`（去重，不改别的字段）。

    decisions：初审模型的输出 items，每项含 `属性 / 词 / 结论(采纳|驳回) / 规范化写法`。

    返回：{"已新增":[...], "跳过重复":[...], "驳回":[...], "未匹配属性":[...]}
    """
    with open(rules_json_path, encoding="utf-8-sig") as fh:
        rules = json.load(fh)
    attrs = rules.get("attributes")
    if not isinstance(attrs, list):
        return {"error": "rules.json 无 attributes 列表"}

    added: list[dict] = []
    dup: list[dict] = []
    rejected: list[dict] = []
    missing: list[dict] = []
    for d in decisions:
        if not isinstance(d, Mapping):
            continue
        name = str(d.get("属性") or "").strip()
        word = str(d.get("词") or "").strip()
        concl = str(d.get("结论") or "").strip()
        if concl != "采纳":
            rejected.append({"属性": name, "词": word})
            continue
        norm = (str(d.get("规范化写法") or "").strip()) or word
        spec = next((a for a in attrs if isinstance(a, dict) and a.get("name") == name), None)
        if spec is None:
            missing.append({"属性": name, "词": word})
            continue
        # 受控值词表位置: 新格式(rule/subcols/params)在 params.values; 旧格式在属性顶层 values
        params = spec.get("params") if isinstance(spec.get("params"), dict) else None
        if params is not None and isinstance(params.get("values"), list):
            values = params["values"]
        elif isinstance(spec.get("values"), list):
            values = spec["values"]
        else:
            missing.append({"属性": name, "词": word, "原因": "该属性无 values 词表(非 enum/multi)"})
            continue
        if norm in values:
            dup.append({"属性": name, "词": norm})
            continue
        values.append(norm)
        added.append({"属性": name, "词": norm})

    with open(rules_json_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh, ensure_ascii=False, indent=2)
    return {"已新增": added, "跳过重复": dup, "驳回": rejected, "未匹配属性": missing}
