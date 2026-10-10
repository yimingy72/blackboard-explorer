import { Alert, Button, Dropdown, Empty, Modal, Spin, Tag } from 'antd';
import InspectorLayout from '../components/InspectorLayout';
import TaskGoal from '../components/TaskGoal';
import {createAgentClientState,type AgentClientState} from '../components/agentClientState';
import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { ApiError, type TaskView } from '../api/client';
import { formatMoney, taskTitle } from '../pages/format';
import { activeDuration } from '../board/agents';
import { ctfApi } from './api';
import { acceptCursor, phaseLabels } from './view';
import type { CtfEvent } from './types';
import { useAction } from './useAction';
import Conversation from './Conversation';
import ChallengePanel from './ChallengePanel';
import RecoveryPanel from './RecoveryPanel';
import CtfBoardCanvas from './CtfBoardCanvas';
import Icon from '../components/Icon';
import styles from './CtfWorkbench.module.css';
const eventTypes = ['task.created', 'task.finished', 'task.stopped', 'task.failed', 'ctf.started', 'ctf.member.created', 'ctf.member.state_changed', 'ctf.member.removed', 'ctf.message.posted', 'ctf.message.delivered', 'ctf.turn.started', 'ctf.turn.finished', 'ctf.conclusion.requested', 'ctf.conclusion.finalized', 'ctf.challenge.created', 'ctf.challenge.updated', 'ctf.record.appended', 'ctf.artifact.registered', 'ctf.verification.updated', 'ctf.target.updated', 'ctf.platform.dispatched', 'ctf.cursor', 'agent.trace.recorded', 'tool_call.recorded'];
export default function CtfWorkbench({ taskId, initialTask }: {
    taskId: string;
    initialTask: TaskView;
}) {
    const navigate = useNavigate();
    const queryClient = useQueryClient();
    const [memberId, setMemberId] = useState<string | null>(null);
    const [challengeId, setChallengeId] = useState<string | null>(null);
    const [dialog, setDialog] = useState<'events' | 'recovery' | null>(null);
    const [goalOpen,setGoalOpen]=useState(false);
    const goalButton=useRef<HTMLButtonElement|HTMLAnchorElement>(null);
    const clientStates=useRef<Record<string,AgentClientState>>({});
    const [drafts,setDrafts]=useState<Record<string,string>>({});
    const closeGoal=()=>{setGoalOpen(false);goalButton.current?.focus({preventScroll:true});};
    const [now, setNow] = useState(() => Date.now());
    const workspace = useRef<HTMLDivElement>(null);
    const returnFocus = useRef<HTMLElement | null>(null);
    const returnNode = useRef<string | null>(null);
    const [connection, setConnection] = useState('connecting');
    const [streamRevision, setStreamRevision] = useState(0);
    const [operation, setOperation] = useState<'start' | 'stop'>('start');
    const statusAction = useAction();
    const cursor = useRef(0);
    const state = useQuery({ queryKey: ['ctf', taskId, 'state'], queryFn: ({ signal }) => ctfApi.state(taskId, signal), refetchInterval: 3000 });
    const challenges = useQuery({ queryKey: ['ctf', taskId, 'challenges'], queryFn: ({ signal }) => ctfApi.challenges(taskId, signal), refetchInterval: 3000 });
    const eventHistory = useQuery({ queryKey: ['ctf', taskId, 'events'], queryFn: ({ signal }) => ctfApi.events(taskId, 0, signal), refetchInterval: 10000 });
    useEffect(() => {
        if (!eventHistory.data)
            return;
        cursor.current = eventHistory.data.reduce((value, event) => Math.max(value, event.version), cursor.current);
    }, [eventHistory.data]);
    useEffect(() => {
        if (state.error instanceof ApiError && state.error.status === 401)
            navigate('/login', { replace: true, state: { from: '/tasks/' + taskId } });
    }, [state.error, navigate, taskId]);
    useEffect(() => {
        let timer: ReturnType<typeof setTimeout> | undefined;
        let alive = true;
        setConnection('connecting');
        const source = new EventSource('/api/tasks/' + encodeURIComponent(taskId) + '/stream?since=' + cursor.current, { withCredentials: true });
        source.addEventListener('open', () => { if (alive)
            setConnection('open'); });
        source.addEventListener('error', () => { if (alive)
            setConnection('reconnecting'); });
        const receive = (raw: Event) => {
            if (!alive)
                return;
            try {
                const event = JSON.parse((raw as MessageEvent<string>).data) as CtfEvent;
                const next = acceptCursor(taskId, cursor.current, event);
                if (next === cursor.current)
                    return;
                cursor.current = next;
                if (!timer)
                    timer = setTimeout(() => { timer = undefined; if (alive)
                        void queryClient.invalidateQueries({ queryKey: ['ctf', taskId] }); }, 250);
            }
            catch {
                setConnection('reconnecting');
            }
        };
        for (const type of eventTypes)
            source.addEventListener(type, receive);
        return () => { alive = false; source.close(); if (timer)
            clearTimeout(timer); };
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
    useEffect(() => {
        if (!['provisioning', 'running', 'closing'].includes(phase))
            return;
        const timer = setInterval(() => setNow(Date.now()), 1000);
        return () => clearInterval(timer);
    }, [phase]);
    useEffect(() => {
        if (selectedGraphId)
            workspace.current?.querySelector<HTMLElement>('[data-inspector-panel] h2')?.focus({ preventScroll: true });
    }, [selectedGraphId]);
    function select(kind: 'agent' | 'challenge', id: string) {
        if (!inspectorOpen) { returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; returnNode.current = `${kind}:${id}`; }
        setMemberId(kind === 'agent' ? id : null); setChallengeId(kind === 'challenge' ? id : null);
    }
    function closeInspector() {
        const active = document.activeElement;
        const restore = active === document.body || Boolean(workspace.current?.querySelector('[data-inspector-panel]')?.contains(active)) || active?.getAttribute('role') === 'separator';
        setMemberId(null); setChallengeId(null);
        if (restore) requestAnimationFrame(() => {
          if (document.activeElement !== document.body && document.activeElement !== active) return;
          const node = returnNode.current ? workspace.current?.querySelector<HTMLElement>(`.react-flow__node[data-id="${CSS.escape(returnNode.current)}"] button`) : null;
          if (node) node.focus({preventScroll:true}); else if (returnFocus.current?.isConnected) returnFocus.current.focus({preventScroll:true});
        });
    }
    return <div className={styles.page}>
      <header className={styles.header}>
        <Button type="text" icon={<Icon name="back" />} aria-label="返回任务" onClick={() => navigate('/tasks')} />
        <h1 title={taskTitle(task)}>{taskTitle(task)}</h1><code className={styles.taskId}>{taskId.slice(0,8)}</code>
        <Tag color={phase === 'running' ? 'processing' : phase === 'failed' ? 'error' : 'default'}>{phaseLabels[phase] ?? phase}</Tag>
        <div className={styles.taskMeta}><span>第 {task.run_number??1} 轮</span><span>运行 {activeDuration(task.active_seconds ?? 0, task.active_since, now)}</span><span>{formatMoney(task.usage?.cost ?? 0, task.cost_currency)} / {formatMoney(task.budget?.max_cost ?? 0, task.cost_currency)}</span><span className={styles.live} data-connected={connection === 'open'}>{connection === 'open' ? '实时同步' : connection === 'connecting' ? '连接中' : '正在重连'}</span></div>
        <div className={styles.actions}><Button ref={goalButton} size="small" type="text" icon={<Icon name="file" />} aria-label={goalOpen?"收起任务目标":"查看任务目标"} aria-expanded={goalOpen} aria-controls="task-goal-content" onClick={()=>setGoalOpen(!goalOpen)} onKeyDown={event=>{if(event.key==="Escape"&&goalOpen){event.stopPropagation();closeGoal();}}}>目标</Button>{task.report_uri && <Button href={`/api/tasks/${encodeURIComponent(taskId)}/report`} target="_blank" icon={<Icon name="external" />}>报告</Button>}
          {phase === 'created' && <Button type="primary" loading={statusAction.busy && operation === 'start'} disabled={statusAction.busy} onClick={() => void changeStatus('start')}>启动任务</Button>}
          {['created','provisioning','running'].includes(phase) && <Button danger disabled={statusAction.busy} loading={statusAction.busy && operation === 'stop'} onClick={() => void changeStatus('stop')}>停止任务</Button>}
          <Dropdown trigger={['click']} menu={{ items: [{ key: 'refresh', label: '刷新', icon: <Icon name="refresh" /> }, { key: 'events', label: '系统事件' }, ...(terminal ? [{ key: 'recovery', label: '收尾与恢复' }] : [])], onClick: ({ key }) => key === 'refresh' ? refresh() : setDialog(key as 'events' | 'recovery') }}><Button type="text" icon={<Icon name="more" />} aria-label="更多任务操作" /></Dropdown>
        </div>
      </header>
      <div className={styles.runSummary} role="status"><span>{activeItems.length} 个题目 · {members.length} 个 Agent</span>{verified > 0 && <Tag color="success">{verified} 已验证</Tag>}{candidates > 0 && <Tag color="warning">{candidates} 候选待验</Tag>}<span className={styles.endReason}>{failure ? String(failure.summary ?? '系统执行失败') : terminal ? conclusionLabels[conclusionReason] ?? conclusionReason : phase === 'closing' ? '正在保存结果' : ''}</span>{failure && <code title={String(failure.correlation_id ?? '')}>追踪 {String(failure.correlation_id ?? '').slice(0,8)}</code>}</div>
      {statusAction.error && <Alert type="error" title={statusAction.error} showIcon />}{state.error && <Alert type="error" title={state.error.message} action={<Button onClick={() => void state.refetch()}>重试</Button>} />}
      <div ref={workspace} className={styles.workspace}>
        <InspectorLayout hideOnMobile={goalOpen} label={challenge ? '题目详情' : `${selected?.display_name} 会话详情`} onClose={closeInspector} inspector={challenge ? <ChallengePanel key={taskId + ':' + challenge.id} taskId={taskId} challenge={challenge} members={members} allRecords={records} phase={phase} onBack={closeInspector} onDiscuss={(id) => select('agent',id)} /> : selected ? <Conversation key={taskId + ':' + selected.id} taskId={taskId} member={selected} members={members} phase={phase} onClose={closeInspector} clientState={clientStates.current[selected.id]??=createAgentClientState()} draftValue={drafts[selected.id]??''} onDraftChange={value=>setDrafts(current=>({...current,[selected.id]:value}))} /> : null}>
          <TaskGoal key={taskId} open={goalOpen} goal={task.goal} requirements={task.completion_requirements} context={task.domain_context} files={task.initial_attachments} onClose={closeGoal} />
          <main className={styles.canvasRegion} aria-label="CTF 团队协作画布">{challenges.error && <Alert type="error" title={challenges.error.message} action={<Button onClick={() => void challenges.refetch()}>重试</Button>} />}<CtfBoardCanvas members={members} challenges={items} records={records} selected={selectedGraphId} phase={phase} onSelect={(next) => select(next.kind,next.id)} /></main>
        </InspectorLayout>
      </div>
      <Modal title="系统事件" open={dialog === 'events'} onCancel={() => setDialog(null)} footer={null} width={720}>{eventHistory.isLoading && <p role="status"><Spin /> 正在读取事件…</p>}{eventHistory.error && <Alert type="error" title={eventHistory.error.message} action={<Button onClick={() => void eventHistory.refetch()}>重试</Button>} />}{!eventHistory.isLoading && !eventHistory.error && !eventHistory.data?.length && <Empty description="尚无事件记录" />}<ol className={styles.systemEvents}>{(eventHistory.data ?? []).slice(-100).reverse().map((event) => <li key={event.version}><code>v{event.version}</code><strong>{event.type}</strong><span>{event.actor}</span></li>)}</ol></Modal>
      <Modal title="收尾与恢复" open={dialog === 'recovery'} onCancel={() => setDialog(null)} footer={null} width={900} destroyOnHidden><RecoveryPanel taskId={taskId} task={task} challenges={items} onClose={() => setDialog(null)} /></Modal>
    </div>;
}
