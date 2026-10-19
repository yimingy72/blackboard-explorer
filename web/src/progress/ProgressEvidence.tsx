import { Alert, Button, Collapse } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { request } from '../api/client';
import LoadingState from '../components/LoadingState';
import { PreviewBody } from '../components/EvidenceViewer';
import type { ProgressRef } from './types';

export default function ProgressEvidence({ taskId, source, kind = 'text' }: { taskId: string; source: ProgressRef; kind?: string }) {
  const [open, setOpen] = useState(false), [offset, setOffset] = useState(0);
  const verified = Boolean(source.evidence_uri && source.sha256);
  const query = useQuery({ queryKey: ['progress-evidence', taskId, source.evidence_uri, source.sha256, source.version, offset], enabled: open && verified, queryFn: ({ signal }) => {
    const params = new URLSearchParams({ uri: source.evidence_uri!, sha256: source.sha256!, version: String(source.version), offset: String(offset), limit: '16000' });
    return request<{ content: string; offset: number; next_offset: number; has_more: boolean }>(`/tasks/${encodeURIComponent(taskId)}/progress/evidence?${params}`, { signal });
  } });
  return <Collapse size="small" activeKey={open ? ['evidence'] : []} onChange={keys => setOpen(keys.length > 0)} items={[{ key: 'evidence', label: source.id, children: !verified ? <Alert type="info" title="这条历史证据未登记可校验的内容哈希，不能以当前文件代替。" /> : <>
    {query.isLoading && <LoadingState label="校验并读取证据" lines={4} />}
    {query.error && <Alert type="error" title={query.error.message} action={<Button onClick={() => void query.refetch()}>重试</Button>} />}
    {query.data && <><PreviewBody kind={kind} raw={query.data.content} path={null} summary={source.id} /><p>内容身份已校验 · 字节 {offset}–{query.data.next_offset}</p><Button size="small" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 16000))}>上一段</Button> <Button size="small" disabled={!query.data.has_more} onClick={() => setOffset(query.data!.next_offset)}>下一段</Button></>}
  </> }]} />;
}
