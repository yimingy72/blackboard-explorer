import { Alert, Button, Collapse, Tag } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { ctfApi } from './api';
import type { CtfChallenge, CtfMember } from './types';
import EvidenceViewer from '../components/EvidenceViewer';
import LoadingState from '../components/LoadingState';
import Markdown from './Markdown';
import styles from './ChallengeKnowledgePanel.module.css';

const intentLabels = { open: '待领取', claimed: '已领取', closed: '已结束' };
const resultLabels = { confirmed: '成立', rejected: '已排除', inconclusive: '未定' };

export default function ChallengeKnowledgePanel({ taskId, challenge, members, onDiscuss }: {
  taskId: string; challenge: CtfChallenge; members: CtfMember[]; onDiscuss: (id: string) => void;
}) {
  const query = useQuery({ queryKey: ['ctf', taskId, 'knowledge', challenge.id],
    queryFn: ({ signal }) => ctfApi.knowledge(taskId, challenge.id, signal) });
  if (query.isLoading) return <LoadingState label="正在读取题内黑板…" />;
  if (query.error) return <Alert type="error" title={query.error.message} action={<Button onClick={() => void query.refetch()}>重试</Button>} />;
  const facts = Object.values(query.data?.facts ?? {});
  const intents = Object.values(query.data?.intents ?? {});
  const team = [challenge.owner_id, ...challenge.collaborator_ids].filter((id): id is string => Boolean(id));
  const name = (id: string) => members.find(member => member.id === id)?.display_name ?? id;
  const finished = challenge.work_status === 'completed';
  return <div className={styles.board} aria-label="题内小黑板">
    <div className={styles.summary}><strong>事实 {facts.length}</strong><strong>意图 {intents.length}</strong><Button size="small" onClick={() => void query.refetch()}>刷新</Button></div>
    {challenge.collaboration && <div className={styles.team}>{team.map(id => <Button key={id} size="small" onClick={() => onDiscuss(id)}>
      {name(id)} · {id === challenge.owner_id ? '主责' : '援助'} · {finished ? '历史认领' : '持有'} {intents.filter(intent => intent.holder === id && intent.status === 'claimed').length}{!finished && '/3'}
    </Button>)}</div>}
    <section><h3>意图</h3>{!intents.length && <p className={styles.muted}>尚无意图。成员可先观察题目，再基于事实提出方向。</p>}
      {intents.map(intent => <article className={styles.item} key={intent.id}>
        <div className={styles.heading}><code>{intent.id}</code><Tag>{finished && intent.status !== 'closed' ? '已停止推进' : intentLabels[intent.status]}</Tag>{intent.result && <Tag>{resultLabels[intent.result]}</Tag>}</div>
        <Markdown text={intent.statement} />
        <p className={styles.muted}>提出：{name(intent.author)}{intent.holder ? <> · {finished ? '原认领' : '认领'}：<Button type="link" size="small" onClick={() => onDiscuss(intent.holder!)}>{name(intent.holder)}</Button></> : null}</p>
        <dl><dt>方法</dt><dd>{intent.method}</dd><dt>预期</dt><dd>{intent.expected}</dd><dt>依据</dt><dd>{intent.based_on.map(id => <a key={id} href={`#knowledge-${id}`}>{id} </a>)}</dd></dl>
        {!!intent.result_facts?.length && <p>结果事实：{intent.result_facts.map(id => <a key={id} href={`#knowledge-${id}`}>{id} </a>)}</p>}
      </article>)}
    </section>
    <section><h3>事实</h3>{!facts.length && <p className={styles.muted}>尚无事实。新的观察和证据会同步到这里。</p>}
      {facts.map(fact => <article className={styles.item} key={fact.id} id={`knowledge-${fact.id}`}>
        <div className={styles.heading}><code>{fact.id}</code><Tag color={fact.status === 'disputed' ? 'orange' : undefined}>{fact.status === 'disputed' ? '存在争议' : '已记录'}</Tag><span>{name(fact.author)}</span>{fact.provenance === 'tool_backed' && <Tag>有工具证据</Tag>}</div>
        <Markdown text={fact.statement} />
        {!!fact.derived_from?.length && <p className={styles.muted}>推导自：{fact.derived_from.join('、')}</p>}
        {!!fact.disputes?.length && <p>质疑：{fact.disputes.join('、')}</p>}
        {fact.resolves && <p>结束意图：{fact.resolves}{fact.result ? ` · ${resultLabels[fact.result]}` : ''}</p>}
        <Collapse size="small" ghost items={[{ key: 'evidence', label: `证据 ${fact.evidence.length}`, children: fact.evidence.map((evidence, index) => <div className={styles.evidence} key={index}>
          {evidence.path && <code>{evidence.path}</code>}{evidence.sha256 && <small>SHA-256 {evidence.sha256}</small>}
          {evidence.call_id && <Button type="link" size="small" onClick={() => onDiscuss(fact.author)}>查看执行 · {evidence.call_id.slice(0, 12)}</Button>}
          <EvidenceViewer evidence={evidence} />
        </div>) }]} />
      </article>)}
    </section>
  </div>;
}
