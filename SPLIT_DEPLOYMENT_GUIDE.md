# 分端部署指南(本地爬取 + 服务器跑 LLM/数据处理/MCP)

> 版本: 2026-10-08 · 阶段 1(交接层)已实现, 阶段 2/3 见文末
> 配套工具: `scripts/sync_artifacts.py` · 机器本地配置示例: `sync.local.example.yaml`
> 测试: `tests/test_sync_artifacts.py`(24 条)

## 1. 先说清楚: 这个方案解决什么

| 你的动机 | 收益 |
|---|---|
| **本地电脑不必长时间开着** | **收益大** —— 爬取只占一小段, 数据处理 + LLM 编排占大头时长 |
| 降低风控 | 收益≈0 —— 关键早已是"爬取固定在本地真实 IP/Chrome Profile"; 服务器侧本来就不爬 |
| 多类目并行提吞吐 | 收益大, 但需要队列 + 严格的"单一写入者"约束(阶段 3) |

⚠️ **LLM 与 MCP 本来就已经是远程 HTTP 调用**(`LLM_API_KEY` 指向远端; `SS_MCP_URL=https://mcp.sellersprite.com/mcp`),
所以把"LLM/MCP"搬到服务器**不会减少本地任何请求量**。真正被搬走的是**数据处理步骤的算力与在线时长**。

## 2. 步骤归属(按代码实测的依赖, 不是猜测)

判定依据: 各步里 `ctx.browser(` / MCP / LLM 的真实调用点 + `core/orchestrator.py` 的登录依赖表。

| 放**本地**(7 步) | 为什么 |
|---|---|
| **0** 榜单收集 | 卖家精灵浏览器(登录态) **+** Amazon 浏览器 |
| **1** 榜单详情 | Amazon COOKIE 直连 + 浏览器兜底 |
| **9** 搜索收集 | Amazon 浏览器 |
| **10** 父项变体 | Amazon 直连 + 无头/有头浏览器补抓(**另有 LLM 过滤段, 可选拆分**) |
| **11 / 12 / 13** 流量来源·查竞品·下载 | 卖家精灵浏览器(导出/下载) |

| 放**服务器**(13 步) | 为什么 |
|---|---|
| **2 / 3 / 4** 提示词·榜单过滤·关键词 | 纯 LLM + 本地数据 |
| **5 / 6** 挖词·趋势 | **MCP HTTP**(无浏览器) |
| **7** 筛选判定 | LLM |
| **8** 关键词报告 | 纯 Excel 生成 |
| **14** 流量拆分 | **MCP** + LLM |
| **15** 类目报告 | 纯数据处理 |
| **16 / 17** 属性集·属性标注 | LLM |
| **18** 标准化 | LLM + 数据处理 |
| **19** 备份与体检 | 看备份目标而定(备份到谁那里就放谁那里) |

**凭据分布(加分项)**: 爬取机只要 `AMZ_PROFILE` / `COOKIE` / 卖家精灵账号; 服务器只要 LLM/MCP key
—— 最敏感的 Amazon 登录态**不出本地**。

## 3. 每轮有 3 次跨端交接(这才是工作量所在)

```
本地 0→1  ┃交接①(1→2)┃  服务器 2→3→4→5→6→7→8  ┃交接②(8→9)┃  本地 9→10→11→12→13  ┃交接③(13→14)┃  服务器 14→…→19
```

每次交接都必须保证: **产物完整**(半份文件不能被当成"这步跑好了") + **状态一致** + **可幂等重跑**。
阶段 1 的 `sync_artifacts.py` 就是干这个的。

## 4. 标准流程

```powershell
# ① 本地: 跑完爬取步骤后, 写"产物指纹"
python scripts/sync_artifacts.py write --config "data/<CAT>/config.yaml" --steps 0 1 9 10 11 12 13 --role local

# ② 同步到另一端(二选一)
python scripts/sync_artifacts.py push  --config "data/<CAT>/config.yaml" --target \\NAS\awp   # 或
#   你自己的网盘/rsync 同步整个 data/<CAT>/(传输方式无关, 本工具不绑定)

# ③ 服务器: 开跑前先校验
python scripts/sync_artifacts.py pull   --config "data/<CAT>/config.yaml" --target \\NAS\awp
python scripts/sync_artifacts.py verify --config "data/<CAT>/config.yaml"       # 退出码 2 = 有问题, 别开跑

# ④ 服务器: 跑 2–8、14–19(用现有 CLI; 单步或续跑都行)
python main.py step 5 --config "data/<CAT>/config.yaml"    # 示例: 单独跑某步

# ⑤ 服务器: 写指纹 → 回传 → 本地再 verify
python scripts/sync_artifacts.py write  --config "data/<CAT>/config.yaml" --role server
python scripts/sync_artifacts.py push   --config "data/<CAT>/config.yaml" --target \\NAS\awp
python scripts/sync_artifacts.py verify --config "data/<CAT>/config.yaml"
```

## 5. 命令参考

| 命令 | 作用 | 退出码 |
|---|---|---|
| `write` | 写/刷新 `data/<CAT>/17_备份与监控/step-manifests/stepNN.json` + `index.json` | 0 |
| `verify` | 把磁盘产物与 manifest 比对(缺文件/内容被改 → 报错) | **0 一致 / 2 有问题** |
| `status` | 一屏看 0..19: 谁在哪台机器、什么时候跑的、现在还一致吗 | 0 |
| `push` / `pull` | 与目标目录互传(选定步骤产物 + 全部 manifest + `step-status.txt`) | 0 / 1 未配置目标 |

常用参数: `--steps 0 1 9 10` 只处理这些步 · `--role local|server` · `--json` 机器可读 ·
`verify --no-hash` 只比体积(快) · `verify --strict` 把 extra 也算问题 · `push --dry-run` 只预演。

**机器本地配置**(`role` 是机器属性, 不能写进 `config.yaml`, 否则会跟着同步跑到另一端):

```yaml
# 仓库根 sync.local.yaml(每台机器一份; 示例见 sync.local.example.yaml)
role: local            # local | server
target: "\\\\NAS\\awp"  # push/pull 目标(可空)
```
优先级: 命令行 `--role/--target` > 环境变量 `AWP_SYNC_ROLE` / `AWP_SYNC_TARGET` > `sync.local.yaml` > 默认(`local`)。

## 6. 判定规则(什么时候算"不一致")

| 情况 | 判定 |
|---|---|
| 文件缺失 | **✗ 失败**(退出码 2) —— 绝不能让下游拿缺件开跑 |
| 体积变了 | **✗ 失败**(附 `旧→新`) |
| 体积同、内容变了(**同体积篡改**) | **✗ 失败**(靠 sha256 抓) |
| 只有 mtime 变了 | ✓ 通过(`· mtime 变了但内容一致`) —— 网盘/SMB 复制必然改 mtime, 不能当冲突 |
| manifest 未记录的新文件 | ⚠ 警告(可能是另一端刚写入; 也可能是"两端同时在写"的征兆), `--strict` 才算失败 |
| `step-status.txt` / `pipeline-progress.log` 等**追加型状态文件** | ✓ 通过(`· 只同步不判定`) —— 它们本来就会随另一端跑步骤而变 |
| 备份/归档目录(`config-backups`、`archive`…)、临时文件 | 不参与指纹(排除) |

**大文件**: ≤64 MB 全量 sha256; 更大的用"头 4MB + 尾 4MB + 体积"的部分哈希(实测 20 步全量
write 仅 **1.4 秒**, 全库约 40 MB)。

## 7. 铁律: 同一类目, 同一时刻只有一端在写

`step-status.txt` / `.run.lock` / `.webui/runs/*.yaml` / `llm-usage*.jsonl` 都是**本地文件**,
**文件锁跨机无效**。所以:

1. 交接前确认另一端**没有在跑**;
2. 一端跑完 → `write` → 同步 → 另一端 `verify` 通过后再开跑;
3. 看到 `⚠ manifest 未记录的新文件` 就先停手查清是哪边写的。

> 账本(`llm-usage*.jsonl`)也在 `data/<CAT>/` 里, 会跟着同步 —— 看板的"类目累计费用"因此仍连续;
> 但**两端同时在写账本会串行错乱**, 同样受上面第 1 条约束。

## 8. 客户端环境要求

| | 本地端 | 服务器端 |
|---|---|---|
| 依赖 | 全套(含 Playwright/Chrome) | 只需 `pandas` / `openpyxl` / `PyYAML` / `requests`(**不需要 Playwright**) |
| 凭据 | `AMZ_PROFILE`、`COOKIE`、卖家精灵账号 | `LLM_API_KEY`、`VISION_API_KEY`、`SS_MCP_KEY` |
| 注意 | 盘符/中文路径、任务期间别关机 | 时区(步骤13 的最早时间判定、月份/时间戳命名)建议与本地一致 |

## 9. 已知限制 / 待办

* `push/pull` **不做原子替换、也不删目标端文件** —— 中断会留半份文件, 这正是 `verify` 要拦的;
  要更强的原子性请用 rsync(`--partial --delay-updates`)或对象存储。
* 步骤 10 的 **LLM 过滤段**目前跟爬取同进程(本地跑); 想进一步省本地时间, 可把"爬取/过滤"拆成两个
  步骤(代码里 `_fetch_all` 与 `judge_batch` 本就分离)。
* 阶段 2(服务器跑步骤集合): 需要给 CLI 加"步骤集合"参数(如 `--steps 2,3,5,6,7,8,14,15,16,17,18,19`);
  今天已做到的"**按步骤精确触发登录预检查**"让这件事天然安全 —— 非爬取步骤不触发 Amazon/卖家精灵检查,
  COOKIE 预检查也只在含 1/10 时跑, 且**失败固定放行**。
* 阶段 3(本地常驻 agent): 轮询服务器任务 → 跑爬取 → 上传 → 回报状态; 此时才需要把
  `.run.lock` / 状态文件的权威方挪到服务器(或双写带版本)。
* 自动写 manifest(每步结束自动 `write`): 见 `sync.local.yaml` 的 `manifest: auto` 预留键, 尚未接线。

## 10. 实测记录(2026-10-09, Dog Dryer-US-202608)

* 全量 `write` 20 步: **1.4 秒**, 20 个 manifest; 产物总量约 40 MB(步骤19 目录 57 个文件 24 MB)。
* 全量 `verify`(size+sha256): **20 步全部 ok**。
* ⚠ **顺带发现**: 步骤18 **声明了但从未产出** `18_标准化/新词初审裁决.json`、
  `18_标准化/新词自动采纳记录.json`(见 `steps/step18_standardization.py` L43-44; 该类目
  `18_标准化/` 目录下只有 2 个文件, 全库也搜不到这两个名字)。分端以前这种"声明存在、文件不存在"
  只会在另一端跑到那一步才炸 —— 现在 `write` 会**当场**把它标出来。
