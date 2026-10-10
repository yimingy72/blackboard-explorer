import { Alert, Button, Checkbox, Dropdown, Input, Modal, Tag } from 'antd';
import InspectorLayout from '../components/InspectorLayout';
import TaskGoal from '../components/TaskGoal';
import {createAgentClientState,type AgentClientState} from '../components/agentClientState';
import Icon from '../components/Icon';
import { taskTitle } from './format';
import { TaskCurrency } from './currency';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { api, ApiError, request, type TaskView } from '../api/client';
import CtfWorkbench from '../ctf/CtfWorkbench';
import { useBoard } from '../board/store';
import { reduceBoard } from '../board/reducer';
import { taskEndExplanation } from '../board/view';
import { activeDuration, agentContributions, agentStyle, agentLabel, agentNumbers, agentRole, agentStatusLabel, orderedAgents } from '../board/agents';
import AgentConversation from '../components/AgentConversation';
import WorkbenchDrawer from '../components/WorkbenchDrawer';
import DetailPanel from '../components/DetailPanel';
import TopologyFlowCanvas from '../components/TopologyFlowCanvas';
import controls from '../styles/controls.module.css';
import { formatMoney, taskStatusLabel } from './format';
import styles from './TaskWorkbenchPage.module.css';
const connectionText = {
    connecting: '正在连接实时事件',
    open: '实时连接正常',
    reconnecting: '连接中断，正在重连',
    closed: '实时连接已关闭',
};
function BlackboardWorkbenchPage() {
    const { taskId } = useParams<{
        taskId: string;
    }>();
    const navigate = useNavigate();
    const location = useLocation();
    const queryClient = useQueryClient();
    const [recordsOpen, setRecordsOpen] = useState(false);
    const [goalOpen,setGoalOpen]=useState(false);
    const goalButton=useRef<HTMLButtonElement|HTMLAnchorElement>(null);
    const taskMenuButton=useRef<HTMLButtonElement|HTMLAnchorElement>(null);
    const clientStates=useRef<Record<string,AgentClientState>>({});
    const [drafts,setDrafts]=useState<Record<string,string>>({});
    const closeGoal=()=>{setGoalOpen(false);goalButton.current?.focus({preventScroll:true});};
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const agentButtons = useRef(new Map<string, HTMLElement>());
    const [resumeOpen, setResumeOpen] = useState(false);
    const resumeRequest = useRef('');
    const [resumeCost, setResumeCost] = useState('0');
    const [resumeMinutes, setResumeMinutes] = useState('0');
    const [refreshTools, setRefreshTools] = useState(false);
    const [resuming, setResuming] = useState(false);
    const [resumeError, setResumeError] = useState('');
    const [action, setAction] = useState<'start' | 'stop' | null>(null);
    const [actionError, setActionError] = useState('');
    const [version, setVersion] = useState<number | null>(null);
    const [now, setNow] = useState(() => Date.now());
    const taskQuery = useQuery({ queryKey: ['task', taskId], queryFn: () => api.getTask(taskId!), enabled: Boolean(taskId), refetchInterval: 5000 });
    const board = useBoard(taskId);
    const viewEvents = useMemo(() => version === null ? board.events : board.events.filter((event) => event.version <= version), [board.events, version]);
    const viewState = useMemo(() => version === null ? board.state : reduceBoard(viewEvents), [version, board.state, viewEvents]);
    const totalObjects = Object.keys(viewState.facts).length + Object.keys(viewState.intents).length;
    const unauthorized = (taskQuery.error instanceof ApiError && taskQuery.error.status === 401) || (board.error instanceof ApiError && board.error.status === 401);
    const numbers = useMemo(() => agentNumbers(viewEvents), [viewEvents]);
    const agents = useMemo(() => orderedAgents(viewState, numbers), [viewState, numbers]);
    const contributions = useMemo(() => agentContributions(viewState, viewEvents), [viewState, viewEvents]);
    const selectedAgent = selectedId ? viewState.agents[selectedId] : undefined;
    function closeAgent(id: string) {
        setSelectedId(null);
        requestAnimationFrame(() => agentButtons.current.get(id)?.focus());
    }
    useEffect(() => {
        if (version !== null || !['running', 'closing'].includes(board.state.task?.status ?? ''))
            return;
        setNow(Date.now());
        const timer = window.setInterval(() => setNow(Date.now()), 1000);
        return () => window.clearInterval(timer);
    }, [version, board.state.task?.status]);
    useEffect(() => { setSelectedId(null); setVersion(null); setRecordsOpen(false); }, [taskId]);
    useEffect(() => {
        if (selectedId && board.state.agents[selectedId] && !viewState.agents[selectedId])
            setSelectedId(null);
    }, [selectedId, board.state.agents, viewState.agents]);
    useEffect(() => {
        const focus = new URLSearchParams(location.search).get('focus');
        if (focus && /^(?:[FI]\d+|agent-\d+|goal)$/.test(focus))
            setSelectedId(focus);
    }, [taskId, location.search]);
    useEffect(() => {
        if (unauthorized)
            navigate('/login', { replace: true, state: { from: location.pathname } });
    }, [unauthorized, navigate, location.pathname]);
    async function changeStatus(next: 'start' | 'stop') {
        if (!taskId)
            return;
        setAction(next);
        setActionError('');
        try {
            if (next === 'start')
                await api.startTask(taskId);
            else
                await api.stopTask(taskId);
            await queryClient.invalidateQueries({ queryKey: ['task', taskId] });
            await queryClient.invalidateQueries({ queryKey: ['tasks'] });
        }
        catch (cause) {
            setActionError(cause instanceof ApiError ? cause.message : '操作失败，请重试。');
        }
        finally {
            setAction(null);
        }
    }
    async function resume(event: React.FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (!taskId)
            return;
        setResuming(true);
        setResumeError('');
        try {
            await api.resumeTask(taskId, {
                request_id: resumeRequest.current,
                additional_cost: resumeCost,
                additional_minutes: Number(resumeMinutes),
                refresh_tools: refreshTools,
            });
            setResumeOpen(false);
            await queryClient.invalidateQueries({ queryKey: ['task', taskId] });
            await queryClient.invalidateQueries({ queryKey: ['tasks'] });
        }
        catch (cause) {
            setResumeError(cause instanceof ApiError ? cause.message : '续跑失败，请重试。');
        }
        finally {
            setResuming(false);
        }
    }
    if (!taskId)
        return null;
    if (board.taskId !== taskId || (taskQuery.isLoading && board.loading))
        return <div className={styles.loading} role="status" aria-label="正在加载任务"><span className={controls.skeleton}/><span className={controls.skeleton}/><span className={controls.skeleton}/></div>;
    if (taskQuery.isError && !unauthorized)
        return <div className={styles.errorState} role="alert"><h1>无法打开任务</h1><p>{taskQuery.error instanceof ApiError ? taskQuery.error.message : '请稍后重试。'}</p><div><Button className={controls.button} onClick={() => void taskQuery.refetch()} htmlType={"button"}>重试</Button><Link to="/tasks" className={controls.button}>返回任务列表</Link></div></div>;
    const task = taskQuery.data;
    const boardTask = viewState.task;
    const status = boardTask?.status ?? task?.status ?? 'created';
    const usedCost = boardTask?.usage.cost ?? (version === null ? task?.usage?.cost : 0) ?? 0;
    const maxCost = boardTask?.budget.max_cost;
    const costLimit = Number(maxCost);
    const costProgress = Number.isFinite(costLimit) && costLimit > 0 ? Math.min(100, Math.max(0, Number(usedCost) / costLimit * 100)) : 0;
    const reportUri = boardTask?.report_uri ?? (version === null ? task?.report_uri : null);
    const explanation = taskEndExplanation(viewState);
    const cacheHit = boardTask?.usage.cache_hit_tokens ?? 0;
    const cacheInput = cacheHit + (boardTask?.usage.cache_miss_tokens ?? 0);
    const cachePercent = cacheInput ? `${(cacheHit / cacheInput * 100).toFixed(1)}%` : '—';
    const steps = Object.values(viewState.agents).reduce((count, agent) => count + agent.steps, 0);
    const workers = agents.filter((agent) => agent.taskType !== 'close' && ['running', 'concluding'].includes(agent.status)).length;
    const workerLimit = Number(boardTask?.budget.max_concurrent_agents) || '—';
    const cutoffAt = version === null ? null : viewEvents.at(-1)?.created_at;
    const duration = boardTask ? activeDuration(boardTask.activeSeconds ?? 0, boardTask.activeSince && (!cutoffAt || Date.parse(cutoffAt) > Date.parse(boardTask.activeSince))
        ? boardTask.activeSince : null, cutoffAt ? Math.min(Date.parse(cutoffAt), now) : now)
        : activeDuration(task?.active_seconds ?? 0, task?.active_since, now);
    return <TaskCurrency.Provider value={task?.cost_currency ?? null}><div className={styles.page}>
      <header className={styles.header}><div className={styles.titleRow}>
        <Button type="text" icon={<Icon name="back" />} aria-label="返回任务" onClick={() => navigate('/tasks')} />
        <h1 title={taskTitle(boardTask ?? task ?? {})}>{taskTitle(boardTask ?? task ?? {})}</h1><code className={styles.taskId}>{taskId.slice(0,8)}</code>
        <Tag color={status === 'running' ? 'processing' : status === 'finished' ? 'success' : status === 'failed' ? 'error' : 'default'}>{taskStatusLabel(status)}</Tag>
        <div className={styles.titleMeta}><span>第 {boardTask?.runNumber??task?.run_number??1} 轮</span><span>累计运行 {duration}</span><span>{formatMoney(usedCost,task?.cost_currency)}{maxCost === undefined ? '' : ` / ${formatMoney(maxCost,task?.cost_currency)}`}</span><span className={styles.connection} data-state={board.connection}><span aria-hidden />{connectionText[board.connection]}</span></div>
        <div className={styles.actions}><Button ref={goalButton} type="text" size="small" icon={<Icon name="file" />} aria-label={goalOpen?"收起任务目标":"查看任务目标"} aria-expanded={goalOpen} aria-controls="task-goal-content" onClick={()=>setGoalOpen(!goalOpen)} onKeyDown={event=>{if(event.key==="Escape"&&goalOpen){event.stopPropagation();closeGoal();}}}>目标</Button><Dropdown trigger={['click']} menu={{items:[{key:'records',label:'复盘记录'},...(reportUri ? [{key:'report',label:'查看报告'}] : []),...((boardTask?.workspace_uri ?? (version === null ? task?.workspace_uri : null)) ? [{key:'archive',label:'下载归档'}] : [])],onClick:({key})=>{if(key==='records')setRecordsOpen(true);if(key==='report')navigate(`/tasks/${encodeURIComponent(taskId)}/report`);if(key==='archive')window.location.assign(`/api/tasks/${encodeURIComponent(taskId)}/workspace`);}}}><Button ref={taskMenuButton} type="text" icon={<Icon name="more" />} aria-label="更多任务操作" /></Dropdown>
          {['finished','failed','stopped'].includes(status) && <Button disabled={version !== null} onClick={() => {resumeRequest.current=crypto.randomUUID();setResumeError('');setResumeOpen(true);}}>续跑任务</Button>}
          {status==='created' && <Button type="primary" loading={action==='start'} disabled={action!==null || version!==null} onClick={()=>void changeStatus('start')}>启动任务</Button>}
          {['created','provisioning','running'].includes(status) && <Button danger loading={action==='stop'} disabled={action!==null || version!==null} onClick={()=>void changeStatus('stop')}>停止</Button>}
        </div>
      </div>
      {task && task.run_number>1 && (task.runs?.length??0)>0 && <nav className={styles.runHistory} aria-label="历史轮次">{task.runs?.map(run=><span key={run.run_number}>第{run.run_number}轮 {run.report_uri&&<Link to={`/tasks/${encodeURIComponent(taskId)}/report?run=${run.run_number}`}>报告</Link>} {run.workspace_uri&&<a href={`/api/tasks/${encodeURIComponent(taskId)}/workspace?run=${run.run_number}`}>归档</a>}</span>)}</nav>}
      <div className={styles.runSummary}>{explanation&&<p className={styles.endReason} title={explanation}>结束原因：{explanation}</p>}<span className={styles.runStats} aria-label="运行统计">对象 {totalObjects} · 并发 {workers}/{workerLimit} · 模型调用 {steps} · 缓存命中 {cachePercent} · 预算 {Math.round(costProgress)}%</span></div>
      {version!==null&&<Alert type="warning" title={`历史回放 · v${version}`} description={`已接收 ${board.events.length-viewEvents.length} 条后续事件；当前视图只读。`} action={<Button onClick={()=>setVersion(null)}>返回实时</Button>} />}
      {(actionError || (board.error&&!unauthorized))&&<Alert type="error" title={actionError||board.error?.message} action={board.error?<Button onClick={()=>window.location.reload()}>重新连接</Button>:undefined} />}
      {board.connection==='reconnecting'&&!board.error&&<Alert type="warning" title="实时连接暂时中断，正在自动重连" />}
      </header>
      <div className={styles.agentBar} role="group" aria-label="全部 Agent">
        <strong>Agent <span>{agents.length}</span></strong>
        <div className={styles.agentList}>{agents.length ? agents.map((agent) => <Button key={agent.id} ref={(node) => { if (node)
        agentButtons.current.set(agent.id, node);
    else
        agentButtons.current.delete(agent.id); }} className={styles.agentItem} style={agentStyle(numbers[agent.id])} aria-pressed={selectedId === agent.id} onClick={() => setSelectedId(agent.id)} htmlType={"button"}><span>{agentLabel(agent.id, numbers)}</span><small>{agentRole(agent)} · {agentStatusLabel[agent.status]}</small><small className={styles.contribution}>{contributions[agent.id]?.facts.length ?? 0} Fact · {contributions[agent.id]?.intents.length ?? 0} Intent{contributions[agent.id]?.judgments ? ` · ${contributions[agent.id].judgments} 次裁定` : ''}</small></Button>) : <span className={styles.agentEmpty}>尚无 Agent，任务启动后会出现在这里。</span>}</div>
      </div>

      <InspectorLayout hideOnMobile={goalOpen} label={selectedAgent ? 'Agent 对话' : '对象详情'} onClose={() => selectedAgent ? closeAgent(selectedAgent.id) : setSelectedId(null)} inspector={selectedAgent ? <AgentConversation contribution={contributions[selectedAgent.id]} color={agentStyle(numbers[selectedAgent.id])} onSelect={setSelectedId} key={`${taskId}-${selectedAgent.id}`} taskId={taskId} agent={selectedAgent} label={agentLabel(selectedAgent.id,numbers)} events={viewEvents} state={viewState} historical={version!==null} onClose={()=>closeAgent(selectedAgent.id)} clientState={clientStates.current[selectedAgent.id]??=createAgentClientState()} draftValue={drafts[selectedAgent.id]??''} onDraftChange={value=>setDrafts(current=>({...current,[selectedAgent.id]:value}))} /> : selectedId ? <DetailPanel taskId={taskId} state={viewState} selectedId={selectedId} onSelect={setSelectedId} historical={version!==null} agentNumbers={numbers} /> : null}>
        <TaskGoal key={taskId} open={goalOpen} goal={boardTask?.goal??(version===null?task?.goal:'')??''} context={boardTask?.domain_context??null} files={boardTask?.initialAttachments??(version===null?task?.initial_attachments:[])??[]} requirements={boardTask?.acceptance.length?<ul>{boardTask.acceptance.map(item=><li key={item.id}>{item.id} · {item.desc}</li>)}</ul>:undefined} onClose={closeGoal} />
        <section className={styles.canvasArea} aria-label="黑板关系图">{board.loading ? <div className={styles.canvasLoading} role="status" aria-label="正在同步关系图"><span className={controls.skeleton} /><span className={controls.skeleton} /><span className={controls.skeleton} /></div> : <TopologyFlowCanvas key={taskId} state={viewState} agentNumbers={numbers} selectedId={selectedId} onSelect={setSelectedId} />}</section>
      </InspectorLayout>
      <WorkbenchDrawer open={recordsOpen} onClose={() => setRecordsOpen(false)} onRestoreFocus={()=>taskMenuButton.current?.focus({preventScroll:true})} taskId={taskId} state={viewState} events={viewEvents} allEvents={board.events} version={version} onVersion={setVersion} onSelect={setSelectedId} agentNumbers={numbers}/>
      <Modal title="续跑任务" open={resumeOpen} onCancel={() => { if(!resuming) setResumeOpen(false); }} footer={null} closable={!resuming} mask={{closable:!resuming}} keyboard={!resuming}>
        <form className={styles.resumeForm} onSubmit={(event) => void resume(event)}>
          <p>保留黑板和 Agent 会话，以及初始附件；恢复已登记文件，重新建立运行依赖。</p>
          <label className={controls.field}><span className={controls.label}>追加金额（{task?.cost_currency ?? '任务币种'}）</span><Input disabled={resuming} className={controls.input} type="number" min="0" step="any" required value={resumeCost} onChange={(event) => setResumeCost(event.target.value)}/></label>
          <label className={controls.field}><span className={controls.label}>追加运行分钟</span><Input disabled={resuming} className={controls.input} type="number" min="0" step="1" required value={resumeMinutes} onChange={(event) => setResumeMinutes(event.target.value)}/></label>
          <label className={styles.refreshTools}><Checkbox disabled={resuming} checked={refreshTools} onChange={(event) => setRefreshTools(event.target.checked)}/>采用当前 Worker 工具设置</label>
          <p className={styles.resumeHint}>当前总限额：{formatMoney(task?.budget?.max_cost ?? boardTask?.budget.max_cost, task?.cost_currency)}、{String(task?.budget?.max_minutes ?? boardTask?.budget.max_minutes ?? 0)} 分钟；已累计运行 {duration}。追加后须留有可探索额度。</p>
          {!task?.cleanup_ready && <p className={styles.resumeHint} role="status">正在归档已登记文件并清理任务容器，完成后即可续跑。</p>}
          {resumeError && <p className={controls.error} role="alert">{resumeError}</p>}
          <div className={styles.resumeActions}><Button className={controls.button} disabled={resuming} onClick={() => setResumeOpen(false)} htmlType={"button"}>取消</Button><Button className={`${controls.button} ${controls.primary}`} disabled={resuming || !task?.cleanup_ready} htmlType={"submit"} type={"primary"}>{resuming ? '正在续跑…' : '确认续跑'}</Button></div>
        </form>
      </Modal>
    </div></TaskCurrency.Provider>;
}
export default function TaskWorkbenchPage() {
    const { taskId } = useParams<{
        taskId: string;
    }>();
    const navigate = useNavigate();
    const task = useQuery({ queryKey: ['task-mode', taskId], queryFn: ({ signal }) => request<TaskView>(`/tasks/${encodeURIComponent(taskId!)}`, { signal }), enabled: Boolean(taskId), retry: false });
    useEffect(() => { if (task.error instanceof ApiError && task.error.status === 401)
        navigate('/login', { replace: true }); }, [task.error, navigate]);
    if (!taskId)
        return <p role="alert">未指定任务。</p>;
    if (task.isLoading)
        return <p role="status">正在读取任务…</p>;
    if (!task.data)
        return <div role="alert"><p>{task.error?.message ?? '无法读取任务。'}</p><Button className={controls.button} onClick={() => void task.refetch()} htmlType={"button"}>重试</Button> <Link to="/tasks">返回任务列表</Link></div>;
    return task.data.mode === 'ctf' ? <CtfWorkbench key={taskId} taskId={taskId} initialTask={task.data}/> : <BlackboardWorkbenchPage key={taskId}/>;
}
