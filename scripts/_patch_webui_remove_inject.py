# -*- coding: utf-8 -*-
"""一次性补丁: 从 webui/index.html 移除「步骤10 注入词」面板。

要点(容易踩):
  * setElDisplay() 藏在要删的 JS 块里, 但**关键词清单面板在用** -> 必须摘出来保留;
  * watchInjectRefresh() 除了刷新注入词面板, **还负责刷新关键词清单面板** -> 不能整段删,
    改名为 watchKeywordRefresh() 并只保留关键词清单部分;
  * RUN_CARDS / reorderCards() / renumberCards() 三处登记必须同步(漏一处面板会错位)。
每一步都断言命中次数, 命中不等于预期就中止不写盘。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "webui" / "index.html"
BAK = TARGET.with_name(TARGET.name + ".bak-before-inject-sync")

log: list[str] = []


def sub1(text: str, old: str, new: str, tag: str) -> str:
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"[{tag}] 期望命中 1 次, 实际 {n} 次 -> 中止(未写盘)")
    log.append(f"OK  {tag}")
    return text.replace(old, new, 1)


def cut_lines(lines: list[str], start_pred, end_pred, tag: str, keep: list[str] | None = None) -> list[str]:
    """删除 [start, end] 行区间(start/end 为谓词命中的首行/其后首个命中行)。"""
    i = next((k for k, s in enumerate(lines) if start_pred(s)), -1)
    if i < 0:
        raise SystemExit(f"[{tag}] 找不到起始行 -> 中止(未写盘)")
    j = next((k for k in range(i, len(lines)) if end_pred(lines[k])), -1)
    if j < 0:
        raise SystemExit(f"[{tag}] 找不到结束行 -> 中止(未写盘)")
    log.append(f"OK  {tag}  删除 {i + 1}..{j + 1} 行({j - i + 1} 行)")
    return lines[:i] + (keep or []) + lines[j + 1:]


def main() -> int:
    if not BAK.is_file():
        print(f"缺少备份 {BAK}, 中止")
        return 1
    text = TARGET.read_text(encoding="utf-8")

    # ---- 1) 删除 secInject <section>(含它前面那条错位的"配置中心"注释) ----
    lines = text.splitlines(keepends=True)
    i_sec = next(k for k, s in enumerate(lines) if 'id="secInject"' in s)
    i_hdr = next(k for k in range(i_sec, -1, -1) if lines[k].startswith("  <!-- ============"))
    j_sec = next(k for k in range(i_sec, len(lines)) if lines[k].strip() == "</section>")
    log.append(f"OK  删除 secInject section  行 {i_hdr + 1}..{j_sec + 1}({j_sec - i_hdr + 1} 行)")
    lines = lines[:i_hdr] + lines[j_sec + 1:]
    text = "".join(lines)

    # ---- 2) wire() 里的注入词绑定 ----
    lines = text.splitlines(keepends=True)
    text = "".join(cut_lines(
        lines,
        lambda s: '$("btnReloadInject").onclick' in s,
        lambda s: 'input[name="injMode"]' in s and "forEach" in s,
        "删除 wire() 注入词绑定",
    ))

    # ---- 3) RUN_CARDS ----
    text = sub1(text, '"secState", "secLog", "secMon", "secInject", "secKwLists",',
                '"secState", "secLog", "secMon", "secKwLists",', "RUN_CARDS 去掉 secInject")

    # ---- 4) loadConfig 流程里的 loadInjectPanel() ----
    text = sub1(text, "  loadInjectPanel();\n  loadKeywordPanel();",
                "  loadKeywordPanel();", "删除 loadConfig 里的 loadInjectPanel()")

    # ---- 5) 删除注入词 JS 块, 但保留 setElDisplay ----
    lines = text.splitlines(keepends=True)
    i_js = next(k for k, s in enumerate(lines)
                if s.startswith("/* ============ 步骤10 注入词面板"))
    j_js = next(k for k in range(i_js, len(lines))
                if lines[k].startswith("/* ============ 关键词清单面板"))
    block = lines[i_js:j_js]
    k_fn = next((k for k, s in enumerate(block) if s.startswith("function setElDisplay(")), -1)
    if k_fn < 0:
        raise SystemExit("[保留 setElDisplay] JS 块里找不到 setElDisplay -> 中止(未写盘)")
    k_end = next(k for k in range(k_fn, len(block)) if block[k].rstrip("\r\n") == "}")
    keep_fn = block[k_fn:k_end + 1]
    log.append(f"OK  删除注入词 JS 块  行 {i_js + 1}..{j_js}  "
               f"({j_js - i_js} 行), 保留 setElDisplay({len(keep_fn)} 行)")
    new_block = (["/* ---- 通用: 按 id 显示/隐藏元素(关键词清单面板在用; 2026-10-06 摘出保留) ---- */\n"]
                 + keep_fn + ["\n"])
    lines = lines[:i_js] + new_block + lines[j_js:]
    text = "".join(lines)

    # ---- 6) reorderCards(): 去掉 inj 环节 ----
    old_reorder = '''  const st = $("secState"), inj = $("secInject"), kw = $("secKwLists");
  const attr = $("secAttr"), std = $("secStd");
  // 最终顺序: 状态 → **关键词清单** → 步骤10 注入词 → 属性集 → 标准化
  // (2026-09-29 用户要求: 关键词清单放在「步骤10 注入词」前面;
  //  必须按这个先后挪, 否则后面的 insert 会把前一步的结果挤走)
  if (st && kw && kw !== st.nextElementSibling) {
    try { st.insertAdjacentElement("afterend", kw); } catch (e) {}
  }
  const a1 = kw || st;                    // 关键词清单缺失时退化为"状态之后"
  if (a1 && inj && inj !== a1.nextElementSibling) {
    try { a1.insertAdjacentElement("afterend", inj); } catch (e) {}
  }
  const a2 = inj || a1;
  if (a2 && attr && attr !== a2.nextElementSibling) {
    try { a2.insertAdjacentElement("afterend", attr); } catch (e) {}
  }
'''
    new_reorder = '''  const st = $("secState"), kw = $("secKwLists");
  const attr = $("secAttr"), std = $("secStd");
  // 最终顺序: 状态 → **关键词清单** → 属性集 → 标准化
  // (2026-10-06: 原「步骤10 注入词」卡片已删除, 步骤10 判定参考词与关键词清单同步)
  if (st && kw && kw !== st.nextElementSibling) {
    try { st.insertAdjacentElement("afterend", kw); } catch (e) {}
  }
  const a1 = kw || st;                    // 关键词清单缺失时退化为"状态之后"
  if (a1 && attr && attr !== a1.nextElementSibling) {
    try { a1.insertAdjacentElement("afterend", attr); } catch (e) {}
  }
'''
    text = sub1(text, old_reorder, new_reorder, "reorderCards 去掉 inj")

    # ---- 7) renumberCards(): 去掉 secInject 并重编号 ----
    text = sub1(text, '''    secKwLists: "03 · Keyword Lists", secInject: "04 · Step10 Inject Words",
    secAttr: "05 · Step16 Attribute Set",
    secStd: "06 · Step18 Standardize", secLog: "07 · Live Log",
    secMon: "08 · Monitor & Alerts", secCfg: "09 · Config Studio"''',
                '''    secKwLists: "03 · Keyword Lists",
    secAttr: "04 · Step16 Attribute Set",
    secStd: "05 · Step18 Standardize", secLog: "06 · Live Log",
    secMon: "07 · Monitor & Alerts", secCfg: "08 · Config Studio"''',
                "renumberCards 去掉 secInject 并重编号")

    # ---- 8) 观察器: 改名 + 只保留关键词清单刷新 ----
    lines = text.splitlines(keepends=True)
    i_w = next(k for k, s in enumerate(lines) if s.startswith("/* ============ 步骤04/07 完成"))
    j_w = next(k for k in range(i_w, len(lines))
               if lines[k].startswith("/* ============ 轮询 ============ */"))
    new_watch = '''/* ============ 步骤04/07 完成 → 自动刷新「关键词清单」面板 ============ */
// 步骤04 产出大词候选、步骤07 产出 FR-07 达标判定 —— 两者都是「关键词清单」面板的数据源,
// 变动后自动重载; 面板有未保存修改时不覆盖, 提示手动处理, 防丢用户改动。
// 2026-10-06: 原「步骤10 注入词」面板已删除 —— 步骤10 的判定参考词改为与关键词清单
// **同步**(核心大词 + 长尾词), 所以这个观察器现在只需刷新关键词清单面板。
let __kwRefreshing = false;             // 关键词清单面板刷新中(防并发重入)
const __doneWatch = {};                 // cat|num -> 上次观察状态(仅步骤 4/7)

function watchKeywordRefresh(d) {
  const steps = (d && d.steps) || [];
  if (!steps.length) return;
  const base = d.cat || "?";
  const done = [];
  for (const s of steps) {
    if (s.number !== 4 && s.number !== 7) continue;   // 仅关注步骤04(关键词候选)/07(达标判定)
    const key = base + "|" + s.number;
    const prev = __doneWatch[key];
    __doneWatch[key] = s.status;
    if (prev !== undefined && prev !== "DONE" && s.status === "DONE") done.push(s.number);
  }
  if (!done.length) return;
  setTimeout(async () => {
    if (__kwRefreshing) return;
    if (__kwDirty) {
      toast(`步骤 ${done.map(n => pad(n)).join("、")} 已完成: 关键词清单面板有未保存修改, 已跳过自动刷新(保存后点「重新加载」即可)`, "err");
      return;
    }
    __kwRefreshing = true;
    try {
      await loadKeywordPanel();
      toast(`步骤 ${done.map(n => pad(n)).join("、")} 已完成: 关键词清单面板已自动刷新`, "ok");
    } catch (e) { /* loadKeywordPanel 内部已提示 */ }
    finally { __kwRefreshing = false; }
  }, 800);
}

'''
    log.append(f"OK  重写观察器  行 {i_w + 1}..{j_w}(原 {j_w - i_w} 行 -> {new_watch.count(chr(10))} 行)")
    lines = lines[:i_w] + [new_watch] + lines[j_w:]
    text = "".join(lines)

    # ---- 9) pollNow 调用点 ----
    text = sub1(text, "    watchInjectRefresh(d);", "    watchKeywordRefresh(d);",
                "pollNow 调用点改名")

    # ---- 10) 注释里的 ⑥注入词 ----
    text = sub1(text, "(②运行状态/③实时日志/⑤产物/⑥注入词 全部跟随 selFile",
                "(②运行状态/③实时日志/⑤产物/⑥关键词清单 全部跟随 selFile",
                "轮询注释更新")

    TARGET.write_text(text, encoding="utf-8")
    print("\n".join(log))
    print(f"\n写入完成: {TARGET}")
    print(f"残留 inject 相关标识: ", end="")
    import re
    left = sorted(set(re.findall(r"inj[A-Za-z]*|Inject[A-Za-z]*|inject-words", text)))
    print(left if left else "无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
