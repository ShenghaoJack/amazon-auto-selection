# -*- coding: utf-8 -*-
"""类目文件的统一加载器（4 份 → 2 份之后，所有读者都走这里）

背景
================================================================================
原先每个类目有 4 份文件，读者各自拼路径 `json.load(open('categories/<cat>.xxx.json'))`：

    <cat>.json          类目规则总纲（属性定义/词表/优先级/语义规则/易混对）
    <cat>.dict.json     归约词典（词表副本 + 原值→受控值 mapping + 正则规则）
    <cat>.detail.json   明细/尺寸/物理先验
    <cat>.values.json   值台账（受控值快照 + 逐轮变更历史）

收敛为 2 份
================================================================================
    <cat>.rules.json    规则 ＝ 主配置 + detail 段（**记录"应该怎样"**）
    <cat>.ledger.json   台账 ＝ 原 values（**记录"发生过什么"**）

`dict` 删除，原因（实测）：
  · `controlled_values` 与主配置 `attributes[].values` 是同一份数据的两个副本，
    实测三处不一致会直接报错，也让逐行执行时看到多份词表而分裂；
  · `mapping` 里大量 target **不在受控值词表内**（照抄即产出非法值），
    且自身矛盾（如某属性 78 条映射里 `家庭、厨房、派对、餐厅 → 厨房` 属信息丢失）；
  · 在「每步都读全部规则再判断」的架构下，查表加速的收益已不存在。

本模块的作用
================================================================================
让读者**只改一行**：把 `json.load(open(path))` 换成 `load_category(...).main/.detail/.ledger`。
逻辑不变，因此不引入新的行为差异。

**向后兼容**：若目录下仍是旧的 4 份文件，`load_category` 会自动按旧布局读取并
把 `dict` 的 `controlled_values` 合并进 `main`（等价于收敛前的视图），
便于「先改代码、后迁文件」或反之。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["CategoryFiles", "load_category", "load_view", "discover_categories",
           "migrate_4to2", "RULES_SUFFIX", "LEDGER_SUFFIX"]

RULES_SUFFIX = ".rules.json"
LEDGER_SUFFIX = ".ledger.json"
# 旧布局的四个后缀（含主配置裸 `<cat>.json`）
_LEGACY_SUFFIXES = (".rules.json", ".ledger.json", ".detail.json",
                    ".dict.json", ".values.json")
# 归约词典里除 controlled_values 之外、**有意丢弃**的段（迁移报告里会列出条数）
DROPPED_DICT_SEGMENTS = ("mapping", "pattern_rules", "_统计", "_mapping_说明", "_说明")


@dataclass
class CategoryFiles:
    cat: str
    cat_dir: str
    main: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    ledger: dict[str, Any] = field(default_factory=dict)
    source: str = "two-files"          # two-files | legacy-four-files
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ 便捷取值
    @property
    def attributes(self) -> list[dict[str, Any]]:
        return self.main.get("attributes") or []

    @property
    def controlled_values(self) -> dict[str, list[str]]:
        """属性 → 声明为合法的受控值（**唯一权威来源**：主配置 attributes[].values）"""
        return {a["name"]: [str(v) for v in (a.get("values") or [])]
                for a in self.attributes
                if isinstance(a, dict) and a.get("name") and a.get("values")}

    @property
    def attr_types(self) -> dict[str, str]:
        return {a["name"]: a.get("type", "enum") for a in self.attributes
                if isinstance(a, dict) and a.get("name")}

    @property
    def size_heuristics(self) -> dict[str, Any]:
        return self.detail.get("size_heuristics") or {}

    @property
    def detail_classes(self) -> set[str]:
        """明细类型名允许集：detail.grind_detail.categories 的类名与判据首词"""
        out: set[str] = set()
        gd = self.detail.get("grind_detail") or {}
        for c in (gd.get("categories") or []):
            if isinstance(c, (list, tuple)) and c:
                out.add(str(c[0]))
                if len(c) > 1 and isinstance(c[1], str):
                    out.add(c[1].split("，")[0].strip())
        return out

    @property
    def current_controlled_values(self) -> dict[str, list[str]]:
        return self.ledger.get("current_controlled_values") or {}

    @property
    def deprecated_values(self) -> dict[str, list[str]]:
        return self.ledger.get("deprecated_values") or {}

    def path(self, kind: str) -> str:
        suf = {"rules": RULES_SUFFIX, "ledger": LEDGER_SUFFIX}[kind]
        return os.path.join(self.cat_dir, self.cat + suf)


# ---------------------------------------------------------------- 发现类目
def discover_categories(cat_dir: str) -> list[str]:
    """列出目录下的类目 ID（显式剥掉各种后缀，避免把 `.rules.json` 当成独立类目）"""
    out: list[str] = []
    for f in sorted(os.listdir(cat_dir)):
        if not f.endswith(".json") or f.startswith("_"):
            continue
        for suf in _LEGACY_SUFFIXES:
            if f.endswith(suf):
                out.append(f[:-len(suf)])
                break
        else:
            out.append(f[:-5])
    return sorted(set(out))


def _read(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


# ---------------------------------------------------------------- 加载
def load_category(cat_dir: str, cat: Optional[str] = None) -> CategoryFiles:
    """加载一个类目的规则与台账。

    优先读收敛后的 `<cat>.rules.json` + `<cat>.ledger.json`；
    若不存在则回退旧 4 份布局（并把 `dict.controlled_values` 合并进 main）。
    """
    if cat is None:
        names = discover_categories(cat_dir)
        if len(names) != 1:
            raise ValueError("cat_dir 下发现 %d 个类目，请显式指定 cat：%s"
                             % (len(names), names))
        cat = names[0]

    rules = _read(os.path.join(cat_dir, cat + RULES_SUFFIX))
    if rules is not None:
        ledger = _read(os.path.join(cat_dir, cat + LEDGER_SUFFIX)) or {}
        detail = rules.get("detail") or {}
        main = {k: v for k, v in rules.items() if k != "detail"}
        cf = CategoryFiles(cat=cat, cat_dir=cat_dir, main=main,
                           detail=detail, ledger=ledger, source="two-files")
        if not cf.ledger:
            cf.warnings.append("缺少台账 %s%s（受控值快照不可查）"
                               % (cat, LEDGER_SUFFIX))
        return cf

    # ---------------- 旧布局
    main = _read(os.path.join(cat_dir, "%s.json" % cat))
    if main is None:
        raise FileNotFoundError("找不到类目文件：%s{%s%s, %s.json}"
                                % (cat_dir, cat, RULES_SUFFIX, cat))
    detail = _read(os.path.join(cat_dir, "%s.detail.json" % cat)) or {}
    ledger = _read(os.path.join(cat_dir, "%s.values.json" % cat)) or {}
    dic = _read(os.path.join(cat_dir, "%s.dict.json" % cat)) or {}
    cf = CategoryFiles(cat=cat, cat_dir=cat_dir, main=dict(main),
                       detail=detail, ledger=ledger, source="legacy-four-files")
    if dic.get("controlled_values"):
        # 旧布局下把词典词表并进 main（**仅补齐缺失**，不覆盖主配置 ——
        # 主配置是权威，词典只是副本；实测两者不一致时会报错）
        byname = {a.get("name"): a for a in cf.main.get("attributes", [])
                  if isinstance(a, dict)}
        added = 0
        for a, vals in dic["controlled_values"].items():
            spec = byname.get(a)
            if spec is not None and not spec.get("values"):
                spec["values"] = [str(v) for v in vals]
                added += len(vals)
        if added:
            cf.warnings.append("旧布局：从 dict.controlled_values 补齐了 %d 个受控值"
                               "（主配置未声明）" % added)
    cf.warnings.append("仍是旧 4 份布局，建议迁移：migrate_4to2(%r, %r)"
                       % (cat_dir, cat))
    return cf


def load_view(cat_dir: str, cat: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """返回**旧 4 份布局的等价视图**，供存量读者最小改动迁移。

    用法（读者只改一行）：
        # 旧：cfg = json.load(open('categories/x.detail.json'))['size_heuristics']
        新：cfg = load_view('categories', 'x')['detail']['size_heuristics']

    返回 `{'main':…, 'detail':…, 'ledger':…, 'dict':…}`
    其中 `dict` 是收敛前的词典视图（`controlled_values` 由 main 反推，
    `mapping`/`pattern_rules` 为空 dict/list —— 它们已被有意废弃，见模块文档）。
    """
    cf = load_category(cat_dir, cat)
    return {
        "main": cf.main,
        "detail": cf.detail,
        "ledger": cf.ledger,
        "dict": {
            "category_id": cf.cat,
            "controlled_values": cf.controlled_values,
            "mapping": {},                    # 已废弃
            "pattern_rules": [],              # 已废弃
            "_说明": "归约词典已废弃；controlled_values 由主配置反推，mapping/pattern_rules 不再提供",
        },
    }


# ---------------------------------------------------------------- 迁移 4→2
def _leaves(o: Any, path: str = "") -> dict[str, Any]:
    """枚举叶子标量 → {路径: 值}（用于无损校验）。

    路径形如 `attributes[0].name`：**键用 `.`、数组下标用 `[i]`**。
    解析必须用 `_get_path`，不能用 `p.split('.')` —— 那样 `unknown_values[0]`
    会被当成一个键名，导致校验器把 590 个正常叶子误报成"未覆盖"（踩过）。
    """
    out: dict[str, Any] = {}
    if isinstance(o, dict):
        for k, v in o.items():
            out.update(_leaves(v, "%s.%s" % (path, k) if path else str(k)))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            out.update(_leaves(v, "%s[%d]" % (path, i)))
    else:
        out[path] = o
    return out


_PATH_RE = None


def _get_path(obj: Any, path: str) -> tuple[bool, Any]:
    """按 `a.b[0].c` 取值，返回 (找到?, 值)。"""
    global _PATH_RE
    if _PATH_RE is None:
        import re as _re
        _PATH_RE = _re.compile(r"([^.\[\]]+)|\[(\d+)\]")
    cur = obj
    for m in _PATH_RE.finditer(path):
        key, idx = m.group(1), m.group(2)
        try:
            if key is not None:
                if not isinstance(cur, dict) or key not in cur:
                    return False, None
                cur = cur[key]
            else:
                i = int(idx)
                if not isinstance(cur, list) or i >= len(cur):
                    return False, None
                cur = cur[i]
        except (TypeError, KeyError, IndexError):
            return False, None
    return True, cur


def migrate_4to2(cat_dir: str, cat: str, *, write: bool = True,
                 delete_legacy: bool = True) -> dict[str, Any]:
    """把旧 4 份收敛为 2 份，并做**无损校验**。

    返回报告 dict（含 lossless 布尔与未覆盖的叶子路径列表）。
    write=False 时只做演算与校验，不落盘。
    """
    main = _read(os.path.join(cat_dir, "%s.json" % cat))
    if main is None:
        raise FileNotFoundError("找不到主配置 %s/%s.json" % (cat_dir, cat))
    detail = _read(os.path.join(cat_dir, "%s.detail.json" % cat)) or {}
    ledger = _read(os.path.join(cat_dir, "%s.values.json" % cat)) or {}
    dic = _read(os.path.join(cat_dir, "%s.dict.json" % cat)) or {}

    # ---- 构造 rules.json：主配置原样 + detail 整段挂在 "detail" 下
    #      注意用 _收敛说明 而不是覆盖 `_说明` —— 覆盖会让原说明丢失
    rules = dict(main)
    rules["_收敛说明"] = ("类目规则（由 %s.json 与 %s.detail.json 合并；"
                          "原归约词典 .dict.json 已废弃）" % (cat, cat))
    rules["detail"] = detail

    report: dict[str, Any] = {
        "cat": cat, "cat_dir": cat_dir,
        "main_keys": len(main), "detail_keys": len(detail),
        "ledger_revisions": len(ledger.get("revisions") or []),
        "dropped_from_dict": {},
        "lossless": False, "missing": [],
    }
    for seg in DROPPED_DICT_SEGMENTS:
        v = dic.get(seg)
        if v is None:
            continue
        report["dropped_from_dict"][seg] = (
            len(v) if isinstance(v, (dict, list)) else v)

    # ---- 无损校验：main 的每个叶子都要在 rules 顶层（detail 之外）找到且相等
    miss: list[str] = []
    for p, v in _leaves(main).items():
        ok, cur = _get_path(rules, p)
        if not ok or cur != v:
            miss.append("main:%s" % p)
    for p, v in _leaves(detail).items():
        ok, cur = _get_path(rules.get("detail"), p)
        if not ok or cur != v:
            miss.append("detail:%s" % p)
    # ---- 词典里 controlled_values 是否有主配置未声明的值（丢了会真丢信息）
    main_cv = {a["name"]: set(map(str, a.get("values") or []))
               for a in (main.get("attributes") or [])
               if isinstance(a, dict) and a.get("name")}
    orphan: dict[str, list[str]] = {}
    for a, vals in (dic.get("controlled_values") or {}).items():
        extra = [str(v) for v in vals if str(v) not in main_cv.get(a, set())]
        if extra:
            orphan[a] = extra
    report["dict_controlled_values_not_in_main"] = orphan
    report["missing"] = miss
    report["lossless"] = (not miss)

    if write and report["lossless"]:
        rp = os.path.join(cat_dir, cat + RULES_SUFFIX)
        lp = os.path.join(cat_dir, cat + LEDGER_SUFFIX)
        with open(rp, "w", encoding="utf-8") as fh:
            json.dump(rules, fh, ensure_ascii=False, indent=2)
        with open(lp, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, ensure_ascii=False, indent=2)
        report["written"] = [rp, lp]
        if delete_legacy:
            removed = []
            for suf in ("%s.json" % cat, "%s.detail.json" % cat,
                        "%s.dict.json" % cat, "%s.values.json" % cat):
                p = os.path.join(cat_dir, suf)
                if os.path.exists(p):
                    os.remove(p)
                    removed.append(p)
            report["removed"] = removed
    return report
