import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { ApiError, api, type TaskView } from '../api/client';
import { formatMoney, taskTitle } from '../pages/format';
import { activeDuration } from '../board/agents';
import { ctfApi } from './api';
import { acceptCursor, phaseLabels } from './view';
import type { CtfEvent } from './types';
import { useAction } from './useAction';
import Conversation from './Conversation';
import ChallengePanel from './ChallengePanel';
import Markdown from './Markdown';
import RecoveryPanel from './RecoveryPanel';
import CtfBoardCanvas from './CtfBoardCanvas';
import Icon from '../components/Icon';
import controls from '../styles/controls.module.css';
import styles from './CtfWorkbench.module.css';

const eventTypes = ['task.created', 'task.finished', 'task.stopped', 'task.failed', 'ctf.started', 'ctf.member.created', 'ctf.member.state_changed', 'ctf.member.removed', 'ctf.message.posted', 'ctf.message.delivered', 'ctf.turn.started', 'ctf.turn.finished', 'ctf.conclusion.requested', 'ctf.conclusion.finalized', 'ctf.challenge.created', 'ctf.challenge.updated', 'ctf.record.appended', 'ctf.artifact.registered', 'ctf.verification.updated', 'ctf.target.updated', 'ctf.platform.dispatched', 'ctf.cursor', 'agent.trace.recorded', 'tool_call.recorded'];

export default function CtfWorkbench({ taskId, initialTask }: { taskId: string; initialTask: TaskView }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [memberId, setMemberId] = useState<string | null>(null);
  const [challengeId, setChallengeId] = useState<string | null>(null);
  const [inspectorWidth, setInspectorWidth] = useState(520);
  const [closing, setClosing] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const workspace = useRef<HTMLDivElement>(null);
  const goalDialog = useRef<HTMLDialogElement>(null);
  const recoveryDialog = useRef<HTMLDialogElement>(null);
  const eventsDialog = useRef<HTMLDialogElement>(null);
  const moreMenu = useRef<HTMLDetailsElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const returnNode = useRef<string | null>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout>>();
  const drag = useRef<{ x: number; width: number } | null>(null);
  const [connection, setConnection] = useState('connecting');
  const [streamRevision, setStreamRevision] = useState(0);
  const [operation, setOperation] = useState<'start' | 'stop'>('start');
  const statusAction = useAction();
  const cursor = useRef(0);
  const state = useQuery({ queryKey: ['ctf', taskId, 'state'], queryFn: ({ signal }) => ctfApi.state(taskId, signal), refetchInterval: 3000 });
  const challenges = useQuery({ queryKey: ['ctf', taskId, 'challenges'], queryFn: ({ signal }) => ctfApi.challenges(taskId, signal), refetchInterval: 3000 });
  const eventHistory = useQuery({ queryKey: ['ctf', taskId, 'events'], queryFn: ({ signal }) => ctfApi.events(taskId, 0, signal), refetchInterval: 10000 });

  useEffect(() => {
    if (!eventHistory.data) return;
    cursor.current = eventHistory.data.reduce((value, event) => Math.max(value, event.version), cursor.current);
  }, [eventHistory.data]);

  useEffect(() => {
    if (state.error instanceof ApiError && state.error.status === 401) navigate('/login', { replace: true, state: { from: '/tasks/' + taskId } });
  }, [state.error, navigate, taskId]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let alive = true;
    setConnection('connecting');
    const source = new EventSource('/api/tasks/' + encodeURIComponent(taskId) + '/stream?since=' + cursor.current, { withCredentials: true });
    source.addEventListener('open', () => { if (alive) setConnection('open'); });
    source.addEventListener('error', () => { if (alive) setConnection('reconnecting'); });
    const receive = (raw: Event) => {
      if (!alive) return;
      try {
        const event = JSON.parse((raw as MessageEvent<string>).data) as CtfEvent;
        const next = acceptCursor(taskId, cursor.current, event);
        if (next === cursor.current) return;
        cursor.current = next;
        if (!timer) timer = setTimeout(() => { timer = undefined; if (alive) void queryClient.invalidateQueries({ queryKey: ['ctf', taskId] }); }, 250);
      } catch { setConnection('reconnecting'); }
    };
    for (const type of eventTypes) source.addEventListener(type, receive);
    return () => { alive = false; source.close(); if (timer) clearTimeout(timer); };
  }, [taskId, queryClient, streamRevision]);

  async function changeStatus(next: 'start' | 'stop') {
    setOperation(next);
    await statusAction.run(async (signal) => {
      await ctfApi.status(taskId, next, signal);
      await queryClient.invalidateQueries({ queryKey: ['ctf', taskId] });
      await queryClient.invalidateQueries({ queryKey: ['tasks'] });
    });
  }

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['ctf', taskId] });
    void queryClient.invalidateQueries({ queryKey: ['ctf', taskId, 'events'] });
    setStreamRevision((value) => value + 1);
  }

  const task = { ...initialTask, ...state.data?.task, cost_currency: state.data?.task.cost_currency ?? initialTask.cost_currency, runs: state.data?.task.runs ?? initialTask.runs };
  const phase = task.ctf_phase === 'closed' || task.status !== 'created' ? task.status : task.ctf_phase ?? task.status;
  const members = state.data?.members ?? [];
  const selected = memberId ? members.find((member) => member.id === memberId) : undefined;
  const items = challenges.data?.challenges ?? state.data?.challenges ?? [];
  const challenge = items.find((item) => item.id === challengeId);
  const records = state.data?.records ?? [];
  const selectedGraphId = challenge ? 'challenge:' + challenge.id : selected ? 'agent:' + selected.id : null;
  const inspectorOpen = Boolean(challenge || selected);
  const terminal = ['finished', 'stopped', 'failed'].includes(phase);
  const activeItems = items.filter((item) => !item.tombstone);
  const verified = activeItems.filter((item) => item.verification?.status === 'accepted' && ['platform', 'user'].includes(String(item.verification.source)) && !item.verification.test_only).length;
  const candidates = activeItems.filter((item) => item.verification?.status === 'candidate').length;
  const conclusionReason = String(task.ctf_conclusion?.end_reason ?? '');
  const failure = task.ctf_conclusion?.failure && typeof task.ctf_conclusion.failure === 'object' ? task.ctf_conclusion.failure as Record<string, unknown> : null;
  const conclusionLabels: Record<string, string> = { goal_claimed: 'Lead 声明完成', user_stop: '用户停止', budget_exhausted: '预算已用尽', system_failure: '系统执行失败', partial: '部分完成' };

  useEffect(() => () => { if (closeTimer.current) clearTimeout(closeTimer.current); }, []);
  useEffect(() => {
    if (!['provisioning', 'running', 'closing'].includes(phase)) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [phase]);
  useEffect(() => {
    if (selectedGraphId) workspace.current?.querySelector<HTMLElement>('aside h2')?.focus({ preventScroll: true });
  }, [selectedGraphId]);

  function select(kind: 'agent' | 'challenge', id: string) {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    if (!inspectorOpen) { returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; returnNode.current = `${kind === 'agent' ? 'agent' : 'challenge'}:${id}`; }
    setClosing(false); setMemberId(kind === 'agent' ? id : null); setChallengeId(kind === 'challenge' ? id : null);
  }
  function closeInspector() {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    setClosing(true);
    const duration = Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--duration-fast')) || 0;
    closeTimer.current = setTimeout(() => {
      const active = document.activeElement;
      const restore = active === document.body || workspace.current?.querySelector('aside')?.contains(active) || active?.getAttribute('role') === 'separator';
      setMemberId(null); setChallengeId(null); setClosing(false);
      if (restore) requestAnimationFrame(() => { if (document.activeElement !== document.body && document.activeElement !== active) return; const node = returnNode.current ? workspace.current?.querySelector<HTMLElement>(`.react-flow__node[data-id="${CSS.escape(returnNode.current)}"] button`) : null; if (node) node.focus({ preventScroll: true }); else if (returnFocus.current?.isConnected) returnFocus.current.focus({ preventScroll: true }); else workspace.current?.querySelector<HTMLElement>('[role="region"]')?.focus({ preventScroll: true }); });
    }, duration);
  }
  function resize(value: number) {
    const max = Math.max(360, Math.min(880, (workspace.current?.clientWidth ?? window.innerWidth) - 360));
    setInspectorWidth(Math.round(Math.max(360, Math.min(max, value))));
  }
  function openMoreDialog(dialog: HTMLDialogElement | null) {
    if (moreMenu.current) { moreMenu.current.open = false; moreMenu.current.querySelector('summary')?.focus(); }
    dialog?.showModal();
  }

  return <div className={styles.page}>
    <header className={styles.header}>
      <Link to="/tasks" className={`${controls.button} ${controls.quiet} ${controls.iconButton}`} aria-label="返回任务"><Icon name="back" /></Link>
      <h1 title={taskTitle(task)}>{taskTitle(task)}</h1><code className={styles.taskId}>{taskId.slice(0, 8)}</code>
      <span className={`${controls.badge} ${phase === 'running' ? controls.badgeInfo : phase === 'failed' ? controls.badgeDanger : phase === 'closing' ? controls.badgeWarning : ''}`}>{phaseLabels[phase] ?? phase}</span>
      <div className={styles.taskMeta}><span>运行 {activeDuration(task.active_seconds ?? 0, task.active_since, now)}</span><span>{formatMoney(task.usage?.cost ?? 0, task.cost_currency)} / {formatMoney(task.budget?.max_cost ?? 0, task.cost_currency)}</span><span className={styles.live} data-connected={connection === 'open'}>{connection === 'open' ? '实时同步' : connection === 'connecting' ? '连接中' : '正在重连'}</span></div>
      <div className={styles.actions}>
        <button type="button" className={`${controls.button} ${controls.quiet} ${controls.compact}`} onClick={() => goalDialog.current?.showModal()}>目标与附件</button>
        {task.report_uri && <a className={`${controls.button} ${controls.compact}`} href={`/api/tasks/${encodeURIComponent(taskId)}/report`} target="_blank" rel="noopener noreferrer">报告<Icon name="external" /></a>}
        {phase === 'created' && <button type="button" className={`${controls.button} ${controls.primary} ${controls.compact}`} disabled={statusAction.busy} onClick={() => void changeStatus('start')}>{statusAction.busy && operation === 'start' ? '正在启动…' : '启动任务'}</button>}
        {['created', 'provisioning', 'running'].includes(phase) && <button type="button" className={`${controls.button} ${controls.danger} ${controls.compact}`} disabled={statusAction.busy} onClick={() => void changeStatus('stop')}>{statusAction.busy && operation === 'stop' ? '正在停止…' : '停止任务'}</button>}
        <details ref={moreMenu} className={styles.moreMenu} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false; }} onKeyDown={(event) => { if (event.key === 'Escape') { event.currentTarget.open = false; event.currentTarget.querySelector('summary')?.focus(); } }}><summary data-no-chevron className={`${controls.button} ${controls.quiet} ${controls.iconButton}`} aria-label="更多任务操作"><Icon name="more" /></summary><div className={styles.moreMenuPanel}><button type="button" onClick={refresh}><Icon name="refresh" />刷新</button><button type="button" onClick={() => openMoreDialog(eventsDialog.current)}>系统事件</button>{terminal && <button type="button" onClick={() => openMoreDialog(recoveryDialog.current)}>收尾与恢复</button>}</div></details>
      </div>
    </header>
    <div className={styles.runSummary} role="status"><span>{activeItems.length} 个题目 · {members.length} 个 Agent</span>{verified > 0 && <span className={styles.verified}>{verified} 已验证</span>}{candidates > 0 && <span className={styles.pending}>{candidates} 候选待验</span>}<span className={styles.endReason}>{failure ? String(failure.summary ?? '系统执行失败') : terminal ? conclusionLabels[conclusionReason] ?? conclusionReason : phase === 'closing' ? '正在保存结果' : ''}</span>{failure && <code title={String(failure.correlation_id ?? '')}>追踪 {String(failure.correlation_id ?? '').slice(0, 8)}</code>}</div>
    {statusAction.error && <p className={styles.error} role="alert">{statusAction.error}</p>}
    {state.error && <p className={styles.error} role="alert">{state.error.message} <button type="button" onClick={() => void state.refetch()}>重试</button></p>}
    <div ref={workspace} style={{ '--inspector-width': `${inspectorWidth}px` } as CSSProperties} className={`${styles.workspace} ${inspectorOpen ? styles.inspectorOpen : ''}`} onKeyDown={(event) => { if (event.key === 'Escape' && inspectorOpen) { event.stopPropagation(); closeInspector(); } }}>
      <main className={styles.canvasRegion} aria-label="CTF 团队协作画布"><header className={styles.canvasHeader}><h2>协作画布</h2><span>{activeItems.length} 个题目 · {members.length} 个 Agent</span></header>{challenges.error && <p className={styles.error} role="alert">{challenges.error.message} <button type="button" onClick={() => void challenges.refetch()}>重试</button></p>}<CtfBoardCanvas members={members} challenges={items} records={records} selected={selectedGraphId} phase={phase} onSelect={(next) => select(next.kind, next.id)} /></main>
      {inspectorOpen && <>
        <div className={styles.resizeHandle} role="separator" aria-label="调整详情面板宽度" aria-orientation="vertical" aria-valuemin={360} aria-valuemax={Math.max(360, Math.min(880, (workspace.current?.clientWidth ?? window.innerWidth) - 360))} aria-valuenow={inspectorWidth} tabIndex={0}
          onPointerDown={(event) => { if (event.button !== 0) return; event.preventDefault(); drag.current = { x: event.clientX, width: inspectorWidth }; event.currentTarget.setPointerCapture(event.pointerId); }}
          onPointerMove={(event) => { if (drag.current) resize(drag.current.width + drag.current.x - event.clientX); }} onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }} onLostPointerCapture={() => { drag.current = null; }} onDoubleClick={() => resize(520)}
          onKeyDown={(event) => { if (event.key === 'ArrowLeft') resize(inspectorWidth + 20); else if (event.key === 'ArrowRight') resize(inspectorWidth - 20); else if (event.key === 'Home') resize(360); else if (event.key === 'End') resize(880); else return; event.preventDefault(); }} />
        <aside className={`${styles.inspector} ${closing ? styles.inspectorClosing : ''}`} aria-label={challenge ? '题目详情' : `${selected?.display_name} 会话详情`}>
          {challenge ? <ChallengePanel key={taskId + ':' + challenge.id} taskId={taskId} challenge={challenge} members={members} allRecords={records} phase={phase} onBack={closeInspector} onDiscuss={(id) => select('agent', id)} /> : selected ? <Conversation key={taskId + ':' + selected.id} taskId={taskId} member={selected} members={members} phase={phase} onClose={closeInspector} /> : null}
        </aside>
      </>}
    </div>
    <dialog ref={goalDialog} className={styles.goalDialog} aria-label="任务目标与附件"><header className={styles.dialogHeader}><h2>任务目标与附件</h2><button type="button" className={`${controls.button} ${controls.quiet} ${controls.iconButton}`} onClick={() => goalDialog.current?.close()} aria-label="关闭任务目标"><Icon name="close" /></button></header><Markdown text={task.goal} />{(task.initial_attachments ?? []).map((file) => <a className={styles.attachmentLink} key={file.id} href={api.evidenceUrl(file.uri)} download><Icon name="file" />{file.filename}<Icon name="download" /></a>)}</dialog>
    <dialog ref={eventsDialog} className={styles.goalDialog} aria-label="系统事件"><header className={styles.dialogHeader}><h2>系统事件</h2><button type="button" className={`${controls.button} ${controls.quiet} ${controls.iconButton}`} onClick={() => eventsDialog.current?.close()} aria-label="关闭系统事件"><Icon name="close" /></button></header>{eventHistory.isLoading && <p role="status">正在读取事件…</p>}{eventHistory.error && <p className={styles.error} role="alert">{eventHistory.error.message} <button type="button" onClick={() => void eventHistory.refetch()}>重试</button></p>}{!eventHistory.isLoading && !eventHistory.error && !eventHistory.data?.length && <p className={styles.empty}>尚无事件记录。</p>}<ol className={styles.systemEvents}>{(eventHistory.data ?? []).slice(-100).reverse().map((event) => <li key={event.version}><code>v{event.version}</code><strong>{event.type}</strong><span>{event.actor}</span></li>)}</ol></dialog>
    <dialog ref={recoveryDialog} className={styles.recoveryDialog} aria-label="收尾与恢复"><RecoveryPanel taskId={taskId} task={task} challenges={items} onClose={() => recoveryDialog.current?.close()} /></dialog>
  </div>;
}
