import { lazy, Suspense } from 'react';
import { resolveDataMode } from './api/mode';

const DemoApp = lazy(() => import('./DemoApp'));

export default function App() {
  const mode = resolveDataMode(import.meta.env.VITE_DATA_MODE);
  if (mode === 'demo') return <Suspense fallback={<div className="empty-state">正在加载演示工作台…</div>}><DemoApp /></Suspense>;
  return <main><section className="empty-state"><h1>选品研究台 · 真实数据模式</h1><p>当前为 M0 契约版本，真实任务查询将在 M1 接入。</p><p>本页面未启动任务，也不会回退到演示数据。</p></section></main>;
}
