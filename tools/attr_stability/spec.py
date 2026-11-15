# -*- coding: utf-8 -*-
"""步骤16 属性集产物的纯函数：N 次抽取合并 + 同义归并提示词 + spec 读写。

spec = `<cat>-attribute-spec.json`，结构：
    {"cat": str, "status": "pending_confirm" | "confirmed",
     "attributes": [{"name": 中文属性名, "hint": 详细说明, "source": "llm"|"manual",
                     "modified": bool, "runs": int}]}
本模块零第三方依赖、不调 LLM(归并提示词由调用方拿去调)，便于单测。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.catname import display_cat

SPEC_STATUS_PENDING = "pending_confirm"
SPEC_STATUS_CONFIRMED = "confirmed"

# rules.json 最小骨架的说明文案(与参考类目 dog_slow_feeder.rules.json 一致)。
_RULES_SKELETON_NOTE = (
    "最小骨架(自动生成): 词表留空, 由步骤18 冷启动聚类自动生成并写回 "
    "params.values。属性类型后续按需精调(material/numeric/multi/bool)。"
)


def _norm(text: Any) -> str:
    return str(text or "").strip()


# 功能性开关类属性：名字以这些前缀开头 → 能用「是/否」独立判断，是选品差异点。
# 全类目通用（只看命名形态，不依赖任何类目词表）。
FUNCTIONAL_ATTR_PREFIXES: tuple[str, ...] = ("是否", "带", "含", "支持", "可", "具备", "无")


def is_functional_attr(name: Any) -> bool:
    """功能性开关类属性（是否/带/含/支持/可/具备/无 开头）。

    这类属性即使抽取频次低，也不应被并进上位词、或按频次直接删掉——它们能区分
    产品档次/功能，是选品决策要看的差异点。判断依据是**命名形态**，不是类目词表。
    """
    return _norm(name).startswith(FUNCTIONAL_ATTR_PREFIXES)


def aggregate_hints(attrs: Sequence[Mapping[str, Any]]) -> list[dict]:
    """按 name 合并：同名取**最长**的 hint，累计 runs。返回 [{name,hint,source,modified,runs}]。"""
    merged: dict[str, dict] = {}
    for it in attrs:
        name = _norm(it.get("name"))
        if not name:
            continue
        hint = _norm(it.get("hint"))
        e = merged.setdefault(name, {"name": name, "hint": "", "runs": 0, "hints": []})
        e["runs"] += 1
        if hint:
            e["hints"].append(hint)
    out: list[dict] = []
    for name, e in merged.items():
        hints = e["hints"]
        out.append({
            "name": name,
            "hint": max(hints, key=len) if hints else "",
            "source": "llm",
            "modified": False,
            "runs": e["runs"],
        })
    return out


def merge_extractions(extractions: Sequence[Sequence[Mapping[str, Any]]]) -> list[dict]:
    """N 份抽取结果 → 并集去重(同名取最长 hint)。extractions: [[{name,hint},…], …]"""
    flat = [it for batch in extractions for it in batch]
    return aggregate_hints(flat)


def build_dedupe_merge_prompt(attrs: Sequence[Mapping[str, Any]]) -> str:
    """同义归并提示词：把"材质/主体材质"这类同义不同名归成一条规范名。

    注意措辞刻意不用「提取/标注」等词，避免被 dry-run 的 mock 分支误匹配；
    且本提示词只做归并，不新增/不删除属性。
    """
    lines = "\n".join(
        "- %s%s" % (it.get("name"), ("：%s" % it.get("hint")) if _norm(it.get("hint")) else "")
        for it in attrs
    )
    return (
        "你是商品属性清单审校员。只做严格同义归并：对象、部件、维度、时间和统计口径一致，合并不丢信息。\n"
        "名称不同但口径一致可合并；相同示例、单位或证据只提示复核，不能证明同义。\n"
        "同一证据可以证明多个独立事实。上下位、部分重叠和可派生关系不是同义，不能写入 aliases。\n"
        "逐条核对完整 hint 的正向触发、否定条件、对象和状态；任何一条存在其他成员没有的不同事实时不要合并。\n"
        "例如相近但不同的功能、能力与随附配件、部件自带结构与独立配件、能否安装与随附安装件都不同；"
        "仅相关或有时共存不能合并，歧义按独立保留。\n"
        "例如不同部件的同类数量或容量（如主体与附件、两个不同仓位）、相近但不同的交互功能都不能合并；"
        "实际值与最大值口径不明确时保留。\n"
        "布尔属性与对应的类型/方式属性（如是否支持X与X类型）可列为后续派生候选，"
        "但本步骤仅做同义映射，未经无损三态验证不能合并。保留独立功能与人工定义，不凑数量。\n"
        "每个输入属性恰好出现一次，规范名优先使用输入名称，aliases 仅填写被合并的输入名称；"
        "hint 保留所有来源的具体边界、限制与未明确状态，开头的「形态：…；」书写格式原样保留。\n"
        "待归并属性清单：\n%s\n\n"
        "严格输出 JSON：{\"attributes\":[{\"name\":\"...\",\"aliases\":[\"...\"],\"hint\":\"...\"}]}\n" % lines
    )


def build_hint_summary_prompt(attrs: Sequence[Mapping[str, Any]]) -> str:
    """属性说明精简提示词：把每条详细 hint 总结成一句话精简说明(hint_short)。

    由步骤16 生成属性集后调用一次, 精简结果供步骤17 注入标注提示词(省 token 且保留
    判定要点)。只做删减, 不新增信息、不改属性名。零第三方依赖、不调 LLM。
    """
    lines = "\n".join(
        "- %s：%s" % (it.get("name"), _norm(it.get("hint")))
        for it in attrs if _norm(it.get("name"))
    )
    return (
        "你是亚马逊类目属性说明精简助手。下面是某类目的属性清单, 每个属性带一条详细说明。\n"
        "请把每条说明精简成**一句话**, 只保留最关键的信息:\n"
        "  ① 判定依据: 从原文哪些关键词能看出来(保留 2-3 个最典型的英文/中文关键词)\n"
        "  ② 典型取值: 保留 2-4 个最有代表性的取值\n"
        "  ③ 易混区分: 只保留『与哪个属性容易混、怎么分』的要点(没有就不写)\n"
        "  ④ 取值形态与书写格式: 说明开头的「形态：…；」及分隔符、单位写法要**原样保留在开头**"
        "(步骤17 按它统一写法, 删掉会让同一个值出现多种写法)\n\n"
        "硬性要求:\n"
        "  - 每条尽量 ≤ 40 字；必须保留对象/部件、计量周期、否定和未明确的边界，无法完整保留时宁可更长；不要新增信息\n"
        "  - 属性名(name)必须原样保留、一个不漏、不多\n"
        "  - 说明里没有典型取值/易混区分的, 只写判定关键词即可\n\n"
        "属性清单:\n%s\n\n"
        "严格输出 JSON: {\"attributes\":[{\"name\":\"...\",\"summary\":\"...\"}]}\n" % lines
    )


def load_spec(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def save_spec(path: str, spec: Mapping[str, Any]) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=2)
    return path


def find_rules_file(cat_root: str) -> str | None:
    """兼容查询：无文件或有歧义时返回 None；生产调用使用严格解析入口。"""
    try:
        return resolve_rules_file(cat_root)
    except ValueError:
        return None


def resolve_rules_file(cat_root: str, *, writable: bool = False) -> str | None:
    """统一规则入口：本地 categories/ → 本地根目录 → 共享定义。

    同一优先级多份规则报错。写入继承规则前复制到当前类目，保护共享定义。
    """
    from tools.attr_stability.catalog import catalog_dir_of, catalog_meta, parse_cat

    root = Path(cat_root)
    for directory in (root / "categories", root):
        files = sorted(p for p in directory.glob("*.rules.json")
                       if p.is_file() and not p.name.startswith("_"))
        if len(files) > 1:
            raise ValueError("规则文件有歧义: %s (%s)" %
                             (directory, ", ".join(p.name for p in files)))
        if files:
            return str(files[0])
    category = parse_cat(root.name)["类目"]
    shared = catalog_dir_of(root.parent, category)
    meta = catalog_meta(root.parent, category)
    declared = str(meta.get("规则") or "").strip()
    if meta.get("属性集策略") == "confirmed_only" and not declared:
        return None
    path = None
    if declared:
        candidate = (shared / declared).resolve()
        if (candidate.parent != shared.resolve() or not candidate.is_file()
                or not candidate.name.endswith(".rules.json")):
            raise ValueError("共享规则声明不存在或不在共享目录内: %s" % declared)
        path = candidate
    else:
        files = sorted(p for p in shared.glob("*.rules.json")
                       if p.is_file() and not p.name.startswith("_"))
        exact = shared / (cat_rules_stem(root.name) + ".rules.json")
        if len(files) > 1 and exact not in files:
            raise ValueError("共享规则文件有歧义: %s" % shared)
        path = exact if exact in files else (files[0] if files else None)
    if path is None:
        return None
    if writable:
        import shutil
        import tempfile

        root.mkdir(parents=True, exist_ok=True)
        target = root / path.name
        with tempfile.NamedTemporaryFile(dir=root, suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
        try:
            shutil.copyfile(path, tmp)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        path = target
    return str(path)


def cat_rules_stem(cat: str) -> str:
    """类目名 → rules.json 文件名主干(小写蛇形, 与 display_cat 口径一致)。

    例: "Cutting Boards-US-202608" → "cutting_boards"。文件名只需在类目目录下唯一
    (步骤17/18 按 `*.rules.json` 唯一匹配), 精确单复数可建文件后人工微调。
    """
    name = display_cat(cat).strip().lower()
    name = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "_", name)
    return name.strip("_") or "category"


def create_rules_skeleton(rules_path: str, cat: str,
                          spec_attrs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """在 rules_path 新建最小 rules.json 骨架(属性对齐 spec, rule=enum, 词表留空)。

    用于类目首次跑属性标注、目录下尚无 *.rules.json 时自动建骨架, 避免步骤17 因缺
    规则文件报错。词表由步骤18 冷启动聚类生成并写回 params.values。写回失败抛异常
    (调用方兜底)。
    """
    attrs = [{"name": str(a.get("name") or "").strip(), "rule": "enum", "params": {}}
             for a in spec_attrs if str(a.get("name") or "").strip()]
    rules: dict[str, Any] = {
        "_说明": _RULES_SKELETON_NOTE,
        "category": display_cat(cat),
        "attributes": attrs,
    }
    os.makedirs(os.path.dirname(os.path.abspath(rules_path)), exist_ok=True)
    with open(rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh, ensure_ascii=False, indent=2)
    return rules


def sync_rules_from_spec(rules_path: str, spec_attrs: Sequence[Mapping[str, Any]],
                         ) -> dict[str, Any]:
    """把 rules.json 的属性列表**对齐到 spec**(spec 为准)。

    - spec 有、rules 无 → 追加默认骨架 {"name": X, "rule": "enum", "params": {}}(词表留空,
      由步骤18 冷启动聚类生成);
    - rules 有、spec 无 → 删除(spec 已删/改名, 保持一致);
    - 两者都有 → 保留 rules 里已有的 rule/params(词表、类型不丢)。

    ★★ **副作用（2026-10-10 加，会直接影响人工闸门，务必知道）**：
      只要属性集**有增或有删**（`added` 或 `removed` 非空），就清掉 `rules` 顶层的
      **`vocab_status`** —— 让「**停②**（步骤18 前的词表确认）」**失效、回到未确认**。
      为什么：属性集一变，词表就与属性集**不再对齐**（新增属性词表为空、删除属性的旧词表被移除），
      此时若 `vocab_status` 还是 `confirmed`，步骤18 会拿着"已确认"状态、用**从未被人审过的
      空词表**做标准化。让停② 与停①（`spec.status` 每次保存都回落 pending）**同规则**。
      **仅调序**（`added == removed == []`）**不清** —— 那种情况映射没变，不该无谓地让人重确认。

    返回 {added: [名], removed: [名], kept: n, path}。写回失败抛异常(调用方兜底)。
    """
    with open(rules_path, encoding="utf-8-sig") as fh:
        rules = json.load(fh)
    if not isinstance(rules, dict):
        raise ValueError("rules.json 顶层不是对象: %s" % rules_path)
    existing: dict[str, dict] = {
        str(a.get("name") or "").strip(): a
        for a in (rules.get("attributes") or []) if isinstance(a, dict)
    }
    spec_names = [str(a.get("name") or "").strip() for a in spec_attrs
                  if str(a.get("name") or "").strip()]
    spec_set = set(spec_names)
    # 追加缺失(保持 spec 顺序)
    added: list[str] = []
    for name in spec_names:
        if name not in existing:
            added.append(name)
    # 删除多余
    removed = [n for n in existing if n not in spec_set]
    # 按 spec 顺序重建 attributes(已有项保留原 rule/params, 新增项给默认骨架)
    attrs: list[dict] = []
    for name in spec_names:
        if name in existing:
            attrs.append(existing[name])
        else:
            attrs.append({"name": name, "rule": "enum", "params": {}})
    rules["attributes"] = attrs
    # ★★ 修复（审核：停②可绕过，2026-10-10）：属性集一旦有增/删，词表就与属性集
    #   **不再对齐**（新增属性词表为空、删除属性的旧词表被移除）→ `vocab_status` 必须
    #   **失效**（回到未确认），否则重跑步骤16 后步骤18 会拿着"已确认"状态、用
    #   **从未被人审过的空词表**做标准化（与停①"保存即失效"的口径不一致）。
    #   停① 是 `spec.status` 每次保存都回落 pending；这里让停② 与之同规则。
    if added or removed:
        rules.pop("vocab_status", None)
    with open(rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh, ensure_ascii=False, indent=2)
    return {"added": added, "removed": removed, "kept": len(spec_names) - len(added),
            "path": rules_path}
