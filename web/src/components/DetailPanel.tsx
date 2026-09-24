import { useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import type { BoardState } from '../board/types';
import controls from '../styles/controls.module.css';
import styles from './DetailPanel.module.css';

type Props = {
  taskId: string;
  state: BoardState;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
};

const factKinds: Record<string, string> = { observation: '观察事实', inference: '推断事实', structure: '结构事实' };
const intentStatus: Record<string, string> = { open: '待认领', claimed: '已认领', closed: '已关闭' };
const agentStatus: Record<string, string> = { running: '运行中', concluding: '收尾中', finished: '已结束', failed: '失败' };

function text(value: unknown): string {
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  return '—';
}

function Meta({ label, value }: { label: string; value: unknown }) {
  return <div className={styles.meta}><dt>{label}</dt><dd>{text(value)}</dd></div>;
}

export default function DetailPanel({ taskId, state, selectedId, onSelect }: Props) {
  const heading = useRef<HTMLHeadingElement>(null);
  const panel = useRef<HTMLElement>(null);
  const isObject = Boolean(selectedId && (selectedId in state.facts || selectedId in state.intents));
  const query = useQuery({
    queryKey: ['board-object', taskId, selectedId],
    queryFn: () => api.getObject(taskId, selectedId!),
    enabled: isObject,
    staleTime: 5000,
  });

  useEffect(() => { if (selectedId && !query.isLoading) heading.current?.focus(); }, [selectedId, query.isLoading]);

  const fact = selectedId ? state.facts[selectedId] : undefined;
  const intent = selectedId ? state.intents[selectedId] : undefined;
  const agent = selectedId ? state.agents[selectedId] : undefined;
  const object = query.data?.object;
  const related = query.data?.related ?? {};
  const objectType = fact ? 'fact' : intent ? 'intent' : null;
  const currentStatus = fact?.status ?? intent?.status ?? text(object?.status);

  function close() {
    onSelect(null);
    requestAnimationFrame(() => panel.current?.focus());
  }

  return (
    <aside ref={panel} tabIndex={-1} className={styles.panel} aria-label="对象详情" onKeyDown={(event) => { if (event.key === 'Escape' && selectedId) close(); }}>
      <div className={styles.top}>
        <span className={styles.panelLabel}>详情</span>
        {selectedId && <button type="button" className={`${controls.button} ${controls.quiet} ${styles.close}`} onClick={close} aria-label="关闭详情">×</button>}
      </div>
      {!selectedId ? (
        <div className={styles.placeholder}>
          <div className={styles.placeholderGlyph} aria-hidden="true">◇</div>
          <h2>选择图上的节点</h2>
          <p>查看任务目标、事实、意图或 Agent 的详细信息。</p>
        </div>
      ) : selectedId === 'goal' ? (
        <div className={styles.body}>
          <div className={styles.titleBlock}><span className={styles.type}>任务目标</span><h2 ref={heading} tabIndex={-1}>{state.task?.goal ?? '任务目标'}</h2></div>
          {state.task?.domain_context && <section className={styles.section}><h3>领域背景</h3><p className={styles.prose}>{state.task.domain_context}</p></section>}
          <section className={styles.section}><h3>验收条件</h3><ul className={styles.acceptance}>{Object.values(state.acceptance).map((item) => <li key={item.id}><span className={`${controls.badge} ${item.status === 'met' ? controls.badgeSuccess : controls.badgeWarning}`}>{item.id} · {item.status === 'met' ? '已满足' : '未满足'}</span><p>{item.desc}</p>{item.reason && <small>裁定：{item.reason}</small>}{item.missing && <small>缺口：{item.missing}</small>}</li>)}</ul></section>
        </div>
      ) : agent ? (
        <div className={styles.body}>
          <div className={styles.titleBlock}><span className={styles.type}>Agent · {agent.taskType}</span><h2 ref={heading} tabIndex={-1}>{agent.id}</h2><span className={`${controls.badge} ${agent.status === 'failed' ? controls.badgeDanger : agent.status === 'running' ? controls.badgeInfo : ''}`}>{agentStatus[agent.status] ?? agent.status}</span></div>
          <section className={styles.section}><h3>运行情况</h3><dl className={styles.metaList}><Meta label="模型调用" value={agent.steps} /><Meta label="上下文 token" value={agent.contextTokens} /><Meta label="最后版本" value={agent.lastSeenVersion} /><Meta label="结束原因" value={agent.endReason} /></dl></section>
          {agent.intentId && <section className={styles.section}><h3>当前意图</h3><button type="button" className={styles.objectLink} onClick={() => onSelect(agent.intentId)}>{agent.intentId}<span aria-hidden="true"> →</span></button></section>}
        </div>
      ) : isObject ? (
        query.isLoading ? <div className={styles.loading} role="status" aria-label="正在加载对象详情"><span className={controls.skeleton} /><span className={controls.skeleton} /><span className={controls.skeleton} /></div> :
        query.isError ? <div className={styles.message} role="alert"><h2 ref={heading} tabIndex={-1}>详情加载失败</h2><p>{query.error instanceof ApiError ? query.error.message : '请稍后重试。'}</p><button type="button" className={controls.button} onClick={() => void query.refetch()}>重试</button>{query.error instanceof ApiError && query.error.status === 401 && <Link to="/login">重新登录</Link>}</div> :
        object && <div className={styles.body}>
          <div className={styles.titleBlock}><span className={styles.type}>{objectType === 'fact' ? factKinds[text(object.kind)] ?? '事实' : '意图'} · {selectedId}</span><h2 ref={heading} tabIndex={-1}>{text(object.statement)}</h2><span className={`${controls.badge} ${currentStatus === 'disputed' ? controls.badgeDanger : currentStatus === 'closed' ? controls.badgeSuccess : ''}`}>{objectType === 'fact' ? currentStatus === 'disputed' ? '有争议' : '已提出' : intentStatus[currentStatus] ?? currentStatus}</span></div>
          <section className={styles.section}><h3>基本信息</h3><dl className={styles.metaList}><Meta label="作者" value={object.author} /><Meta label="版本" value={object.version} />{objectType === 'fact' ? <><Meta label="来源" value={object.provenance === 'tool_backed' ? '工具支持' : '自述'} /><Meta label="依赖次数" value={fact?.reliedBy ?? object.relied_by} /></> : <><Meta label="持有者" value={intent ? intent.holder : object.holder} /><Meta label="尝试次数" value={intent?.attempts ?? object.attempts} /><Meta label="结果" value={intent ? intent.result : object.result} /></>}</dl></section>
          {objectType === 'intent' && <section className={styles.section}><h3>计划</h3><dl className={styles.metaList}><Meta label="预期" value={object.expected} /><Meta label="方法" value={object.method} /></dl></section>}
          {objectType === 'fact' && Array.isArray(object.evidence) && object.evidence.length > 0 && <section className={styles.section}><h3>证据摘要</h3><ul className={styles.evidence}>{object.evidence.map((raw, index) => { const item = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}; return <li key={index}><span className={styles.evidenceType}>{text(item.type)}</span><p>{text(item.summary)}</p>{typeof item.uri === 'string' && <small>对象：{item.uri}</small>}</li>; })}</ul></section>}
          <section className={styles.section}><h3>关联对象</h3>{Object.keys(related).length ? <ul className={styles.related}>{Object.entries(related).map(([id, item]) => <li key={id}><button type="button" className={styles.objectLink} onClick={() => onSelect(id)}><strong>{id}</strong><span>{text(item.statement)}</span></button></li>)}</ul> : <p className={styles.muted}>暂无一跳关联对象。</p>}</section>
        </div>
      ) : (
        <div className={styles.message}><h2 ref={heading} tabIndex={-1}>节点已不在当前视图中</h2><p>选择其他节点继续查看。</p></div>
      )}
    </aside>
  );
}
