export type DataMode = 'demo' | 'live';

export function resolveDataMode(configured: string | undefined): DataMode {
  if (configured === undefined || configured === 'live') return 'live';
  if (configured === 'demo') return 'demo';
  throw new Error('VITE_DATA_MODE 必须显式设置为 demo 或 live');
}
