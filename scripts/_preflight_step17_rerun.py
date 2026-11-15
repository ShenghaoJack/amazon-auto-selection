# -*- coding: utf-8 -*-
"""只读预检：重跑步骤 17 之前，先证明「新提示词 + 新闸门」一定会过。

不调用 LLM、不写任何产物。三件事：
  A. 旧产物重放（应只剩真实数据错，schema 错 0）
  B. **模拟新 schema**：把旧产物的多字段字典压平成「值键 + 值键_来源」、
     并去掉 `形态`，喂给闸门 —— 应 ERR 0 / WARN 0 / PASS
  C. 负向测试：新 schema 但**故意漏一个属性的来源键** —— 必须 REJECT
     （证明闸门没有被我改聋）
"""
from __future__ import annotations

import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.attr_stability.attr_stability import RuleSource, gate_run  # noqa: E402

CAT = "Salt and Pepper Mill Sets-US-202608"
CID = "salt_pepper_mill"
RUNS = os.path.join("data", CAT, "17_属性标注", "runs")


def load_rows(path):
    with open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    if isinstance(obj, dict):
        for k in ("rows", "data", "result"):
            if isinstance(obj.get(k), (dict, list)):
                obj = obj[k]
                break
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        return [obj[k] for k in sorted(obj, key=lambda x: str(x))]
    raise TypeError(type(obj))


def to_flat(rows, drop_allow=True):
    """模拟项目根提示词的扁平 schema：值 = 字符串，来源 = 独立 `_来源` 键。"""
    out = []
    for r in rows:
        nr = {k: v for k, v in r.items()
              if k != "attributes" and not (drop_allow and k == "形态")}
        A = {}
        for name, v in (r.get("attributes") or {}).items():
            if isinstance(v, dict):
                A[name] = str(v.get("标准") or "").strip()
                src = str(v.get("来源") or "").strip()
                if src:
                    A[name + "_来源"] = src
            else:
                A[name] = v
        nr["attributes"] = A
        out.append(nr)
    return out


def report(tag, rows, rules):
    g = gate_run(rows, rules, None, tag=tag)
    print("  %-24s rows=%-5d %-44s WARN %-5d → %s"
          % (tag, g.rows, g.err_summary or "ERR 0", g.warnings, g.verdict))
    if g.issues:
        c = collections.Counter(
            (i.level, i.kind, i.field) for i in g.issues)
        for (lv, kd, f), n in c.most_common(5):
            print("        [%s/%s] %-14s x%d" % (lv, kd, f, n))
    return g


def main() -> None:
    rules = RuleSource.load(os.path.join("data", CAT), CID)
    print("=" * 86)
    print("必需 top_keys(%d) = %s" % (len(rules.top_keys), sorted(rules.top_keys)))
    print("允许 allow_keys(%d) = %s" % (len(rules.allow_keys), sorted(rules.allow_keys)))

    olds = {}
    for tag in ("a1", "a2", "a3", "a4"):
        p = os.path.join(RUNS, "%s.json" % tag)
        if os.path.exists(p):
            olds[tag] = load_rows(p)

    print("\nA. 旧产物重放（旧提示词：多字段字典 + 无 _来源 + 带 形态）")
    for tag, rows in olds.items():
        report(tag, rows, rules)

    print("\nB. 模拟新 schema（压平 + 去 形态）—— 这就是重跑后的预期产物")
    flat = {t: to_flat(r) for t, r in olds.items()}
    ok = True
    for tag, rows in flat.items():
        g = report(tag + "-flat", rows, rules)
        ok = ok and g.verdict == "PASS" and g.errors == 0 and g.warnings == 0
    print("  预检结论：%s" % ("PASS —— 重跑后的包装层与属性层都会过闸门"
                             if ok else "!!! 仍有问题，先别花钱重跑 !!!"))

    print("\nC. 负向测试（必须 REJECT，证明闸门没被改聋）")
    bad = json.loads(json.dumps(flat["a3"]))
    victim = sorted(rules.attrs)[0]
    n_drop = 0
    for r in bad:
        A = r["attributes"]
        if victim + "_来源" in A:
            del A[victim + "_来源"]
            n_drop += 1
    print("  故意删掉 `%s_来源` 共 %d 行" % (victim, n_drop))
    g = report("a3-漏来源键", bad, rules)
    print("  负向结论：%s" % ("REJECT —— 闸门正常工作" if g.verdict == "REJECT"
                             else "!!! 闸门没拦住，需要修 !!!"))

    print("\nD. 负向测试 2：缺一个必需行级键（应 ERR/data，必须拦）")
    bad2 = json.loads(json.dumps(flat["a1"]))
    for r in bad2:
        r.pop("pack_count_数值", None)
    report("缺 pack_count_数值", bad2, rules)


if __name__ == "__main__":
    main()
