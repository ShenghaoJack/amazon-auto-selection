# 选品研究台 · 第一阶段

2026-10-16。使用 UI UX Pro Max Skill 开发的 React / TypeScript 前端，独立于现有 Python WebUI。用户已撤回 React Flow Skill，该 Skill 已卸载，本工程也没有 React Flow 依赖。

## 启动

需要 Node.js 22.12 或更高版本。首次使用运行：

~~~powershell
cd 'D:\deepseek工作区\auto-workflow-python\frontend'
npm install
npm run dev
~~~

访问 http://127.0.0.1:8797/ 。仅监听本机；与现有 8790 服务独立。按 Ctrl+C 停止。

~~~powershell
npm run build
npm run preview
npm test
~~~

生产构建位于 dist/，需要通过 HTTP 静态服务访问，不能直接打开 index.html。使用 npm ci 可依据 package-lock.json 重现依赖。

## 已实现

- 八阶段 SVG 流程画布、依赖箭头、状态区分、缩放、适配及列表切换。
- 七个独立演示任务：三个执行、一个人工等待、一个资源等待、一个许可排队、一个完成。
- 任务搜索、状态筛选、空态、逐任务切换、暂停与恢复。
- 并发许可上限 3、共享浏览器资源互斥、释放唤醒、恢复时重新竞争资源。
- 节点详情、输入输出、工具映射、产物证据说明。
- 词表编辑、保存新版本、按版本确认、未保存修改保护。
- 深浅主题、键盘操作、dialog 焦点管理、小屏默认列表及减少动态效果支持。
- 页面内可打开上一版原型；副本来自 docs/agent-workflow-v2/prototype.html，原文件保留。

## 推荐体验

1. 打开流程工作台，查看节点顺序，点击产品属性查看词表。
2. 修改规范值；确认按钮禁用，保存后版本从 r7 增至 r8。
3. 确认后进入资源等待：browser-alpha 正由 Dog Dryer 占用。
4. 在任务中心暂停 Dog Dryer，观察当前任务获得资源，Coffee Grinder 独立执行。
5. 恢复 Dog Dryer，观察其因共享资源被占用而等待。
6. 在任务中心重置演示，搜索、筛选任务，查看浅色主题。

## 代码结构

- src/domain/workflow.ts：示例数据、状态转移和演示调度逻辑。
- src/components/WorkflowCanvas.tsx：SVG 连线与可聚焦节点。
- src/components/Inspector.tsx：节点详情、词表审阅。
- src/App.tsx：数据模式边界；src/DemoApp.tsx：原有演示页面。
- src/api/：待 M1 接线的类型化客户端与数据模式定义。
- src/contracts/：由 workflow/contracts.py 生成的类型、JSON Schema 与客户端校验。
- src/styles.css：语义主题变量与响应式布局。
- design-system/product-research-workbench/MASTER.md：Skill 原始设计建议。
- design-system/product-research-workbench/pages/workbench.md：项目适配规范，覆盖通用建议。
- tests/workflow.test.mjs：并发隔离与版本确认规则验证。
- public/responsive-check.html：iframe 视口验收夹具，访问 /responsive-check.html。
- qa/：浏览器截图与阶段验收记录。

## 范围

第一阶段为可运行的前端交互实现，状态仅保存在 React 内存中，刷新复位。没有对接任务 API、LangGraph、Python 步骤、真实日志与业务产物，也没有开始采集或调用模型。

确认词表会演示完成属性阶段并请求执行下一阶段；真实系统应先执行步骤 18，再进入步骤 19。生产环境的锁、许可、幂等、租约、确认版本和权限必须由后端强制执行，不能信任前端状态。

当前流程图表达阶段顺序，多任务之间并发。可拖拽编排、自定义依赖和单任务并行分支不在第一阶段范围。

## M0 更新（2026-10-17）

前后端公共契约与旧执行范围修复已完成，详见 [M0 交付](../docs/M0开发交付与接口约定.md)。

- npm run dev / npm run build 显式使用 demo 模式，保留原交互演示。
- npm run build:live 输出 dist-live/，使用 live 模式。M0 版本仅显示未接入说明，不载入演示任务，也不回退模拟成功。
- 真实查询与页面接线在 M1 实现；M0 客户端仅通过契约测试联调，没有注册实际 /api/v2 路由。
- 后续使用 VITE_DATA_MODE 只能选择 demo 或 live；未知值拒绝，不静默降级。

## 阶段计划

第一阶段记录见 [前端阶段开发计划](../docs/前端阶段开发计划-UIUXProMax.md)。后续以前后端共同交付功能的 [一体化开发计划 V1.0](../docs/前后端一体化开发计划-V1.0.md) 为准，覆盖真实查询、受控执行、人工确认、同类目隔离、产品机会分析与生产切换。
