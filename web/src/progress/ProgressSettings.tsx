import { Alert, Button, Checkbox, Input, Select } from 'antd';
import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import LoadingState from '../components/LoadingState';
import { progressApi } from './api';
import type { ProgressSettings as Settings } from './types';
import styles from './ProgressPage.module.css';

const tools = { read_progress_sources: '黑板来源与增量', get: '读取对象', search: '搜索已存资料', read_evidence: '读取登记证据', get_progress_tree: '读取成果树', update_progress_tree: '维护成果树' };
export default function ProgressSettings({ onDirtyChange, onBusyChange }: { onDirtyChange: (value: boolean) => void; onBusyChange: (value: boolean) => void }) {
  const query = useQuery({ queryKey: ['progress-settings'], queryFn: ({ signal }) => progressApi.settings(signal) });
  const client = useQueryClient();
  const models = useQuery({ queryKey: ['platform-models'], queryFn: api.listPlatformModels });
  const [saved, setSaved] = useState<Settings | null>(null), [draft, setDraft] = useState<Settings | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const dirty = Boolean(saved && draft && JSON.stringify(saved) !== JSON.stringify(draft));
  useEffect(() => { if (query.data && (!saved || !dirty && JSON.stringify(saved) !== JSON.stringify(query.data))) { setSaved(query.data); setDraft(query.data); } }, [query.data, saved, dirty]);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  async function save() {
    if (!draft || busy) return; setBusy(true); onBusyChange(true); setError(''); setNotice('');
    try { const value = await progressApi.saveSettings(draft); client.setQueryData(['progress-settings'], value); setSaved(value); setDraft(value); setNotice('已保存，之后的整理回合生效。'); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '保存失败，草稿已保留。'); }
    finally { setBusy(false); onBusyChange(false); }
  }
  if (!draft) return query.error ? <Alert type="error" title={query.error.message} action={<Button onClick={() => void query.refetch()}>重试</Button>} /> : <LoadingState label="成果整理配置" lines={6} />;
  const update = (value: Partial<Settings>) => { setDraft({ ...draft, ...value }); setNotice(''); };
  return <section className={styles.settings}>
    <div className={styles.settingsHeader}><h2>成果整理 Agent</h2><Button type="primary" size="small" disabled={!dirty || busy} loading={busy} onClick={() => void save()}>保存并应用</Button></div>
    {error && <Alert type="error" title={error} />}{notice && <Alert type="success" title={notice} />}
    <div className={styles.settingsGrid}><label>完整系统提示词<Input.TextArea aria-label="成果整理系统提示词" value={draft.prompt} disabled={busy} onChange={event => update({ prompt: event.target.value })} autoSize={{ minRows: 22, maxRows: 38 }} style={{ marginTop: 10, fontFamily: 'var(--font-mono)', fontSize: 13 }} /></label><div className={styles.tools}><label>模型<Select aria-label="成果整理模型" style={{ width: '100%', marginTop: 8 }} disabled={busy || models.isLoading} value={draft.model_id ?? ''} options={[{ value: '', label: '沿用任务模型' }, ...(models.data?.filter(model => model.enabled).map(model => ({ value: model.name, label: model.label })) ?? [])]} onChange={value => update({ model_id: value || null })} /></label><p className={styles.modelInfo}>模型与价格在任务启用时固定；提示词在下一个整理回合应用。整理费用由任务单独限额控制。</p><h3>允许的工具</h3>{Object.entries(tools).map(([id, label]) => <Checkbox key={id} disabled={busy || ['read_progress_sources', 'get_progress_tree', 'update_progress_tree'].includes(id)} checked={draft.enabled_tools.includes(id)} onChange={event => update({ enabled_tools: event.target.checked ? [...draft.enabled_tools, id] : draft.enabled_tools.filter(tool => tool !== id) })}>{label}</Checkbox>)}<p className={styles.modelInfo}>只读任务资料，写入仅限成果树。没有命令、派工或黑板写权限。</p></div></div>
  </section>;
}
