import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Check, FileText, GitBranch, Save, ShieldCheck, X } from 'lucide-react';
import { getStageStatus, stages } from '../domain/workflow';
import type { Action, Task } from '../domain/workflow';
import { stageIcons } from './WorkflowCanvas';

export default function Inspector({ task, index, onClose, dispatch }: { task: Task; index: number; onClose: () => void; dispatch: (action: Action) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [tab, setTab] = useState<'overview' | 'tools' | 'evidence'>('overview');
  const [draft, setDraft] = useState(task.mapping);
  const [discard, setDiscard] = useState(false);
  useEffect(() => { dialog.current?.showModal(); return () => dialog.current?.close(); }, []);
  useEffect(() => { setDraft(task.mapping); }, [task.id, task.revision, task.mapping]);
  const stage = stages[index]; const Icon = stageIcons[index];
  const dirty = draft !== task.mapping;
  const editing = stage.id === 'attribute' && task.status === 'waiting_human';
  const close = () => { if (editing && dirty) setDiscard(true); else onClose(); };
  const status = getStageStatus(task, index);
  return <dialog className="inspector" ref={dialog} aria-labelledby="inspector-title" onCancel={e => { e.preventDefault(); close(); }}>
    <div className="inspector-header"><span className="muted">节点 {String(index + 1).padStart(2, '0')} / 08</span><button className="icon-button" aria-label="关闭节点详情" onClick={close}><X size={19} /></button></div>
    <div className={'inspector-identity ' + status}><span className="inspector-icon"><Icon size={25} /></span><h2 id="inspector-title">{stage.name}</h2><p>{stage.agent}</p></div>
    <p className="inspector-description">{stage.summary}</p>
    <div className="inspector-version"><GitBranch size={14} />{stage.steps}<span>{task.id}</span></div>
    <div className="detail-tabs" role="tablist" aria-label="节点详情">
      {(['overview', 'tools', 'evidence'] as const).map((name, i) => <button key={name} id={'tab-' + name} aria-controls={'panel-' + name} role="tab" aria-selected={tab === name} tabIndex={tab === name ? 0 : -1} onClick={() => setTab(name)} onKeyDown={e => { if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { const options = ['overview', 'tools', 'evidence'] as const; const next = options[(i + (e.key === 'ArrowRight' ? 1 : 2)) % 3]; setTab(next); document.getElementById('tab-' + next)?.focus(); } }}>{['概览', '工具调用', '产物证据'][i]}</button>)}
    </div>
    <div className="inspector-body" role="tabpanel" id={'panel-' + tab} aria-labelledby={'tab-' + tab}>
      {tab === 'overview' && <>
        <div className="io-block"><span>输入</span><p>{stage.input}</p><ArrowRight size={15} /><span>输出</span><p>{stage.output}</p></div>
        {editing ? <section className="review-editor"><div className="section-label"><ShieldCheck size={15} /><h3>标准化词表确认</h3><span>r{task.revision}</span></div><p>属性规格已确认。核对本版词表后，演示流程将进入下一阶段。</p><label htmlFor="raw-value">原始值</label><input id="raw-value" value="stainless steel" readOnly /><label htmlFor="standard-value">规范值</label><input id="standard-value" value={draft} onChange={e => setDraft(e.target.value)} maxLength={80} aria-describedby="review-help" /><p className="field-help" id="review-help">{dirty ? '有未保存修改，请先保存新版本。' : `已保存 r${task.revision}，可以确认。`}</p><button className="secondary-button full" disabled={!dirty || !draft.trim()} onClick={() => dispatch({ type: 'save', id: task.id, mapping: draft })}><Save size={15} />保存新版本</button><button className="primary-button full" disabled={dirty || !draft.trim()} onClick={() => dispatch({ type: 'confirm', id: task.id, revision: task.revision })}><Check size={16} />确认 r{task.revision} 并演示继续</button><small>确认后仍需检查演示执行许可和资源；不会调用真实步骤 18。</small></section> : <div className="detail-note"><ShieldCheck size={18} /><div><strong>{index < task.stage || task.status === 'completed' ? '本阶段已完成（示例）' : index === task.stage ? '当前阶段' : '等待上游阶段'}</strong><p>输入与输出通过阶段依赖关联。后端接入前，页面只展示示例状态。</p></div></div>}
        <h3 className="detail-subheading">最近记录</h3><ol className="detail-events">{task.events.slice(0, 5).map((item, i) => <li key={i}><strong>{item.label}</strong><p>{item.detail}</p></li>)}</ol>
      </>}
      {tab === 'tools' && <><p className="muted">业务能力映射；本阶段未发起真实调用。</p><div className="tool-list">{stage.tools.map((tool, i) => <div key={tool}><span>{String(i + 1).padStart(2, '0')}</span><strong>{tool}</strong><span className="subtle-tag">{tool.includes('规划') ? '规划' : '待接入'}</span></div>)}</div><div className="detail-note"><GitBranch size={18} /><p>后续由后端返回调用 ID、状态、耗时与输入输出引用；前端不会直接执行 Python 函数。</p></div></>}
      {tab === 'evidence' && <><p className="muted">以下为产物说明，未读取真实业务文件。</p>{stage.evidence.map(item => <article className="evidence-item" key={item.name}><FileText size={21} /><div><strong>{item.name}</strong><span className="subtle-tag">{item.kind}</span><p>{item.description}</p></div></article>)}</>}
    </div>
    {discard && <div className="discard-warning" role="alert"><p>词表有未保存修改，关闭会放弃本次编辑。</p><button className="secondary-button" onClick={() => setDiscard(false)}>继续编辑</button><button className="danger-button" onClick={onClose}>放弃修改并关闭</button></div>}
    <div className="inspector-footer"><span className="demo-dot" />前端交互演示 · 不触发采集或模型调用</div>
  </dialog>;
}
