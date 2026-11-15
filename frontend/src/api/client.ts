import type { CommandAccepted, CommandRequest, RunSnapshot, RunStatus, WorkflowList } from '../contracts/generated.ts';
import { ContractError, parseContract } from '../contracts/validate.ts';

export class WorkflowApiError extends Error {
  readonly code: string;
  readonly status: number;
  constructor(code: string, message: string, status: number) {
    super(message); this.name = 'WorkflowApiError'; this.code = code; this.status = status;
  }
}

const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function uuid(value: string): string {
  if (!uuidPattern.test(value)) throw new ContractError('需要有效的 UUID');
  return value;
}

export class WorkflowApi {
  private readonly transport: typeof fetch;
  private readonly timeoutMs: number;
  constructor(transport: typeof fetch = fetch, timeoutMs = 15000) { this.transport = transport; this.timeoutMs = timeoutMs; }

  private async request(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<unknown> {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (signal?.aborted) cancel();
    signal?.addEventListener('abort', cancel, { once: true });
    const timer = setTimeout(cancel, this.timeoutMs);
    try {
      const response = await this.transport(`/api/v2${path}`, { ...init, signal: controller.signal, credentials: 'same-origin' });
      if (!response.headers.get('content-type')?.includes('application/json')) throw new ContractError('任务服务未返回 JSON；请检查接口和代理配置');
      const body: unknown = await response.json();
      if (!response.ok) {
        const error = parseContract('ApiError', body);
        throw new WorkflowApiError(error.code, error.message, response.status);
      }
      const expected = init.method === 'POST' ? 202 : 200;
      if (response.status !== expected) throw new ContractError(`预期 HTTP ${expected}，收到 ${response.status}`);
      return body;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', cancel);
    }
  }

  async list(options: { status?: RunStatus; cursor?: string; limit?: number; signal?: AbortSignal } = {}): Promise<WorkflowList> {
    const params = new URLSearchParams();
    if (options.status) params.set('status', options.status);
    if (options.cursor) params.set('cursor', options.cursor);
    if (options.limit !== undefined) {
      if (!Number.isInteger(options.limit) || options.limit < 1 || options.limit > 100) throw new ContractError('分页大小需要在 1–100 之间');
      params.set('limit', String(options.limit));
    }
    return parseContract('WorkflowList', await this.request(`/workflows${params.size ? '?' + params : ''}`, {}, options.signal));
  }

  async getRun(runId: string, signal?: AbortSignal): Promise<RunSnapshot> {
    const run = parseContract('RunSnapshot', await this.request(`/workflows/${uuid(runId)}`, {}, signal));
    if (run.run_id.toLowerCase() !== runId.toLowerCase()) throw new ContractError('响应属于其他任务');
    return run;
  }

  async command(runId: string, request: CommandRequest, idempotencyKey: string, signal?: AbortSignal): Promise<CommandAccepted> {
    parseContract('CommandRequest', request);
    const receipt = parseContract('CommandAccepted', await this.request(`/workflows/${uuid(runId)}/commands`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': uuid(idempotencyKey) }, body: JSON.stringify(request),
    }, signal));
    if (receipt.run_id.toLowerCase() !== runId.toLowerCase()) throw new ContractError('命令回执属于其他任务');
    return receipt;
  }
}
