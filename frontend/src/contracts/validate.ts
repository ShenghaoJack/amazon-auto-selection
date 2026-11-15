import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import schema from './schema.json' with { type: 'json' };
import { legalCommands } from './generated.ts';
import type { ContractMap, RunSnapshot } from './generated.ts';

const ajv = new Ajv2020({ allErrors: true, strict: true, coerceTypes: false, removeAdditional: false });
addFormats(ajv);
ajv.addSchema(schema);

export class ContractError extends Error {
  constructor(message: string) { super(message); this.name = 'ContractError'; }
}

function assertRun(run: RunSnapshot) {
  const ids = run.node_summary.map(node => node.node_id);
  if (new Set(ids).size !== ids.length || new Set(run.current_node_ids).size !== run.current_node_ids.length)
    throw new ContractError('节点 ID 重复');
  if (run.current_node_ids.some(id => !ids.includes(id))) throw new ContractError('当前节点不在快照中');
  const expectedIds = schema.$defs.NodeSnapshot.properties.node_id.enum;
  if (expectedIds.length !== ids.length || expectedIds.some(id => !ids.includes(id as RunSnapshot['current_node_ids'][number])))
    throw new ContractError('快照缺少完整业务节点');
  if (run.node_summary.some(node => run.current_node_ids.includes(node.node_id) && ['SUCCEEDED', 'SKIPPED'].includes(node.status)))
    throw new ContractError('当前节点已经结束');
  const allowed: readonly string[] = legalCommands[run.status];
  if (new Set(run.allowed_actions).size !== run.allowed_actions.length || run.allowed_actions.some(action => !allowed.includes(action)))
    throw new ContractError('允许操作与任务状态冲突');
  if (run.status === 'SUCCEEDED' && (run.current_node_ids.length > 0 || run.node_summary.some(n => !['SUCCEEDED', 'SKIPPED'].includes(n.status)) || !run.node_summary.some(n => n.node_id === 'quality' && n.status === 'SUCCEEDED')))
    throw new ContractError('成功状态缺少最终体检结果');
}

export function parseContract<K extends keyof ContractMap>(name: K, value: unknown): ContractMap[K] {
  const validate = ajv.getSchema(`urn:workflow:contracts#/$defs/${name}`);
  if (!validate || !validate(value)) throw new ContractError(`${name} 数据契约不匹配`);
  if (name === 'RunSnapshot') assertRun(value as RunSnapshot);
  if (name === 'WorkflowList') {
    const list = value as ContractMap['WorkflowList'];
    list.items.forEach(assertRun);
    if (new Set(list.items.map(run => run.run_id)).size !== list.items.length) throw new ContractError('列表中任务 ID 重复');
  }
  return value as ContractMap[K];
}
