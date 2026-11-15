
"use strict";
/* ============ 常量: 20 步元数据 ============ */
const STEPS = [
 {n:0, t:"榜单收集", d:"FR-00: 浏览器自动化抓取双榜 ASIN 并合并去重", a:["CAT-bestseller-newrelease.json","CAT-bestseller-newrelease-all.txt"]},
 {n:1, t:"榜单详情抓取", d:"FR-01: HTTP 直连抓取双榜 ASIN 详情(标题/五点)并追加写 JSONL", a:["CAT-dual-details.jsonl"]},
 {n:2, t:"生成过滤提示词", d:"FR-02: LLM 生成 5 类过滤词 + 产品定义(附真实标题样本; 不写达标关键词), 组装为类目过滤提示词", a:["filter-CAT-prompt.json"]},
 {n:3, t:"榜单详情过滤", d:"FR-03: 分批 LLM 判定剔除不符合类目行, 输出过滤后榜单详情明细", a:["CAT-dual-details-filtered.jsonl","CAT-filtered-dual.txt"]},
 {n:4, t:"生成关键词", d:"FR-04: 大词候选(LLM 温度0 ∪ 确定性挖掘 ∪ 根词, 按覆盖率排序取前 CANDIDATE_LIMIT 默认20) + 核心大词(=全部候选, 根词按类目名实词覆盖选) + 五维度长尾词(场景/人群/功能/属性/痛点 各 LONGTAIL_PER_DIM 条, 实体按标题+五点频率取)", a:["CAT-keywords-candidates.txt","CAT-keywords.txt","CAT-keywords-entities.txt"]},
 {n:5, t:"关键词挖词", d:"FR-05: 三 MCP 接口逐词挖取关键词指标数据", a:["CAT-amazon-keyword-data.json"]},
 {n:6, t:"站内月度趋势", d:"FR-06: 核心大词历史月度序列", a:["CAT-amazon-monthly.json"]},
 {n:7, t:"筛选脚本与筛选", d:"FR-07: LLM 生成筛选脚本并逐词判定", a:["screen-CAT.py","CAT-screen-results.json"]},
 {n:8, t:"关键词报告", d:"FR-08: 生成 7 sheet 关键词报告", a:["CAT-keyword-report.xlsx"]},
 {n:9, t:"搜索收集", d:"FR-09: 浏览器逐词搜索自然位 ASIN(达标核心词 CORE_SEARCH_PAGES 页 + 抓取词 LONGTAIL_SEARCH_PAGES 页, 默认 5/4); 抓取词=五维度各取频率最高实体 × 搜索量最高的达标词(最多 5 条); 只收主列表结果卡+标题快照+规则校验(search-verify.csv), 断点续爬, 并入双榜清单", a:["CAT-search-asins.json","CAT-search-asins-all.txt"]},
 {n:10,t:"变体抓取与第二遍过滤", d:"FR-10: HTTP 抓取变体详情 + 注入达标关键词(自动=FR-07 全部达标词/人工清单可配)后第二遍过滤 + 明细/清单/审计输出", a:["CAT-results.jsonl","CAT-父项变体明细.xlsx","CAT-parents.txt","CAT-variants.txt","CAT-过滤审计.csv"]},
 {n:11,t:"查流量来源", d:"FR-11: 浏览器自动化反查流量来源(卖家精灵)并自动合并", a:["11_查流量来源/合并-11_查流量来源.xlsx"]},
 {n:12,t:"查竞品提交", d:"FR-12: 变体分批提交查竞品导出任务并记录提交记录", a:["12_查竞品/CAT-submitted.json"]},
 {n:13,t:"竞品数据下载", d:"FR-13: 我的导出页下载查竞品任务, 解压按组归档并自动合并", a:["13_查竞品下载/合并-13_查竞品下载.xlsx"]},
 {n:14,t:"流量拆分", d:"FR-14: 变体明细/流量来源/竞品数据关联合并, 流量占比拆分与缺失字段补齐", a:["14_流量拆分/流量拆分数据-需人工补充.xlsx"]},
 {n:15,t:"初步类目分析报告", d:"FR-15: 基于流量拆分与关键词报告生成 4 sheet 初步类目分析报告", a:["CAT-初步类目分析报告.xlsx"]},
 {n:16,t:"属性集提取与确认", d:"FR-16: 属性集 N 次抽取合并 + 同义归并, 固化为 attribute-spec.json 待人工确认", a:["CAT-attribute-spec.json"]},
 {n:17,t:"属性标注", d:"FR-17: 对已确认属性集 N 次补充标注 + 结构闸门 + 一致性表决 + 图片视觉补充(仅未明确), 产出单遍原始/唯一值统计", a:["17_属性标注/CAT-属性标注-单遍原始.xlsx","17_属性标注/唯一值统计.json"]},
 {n:18,t:"标准化", d:"FR-18: 读步骤17 原始标注, 词表生成/聚类(每属性粒度阈值) + 归并标准化 + 新词初审, 产出标准化交付 xlsx", a:["18_标准化/CAT-属性标注-标准化交付.xlsx"]},
 {n:19,t:"备份与最终体检", d:"FR-19: 全产物体检(抓取质量/连续失败/卡死/完整性/耗时)+预警推送+归档提示", a:["17_备份与监控/health-report.json"]}
];
const M = {CAT:"类目名",LIST_URLS:"榜单URL(;分隔)",SITE:"卖家精灵站点名",MARKET:"亚马逊市场站点",
 TRAFFIC_MONTH:"流量来源目标月 YYYY-MM",COMPETITOR_MONTH:"竞品目标月 YYYY-MM",TREND_MONTH:"趋势/挖词月 YYYYMM",
 LLM_API_KEY:"LLM 密钥",LLM_BASE_URL:"LLM Base URL",LLM_MODEL:"LLM 模型",LLM_NO_THINK:"强制非思考模式",
 VISION_API_KEY:"视觉密钥(可选)",VISION_BASE_URL:"视觉 Base URL",VISION_MODEL:"视觉模型",
 ATTR_MODE:"属性提取模式",ATTR_KEYS:"补充属性键(;分隔)",ATTR_DESC:"属性键=描述(;分隔)",
 FILTER_REQUIREMENT:"过滤要求",SCREEN_REQUIREMENT:"筛选标准",GATE_MIN_QUALIFIED:"开品门槛达标词数",
 GATE_NEW_SELLERS:"新品BuyBox卖家数阈值(步骤15门禁④, 默认10)",
 LONGTAIL_PER_DIM:"长尾词每维条数(步骤4, 默认5; 5 维共 25 条)",
 MONTHLY_SALES_MIN:"月销量筛选阈值",BATCH_FILTER:"LLM 过滤批判定起点(默认10, 失败降档至1条/批; 1=全逐条, 步骤3/10)",LLM_TIMEOUT:"LLM 单请求超时秒(默认60)",
 FILTER_JUDGE_TRIES:"1条/批阶段单条重试次数(默认3)",FILTER_JUDGE_FALLBACK:"单条多次失败兜底(keep=保留待复核/drop=剔除)",FILTER_JUDGE_WORKERS:"LLM 过滤判定并发批数(首轮/集中重发档, 默认3; 1=串行)",
 CORE_SEARCH_PAGES:"核心大词搜索页数(步骤9, 默认5)",LONGTAIL_SEARCH_PAGES:"长尾词搜索页数(步骤9, 默认4)",
 SS_MCP_URL:"卖家精灵 MCP URL",SS_MCP_KEY:"MCP 密钥",SS_ACCOUNT:"卖家精灵账号",SS_PASSWORD:"卖家精灵密码",
 SS_PROFILE:"卖家精灵 Profile",SS_CDP_URL:"CDP 直连地址(留空=弹窗)",
 SS_QUOTA_WINDOW_START:"备用账号每日可用起始时刻",
 SS_QUOTA_WINDOW_END:"备用账号每日可用结束时刻(默认08:00)",
 SS_ACTIVE_ACCOUNT:"本轮起始账号(手动指定)",
 SS_ACCOUNTS:"卖家精灵账号列表(主/备用)",
 AMZ_PROFILE:"亚马逊 Profile",COOKIE:"亚马逊 Cookie(HTTP直连+自动导入)",COOKIE_CHECK:"启动时校验 COOKIE",
 REQUEST_MIN_INTERVAL:"直连最小间隔秒",REQUEST_MAX_INTERVAL:"直连最大间隔秒",
 FETCH_WORKERS:"变体抓取并发线程",FALLBACK:"浏览器兜底开关(auto/off)",
 FETCH_LONG_EVERY:"长间隔条数(0=关)",FETCH_LONG_MIN:"长间隔最小秒",FETCH_LONG_MAX:"长间隔最大秒",
 FETCH_ASIN_TRIES:"变体抓取每ASIN尝试次数(已内化)",
 QUERY_WAIT_TIMEOUT:"步骤11 查询结果等待秒数(未配置=沿用DOWNLOAD_TIMEOUT=120)",
 REVIEW_LIMIT:"复核抽样条数(0=全量)",REVIEW_AUTO:"步骤10后自动复核",REVIEW_MIN_RATE:"复核一致率门槛%",REVIEW_POLICY:"不一致项策略",
 SS_ASIN_PREFIX:"ASIN清单前缀",SS_FORCE_RESUBMIT:"强制重新提交竞品",EXPORT_STATUS_WAIT:"导出等待上限秒",EXPORT_STATUS_INTERVAL:"导出刷新间隔秒",
 WECOM_WEBHOOK:"企业微信机器人 Webhook",DATA_ROOT:"数据根目录",DRY_RUN:"离线模拟模式",
  ATTR_SPEC_RUNS:"属性集抽取次数(步骤16)",ATTR_STAB_RUNS:"标注遍数(步骤17)",ATTR_STAB_CHUNK:"每批行数(步骤17)",ATTR_STAB_WORKERS:"并发线程数(步骤17)",
  ATTR_STAB_MAX_ALONE:"独异格率阈值%(步骤17)",ATTR_STAB_MAX_RETRY:"被拒后重跑轮数(步骤17)",ATTR_STAB_MIN_RATE:"一致率硬门槛%(步骤17)",
  ATTR_STAB_PROMPT:"标注提示词路径(步骤17)",ATTR_STAB_GEN_VOCAB:"冷启动词表生成(步骤18)",
  ATTR_STAB_VOCAB_GRANULARITY:"词表合并粒度(步骤18)",ATTR_STAB_AUTO_ONBOARD:"新词全自动写回(步骤18)",
  RECENT_LOG_LINES:"成功率统计窗口(步骤19)",REPORT_FRESH_DAYS:"报告新鲜度天数(步骤19)",
  STEP_DURATION_MAX_SECONDS:"单步耗时阈值秒(步骤19)",STEP_DURATION_MEAN_MULT:"耗时超均值倍数(步骤19)",STEP_DURATION_MEAN_FLOOR:"耗时均值下限秒(步骤19)"};
const BOOL_KEYS = new Set(["LLM_NO_THINK","COOKIE_CHECK","REVIEW_AUTO","SS_FORCE_RESUBMIT","DRY_RUN","ATTR_STAB_GEN_VOCAB","ATTR_STAB_AUTO_ONBOARD"]);
const NUM_KEYS = new Set(["GATE_MIN_QUALIFIED","GATE_NEW_SELLERS","LONGTAIL_PER_DIM","MONTHLY_SALES_MIN","BATCH_FILTER","FILTER_JUDGE_TRIES","FILTER_JUDGE_WORKERS","LLM_TIMEOUT","REQUEST_MIN_INTERVAL","REQUEST_MAX_INTERVAL","FETCH_WORKERS","FETCH_ROUND_PAUSE","FETCH_STALL_ROUNDS","FETCH_ASIN_TRIES","QUERY_WAIT_TIMEOUT","FETCH_LONG_EVERY","FETCH_LONG_MIN","FETCH_LONG_MAX","REVIEW_LIMIT","REVIEW_MIN_RATE","EXPORT_STATUS_WAIT","EXPORT_STATUS_INTERVAL","CORE_SEARCH_PAGES","LONGTAIL_SEARCH_PAGES","ATTR_SPEC_RUNS","ATTR_STAB_RUNS","ATTR_STAB_CHUNK","ATTR_STAB_MAX_ALONE","ATTR_STAB_MAX_RETRY","ATTR_STAB_MIN_RATE","ATTR_STAB_VOCAB_GRANULARITY","RECENT_LOG_LINES","REPORT_FRESH_DAYS","STEP_DURATION_MAX_SECONDS","STEP_DURATION_MEAN_MULT","STEP_DURATION_MEAN_FLOOR","ATTR_STAB_WORKERS"]);
// 页面不展示的键(代码默认值已固定, 如 CANDIDATE_LIMIT=20; 即使出现在 yaml 也在表单中隐藏, 保存不改动)
// 注: LONGTAIL_COUNT 是**兼容旧键**(长尾总条数) —— 2026-09-13 起长尾条数以 LONGTAIL_PER_DIM(每维条数, 可见可改)为准,
//     仅当 yaml 里没有 LONGTAIL_PER_DIM 时才按 LONGTAIL_COUNT//5 折算每维条数, 故仍保留在隐藏列表里
const HIDDEN_KEYS = new Set(["CANDIDATE_LIMIT","CORE_KEYWORD_COUNT","LONGTAIL_COUNT","SEARCH_VOLUME_THRESHOLD","MOM_YOY_MIN","GOOGLE_YOY_MIN","CPC_MAX","REQUEST_RETRY","BATCH_PARENT","BATCH_VARIANT","DOWNLOAD_TIMEOUT","SCREENSHOT_EVERY","STUCK_MINUTES","HTTP_TIMEOUT","CONSECUTIVE_FAIL_WARN","RECENT_SUCCESS_RATIO",
 // 2026-09-16 起"已内化"的直连重试/补抓细节: 取值固定在代码里(直连首轮+1 轮重试/轮间 60s →
 // 每 ASIN 每轮 1 次 → HTTP 单请求 2 次尝试 → 浏览器补抓 2 轮/轮间 30s → **无头用尽再有头 1 轮**), 界面不再展示,
 // 统一由 `FALLBACK` 一个开关控制。仍兼容(写了以配置为准), 故从旧备份恢复配置时也不会冒进表单。
 "FETCH_ROUND_PAUSE","FETCH_STALL_ROUNDS","FETCH_ASIN_TRIES","BROWSER_FALLBACK_ROUNDS","BROWSER_ROUND_PAUSE","BROWSER_HEADED_ROUNDS","STEP01_BROWSER_FALLBACK"]);
// 有**专属编辑器**的键(不参与通用表单, 也不进"其他参数"兜底组; 2026-09-11)
//   SS_ACCOUNTS: 账号列表由 ◈ 专用编辑器维护(见 renderSsAccounts) —— 曾被通用渲染当成普通文本,
//   保存时会把数组串成字符串写回 YAML, 必须显式排除。
const SPECIAL_KEYS = new Set(["SS_ACCOUNTS"]);
const SECRET_KEYS = new Set(["LLM_API_KEY","VISION_API_KEY","SS_MCP_KEY","SS_PASSWORD","COOKIE"]);
const GROUPS = [
 {id:"类目与月份", keys:["CAT","LIST_URLS","SITE","MARKET","TRAFFIC_MONTH","COMPETITOR_MONTH","TREND_MONTH"]},
 {id:"LLM 文本接口", keys:["LLM_API_KEY","LLM_BASE_URL","LLM_MODEL","LLM_NO_THINK"]},
 {id:"LLM 视觉接口(可选)", keys:["VISION_API_KEY","VISION_BASE_URL","VISION_MODEL"]},
 {id:"业务参数(过滤/筛选/门槛)", keys:["ATTR_MODE","ATTR_KEYS","ATTR_DESC","FILTER_REQUIREMENT","SCREEN_REQUIREMENT","BATCH_FILTER","FILTER_JUDGE_TRIES","FILTER_JUDGE_FALLBACK","FILTER_JUDGE_WORKERS","LLM_TIMEOUT","GATE_MIN_QUALIFIED","GATE_NEW_SELLERS","LONGTAIL_PER_DIM","MONTHLY_SALES_MIN"]},
 {id:"卖家精灵 MCP", keys:["SS_MCP_URL","SS_MCP_KEY"]},
 {id:"卖家精灵账号/浏览器", keys:["SS_ACCOUNT","SS_PASSWORD","SS_PROFILE","SS_CDP_URL","SS_QUOTA_WINDOW_START","SS_QUOTA_WINDOW_END","SS_ACTIVE_ACCOUNT","SS_ASIN_PREFIX","SS_FORCE_RESUBMIT"]},
 {id:"亚马逊登录态", keys:["AMZ_PROFILE","COOKIE","COOKIE_CHECK"]},
 {id:"兜底(浏览器补抓)", keys:["FALLBACK"]},
 {id:"抓取与复核调优", keys:["REQUEST_MIN_INTERVAL","REQUEST_MAX_INTERVAL","FETCH_WORKERS","FETCH_LONG_EVERY","FETCH_LONG_MIN","FETCH_LONG_MAX","REVIEW_LIMIT","REVIEW_AUTO","REVIEW_MIN_RATE","REVIEW_POLICY","EXPORT_STATUS_WAIT","EXPORT_STATUS_INTERVAL","QUERY_WAIT_TIMEOUT","CORE_SEARCH_PAGES","LONGTAIL_SEARCH_PAGES"]},
 {id:"推送与其他", keys:["WECOM_WEBHOOK","DATA_ROOT","DRY_RUN"]}
];
const HINTS = {
 CAT:"必填。换类目改这里即可, 数据按类目自动隔离; 词间不用'-'; 版本/日期/地区只放末尾后缀(如 xxx-v2 / xxx-202609 / xxx-US), 提示词会自动去掉后缀",
 LIST_URLS:"必填。Best Sellers 榜单 URL 即可, 分号分隔; New Releases 可自动推导",
 SITE:"卖家精灵站点名(步骤11/12)", MARKET:"亚马逊市场站点", TREND_MONTH:"YYYYMM 格式",
 LLM_NO_THINK:"需求 7.3 硬性要求, 不建议关闭",
 ATTR_MODE:"auto=LLM自动提取+人工补充; manual=仅用 ATTR_KEYS",
 SS_CDP_URL:"填 http://localhost:9222 直连已开 Chrome(不弹窗); 留空自动启动有头浏览器",
 SS_QUOTA_WINDOW_START:"备用账号仅该时刻(默认20:00)~次日08:00可导出, 未到点自动等待",
 SS_QUOTA_WINDOW_END:"备用账号窗口结束时刻(HH:MM, 默认08:00, 跨天区间)",
 SS_ACTIVE_ACCOUNT:"本轮起始账号: auto=默认(主账号优先); main=主账号; backupN=第N个备用; 或直接填账号(如 Boen05)。仅调整使用顺序, 配额0仍自动换号, 不绕过 20:00 窗口",
 COOKIE:"HTTP直连(步骤1/10)鉴权; 仅在 Profile 无会话时导入",
 COOKIE_CHECK:"失效/被风控则启动前告警+阻断, 避免整轮抓取全失败",
 FETCH_ROUND_PAUSE:"已内化(2026-09-16): 直接请求轮间冷却固定 60 秒, 无需配置",
 FETCH_STALL_ROUNDS:"已内化(2026-09-16): 直接请求固定 = 首轮 + 1 轮重试; 残余全部转浏览器补抓(见 FALLBACK)",
 CORE_SEARCH_PAGES:"步骤9: 每个达标核心大词搜索翻 N 页(默认5); 页数越多收集越全、耗时越长",
 LONGTAIL_SEARCH_PAGES:"步骤9: 每个抓取词(五维度各取频率最高实体 × 搜索量最高的达标词, 最多 5 条)搜索翻 N 页(默认4); 页数越多收集越全、耗时越长",
 REQUEST_MIN_INTERVAL:"调大可降低亚马逊风控; 风控频繁建议 4-6 秒",
 REVIEW_POLICY:"keep_original=只提示 / follow_review=跟随复核 / drop_on_disagree=不一致一律剔除",
 MONTHLY_SALES_MIN:"步骤14行清洗: 月销量统一补全后删除月销量 ≤ 该值的行(默认10; 填0=仅排除空/0销量, 不额外筛选)",
 FETCH_ASIN_TRIES:"已内化(2026-09-16): 单轮内每 ASIN 固定试 1 次 URL 形态, 无需配置",
 FALLBACK:"步骤01 详情 + 步骤10 变体共用的唯一抓取开关: auto(默认)=直连抓不到就自动用本机浏览器补回来(带 Profile 登录态, 即\"Cookie 试 / Profile 兜底\"); off=完全不用浏览器, 只走直连、抓不到如实记为失败。直连重试轮数/间隔/退避/补抓轮数已内化到代码(直连首轮+1 轮重试 → 浏览器补抓 2 轮), 无需在配置里填写",
 QUERY_WAIT_TIMEOUT:"步骤11 查流量来源: 点查询后等待结果表格/导出按钮的最长秒数(不配置=沿用 DOWNLOAD_TIMEOUT 默认120)。2026-09-11 新增: 原与下载等待共用, 而具体历史月份查询偶发长时间无响应(实战 3 次 120s 超时) → 拆出本键, 建议 300; 注意『导出下载』的重试等待仍由 DOWNLOAD_TIMEOUT 控制。若同一账号改选『最近30天』立刻恢复正常, 说明是该月份数据不全(非超时问题)",
 GATE_NEW_SELLERS:"步骤15 类目门禁④: 「新老品=新品」的行对 BuyBox卖家 去重后的个数 > 本值 → 达标(默认10)。注意该指标实测在 50~136(6 个类目), 想有区分度需上调(如 40~65); 改完需重跑步骤15 才生效",
 LONGTAIL_PER_DIM:"步骤4 长尾词每维条数(默认5)。长尾词 = 该维度在「全部标题+五点」里出现频率最高的实体 × 核心根词, 5 个维度(场景/人群/功能/属性/痛点) 各取本值条 → 默认共 25 条; 实体台账见 <CAT>-keywords-entities.txt。改完需重跑步骤4(及其下游)",
 DRY_RUN:"true=离线模拟(无需密钥/网络/费用); false=真实运行",
 WECOM_WEBHOOK:"未配置则告警推送静默",
ATTR_SPEC_RUNS:"步骤16 属性集提取: 对标题+五点跑几遍 LLM 抽取再并集去重(默认3)",
ATTR_STAB_RUNS:"步骤17 标注: 同一批行标注几遍做一致性表决(默认5; 3=小试; <2 则跳过本步)",
ATTR_STAB_CHUNK:"步骤17 标注: 单次 LLM 调用送几行(默认12; 调大输出易被截断)",
ATTR_STAB_WORKERS:"步骤17 标注: 同一遍内同时发几个 LLM 请求(默认3; 撞限流自动退避重试)",
ATTR_STAB_MAX_ALONE:"步骤17 表决: 某遍与众数不同的格占比超过此% 视为语义离群、不纳入交付(默认10)",
ATTR_STAB_MAX_RETRY:"步骤17 标注: 被结构闸门拒绝后最多重跑几轮(默认2)",
ATTR_STAB_MIN_RATE:"步骤17 交付值一致率硬门槛%: 低于此值阻断(默认85; 首跑建议0不阻断, 看实际值再定)",
ATTR_STAB_PROMPT:"步骤17 标注提示词路径(留空=默认本类目目录 prompts/23_llm_only_pipeline.md)",
ATTR_STAB_GEN_VOCAB:"步骤18 冷启动词表生成: 对无词表的 enum/multi 属性自动聚类生成规范值(true=开/false=关)",
ATTR_STAB_VOCAB_GRANULARITY:"步骤18 词表聚类合并粒度: 0=最粗合并(类别少)/100=最细细分(类别多), 默认50",
ATTR_STAB_AUTO_ONBOARD:"步骤18 新词写回: true=初审后自动写回 rules.json(全自动); false=半自动交人工审(默认)",
RECENT_LOG_LINES:"步骤19 体检: 最近N条日志的成功率统计窗口(默认20)",
REPORT_FRESH_DAYS:"步骤19 体检: 报告新鲜度检查天数(默认7)",
STEP_DURATION_MAX_SECONDS:"步骤19 体检: 单步耗时超过此秒数预警(默认7200=2小时)",
STEP_DURATION_MEAN_MULT:"步骤19 体检: 单步耗时超过本轮均值N倍预警(默认3)",
STEP_DURATION_MEAN_FLOOR:"步骤19 体检: 耗时均值下限秒(默认600; 低于此不计入均值)"
};
const STATUS_COLOR = {DONE:"#34d399",RUNNING:"#60a5fa",FAILED:"#fb7185",SKIP:"#2dd4bf",PENDING:"#5b6b85",INTERRUPTED:"#f59e0b"};

/* ============ DOM ============ */
const $ = id => document.getElementById(id);
let selFile = localStorage.getItem("wui_cfg_file") || "config.yaml";
let lastLogId = -1, logMode = null, running = false, busy = false, lastStatus = null;
// 日志游标归属的**缓冲**(每个在跑的类目一份, id 序列各自从 1 开始; 无运行记录时是 "__server__")。
// 2026-09-15 修: 换类目后若继续用上一个类目的游标, `since(旧的大数)` 对新缓冲永远返回空
// → 日志面板停在旧类目内容上(用户实测: 点进 FR 运行台却还显示 UK 的步骤10)。
let lastLogKey = "";
let fullLogOn = false, lastFullLogAt = 0;
let bootInfo = null, cfgValues = {}, metaTimer = null, lastAlertSig = "", lastHealthSig = "";
// 与 webui/server.py WEBUI_VERSION 保持一致: 页面据此识别旧版服务并提示重启
const WUI_EXPECT = "2026-09-21.01";
let shownPct = -1, shownDone = -1;
const GAUGE_C = 314.159;

/* ============ 工具 ============ */
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function toast(msg, type = "") {
  const d = document.createElement("div");
  d.className = "toast " + type;
  d.innerHTML = `<span></span><span class="bar" style="--dur:${type === "err" ? 7 : 4.5}s"></span><span class="close">✕</span>`;
  d.firstChild.textContent = msg;
  const close = () => { if (d.parentNode) { d.classList.add("out"); setTimeout(() => d.remove(), 380); } };
  d.querySelector(".close").onclick = close;
  d.onclick = e => { if (e.target === d) close(); };
  $("toasts").appendChild(d);
  setTimeout(close, type === "err" ? 7000 : 4600);
}
async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, {
      headers: {"Content-Type": "application/json"},
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined
    });
  } catch (e) {
    // 浏览器网络层失败(服务未启动 / file:// 打开 / 端口占用等)
    throw new Error("无法连接本地服务(127.0.0.1:8790)。请先双击 start_webui.bat 启动服务, 再刷新本页。(" + e.message + ")");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = {ok:false, error:"响应非 JSON"}; }
  if (!res.ok || data.ok === false) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}
function fmtDur(start, end) {
  const p = s => { const m = /^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})$/.exec(s); return m ? new Date(m[1].replace(/-/g,"/") + " " + m[2] + ":" + m[3] + ":" + m[4]) : null; };
  const a = p(start), b = end ? p(end) : null;
  if (!a) return "";
  const t = b ? b - a : Date.now() - a;
  if (t < 0 || !isFinite(t)) return "";
  const s = Math.floor(t / 1000);
  if (s < 60) return s + "s";
  const mi = Math.floor(s / 60);
  if (mi < 60) return mi + "m" + (s % 60) + "s";
  return (mi / 60).toFixed(1) + "h";
}
function fmtClock(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), r = sec % 60;
  if (h > 0) return h + "h " + pad(m) + "m";
  if (m > 0) return m + "m " + pad(r) + "s";
  return r + "s";
}
const pad = n => String(n).padStart(2, "0");

/* ============ LLM 用量/费用展示(2026-09-28) ============
   口径: tokens 以模型返回的 usage 为准; 费用是本地按价表**估算**(不等于平台账单)。
   项目有**两个模型通道**(文本走 LLM_MODEL / 图片走 VISION_MODEL, 两个 key),
   且图片 token 是服务端按尺寸折算进 prompt 的 → 看板上**合计为主, 拆分放 tooltip**。 */
function fmtTok(n) {                     // token 缩写: 1.58M / 12.3k / 950
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
function fmtYuan(v) { return (Number(v) || 0).toFixed(4) + " 元"; }
const CH_LABEL = { text: "文本", vision: "视觉", unknown: "未分通道" };
const chLabel = c => CH_LABEL[c] || c;
function usageLine(u) {                  // "2.18M tok · 预估费用 3.3972 元"; 无调用返回 ""
  if (!u || !u.calls) return "";
  return fmtTok(u.total_tokens) + " tok · 预估费用 " + fmtYuan(u.cost_cny);
}
function usageTip(u, head) {             // tooltip: 累计/本轮 + 通道拆分 + 口径说明
  if (!u || !u.calls) return "";
  const lines = [head || "LLM 用量(累计)"];
  // 累计 vs 本轮(2026-09-29 用户定稿): 合计是**累计**口径 —— 该类目历史轮次 + 本次,
  // 含重跑与中途中断, **不因重置步骤/重置类目清零**; 与本轮不同时补一行"本轮",
  // 免得再把累计数误读成"这一轮刚烧的"(实例: 未开跑的步骤10 卡片上已有 06:10 那次的账)。
  const runCalls = Number(u.run_calls || 0);
  if (runCalls && runCalls !== Number(u.calls || 0)) {
    lines.push(`本轮: ${runCalls} 次调用 · ${fmtTok(u.run_total_tokens)} tok · ${fmtYuan(u.run_cost_cny)}`);
  }
  const ch = u.by_channel || {};
  const keys = Object.keys(ch).filter(k => ch[k] && ch[k].calls);
  if (keys.length > 1) {
    keys.sort().forEach(k => lines.push(
      `  ${chLabel(k)}: ${ch[k].calls} 次 · ${fmtTok(ch[k].total_tokens)} tok · ${fmtYuan(ch[k].cost_cny)}`));
  }
  lines.push(`合计(累计): ${u.calls} 次调用 · ${Number(u.total_tokens || 0).toLocaleString()} tok`);
  lines.push(`预估费用 ${fmtYuan(u.cost_cny)}(命中 ${fmtTok(u.in_hit)} / 未命中 ${fmtTok(u.in_miss)} / 输出 ${fmtTok(u.out)})`);
  if (u.price_source) lines.push(`价格来源: ${u.price_source}`);
  if (u.failed_attempts) lines.push(`失败尝试 ${u.failed_attempts} 次(未计费)`);
  if (u.unpriced_calls) lines.push(`未定价 ${u.unpriced_calls} 次(模型不在官方价目表, 费用未计入)`);
  lines.push("口径: **累计**(该类目历史轮次 + 本次, 含重跑与中断; 重置步骤/重置类目都不清零)。");
  lines.push("token 以模型返回的 usage 为准; 费用为本地按价表估算, 非平台账单。");
  return lines.join("\n");
}
/* 类目中心那一格的费用小字(renderHomeRows 与 pollHubDur 共用, 口径必须一致)
   文案 = `💰 <token总数> tok · 预估费用 <金额> 元`(2026-09-28 用户要求: 费用改称"预估费用",
   并在其前加上 token 总数 —— 与顶部/步骤卡片共用 usageLine, 全页面只有一种说法) */
function hubCostText(u) {
  return (u && u.calls) ? "💰 " + usageLine(u) : "";
}
function hubCostHtml(u) {
  const txt = hubCostText(u);
  const tip = txt ? usageTip(u, "该类目累计 LLM 预估费用(含历史轮次/重跑/中断)") : "";
  return `<div class="costv" style="font-size:11px;color:var(--txt-2);font-family:var(--mono)`
    + (txt ? "" : ";display:none") + `"` + (tip ? ` title="${esc(tip)}"` : "") + `>${txt}</div>`;
}

/* ============ 入场动画 ============ */
function initReveal() {
  const els = document.querySelectorAll(".fade-up");
  if (!("IntersectionObserver" in window)) { els.forEach(e => e.classList.add("in")); return; }
  const io = new IntersectionObserver((es) => {
    es.forEach(en => { if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); } });
  }, { rootMargin: "0px 0px -6% 0px", threshold: 0.05 });
  els.forEach(e => io.observe(e));
  // 兜底: 3s 后强制可见(防止观察器异常)
  setTimeout(() => els.forEach(e => e.classList.add("in")), 3200);
}

/* ============ 引导与轮询 ============ */
function setConn(on, runningNow) {
  const pill = $("connPill");
  pill.className = "pill " + (on ? (runningNow ? "run" : "ok") : "off");
  $("connTxt").textContent = on ? (runningNow ? "流水线运行中" : "服务在线 · 空闲") : "服务离线";
  const b = $("offBanner");
  if (b) b.style.display = on ? "none" : "flex";
}
async function boot() {
  reorderCards(); renumberCards(); applyCfgFoldUI();   // 2026-09: 卡片重排/重编号 + 配置中心可折叠
  // file:// 直开提示(服务必须通过 http 访问)
  if (location.protocol === "file:") {
    const ov = $("fileOverlay");
    if (ov) {
      ov.style.display = "flex";
      $("btnGoUrl").onclick = () => { location.href = "http://127.0.0.1:8790/"; };
    }
    return;
  }
  try {
    bootInfo = await api("/api/bootstrap");
    const bt = $("buildTag");
    const ver = bootInfo.webui_version || "";
    if (bt) bt.textContent = ver || "旧版(无版本号)";
    // 版本缺失/不符 = 旧服务进程: 累计计数/产物下载白名单等新功能不会生效, 明确提示
    if (ver !== WUI_EXPECT) {
      if (bt) { bt.style.color = "#fbbf24"; bt.style.fontWeight = "700"; }
      const why = ver ? `旧构建(${ver})` : "旧版服务(无版本号字段)";
      toast(`⚠ ${why}: 累计失败/中断计数、步骤产物弹窗等未生效 —— 请重启服务(重启后应显示 ${WUI_EXPECT})`, "err");
    }
    setConn(true, false);
    await loadConfigs();
    $("stepSel").innerHTML = STEPS.map(s => `<option value="${s.n}">${pad(s.n)}</option>`).join("");
    $("stepSel").value = 0;
    syncStepLabels();
    wire();
    // 2026-09: 默认落在「类目中心」; 占位模板(config.yaml)不进入运行台 ——
    // 若记忆的是占位/模板, 切到第一个真实类目(2026-09-15: 优先切回"上次记住的类目", 并给出提示,
    // 不再静默落到字母序第一个类目)
    if (!String(selFile || "").startsWith("data/")) {
      const opts = [...$("cfgSel").options];
      const wantCat = localStorage.getItem("wui_cat") || catOfValue(selFile);
      const first = (wantCat && opts.find(o => o.dataset.cat === wantCat && String(o.value || "").startsWith("data/")))
        || opts.find(o => String(o.value || "").startsWith("data/"));
      if (first) {
        const prev = selFile;
        selFile = first.value;
        localStorage.setItem("wui_cfg_file", selFile);
        $("cfgSel").value = selFile;
        rememberCat(first.dataset.cat || catOfValue(selFile));
        if (prev && prev !== selFile && !wantCat) {
          toast(`已切到类目「${first.dataset.cat || selFile}」(原「${prev}」是根模板/占位, 不进入运行台)—— 请确认这是你要跑的类目`);
        }
      }
    }
    const onReal = String(selFile || "").startsWith("data/");
    if (!onReal) { const rc = $("navRunCat"); if (rc) rc.textContent = "—"; }
    // 2026-09-09: 刷新后恢复上次视图(运行台 / 类目中心), 不再无条件回类目中心;
    // 仅在选中真实类目时允许落在运行台(占位/模板仍回类目中心)
    const wantRun = onReal && localStorage.getItem(WUI_VIEW_KEY) === "run";
    if (wantRun) { showRun(); } else { showHome(); }
    if (onReal) { try { await loadConfig(); await pollNow(); } catch (e) {} }
    pollLoop();
  } catch (e) {
    setConn(false);
    toast("初始化失败: " + e.message, "err");
  }
}
function clearLogPanel() {
  // 2026-09-11: 清空实时日志面板(新运行开始 / 手动「清屏」共用)。
  // 背景: 流式日志是服务端全局缓冲, 新运行时若不清面板, 上一次运行的旧行会留在面板上,
  // 新类目的行只在其后追加 → 用户看着像"日志还接着上一个类目"。
  const c = $("console");
  if (c) c.innerHTML = "";
}

function wire() {
  $("stepSel").addEventListener("change", syncStepLabels);
  $("cfgSel").addEventListener("change", async () => {
    selFile = $("cfgSel").value;
    localStorage.setItem("wui_cfg_file", selFile);
    rememberCat(catOfValue(selFile));      // 记住类目, 供配置失效时按类目找回(2026-09-15)
    await loadConfig(); pollNow();
  });
  $("btnRefreshCfg").onclick = async () => { await loadConfig(); toast("已重新读取 " + selFile, "ok"); };
  $("btnReconn").onclick = () => location.reload();
  $("btnRun").onclick = () => startRun("resume");
  $("btnFrom").onclick = () => startRun("from");
  $("btnStep").onclick = () => startRun("step");
  $("btnStop").onclick = stopRun;
  $("btnResetStep").onclick = resetStep;
  $("btnArchive").onclick = archiveNow;
  $("btnClearLog").onclick = clearLogPanel;
  $("logFull").addEventListener("change", () => {
    fullLogOn = $("logFull").checked;
    lastFullLogAt = 0;          // 立即触发一次完整加载
    if (fullLogOn) refreshFullLog();
  });
  $("btnShowRaw").onclick = () => {
    expandCfgIfFolded();   // 配置中心被收起时先展开(2026-09)
    const w = $("rawWrap");
    const show = w.style.display === "none";
    w.style.display = show ? "block" : "none";
    $("btnShowRaw").textContent = show ? "收起原始 YAML" : "查看原始 YAML";
    if (show) $("rawBox").scrollTop = 0;
  };
  $("btnSaveCfg").onclick = saveConfig;
  $("btnCfgFold").onclick = toggleCfgFold;
  $("ncCancel").onclick = () => { $("newCatOverlay").style.display = "none"; };
  $("ncOk").onclick = submitNewCat;
  $("ncCat").addEventListener("keydown", e => { if (e.key === "Enter") submitNewCat(); });
  $("navHome").onclick = showHome;
  $("navRun").onclick = showRun;
  $("btnNewCatHome").onclick = openNewCatWizard;
  const hrH = $("homeHistRows");
  if (hrH) hrH.addEventListener("click", onHomeClick);
  const hsb = $("homeSearchBtn"), hcl = $("homeClearBtn"), hsin = $("homeSearch");
  if (hsb) hsb.onclick = applyHomeSearch;
  if (hcl) hcl.onclick = clearHomeSearch;
  if (hsin) hsin.addEventListener("keydown", e => { if (e.key === "Enter") applyHomeSearch(); });
  const hoo = $("homeOpenOnly"); if (hoo) hoo.onclick = toggleHomeOpen;
  const hpv = $("homePrev"); if (hpv) hpv.onclick = () => homePageMove(-1);
  const hnx = $("homeNext"); if (hnx) hnx.onclick = () => homePageMove(1);
  const hvc = $("homeViewCards"), hvt = $("homeViewTree");
  if (hvc) hvc.onclick = () => applyHubView("cards");
  if (hvt) hvt.onclick = () => applyHubView("tree");
  const tr = $("homeTree");
  if (tr) tr.addEventListener("click", onHomeClick);
  // 关键词清单面板(核心大词/长尾词, 三态, 2026-09-29)
  $("btnReloadKw").onclick = () => loadKeywordPanel();
  $("btnSaveKwMode").onclick = saveKwMode;
  $("btnSaveKwGate").onclick = saveKwGate;
  $("btnKwCoreAdd").onclick = () => addKwWords("core");
  $("btnKwTailAdd").onclick = () => addKwWords("longtail");
  $("btnKwCoreFill").onclick = () => fillKwFromAuto("core");
  $("btnKwTailFill").onclick = () => fillKwFromAuto("longtail");
  $("btnKwCoreClear").onclick = () => clearKwEdit("core");
  $("btnKwTailClear").onclick = () => clearKwEdit("longtail");
  $("btnKwInjectAdd").onclick = () => addKwWords("inject");
  $("btnKwInjectClear").onclick = () => clearKwEdit("inject");
  document.querySelectorAll('input[name="kwMode"]').forEach(r => r.addEventListener("change", () => {
    // 切到人工清单且清单为空时, 先用自动词打底(典型用法: 在达标词基础上删掉不想要的)
    if (currentKwMode() === "manual" && !kwEdit.core.length && kwData) {
      fillKwFromAuto("core");
    } else {
      renderKeywordPanel();
    }
  }));
  $("btnReloadAttr").onclick = () => loadAttrSpec();
  $("btnSaveAttr").onclick = saveAttrSpec;
  $("btnConfirmAttr").onclick = confirmAttrSpec;
  $("btnAddAttr").onclick = addAttrRow;
  const attrTbl = $("attrRows");
  if (attrTbl) attrTbl.addEventListener("input", ev => {
    if (ev.target && ev.target.classList
        && (ev.target.classList.contains("attr-name") || ev.target.classList.contains("attr-hint"))) {
      markAttrDirty();
    }
  });
  if (attrTbl) attrTbl.addEventListener("click", ev => {
    if (ev.target && ev.target.classList && ev.target.classList.contains("attr-del")) {
      deleteAttrRow(ev.target);
    }
  });
  $("btnReloadStd").onclick = () => loadStdVocab();
  $("btnSaveStd").onclick = saveStdVocab;
  const stdBox = $("stdRows");
  if (stdBox) stdBox.addEventListener("click", ev => {
    const btn = ev.target && ev.target.classList && ev.target.classList.contains("std-regen") ? ev.target : null;
    if (btn) regenerateStdAttr(btn.dataset.attr, btn.dataset.gran);
  });
  if (stdBox) stdBox.addEventListener("input", ev => {
    if (ev.target && ev.target.classList && (ev.target.classList.contains("std-vals") || ev.target.classList.contains("std-gran"))) {
      markStdDirty();
      if (ev.target.classList.contains("std-gran")) {
        const row = ev.target.closest(".std-row");
        const val = row && row.querySelector(".std-gran-val");
        const regen = row && row.querySelector(".std-regen");
        if (val) val.textContent = ev.target.value;
        if (regen) regen.dataset.gran = ev.target.value;
      }
    }
  });
  const hb = $("btnReloadHist");
  if (hb) hb.onclick = loadHistory;
  const hr = $("histRows");
  if (hr) hr.addEventListener("click", onHistoryClick);
  wireHistControls();
  $("lvFilter").onchange = () => { /* 重绘当前控制台以应用过滤 */
    if (logMode === "file") pollNow();
  };
}

/* ============ 历史类目(并入运行控制; 搜索+分页 2026-09-04) ============ */
let __histAll = [];        // 全量类目(API 返回, 按 CAT 聚合)
let __histQ = "";          // 搜索词
let __histPage = 1;        // 当前页(1 起)
let __histSize = 8;        // 每页条数

function _histFiltered() {
  const q = __histQ.trim().toLowerCase();
  if (!q) return __histAll;
  return __histAll.filter(it => String(it.cat || "").toLowerCase().includes(q));
}
function _histRowHtml(it) {
  const badge = it.done >= it.total
    ? '<b style="color:#34d399">✓ 已完成</b>'
    : (it.running && it.running.length
       ? '<b style="color:#60a5fa">⏵ 运行中</b>'
       : (it.failed && it.failed.length
          ? '<b style="color:#f59e0b">⚠ 中断/失败</b>'
          : '<b style="color:#7d8bab">待续</b>'));
  const ops = `<button class="btn-sm btn-ghost" data-act="view" data-file="${esc(it.run_file)}" title="切到该类目查看步骤/日志">查看</button> ` +
    (it.can_resume ? `<button class="btn-sm btn-ok" data-act="resume" data-file="${esc(it.run_file)}" data-cat="${esc(it.cat)}" title="从该类目第一个未完成步骤续跑">续跑</button>` : "");
  return `<tr>
    <td><b>${esc(it.cat)}</b>${it.dry_run ? ' <span class="tiny" style="color:#7d8bab">DRY</span>' : ""}</td>
    <td><b>${it.done}</b><span style="color:var(--dim)">/${it.total}</span></td>
    <td>${badge}<div class="tiny" style="color:var(--dim)">${esc(it.current || "")}</div></td>
    <td><div class="tiny" style="color:var(--dim)">${esc(it.run_time || "")}</div><div class="tiny" style="color:var(--dim)">${esc(it.base || "")}</div></td>
    <td>${ops}</td>
  </tr>`;
}
function renderHistPage() {
  const rows = $("histRows");
  if (!rows) return;
  const list = _histFiltered();
  const pages = Math.max(1, Math.ceil(list.length / __histSize));
  if (__histPage > pages) __histPage = pages;
  if (__histPage < 1) __histPage = 1;
  const slice = list.slice((__histPage - 1) * __histSize, __histPage * __histSize);
  rows.innerHTML = list.length
    ? slice.map(_histRowHtml).join("")
    : `<tr><td colspan="5" class="empty">${__histQ ? "没有匹配该搜索词的类目" : "暂无历史运行记录 — 运行过流程后 .webui/runs/ 会留下配置快照"}</td></tr>`;
  const pg = $("histPage"), prev = $("histPrev"), next = $("histNext");
  if (pg) pg.textContent = `${__histPage} / ${pages}`;
  if (prev) prev.disabled = __histPage <= 1;
  if (next) next.disabled = __histPage >= pages;
  const cnt = $("histCount");
  if (cnt) cnt.textContent = __histQ ? `匹配 ${list.length} / ${__histAll.length} 个类目` : `共 ${list.length} 个类目`;
}
async function loadHistory() {
  try {
    const d = await api("/api/history");
    const rows = $("histRows");
    if (!rows) return;
    __histAll = d.items || [];
    __histQ = ($("histSearch") ? $("histSearch").value : "") || "";
    renderHistPage();
    const hint = $("histHint");
    if (hint) hint.textContent = __histAll.length
      ? "提示: 点「查看」用该次运行的配置快照切换到对应类目(流程状态/实时日志/配置中心/注入词面板随之切换); 点「续跑」从该类目第一个未完成步骤断点续跑。"
      : "";
    const w = $("catHistWrap");
    if (w && __histAll.length) w.open = true;
  } catch (e) {
    const rows = $("histRows");
    if (rows) rows.innerHTML = `<tr><td colspan="5" class="empty">加载失败: ${esc((e && e.message) || e)}</td></tr>`;
  }
}
function wireHistControls() {
  const q = $("histSearch"), prev = $("histPrev"), next = $("histNext"), size = $("histPageSize");
  const searchBtn = $("histSearchBtn"), clearBtn = $("histClearBtn");
  const applyQ = () => { __histQ = (q ? q.value : "") || ""; __histPage = 1; renderHistPage(); };
  if (q) {
    q.addEventListener("input", applyQ);                       // 实时过滤
    q.addEventListener("keydown", e => { if (e.key === "Enter") applyQ(); });
  }
  if (searchBtn) searchBtn.addEventListener("click", applyQ);  // 显式搜索按钮
  if (clearBtn) clearBtn.addEventListener("click", () => {     // 清空: 复位输入框与过滤
    if (q) q.value = "";
    __histQ = ""; __histPage = 1; renderHistPage();
  });
  if (prev) prev.addEventListener("click", () => { if (__histPage > 1) { __histPage--; renderHistPage(); } });
  if (next) next.addEventListener("click", () => {
    const pages = Math.max(1, Math.ceil(_histFiltered().length / __histSize));
    if (__histPage < pages) { __histPage++; renderHistPage(); }
  });
  if (size) size.addEventListener("change", () => {
    __histSize = parseInt(size.value, 10) || 8;
    __histPage = 1;
    renderHistPage();
  });
}
async function onHistoryClick(ev) {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const file = btn.dataset.file;
  if (btn.dataset.act === "view") {
    await viewHistoryRun(file);
  } else if (btn.dataset.act === "resume") {
    await resumeHistoryRun(file, btn.dataset.cat);
  }
}
async function viewHistoryRun(file) {
  // 用历史 run 快照作为"当前配置": 切 selFile → 各面板(状态/日志/注入词/配置)自动跟随该类目
  selFile = file;
  localStorage.setItem("wui_cfg_file", file);
  rememberCat(catOfValue(file));            // 同上(2026-09-15)
  pruneOtherSnapshots(file);   // 移除其它类目的历史快照残留
  // 历史快照不在"根配置下拉"(cfgSel 只列 config*.yaml): 补一个占位选项并选中, 避免下拉与实际不符
  const sel = $("cfgSel");
  if (sel && ![...sel.options].some(o => o.value === file)) {
    const o = document.createElement("option");
    o.value = file;
    o.textContent = cfgLabelFor(file);      // 类目配置 -> 类目名·REAL/DRY; 真快照才带"(历史快照)"
    o.dataset.cat = catOfValue(file);
    sel.appendChild(o);
  }
  if (sel) sel.value = file;
  try {
    await loadConfig();   // 配置中心/注入词按该快照的 CAT 渲染
    pollNow();            // ②步骤看板 + ③日志 切到该类目
    showRun();            // 进入运行台视图(从类目中心点入时切页)
    toast("已切换到历史配置 " + file, "ok");
    const c = $("secState");
    if (c) c.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { toast("切换失败: " + e.message, "err"); }
}
async function resumeHistoryRun(file, cat) {
  // 多类目并行(2026-09-15): 只有**该类目自己**在跑时才拦; 别的类目在跑不影响本类目续跑
  const targetCat = String(cat || "").trim();
  if (busy) { toast("已有维护任务进行中, 请稍候", "err"); return; }
  const _sameCatRunning = running && targetCat && targetCat === String((cfgValues || {}).CAT || "").trim();
  if (_sameCatRunning) { toast("该类目正在运行中, 请先中断", "err"); return; }
  await viewHistoryRun(file);           // 先切到该类目(含 cfgValues)
  const v = cfgValues;
  const effCat = String(cat || v.CAT || "").trim();
  const effList = String(v.LIST_URLS || "").trim();
  const urls = effList.split(/[;；]/).map(s => s.trim()).filter(Boolean);
  if (!effCat) { toast("该历史配置缺少 CAT, 无法续跑", "err"); return; }
  if (!urls.length) { toast("该历史配置缺少 LIST_URLS(榜单URL), 无法续跑 — 请先在配置中心补充", "err"); return; }
  const dry = !!v.DRY_RUN;
  // 实时拉一次运行清单(启动后可能又有别的类目在跑; bootInfo 是页面加载时的快照)
  let _runs = [];
  try { _runs = (await api("/api/bootstrap")).runs || []; } catch (e) { _runs = []; }
  const others = _runs.filter(r => r && r.cat && r.active && r.cat !== effCat).map(r => r.cat);
  const msg = `从历史断点续跑该类目?\n\n◆ 配置快照: ${file}\n◆ 类目: ${effCat}\n◆ 模式: ${dry ? "dry-run" : "真实运行"}\n◆ 行为: 从该类目第一个未完成步骤自动续跑`
    + (others.length ? `\n\n⚠ 并行运行中: ${others.join("、")}\n   · 卖家精灵浏览器(步骤0/11/12/13)会**自动排队串行**, 等待属正常现象\n   · 亚马逊抓取(步骤0/1/10)与并行的类目共用本机 IP, 更易触发风控` : "")
    + `\n\n确认开始?`;
  if (!confirm(msg)) return;
  busy = true; disableCtl(true);
  try {
    const d = await api("/api/run", { method: "POST", body: { config_file: file, mode: "resume", overrides: {} } });
    lastLogId = -1; logMode = null; clearLogPanel();   // 2026-09-11 新运行: 清空面板, 防上一次运行的旧行残留
    toast("已启动断点续跑: " + (d.run.label || "resume"), "ok");
    pollNow();
  } catch (e) { toast("启动失败: " + e.message, "err"); }
  busy = false; disableCtl(false);
}
function syncStepLabels() {
  const v = $("stepSel").value;
  $("fromLabel").textContent = v; $("stepLabel").textContent = v;
}
/* ============ 配置下拉"记住的值失效"时的回退(2026-09-15 修) ============
   现象(用户报障"不运行的时候运行控制里的配置文件会变成别的名称"):
   刷新页面时若 localStorage 里记的配置**不在** /api/configs 列表里(根模板 config.yaml、
   已删除/改名的类目、或被清理的历史快照路径 —— 快照不进这个列表), 原实现直接
   `selFile = d.configs[0].name`, 而 configs[0] 是**字母序第一个类目**(实测变成
   `Air Purifiers for Home Pets-US-202608`)。更糟的是它**不写回 localStorage**, 于是每次刷新
   都会再跳一次; 而"运行控制"里显示的就是另一个类目 —— 用户可能据此误启动别的类目(实测复现:
   存快照路径 → 显示 Air Purifiers)。
   现在: ①优先按**上次的类目名**(localStorage wui_cat, 或从 `data/<CAT>/config.yaml`、
   `run-<ts>-<CAT>-config-<rand>.yaml` 里解析出的 CAT)找回同类目配置;
   ②找不回才退回第一个, 并且**一定弹提示**说明原值失效、切到了哪个类目, 不再静默。 */
function snapshotCatOf(value) {
  const base = String(value || "").split(/[\\/]/).pop() || "";
  const m = /^run-\d{8}-\d{6}-(.+?)-config-[0-9a-f]{4,}\.ya?ml$/i.exec(base);
  return m ? m[1] : "";
}
function catOfValue(value) {
  const v = String(value || "");
  const m = /^data\/(.+)\/config\.ya?ml$/i.exec(v);
  return m ? m[1] : snapshotCatOf(v);
}
function rememberCat(cat) {
  try { if (cat) localStorage.setItem("wui_cat", cat); } catch (e) { /* 隐私模式等, 忽略 */ }
}
/* 下拉选项标签(2026-09-15 修): **按内容判断**, 不再把"路径"当标题。
   用户报障: 从类目中心进运行台, 配置显示成 `data/Chopping Boards-FR-202608/config.yaml · (历史快照)`
   —— 因为它其实只是**类目配置**, 而补选项的代码无条件加了"(历史快照)"后缀、还直接用原始路径当文字
   (这两处: viewHistoryRun / enterRunWith; scopeCfgDropdown 里也用过原始路径)。
   现在: 真快照(`.webui/runs/run-…`)才带"· (历史快照)"; 类目配置一律显示**类目名 + REAL/DRY**。 */
function isSnapshotPath(value) {
  const s = String(value || "").replace(/\\/g, "/");
  return /\.webui\/runs\//.test(s) || /(^|\/)run-\d{8}-\d{6}-/.test(s);
}
function cfgLabelFor(value) {
  const v = String(value || "");
  if (isSnapshotPath(v)) return v + " · (历史快照)";
  const cat = catOfValue(v);
  if (!cat) return v;
  let dry = null;
  const hit = (__cfgList || []).find(c => c.name === v);
  if (hit && hit.has_dry_run_key) dry = !!hit.dry_run;
  else if (cfgValues && String(cfgValues.CAT || "") === cat && "DRY_RUN" in cfgValues) dry = !!cfgValues.DRY_RUN;
  return cat + (dry === null ? "" : (dry ? " · DRY" : " · REAL"));
}
function resolveFallbackConfig(configs, prev) {
  const want = (localStorage.getItem("wui_cat") || "") || catOfValue(prev);
  if (want) {
    const hit = (configs || []).find(c => String(c.cat || "") === want);
    if (hit) return { name: hit.name, cat: hit.cat, matched: true };
  }
  const first = (configs || [])[0];
  return { name: first ? first.name : "config.yaml", cat: first ? first.cat : "", matched: false };
}
async function loadConfigs() {
  const d = await api("/api/configs");
  __cfgList = d.configs || [];        // 供 cfgLabelFor 判断 REAL/DRY(2026-09-15)
  const sel = $("cfgSel");
  sel.innerHTML = d.configs.map(c => {
    // 下拉以"类目名"为主展示(2026-09): data/<类目>/config.yaml -> 只显示类目名;
    // 根目录模板/占位 -> 显示文件名并标注"占位模板(勿运行)"
    const isCat = String(c.name || "").startsWith("data/") && /config\.ya?ml$/i.test(c.name);
    const ph = /YYYYMM|your-category|占位|template/i.test(String(c.cat || ""));
    let label;
    if (isCat) label = c.cat || c.name.replace(/^data\//, "").replace(/\/config\.yaml$/i, "");
    else label = ph ? (c.cat || c.name) + " · 占位模板(勿运行)" : c.name + (c.cat ? " · " + c.cat : "");
    // 防呆(2026-09-13): CAT 与所在目录名不一致的错位配置(运行/保存会被阻断) —— 两个条目可能同名, 必须标出来
    if (c.cat_dir_mismatch) label = "⚠ " + label + " · 目录名不一致(data/" + (c.dir || "?") + ", 勿用)";
    const dry = c.has_dry_run_key ? (c.dry_run ? " · DRY" : " · REAL") : "";
    return `<option value="${esc(c.name)}" title="${esc(c.name)}" data-cat="${esc(c.cat || "")}">${esc(label)}${dry}</option>`;
  }).join("");
  if (![...sel.options].some(o => o.value === selFile)) {
    // 记的值失效: 优先按类目找回, 找不回才退回第一个, 并且**明确告知**(不再静默跳到别类目)
    const prev = selFile;
    const pick = resolveFallbackConfig(d.configs, prev);
    selFile = pick.name;
    if (prev && prev !== selFile) {
      toast(pick.matched
        ? `已切回类目「${pick.cat}」(原配置「${prev}」已不在可用列表)`
        : `已切到类目「${pick.cat || selFile}」(原配置「${prev}」已不在可用列表)—— 请确认这是你要跑的类目`);
    }
  }
  sel.value = selFile;
  const cur = d.configs.find(c => c.name === selFile);
  if (cur && cur.cat) rememberCat(cur.cat);
}
/* ============ 新建类目向导(2026-09) ============ */
let __cfgListCache = [];
let __cfgList = [];          // /api/configs 的最新快照(标签渲染用, 2026-09-15)
async function openNewCatWizard() {
  try {
    if (!__cfgListCache.length) { const d = await api("/api/configs"); __cfgListCache = d.configs || []; }
  } catch (e) { /* 服务未启动等: 下拉留空由按钮报错 */ }
  const all = __cfgListCache || [];
  const cats = all.filter(c => String(c.name || "").startsWith("data/"));
  const rest = all.filter(c => !String(c.name || "").startsWith("data/"));
  const opts = [];
  if (cats.length) opts.push(...cats.map(c => `<option value="${esc(c.name)}">${esc(c.cat || c.name)} (真实类目, 推荐)</option>`));
  if (rest.length) opts.push(...rest.map(c => `<option value="${esc(c.name)}">${esc(c.cat || c.name)} (根/模板)</option>`));
  if (!opts.length) opts.push(`<option value="config.yaml">config.yaml (占位模板)</option>`);
  $("ncFrom").innerHTML = opts.join("");
  $("ncCat").value = ""; $("ncList").value = "";
  $("newCatOverlay").style.display = "flex";
  setTimeout(() => { const i = $("ncCat"); if (i) i.focus(); }, 60);
}
async function submitNewCat() {
  const cat = ($("ncCat").value || "").trim();
  if (!cat) { toast("请填写类目名(CAT)", "err"); return; }
  const body = { cat, copy_from: $("ncFrom").value, list_url: ($("ncList").value || "").trim() };
  try {
    const d = await api("/api/category/create", { method: "POST", body });
    $("newCatOverlay").style.display = "none";
    toast("已创建类目并选中: " + d.cat, "ok");
    __cfgListCache = [];
    selFile = d.file;
    localStorage.setItem("wui_cfg_file", selFile);
    await loadConfigs();
    selFile = d.file;                       // 确保选中新建的类目配置
    const sel = $("cfgSel"); if (sel) sel.value = d.file;
    await loadConfig(); pollNow();
    showRun();                              // 创建后进入该新类目的运行台
  } catch (e) { toast("创建失败: " + (e.message || e), "err"); }
}

/* ============ 视图切换: 类目中心 / 运行台(2026-09) ============ */
const RUN_CARDS = ["secRun", "secState", "secLog", "secMon", "secKwLists",
                   "secAttr", "secStd", "secCfg"];
// 2026-09-09: 记忆当前视图, 刷新后恢复(见 initBoot)
const WUI_VIEW_KEY = "wui_view";
function showHome() {
  const hc = $("homeCard");
  if (hc) hc.style.display = "";
  for (const id of RUN_CARDS) { const el = $(id); if (el) el.style.display = "none"; }
  try { localStorage.setItem(WUI_VIEW_KEY, "home"); } catch (e) {}
  loadHome();
  window.scrollTo({ top: 0 });
}
function showRun() {
  const hc = $("homeCard");
  if (hc) hc.style.display = "none";
  for (const id of RUN_CARDS) { const el = $(id); if (el) el.style.display = ""; }
  try { localStorage.setItem(WUI_VIEW_KEY, "run"); } catch (e) {}
  window.scrollTo({ top: 0 });
}
/* 从类目中心打开某类目运行台: 选中其配置(历史快照也补进下拉)并刷新各面板 */
function enterRunWith(file) {
  rememberCat(catOfValue(file));            // 同上(2026-09-15)
  selFile = file;
  localStorage.setItem("wui_cfg_file", file);
  __cfgListCache = [];
  pruneOtherSnapshots(file);   // 移除其它类目的历史快照残留
  // 历史快照文件名不在根/类目下拉里: 补占位选项并选中, 避免 loadConfigs 把 selFile 重置
  const sel = $("cfgSel");
  if (sel && ![...sel.options].some(o => o.value === file)) {
    const o = document.createElement("option");
    o.value = file;
    o.textContent = cfgLabelFor(file);      // 同上: 按内容判断, 不把路径当标题
    o.dataset.cat = catOfValue(file);
    sel.appendChild(o);
  }
  if (sel) sel.value = file;
  showRun();
  Promise.resolve().then(async () => {
    try { await loadConfig(); pollNow(); } catch (e) { toast("打开失败: " + (e.message || e), "err"); }
  });
}
/* ============ 类目中心: 两级分组(基类目卡片 → 站点 → 下方列表) 2026-09 ============ */
const __hub = { items: [], base: "", site: "", q: "", openOnly: false, page: 1, showAllBase: false, showAllSite: false, mode: "cards" };
const HUB_BASE_CAP = 12, HUB_SITE_CAP = 12, HUB_PAGE_SIZE = 20;
const REGION_TOKENS = new Set(["US","UK","GB","DE","FR","IT","ES","CA","JP","AU","MX","BR","IN","NL","SE","PL","TR","SG","AE"]);
function hubParts(cat) {
  let c = String(cat || "").trim(), month = "", site = "";
  const mm = /-(20\d{4})$/.exec(c);
  if (mm) { c = c.slice(0, c.lastIndexOf("-")); month = mm[1].slice(0, 4) + "-" + mm[1].slice(4); }
  const im = c.lastIndexOf("-");
  if (im >= 0) {
    const tok = c.slice(im + 1);
    if (REGION_TOKENS.has(tok.toUpperCase())) { site = tok; c = c.slice(0, im); }
  }
  return { base: c || cat, site, month };
}
function hubMatch(it, q) {
  const p = hubParts(it.cat);
  const hay = ((it.cat || "") + " " + p.base + " " + p.site + " " + p.month).toLowerCase();
  const eq = (v) => v && String(v).toLowerCase() === q;
  if (eq(p.base) || eq(it.cat) || eq(p.site)) return 3;      // 精准: 基类目/完整类目/站点完全一致
  const tokens = q.split(/[\s\-_]+/).filter(Boolean);
  if (tokens.length && tokens.every(t => hay.includes(t))) return 2;  // 模糊: 分词全命中
  if (hay.includes(q)) return 1;                              // 模糊: 子串命中
  return 0;
}
function hubStatus(it) {
  if (it.running && it.running.length) return "run";
  if (it.failed && it.failed.length) return "bad";
  if (it.done >= it.total) return "done";
  return "todo";
}
function hubDot(it) {
  const c = { run: "#60a5fa", bad: "#f59e0b", done: "#34d399", todo: "#7d8bab" }[hubStatus(it)] || "#7d8bab";
  return `<span style="color:${c}">●</span>`;
}
/* ============ 类目中心树形视图(2026-09 尝试, 可回退) ============ */
function hubLeafHtml(it) {
  const badge = it.done >= it.total
    ? '<span style="color:#34d399">✓</span>'
    : (it.running && it.running.length
      ? '<span style="color:#60a5fa">运行中</span>'
      : (it.failed && it.failed.length
        ? '<span style="color:#f59e0b">卡点' + esc(it.failed[0]) + '</span>'
        : '<span style="color:#7d8bab">待续</span>'));
  return `<li style="list-style:none;display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:3px 0">
    ${hubDot(it)} <b>${esc(it.cat)}</b> <span style="color:var(--dim)">${it.done}/${it.total} ${badge}</span>
    <button class="btn-sm btn-ghost" data-act="view" data-file="${esc(it.run_file)}">打开运行台</button>${it.can_resume ? ` <button class="btn-sm btn-ok" data-act="resume" data-file="${esc(it.run_file)}" data-cat="${esc(it.cat)}">续跑</button>` : ""}</li>`;
}
function renderHomeTree() {
  const tree = $("homeTree"), hint = $("homeHint");
  let list = __hub.items;
  if (__hub.openOnly) list = list.filter(it => !(it.done >= it.total) || (it.running && it.running.length));
  const q = (__hub.q || "").trim().toLowerCase();
  if (q) {
    const sc = [];
    list.forEach(it => { const s = hubMatch(it, q); if (s > 0) sc.push([it, s]); });
    sc.sort((a, b) => b[1] - a[1] || (a[0].run_time < b[0].run_time ? 1 : -1));
    list = sc.map(x => x[0]);
  }
  if (hint) hint.textContent = list.length
    ? `树形视图 · ${list.length} 项 — 点击「基类目 / 站点 / 月份」逐层展开, 叶子可直接「打开运行台 / 续跑」。`
    : "暂无匹配记录 — 点「全部类目」/「清空」查看, 或用「＋ 新建类目」开始。";
  const bases = {};
  list.forEach(it => {
    const p = hubParts(it.cat);
    const k = (p.base || it.cat).toLowerCase();
    bases[k] = bases[k] || { label: p.base || it.cat, n: 0, sites: {} };
    bases[k].n++;
    const sk = p.site || "—";
    (bases[k].sites[sk] = bases[k].sites[sk] || []).push(it);
  });
  const baseHtml = Object.keys(bases).sort().map(k => {
    const b = bases[k];
    const siteHtml = Object.keys(b.sites).sort().map(sk => {
      const its = b.sites[sk];
      const monthMap = {};
      its.forEach(it => { const m = hubParts(it.cat).month || "—"; (monthMap[m] = monthMap[m] || []).push(it); });
      const monthHtml = Object.keys(monthMap).sort().map(m => {
        const items = monthMap[m];
        return `<details><summary>${esc(m)} · ${items.length} 项</summary>
          <ul style="margin:4px 0 8px 18px;padding:0">${items.map(hubLeafHtml).join("")}</ul></details>`;
      }).join("");
      return `<details><summary>${esc(sk)} · ${its.length} 项</summary>
        <div style="margin-left:16px">${monthHtml}</div></details>`;
    }).join("");
    return `<details class="hubTreeBase"><summary><b>${esc(b.label)}</b> · ${b.n} 项</summary>
      <div style="margin-left:16px">${siteHtml}</div></details>`;
  }).join("");
  tree.innerHTML = list.length ? baseHtml : `<div class="empty">暂无匹配记录</div>`;
}
function syncHubViewUI() {
  const tree = __hub.mode === "tree";
  const c = $("homeViewCards"), t = $("homeViewTree");
  if (c) c.className = "btn-sm " + (tree ? "btn-ghost" : "btn-ok shine");
  if (t) t.className = "btn-sm " + (tree ? "btn-ok shine" : "btn-ghost");
  const treeEl = $("homeTree");
  if (treeEl) treeEl.style.display = tree ? "" : "none";
  const rows = $("homeHistRows");
  const tbl = rows ? rows.closest("table") : null;
  if (tbl) tbl.style.display = tree ? "none" : "";
  const pv = $("homePrev"), pgr = pv ? pv.parentElement : null;
  if (pgr) pgr.style.display = tree ? "none" : "";
  const bb = $("homeBaseBar"), sb = $("homeSiteBar");
  if (bb) bb.style.display = tree ? "none" : "";
  if (sb) sb.style.display = tree ? "none" : "";
  if (tree) renderHomeTree();
}
function applyHubView(mode) {
  __hub.mode = mode;
  syncHubViewUI();
  if (mode !== "tree") { renderHomeGroups(); renderHomeRows(); }
}
function renderHomeRows() {
  const rows = $("homeHistRows"), hint = $("homeHint");
  let list = __hub.items;
  if (__hub.openOnly) {
    list = list.filter(it => !(it.done >= it.total) || (it.running && it.running.length));
  }
  const selBase = __hub.base, selSite = __hub.site;
  if (selBase) {
    list = list.filter(it => hubParts(it.cat).base.toLowerCase() === selBase.toLowerCase());
    if (selSite) list = list.filter(it => (hubParts(it.cat).site || "—") === selSite);
  }
  const q = (__hub.q || "").trim().toLowerCase();
  if (q) {
    const scored = [];
    list.forEach(it => { const s = hubMatch(it, q); if (s > 0) scored.push([it, s]); });
    scored.sort((a, b) => b[1] - a[1] || (a[0].run_time < b[0].run_time ? 1 : -1));
    list = scored.map(x => x[0]);
  }
  const total = list.length;
  const cnt = $("homeSearchCount");
  if (cnt) cnt.textContent = (q ? `搜索匹配 ${total}` : "") + (__hub.openOnly ? (q ? " · " : "") + "仅未完成" : "");
  const oo = $("homeOpenOnly");
  if (oo) oo.className = "btn-sm " + (__hub.openOnly ? "btn-ok shine" : "btn-ghost");
  // 分页(每页 20)
  const pages = Math.max(1, Math.ceil(total / HUB_PAGE_SIZE));
  if (__hub.page > pages) __hub.page = pages;
  if (__hub.page < 1) __hub.page = 1;
  const slice = list.slice((__hub.page - 1) * HUB_PAGE_SIZE, __hub.page * HUB_PAGE_SIZE);
  const pg = $("homePage"), prev = $("homePrev"), next = $("homeNext"), tot = $("homeTotal");
  if (pg) pg.textContent = `${__hub.page} / ${pages}`;
  if (prev) prev.disabled = __hub.page <= 1;
  if (next) next.disabled = __hub.page >= pages;
  if (tot) tot.textContent = `共 ${total} 项`;
  const parts = [];
  if (selBase) parts.push(`${selBase}${selSite ? " · " + selSite : " · 全部站点"}`);
  if (q) parts.push(`搜「${__hub.q}」`);
  if (__hub.openOnly) parts.push("只看未完成");
  if (parts.length) parts.push(`第 ${__hub.page}/${pages} 页 · 共 ${total} 项`);
  if (hint) hint.textContent = total
    ? parts.join(" — ")                       // 未做任何过滤时留空, 不再显示提示行(2026-09-30)
    : "暂无匹配记录 — 点「全部类目」/「清空」查看, 或用「＋ 新建类目」开始。";
  rows.innerHTML = slice.length ? slice.map(it => {
    const p = hubParts(it.cat);
    const badge = it.done >= it.total
      ? '<b style="color:#34d399">✓ 已完成</b>'
      : (it.running && it.running.length
        ? '<b style="color:#60a5fa">⏵ 运行中 ' + esc(it.running.join(",")) + '</b>'
        : (it.failed && it.failed.length
          ? '<b style="color:#f59e0b">⚠ 卡在步骤' + esc(it.failed[0]) + '</b>'
          : '<b style="color:#7d8bab">待续</b>'));
    const tag = (p.site ? " · " + esc(p.site) : "") + (p.month ? " · " + esc(p.month) : "");
    // 累计运行时长(2026-09-15): 与 ②流程状态 的 ⏱「累计执行时长」同口径(同一后端字段
    // dur_sec, 由 core.state.RunState.cumulative_seconds 计算), 格式也复用 fmtClock。
    const durTxt = (typeof it.dur_sec === "number" && it.dur_sec > 0) ? fmtClock(it.dur_sec) : "—";
    // 两行: 上行累计运行时长, 下行 LLM `💰 <token总数> tok · 预估费用 X 元`(2026-09-28;
    // 2026-09-29 起该费用为**该类目累计**, 含历史轮次/重跑/中断, 重置不清零)。
    // ⚠️ `pollHubDur` 每 12s 会**静默刷新这一列** —— 它必须改写内层 `.durv`/`.costv`,
    //    不能再对整个 <td> 赋 textContent(那会把费用那行清掉, 表现为"费用显示几秒后消失")。
    const durCell = `<span class="durv" title="累计运行时长 = 累计执行时长(各步骤真正在跑的时长之和, 含中断/失败后重跑的段, 不含进程段之间没有在跑的空档); 与运行台 ②「累计执行时长」同一数字" style="font-family:var(--mono)">${durTxt}</span>`
      + hubCostHtml(it.usage);
    return `<tr><td><b>${esc(p.base)}</b>${tag}</td><td>${it.done}<span style="color:var(--dim)">/${it.total}</span></td>
      <td>${badge}${it.current ? " · " + esc(it.current) : ""}</td><td>${durCell}</td><td>${esc(it.run_time || "")}</td>
      <td><button class="btn-sm btn-ghost" data-act="view" data-file="${esc(it.run_file)}">打开运行台</button>${it.can_resume ? ` <button class="btn-sm btn-ok" data-act="resume" data-file="${esc(it.run_file)}" data-cat="${esc(it.cat)}">续跑</button>` : ""}</td></tr>`;
  }).join("") : `<tr><td colspan="6" class="empty">暂无匹配记录</td></tr>`;
}
function renderHomeGroups() {
  const barB = $("homeBaseBar"), barS = $("homeSiteBar");
  if (!barB) return;
  // ---- 基类目卡片(折叠 >HUB_BASE_CAP; 状态色点 + 数量) ----
  const bases = {};
  __hub.items.forEach(it => {
    const p = hubParts(it.cat);
    const k = p.base.toLowerCase();
    bases[k] = bases[k] || { label: p.base, n: 0, items: [] };
    bases[k].n++; bases[k].items.push(it);
  });
  const keys = Object.keys(bases).sort();
  const visible = __hub.showAllBase ? keys : keys.slice(0, HUB_BASE_CAP);
  const parts = [`<button class="btn-sm ${__hub.base ? "btn-ghost" : "btn-ok shine"}" data-base="">全部类目 (${__hub.items.length})</button>`];
  visible.forEach(k => {
    const b = bases[k];
    const rank = it => ({ run: 1, bad: 2, done: 3, todo: 4 }[hubStatus(it)] || 4);
    const worst = b.items.reduce((w, it) => (rank(it) < rank(w) ? it : w), b.items[0]);
    const sites = {};
    b.items.forEach(it => { const s = hubParts(it.cat).site || "—"; sites[s] = (sites[s] || 0) + 1; });
    const months = [...new Set(b.items.map(it => hubParts(it.cat).month).filter(Boolean))].sort();
    const durSum = b.items.reduce((s, it) => s + (typeof it.dur_sec === "number" ? it.dur_sec : 0), 0);
    const tip = `共 ${b.n} 项; 站点: ${Object.entries(sites).map(([s, c]) => s + "×" + c).join(", ")}; 月份: ${months.join(", ") || "—"}`
      + (durSum > 0 ? `; 累计运行时长合计 ${fmtClock(durSum)}` : "");
    parts.push(`<button class="btn-sm btn-ghost" data-base="${esc(b.label)}" title="${esc(tip)}">${hubDot(worst)} ${esc(b.label)} (${b.n})</button>`);
  });
  if (keys.length > HUB_BASE_CAP) {
    parts.push(`<button class="btn-sm btn-ghost" data-toggle="base">${__hub.showAllBase ? "收起 ▲" : `展开全部 (${keys.length})`}</button>`);
  }
  barB.innerHTML = parts.join("");
  [...barB.children].forEach(btn => {
    if (btn.dataset.toggle === "base") { btn.onclick = () => { __hub.showAllBase = !__hub.showAllBase; renderHomeGroups(); }; return; }
    btn.onclick = () => { __hub.base = btn.dataset.base || ""; __hub.site = ""; __hub.page = 1; renderHomeGroups(); renderHomeRows(); };
  });
  if (!__hub.base) { barS.style.display = "none"; barS.innerHTML = ""; return; }
  // ---- 站点卡片(折叠 >HUB_SITE_CAP) ----
  const sites = {};
  __hub.items.filter(it => hubParts(it.cat).base.toLowerCase() === __hub.base.toLowerCase())
    .forEach(it => { const s = hubParts(it.cat).site || "—"; sites[s] = (sites[s] || 0) + 1; });
  const siteKeys = Object.keys(sites).sort();
  const svis = __hub.showAllSite ? siteKeys : siteKeys.slice(0, HUB_SITE_CAP);
  const sp = [`<button class="btn-sm ${__hub.site ? "btn-ghost" : "btn-ok shine"}" data-site="">全部站点 (${siteKeys.length})</button>`];
  svis.forEach(s => sp.push(`<button class="btn-sm btn-ghost" data-site="${esc(s)}">${esc(s)} (${sites[s]})</button>`));
  if (siteKeys.length > HUB_SITE_CAP) {
    sp.push(`<button class="btn-sm btn-ghost" data-toggle="site">${__hub.showAllSite ? "收起 ▲" : `展开全部 (${siteKeys.length})`}</button>`);
  }
  barS.innerHTML = sp.join("");
  barS.style.display = "flex";
  [...barS.children].forEach(btn => {
    if (btn.dataset.toggle === "site") { btn.onclick = () => { __hub.showAllSite = !__hub.showAllSite; renderHomeGroups(); }; return; }
    btn.onclick = () => { __hub.site = btn.dataset.site || ""; __hub.page = 1; renderHomeGroups(); renderHomeRows(); };
  });
}
function applyHomeSearch() {
  const i = $("homeSearch");
  __hub.q = (i ? i.value : "").trim();
  __hub.page = 1;
  renderHomeRows();
}
function clearHomeSearch() {
  const i = $("homeSearch");
  if (i) i.value = "";
  __hub.q = "";
  __hub.page = 1;
  renderHomeRows();
}
function toggleHomeOpen() {
  __hub.openOnly = !__hub.openOnly;
  __hub.page = 1;
  renderHomeRows();
}
function homePageMove(d) {
  __hub.page = Math.max(1, __hub.page + (d || 0));
  renderHomeRows();
}
async function loadHome() {
  try {
    const d = await api("/api/history");
    __hub.items = d.items || [];
    __hub.base = ""; __hub.site = ""; __hub.q = "";
    __hub.page = 1; __hub.openOnly = false; __hub.showAllBase = false; __hub.showAllSite = false;
    __hub.mode = "cards";
    const si = $("homeSearch"); if (si) si.value = "";
    renderHomeGroups(); renderHomeRows(); syncHubViewUI();
    pollHubDur();          // 类目中心停留时, 「累计运行时长」列每 12s 静默跟随刷新
  } catch (e) {
    const rows = $("homeHistRows"), hint = $("homeHint");
    if (hint) hint.textContent = "加载失败: " + ((e && e.message) || e);
    if (rows) rows.innerHTML = `<tr><td colspan="6" class="empty">加载失败: ${esc((e && e.message) || e)}</td></tr>`;
  }
}
/* 类目中心「累计运行时长 / 预估费用」的静默刷新(2026-09-15; 2026-09-28 加费用行):
   只改那一列的内层 span, 不重渲染整表 —— 保住滚动位置与筛选。
   ⚠️ 必须写 `.durv`/`.costv` 两个子元素, **不能**对 <td> 整体赋 textContent,
      否则每 12s 会把费用那行抹掉(renderHomeRows 里同一处有对应说明)。 */
async function pollHubDur() {
  const hc = $("homeCard");
  if (!hc || hc.style.display === "none" || document.hidden) return;
  let d;
  try { d = await api("/api/history"); } catch (e) { return; }
  const items = (d && d.items) || [];
  const dur = {}, cost = {};
  items.forEach(it => {
    const k = String(it.run_file || "");
    if (typeof it.dur_sec === "number") dur[k] = it.dur_sec;
    if (it.usage) cost[k] = it.usage;
  });
  __hub.items.forEach(it => {
    const k = String(it.run_file || "");
    if (typeof dur[k] === "number") it.dur_sec = dur[k];
    if (cost[k]) it.usage = cost[k];
  });
  const rows = $("homeHistRows");
  if (!rows) return;
  const slice = hubFiltered().slice((__hub.page - 1) * HUB_PAGE_SIZE, __hub.page * HUB_PAGE_SIZE);
  [...rows.children].forEach((tr, i) => {
    const cell = tr.children[3];
    if (!cell || !slice[i]) return;
    const v = slice[i].dur_sec;
    const txt = (typeof v === "number" && v > 0) ? fmtClock(v) : "—";
    const durEl = cell.querySelector(".durv");
    if (durEl) { if (durEl.textContent !== txt) durEl.textContent = txt; }
    else if (cell.textContent !== txt) cell.textContent = txt;      // 兼容旧结构(无 .durv)
    const u = slice[i].usage || {};
    const costEl = cell.querySelector(".costv");
    if (costEl) {
      const cTxt = hubCostText(u);
      if (costEl.textContent !== cTxt) costEl.textContent = cTxt;
      const cTip = cTxt ? usageTip(u, "该类目累计 LLM 预估费用(含历史轮次/重跑/中断)") : "";
      if (costEl.title !== cTip) costEl.title = cTip;
      const want = cTxt ? "" : "none";
      if (costEl.style.display !== want) costEl.style.display = want;
    }
  });
}
/* 与 renderHomeRows 相同的过滤链(仅用于让"刷新那一列"与当前所在页对齐) */
function hubFiltered() {
  let list = __hub.items;
  if (__hub.openOnly) list = list.filter(it => !(it.done >= it.total) || (it.running && it.running.length));
  if (__hub.base) {
    list = list.filter(it => hubParts(it.cat).base.toLowerCase() === __hub.base.toLowerCase());
    if (__hub.site) list = list.filter(it => (hubParts(it.cat).site || "—") === __hub.site);
  }
  const q = (__hub.q || "").trim().toLowerCase();
  if (q) {
    const scored = [];
    list.forEach(it => { const s = hubMatch(it, q); if (s > 0) scored.push(it); });
    list = scored;
  }
  return list;
}
async function onHomeClick(ev) {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const file = btn.dataset.file;
  if (btn.dataset.act === "view") { enterRunWith(file); }
  else if (btn.dataset.act === "resume") { await resumeHistoryRun(file, btn.dataset.cat); }
}

/* 运行台配置下拉只显示"当前类目相关"(2026-09): 该 data/<类目>/config.yaml + 该类目的历史快照。
   切类目请回 🏠 类目中心。 */
function ensureCfgOption(value, label) {
  const sel = $("cfgSel");
  if (!sel) return;
  if (![...sel.options].some(o => o.value === value)) {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = label || cfgLabelFor(value);
    o.dataset.cat = catOfValue(value);
    sel.appendChild(o);
  }
}
/* 历史快照选项统一标注(2026-09-08): 下拉里 run-* 全部带 "(历史快照)", 与真实配置区分 */
function labelRunSnapshotOptions() {
  const sel = $("cfgSel");
  if (!sel) return;
  [...sel.options].forEach(o => {
    const v = o.value || "";
    if (/^run-/.test(v) && !String(o.textContent).includes("历史快照")) {
      o.textContent = v + " · (历史快照)";
    }
  });
}
/* 进入某类目前, 先移除下拉里其它类目的历史快照选项(避免旧页面残留串台) */
function pruneOtherSnapshots(file) {
  const sel = $("cfgSel");
  if (!sel) return;
  [...sel.options].forEach(o => {
    const v = o.value || "";
    if (/^run-/.test(v) && v !== file) sel.removeChild(o);
  });
}
function scopeCfgDropdown() {
  const sel = $("cfgSel");
  if (!sel) return;
  const cur = (cfgValues && cfgValues.CAT) || "";
  if (!cur) return;
  let kept = 0;
  [...sel.options].forEach(o => {
    const v = o.value || "";
    let keep;
    if (v === selFile) keep = true;
    else if (/^run-/.test(v)) {          // 历史快照选项: 归属上次进入的类目
      if (o.dataset.snapcat === undefined) o.dataset.snapcat = cur;
      keep = o.dataset.snapcat === cur;
    } else {                              // 根/类目列表选项: 按 data-cat 精确匹配
      keep = (o.dataset.cat || "") === cur;
    }
    if (keep) kept++;
    else sel.removeChild(o);
  });
  ensureCfgOption(selFile, cfgLabelFor(selFile));
  if (![...sel.options].some(o => o.value === selFile)) {
    const o = document.createElement("option");
    o.value = selFile; o.textContent = selFile;
    sel.appendChild(o);
  }
  sel.value = selFile;
  labelRunSnapshotOptions();
}

async function loadConfig() {
  const d = await api("/api/config?file=" + encodeURIComponent(selFile));
  cfgValues = d.values || {};
  const rc = $("navRunCat");
  if (rc) rc.textContent = cfgValues.CAT || selFile;
  scopeCfgDropdown();   // 下拉收窄到当前类目相关
  window.__ssAbsent = !(d.lists && d.lists.SS_ACCOUNTS !== null && d.lists.SS_ACCOUNTS !== undefined);
  window.__ssJson = JSON.stringify(d.lists && d.lists.SS_ACCOUNTS || []);
  $("rawBox").value = d.raw || "";
  renderChips(cfgValues);
  renderConfigForm(cfgValues);
  renderSsAccounts(d.lists && d.lists.SS_ACCOUNTS);
  renderStatusEmptyHint();
  refreshStepFilesIfOpen();     // 类目切换后: 产物弹窗开着才刷新(2026-09-15 取代产物列表)
  loadKeywordPanel();
  loadAttrSpec();
  loadStdVocab();
}

/* ---- 通用: 按 id 显示/隐藏元素(关键词清单面板在用; 2026-10-06 摘出保留) ---- */
function setElDisplay(id, show) {
  const el = $(id);
  if (el) el.style.display = show ? "" : "none";
}

/* ============ 关键词清单面板(核心大词 / 长尾词, 三态) ============ */
// 三态语义见 core/keyword_lists.py:
//   自动 = FR-04/FR-07 自动口径(与旧版完全一致);
//   人工 = 用下方清单(可在达标词里删掉不想要的, 也可加不达标词/手填新词), 门槛按最终词数判;
//   停止 = 步骤9 直接中止, 不抓取任何搜索数据。
let kwData = null;
let __kwDirty = false;
const kwEdit = { core: [], longtail: [], inject: [] };   // 编辑中的人工清单(未保存)
let __kwGateSaved = null;                            // 上次保存的门槛值(用于脏判断)

function markKwDirty() { __kwDirty = true; }
function currentKwMode() {
  const el = document.querySelector('input[name="kwMode"]:checked');
  return el ? el.value : "auto";
}
function kwModeRadio(v) { return document.querySelector(`input[name="kwMode"][value="${v}"]`); }

async function loadKeywordPanel() {
  try {
    const d = await api(`/api/keyword-lists?file=${encodeURIComponent(selFile)}`);
    if (d && d.ok === false) { throw new Error(d.error || "加载失败"); }
    kwData = d;
    const mode = d.mode === "manual" ? "manual" : (d.stopped ? "stop" : "auto");
    const r = kwModeRadio(mode); if (r) r.checked = true;
    kwEdit.core = ((d.core || {}).manual || []).slice();
    kwEdit.longtail = ((d.longtail || {}).manual || []).slice();
    kwEdit.inject = (((d.inject_only || {}).words) || []).slice();
    const g = $("kwGate"); if (g) g.value = d.gate;
    __kwGateSaved = d.gate;
    renderKeywordPanel();
    __kwDirty = false;
  } catch (e) {
    const box = $("kwGateInfo");
    if (box) box.textContent = "加载失败: " + ((e && e.message) || e);
  }
}

function renderKeywordPanel() {
  if (!kwData) return;
  const mode = currentKwMode();
  const core = kwData.core || {};
  const tail = kwData.longtail || {};
  const gate = kwData.gate || 2;
  const isManual = mode === "manual";
  const hint = $("kwModeHint");
  if (hint) hint.textContent = mode === "auto"
    ? "自动: 与旧版完全一致 —— 美国站按 FR-07 达标词判门槛; 非美国站门槛不适用(取站内搜索量前 5)。想改词就点下方「候选词 / 长尾词池」里的词, 会自动切到人工清单。"
    : isManual
      ? "人工清单: 保存后**步骤8(关键词报告) 与 步骤9(搜索收集)** 都按清单跑(不达标词、手填新词都会照搜/照列), 门槛按最终词数判 —— 记得点右上「保存并应用」; 想让已生成的报告跟着变, 用【仅运行步骤】跑 5 → 6 → 7 → 8(不必重置)。"
      : "人工停止: 保存后步骤9 直接中止, 本轮不抓取任何搜索数据(不需要填理由)。";
  const gi = $("kwGateInfo");
  if (gi) {
    const n = kwEdit.core.length;
    gi.textContent = mode === "stop"
      ? `当前门槛 ${gate}; 停止标记生效 → 步骤9 不抓取`
      : isManual
        ? `当前门槛 ${gate}; 人工清单 ${n} 个 ${n >= gate ? "≥" : "<"} 门槛 → ${n >= gate ? "✅继续" : "❌停止(补词或下调门槛)"}`
        : `当前门槛 ${gate}; ${core.gate_text || ""}`;
  }
  const sp = $("kwSearchPlan");
  if (sp) sp.textContent = kwData.search_plan ? "预计搜索量: " + kwData.search_plan.text : "";
  const uh = $("kwUsedHead");
  if (uh) uh.textContent = (kwData.used_head && kwData.used_head.length)
    ? "上轮实际生效: " + kwData.used_head.join(" ｜ ")
    : "上轮留痕: (暂无 —— 跑过一次步骤9 后生成 <CAT>-used-keywords.txt)";
  // 编辑控件只在人工模式下可用(自动/停止态隐藏, 避免误以为已生效)
  ["kwCoreAdd", "btnKwCoreAdd", "btnKwCoreFill", "btnKwCoreClear"].forEach(id => setElDisplay(id, isManual));
  ["kwTailAdd", "btnKwTailAdd", "btnKwTailFill", "btnKwTailClear"].forEach(id => setElDisplay(id, isManual));
  // 仅注入词是**独立附加**清单(不受三态影响, 自动态下也生效) -> 控件始终可用
  const ic = $("kwInjectCount");
  if (ic) {
    const io = kwData.inject_only || {};
    ic.textContent = `(人工 ${kwEdit.inject.length} 个`
      + (kwEdit.inject.length ? "" : `; 步骤10 实际注入 ${(io.effective_inject || []).length} 个`) + ")";
  }
  const cc = $("kwCoreCount");
  if (cc) cc.textContent = `(自动 ${(core.auto || []).length} 个 / 人工 ${kwEdit.core.length} 个`
    + (isManual ? "" : `; 生效 ${(core.effective || []).length} 个, 达标 ${core.qualified || 0} / 无判定 ${core.no_verdict || 0}`) + ")";
  const tc = $("kwTailCount");
  if (tc) tc.textContent = `(自动 ${(tail.auto || []).length} 个 / 人工 ${kwEdit.longtail.length} 个)`;
  const ca = $("kwCoreAuto");
  if (ca) ca.textContent = "自动核心词: " + ((core.auto || []).join("、") || "(无 — 请先跑步骤 4/5/7)");
  const ta = $("kwTailAuto");
  if (ta) {
    ta.textContent = `自动长尾词 ${(tail.auto || []).length} 个`
      + (Object.keys(tail.dimensions || {}).length
        ? "(按五维分组见下方词池 — 点词即可选入/移出人工清单)"
        : "(无 — 请先跑步骤 4)");
  }
  renderKwChips("core");
  renderKwChips("longtail");
  renderKwChips("inject");
  renderKwInjectPool();
  renderKwTailPool();
  renderKwCandPool();
}

/* 候选词池(2026-09-30 与长尾词池对齐布局/UI):
   原先是一个扁平 chip 列表、容器只有 max-height 没有边框 —— 与长尾词池(圆角边框 + 分组)不对称。
   现在同样: 同款圆角滚动框、按 **FR-07 判定**分组(长尾词池按五维)、点词即加入/移出人工核心清单,
   chip 样式/选中态/✓ 前缀/组内计数标题与长尾词池完全一致。 */
function renderKwCandPool() {
  const box = $("kwCandChips");
  if (!box || !kwData) return;
  const cands = kwData.candidates || [];
  const cc = $("kwCandCount");
  if (cc) {
    const q = cands.filter(c => c.qualified).length;
    cc.textContent = cands.length ? `(候选 ${cands.length} 个 / 达标 ${q} 个)` : "";
  }
  if (!cands.length) {
    box.innerHTML = '<span class="tiny" style="color:var(--dim)">(暂无候选词池 — 请先跑步骤 4/5/7 生成候选与判定)</span>';
    return;
  }
  // 分组键与长尾词池同构: 长尾按五维, 候选按判定; 达标恒排第一, 其余按判定字面
  const ORDER = ["达标", "未达标", "数据不足", "未判定"];
  const groups = new Map();
  cands.forEach(c => {
    const g = c.qualified ? "达标" : (String(c.verdict || "").trim() || "未判定");
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(c);
  });
  const rank = g => { const i = ORDER.indexOf(g); return i < 0 ? 99 : i; };
  const keys = [...groups.keys()].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  box.innerHTML = keys.map(g => {
    const rows = groups.get(g);
    const chips = rows.map(c => {
      const on = (kwEdit.core || []).includes(c.word);
      const bg = on ? "rgba(52,211,153,.14)" : "rgba(140,170,255,.12)";
      const bd = on ? "rgba(52,211,153,.4)" : "rgba(140,170,255,.25)";
      const vol = c.volume != null ? Number(c.volume).toLocaleString() : "—";
      return `<span class="chip kw-cand" data-kind="core" data-w="${esc(c.word)}" title="月搜索量 ${vol} / 判定 ${esc(c.verdict || "未判定")} — 点击${on ? "移出" : "加入"}人工核心清单"
        style="display:inline-block;cursor:pointer;border-radius:20px;padding:2px 10px;margin:2px 4px;font-size:12px;background:${bg};border:1px solid ${bd}">${on ? "✓ " : ""}${esc(c.word)}${c.qualified ? " ★" : ""}</span>`;
    }).join("");
    return `<div style="margin-bottom:5px"><span class="tiny" style="color:var(--txt-2)">${esc(g)}(${rows.length}):</span> ${chips}</div>`;
  }).join("");
  box.querySelectorAll(".kw-cand").forEach(x => x.onclick = () => kwPickWord("core", x.dataset.w));
}

/* 长尾词池(2026-09-29 用户报"长尾词怎么不可以选择"后新增):
   此前长尾词只能手打或用「以自动词为基础」, 没有可点的池子 —— 与核心词那边不对称。
   现在按五维(场景/人群/功能/属性/痛点)分组展示**步骤4 台账派生的长尾词**,
   并用 keywords.txt 的自动长尾词兜住"台账缺失/旧数据"的情况, 点词即加入/移出人工清单。 */
function renderKwTailPool() {
  const box = $("kwTailPool");
  if (!box || !kwData) return;
  const tail = kwData.longtail || {};
  const dims = tail.dimensions || {};
  const auto = tail.auto || [];
  const haveDims = Object.keys(dims).length > 0;
  const groups = [];
  const seen = new Set();
  Object.keys(dims).forEach(d => {
    const words = (dims[d] || []).filter(Boolean);
    if (words.length) {
      groups.push([d, words]);
      words.forEach(w => seen.add(w));
    }
  });
  const rest = auto.filter(w => !seen.has(w));
  if (rest.length) groups.push([haveDims ? "其它(台账外)" : "自动长尾词", rest]);
  if (!groups.length) {
    box.innerHTML = '<span class="tiny" style="color:var(--dim)">(暂无长尾词池 — 请先跑步骤4 生成五维实体台账)</span>';
    return;
  }
  box.innerHTML = groups.map(([d, words]) => {
    const chips = words.map(w => {
      const on = (kwEdit.longtail || []).includes(w);
      const bg = on ? "rgba(52,211,153,.14)" : "rgba(140,170,255,.12)";
      const bd = on ? "rgba(52,211,153,.4)" : "rgba(140,170,255,.25)";
      return `<span class="chip kw-pool" data-kind="longtail" data-w="${esc(w)}" title="点击${on ? "移出" : "加入"}人工长尾清单"
        style="display:inline-block;cursor:pointer;border-radius:20px;padding:2px 10px;margin:2px 4px;font-size:12px;background:${bg};border:1px solid ${bd}">${on ? "✓ " : ""}${esc(w)}</span>`;
    }).join("");
    return `<div style="margin-bottom:5px"><span class="tiny" style="color:var(--txt-2)">${esc(d)}(${words.length}):</span> ${chips}</div>`;
  }).join("");
  box.querySelectorAll(".kw-pool").forEach(x => x.onclick = () => kwPickWord(x.dataset.kind, x.dataset.w));
}

/* 仅注入词池(2026-10-06 用户口径: "仅注入改成自选，可以从核心词中选择"):
   池子 = **核心词候选里当前尚未被注入的那些**(候选池数据 kwData.candidates 减去
   生效核心词 kwData.core.effective 与正在编辑的人工核心清单 kwEdit.core)。
   点词即加入/移出「仅注入清单」—— 于是"想让某个核心词参与判定但不去抓取"只需点一下。
   分组/配色/计数与候选词池同构, 仅把选中态换成琥珀色 + ▸ 前缀以示区别。 */
function renderKwInjectPool() {
  const box = $("kwInjectPool");
  if (!box || !kwData) return;
  const injected = new Set([...(kwData.core || {}).effective || [], ...(kwEdit.core || [])]);
  const inj = kwEdit.inject || [];
  const cands = (kwData.candidates || []).filter(c => !injected.has(c.word));
  const cc = $("kwInjectPoolCount");
  if (cc) cc.textContent = cands.length ? `(可选 ${cands.length} 个)` : "";
  if (!cands.length) {
    box.innerHTML = '<span class="tiny" style="color:var(--dim)">(核心词候选已全部注入 —— 没有可选的仅注入词; 可在下面手填补充)</span>';
    return;
  }
  const ORDER = ["达标", "未达标", "数据不足", "未判定"];
  const groups = new Map();
  cands.forEach(c => {
    const g = c.qualified ? "达标" : (String(c.verdict || "").trim() || "未判定");
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(c);
  });
  const rank = g => { const i = ORDER.indexOf(g); return i < 0 ? 99 : i; };
  const keys = [...groups.keys()].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  box.innerHTML = keys.map(g => {
    const rows = groups.get(g);
    const chips = rows.map(c => {
      const on = inj.includes(c.word);
      const bg = on ? "rgba(245,158,11,.18)" : "rgba(140,170,255,.12)";
      const bd = on ? "rgba(245,158,11,.5)" : "rgba(140,170,255,.25)";
      const vol = c.volume != null ? Number(c.volume).toLocaleString() : "—";
      return `<span class="chip kw-inj-pool" data-w="${esc(c.word)}" title="月搜索量 ${vol} / 判定 ${esc(c.verdict || "未判定")} — 点击${on ? "移出" : "加入"}仅注入(只判定, 不抓取)"
        style="display:inline-block;cursor:pointer;border-radius:20px;padding:2px 10px;margin:2px 4px;font-size:12px;background:${bg};border:1px solid ${bd}">${on ? "▸ " : ""}${esc(c.word)}${c.qualified ? " ★" : ""}</span>`;
    }).join("");
    return `<div style="margin-bottom:5px"><span class="tiny" style="color:var(--txt-2)">${esc(g)}(${rows.length}):</span> ${chips}</div>`;
  }).join("");
  box.querySelectorAll(".kw-inj-pool").forEach(x => x.onclick = () => toggleKwWord("inject", x.dataset.w));
}

function renderKwChips(kind) {
  const box = $({ core: "kwCoreChips", longtail: "kwTailChips", inject: "kwInjectChips" }[kind]);
  if (!box) return;
  const words = kwEdit[kind] || [];
  const empty = kind === "inject"
    ? '<span class="tiny" style="color:var(--dim)">(无仅注入词 — 用下方「从核心词中选择」池点选, 或在补充框手填)</span>'
    : '<span class="tiny" style="color:var(--dim)">(人工清单为空 = 自动口径)</span>';
  box.innerHTML = words.length
    ? words.map(w => `<span class="chip" style="display:inline-block;background:rgba(52,211,153,.12);border:1px solid rgba(52,211,153,.4);border-radius:20px;padding:2px 10px;margin:2px 4px;font-size:12px">${esc(w)} <a href="javascript:void 0" class="kw-x" data-kind="${kind}" data-w="${esc(w)}" style="color:#fb7185">✕</a></span>`).join("")
    : empty;
  box.querySelectorAll(".kw-x").forEach(x => x.onclick = () => toggleKwWord(x.dataset.kind, x.dataset.w));
}

/* 点词 = "我要这个词": 非人工态下先以**当前生效词**为基础切到人工清单, 再把这个词加入/移出。
   为什么要先铺基础: 人工清单是**整体替换**语义, 从空清单开始点一个词会把其余自动词全丢掉,
   既不是用户意图, 也会让门槛判定突然不达标。 */
function kwPickWord(kind, word) {
  const wasManual = currentKwMode() === "manual";
  if (!wasManual) {
    const base = kind === "core" ? ((kwData.core || {}).effective || [])
                                 : ((kwData.longtail || {}).effective || []);
    kwEdit[kind] = base.slice();
    const r = kwModeRadio("manual");
    if (r) r.checked = true;
  }
  const arr = kwEdit[kind] || (kwEdit[kind] = []);
  const i = arr.indexOf(word);
  if (i >= 0) arr.splice(i, 1); else arr.push(word);
  markKwDirty();
  renderKeywordPanel();
  if (!wasManual) {
    toast(`已切到「人工清单」并以${kind === "core" ? "自动核心词" : "自动长尾词"}为基础`
      + `${i >= 0 ? "，移除了" : "，加入了"}「${word}」(记得点右上【保存并应用】)`, "ok");
  }
}

function toggleKwWord(kind, word) {
  if (!kwEdit[kind]) kwEdit[kind] = [];
  const i = kwEdit[kind].indexOf(word);
  if (i >= 0) kwEdit[kind].splice(i, 1); else kwEdit[kind].push(word);
  markKwDirty();
  renderKeywordPanel();
}
function addKwWords(kind) {
  const el = $({ core: "kwCoreAdd", longtail: "kwTailAdd", inject: "kwInjectAdd" }[kind]);
  if (!el || !el.value.trim()) return;
  const add = el.value.split(/[,，\n]/).map(s => s.trim().toLowerCase()).filter(Boolean);
  if (!kwEdit[kind]) kwEdit[kind] = [];
  add.forEach(w => { if (!kwEdit[kind].includes(w)) kwEdit[kind].push(w); });
  el.value = "";
  markKwDirty();
  renderKeywordPanel();
}
function fillKwFromAuto(kind) {
  const src = kind === "core" ? ((kwData.core || {}).effective || []) : ((kwData.longtail || {}).effective || []);
  if (!src.length) { toast("没有可用的自动词(请先跑步骤 4/5/7)", "err"); return; }
  kwEdit[kind] = src.slice();
  markKwDirty();
  renderKeywordPanel();
  toast("已把自动词填入人工清单, 现在可以逐条删除不需要的(记得保存并应用)", "ok");
}
function clearKwEdit(kind) {
  kwEdit[kind] = [];
  markKwDirty();
  renderKeywordPanel();
}

async function saveKwMode() {
  const mode = currentKwMode();
  // 仅注入词是独立附加清单(与三态无关) -> 无论自动/人工都一并保存
  const saveInject = async () => {
    const d = await api("/api/keyword-lists/save", { method: "POST",
      body: { config_file: selFile, list: "inject",
              mode: kwEdit.inject.length ? "manual" : "auto", words: kwEdit.inject } });
    if (!d.ok) throw new Error(d.error || "仅注入词保存失败");
    return (d.saved || []).length;
  };
  try {
    if (mode === "stop") {
      const d = await api("/api/keyword-lists/stop", { method: "POST",
        body: { config_file: selFile, stopped: true } });
      if (!d.ok) { toast(d.error || "操作失败", "err"); return; }
      toast("已设置人工停止: 下次跑步骤9 将直接中止(不抓取搜索数据)", "ok");
    } else if (mode === "manual") {
      if (!kwEdit.core.length) { toast("人工清单为空 —— 请先选词, 或切回「自动」", "err"); return; }
      const a = await api("/api/keyword-lists/save", { method: "POST",
        body: { config_file: selFile, list: "core", mode: "manual", words: kwEdit.core } });
      if (!a.ok) { toast(a.error || "核心词保存失败", "err"); return; }
      const b = await api("/api/keyword-lists/save", { method: "POST",
        body: { config_file: selFile, list: "longtail",
                mode: kwEdit.longtail.length ? "manual" : "auto", words: kwEdit.longtail } });
      if (!b.ok) { toast(b.error || "长尾词保存失败", "err"); return; }
      const inj = await saveInject();
      await api("/api/keyword-lists/stop", { method: "POST",
        body: { config_file: selFile, stopped: false } });
      toast(`已保存人工清单: 核心词 ${(a.saved || []).length} 个 / 长尾词 ${(b.saved || []).length} 个`
        + ` / 仅注入 ${inj} 个(下次跑步骤9/10 生效)`, "ok");
    } else {
      await api("/api/keyword-lists/save", { method: "POST",
        body: { config_file: selFile, list: "core", mode: "auto" } });
      await api("/api/keyword-lists/save", { method: "POST",
        body: { config_file: selFile, list: "longtail", mode: "auto" } });
      const inj = await saveInject();
      await api("/api/keyword-lists/stop", { method: "POST",
        body: { config_file: selFile, stopped: false } });
      toast("已切回自动口径(人工清单与停止标记均已清除"
        + (inj ? `; 仅注入词保留 ${inj} 个` : "") + ")", "ok");
    }
    await loadKeywordPanel();
  } catch (e) { toast("保存失败: " + (e.message || e), "err"); }
}

async function saveKwGate() {
  const el = $("kwGate");
  const v = parseInt(el && el.value, 10);
  if (!v || v < 1) { toast("门槛要 >= 1", "err"); return; }
  if (v === __kwGateSaved) { toast("门槛未改动", "err"); return; }
  try {
    const d = await api("/api/config/save", { method: "POST",
      body: { config_file: selFile, values: { GATE_MIN_QUALIFIED: v } } });
    if (d && d.ok === false) { toast(d.error || "保存失败", "err"); return; }
    toast(`开品门槛已保存为 ${v}`, "ok");
    await loadKeywordPanel();
  } catch (e) { toast("保存失败: " + (e.message || e), "err"); }
}

/* ============ 步骤16 属性集提取与确认面板 ============ */
let attrData = null;                 // 最近一次 GET /api/attr-spec 的结果
let __attrDirty = false;             // 面板是否有未保存修改
let __attrRefreshing = false;
const __attrWatch = {};              // cat|16 -> 上次观察状态(步骤16 完成自动刷新)

function markAttrDirty() { __attrDirty = true; syncAttrButtons(); }

function syncAttrButtons() {
  const rows = $("attrRows");
  const count = rows ? rows.querySelectorAll("tr.attr-row").length : 0;
  const status = attrData ? attrData.status : "";
  const saveBtn = $("btnSaveAttr"), confBtn = $("btnConfirmAttr");
  if (saveBtn) saveBtn.disabled = count === 0 || !__attrDirty;
  if (confBtn) confBtn.disabled = __attrDirty || count === 0 || status === "confirmed";
}

function attrRow(name, hint, idx) {
  return `<tr class="attr-row">
    <td class="attr-idx" style="color:var(--txt-2)">${idx}</td>
    <td><input class="attr-name" value="${esc(name)}" placeholder="属性名(中文)"></td>
    <td><input class="attr-hint" value="${esc(hint)}" placeholder="判定依据/典型取值/易混区分/未明确" style="width:100%"></td>
    <td><button class="attr-del btn-sm btn-ghost" title="删除该属性">删除</button></td>
  </tr>`;
}

function reindexAttrRows() {
  const rows = $("attrRows");
  if (!rows) return;
  rows.querySelectorAll("tr.attr-row").forEach((tr, i) => {
    const idx = tr.querySelector(".attr-idx");
    if (idx) idx.textContent = i + 1;
  });
}

function renderAttrRows(attrs) {
  const rows = $("attrRows");
  if (!rows) return;
  if (!attrs.length) {
    rows.innerHTML = `<tr><td colspan="4" class="empty">暂无属性 — 请先运行步骤16, 或在上方手动添加</td></tr>`;
    return;
  }
  rows.innerHTML = attrs.map((a, i) => attrRow(a.name, a.hint, i + 1)).join("");
}

function updateAttrStatus() {
  const hint = $("attrStatusHint"), summary = $("attrSummary");
  if (!attrData || !attrData.exists) {
    if (hint) hint.textContent = "尚未生成属性集规格";
    if (summary) summary.textContent = "请先运行步骤16(或手动添加属性), 再在此核对/确认。";
    return;
  }
  const n = (attrData.attributes || []).length;
  const confirmed = attrData.status === "confirmed";
  if (hint) hint.textContent = confirmed ? `已确认 · ${n} 个属性` : `待人工确认 · ${n} 个属性`;
  if (summary) summary.textContent = confirmed
    ? "已确认: 步骤17(属性标注)将复用该属性集。若再改动并保存, 状态会回落到待确认。"
    : "请核对/增删/改名/改说明后点「保存属性集」, 再点「确认属性集」以进入步骤17。";
}

async function loadAttrSpec() {
  try {
    const d = await api(`/api/attr-spec?file=${encodeURIComponent(selFile)}`);
    attrData = d;
    renderAttrRows(d.attributes || []);
    updateAttrStatus();
    __attrDirty = false;
    syncAttrButtons();
  } catch (e) {
    attrData = null;
    const rows = $("attrRows");
    if (rows) rows.innerHTML = `<tr><td colspan="4" class="empty">加载失败: ${esc((e && e.message) || e)}</td></tr>`;
    const hint = $("attrStatusHint");
    if (hint) hint.textContent = "属性集规格读取失败";
    syncAttrButtons();
  }
}

function collectAttrRows() {
  const out = [];
  const rows = $("attrRows");
  if (rows) rows.querySelectorAll("tr.attr-row").forEach(tr => {
    const name = tr.querySelector(".attr-name");
    const hint = tr.querySelector(".attr-hint");
    if (!name) return;
    out.push({ name: name.value.trim(), hint: hint ? hint.value.trim() : "" });
  });
  return out;
}

function addAttrRow() {
  const nameInput = $("attrNewName"), hintInput = $("attrNewHint");
  const name = (nameInput && nameInput.value.trim()) || "";
  if (!name) { toast("请先填写属性名", "err"); if (nameInput) nameInput.focus(); return; }
  const hint = (hintInput && hintInput.value.trim()) || "";
  const rows = $("attrRows");
  if (rows && rows.querySelector(".empty")) rows.innerHTML = "";
  if (rows) rows.insertAdjacentHTML("beforeend", attrRow(name, hint, 0));
  if (nameInput) nameInput.value = "";
  if (hintInput) hintInput.value = "";
  reindexAttrRows();
  markAttrDirty();
}

function deleteAttrRow(btn) {
  const tr = btn && btn.closest("tr");
  if (tr) tr.remove();
  const rows = $("attrRows");
  if (rows && !rows.querySelector("tr.attr-row")) {
    rows.innerHTML = `<tr><td colspan="4" class="empty">暂无属性 — 请先运行步骤16, 或在上方手动添加</td></tr>`;
  }
  reindexAttrRows();
  markAttrDirty();
}

async function saveAttrSpec() {
  const attrs = collectAttrRows();
  if (!attrs.length) { toast("没有可保存的属性", "err"); return; }
  try {
    const d = await api("/api/attr-spec/save", { method: "POST", body: { config_file: selFile, attributes: attrs } });
    toast(`已保存属性集 ${d.count} 个属性(待人工确认)`, "ok");
    await loadAttrSpec();
  } catch (e) { toast("保存失败: " + (e.message || e), "err"); }
}

function confirmAttrSpec() {
  const attrs = collectAttrRows();
  if (!attrs.length) { toast("属性集为空, 无法确认", "err"); return; }
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<div class="box" style="max-width:520px">
    <h3>确认属性集</h3>
    <div style="margin:10px 0;line-height:1.8;color:var(--txt-2)">
      即将把步骤16 属性集规格标记为<b>已确认</b>(共 <b>${attrs.length}</b> 个属性)。<br>
      确认后步骤17(属性标注)将复用该属性集。<br>
      若之后再次改动并保存, 状态会回落到「待确认」, 需要重新确认。
    </div>
    <div class="btnbar">
      <button class="btn-sm btn-ghost" id="acCancel">取消</button>
      <button class="btn-ok shine" id="acOk">确认</button>
    </div></div>`;
  document.body.appendChild(ov);
  const close = () => { if (ov.parentNode) ov.parentNode.removeChild(ov); };
  ov.querySelector("#acCancel").onclick = close;
  ov.addEventListener("click", e => { if (e.target === ov) close(); });
  ov.querySelector("#acOk").onclick = async () => {
    close();
    try {
      const d = await api("/api/attr-spec/confirm", { method: "POST", body: { config_file: selFile } });
      toast(`属性集已确认(${d.count} 个属性)`, "ok");
      await loadAttrSpec();
    } catch (e) { toast("确认失败: " + (e.message || e), "err"); }
  };
}

/* ============ 步骤16 完成 → 自动刷新属性集面板 ============ */
function watchAttrRefresh(d) {
  const steps = (d && d.steps) || [];
  if (!steps.length) return;
  const base = d.cat || "?";
  let done16 = false;
  for (const s of steps) {
    if (s.number !== 16) continue;
    const key = base + "|16";
    const prev = __attrWatch[key];
    __attrWatch[key] = s.status;
    if (prev !== undefined && prev !== "DONE" && s.status === "DONE") done16 = true;
  }
  if (!done16) return;
  setTimeout(async () => {
    if (__attrRefreshing) return;
    if (__attrDirty) {
      toast("步骤16 已完成: 属性集面板有未保存修改, 已跳过自动刷新(保存后点「重新加载」即可)", "err");
      return;
    }
    __attrRefreshing = true;
    try {
      await loadAttrSpec();
      toast("步骤16 已完成: 属性集规格已自动刷新", "ok");
    } catch (e) { /* loadAttrSpec 内部已提示 */ }
    finally { __attrRefreshing = false; }
  }, 800);
}

/* ============ 步骤18 标准化词表面板 ============ */
let stdData = null, __stdDirty = false;

function markStdDirty() { __stdDirty = true; syncStdButtons(); }
function syncStdButtons() {
  const rows = $("stdRows");
  const n = rows ? rows.querySelectorAll(".std-row").length : 0;
  const btn = $("btnSaveStd");
  if (btn) btn.disabled = n === 0 || !__stdDirty;
}

function stdRow(it) {
  const uniq = (it.唯一值 || []).slice(0, 6).map(v => `${esc(v.值)}×${v.次数}`).join(" ");
  const map = it.归并 || [];
  const merged = map.filter(m => m.原值 && m.归入 && m.原值 !== m.归入);
  const mapText = merged.length
    ? merged.slice(0, 14).map(m => `${esc(m.原值)}→${esc(m.归入)}`).join("　") + (merged.length > 14 ? " …" : "")
    : "";
  const mapTitle = map.map(m => `${esc(m.原值)}→${esc(m.归入)}`).join("　");
  return `<div class="std-row" data-attr="${esc(it.属性)}" style="border-bottom:1px solid var(--line);padding:8px 0">
    <div style="display:flex;gap:10px;align-items:center">
      <div style="flex:0 0 150px;font-weight:600">${esc(it.属性)}</div>
      <div class="tiny" style="flex:1;color:var(--txt-2)" title="${esc((it.唯一值 || []).map(v => v.值).join('、'))}">${uniq}${(it.唯一值 || []).length > 6 ? " …" : ""}</div>
      <input class="std-vals" data-attr="${esc(it.属性)}" value="${esc((it.规范值 || []).join('、'))}" style="flex:1;min-width:180px" placeholder="规范值(用、分隔)">
      <div style="flex:0 0 150px;display:flex;align-items:center;gap:6px">
        <input type="range" class="std-gran" data-attr="${esc(it.属性)}" min="0" max="100" value="${it.粒度 || 50}" style="width:90px">
        <span class="std-gran-val tiny" style="color:var(--acc)">${it.粒度 || 50}</span>
      </div>
      <button class="std-regen btn-sm btn-ghost" data-attr="${esc(it.属性)}" data-gran="${it.粒度 || 50}">重新生成</button>
    </div>
    ${mapText ? `<div class="std-map tiny" style="color:var(--acc);padding-left:160px;margin-top:4px" title="${esc(mapTitle)}">归并: ${mapText}</div>` : ""}
  </div>`;
}

async function loadStdVocab() {
  try {
    const d = await api(`/api/std-vocab?file=${encodeURIComponent(selFile)}`);
    stdData = d;
    const box = $("stdRows");
    const rows = d.attributes || [];
    if (box) box.innerHTML = rows.length
      ? rows.map(stdRow).join("")
      : `<div class="empty">暂无唯一值统计 — 请先运行步骤17(标注)</div>`;
    const hint = $("stdStatusHint"), sum = $("stdSummary");
    if (hint) hint.textContent = rows.length ? `共 ${rows.length} 个属性待标准化` : "尚未生成(请先运行步骤17 标注)";
    if (sum) sum.textContent = rows.length
      ? "调粒度 → 重新生成 → 点「保存词表」写回 rules.json(规范值用、分隔)"
      : "";
    __stdDirty = false;
    syncStdButtons();
  } catch (e) {
    const box = $("stdRows");
    if (box) box.innerHTML = `<div class="empty">加载失败: ${esc((e && e.message) || e)}</div>`;
    syncStdButtons();
  }
}

async function regenerateStdAttr(attr, gran) {
  try {
    const d = await api("/api/std-vocab/regenerate", { method: "POST", body: { config_file: selFile, 属性: attr, 粒度: gran } });
    const row = document.querySelector(`.std-row[data-attr="${CSS.escape(attr)}"]`);
    const input = row ? row.querySelector(".std-vals") : null;
    if (input) input.value = (d.规范值 || []).join("、");
    // 更新归并映射显示(原值→归入)
    if (row) {
      const merged = (d.归并 || []).filter(m => m.原值 && m.归入 && m.原值 !== m.归入);
      const text = merged.slice(0, 14).map(m => `${esc(m.原值)}→${esc(m.归入)}`).join("　") + (merged.length > 14 ? " …" : "");
      let mapEl = row.querySelector(".std-map");
      if (text) {
        if (!mapEl) {
          mapEl = document.createElement("div");
          mapEl.className = "std-map tiny";
          mapEl.style.cssText = "color:var(--acc);padding-left:160px;margin-top:4px";
          row.appendChild(mapEl);
        }
        mapEl.textContent = "归并: " + text;
        mapEl.title = (d.归并 || []).map(m => `${esc(m.原值)}→${esc(m.归入)}`).join("　");
      } else if (mapEl) {
        mapEl.remove();
      }
    }
    markStdDirty();
    toast(`「${attr}」已重新生成 ${(d.规范值 || []).length} 个规范值`, "ok");
  } catch (e) { toast("重新生成失败: " + (e.message || e), "err"); }
}

async function saveStdVocab() {
  const rows = [...document.querySelectorAll(".std-row")];
  const attrs = rows.map(r => ({
    属性: r.querySelector(".std-vals").dataset.attr,
    规范值: r.querySelector(".std-vals").value.split(/[、,，]/).map(s => s.trim()).filter(Boolean),
    粒度: parseInt(r.querySelector(".std-gran").value, 10),
  }));
  if (!attrs.length) { toast("没有可保存的词表", "err"); return; }
  try {
    await api("/api/std-vocab/save", { method: "POST", body: { config_file: selFile, attributes: attrs } });
    toast(`已保存 ${attrs.length} 个属性的词表`, "ok");
    await loadStdVocab();
  } catch (e) { toast("保存失败: " + (e.message || e), "err"); }
}

/* 步骤17 完成 → 弹窗提示进入步骤18 标准化 */
const __stdWatch = {};
function watchStdRefresh(d) {
  const steps = (d && d.steps) || [];
  if (!steps.length) return;
  const base = d.cat || "?";
  for (const s of steps) {
    if (s.number !== 17) continue;
    const key = base + "|17";
    const prev = __stdWatch[key];
    __stdWatch[key] = s.status;
    if (prev !== undefined && prev !== "DONE" && s.status === "DONE") {
      setTimeout(() => {
        loadStdVocab();
        toast("步骤17 标注完成: 请到「步骤18 标准化」面板设定每个属性的合并粒度", "ok");
      }, 600);
    }
  }
}

/* SS_ACCOUNTS 账号列表编辑器(挂在"卖家精灵账号/浏览器"组内) */
function renderSsAccounts(items) {
  const group = [...document.querySelectorAll("#cfgForm .fgroup")].find(g =>
    g.querySelector(".fh") && g.querySelector(".fh").textContent.trim() === "卖家精灵账号/浏览器");
  if (!group) return;
  group.querySelectorAll(".ssacct").forEach(e => e.remove());
  const box = document.createElement("div");
  box.className = "ssacct";
  box.innerHTML = `
    <div class="fh">◈ SS_ACCOUNTS · 卖家精灵账号列表(主账号 + 备用)</div>
    <div class="hint" style="margin-bottom:9px">
      勾选"主账号"= is_main: true(随时可用); 未勾选= 备用账号(仅 SS_QUOTA_WINDOW_START(默认20:00)之后可导出, 配额耗尽自动换号)。
      ${window.__ssAbsent ? '<b style="color:#fbbf24">当前配置文件没有 SS_ACCOUNTS 键, 保存后将新增。</b>' : ""}
    </div>
    <div id="ssrows"></div>
    <div style="margin-top:4px"><button id="btnAddAcct" class="btn-sm" type="button">＋ 添加账号</button></div>`;
  group.appendChild(box);
  const rows = box.querySelector("#ssrows");
  const list = (items && items.length) ? items : [{}];
  list.forEach(it => rows.appendChild(ssRow(it)));
  box.querySelector("#btnAddAcct").onclick = () => rows.appendChild(ssRow({}));
}
function ssRow(it) {
  const r = document.createElement("div");
  r.className = "ssrow";
  r.innerHTML = `
    <input type="text" class="sa-acc" placeholder="卖家精灵账号(必填)" value="${esc(it.account || "")}">
    <span class="secwrap"><input type="password" class="sa-pwd" placeholder="密码" value="${esc(it.password || "")}">
      <span class="eye" title="显示/隐藏">👁</span></span>
    <input type="text" class="sa-note" placeholder="备注(如: 备用1)" value="${esc(it.note || "")}">
    <label style="display:inline-flex;align-items:center;gap:4px;color:var(--txt-2);font-size:12px;white-space:nowrap">
      <input type="checkbox" class="sa-main" ${it.is_main ? "checked" : ""} style="accent-color:#f59e0b"> 主账号</label>
    <button class="sa-del btn-sm" type="button" title="删除该账号">✕</button>`;
  r.querySelector(".sa-del").onclick = () => {
    const acc = (r.querySelector(".sa-acc").value || "").trim();
    const tip = acc ? `：${acc}` : "";
    if (!confirm(`确定删除该账号${tip}?\n\n删除仅影响本表单, 需点"保存配置"才会写入文件。`)) return;
    r.remove();
  };
  r.querySelector(".eye").onclick = () => {
    const i = r.querySelector(".sa-pwd");
    i.type = i.type === "password" ? "text" : "password";
  };
  return r;
}
function collectSsAccounts() {
  return [...document.querySelectorAll(".ssrow")].map(r => ({
    account: (r.querySelector(".sa-acc").value || "").trim(),
    password: r.querySelector(".sa-pwd").value,
    note: (r.querySelector(".sa-note").value || "").trim(),
    is_main: r.querySelector(".sa-main").checked
  })).filter(a => a.account || a.password || a.note);
}
function renderChips(v) {
  const items = [["CAT", v.CAT || "—"],["站点", v.SITE || "—"],["市场", v.MARKET || "—"],
    ["流量月", v.TRAFFIC_MONTH || "—"],["竞品月", v.COMPETITOR_MONTH || "—"],["趋势月", v.TREND_MONTH || "—"],
    ["模式", v.DRY_RUN ? "离线 dry-run" : "真实运行"]];
  $("cfgChips").innerHTML = items.map(([k, val]) =>
    `<span class="chip"><span class="k">${k}</span><b>${esc(String(val))}</b></span>`).join("");
}
function renderStatusEmptyHint() {
  /* 已按要求不展示监控目录路径 */
}

/* ============ 运行控制 ============ */
function parseOverrides() {
  // 2026-09: "本次覆盖参数"输入框已移除, 仅保留 dry-run 覆盖(模式 radio)
  const ov = {};
  const dr = document.querySelector('input[name="dryOverride"]:checked');
  if (dr && dr.value !== "inherit") ov.DRY_RUN = dr.value === "true";
  return ov;
}
async function startRun(mode) {
  if (busy || running) { toast("已有运行/维护任务进行中, 请先中断", "err"); return; }
  const v = cfgValues;
  const ov = parseOverrides();
  // 生效值 = 本次覆盖(如有)优先, 否则配置文件原值 —— 供空值检查与核对展示
  const effCat = String(ov.CAT !== undefined ? ov.CAT : v.CAT || "").trim();
  if (/(your-category|YYYYMM|template)/i.test(effCat)) {
    toast("当前是占位模板, 请先在 🏠 类目中心选择/新建真实类目", "err");
    return;
  }
  const effList = String(ov.LIST_URLS !== undefined ? ov.LIST_URLS : v.LIST_URLS || "");
  const urls = effList.split(/[;；]/).map(s => s.trim()).filter(Boolean);
  if (!effCat) { toast("本次运行的类目名为空, 无法运行 —— 请到 ⑤ 配置中心填写 CAT 或在本框覆盖 CAT", "err"); return; }
  if (!urls.length) { toast("本次运行的榜单 URL 为空, 无法运行 —— 请到 ⑤ 配置中心填写 LIST_URLS 或在本框覆盖", "err"); return; }
  const body = { config_file: selFile, mode, overrides: ov };
  if (mode === "from") body.from_step = parseInt($("stepSel").value, 10);
  if (mode === "step") body.step = parseInt($("stepSel").value, 10);
  const dry = ov.DRY_RUN !== undefined ? !!ov.DRY_RUN : !!v.DRY_RUN;
  // ---- 运行前核对: 类目名与榜单 URL 是否填充准确 ----
  const actionLabel = mode === "step" ? `仅运行步骤 ${body.step}` :
                      mode === "from" ? `从第 ${body.from_step} 步开始(续跑到最后)` :
                      "全流程运行 / 断点续跑";
  let msg = `开始前请核对以下信息:\n\n`;
  msg += `◆ 动作: ${actionLabel}\n`;
  if (mode === "step") {
    msg += `　【范围】仅运行步骤 ${body.step}, 结束后即停 —— 不会运行后续步骤(16/17/18/19 不会跑)\n`;
  } else if (mode === "from") {
    msg += `　【范围】从第 ${body.from_step} 步连续运行到流程末尾 —— 会依次执行后续步骤(含 16/17/18/19)\n`;
  } else {
    msg += `　【范围】断点续跑: 从第一个未完成步骤跑到流程末尾\n`;
  }
  msg += `◆ 配置: ${selFile}\n`;
  msg += `◆ 类目(CAT): ${effCat}\n`;
  msg += `◆ 榜单 URL 共 ${urls.length} 条:\n`;
  urls.forEach((u, i) => {
    msg += `   ${i + 1}. ${u}\n`;
  });
  msg += `◆ 模式: ${dry ? "离线 dry-run(模拟数据, 不产生费用/请求)" : "真实运行(真实抓取 + LLM 费用, 需有效登录态)"}\n\n`;
  msg += "请确认上面【类目名】与【榜单 URL】无误后再开始; 有误请先在 ⑤ 配置中心修改并保存。";
  if (!confirm(msg)) return;
  busy = true; disableCtl(true);
  try {
    const d = await api("/api/run", { method: "POST", body });
    lastLogId = -1; logMode = null; clearLogPanel();   // 2026-09-11 新运行: 清空面板, 防上一次运行的旧行残留
    toast("已启动: " + (d.run.label || mode), "ok");
    pollNow();
  } catch (e) { toast("启动失败: " + e.message, "err"); }
  busy = false; disableCtl(false);
}
async function stopRun() {
  if (!running) return;
  const ext = !!(lastStatus && lastStatus.cat_external);
  const tip = ext
    ? "\n\n注意: 这是外部/孤儿运行(非本服务启动, 多为看板重启后遗留) —— "
      + "将按 .run.lock 里记录的 pid 直接强杀它的进程树。"
    : "";
  if (!confirm("确定中断当前类目的流水线?\n中断后步骤状态会停留在 RUNNING, 下次续跑会从该步重跑。\n(只中断当前查看的类目; 并行运行的其它类目不受影响)" + tip)) return;
  try {
    // 多类目并行(2026-09-15): 带上当前类目配置 → 只中断该类目
    const d = await api("/api/stop", { method: "POST", body: { config_file: selFile } });
    toast(d.note || "已发送中断请求", "ok");
    // 中断后进程结束需要短暂时间: 连续快速轮询几次, 让 RUNNING→INTERRUPTED 尽快上卡
    [300, 900, 1800].forEach(ms => setTimeout(() => { pollNow(); refreshStepFilesIfOpen(); }, ms));
  } catch (e) { toast("中断失败: " + e.message, "err"); }
}
async function resetStep() {
  if (busy || running) { toast("请先中断当前运行", "err"); return; }
  const n = prompt("重置步骤 N 及全部下游(N-19), 输入步骤号(0-19):", "0");
  if (n === null) return;
  const num = parseInt(n, 10);
  if (isNaN(num) || num < 0 || num > 19) { toast("步骤号无效", "err"); return; }
  if (!confirm(`确定重置步骤 ${num}-19?\n将删除对应产物与状态(上游 0-${num - 1} 保留), 不可恢复!`)) return;
  busy = true; disableCtl(true);
  try {
    const d = await api("/api/reset-step", { method: "POST", body: { config_file: selFile, step: num } });
    toast(`重置完成 exit=${d.exit_code}`, d.exit_code === 0 ? "ok" : "err");
    pollNow(); refreshStepFilesIfOpen();
  } catch (e) { toast("重置失败: " + e.message, "err"); }
  busy = false; disableCtl(false);
}
async function archiveNow() {
  if (busy || running) { toast("请先中断当前运行", "err"); return; }
  if (!confirm("对当前类目执行手动完整快照归档(新建时间戳目录)?")) return;
  busy = true; disableCtl(true);
  try {
    const d = await api("/api/archive", { method: "POST", body: { config_file: selFile } });
    toast(`快照完成 exit=${d.exit_code}`, d.exit_code === 0 ? "ok" : "err");
    refreshStepFilesIfOpen();
  } catch (e) { toast("快照失败: " + e.message, "err"); }
  busy = false; disableCtl(false);
}
function disableCtl(dis) {
  ["btnRun","btnFrom","btnStep","btnResetStep","btnArchive","btnSaveCfg","cfgSel"].forEach(id => { const el = $(id); if (el) el.disabled = dis; });
}

/* ============ 步骤产物弹窗(点击②步骤卡片打开; 原"产物下载"列表已并入此弹窗) ============ */
function fmtSize(n) {
  if (n == null || n < 0) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
}
let sfStep = null;                     // 当前弹窗展示的步骤号(null=未打开)
let sfSnap = "";                       // 当前弹窗对应的归档快照名(读归档模式下载要用)
let sfArchive = false;                 // 服务端是否已支持"读归档"(旧服务进程不支持 → 自动退回旧口径)
function sfHref(name) {
  // 2026-09-16: 卡片改为"读归档快照" → 下载也走归档(带 source/snapshot, 服务端做包含性校验);
  // 旧服务进程(未重启)不支持该模式 → 不带这两个参数, 仍按"实时目录内文件"下载, 行为与旧版一致。
  const extra = sfArchive
    ? `&source=archive&snapshot=${encodeURIComponent(sfSnap)}` : "";
  return `/api/download?file=${encodeURIComponent(selFile)}&step=${sfStep}${extra}`
    + `&name=${encodeURIComponent(name)}`;
}
function sfRows(list) {
  return list.map(f => `<tr>
      <td><a class="dl" href="${sfHref(f.name)}" download>${esc(f.name)}</a></td>
      <td style="white-space:nowrap">${fmtSize(f.size)}</td>
      <td style="white-space:nowrap">${esc(f.mtime)}</td></tr>`).join("");
}
function sfTable(list) {
  return `<table class="f-tbl"><thead><tr><th>文件(点击下载)</th><th>大小</th><th>更新时间</th></tr></thead>
    <tbody>${sfRows(list)}</tbody></table>`;
}
function openStepFiles(n) {
  // 换步骤时先清掉上一个步骤的表格并显示"加载中"(2026-09-15): 否则在新响应到达前会短暂
  // 出现"标题是 STEP 16、内容还是 STEP 00 的文件"这种错觉(实测浏览器核对时看到过)。
  if (sfStep !== n) $("sfBox").innerHTML = `<div class="empty">加载中…</div>`;
  sfStep = n;
  const meta = STEPS[n] || {};
  $("sfTitle").textContent = `STEP ${pad(n)} · ${meta.t || "步骤" + n}`;
  $("sfDesc").textContent = meta.d || "";
  $("stepFilesOverlay").style.display = "flex";
  loadStepFiles();
}
function closeStepFiles() {
  $("stepFilesOverlay").style.display = "none";
  sfStep = null;
}
async function loadStepFiles() {
  if (sfStep == null) return;
  const want = sfStep;                                   // 本次请求对应的步骤号
  const box = $("sfBox");
  if (!box.querySelector("table")) box.innerHTML = `<div class="empty">加载中…</div>`;   // 同步骤刷新时保留旧表, 不闪
  try {
    const d = await api(`/api/step-files?file=${encodeURIComponent(selFile)}&step=${want}&source=archive`);
    // **乱序保护(2026-09-15 实测踩到)**: 连点两个步骤卡片时, 先发的请求可能后到 ——
    // 若直接渲染, 会出现"标题是 STEP 00, 内容却是 STEP 09 的文件"。只认当前步骤的响应。
    if (sfStep !== want) return;
    const files = d.files || [];
    sfArchive = (d.source === "archive");
    sfSnap = d.snapshot || "";
    // ---- 2026-09-16: 卡片数据源 = **该类目的归档快照**(data/archive/<CAT>-<ts>/<NN_步骤名>/)
    // 用户定调"改读取方式: 读取每个类目 archive 中每个步骤里的数据" → 不再有"未归属产物"分节。
    // (2026-09-16 用户追加: 末尾那行"另有 N 个类目根文件未被任何步骤声明…"的提示**不要了** ——
    //  卡片只呈现归档内容本身, 不掺其它说明。)
    if (sfArchive) {
      $("sfMeta").textContent = (d.cat ? `类目: ${d.cat}　` : "")
        + (d.snapshot
            ? `归档快照: ${d.snapshot_created || d.snapshot}（最近同步 ${d.snapshot_synced || "—"}，共 ${d.snapshot_count} 份）　`
            : "尚无归档快照　")
        + `共 ${files.length} 个文件`
        + (d.dir ? `　目录: ${d.dir}/` : "");
      if (!d.snapshot) {
        box.innerHTML = `<div class="empty">该类目还没有归档快照 —— 跑完一轮流水线, 或执行
          <code>python main.py archive --config …</code> 后才会出现</div>`;
        return;
      }
      if (!files.length) {
        box.innerHTML = `<div class="empty">该快照里还没有 <b>${esc(d.dir || "")}/</b> 的内容 ——
          这一步在本轮还没跑完(归档是每步跑完时同步的)</div>`;
        return;
      }
      box.innerHTML = `<div class="overline" style="color:var(--acc);margin:8px 0 4px">归档内容 · ${esc(d.dir || "")}/</div>`
        + sfTable(files);
      return;
    }
    const decl = files.filter(f => f.declared), extra = files.filter(f => !f.declared);
    $("sfMeta").textContent = (d.cat ? `类目: ${d.cat}　` : "")
      + `共 ${files.length} 个文件(产物 ${decl.length} · 过程 ${extra.length})`
      + (d.dir ? `　目录: ${d.dir}/` : "");
    if (!files.length) {
      box.innerHTML = `<div class="empty">该步骤暂无产物 — 跑完这一步后会自动出现在这里</div>`;
      return;
    }
    let html = "";
    if (decl.length) {
      html += `<div class="overline" style="color:var(--acc);margin:8px 0 4px">步骤产物 · Artifacts</div>` + sfTable(decl);
    }
    if (extra.length) {
      html += `<div class="overline" style="color:var(--dim);margin:16px 0 4px">过程文件 · Working Files</div>` + sfTable(extra);
    }
    const un = d.unclaimed || [];
    if (un.length) {
      html += `<div class="overline" style="color:var(--warn);margin:16px 0 4px">未归属产物 · 类目根其它文件</div>`
        + sfTable(un);
    }
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = `<div class="empty" style="color:#fbbf24">产物列表加载失败: ${esc(e.message)}
      <br><span style="color:var(--dim);font-size:11.5px">若服务未重启过: server.py 新增了 /api/step-files, 请重启服务(Ctrl+C 后重新 start_webui.bat)并刷新页面。</span></div>`;
  }
}
/* 弹窗开着时刷新(原 refreshFiles 的调用点都改走它; 关着就不发请求) */
function refreshStepFilesIfOpen() {
  if (sfStep != null) loadStepFiles();
}

/* ============ 运行横幅 ============ */
let bannerActive = false;
function renderRunBanner(data) {
  const run = data.run || {};
  const active = !!run.active;                          // 本服务启动的子进程在跑
  const here = !!data.cat_running;                      // 本类目**自己**有进程(含外部/孤儿进程)
  const external = !!data.cat_external;                 // 有进程但不是本服务启动的(看板重启后遗留)
  // 2026-09-16: 外部/孤儿运行也要点亮「中断」—— 服务端 stop() 会回退按 .run.lock 的 pid 强杀;
  // 否则按钮恒禁用, 用户以为没在跑(实测报障: "UK 的不是已经断了吗, 卡片上还显示 running")。
  running = active || here;
  if (bannerActive && !running) { bannerActive = false; refreshStepFilesIfOpen(); }  // 运行结束: 弹窗开着就刷新产物
  else bannerActive = running;
  setConn(true, running);
  disableCtl(busy);
  $("btnStop").disabled = !running;
  const b = $("runBanner");
  if (!run.pid && !running) { b.style.display = "none"; return; }
  b.style.display = "block";
  b.className = "runbanner " + (running ? "live" : "");
  // 多类目并行(2026-09-15): 提示还有哪些类目在一起跑(它们各自有独立的运行横幅/控制台,
  // 切换类目即可查看; 卖家精灵浏览器自动排队串行, 亚马逊抓取共用本机 IP)
  const others = (data.other_runs || []).filter(r => r && r.cat);
  const otherTxt = others.length
    ? `<span class="mini-stat" style="color:#93c5fd">⧉ 并行中: <b>${esc(others.map(r => r.cat).join("、"))}</b>`
      + ` <span style="color:var(--dim)">(共用本机 IP; 卖家精灵自动排队)</span></span>`
    : "";
  let codeTxt = "";
  if (!active && run.exit_code !== null && run.exit_code !== undefined) {
    const c = run.exit_code;
    const col = c === 0 ? "#86efac" : c === 1 ? "#fcd34d" : "#fda4af";
    codeTxt = `<span class="mini-stat">退出码 <b style="color:${col}">${c}</b> ${c === 0 ? "· 正常完成" : c === 1 ? "· 有警告" : "· 失败"}</span>`;
    if (run.killed) codeTxt = `<span class="mini-stat"><b style="color:#fcd34d">已手动中断</b></span>`;
  }
  // 外部/孤儿运行没有 RUNNER 记录 → pid 与开始时刻取服务端探测到的锁信息
  const liveStart = run.started_at || (external ? (data.cat_live_started || "") : "");
  const livePid = (run.pid ?? null) !== null ? run.pid : (external ? (data.cat_pid ?? "—") : "—");
  b.innerHTML = `
    <div class="r1">
      ${active ? '<span class="pill run" style="padding:4px 11px"><span class="pd"></span>RUNNING</span>'
               : external ? '<span class="pill run" style="padding:4px 11px"><span class="pd"></span>RUNNING · 外部进程</span>'
               : '<span class="pill ok" style="padding:4px 11px;opacity:.85"><span class="pd"></span>ENDED</span>'}
      <span class="t">${esc(run.label || (external ? "外部/孤儿运行(非本服务启动, 多为看板重启遗留)" : "流水线"))}</span>
      <span class="mini-stat">pid <b>${livePid}</b></span>
      <span class="mini-stat">开始 <b>${esc(liveStart || "—")}</b></span>
      ${run.ended_at ? `<span class="mini-stat">结束 <b>${esc(run.ended_at)}</b></span>` : ""}
      ${codeTxt}
      ${running ? `<span class="mini-stat" id="runClock">—</span>` : ""}
      ${otherTxt}
    </div>`;
  if (running && !metaTimer) metaTimer = setInterval(() => {
    const el = $("runClock");
    if (el && liveStart) {
      const start = new Date(liveStart.replace(/-/g, "/"));
      const s = Math.max(0, Math.floor((Date.now() - start) / 1000));
      el.innerHTML = `已运行 <b>${Math.floor(s / 60)}m ${pad(s % 60)}s</b>`;
    }
  }, 1000);
  if (!running && metaTimer) { clearInterval(metaTimer); metaTimer = null; }
  const hist = window.__hist || [];
  if (!active && !external && run.exit_code !== null && run.exit_code !== undefined && !hist.some(h => h.pid === run.pid)) {
    hist.unshift({pid: run.pid, label: run.label, started: run.started_at, ended: run.ended_at, code: run.exit_code});
    hist.length = Math.min(hist.length, 8);
    window.__hist = hist;
  }
  if (hist.length) $("histWrap").open = true;
  $("runHistory").innerHTML = hist.length ? hist.map(h =>
    `<span class="hist-line">${esc(h.label)} · ${esc(h.started)} → ${h.code === 0 ? '<b style="color:#86efac">ok</b>' : h.code === 2 ? '<b style="color:#fda4af">fail</b>' : '<b style="color:#fcd34d">warn</b>'}</span>`).join("") : "";
}

/* ============ 状态渲染(就地 diff, 避免整页闪烁) ============ */
function renderSteps(data) {
  const grid = $("stepGrid");
  const map = window.__stepEls || (window.__stepEls = {});
  if (!grid.childElementCount) { const io2 = null; }
  // 时间解析与秒差(供各步卡片/累计时长使用, 含 acc 累计段)
  const parseTs = s => {
    const m = /^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})$/.exec(s || "");
    return m ? new Date(m[1].replace(/-/g, "/") + " " + m[2] + ":" + m[3] + ":" + m[4]).getTime() : null;
  };
  const gapSec = (a, b) => {
    const pa = parseTs(a);
    if (pa == null) return null;
    const pb = b ? parseTs(b) : Date.now();
    if (pb == null) return null;
    const t = pb - pa;
    return t >= 0 ? Math.round(t / 1000) : null;
  };
  data.steps.forEach((s, i) => {
    const meta = STEPS[s.number];
    const reasonLabel = s.status === "FAILED" ? "失败原因"
                      : s.status === "INTERRUPTED" ? "中断说明"
                      : "说明";
    let el = map[s.number];
    if (!el) {
      el = document.createElement("div");
      el.className = "stp s-" + s.status + " pop";
      el.style.animationDelay = (i * 26) + "ms";
      el.title = (meta ? meta.d : "") + (s.reason ? "\n" + reasonLabel + ": " + s.reason : "");
      el.innerHTML = `<div class="top"><span class="num">STEP ${pad(s.number)}</span>
          <span class="ico" title="点击查看本步骤产物">📄</span>
          <span class="badge">${s.status}</span></div>
          <div class="tt">${esc(meta ? meta.t : "步骤" + s.number)}</div>
          <div class="top" style="margin-top:4px"><span class="tm">${esc(s.start || "")}</span>
            <span class="dur"></span></div>
          <div class="top usgrow" style="display:none;margin-top:2px"><span class="usg"
            style="font-size:11px;color:var(--txt-2);font-family:var(--mono)"></span></div>
          <div class="reason" style="display:none"></div>`;
      // 点击卡片 = 打开该步骤的产物弹窗(2026-09-15: 取代原"产物下载"列表)
      el.dataset.step = s.number;
      el.setAttribute("role", "button");
      el.tabIndex = 0;
      el.onclick = () => openStepFiles(Number(el.dataset.step));
      el.onkeydown = ev => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openStepFiles(Number(el.dataset.step)); }
      };
      grid.appendChild(el);
      map[s.number] = el;
    } else {
      const prev = el.dataset.st;
      const changed = prev && prev !== s.status;
      el.className = "stp s-" + s.status + (changed ? " flash" : "");
      if (changed) setTimeout(() => { if (el.parentNode) el.classList.remove("flash"); }, 1000);
      if (el.title && (!prev || changed)) {
        el.title = (meta ? meta.d : "") + (s.reason ? "\n" + reasonLabel + ": " + s.reason : "");
      }
      el.querySelector(".badge").textContent = s.status;
      const reason = el.querySelector(".reason");
      if ((s.status === "FAILED" || s.status === "INTERRUPTED") && s.reason) {
        reason.className = "reason" + (s.status === "INTERRUPTED" ? " note" : "");
        reason.style.display = "";
        if (reason.textContent !== s.reason) reason.textContent = s.reason;
      } else reason.style.display = "none";
    }
    el.dataset.st = s.status;
    // 计时口径:
    // - RUNNING 且本次运行尚未真正进入本步(登录预检/等待, 状态文件 start 还是旧残留)
    //   → 显示 0s/预检中, 归零等待; 一旦正式执行(start 刷新为本次时间)从 0 实时增长;
    // - RUNNING 正常 → 实时计; INTERRUPTED → 冻结于首次识别时刻; 其余按 开始→结束。
    const run = data.run || {};
    const pending = s.status === "RUNNING" && run.active && run.started_at && s.start && s.start < run.started_at;
    const tmEl = el.querySelector(".tm");
    const tmWant = pending ? "预检中, 即将进入…" : (s.status === "RUNNING" ? "running…" : (s.start || ""));
    if (tmEl.textContent !== tmWant) tmEl.textContent = tmWant;
    let durTxt;
    const accMark = s.acc ? " ⤾" : "";   // 含历史中断/失败段的重跑标记
    if (pending) durTxt = "0s";
    else if (s.status === "RUNNING") {
      const sec = gapSec(s.start, null);
      durTxt = fmtClock((sec != null ? sec : 0) + (s.acc || 0)) + accMark;
    } else if (s.status === "INTERRUPTED") {
      // 优先用服务端从日志推断的结束时刻(真实中断时间); 无则用前端识别时刻兜底
      let it = s.end;
      if (!it) {
        if (!el.dataset.ints && s.start) el.dataset.ints = new Date().toLocaleString("sv-SE").replace("T", " ").slice(0, 19);
        it = el.dataset.ints;
      }
      const sec = gapSec(s.start, it);
      durTxt = fmtClock((sec != null ? sec : 0) + (s.acc || 0)) + accMark;
    } else {
      const sec = (typeof s.dur_sec === "number") ? s.dur_sec : gapSec(s.start, s.end);
      durTxt = (sec != null ? fmtClock(sec) : "") + accMark;
    }
    const durEl = el.querySelector(".dur");
    if (durEl.textContent !== durTxt) durEl.textContent = durTxt;
    // 该步的 LLM 用量/费用(2026-09-28): 无 LLM 调用的步骤整行不显示(不刷噪音)。
    // 数据来自 /api/status 的 usage.steps[步骤号](服务端按账本指纹缓存, 不重复解析)。
    // 口径(2026-09-29): **该步累计**(含历史轮次/重跑/中断), 所以还没跑到的步骤也可能
    // 已经有数(上一次尝试留下的) —— tooltip 里明确写出尝试次数与最近一次时间。
    const us = ((data.usage && data.usage.steps) || {})[String(s.number)] || null;
    const usRow = el.querySelector(".usgrow"), usEl = el.querySelector(".usg");
    if (usRow && usEl) {
      const usTxt = usageLine(us);
      if (!usTxt) {
        if (usRow.style.display !== "none") usRow.style.display = "none";
      } else {
        if (usEl.textContent !== usTxt) usEl.textContent = usTxt;
        let tip = usageTip(us, "步骤 " + pad(s.number) + " LLM 用量累计(含历史轮次/重跑/中断)");
        const extra = [];
        if (Number(us.attempts || 0) > 1) extra.push(`本步共 ${us.attempts} 次尝试`);
        if (us.last_ts) extra.push(`最近一次 ${String(us.last_ts).replace("T", " ").slice(0, 19)}`);
        if (extra.length) tip += "\n" + extra.join(" · ");
        if (usRow.title !== tip) usRow.title = tip;
        if (usRow.style.display === "none") usRow.style.display = "";
      }
    }
  });
  // 进度环 + 计数(SKIP 不占统计卡, 中断卡常显)
  const c = data.counts || {};
  const done = c.DONE || 0, skip = c.SKIP || 0;
  const pct = data.progress || 0;
  animateGauge(pct, done + skip);
  $("progTxt").innerHTML = `已完成 <b style="color:var(--txt)">${done + skip}</b> / 20 步 · DONE ${done}` +
    (skip ? ` · SKIP ${skip}` : "") +
    (c.RUNNING ? ` · RUNNING ${c.RUNNING}` : "") +
    (c.FAILED ? ` · FAILED ${c.FAILED}` : "") +
    (c.INTERRUPTED ? ` · <b style="color:#fcd34d">INTERRUPTED ${c.INTERRUPTED}(续跑自动重跑)</b>` : "");
  const tiles = [["DONE", done, "c-DONE"], ["RUNNING", c.RUNNING || 0, "c-RUNNING"],
    ["FAILED", c.FAILED || 0, "c-FAILED"], ["INTERRUPTED", c.INTERRUPTED || 0, "c-INTERRUPTED"],
    ["PENDING", c.PENDING || 0, "c-PENDING"]];
  // FAILED/INTERRUPTED 为累计次数(重跑不清零); 其余为当前状态数
  const TILE_TIP = {
    DONE: "已完成步骤数(当前状态)",
    RUNNING: "运行中(当前状态)",
    FAILED: "累计失败次数: 按流水线日志统计(含历史与重跑), 不随重跑/重置清零",
    INTERRUPTED: "累计中断次数: 按流水线日志统计(Ctrl+C/页面中断/强杀, 含历史与重跑), 不随重跑/重置清零; 页面手动中断实时计入, 同一残留重复报告已去重",
    PENDING: "未开始(当前状态)",
  };
  $("countChips").innerHTML = tiles.map(([k, v, cls]) =>
    `<div class="stat-tile ${cls}" title="${TILE_TIP[k] || ""}"><div class="st-n">${v}</div><div class="st-l">${k}</div></div>`).join("");
  // 累计执行时长(2026-09-15 口径与流水线日志「本轮总耗时」统一):
  //   = 已完成/中断步骤的 [acc 历史段 + (end-start)] 之和 + 运行中步骤的实时递增。
  //   它统计"真正在执行"的时间 —— 被强杀后重跑的段会并入, 而进程段之间"没在跑"的
  //   空档不计入, 所以它**不等于**整轮墙钟(10:53 起跑、次日 02:28 结束那种跨段轮次,
  //   墙钟会明显大于该值)。同一行右侧另给"本段"与"墙钟"两个参照数, 避免误读。
  const du = data.durations || {total_sec: 0, covered: 0, excluded: 0};
  const run = data.run || {};
  let live = du.total_sec, liveExtra = 0;
  if (run.active) {
    data.steps.forEach(st => {
      if (st.status === "RUNNING" && st.start && (!run.started_at || st.start >= run.started_at)) {
        const el0 = gapSec(st.start, null);                    // 本轮已执行秒
        const p = (st.acc || 0) + (el0 != null ? el0 : 0);     // + 历史中断/失败段
        if (p > 0) liveExtra = Math.max(liveExtra, p);
      }
    });
    if (liveExtra <= 0 && run.started_at) {           // 登录预检等尚无步骤启动时: 从本轮开始计时
      const ms = parseTs(run.started_at);
      if (ms != null) liveExtra = Math.max(0, Math.floor((Date.now() - ms) / 1000));
    }
    live += liveExtra;
  }
  $("durVal").textContent = live > 0 ? fmtClock(live) : (du.excluded ? "—" : "0s");
  // LLM 用量/费用合计(2026-09-28): 与 ⏱ 并列一行; 无账本(或从没调过 LLM)时整块隐藏。
  // 口径(2026-09-29): **该类目累计**(历史轮次 + 本次, 含重跑与中断, 重置不清零);
  // tooltip 里另给"本轮"一行 + 每步/每通道明细。
  const usg = data.usage || {};
  const costLine = $("costLine");
  if (costLine) {
    if (usg.calls) {
      $("costVal").textContent = usageLine(usg);
      $("costTip").title = usageTip(usg, "该类目累计 LLM 用量(历史轮次 + 本次; 重跑/中断/重置都不清零)");
      costLine.style.display = "";
    } else {
      costLine.style.display = "none";
    }
  }
  const notes = [];
  if (du.covered) notes.push(`已计 ${du.covered}/20 步`);
  // 参照数(2026-09-15): 本段 = 现进程启动至今; 墙钟 = 步骤00开始至今(含中断空档)。
  // 有本段/墙钟两数, 就不会再把"累计执行"误读成"整轮跑了多久"。
  if (run.started_at) {
    const msSeg = parseTs(run.started_at);
    if (msSeg != null) {
      const segEnd = run.active ? Date.now() : (parseTs(run.ended_at) || Date.now());
      const segSec = Math.max(0, Math.floor((segEnd - msSeg) / 1000));
      if (segSec > 0) notes.push(`本段 ${fmtClock(segSec)}`);
      let firstStart = null;
      data.steps.forEach(st => { if (st.start && (firstStart == null || st.start < firstStart)) firstStart = st.start; });
      const msWall = firstStart ? parseTs(firstStart) : null;
      if (msWall != null && msWall <= segEnd) {
        const wallSec = Math.max(0, Math.floor((segEnd - msWall) / 1000));
        if (wallSec > segSec) notes.push(`整轮墙钟 ${fmtClock(wallSec)}`);
      }
    }
  }
  if (run.active) notes.push(liveExtra > 0 ? "运行中 · 实时计入" : "运行中");
  if (du.excluded) notes.push(`运行/无结束 ${du.excluded} 步未静态计入(运行中实时计)`);
  $("durNote").textContent = notes.join(" · ");
  $("durLine").title = "口径(2026-09-15 与流水线日志「本轮总耗时」统一): 每步从开始即计时, 中断/失败即暂停并把已执行时长保留在卡片; 重跑从暂停时刻的累计继续(跨轮累计, acc); 展示值 = 各步累计已执行时长之和 + 运行中步骤的实时计时; 页面中断(强杀)段以日志最后活动时间近似计入(无日志时按检测时刻); 进程已结束的残留 RUNNING 步骤按中断计入(acc+本轮段), 不因强杀漏计。注意: 该值统计「真正在执行」的时间, **不含**进程段之间没有在跑的空档 —— 跨多段跑完的一轮, 它小于整轮墙钟(故右侧另给 本段/整轮墙钟 作参照)。";
}
function animateGauge(pct, done) {
  const val = $("gaugeVal");
  const pEl = $("progPct");
  const from = shownPct < 0 ? 0 : shownPct;
  const t0 = performance.now(), dur = 700;
  const tick = t => {
    const k = Math.min(1, (t - t0) / dur);
    const e = 1 - Math.pow(1 - k, 3);
    const cur = from + (pct - from) * e;
    val.style.strokeDashoffset = String(GAUGE_C * (1 - cur / 100));
    pEl.textContent = Math.round(cur) + "%";
    if (k < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  shownPct = pct; shownDone = done;
}

/* ============ 告警 / 健康(变化时重绘) ============ */
function renderAlerts(data) {
  const box = $("alertsBox");
  const arr = data.alerts || [];
  const sig = arr.map(a => a.ts + a.level + a.text).join("|");
  if (sig === lastAlertSig) return;
  lastAlertSig = sig;
  if (!arr.length) { box.innerHTML = `<div class="empty">暂无告警记录 — 一切正常 ✦</div>`; return; }
  const html = arr.slice().reverse().map((a, i) => {
    const lv = a.level || "WARN";
    const cls = lv === "CRIT" ? "a-crit" : lv === "WARN" ? "a-warn" : "";
    return `<div class="alert ${cls}" style="animation-delay:${Math.min(i, 8) * 24}ms">
      <span class="ats">${esc(a.ts)}</span><span class="a-lv">${esc(lv)}</span>
      <span class="a-txt">${esc(a.text)}</span></div>`;
  }).join("");
  box.innerHTML = html;
  box.scrollTop = box.scrollHeight;
}
function renderHealth(data) {
  const box = $("healthBox");
  const h = data.health || {};
  const checks = h.checks || [];
  const sig = JSON.stringify(checks) + (h.exit_code ?? "");
  if (sig === lastHealthSig && box.childElementCount) return;
  lastHealthSig = sig;
  if (!checks.length) { box.innerHTML = `<div class="empty">暂无体检报告 — 步骤 19 运行后生成</div>`; return; }
  const ico = {OK:"✓", WARN:"!", CRIT:"✕"};
  box.innerHTML = checks.map((ch, i) =>
    `<div class="hitem lv-${esc(ch.level)}" style="animation:pop .5s var(--ease) both;animation-delay:${Math.min(i, 8) * 60}ms">
      <div class="ico">${ico[ch.level] || "•"}</div>
      <div><div class="hn">${esc(ch.name)}</div><div class="hm">${esc(ch.message)}</div></div>
    </div>`).join("");
}

/* ============ 控制台 ============ */
function renderConsole(data) {
  const runActive = !!(data.run && data.run.active);   // 本服务启动的子进程在跑(全局)
  const runCat = (data.run && data.run.cat) || "";
  const here = !!data.cat_running;                     // 本页显示的类目**自己**有没有进程在跑
  // 服务端 RING 实时流只属于"本服务启动的那个子进程", 因此只有"子进程在跑 且 看的正是它的类目"
  // 才用流; 其余一律显示**所看类目的日志文件尾部**(服务端按 file 返回 file_log, 每轮轮询刷新):
  //   ① 看别的类目: 那个类目的实时流不属于它 —— 不再把运行类目的输出混进来(2026-09-14 修复);
  //   ② CLI/外部进程在跑(本服务没有子进程流): 用文件尾部, 原实现这里显示的是空的"实时流"。
  const streaming = runActive && !!data.cat && (!runCat || data.cat === runCat);
  const otherRun = !streaming && runActive && !!runCat && !!data.cat && data.cat !== runCat;
  const mode = streaming ? "stream" : "file";
  const lv = $("lvFilter").value;
  const cons = $("console");
  const head = $("consoleHead");
  const filterFn = l => {
    if (lv === "ALL") return true;
    if (lv === "CRIT") return l.level === "CRIT" || l.level === "ERROR";
    return l.level === lv;
  };
  if (mode !== logMode) { cons.innerHTML = ""; logMode = mode; }
  head.className = "console-head " + (streaming ? "live" : "");
  $("logMode").textContent = streaming ? "子进程实时输出流"
    : otherRun ? "浏览类目日志尾部 · 运行中 " + runCat + " 的实时流不在此显示"
    : here ? "外部/CLI 进程运行中 · 显示该类目日志文件尾部(自动刷新)"
    : "最近日志尾部 · 当前无运行进程";
  const build = arr => arr.filter(filterFn).map(l => {
    const cls = l.level === "CRIT" || l.level === "ERROR" ? "crit"
              : l.level === "WARN" ? "warn" : "info";
    const evt = l.text.startsWith("▶") || l.text.startsWith("■") ? "evt" : "";
    return `<div class="ln ${cls}"><span class="ts">${esc(l.ts)}</span><span class="lv">[${esc(l.level)}]</span><span class="${evt}">${esc(l.text)}</span></div>`;
  }).join("");
  if (mode === "stream") {
    if (data.stream && data.stream.length) {
      const stick = $("follow").checked;
      const nearBottom = cons.scrollTop + cons.clientHeight >= cons.scrollHeight - 50;
      const frag = document.createElement("div");
      frag.innerHTML = build(data.stream).replace(/class="ln /g, 'class="ln in ');
      while (frag.firstChild) cons.appendChild(frag.firstChild);
      const cap = 2600;
      while (cons.childElementCount > cap) cons.removeChild(cons.firstChild);
      if (stick && nearBottom) cons.scrollTop = cons.scrollHeight;
    }
  } else {
    // file 模式(空闲回看): 尊重"自动滚动"checkbox(2026-09-04 修复:
    // 原无条件滚到底, 未勾选也滚)。重建前记住滚动位置——
    // 勾选 → 滚到底; 未勾选 → 恢复原位(不打扰往上翻看历史)。
    if (fullLogOn) { refreshFullLog(); return; }   // 完整日志(2026-09-08): 显示 log 文件全部行
    const stick = $("follow").checked;
    const prevTop = cons.scrollTop;
    const wasAtBottom = cons.scrollHeight > 0 && (prevTop + cons.clientHeight >= cons.scrollHeight - 50);
    cons.innerHTML = build(data.file_log || []);
    if (stick && (wasAtBottom || prevTop === 0)) {
      cons.scrollTop = cons.scrollHeight;
    } else if (!stick) {
      cons.scrollTop = Math.min(prevTop, cons.scrollHeight || 0);
    }
  }
  lastLogId = data.stream_watermark || lastLogId;
}

/* 完整日志(2026-09-08): 拉取并渲染 pipeline-progress.log 全部内容(3s 节流) */
async function refreshFullLog() {
  const now = Date.now();
  if (now - lastFullLogAt < 3000) return;
  lastFullLogAt = now;
  const cons = $("console");
  if (!cons) return;
  const stick = $("follow").checked;
  const prevTop = cons.scrollTop;
  const wasAtBottom = cons.scrollHeight > 0 && (prevTop + cons.clientHeight >= cons.scrollHeight - 50);
  try {
    const d = await api(`/api/log?file=${encodeURIComponent(selFile)}&full=1`);
    if (!fullLogOn) return;
    const lv = $("lvFilter").value;
    const rows = (d.file_log || []).filter(l => {
      if (lv === "ALL") return true;
      if (lv === "CRIT") return l.level === "CRIT" || l.level === "ERROR";
      return l.level === lv;
    });
    cons.innerHTML = rows.map(l => {
      const cls = l.level === "CRIT" || l.level === "ERROR" ? "crit"
                : l.level === "WARN" ? "warn" : "info";
      const evt = l.text.startsWith("▶") || l.text.startsWith("■") ? "evt" : "";
      return `<div class="ln ${cls}"><span class="ts">${esc(l.ts)}</span><span class="lv">[${esc(l.level)}]</span><span class="${evt}">${esc(l.text)}</span></div>`;
    }).join("");
    if (d.truncated) {
      cons.innerHTML += `<div class="ln warn"><span class="ts"></span><span class="lv">[WARN]</span><span>日志超长, 仅显示末尾 ${(d.file_log || []).length} 行</span></div>`;
    }
    if (stick && (wasAtBottom || prevTop === 0)) cons.scrollTop = cons.scrollHeight;
    else if (!stick) cons.scrollTop = Math.min(prevTop, cons.scrollHeight || 0);
  } catch (e) { /* 接口异常静默, 下次轮询再试 */ }
}

/* ============ 布局: 卡片重排/重编号 + 配置中心可折叠(2026-09) ============ */
function reorderCards() {
  const st = $("secState"), kw = $("secKwLists");
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
  if (attr && std && std !== attr.nextElementSibling) {
    try { attr.insertAdjacentElement("afterend", std); } catch (e) {}
  }
}
function renumberCards() {
  const map = {
    secRun: "01 · Run Control", secState: "02 · Pipeline State",
    secKwLists: "03 · Keyword Lists",
    secAttr: "04 · Step16 Attribute Set",
    secStd: "05 · Step18 Standardize", secLog: "06 · Live Log",
    secMon: "07 · Monitor & Alerts", secCfg: "08 · Config Studio"
  };
  Object.keys(map).forEach(id => {
    const card = $(id);
    if (!card) return;
    const ov = card.querySelector(".overline");
    if (ov) ov.textContent = map[id];
  });
}
const CFG_FOLD_KEY = "wui_cfg_fold";   // "1"=收起(默认) "0"=展开
function cfgFolded() { return localStorage.getItem(CFG_FOLD_KEY) !== "0"; }
function applyCfgFoldUI() {
  const body = $("cfgBody"), btn = $("btnCfgFold");
  const folded = cfgFolded();
  if (body) body.style.display = folded ? "none" : "";
  if (btn) btn.textContent = folded ? "展开配置 ⏵" : "收起配置 ▾";
}
function toggleCfgFold() { localStorage.setItem(CFG_FOLD_KEY, cfgFolded() ? "0" : "1"); applyCfgFoldUI(); }
function expandCfgIfFolded() { if (cfgFolded()) { localStorage.setItem(CFG_FOLD_KEY, "0"); applyCfgFoldUI(); } }

/* ============ 运行失败/中断 页内弹窗(步骤进入 FAILED/INTERRUPTED 时提醒) ============ */
// 覆盖场景: 步骤0 LIST_URLS 格式预检失败(阻断运行)、任意步骤失败、手动/异常中断等。
// 记录同步: 弹窗 + ③日志(流水线日志已含) + ②步骤状态卡(FAILED/INTERRUPTED 瓦片与原因)。
const __stWatch = {};   // cat|step -> 上次观察状态; 首次观察只建基线不弹(浏览历史类目不误弹)
let __stModal = null;

function showRunAlert(steps) {
  const items = steps.map(s => {
    const m = STEPS[s.number] || {t: "步骤" + s.number};
    const fail = s.status === "FAILED";
    const color = fail ? "#fb7185" : "#fcd34d";
    return `<div style="margin:10px 0;padding:10px 12px;border:1px solid ${fail ? "rgba(251,113,133,.35)" : "rgba(245,158,11,.4)"};border-radius:8px;background:${fail ? "rgba(251,113,133,.07)" : "rgba(245,158,11,.07)"}">
      <b style="color:${color}">${fail ? "⛔ 失败" : "⚠ 中断"} · 步骤 ${pad(s.number)} ${esc(m.t)}</b>
      <div style="margin-top:5px;font-size:12.5px;color:var(--txt-2);line-height:1.7;word-break:break-all">${esc(s.reason || "(无说明)")}</div>
    </div>`;
  }).join("");
  const title = steps.some(s => s.status === "FAILED") ? "⛔ 流水线运行失败(流程已中断)" : "⚠ 流水线运行被中断";
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<div class="box" style="max-width:640px">
    <h3>${title}</h3>
    <div style="max-height:46vh;overflow:auto">${items}</div>
    <div class="hint" style="margin-top:10px">详细原因见 ③日志与 ④监控告警; 该失败/中断已记录在下方 ②步骤状态卡(FAILED/INTERRUPTED)。<br>
    若是 LIST_URLS 格式问题: 请按控制台提示修改配置文件后重新运行。</div>
    <div class="btnbar">
      <button class="btn-sm btn-ghost" id="raGoState">查看步骤状态卡</button>
      <button class="btn-ok shine" id="raOk">知道了</button>
    </div></div>`;
  document.body.appendChild(ov);
  const close = () => { if (ov.parentNode) ov.parentNode.removeChild(ov); __stModal = null; };
  ov.querySelector("#raOk").onclick = close;
  ov.querySelector("#raGoState").onclick = () => { close(); const c = $("secState"); if (c) c.scrollIntoView({behavior: "smooth", block: "start"}); };
  ov.addEventListener("click", e => { if (e.target === ov) close(); });
  __stModal = ov;
}

function watchAbnormal(d) {
  const steps = (d && d.steps) || [];
  if (!steps.length) return;
  const base = d.cat || "?";
  const hit = [];
  for (const s of steps) {
    const st = s.status;
    const key = base + "|" + s.number;
    const prev = __stWatch[key];
    __stWatch[key] = st;
    if (prev === undefined || prev === st) continue;          // 首次基线 / 无变化
    const abn = st === "FAILED" || st === "INTERRUPTED";
    const wasAbn = prev === "FAILED" || prev === "INTERRUPTED";
    if (abn && !wasAbn) hit.push(s);
  }
  if (hit.length && !__stModal) {
    // 步骤11/12 "目标月缺失→待确认" 标记: 弹专门确认框征求用户同意后再决定是否改写配置(2026-09-30)
    const mf = hit.filter(s => /\[MONTH_FALLBACK_CONFIRM\]/.test(s.reason || ""));
    const normal = hit.filter(s => !/\[MONTH_FALLBACK_CONFIRM\]/.test(s.reason || ""));
    if (mf.length) showMonthFallbackDialog(mf[0]);
    if (normal.length) showRunAlert(normal);
  }
}

/* ============ 目标月缺失 → 弹窗征求同意(步骤11/12, 2026-09-30) ============ */
// 同意 → 调服务端接口把 TRAFFIC_MONTH/COMPETITOR_MONTH 改写为「最近30天」(不备份, 改动记日志);
// 不同意 → 配置保持原值, 由用户自行处理后再重跑。
function showMonthFallbackDialog(step) {
  const reason = step.reason || "";
  const mKey = /(TRAFFIC_MONTH|COMPETITOR_MONTH)/.exec(reason);
  const mM = /目标月 (\S+)/.exec(reason);
  const key = mKey ? mKey[1] : "";
  const month = mM ? mM[1] : "";
  const meta = STEPS[step.number] || { t: "步骤" + step.number };
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<div class="box" style="max-width:560px">
    <h3>⚠ 目标月份在下拉中不存在(步骤 ${pad(step.number)} ${esc(meta.t)})</h3>
    <p>卖家精灵下拉中找不到配置的目标月份 <b>${esc(month || "?")}</b>(配置键 ${esc(key || "?")}), 详情见 ③日志(已列出可用选项)。</p>
    <p style="color:var(--txt-2)">是否把 <b>${esc(key || "?")}</b> 改写为 <b>「最近30天」</b>?</p>
    <div class="hint">同意 → 自动改写配置(不备份, 改动会记入日志), 然后请你<u>重新运行本步骤</u>; 不同意 → 配置保持原值, 由你自行处理后再重跑。</div>
    <div class="btnbar">
      <button class="btn-sm btn-ghost" id="mfNo">不同意, 我自己处理</button>
      <button class="btn-ok shine" id="mfYes">同意: 改写为「最近30天」</button>
    </div></div>`;
  document.body.appendChild(ov);
  const close = () => { if (ov.parentNode) ov.parentNode.removeChild(ov); __stModal = null; };
  ov.querySelector("#mfNo").onclick = () => {
    close();
    toast(`已保留原值 ${month || "?"}(${key || ""}), 未修改配置 —— 请自行处理后再重跑`, "err");
  };
  ov.querySelector("#mfYes").onclick = async () => {
    close();
    try {
      // 一律改写"类目实时配置 data/<CAT>/config.yaml"(与 PIPELINE_CONFIG_SOURCE 同源),
      // 避免误写历史快照导致下次重跑仍缺月、弹窗反复出现(2026-09-30)
      const cat = (cfgValues && cfgValues.CAT) || "";
      const target = cat ? `data/${cat}/config.yaml` : selFile;
      const d = await api("/api/month-fallback-confirm", { method: "POST", body: { config_file: target, key, month } });
      toast(`已改写 ${d.key}=「最近30天」(${target}; 见日志, 不备份) —— 请重新运行本步骤`, "ok");
      try { await loadConfig(); } catch (e) {}
    } catch (e) { toast("改写失败: " + (e.message || e), "err"); }
  };
  ov.addEventListener("click", e => { if (e.target === ov) close(); });
  __stModal = ov;
}

/* ============ 步骤04/07 完成 → 自动刷新「关键词清单」面板 ============ */
// 步骤04 产出大词候选、步骤07 产出 FR-07 达标判定 —— 两者都是「关键词清单」面板的数据源,
// 变动后自动重载; 面板有未保存修改时不覆盖, 提示手动处理, 防丢用户改动。
// 2026-10-06: 原「步骤10 注入词」面板已删除 —— 步骤10 的判定参考词改为与关键词清单
// **同步**(核心大词列即注入词), 所以这个观察器现在只需刷新关键词清单面板。
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

/* ============ 轮询 ============ */
let polling = false;
/* 轮询: 只上报"当前所选配置" selFile —— 服务端 2026-09-14 起以 file 唯一决定类目
   (②运行状态/③实时日志/⑤产物/⑥关键词清单 全部跟随 selFile, 不再出现"控制面板是本类目、
   看板却是运行类目"的半页跟随; 原先还要靠一个浏览态标记才切得过来)。 */
async function pollNow() {
  if (polling) return;
  polling = true;
  try {
    const d = await api(`/api/status?file=${encodeURIComponent(selFile)}&after=${lastLogId}`);
    lastStatus = d;      // 「中断」确认框要据此判断"是否外部/孤儿运行"(2026-09-16)
    // 缓冲变了(换类目 / 从 CLI 类目切到本服务类目): 旧游标作废 → 清面板并**立刻重取一次**,
    // 否则 `since(旧的大数)` 对新缓冲恒为空, 面板会一直停在上一个类目的日志(2026-09-15 修)。
    // 键优先用服务端的 stream_key(精确到"哪个缓冲"); 旧服务进程没这个字段时退回 cat
    // (缓冲本来就是按类目选的) —— 这样**不重启看板**、只刷新页面也能修好。
    const key = d.stream_key || d.cat || "";
    if (key !== lastLogKey) {
      lastLogKey = key;
      lastLogId = -1;
      logMode = null;                      // 下一次渲染会先清空
      clearLogPanel();
      setTimeout(pollNow, 0);
      return;
    }
    renderRunBanner(d);
    renderSteps(d);
    watchAbnormal(d);
    watchKeywordRefresh(d);
    watchAttrRefresh(d);
    watchStdRefresh(d);
    renderAlerts(d);
    renderHealth(d);
    renderConsole(d);
  } catch (e) { setConn(false); }
  finally { polling = false; }
}
let loopTimer = null;
function pollLoop() {
  clearTimeout(loopTimer);
  const delay = running ? 1000 : 2200;
  // 立刻按当前类目重新取一次(不等下一次轮询): 切类目后日志面板要马上换成新类目的缓冲
  if (!polling) pollNow();
  loopTimer = setTimeout(async () => { await pollNow(); pollLoop(); }, delay);
}

/* ============ 配置表单 ============ */
function fieldInput(key, val) {
  const type = BOOL_KEYS.has(key) ? "bool" : NUM_KEYS.has(key) ? "num"
            : (key === "LIST_URLS" || key === "ATTR_KEYS" || key === "ATTR_DESC" || key === "COOKIE") ? "textarea" : "text";
  const label = M[key] || key;
  const secret = SECRET_KEYS.has(key);
  const hint = HINTS[key];
  const id = "f_" + key;
  let ctrl = "";
  if (type === "bool") {
    ctrl = `<select id="${id}" data-key="${key}">
      <option value="true" ${val ? "selected" : ""}>true · 开启</option>
      <option value="false" ${!val ? "selected" : ""}>false · 关闭</option></select>`;
  } else if (key === "SS_ACTIVE_ACCOUNT") {
    // 动态枚举: auto/main + SS_ACCOUNTS 账号(带 [主]/[备用N] 标注)
    const opts = (window.__activeOptions || []).slice();
    if (!opts.some(o => String(o.v) === String(val))) {
      opts.push({v: String(val), t: String(val) + " — (当前值, 自由填写)"});
    }
    ctrl = `<select id="${id}" data-key="${key}">` +
      opts.map(o => `<option value="${esc(o.v)}" ${String(val) === String(o.v) ? "selected" : ""}>${esc(o.t)}</option>`).join("") +
      `</select>`;
  } else if (key === "ATTR_MODE" || key === "REVIEW_POLICY" || key === "FALLBACK") {
    const opts = key === "ATTR_MODE" ? ["auto", "manual"]
               : key === "FALLBACK" ? ["auto", "off"]
               : ["keep_original", "follow_review", "drop_on_disagree"];
    // FALLBACK 是"唯一开关", 选项给中文解释(值仍是 auto/off, 写回配置原样)
    const optLabel = (o) => key !== "FALLBACK" ? o
      : (o === "auto" ? "auto · 直连失败自动用浏览器补抓(推荐)" : "off · 完全不用浏览器(失败如实记录)");
    ctrl = `<select id="${id}" data-key="${key}">` +
      opts.map(o => `<option value="${o}" ${String(val) === o ? "selected" : ""}>${esc(optLabel(o))}</option>`).join("") +
      `</select>`;
  } else if (type === "num") {
    ctrl = `<input type="number" id="${id}" data-key="${key}" value="${esc(val ?? "")}">`;
  } else if (type === "textarea") {
    // LIST_URLS/COOKIE/ATTR_DESC 长文本: 更大行数便于看全(2026-09)
    const rows = (key === "LIST_URLS" || key === "COOKIE" || key === "ATTR_DESC") ? 6
               : (key === "FILTER_REQUIREMENT" || key === "SCREEN_REQUIREMENT") ? 4 : 3;
    ctrl = `<textarea id="${id}" data-key="${key}" rows="${rows}" style="white-space:pre-wrap">${esc(val ?? "")}</textarea>`;
  } else {
    ctrl = `<div class="secwrap"><input type="${secret ? "password" : "text"}" id="${id}" data-key="${key}"
      value="${esc(val ?? "")}" ${secret ? "data-secret=1 autocomplete=off" : ""}>
      ${secret ? `<span class="eye" title="显示 / 隐藏">👁</span>` : ""}</div>`;
  }
  return `<div class="frow"><label for="${id}"><b>${esc(key)}</b> <span class="lk">${label !== key ? esc(label) : ""}</span></label>
    <div>${ctrl}${hint ? `<div class="hint">${esc(hint)}</div>` : ""}</div></div>`;
}
function renderConfigForm(values) {
  const form = $("cfgForm");
  const known = new Set();
  GROUPS.forEach(g => g.keys.forEach(k => known.add(k)));
  const extra = Object.keys(values).filter(k => !known.has(k) && !HIDDEN_KEYS.has(k) && !SPECIAL_KEYS.has(k));
  // SS_ACTIVE_ACCOUNT 下拉选项: auto/main + 主账号/备用账号
  const opts = [{v: "auto", t: "auto — 自动(主账号优先)"}, {v: "main", t: "main — 主账号"}];
  const seen = new Set(["auto", "main"]);
  const pushOpt = (v, t) => { if (v && !seen.has(v)) { seen.add(v); opts.push({v, t}); } };
  if (values.SS_ACCOUNT) pushOpt(values.SS_ACCOUNT, values.SS_ACCOUNT + " — 主账号(SS_ACCOUNT)");
  try {
    (JSON.parse(window.__ssJson || "[]") || []).forEach((a, i) => {
      const tag = a.is_main ? "[主] " : "[备用" + (i + 1) + "] ";
      pushOpt(a.account, a.account + " — " + tag + (a.note || ""));
    });
  } catch (e) { /* ignore */ }
  window.__activeOptions = opts;
  let html = "";
  GROUPS.forEach(g => {
    const present = g.keys.filter(k => k in values);
    if (!present.length && !g.keys.includes("SS_QUOTA_WINDOW_END")) return;
    html += `<div class="fgroup"><div class="fh">${esc(g.id)}</div>` + present.map(k => fieldInput(k, values[k])).join("");
    // SS_QUOTA_WINDOW_END 未配置时可启用(代码默认 08:00)
    if (g.keys.includes("SS_QUOTA_WINDOW_END") && !("SS_QUOTA_WINDOW_END" in values)) {
      html += `<div class="frow" id="endAddRow"><label><b>SS_QUOTA_WINDOW_END</b> <span class="lk">备用账号每日可用结束时刻</span></label>
        <div><label class="tiny" style="display:flex;gap:6px;align-items:center;color:var(--txt-2)">
          <input type="checkbox" id="endAddChk" style="accent-color:#7aa2ff"> 自定义结束时刻(不勾选=使用代码默认 08:00)</label>
          <input type="text" id="f_SS_QUOTA_WINDOW_END" data-key="SS_QUOTA_WINDOW_END" data-absent="1"
            value="08:00" placeholder="HH:MM, 如 07:30" style="display:none;margin-top:6px;max-width:170px"></div></div>`;
    }
    html += `</div>`;
  });
  if (extra.length) {
    // 未登记键的兜底分组(2026-09-11: 标题与其它分组统一为"主题名(限定语)"的风格,
    // 技术说明放进分组提示行; 未登记键没有专属说明时给通用提示, 避免用户对着英文键猜)
    html += `<div class="fgroup"><div class="fh">其他参数(高级配置)</div>`
      + `<div class="hint">以下参数在 config.yaml 中出现、未归入上方分组; 每个参数已附说明, 一般无需修改。修改后原样写回配置文件。</div>`
      + extra.map(k => fieldInput(k, values[k])).join("") + `</div>`;
  }
  form.innerHTML = html;
  const endChk = $("endAddChk");
  if (endChk) {
    endChk.onchange = () => {
      const inp = $("f_SS_QUOTA_WINDOW_END");
      if (inp) inp.style.display = endChk.checked ? "block" : "none";
    };
  }
  form.querySelectorAll(".eye").forEach(btn => {
    btn.onclick = () => {
      const input = btn.parentElement.querySelector("input");
      if (!input || input.dataset.secret !== "1") return;
      if (input.type === "password") { input.type = "text"; btn.textContent = "🙈"; }
      else { input.type = "password"; btn.textContent = "👁"; }
    };
  });
}
function collectConfigValues() {
  const out = {};
  $("cfgForm").querySelectorAll("[data-key]").forEach(el => {
    const key = el.dataset.key;
    // 专属编辑器维护的键(如 SS_ACCOUNTS)不从这里收集: 其值是列表, 通用控件读出来会是字符串(2026-09-11)
    if (SPECIAL_KEYS.has(key)) return;
    // 未配置键(如 SS_QUOTA_WINDOW_END)仅在用户勾选启用时才保存
    if (el.dataset.absent === "1") {
      const chk = $("endAddChk");
      if (!chk || !chk.checked) return;
    }
    if (BOOL_KEYS.has(key)) { out[key] = el.value === "true"; return; }
    if (NUM_KEYS.has(key)) {
      const raw = el.value.trim();
      out[key] = raw === "" ? "" : (/\./.test(raw) ? parseFloat(raw) : parseInt(raw, 10));
      return;
    }
    if (SECRET_KEYS.has(key) && el.type === "password") { out[key] = el.value; return; }
    out[key] = el.value;
  });
  return out;
}
async function saveConfig() {
  const values = collectConfigValues();
  const items = collectSsAccounts();
  const lists = {};
  if (!window.__ssAbsent || items.length) lists.SS_ACCOUNTS = items;
  const scalarChanged = Object.keys(values).filter(k => String(values[k]) !== String(cfgValues[k] ?? ""));
  const acctChanged = JSON.stringify(items) !== window.__ssJson;
  if (!scalarChanged.length && !acctChanged) { toast("没有字段被修改", "err"); return; }
  let msg = "保存到 " + selFile + "?\n将自动备份到 .webui/config-backups/ 后写入; 修改只对之后的运行生效。";
  if (lists.SS_ACCOUNTS !== undefined) {
    msg += "\n\nSS_ACCOUNTS 将保存 " + lists.SS_ACCOUNTS.length + " 个账号:";
    lists.SS_ACCOUNTS.forEach(a => msg += "\n  · " + (a.is_main ? "[主] " : "[备用] ") + (a.account || "(空)") + (a.note ? " (" + a.note + ")" : ""));
  }
  if (values.DRY_RUN !== undefined) msg += "\n\nDRY_RUN 将设为: " + values.DRY_RUN;
  if (!confirm(msg)) return;
  try {
    const d = await api("/api/config/save", { method: "POST", body: { config_file: selFile, values, lists } });
    toast("已保存 " + d.changed.length + " 项(已自动备份)", "ok");
    // 站点口径提示(2026-09-14): CAT 后缀 / SITE / MARKET 三者不一致时明确告警(不阻断保存)
    if (d.warnings && d.warnings.length) setTimeout(() => toast("⚠ " + d.warnings.join("\n⚠ "), "err"), 400);
    await loadConfig();      // 刷新表单/注入词/文件
    pollNow();               // 刷新②状态看板/③日志(保存 CAT 等后切到对应类目)
    loadHistory();           // 刷新历史类目列表
  } catch (e) {
    const emsg = String(e.message || "");
    // 防呆(2026-09-13): CAT 与所在目录名不一致 -> 询问是否自动迁移到 data/<新CAT>/ 后继续保存
    if (emsg.includes("[CAT_DIR]")) {
      const ask = emsg.replace("[CAT_DIR]", "").trim()
        + "\n\n是否自动迁移到 data/<新CAT>/config.yaml 后继续保存?(原目录留下的空目录需要你手动删除)";
      if (confirm(ask)) {
        try {
          const d = await api("/api/config/save", {
            method: "POST", body: { config_file: selFile, values, lists, allow_move: true } });
          toast("已迁移并保存: " + (d.moved_to || selFile), "ok");
          __cfgListCache = [];
          await loadConfigs();
          if (d.moved_to) selFile = d.moved_to;
          await loadConfig();
          pollNow();
          loadHistory();
          return;
        } catch (e2) { toast("迁移保存失败: " + e2.message, "err"); return; }
      }
    }
    toast("保存失败: " + e.message, "err");
  }
}

/* ============ 启动 ============ */
document.addEventListener("DOMContentLoaded", () => { initReveal(); boot();
  // 步骤产物弹窗: 刷新按钮 / 关闭 / Esc / 点遮罩关闭(2026-09-15)
  $("sfRefresh").onclick = loadStepFiles;
  $("sfClose").onclick = closeStepFiles;
  $("stepFilesOverlay").onclick = ev => { if (ev.target === $("stepFilesOverlay")) closeStepFiles(); };
  document.addEventListener("keydown", ev => { if (ev.key === "Escape" && sfStep != null) closeStepFiles(); });
  // 类目中心「累计运行时长」静默跟随(2026-09-15): 停留页面时每 12s 对齐一次,
  // 页内自身会判"是否在类目中心"(运行台视图下直接返回, 不发请求)。
  setInterval(pollHubDur, 12000);
});
window.addEventListener("error", e => { if (e && e.message && !/ResizeObserver/i.test(e.message)) toast("页面错误: " + e.message, "err"); });
