import { Button, Collapse } from 'antd';
import { useEffect, useState } from 'react';
import { api, type PreviewData } from '../api/client';
import { codeLines, formatBytes, highlightLiteral, logKeywords, parseEvidence, parseHttpPreview, parseScriptPreview, parseToolLog, } from './evidence';
import styles from './EvidenceViewer.module.css';
const labels: Record<string, string> = {
    http: 'HTTP', log: '日志', code_ref: '代码引用', script: '脚本',
    command_output: '命令输出', text: '文本', file: '文件',
};
function TwoColumns({ firstLabel, first, secondLabel, second }: {
    firstLabel: string;
    first: string;
    secondLabel: string;
    second: string;
}) {
    return (<div className={styles.columns}>
      <div><h4>{firstLabel}</h4><pre>{first || '未提供'}</pre></div>
      <div><h4>{secondLabel}</h4><pre>{second || '未提供'}</pre></div>
    </div>);
}
function PreviewBody({ kind, raw, path, summary }: {
    kind: string;
    raw: string;
    path: string | null;
    summary: string;
}) {
    if (kind === 'http') {
        const { request, response } = parseHttpPreview(raw);
        return <TwoColumns firstLabel="请求" first={request} secondLabel="响应" second={response}/>;
    }
    if (kind === 'log') {
        return <pre className={styles.log}>{highlightLiteral(raw, logKeywords(summary)).map((part, index) => part.match ? <mark key={index}>{part.text}</mark> : <span key={index}>{part.text}</span>)}</pre>;
    }
    if (kind === 'code_ref') {
        return <div className={styles.code}><p>{path ?? '代码片段'}</p><pre>{codeLines(raw, path)}</pre></div>;
    }
    if (kind === 'script') {
        const { script, output } = parseScriptPreview(raw);
        return <TwoColumns firstLabel="脚本" first={script} secondLabel="输出" second={output}/>;
    }
    if (kind === 'command_output') {
        const call = parseToolLog(raw);
        if (call)
            return (<div className={styles.command}>
        <div className={styles.commandName}><strong>工具</strong><span>{call.tool}</span></div>
        <h4>参数</h4><pre>{call.args}</pre>
        <h4>完整结果</h4><pre>{call.result}</pre>
      </div>);
    }
    return <pre>{raw}</pre>;
}
export default function EvidenceViewer({ evidence, initiallyOpen=false }: {
    evidence: unknown;
    initiallyOpen?: boolean;
}) {
    const item = parseEvidence(evidence);
    const [open, setOpen] = useState(initiallyOpen);
    const [preview, setPreview] = useState<PreviewData | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [retry, setRetry] = useState(0);
    const uri = item?.uri ?? null;
    useEffect(() => {
        if (!open || !uri)
            return;
        const controller = new AbortController();
        setLoading(true);
        setPreview(null);
        setError('');
        void api.getEvidencePreview(uri, controller.signal)
            .then((value) => { if (!controller.signal.aborted)
            setPreview(value); })
            .catch((cause: unknown) => {
            if (!controller.signal.aborted)
                setError(cause instanceof Error ? cause.message : '无法读取证据预览。');
        })
            .finally(() => { if (!controller.signal.aborted)
            setLoading(false); });
        return () => controller.abort();
    }, [open, uri, retry]);
    if (!item)
        return <p className={styles.invalid}>这条证据记录格式不完整，无法预览。</p>;
    return <Collapse className={styles.viewer} size="small" activeKey={open?['evidence']:[]} onChange={keys=>setOpen(keys.length>0)} items={[{key:'evidence',label:<span className={styles.summary}>
        <span className={styles.kind}>{labels[item.type] ?? item.type}</span>
        <span className={styles.summaryText}>{item.summary || '无摘要'}</span>
        {item.auto && <span className={styles.auto}>系统附加</span>}
      </span>,children:<>      <div className={styles.body}>
        {(item.path || item.callId || item.size !== null) && (<div className={styles.metadata}>
            {item.path && <span>路径：{item.path}</span>}
            {item.callId && <span>调用：{item.callId}</span>}
            {item.size !== null && <span>大小：{formatBytes(item.size)}</span>}
          </div>)}
        {!uri ? <p className={styles.notice}>这条证据没有可下载的持久化对象。</p> : (<>
            <a className={styles.download} href={api.evidenceUrl(uri)} download>下载完整证据</a>
            {loading && <p role="status" className={styles.notice}>正在加载证据预览…</p>}
            {error && <div role="alert" className={styles.error}><p>预览失败：{error}</p><Button onClick={() => setRetry((count) => count + 1)} htmlType={"button"}>重试</Button></div>}
            {preview && (<div className={styles.preview}>
                {preview.truncated && <p className={styles.truncated}>文件共 {formatBytes(preview.size)}；仅显示开头和结尾。可下载完整文件。</p>}
                {preview.binary ? <p className={styles.notice}>这是二进制证据，无法显示文字预览。请下载查看。</p> :
                    <PreviewBody kind={item.type} raw={preview.text} path={item.path} summary={item.summary}/>}
              </div>)}
          </>)}
      </div>
</>}]} />;
}
