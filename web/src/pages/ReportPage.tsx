import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import { api, ApiError } from '../api/client';
import { formatMoney, formatDate } from './format';
import controls from '../styles/controls.module.css';
import styles from './ReportPage.module.css';

type MarkdownNode = { type: string; value?: string; url?: string; children?: MarkdownNode[] };
function linkReferences(taskId: string) {
  return () => (tree: MarkdownNode) => {
    function visit(node: MarkdownNode) {
      if (!node.children || ['link', 'code', 'inlineCode'].includes(node.type)) return;
      node.children = node.children.flatMap((child) => {
        if (child.type !== 'text' || !child.value) { visit(child); return [child]; }
        return child.value.split(/(\b[FI]\d+\b)/).filter(Boolean).map((value) => /^([FI]\d+)$/.test(value)
          ? { type: 'link', url: `/tasks/${encodeURIComponent(taskId)}?focus=${value}`, children: [{ type: 'text', value }] }
          : { type: 'text', value });
      });
    }
    visit(tree);
  };
}

export default function ReportPage() {
  const { taskId = '' } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const run = Number(new URLSearchParams(location.search).get('run')) || undefined;
  const runQuery = run ? `?run=${run}` : '';
  const task = useQuery({ queryKey: ['task', taskId], queryFn: () => api.getTask(taskId), enabled: Boolean(taskId) });
  const report = useQuery({ queryKey: ['report', taskId, run], queryFn: ({ signal }) => api.getReport(taskId, signal, run), enabled: Boolean(taskId) });
  const unauthorized = [task.error, report.error].some((error) => error instanceof ApiError && error.status === 401);
  useEffect(() => { if (unauthorized) navigate('/login', { replace: true, state: { from: location.pathname } }); }, [unauthorized, navigate, location.pathname]);
  return <div className={styles.page}>
    <header className={styles.header}>
      <Link to={`/tasks/${encodeURIComponent(taskId)}`}>← 返回工作台</Link>
      <div className={styles.title}><div><h1>{run ? `第 ${run} 轮报告` : '最终报告'}</h1><p>{task.data?.goal ?? '正在读取任务…'}</p></div><div className={styles.actions}><a className={controls.button} href={`/api/tasks/${encodeURIComponent(taskId)}/report${runQuery}`} download>下载原文</a>{(run ? task.data?.runs?.find((item) => item.run_number === run)?.workspace_uri : task.data?.workspace_uri) && <a className={controls.button} href={`/api/tasks/${encodeURIComponent(taskId)}/workspace${runQuery}`}>下载工作区</a>}</div></div>
      {task.data && <p className={styles.meta}>{taskId.slice(0, 8)} · {`创建于 ${formatDate(task.data.created_at)}`} · 估算费用 {formatMoney(task.data.usage?.cost, task.data.cost_currency)}</p>}
    </header>
    {report.isLoading ? <p className={styles.message} role="status">正在读取报告…</p> : report.error ? <div className={styles.message} role="alert"><h2>报告尚不可用</h2><p>{report.error.message}</p><button className={controls.button} type="button" onClick={() => void report.refetch()}>重试</button></div> : <article className={styles.markdown} aria-label="最终报告正文"><ReactMarkdown skipHtml remarkPlugins={[linkReferences(taskId)]} components={{
      a: ({ href, children }) => href?.startsWith(`/tasks/${taskId}`) ? <Link to={href}>{children}</Link> : <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
      img: ({ alt }) => <span>图片：{alt || '报告中的图片未自动加载'}</span>,
      code: ({ children, className }) => {
        const value = String(children).trim();
        return !className && /^(evidence|toolcalls)\/[0-9a-f-]{36}\//i.test(value) && !value.includes('\n')
          ? <a href={api.evidenceUrl(value)} target="_blank" rel="noopener noreferrer"><code>{children}</code></a>
          : <code className={className}>{children}</code>;
      },
    }}>{report.data ?? ''}</ReactMarkdown></article>}
  </div>;
}
