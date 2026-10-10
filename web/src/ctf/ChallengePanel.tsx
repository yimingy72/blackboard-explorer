import { useId, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ctfApi } from './api';
import type { CtfArtifact, CtfChallenge, CtfMember, CtfRecord } from './types';
import { candidateRecords, candidateText } from './graph';
import { verificationLabel, workLabels } from './view';
import Markdown from './Markdown';
import EvidenceViewer from '../components/EvidenceViewer';
import Icon from '../components/Icon';
import controls from '../styles/controls.module.css';
import styles from './CtfWorkbench.module.css';

function Artifact({ taskId, artifact }: { taskId: string; artifact: CtfArtifact }) {
  const [open, setOpen] = useState(false);
  const query = useQuery({ queryKey: ['ctf', taskId, 'artifact', artifact.id], queryFn: ({ signal }) => ctfApi.artifact(taskId, artifact.id, signal), enabled: open });
  return <div className={styles.artifact}><button type="button" onClick={() => setOpen(!open)} aria-expanded={open}><Icon name="file" /> {artifact.filename} <small>{artifact.size < 1024 ? `${artifact.size} B` : `${(artifact.size / 1024).toFixed(1)} KB`}</small></button>
    {open && <div className={styles.artifactDetail}>{query.isLoading && <p role="status">正在读取产物…</p>}{query.error && <p role="alert">{query.error.message} <button type="button" onClick={() => void query.refetch()}>重试</button></p>}{query.data && <><details><summary>文件信息</summary><code>{query.data.path}</code><p className={styles.muted}>SHA-256 {query.data.sha256}</p></details><EvidenceViewer evidence={{ type: 'file', summary: query.data.filename, uri: query.data.uri }} /></>}</div>}
  </div>;
}

function Record({ taskId, record, name }: { taskId: string; record: CtfRecord; name: (id: string) => string }) {
  const labels: Record<string, string> = { note: '工作记录', correction: '补充更正', help_request: '请求增援', verification: '验证记录', target: '目标操作' };
  const fields: Array<[string, string | null | undefined]> = [['尝试路线', record.attempted_routes], ['观察与依据', record.observations_and_basis], ['失败条件', record.failure_conditions], ['当前难点', record.current_blocker], ['需要帮助', record.help_needed]];
  return <article className={`${styles.record} ${record.kind === 'help_request' ? styles.helpRecord : ''}`}><div className={styles.entryMeta}><strong>{labels[record.kind] ?? record.kind}</strong><span>{name(record.author_id)} · v{record.created_version}</span></div><Markdown text={record.body} />
    {fields.some(([, value]) => value) && <dl className={styles.helpFields}>{fields.map(([label, value]) => value && <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>}
    {record.verification && <div className={styles.resultDetails}><strong>{verificationLabel(record.verification)}</strong>{record.verification.test_only === true && <p>仅模拟测试，不代表真实平台验证。</p>}{typeof record.verification.summary === 'string' && <Markdown text={record.verification.summary} />}{typeof record.verification.user_id === 'string' && <p>确认用户：{record.verification.user_id}</p>}{typeof record.verification.call_id === 'string' && <p>调用引用：<code>{record.verification.call_id}</code></p>}{typeof record.verification.response_uri === 'string' && <EvidenceViewer evidence={{ type: 'file', summary: '平台返回证据', uri: record.verification.response_uri }} />}</div>}
    {record.target && <details className={styles.details}><summary>{record.target.test_only ? '模拟平台目标操作' : '平台目标操作'} · {String(record.target.status ?? '未知')}</summary><pre>{JSON.stringify(record.target, null, 2)}</pre></details>}
    {record.platform_call && <details className={styles.details}><summary>{record.platform_call.test_only ? '模拟平台调用记录' : '平台调用记录'}</summary><pre>{JSON.stringify(record.platform_call, null, 2)}</pre></details>}
    {record.summary_applied === false && <p className={styles.muted}>历史结果已保存；当前摘要保留较新的记录。</p>}
    {record.no_artifacts_reason && <p className={styles.muted}>产物说明：{record.no_artifacts_reason}</p>}
    {(record.artifact_refs ?? []).map((artifact) => <Artifact taskId={taskId} artifact={artifact} key={artifact.id} />)}
  </article>;
}

export default function ChallengePanel({ taskId, challenge, members, allRecords, phase, onBack, onDiscuss }: { taskId: string; challenge: CtfChallenge; members: CtfMember[]; allRecords: CtfRecord[]; phase: string; onBack: () => void; onDiscuss: (member: string) => void }) {
  const [offset, setOffset] = useState(0);
  const [tab, setTab] = useState<'overview' | 'records' | 'files'>('overview');
  const tabId = useId();
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const tabs = [['overview', '概览'], ['records', '记录'], ['files', '文件']] as const;
  function keySwitch(event: KeyboardEvent) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = tabs.findIndex(([key]) => key === tab);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (index + (event.key === 'ArrowRight' ? 1 : -1) + 3) % 3;
    setTab(tabs[next][0]); tabRefs.current[next]?.focus();
  }

  const records = useQuery({ queryKey: ['ctf', taskId, 'records', challenge.id, offset], queryFn: ({ signal }) => ctfApi.records(taskId, challenge.id, signal, offset), refetchInterval: 3000 });
  const artifacts = useMemo(() => [...new Map(allRecords.filter((record) => record.challenge_id === challenge.id).flatMap((record) => record.artifact_refs ?? []).map((item) => [item.id, item])).values()], [allRecords, challenge.id]);
  const candidates = useMemo(() => candidateRecords(allRecords, challenge.id), [allRecords, challenge.id]);
  const related = allRecords.filter((record) => record.challenge_id === challenge.id);
  const latest = candidates.at(-1) ?? related.at(-1);
  const currentText = latest ? typeof latest.verification?.summary === 'string' ? latest.verification.summary : latest.body : '';
  const name = (id: string) => members.find((member) => member.id === id)?.display_name ?? (id === 'user' ? '用户' : id);
  return <section className={styles.challengeDetail} aria-label={`题目详情：${challenge.title}`}>
    <header className={styles.panelHeader}><div className={styles.panelIdentity}><p>题目</p><h2 tabIndex={-1}>{challenge.title}</h2></div><button type="button" className={`${controls.button} ${controls.quiet} ${controls.iconButton}`} onClick={onBack} aria-label="关闭详情"><Icon name="close" /></button></header>
    <nav className={styles.detailTabs} role="tablist" aria-label="题目详情分类" onKeyDown={keySwitch}>{tabs.map(([key, label], index) => <button type="button" key={key} ref={(node) => { tabRefs.current[index] = node; }} role="tab" id={`${tabId}-${key}`} aria-controls={`${tabId}-panel`} aria-selected={tab === key} tabIndex={tab === key ? 0 : -1} onClick={() => setTab(key)}>{label}{key === 'records' ? <small>{related.length}</small> : key === 'files' ? <small>{artifacts.length}</small> : null}</button>)}</nav>
    <div className={styles.detailScroll} id={`${tabId}-panel`} role="tabpanel" aria-labelledby={`${tabId}-${tab}`} tabIndex={0}>
      {tab === 'overview' && <>
        <div className={styles.detailState}><span className={controls.badge}>{workLabels[challenge.work_status] ?? challenge.work_status}</span><span className={`${controls.badge} ${challenge.verification?.status === 'accepted' && !challenge.verification.test_only ? controls.badgeSuccess : controls.badgeWarning}`}>{verificationLabel(challenge.verification ?? {})}</span></div>
        {currentText && <section className={styles.detailSection}><h3>{latest?.verification?.status === 'candidate' ? '当前候选' : '最新记录'}</h3><Markdown text={currentText.length > 360 ? currentText.slice(0, 360) + '…' : currentText} /><div className={styles.source}><span>{latest && name(latest.author_id)}</span><button type="button" onClick={() => setTab('records')}>查看原始记录<Icon name="arrow" /></button></div></section>}
        <section className={styles.detailSection}><h3>验证</h3><dl className={styles.challengeFacts}><div><dt>当前状态</dt><dd>{verificationLabel(challenge.verification ?? {})}</dd></div><div><dt>验证要求</dt><dd>{challenge.verification?.required ? '要求独立验证' : '未设为必需'}</dd></div></dl>{typeof challenge.verification?.summary === 'string' && <Markdown text={challenge.verification.summary} />}</section>
        <section className={styles.detailSection}><h3>参与 Agent</h3><div className={styles.people}>{challenge.owner_id && <button type="button" onClick={() => onDiscuss(challenge.owner_id!)}>{name(challenge.owner_id)}<small>负责人</small></button>}{challenge.collaborator_ids.map((id) => <button type="button" key={id} onClick={() => onDiscuss(id)}>{name(id)}<small>协作</small></button>)}{!challenge.owner_id && !challenge.collaborator_ids.length && <span className={styles.muted}>尚未分派</span>}</div></section>
        {artifacts.length > 0 && <section className={styles.detailSection}><h3>关键证据</h3>{artifacts.slice(0, 3).map((artifact) => <Artifact taskId={taskId} artifact={artifact} key={artifact.id} />)}{artifacts.length > 3 && <button type="button" className={styles.textButton} onClick={() => setTab('files')}>全部 {artifacts.length} 个文件<Icon name="arrow" /></button>}</section>}
        <details className={styles.details}><summary>题目目标与连接信息</summary>{challenge.description && <Markdown text={challenge.description} />}{challenge.requirements && <div className={styles.requirements}><strong>工作要求</strong><Markdown text={challenge.requirements} /></div>}{challenge.connection && <p className={styles.connectionAddress}><code>{challenge.connection}</code></p>}{Object.keys(challenge.target ?? {}).length > 0 && <details><summary>{challenge.target.test_only ? '模拟平台目标信息' : '平台目标信息'}</summary><pre>{JSON.stringify(challenge.target, null, 2)}</pre></details>}</details>
      </>}
      {tab === 'records' && <>
        {candidates.length > 0 && <section className={styles.candidateSection} aria-label="候选答案与独立验证"><h3>候选记录 <small>{candidates.length}</small></h3>{candidates.map((record, index) => <details key={record.id} className={styles.candidateRecord} open={index === candidates.length - 1}><summary>候选 {index + 1} · {name(record.author_id)}</summary><Markdown text={candidateText(record)} />{typeof record.verification?.summary === 'string' && <p className={styles.muted}>{record.verification.summary}</p>}</details>)}</section>}
        {records.isLoading && <p role="status">正在读取记录…</p>}{records.error && <p className={styles.error} role="alert">{records.error.message} <button type="button" onClick={() => void records.refetch()}>重试</button></p>}
        {!records.isLoading && !records.error && !records.data?.records.length && <p className={styles.empty}>还没有共享记录。</p>}
        {records.data?.records.filter((record) => record.verification?.status !== 'candidate').map((record) => <Record key={record.id} taskId={taskId} record={record} name={name} />)}
        {(offset > 0 || records.data?.next_offset != null) && <nav className={styles.actions} aria-label="记录分页"><button type="button" className={controls.button} disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>上一页</button><button type="button" className={controls.button} disabled={records.data?.next_offset == null} onClick={() => setOffset(records.data?.next_offset ?? offset)}>下一页</button></nav>}
      </>}
      {tab === 'files' && <section className={styles.detailSection}><h3>已登记文件 <small>{artifacts.length}</small></h3>{artifacts.length ? artifacts.map((artifact) => <Artifact taskId={taskId} artifact={artifact} key={artifact.id} />) : <p className={styles.empty}>尚无登记文件。</p>}</section>}
    </div><footer className={styles.detailFooter}><span>{['finished', 'stopped', 'failed'].includes(phase) ? '本轮已结束' : '探索中'}</span><button type="button" className={styles.textButton} onClick={() => onDiscuss('lead')}>与 Lead 讨论<Icon name="arrow" /></button></footer>
  </section>;
}
