import { ArrowRightOutlined, FileTextOutlined, MessageOutlined, TeamOutlined } from '@ant-design/icons';
import { Button } from 'antd';
import { ProgressStatus } from './ProgressCanvas';
import type { ProgressNode, ProgressRef } from './types';
import styles from './ProgressDetails.module.css';

type Props = { node: ProgressNode; onSource: (ref: ProgressRef) => void; onAgent: (id: string) => void; onAsk: () => void };
const sourceNames: Record<string, string> = { task: '任务目标', fact: '事实记录', intent: '探索意图', event: '过程记录', evidence: '证据附件', acceptance: '验收裁定' };
const refKey = (ref: ProgressRef) => `${ref.kind}:${ref.id}:${ref.version}:${ref.evidence_uri ?? ''}:${ref.sha256 ?? ''}`;
function SourceLinks({ refs, onSource }: { refs: ProgressRef[]; onSource: Props['onSource'] }) {
  return <div className={styles.inlineSources}>{refs.map((ref, index) => <Button className={styles.sourceLink} key={`${refKey(ref)}:${index}`} size="small" type="link" icon={<FileTextOutlined />} onClick={() => onSource(ref)} aria-label={`查看${sourceNames[ref.kind]} ${ref.id}，版本 ${ref.version}`}>{ref.id}<span className={styles.version}>v{ref.version}</span></Button>)}</div>;
}
export default function ProgressDetails({ node, onSource, onAgent, onAsk }: Props) {
  const refs = [...new Map([...node.source_refs, ...node.key_steps.flatMap(step => step.source_refs)].map(ref => [refKey(ref), ref])).values()];
  return <div className={styles.details}>
    <div className={styles.scroll}>
      <div className={styles.status}><ProgressStatus node={node} /><span>{node.effective_state === 'sources_changed' ? '依据有变化，摘要待更新' : '成果整理 Agent 归纳'}</span></div>
      <p className={styles.summary}>{node.summary || node.question || '当前尚未形成具体认识。'}</p>
      {node.question && <section className={styles.section}><h2>正在解决的问题</h2><p>{node.question}</p></section>}
      {node.significance && <section className={styles.section}><h2>为什么重要</h2><p>{node.significance}</p></section>}
      {node.assessment && <section className={styles.section}><h2>Agent 归纳</h2><p>{node.assessment}</p></section>}
      <section className={styles.section}><h2>依据与原始记录<span>{refs.length} 条引用</span></h2>
        {refs.length ? <div className={styles.sources}>{refs.map(ref => <Button className={styles.source} type="text" key={refKey(ref)} onClick={() => onSource(ref)}><span className={styles.sourceTop}><FileTextOutlined /><code>{ref.id}</code><span>{sourceNames[ref.kind]} · v{ref.version}</span><ArrowRightOutlined /></span><span className={styles.sourceCaption}>{ref.kind === 'evidence' ? '查看登记的证据内容' : '查看对应版本的原始记录'}</span></Button>)}</div> : <p className={styles.empty}>尚无可引用的来源，当前内容仍待补证。</p>}
      </section>
      {node.key_steps.length > 0 && <section className={styles.section}><h2>关键过程</h2><ol className={styles.timeline}>{node.key_steps.map((step, index) => <li key={index}><p>{step.text}</p><SourceLinks refs={step.source_refs} onSource={onSource} /></li>)}</ol></section>}
      {(node.limitations || node.next_step) && <section className={styles.section}><h2>尚未解决</h2><div className={styles.gap}>{node.limitations && <p><strong>适用范围与局限</strong>{node.limitations}</p>}{node.next_step && <p><strong>下一步</strong>{node.next_step}</p>}</div></section>}
      {!!node.contributors?.length && <section className={styles.section}><h2>参与 Agent<span>打开已有会话</span></h2><div className={styles.contributors}>{node.contributors.map(id => <Button className={styles.contributor} key={id} icon={<TeamOutlined />} onClick={() => onAgent(id)}>{id}</Button>)}</div></section>}
    </div>
    <div className={styles.footer}><span>基于已登记来源整理</span><Button className={styles.ask} type="link" size="small" icon={<MessageOutlined />} onClick={onAsk}>问整理 Agent</Button></div>
  </div>;
}
