# market_report_html —— vendored 报告查看器

本目录是**桌面 kit 的只读副本**(vendored), 供流水线在**不依赖桌面文件夹**的前提下生成整合报告 HTML。

| | |
|---|---|
| 来源 | `C:\Users\Jack_New\Desktop\Build_Amazon_Integrated_Market_Report_Skill_HTML_Scripts` |
| 取用时间 | 2026-10-09 |
| 取用文件 | `index.html` · `app.js` · `styles.css` · `charts.css` · `package_standalone.py` |
| 未取用 | `integration/*`(其 `build_offline_html.py` 的校验规则已由 `scripts/build_integrated_report_html.py::validate_report()` 复刻) · `convert_report.py`(按单元格位置猜结构, 我们不产四表 xlsx) · `examples/*` |

## 这份副本怎么被用

`scripts/build_integrated_report_html.py`:

1. 把三份输入(步骤8 关键词 / 步骤18 标准化交付 / 步骤19 VOC)装配成 kit 的**契约 JSON**(4 个 sheet);
2. `validate_report()` 按 kit 的规则自检(表 id 唯一 / `columns` 与 `headers` 等长 / `sourceRow≥1` / 无 NaN …);
3. 调本目录 `package_standalone.build(out_html, data_js)` —— 它读**本目录**的 `index.html`, 把
   `styles.css`/`charts.css` 内联成 `<style>`、把 `data/report-data.js` 与 `app.js` 内联成 `<script>`,
   并断言"不残留任何外部 script/link 引用" → 产出**单文件、离线、双击即看**的 HTML;
4. 生成后把 kit `index.html` 里写死的示例侧栏名(`Cat Fountains`)替换成真实类目名。

## 注意

* **改这里之前先想清楚**: 这是第三方快照, 升级请整批替换(并更新上表), 不要局部手改 —— 否则
  `package_standalone.replace_one()` 的"占位符必须恰好命中一次"断言会失败。
* 图表是 `app.js` 按**中文表头/表名**推断出来的(`chartSpec()`); 想让某张表出图, 表头/表名要匹配它的规则
  (如 `价格段`+`月销额` 出柱图, `生命周期`+占比 出组合条, `月份`+`类别合成搜索量` 出折线)。
* 与 Excel 版整合报告(`scripts/build_integrated_report.py`)是**同一批数据源**(共用 `tools/cat_report` 的
  载入与价格分段), 两套产物口径一致。

## 本地升级记录

- **2026-10-10(本仓库)**: `app.js::chartSpec()` 增加/泛化规则, 以落地 skill `report-spec.md` §4 的必需图表:
  1. **主配置配对图** —— 表头 `属性/功能 | 新品主配置占比 | 老品主配置占比 | 百分点差 | 新品主配置数 | 老品主配置数 | 新品已明确数 | 老品已明确数 | 新品证据完整度 | 老品证据完整度` → `lifecycle`(哑铃图)。用途: 类目 `是否*` 取值**未登记『是/否』词表**时, 按"该列取值最多的配置"对比新老品, 不做是/否臆断。
  2. **材质环图泛化** —— 原规则只认 `食碗材质/机身材质/过滤类型`, 现增加 `主体材质/材质/材料/机身材料/碗体材质/面料`。
  3. 配对图文案区分三种口径(明确宣称率 / 覆盖率 / 主配置占比), 避免把"主配置数/总变体数"误读成"已明确"。
  升级方式: 仅**新增/泛化**规则, 未改动既有规则语义; 重新同步 kit 时需把以上 3 点重新施加。
\n### 第二批本地升级(2026-10-10, 参照 `~/.codex/skills/build-amazon-integrated-html-report`)\n\n依据该 skill 的 `references/html-report-contract.md` §3(HTML 交付与交互)与 `references/qa-checklist.md`, 在**不改 kit 既有语义**的前提下补齐:\n1. `.table-scroll{overflow-x:auto}` —— 宽矩阵**局部**横滚, 页面不再整体横向溢出。\n2. `@media print` —— 打印保留叙述+证据表(折叠表格强制展开), 隐藏导航/工具栏/抽屉, `print-color-adjust:exact` 保风险色可见(数据本身带文字标签, 不依赖颜色)。\n3. 表格行 `tabindex=0 role=button aria-expanded` + Enter/Space 打开证据抽屉(键盘可操作)。\n4. 搜索计数区分 **行匹配 / 正文匹配**, 零表行但正文命中时给出区分提示 + 行内『清除筛选』。\n5. 生成端内嵌 JSON 把 `<` 转义为 `\\u003c` —— 防 `</script>`/`<script`/`<!--` 序列提前闭合脚本块。\n6. `.text-note` 供『颜色之外的状态说明』使用。\n升级方式: 仅**新增**选择器/属性与**新增**事件分支; 重新同步 kit 时需把以上 6 点重新施加。\n
### 第三批本地升级(2026-10-10, S1: 多 sheet 支持)\n\n1. `state.sheet` 默认 **0**(首屏) —— 报告从 4 张表扩成 5 张后, 默认页应为「决策摘要」。\n2. `renderNav()` 改为**按报告实际 sheets 生成**导航(名字/图标), 原实现写死 4 个中文名与 4 个图标, 扩表后会少一项并错位; 图标按位置循环取。\n3. `setSheet(1)` 全部改为 `setSheet(0)`(切换报告/导入后回到首屏)。\n升级方式: 仅改默认值与导航数据来源; 重新同步 kit 时需重新施加。\n\n### 第四批本地升级(2026-10-12, S4: 评论正文检索)\n\n1. 搜索计数新增 **评论正文命中 N 条**: 对标题含『证据库/评论』且表头含『正文』的表格, 按正文列单独统计命中(即使该表因筛选未显示、表格行为 0 也照常报数)。\n2. `state._lastTextOnly` 同时纳入正文命中 → 全空状态会提示『没有匹配的**表格行**, 但正文命中 N 处 —— 这不是没有结果』(contract §3 要求)。\n重新同步 kit 时需重新施加。\n