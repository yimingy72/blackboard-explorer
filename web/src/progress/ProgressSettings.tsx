import { Alert, App, Button, Select } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError, piModelCanRunTask } from '../api/client';
import LoadingState from '../components/LoadingState';
import AgentWorkbench from '../settings/AgentWorkbench';
import PromptEditor from '../settings/PromptEditor';
import { useTrackSettings, type ChangeTracking } from '../settings/settingsChanges';
import ToolPermissions from '../settings/ToolPermissions';
import type { CatalogQuery } from '../settings/toolCatalog';
import styles from '../settings/Settings.module.css';
import { progressApi } from './api';
import type { ProgressSettings as Settings } from './types';

type Draft = Omit<Settings, 'prompt'> & { prompt: string | null };
const toDraft = (value: Settings): Draft => ({ ...value, prompt: value.prompt || null });
const lifecycle = '提示词与工具从下次整理回合生效；模型和费率在任务首次启用成果整理时固定。';
const errorText = (error: unknown) => error instanceof ApiError && error.status === 409
  ? '配置已更新。草稿已保留，可复制后重新读取。'
  : error instanceof Error ? error.message : '操作失败，请重试。';

export default function ProgressSettings({ query, tracking }: { query: CatalogQuery; tracking: ChangeTracking }) {
  const { modal } = App.useApp(), client = useQueryClient();
  const settings = useQuery({ queryKey: ['progress-settings'], queryFn: ({ signal }) => progressApi.settings(signal), refetchOnWindowFocus: false });
  const models = useQuery({ queryKey: ['pi-models'], queryFn: api.piModels });
  const available = (models.data?.models ?? []).filter(piModelCanRunTask);
  const [saved, setSaved] = useState<Settings | null>(null), [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false), [reloading, setReloading] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const inFlight = useRef(false);
  const dirty = Boolean(saved && draft && JSON.stringify(toDraft(saved)) !== JSON.stringify(draft));
  const busy = saving || reloading;
  useTrackSettings(dirty, busy, tracking);
  const directory = query.isError ? undefined : query.data?.roles?.progress;
  function load(value: Settings) { setSaved(value); setDraft(toDraft(value)); }
  useEffect(() => { if (settings.data && !saved) load(settings.data); }, [settings.data, saved]);

  function patch(value: Partial<Draft>) { setDraft(old => old && ({ ...old, ...value })); setNotice(''); }
  async function save() {
    if (!draft || !directory || inFlight.current) return;
    if (draft.prompt !== null && !draft.prompt.trim()) { setError('请填写自定义提示词，或选择恢复默认。'); return; }
    inFlight.current = true; setSaving(true); setError(''); setNotice('');
    try {
      const value = await progressApi.saveSettings({ ...draft, prompt: draft.prompt ?? '' });
      client.setQueryData(['progress-settings'], value);
      load(value); setNotice('已保存成果整理配置。');
    } catch (cause) { setError(errorText(cause)); }
    finally { inFlight.current = false; setSaving(false); }
  }
  async function reload() {
    if (inFlight.current) return;
    if (dirty && !await new Promise<boolean>(resolve => modal.confirm({
      title: '放弃草稿并重新读取？', okText: '放弃并读取', cancelText: '继续编辑', onOk: () => resolve(true), onCancel: () => resolve(false),
    }))) return;
    if (inFlight.current) return;
    inFlight.current = true; setReloading(true);
    try {
      const result = await settings.refetch({ throwOnError: true });
      if (result.data) { load(result.data); setError(''); setNotice('已重新读取。'); }
    } catch (cause) { setError(errorText(cause)); }
    finally { inFlight.current = false; setReloading(false); }
  }
  if (!draft || !saved) return settings.isError
    ? <Alert type="error" title="无法读取成果整理配置" description={errorText(settings.error)} action={<Button onClick={() => void reload()}>重试</Button>} />
    : <LoadingState label="正在读取成果整理配置" />;
  const selected = draft.pi_model?.model_id ?? '';
  return <>
    {error && <Alert className={styles.feedback} type="error" showIcon title={error} action={<Button disabled={busy} onClick={() => void reload()}>重新读取</Button>} />}
    {notice && <Alert className={styles.feedback} type="success" showIcon role="status" title={notice + lifecycle} />}
    <AgentWorkbench role="progress" choices={['progress']} onRole={() => undefined}
      intro="管理任务资料整理规则和成果树写入权限。" note={lifecycle} query={query} badge={'版本 ' + saved.revision}
      actions={() => <>{dirty && <small className={styles.unsaved}>未保存</small>}<Button size="small" type="primary"
        loading={saving} disabled={busy || !dirty || !directory} onClick={() => void save()}>保存成果整理</Button></>}
      render={(panel, source) => panel === 'prompt' ? <PromptEditor role="成果整理 Agent" value={draft.prompt ?? saved.default_prompt ?? ''}
        onChange={prompt => patch({ prompt })} isDefault={draft.prompt === null} resetDefault={() => patch({ prompt: null })} disabled={busy} />
        : panel === 'tools' ? <ToolPermissions role="progress" directory={directory} query={query}
          initial={source} enabled={draft.enabled_tools} onChange={enabled_tools => patch({ enabled_tools })} disabled={busy} />
        : <><div className={styles.panelHeading}><div><h3>运行设置</h3><p>为未来启用成果整理的任务选择模型。</p></div></div>
          <section className={styles.formBlock}><h4>整理模型</h4>
            {models.isError && <Alert type="warning" title="无法读取模型目录" action={<Button onClick={() => void models.refetch()}>重试</Button>} />}
            <div className={styles.fields}><label className={styles.field}><span>模型来源</span><Select aria-label="成果整理模型"
              disabled={busy || models.isError || models.isLoading} value={selected}
              options={[{ value: '', label: '沿用任务模型' }, ...available.map(item => ({ value: item.id, label: item.label })),
                ...(selected && !available.some(item => item.id === selected) ? [{ value: selected, label: '已配置模型 · 暂不可用', disabled: true }] : [])]}
              onChange={id => { const model = available.find(item => item.id === id); patch({ pi_model: model ? { model_id: id, model_revision: model.model_revision } : null }); }} />
            </label></div><Link to="/models" className={styles.textLink}>前往模型管理</Link>
          </section><dl className={styles.values}><div><dt>费用边界</dt><dd>整理费用由任务内独立预算控制。</dd></div></dl>
          <p className={styles.note}><InfoCircleOutlined /> {lifecycle}</p></>}
    />
  </>;
}
