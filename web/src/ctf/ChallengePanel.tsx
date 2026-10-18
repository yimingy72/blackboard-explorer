import LoadingState from '../components/LoadingState';
import { Alert, App, Button, Collapse, Modal, Tabs, Tag } from 'antd';
import { BulbOutlined, CopyOutlined, ExperimentOutlined, FileOutlined, InboxOutlined } from '@ant-design/icons';
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ctfApi } from './api';
import type { CtfArtifact, CtfChallenge, CtfMember, CtfRecord, CtfEvent, CtfSolutionStep } from './types';
import { formatDate, formatTime } from '../pages/format';
import { challengeTimeline, type CtfTimelineEntry } from './progress';
import { verificationLabel } from './view';
import { challengeSolution, challengeState, challengeTitle, solutionMarkdown, stageLabels, stepResultLabels } from './challengeView';
import ChallengeStateTag from './ChallengeState';
import Markdown from './Markdown';
import EvidenceViewer from '../components/EvidenceViewer';
import Icon from '../components/Icon';
import InspectorActions from '../components/InspectorActions';
import styles from './CtfWorkbench.module.css';
import detailStyles from './ChallengePanel.module.css';

function Artifact({ taskId, artifact }: { taskId: string; artifact: CtfArtifact }) {
  const [open, setOpen] = useState(false);
  const query = useQuery({ queryKey: ['ctf', taskId, 'artifact', artifact.id], queryFn: ({ signal }) => ctfApi.artifact(taskId, artifact.id, signal), enabled: open });
  const size = artifact.size < 1024 ? `${artifact.size} B` : `${(artifact.size / 1024).toFixed(1)} KB`;
  return <><Button size="small" className={detailStyles.fileChip} icon={<FileOutlined />} title={artifact.path} onClick={() => setOpen(true)}><span>{artifact.filename}</span><small>{size}</small></Button>
    <Modal title={artifact.filename} open={open} onCancel={() => setOpen(false)} footer={null} width={720}>
      {query.isLoading && <LoadingState label="正在读取文件…" />}
      {query.error && <Alert type="error" title={query.error.message} action={<Button onClick={() => void query.refetch()}>重试</Button>} />}
      {query.data && <><Collapse size="small" ghost items={[{ key: 'info', label: '文件信息', children: <div className={detailStyles.fileInfo}><code>{query.data.path}</code><p>SHA-256 {query.data.sha256}</p></div> }]} /><EvidenceViewer evidence={{ type: 'file', summary: query.data.filename, uri: query.data.uri }} /></>}
    </Modal>
  </>;
}

function Record({ taskId, record, name, compact = false }: { taskId: string; record: CtfRecord; name: (id: string) => string; compact?: boolean }) {
  const labels: Record<string, string> = { note: '工作记录', correction: '补充更正', help_request: '请求增援', verification: '结果记录', target: '目标操作', solution: '解题流程' };
  const fields = [['尝试路线', record.attempted_routes], ['观察与依据', record.observations_and_basis], ['失败条件', record.failure_conditions], ['当前难点', record.current_blocker], ['需要帮助', record.help_needed]];
  return <article className={`${styles.record} ${compact ? detailStyles.compactRecord : ''}`}>
    {!compact && <div className={styles.entryMeta}><strong>{labels[record.kind] ?? '共享记录'}</strong><span>{name(record.author_id)} · v{record.created_version}</span></div>}
    <Markdown text={record.body} />
    {fields.some(([, value]) => value) && <dl className={styles.helpFields}>{fields.map(([label, value]) => value && <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>}
    {record.summary_applied === false && <p className={styles.muted}>此条为历史结果。</p>}
    {record.no_artifacts_reason && <p className={styles.muted}>产物说明：{record.no_artifacts_reason}</p>}
    {!compact && record.verification && <Collapse size="small" ghost items={[{ key: 'result', label: '原始结果信息', children: <><p className={styles.muted}>{verificationLabel(record.verification)}</p>{typeof record.verification.summary === 'string' && record.verification.summary !== record.body && <Markdown text={record.verification.summary} />}{typeof record.verification.user_id === 'string' && <p className={styles.muted}>确认用户：{record.verification.user_id}</p>}{typeof record.verification.call_id === 'string' && <p className={styles.muted}>调用引用：<code>{record.verification.call_id}</code></p>}{typeof record.verification.response_uri === 'string' && <EvidenceViewer evidence={{ type: 'file', summary: '原始返回证据', uri: record.verification.response_uri }} />}</> }]} />}
    {(record.target || record.platform_call) && <Collapse size="small" ghost items={[{ key: 'source', label: '操作原始记录', children: <pre className={detailStyles.rawOperation}>{JSON.stringify({ ...(record.target ? { target: record.target } : {}), ...(record.platform_call ? { platform_call: record.platform_call } : {}) }, null, 2)}</pre> }]} />}
    {!compact && <div className={detailStyles.fileChips}>{(record.artifact_refs ?? []).map(artifact => <Artifact taskId={taskId} artifact={artifact} key={artifact.id} />)}</div>}
  </article>;
}

function ProgressStep({ taskId, entry, name, latest }: { taskId: string; entry: Extract<CtfTimelineEntry, { type: 'record' }>; name: (id: string) => string; latest: boolean }) {
  const [open, setOpen] = useState(false);
  const record = entry.record;
  const stage = record.progress?.stage;
  const symbol = stage === 'breakthrough' || stage === 'approach' ? <BulbOutlined /> : stage === 'attempt' ? <ExperimentOutlined /> : stage === 'result' ? <InboxOutlined /> : <FileOutlined />;
  return <li id={`progress-${record.id}`} className={`${detailStyles.step} ${latest ? detailStyles.latest : ''}`}>
    <time className={detailStyles.stepTime} dateTime={entry.createdAt} title={entry.createdAt ? formatDate(entry.createdAt) : `v${entry.version}`}>{entry.createdAt ? formatTime(entry.createdAt).slice(0, 5) : `v${entry.version}`}</time>
    <span className={detailStyles.marker} aria-hidden>{symbol}</span>
    <div className={detailStyles.stepContent}>
      <div className={detailStyles.stepMeta}><span>{name(entry.actorId)}</span><span className={detailStyles.stage}>{stage ? stageLabels[stage] : record.kind === 'help_request' ? '增援' : '记录'}</span></div>
      <button type="button" className={detailStyles.stepToggle} aria-expanded={open} aria-controls={`progress-body-${record.id}`} onClick={() => setOpen(!open)}><strong>{entry.title}</strong><Icon name="chevron" className={open ? detailStyles.rotated : ''} /></button>
      {!open && entry.detail.trim() !== entry.title.trim() && <p className={detailStyles.stepSummary}>{entry.detail}</p>}
      {open && <div id={`progress-body-${record.id}`} className={detailStyles.stepBody}><Record taskId={taskId} record={record} name={name} compact /></div>}
      {!!record.artifact_refs?.length && <div className={detailStyles.fileChips}>{record.artifact_refs.map(artifact => <Artifact key={artifact.id} taskId={taskId} artifact={artifact} />)}</div>}
    </div>
  </li>;
}

function SolutionFlow({ taskId, challenge, record, artifacts, name, onRecord, previous = false }: { taskId: string; challenge: CtfChallenge; record: CtfRecord; artifacts: CtfArtifact[]; name: (id: string) => string; onRecord: (id: string) => void; previous?: boolean }) {
  const { message } = App.useApp();
  const solution = record.solution!;
  const authorRole = record.author_id === 'lead' ? 'Lead 代交' : record.author_id === 'user' ? '用户整理' : record.author_id === challenge.owner_id ? '负责人整理' : '阶段整理';
  const files = new Map(artifacts.map(artifact => [artifact.id, artifact]));
  const references = (step: CtfSolutionStep) => <div className={detailStyles.references}>{step.record_ids.map((id, index) => <Button type="link" size="small" key={id} onClick={() => onRecord(id)}>依据 {index + 1}<Icon name="arrow" /></Button>)}{step.artifact_ids.map(id => files.has(id) ? <Artifact key={id} taskId={taskId} artifact={files.get(id)!} /> : <span className={styles.muted} key={id}>文件引用未加载</span>)}</div>;
  const renderSteps = (kind: CtfSolutionStep['kind']) => solution.steps.filter(step => step.kind === kind).map((step, index) => <div className={detailStyles.solutionStep} key={`${kind}:${index}`}><div className={detailStyles.solutionStepTitle}><strong>{step.title}</strong><Tag className={detailStyles.resultTag} color={step.result === 'supported' ? 'success' : undefined}>{stepResultLabels[step.result]}</Tag></div><Markdown text={step.summary} />{references(step)}</div>);
  return <div className={detailStyles.solutionFlow}>
    <div className={detailStyles.flowByline}><FileOutlined /><span>{name(record.author_id)} · {authorRole}</span>{record.created_at && <time title={formatDate(record.created_at)}>{formatTime(record.created_at).slice(0, 5)}</time>}{record.solution_run_number && <span>第 {record.solution_run_number} 轮</span>}{previous && <span>上一轮</span>}<Button type="text" size="small" icon={<CopyOutlined />} onClick={async () => { try { await navigator.clipboard.writeText(`${previous ? '> 上一轮结论\n\n' : ''}${solutionMarkdown(challenge.title, solution)}`); void message.success('已复制解题流程'); } catch { void message.error('无法复制，请检查浏览器剪贴板权限'); } }}>复制 Markdown</Button></div>
    <section className={detailStyles.flowSection}><h3>思路</h3><Markdown text={solution.approach} />{renderSteps('approach')}</section>
    {solution.steps.some(step => step.kind === 'attempt') && <section className={detailStyles.flowSection}><h3>关键尝试与取舍</h3>{renderSteps('attempt')}</section>}
    {solution.steps.some(step => step.kind === 'breakthrough') && <section className={detailStyles.flowSection}><h3>突破点</h3>{renderSteps('breakthrough')}</section>}
    <section className={detailStyles.flowSection}><h3>结论与复现</h3><Markdown text={solution.conclusion} />{renderSteps('result')}{!!solution.remaining.length && <div className={detailStyles.remaining}><strong>未解决事项</strong><ul>{solution.remaining.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}<div className={detailStyles.reproduction}><Markdown text={solution.reproduction} /></div></section>
  </div>;
}

export default function ChallengePanel({ taskId, challenge, members, allRecords, events = [], historyLoading, historyError, onRetryHistory, phase, onBack, onDiscuss }: {
  taskId: string; challenge: CtfChallenge; members: CtfMember[]; allRecords: CtfRecord[]; events?: CtfEvent[];
  historyLoading?: boolean; historyError?: string; onRetryHistory?: () => void; phase: string; onBack: () => void; onDiscuss: (member: string) => void;
}) {
  const [offset, setOffset] = useState(0), [visibleSteps, setVisibleSteps] = useState(12);
  const [tab, setTab] = useState<'overview' | 'flow' | 'files'>('overview');
  const [historyOpen, setHistoryOpen] = useState(false);
  const [referenceId, setReferenceId] = useState<string | null>(null);
  const terminal = ['finished', 'stopped', 'failed'].includes(phase);
  const page = useQuery({ queryKey: ['ctf', taskId, 'records', challenge.id, offset], queryFn: ({ signal }) => ctfApi.records(taskId, challenge.id, signal, offset), enabled: historyOpen, refetchInterval: terminal ? false : 3000 });
  const localSolution = allRecords.find(record => record.id === challenge.solution_record_id && record.challenge_id === challenge.id && record.kind === 'solution' && record.solution);
  const solutionRead = useQuery({ queryKey: ['ctf', taskId, 'record', challenge.id, challenge.solution_record_id], queryFn: ({ signal }) => ctfApi.record(taskId, challenge.id, challenge.solution_record_id!, signal), enabled: Boolean(challenge.solution_record_id && !localSolution) });
  const related = useMemo(() => [...new Map([...allRecords, ...(page.data?.records ?? []), ...(solutionRead.data ? [solutionRead.data] : [])].filter(record => record.challenge_id === challenge.id).map(record => [record.id, record])).values()].sort((a, b) => a.created_version - b.created_version), [allRecords, page.data, solutionRead.data, challenge.id]);
  const artifacts = useMemo(() => [...new Map(related.flatMap(record => record.artifact_refs ?? []).map(artifact => [artifact.id, artifact])).values()], [related]);
  const solutions = useMemo(() => challengeSolution(challenge, related), [challenge, related]);
  const timeline = useMemo(() => challengeTimeline(challenge.id, related, events, members), [challenge.id, related, events, members]);
  const progress = timeline.filter((entry): entry is Extract<CtfTimelineEntry, { type: 'record' }> => entry.type === 'record' && entry.record.kind !== 'solution');
  const dispatch = timeline.filter(entry => entry.type !== 'record');
  const name = (id: string) => members.find(member => member.id === id)?.display_name ?? (id === 'user' ? '用户' : id === 'system' ? '系统' : id);
  const participants = [...new Set([challenge.owner_id, ...challenge.collaborator_ids].filter((id): id is string => Boolean(id)))];
  const title = challengeTitle(challenge.title);
  const localReference = related.find(record => record.id === referenceId);
  const reference = useQuery({ queryKey: ['ctf', taskId, 'record', challenge.id, referenceId], queryFn: ({ signal }) => ctfApi.record(taskId, challenge.id, referenceId!, signal), enabled: Boolean(referenceId && !localReference) });
  const selectedRecord = localReference ?? (reference.data?.challenge_id === challenge.id ? reference.data : undefined);
  const solutionStatus = challenge.solution_record_id && !solutions.current ? solutionRead.error || solutionRead.data ? <Alert type="error" title="解题流程读取失败" action={<Button onClick={() => void solutionRead.refetch()}>重试</Button>} /> : <LoadingState label="正在读取解题流程…" /> : null;
  const history = <Collapse className={detailStyles.history} ghost activeKey={historyOpen ? ['history'] : []} onChange={keys => setHistoryOpen(keys.includes('history'))} items={[{ key: 'history', label: '历史共享记录', children: <>
    {page.isLoading && <LoadingState label="正在读取历史记录…" />}{page.error && <Alert type="error" title={page.error.message} action={<Button onClick={() => void page.refetch()}>重试</Button>} />}
    {page.data?.records.map(record => <Record key={record.id} taskId={taskId} record={record} name={name} />)}
    {page.data && !page.data.records.length && <p className={styles.empty}>尚无共享记录。</p>}
    {(offset > 0 || page.data?.next_offset != null) && <nav className={styles.actions} aria-label="记录分页"><Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>上一页</Button><Button disabled={page.data?.next_offset == null} onClick={() => setOffset(page.data?.next_offset ?? offset)}>下一页</Button></nav>}
  </> }]} />;
  const content = <div className={`${styles.detailScroll} ${detailStyles.content}`}>
    {tab === 'overview' && <>
      <section className={detailStyles.participants}><h3>参与成员</h3><div>{participants.map(id => members.some(member => member.id === id) ? <Button key={id} size="small" onClick={() => onDiscuss(id)}><span className={detailStyles.avatar} aria-hidden>{name(id).slice(0, 1).toUpperCase()}</span>{name(id)}<span className={detailStyles.participantRole}>{challenge.owner_id === id ? '主责' : '协作'}</span></Button> : <Tag key={id} title={id}>未知成员 · {challenge.owner_id === id ? '主责' : '协作'}</Tag>)}{!participants.length && <span className={styles.muted}>尚未分派</span>}</div></section>
      <Collapse size="small" className={detailStyles.description} items={[{ key: 'description', label: '题目说明', children: <><dl className={detailStyles.briefFields}><div><dt>Agent 登记简述</dt><dd>{challenge.description ? <Markdown text={challenge.description} /> : '尚未登记题目说明。'}</dd></div>{challenge.connection && !challenge.description.includes(challenge.connection) && <div><dt>连接</dt><dd><code>{challenge.connection}</code></dd></div>}{challenge.requirements && <div><dt>交付要求</dt><dd><Markdown text={challenge.requirements} /></dd></div>}</dl>{challenge.external_id && <p className={detailStyles.sourceId}>外部标识 <code>{challenge.external_id}</code></p>}</> }]} />
      <section className={detailStyles.conclusion}><div className={detailStyles.conclusionTitle}><InboxOutlined /><h3>工作结论</h3>{solutions.current?.created_at && <time title={formatDate(solutions.current.created_at)}>{formatTime(solutions.current.created_at).slice(0, 5)}</time>}</div>{solutions.current?.solution ? <><Markdown text={solutions.current.solution.conclusion} /><div className={detailStyles.conclusionSource}><span>{name(solutions.current.author_id)} · {solutions.current.author_id === 'lead' ? 'Lead 整理' : '负责人整理'}</span><Button type="link" size="small" onClick={() => setTab('flow')}>解题流程<Icon name="arrow" /></Button></div></> : solutionStatus ?? <p className={styles.muted}>{challenge.work_status === 'in_progress' ? '解题仍在进行，负责人尚未提交最终流程。' : '负责人尚未提交解题流程。'}</p>}</section>
      <section className={detailStyles.timelineSection}><h3>关键进展<span>{progress.length} 项</span></h3>{progress.length > visibleSteps && <Button type="text" size="small" onClick={() => setVisibleSteps(count => count + 12)}>显示更早的进展（{progress.length - visibleSteps}）</Button>}{progress.length ? <ol className={detailStyles.timeline}>{progress.slice(-visibleSteps).map(entry => <ProgressStep key={entry.id} taskId={taskId} entry={entry} name={name} latest={entry.id === progress.at(-1)?.id} />)}</ol> : <p className={styles.empty}>尚无共享进展。</p>}</section>
      <Collapse className={detailStyles.dispatch} size="small" ghost items={[{ key: 'dispatch', label: <>派工动态 <span>{dispatch.length}</span></>, children: <>{historyLoading && <LoadingState label="正在读取派工动态…" />}{historyError && <Alert type="error" title={historyError} action={<Button onClick={onRetryHistory}>重试</Button>} />}<ol>{dispatch.map(entry => <li key={entry.id}><time>{entry.createdAt ? formatTime(entry.createdAt).slice(0, 5) : `v${entry.version}`}</time><div><strong>{entry.title}</strong><span>{name(entry.actorId)}</span><p>{entry.detail}</p></div></li>)}</ol></> }]} />
    </>}
    {tab === 'flow' && <>{solutions.current ? <SolutionFlow taskId={taskId} challenge={challenge} record={solutions.current} artifacts={artifacts} name={name} onRecord={setReferenceId} /> : solutionStatus ?? <div className={detailStyles.emptyFlow}><FileOutlined /><h3>负责人尚未提交解题流程</h3><p>{challenge.work_status === 'in_progress' ? '关键进展见概览。' : '已有工作记录和文件继续保留。'}</p></div>}{solutions.previous && <Collapse ghost className={detailStyles.previous} items={[{ key: 'previous', label: '上一轮结论', children: <SolutionFlow taskId={taskId} challenge={challenge} record={solutions.previous} artifacts={artifacts} name={name} onRecord={setReferenceId} previous /> }]} />}{history}</>}
    {tab === 'files' && <section className={detailStyles.files}><h3>已登记文件<span>{artifacts.length}</span></h3>{artifacts.length ? artifacts.map(artifact => <div className={detailStyles.fileRow} key={artifact.id}><Artifact taskId={taskId} artifact={artifact} /><small>{artifact.path}</small></div>) : <p className={styles.empty}>尚无登记文件。</p>}</section>}
  </div>;
  return <aside className={styles.challengeDetail} aria-label={`题目详情：${challenge.title}`}>
    <header className={`${styles.panelHeader} ${detailStyles.header}`}><FileOutlined className={detailStyles.titleIcon} /><div className={styles.panelIdentity}><h2 tabIndex={-1} title={challenge.title}>{title.title}</h2>{!!title.metadata.length && <div className={detailStyles.titleMeta}>{title.metadata.join(' · ')}</div>}</div><ChallengeStateTag state={challengeState(challenge)} /><InspectorActions onClose={onBack} /></header>
    <Tabs className={styles.detailTabs} activeKey={tab} onChange={key => setTab(key as typeof tab)} items={[{ key: 'overview', label: '概览', children: tab === 'overview' ? content : null }, { key: 'flow', label: '解题流程', children: tab === 'flow' ? content : null }, { key: 'files', label: <>文件 <span className={detailStyles.tabCount}>{artifacts.length}</span></>, children: tab === 'files' ? content : null }]} />
    <Modal title="依据记录" open={Boolean(referenceId)} onCancel={() => setReferenceId(null)} footer={null} width={720}>{selectedRecord ? <Record taskId={taskId} record={selectedRecord} name={name} /> : reference.error ? <Alert type="error" title="依据记录读取失败" action={<Button onClick={() => void reference.refetch()}>重试</Button>} /> : <LoadingState label="正在读取依据…" />}</Modal>
  </aside>;
}
