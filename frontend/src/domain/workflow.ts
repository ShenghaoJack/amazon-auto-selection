// Frontend demo only. Production leases, admission and confirmations belong to the server.
export type TaskStatus = 'running' | 'waiting_human' | 'waiting_resource' | 'queued' | 'paused' | 'completed';
export type StageStatus = 'done' | 'running' | 'waiting' | 'blocked' | 'pending';
export type NodeId = import('../contracts/generated.ts').NodeSnapshot['node_id'];
export type Evidence = { name: string; description: string; kind: string };
export type Stage = { id: NodeId; name: string; agent: string; summary: string; steps: string; input: string; output: string; tools: string[]; evidence: Evidence[] };
export type Task = {
  id: string; name: string; market: string; period: string; status: TaskStatus;
  stage: number; resource: string; revision: number; approvedRevision: number | null;
  mapping: string; created: string; events: { label: string; detail: string }[];
};
export const MAX_RUNNING = 3;
export const statusText: Record<TaskStatus, string> = {
  running: '执行中', waiting_human: '待确认', waiting_resource: '资源等待', queued: '排队中', paused: '已暂停', completed: '已完成',
};
export const stages: Stage[] = [
  { id: 'collect', name: '采集与筛选', agent: 'Collection Agent', summary: '建立可追溯的类目样本', steps: '现有步骤 0–3', input: '类目、站点、榜单入口', output: '清洗后的商品样本', tools: ['榜单采集', '详情补全', '规则过滤'], evidence: [{ name: '类目样本清单', description: '保留父体、变体与来源关系；此处为示例元信息。', kind: '数据集' }] },
  { id: 'keyword', name: '关键词研究', agent: 'Keyword Agent', summary: '识别需求、趋势与准入条件', steps: '现有步骤 4–8', input: '商品样本、种子关键词', output: '关键词与趋势报告', tools: ['关键词挖掘', '趋势核验', '准入筛选'], evidence: [{ name: '双榜关键词报告', description: '关键词、搜索量与季节性需要保留各自的来源口径。', kind: '报告' }] },
  { id: 'market', name: '市场与竞品', agent: 'Market Agent', summary: '构建量额价与品牌竞争结构', steps: '现有步骤 9–15', input: '关键词报告、变体数据', output: '市场结构与竞品矩阵', tools: ['竞品检索', '变体解析', '销量拆分'], evidence: [{ name: '变体市场矩阵', description: '按变体粒度分析销量、销售额、价格与品牌。', kind: '数据集' }] },
  { id: 'attribute', name: '产品属性', agent: 'Attribute Agent', summary: '提取参数，并确认标准化词表', steps: '现有步骤 16–18 · 两处确认', input: '商品文本、属性规格', output: '已标准化的产品属性', tools: ['属性规格生成', '属性标注', '词表标准化'], evidence: [{ name: '属性规格', description: '步骤 16 后确认属性口径；本演示假定已完成规格确认。', kind: '规格' }, { name: '标准化词表', description: '当前停在步骤 17 → 18；保存版本后才可以确认。', kind: '待确认' }] },
  { id: 'voc', name: '外部用户声音', agent: '外部资料', summary: '使用已有的评论分析材料', steps: '流程外资料，不自动采集', input: '外部 VOC 报告', output: '用户声音参考', tools: ['外部报告'], evidence: [{ name: 'VOC 证据索引', description: '使用外部提供的评论来源与主题证据。', kind: '证据' }] },
  { id: 'opportunity', name: '产品机会', agent: 'Opportunity Agent', summary: '形成产品假设与报告草稿', steps: '规划新增能力', input: '市场矩阵、属性、VOC', output: '机会卡、验证计划与报告草稿', tools: ['需求缺口分析（规划）', '机会优先级（规划）', '报告草稿生成（规划）'], evidence: [{ name: '产品机会卡', description: '区分证据、推断与待验证假设，避免把建议视为事实。', kind: '规划' }] },
  { id: 'quality', name: '质量审核', agent: 'Quality Agent', summary: '核验完整性、口径与引用链路', steps: '步骤 20 + 规划审核', input: '分析结果、证据索引', output: '质量问题与交付结论', tools: ['产物体检', '证据一致性审核（规划）'], evidence: [{ name: '质量审核清单', description: '审核门槛与退回策略需要在后端接入阶段实现。', kind: '清单' }] },
  { id: 'report', name: '报告交付', agent: 'Delivery Agent', summary: '查看已审核报告与来源', steps: '规划交付查看（只读）', input: '已审核结果与版本', output: '综合报告、交付清单', tools: ['交付清单查询（规划）', '报告与证据下载（规划）'], evidence: [{ name: '综合报告', description: '最终体检后仅查看与下载；修改报告需要重新审核。当前为示例说明。', kind: '规划' }] },
];

export function createDemoTasks(): Task[] {
  const specs: [string, string, TaskStatus, number, string][] = [
    ['CAT-001', 'Cat Feeders', 'waiting_human', 3, 'browser-alpha'],
    ['DOG-002', 'Dog Dryer', 'running', 4, 'browser-alpha'],
    ['COF-003', 'Coffee Grinder', 'running', 2, 'browser-beta'],
    ['LGT-004', 'Desk Lamps', 'waiting_resource', 2, 'browser-alpha'],
    ['BRD-005', 'Bird Feeders', 'running', 0, 'browser-gamma'],
    ['WTR-006', 'Water Fountains', 'completed', 8, 'browser-delta'],
    ['PET-007', 'Pet Cameras', 'queued', 0, 'browser-epsilon'],
  ];
  return specs.map(([id, name, status, stage, resource], i) => ({
    id, name, status, stage, resource, market: 'US', period: '2026-10', revision: 7,
    approvedRevision: status === 'waiting_human' || stage < 4 ? null : 7,
    mapping: '不锈钢', created: `10:${String(10 + i * 8).padStart(2, '0')}`,
    events: [{ label: '示例快照已载入', detail: `${name} · ${statusText[status]}，所有记录均为模拟数据。` }],
  }));
}

export function getStageStatus(task: Task, index: number): StageStatus {
  if (task.status === 'completed' || index < task.stage) return 'done';
  if (index > task.stage) return 'pending';
  if (task.status === 'running') return 'running';
  if (task.status === 'waiting_human') return 'waiting';
  return 'blocked';
}

export function waitReason(task: Task, tasks: Task[]): string {
  if (task.status === 'waiting_human') return `等待标准化词表 r${task.revision} 确认；未占用执行许可。`;
  if (task.status === 'waiting_resource') {
    const holder = tasks.find(t => t.status === 'running' && t.resource === task.resource);
    return holder ? `${task.resource} 由 ${holder.name} 占用。` : '等待调度器重新检查资源。';
  }
  if (task.status === 'queued') return '等待执行许可；演示调度按列表顺序检查。';
  if (task.status === 'paused') return '已释放演示资源，可从当前阶段恢复。';
  if (task.status === 'completed') return '所有演示阶段已完成。';
  return `正在执行${stages[task.stage].name}，使用 ${task.resource}。`;
}

function event(task: Task, label: string, detail: string): Task {
  return { ...task, events: [{ label, detail }, ...task.events].slice(0, 20) };
}

function admit(task: Task, tasks: Task[]): Task {
  if (tasks.some(t => t.id !== task.id && t.status === 'running' && t.resource === task.resource))
    return { ...task, status: 'waiting_resource' };
  if (tasks.filter(t => t.id !== task.id && t.status === 'running').length >= MAX_RUNNING)
    return { ...task, status: 'queued' };
  return { ...task, status: 'running' };
}

export function drainQueue(tasks: Task[]): Task[] {
  let next = tasks.slice();
  for (const original of tasks) {
    if (original.status !== 'queued' && original.status !== 'waiting_resource') continue;
    const candidate = admit(original, next);
    next = next.map(t => t.id === original.id ? candidate.status === 'running' ? event(candidate, '调度恢复', '演示执行许可和资源均可用，进入当前阶段。') : candidate : t);
  }
  return next;
}

export type Action =
  | { type: 'pause' | 'resume' | 'advance'; id: string }
  | { type: 'save'; id: string; mapping: string }
  | { type: 'confirm'; id: string; revision: number };

export function transition(tasks: Task[], action: Action): Task[] {
  const current = tasks.find(t => t.id === action.id);
  if (!current) return tasks;
  let updated = current;
  switch (action.type) {
    case 'pause':
      if (!['running', 'queued', 'waiting_resource'].includes(current.status)) return tasks;
      updated = event({ ...current, status: 'paused' }, '已暂停演示任务', '当前阶段保留；演示资源已释放。');
      break;
    case 'resume':
      if (current.status !== 'paused') return tasks;
      updated = event(admit(current, tasks), '已请求恢复', '重新检查全局许可和资源占用。');
      break;
    case 'save':
      if (current.status !== 'waiting_human' || !action.mapping.trim() || current.mapping === action.mapping.trim()) return tasks;
      updated = event({ ...current, revision: current.revision + 1, mapping: action.mapping.trim(), approvedRevision: null }, '词表已保存新版本', '新版本必须重新确认后才能继续。');
      break;
    case 'confirm':
      if (current.status !== 'waiting_human' || current.stage !== 3 || current.revision !== action.revision) return tasks;
      updated = event(admit({ ...current, approvedRevision: current.revision, stage: 4 }, tasks), '标准化词表已确认', `已绑定 r${current.revision}，演示完成属性阶段并请求后续执行。真实步骤 18 尚未接入。`);
      break;
    case 'advance':
      if (current.status !== 'running') return tasks;
      if (current.stage === 3 && current.approvedRevision !== current.revision) {
        updated = event({ ...current, status: 'waiting_human' }, '需要人工确认', `步骤 17 已准备词表 r${current.revision}；等待确认。`);
      } else {
        const stage = current.stage + 1;
        updated = event({ ...current, stage, status: stage >= stages.length ? 'completed' : 'running' }, '演示阶段已推进', `已完成${stages[current.stage].name}。`);
      }
      break;
  }
  return drainQueue(tasks.map(t => t.id === action.id ? updated : t));
}
