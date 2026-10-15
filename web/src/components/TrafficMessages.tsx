import { Collapse, Tag, Tooltip, Typography } from 'antd';
import { CodeOutlined, FileTextOutlined } from '@ant-design/icons';
import type { TrafficDetail, TrafficMessage } from '../api/traffic';
import styles from './TrafficMessages.module.css';

function body(message: TrafficMessage) {
  if (message.body_encoding === 'omitted') return '';
  try { return JSON.stringify(JSON.parse(message.body_preview), null, 2); }
  catch { return message.body_preview; }
}
function Message({ message }: { message: TrafficMessage | null | undefined }) {
  if (!message) return <p className={styles.empty}>没有保存此侧内容</p>;
  const text = body(message);
  return <div className={styles.message}>
    {!!message.headers?.length && <div className={styles.headers}>{message.headers.map(([name, value], index) => <div key={`${name}:${index}`}><span>{name}</span><code>{value}</code></div>)}</div>}
    <div className={styles.bodyHeader}><span>Body</span><span>{message.body_bytes.toLocaleString()} B{message.body_encoding === 'base64' ? ' · Base64' : ''}{message.body_truncated ? ' · 预览截断' : ''}</span>{text && <Typography.Text copyable={{ text, tooltips: ['复制正文', '已复制'] }} />}</div>
    {text ? <pre tabIndex={0}>{text}</pre> : <p className={styles.empty}>{message.body_bytes ? '未保存正文预览' : '无正文'}</p>}
  </div>;
}

export default function TrafficMessages({ detail }: { detail: TrafficDetail }) {
  const logical = detail.observation?.scope === 'logical_call';
  const attributes = detail.observation?.attributes ?? {};
  return <section className={styles.root} aria-label="流量内容详情">
    <div className={styles.heading}><span className={styles.icon}>{logical ? <CodeOutlined aria-hidden="true" /> : <FileTextOutlined aria-hidden="true" />}</span><strong>{logical ? '模型 / 工具调用' : `${detail.method ?? detail.protocol.toUpperCase()} ${detail.status_code ?? ''}`}</strong><Tooltip title={logical ? 'MAF 逻辑调用；SDK 内部重试次数未知，不对应单独的网络包。' : '由采集代理保存的完整请求响应摘要。'}><Tag>{logical ? '逻辑调用' : 'HTTP 事务'}</Tag></Tooltip></div>
    <code className={styles.address}>{detail.url || detail.summary}</code>
    {detail.observation?.error && <p className={styles.error}>{detail.observation.error}</p>}
    <Collapse size="small" defaultActiveKey={['request', 'response']} items={[
      { key: 'request', label: logical ? '调用输入' : 'Request · 请求', children: <Message message={detail.request} /> },
      { key: 'response', label: logical ? '调用输出' : 'Response · 响应', children: <Message message={detail.response} /> },
      ...(Object.keys(attributes).length ? [{ key: 'attributes', label: '关联信息', children: <dl className={styles.attributes}>{Object.entries(attributes).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl> }] : []),
    ]} />
  </section>;
}
