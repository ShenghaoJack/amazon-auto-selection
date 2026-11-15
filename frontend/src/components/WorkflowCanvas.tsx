import { useEffect, useRef, useState } from 'react';
import { Archive, ArrowDown, Check, ClipboardCheck, Database, FileText, Layers3, List, Maximize2, MessageSquareText, Minus, Plus, Search, Sparkles } from 'lucide-react';
import type { Task } from '../domain/workflow';
import { getStageStatus, stages, statusText } from '../domain/workflow';

export const stageIcons = [Database, Search, Layers3, ClipboardCheck, MessageSquareText, Sparkles, Check, FileText];
const positions = [{ x: 28, y: 58 }, { x: 306, y: 58 }, { x: 584, y: 58 }, { x: 862, y: 58 }, { x: 862, y: 324 }, { x: 584, y: 324 }, { x: 306, y: 324 }, { x: 28, y: 324 }];
const labels = { done: '已完成', running: '执行中', waiting: '待确认', blocked: '等待恢复', pending: '未开始' };

export default function WorkflowCanvas({ task, onSelect }: { task: Task; onSelect: (index: number) => void }) {
  const viewport = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [list, setList] = useState(() => window.matchMedia('(max-width: 600px)').matches);
  useEffect(() => {
    const media = window.matchMedia('(max-width: 600px)');
    const change = () => setList(media.matches);
    media.addEventListener('change', change);
    return () => media.removeEventListener('change', change);
  }, []);
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const resize = () => setFit(Math.min(1.1, Math.max(0.65, (element.clientWidth - 32) / 1114)));
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => observer.disconnect();
  }, [list]);
  const scale = fit * zoom;
  const nodeLabel = (index: number) => getStageStatus(task, index) === 'blocked' ? statusText[task.status] : labels[getStageStatus(task, index)];
  return <section className="flow-section" aria-labelledby="flow-title">
    <div className="flow-toolbar">
      <div className="flow-heading"><span className="small-icon"><Layers3 size={16} /></span><h2 id="flow-title">Agent 工作流</h2><span className="subtle-tag">方案 v3</span></div>
      <div className="flow-actions">
        <button className={'text-button ' + (list ? 'is-active' : '')} onClick={() => setList(!list)} aria-pressed={list}><List size={15} />{list ? '画布视图' : '列表视图'}</button>
        {!list && <><span className="toolbar-divider" />
        <button className="icon-button" aria-label="缩小流程" disabled={zoom <= 0.8} onClick={() => setZoom(z => Math.max(0.8, z - 0.2))}><Minus size={16} /></button>
        <span className="zoom-value">{Math.round(scale * 100)}%</span>
        <button className="icon-button" aria-label="放大流程" disabled={zoom >= 1.8} onClick={() => setZoom(z => Math.min(1.8, z + 0.2))}><Plus size={16} /></button>
        <button className="icon-button" aria-label="适配流程画布" onClick={() => { setZoom(1); viewport.current?.scrollTo({ top: 0, left: 0 }); }}><Maximize2 size={16} /></button></>}
      </div>
    </div>
    {list ? <ol className="flow-list">{stages.map((stage, i) => <li key={stage.id}><button onClick={() => onSelect(i)}><span className={'list-index ' + getStageStatus(task, i)}>{String(i + 1).padStart(2, '0')}</span><span><strong>{stage.name}</strong><small>{stage.summary}</small></span><span className={'stage-state ' + getStageStatus(task, i)}>{nodeLabel(i)}</span></button></li>)}</ol> :
      <div ref={viewport} className="canvas-viewport" tabIndex={0} aria-label="流程画布，可滚动查看。也可切换到列表视图。">
        <div className="canvas-size" style={{ width: 1114 * scale, height: 530 * scale }}>
          <div className="canvas-content" style={{ transform: `scale(${scale})` }}>
            <div className="lane-caption top"><span>数据与需求研究</span><span>先建立样本，再形成产品认知</span></div>
            <div className="lane-caption bottom"><span>洞察与决策交付</span><span>从用户声音回到产品机会</span></div>
            <svg className="flow-edges" viewBox="0 0 1114 530" role="img" aria-label="流程方向：上排从左到右，进入下排后从右到左，依次经过 8 个阶段。">
              <defs>{['done', 'running', 'waiting', 'blocked', 'pending'].map(status => <marker key={status} id={'arrow-' + status} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 1 L 8 5 L 0 9" className={'arrow-fill ' + status} /></marker>)}</defs>
              {positions.slice(0, -1).map((point, i) => {
                const target = positions[i + 1];
                const path = i === 3 ? `M ${point.x + 112} ${point.y + 156} V ${target.y - 8}` : i < 3 ? `M ${point.x + 224} ${point.y + 78} H ${target.x - 8}` : `M ${point.x} ${point.y + 78} H ${target.x + 232}`;
                const status = getStageStatus(task, i);
                return <path key={i} d={path} className={'flow-edge ' + status} markerEnd={`url(#arrow-${status})`} />;
              })}
            </svg>
            <span className="handoff-label"><ArrowDown size={13} /> 研究结果进入洞察</span>
            {stages.map((stage, i) => {
              const status = getStageStatus(task, i); const Icon = stageIcons[i];
              return <button className={'agent-node ' + status} key={stage.id} style={{ left: positions[i].x, top: positions[i].y }} onClick={() => onSelect(i)} aria-label={`${i + 1}. ${stage.name}，${nodeLabel(i)}，查看节点详情`}>
                <span className="node-top"><span className="node-icon"><Icon size={18} /></span><span className="node-number">{String(i + 1).padStart(2, '0')}</span></span>
                <strong>{stage.name}</strong><span className="node-summary">{stage.summary}</span>
                <span className="node-bottom"><span className={'stage-state ' + status}>{status === 'done' ? <Check size={12} /> : <span className="state-dot" />}{nodeLabel(i)}</span><span>{status === 'waiting' ? `词表 r${task.revision}` : stage.steps.startsWith('规划') ? '规划节点' : '查看详情'}</span></span>
              </button>;
            })}
          </div>
        </div>
      </div>}
    <div className="canvas-footer"><span><Archive size={13} /> 点击节点查看输入、工具与产物</span><div className="legend"><span><i className="legend-dot done" />已完成</span><span><i className="legend-dot running" />执行中</span><span><i className="legend-dot waiting" />待确认</span><span><i className="legend-dot pending" />未开始</span></div></div>
  </section>;
}
