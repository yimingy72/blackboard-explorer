import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router-dom';
import WorkerPrompts from '../components/WorkerPrompts';
import CtfSettings from '../ctf/CtfSettings';
import { defaultTools, workers } from '../components/workerOptions';
import PlatformCatalog from '../components/PlatformCatalog';
import { api, ApiError, type ProfileInput, type RuntimeInput, type WorkerRole, type WorkerSettings, type WorkerTools } from '../api/client';
import controls from '../styles/controls.module.css';
import styles from './ProfilesPage.module.css';

type WorkerDraft = { prompt: string; tools: WorkerTools };
type Drafts = Record<WorkerRole, WorkerDraft>;
const roles: WorkerRole[] = ['explore', 'derive', 'close'];
const paramLabels: Record<string, string> = {
  close_reserve_ratio: '收尾预算预留比例', conclude_grace_calls: '结束后工具调用宽限次数',
  context_threshold: '交接上下文 token 阈值', delta_max_lines: '增量推送最大行数',
  derive_empty_limit: '连续空推导上限', dispute_notify_depth: '争议通知最大深度',
  explore_max_steps: '单次探索最多模型调用', grace_timeout: '结束宽限（分钟）',
  heartbeat_timeout: '心跳超时（分钟）', intent_max_attempts: '意图最大尝试次数',
  max_consecutive_failures: '连续运行错误上限', seed_max_steps: '种子探索最大步数',
  snapshot_max_lines: 'YAML 快照最大行数',
};
const workerDraft = (profile: ProfileInput, role: WorkerRole): WorkerDraft => ({
  prompt: profile.prompt_templates[role], tools: profile.worker_tools?.[role] ?? defaultTools(role),
});
const runtimeDraft = (profile: ProfileInput): RuntimeInput => ({
  params: profile.params, exec_image: profile.exec_image, exec_resources: profile.exec_resources,
  privileged_allowlist: profile.privileged_allowlist ?? [],
});
const draftsFrom = (profile: ProfileInput): Drafts => ({ explore: workerDraft(profile, 'explore'), derive: workerDraft(profile, 'derive'), close: workerDraft(profile, 'close') });
const message = (error: unknown) => error instanceof Error ? error.message : '操作失败，请稍后重试。';

export default function ProfilesPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const settings = useQuery({ queryKey: ['worker-settings'], queryFn: api.getWorkerSettings, refetchOnWindowFocus: false });
  const [snapshot, setSnapshot] = useState<WorkerSettings | null>(null);
  const [drafts, setDrafts] = useState<Drafts | null>(null);
  const [runtime, setRuntime] = useState<RuntimeInput | null>(null);
  const [role, setRole] = useState<WorkerRole>('explore');
  const [section, setSection] = useState<'worker' | 'ctf' | 'models' | 'mcp'>('worker');
  const [catalogDirty, setCatalogDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    if (settings.data && !snapshot) {
      setSnapshot(settings.data); setDrafts(draftsFrom(settings.data.profile)); setRuntime(runtimeDraft(settings.data.profile));
    }
  }, [settings.data, snapshot]);
  useEffect(() => {
    if (settings.error instanceof ApiError && settings.error.status === 401) navigate('/login', { replace: true, state: { from: location.pathname } });
  }, [settings.error, navigate, location.pathname]);
  const workerDirty = Boolean(snapshot && drafts && roles.some((item) => JSON.stringify(drafts[item]) !== JSON.stringify(workerDraft(snapshot.profile, item))));
  const runtimeDirty = Boolean(snapshot && runtime && JSON.stringify(runtime) !== JSON.stringify(runtimeDraft(snapshot.profile)));
  const dirty = workerDirty || runtimeDirty;
  useEffect(() => {
    if (!dirty && !catalogDirty) return;
    const prevent = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', prevent);
    return () => window.removeEventListener('beforeunload', prevent);
  }, [dirty, catalogDirty]);
  function chooseSection(next: typeof section) {
    if (next === section) return;
    if ((dirty || catalogDirty) && !window.confirm('当前配置尚未保存，确定放弃本次修改吗？')) return;
    if (dirty && snapshot) { setDrafts(draftsFrom(snapshot.profile)); setRuntime(runtimeDraft(snapshot.profile)); }
    setCatalogDirty(false); setSection(next); setError(''); setNotice('');
  }
  async function saveWorker() {
    if (!snapshot || !drafts) return;
    setSaving(true); setError(''); setNotice('');
    try {
      const saved = await api.saveWorkerSettings(role, snapshot.revision, drafts[role].prompt, drafts[role].tools);
      setSnapshot(saved);
      setDrafts((current) => current ? { ...current, [role]: workerDraft(saved.profile, role) } : null);
      setNotice('已保存并应用。提示词将在运行中 Agent 的下一次模型调用生效；工具设置对新任务生效。');
    } catch (cause) { setError(cause instanceof ApiError && cause.status === 409 ? '配置已在其他窗口更新。当前草稿已保留，请复制内容后重新读取。' : message(cause)); }
    finally { setSaving(false); }
  }
  async function saveRuntime() {
    if (!snapshot || !runtime) return;
    setSaving(true); setError(''); setNotice('');
    try {
      const saved = await api.saveRuntimeSettings(snapshot.revision, runtime);
      setSnapshot(saved); setRuntime(runtimeDraft(saved.profile));
      setNotice('全局调度与执行环境已保存，将对新任务生效。');
    } catch (cause) { setError(cause instanceof ApiError && cause.status === 409 ? '配置已在其他窗口更新。当前草稿已保留，请复制内容后重新读取。' : message(cause)); }
    finally { setSaving(false); }
  }
  async function reload() {
    if ((dirty || catalogDirty) && !window.confirm('当前草稿尚未保存，确定放弃并重新读取吗？')) return;
    const result = await settings.refetch();
    if (result.data) { setSnapshot(result.data); setDrafts(draftsFrom(result.data.profile)); setRuntime(runtimeDraft(result.data.profile)); setError(''); setNotice('已重新读取。'); }
  }
  const current = drafts?.[role];
  const currentDirty = Boolean(snapshot && current && JSON.stringify(current) !== JSON.stringify(workerDraft(snapshot.profile, role)));
  return <div className={styles.page}>
    <header className={styles.pageHeader}><h1>Agent 配置</h1></header>
    <nav className={styles.topNav} aria-label="配置中心">{([['worker', 'Worker 配置'], ['ctf', 'CTF 配置'], ['models', '模型配置'], ['mcp', 'MCP 工具']] as const).map(([id, label]) => <button key={id} type="button" className={section === id ? styles.tabActive : styles.tab} aria-current={section === id ? 'page' : undefined} onClick={() => chooseSection(id)}>{label}</button>)}</nav>
    {section === 'ctf' ? <CtfSettings onDirtyChange={setCatalogDirty} /> : section === 'models' ? <PlatformCatalog key="models" kind="models" onDirtyChange={setCatalogDirty} /> : section === 'mcp' ? <PlatformCatalog key="mcp" kind="mcp" onDirtyChange={setCatalogDirty} /> : <>
      {error && <p className={styles.errorBanner} role="alert">{error} <button type="button" onClick={() => void reload()}>重新读取</button></p>}
      {notice && <p className={styles.noticeBanner} role="status">{notice}</p>}
      {settings.isLoading && !snapshot && <p role="status">正在读取 Worker 配置…</p>}
      {settings.isError && !snapshot && <p className={styles.errorBanner} role="alert">配置读取失败：{message(settings.error)} <button type="button" onClick={() => void reload()}>重试</button></p>}
      {snapshot && drafts && runtime && current && <>
        <div className={styles.workerLayout}><div className={styles.workerSidebar}><p className={styles.sidebarLabel}>工作角色</p><div className={styles.workerTabs} role="tablist" aria-label="Worker" aria-orientation="vertical" onKeyDown={(event) => {
          if (!['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const index = roles.indexOf(role);
          const next = event.key === 'Home' ? roles[0] : event.key === 'End' ? roles[2] : roles[(index + (['ArrowDown', 'ArrowRight'].includes(event.key) ? 1 : 2)) % roles.length];
          setRole(next); setError(''); setNotice(''); document.getElementById(`worker-tab-${next}`)?.focus();
        }}>{workers.map((item) => <button key={item.id} type="button" role="tab" id={`worker-tab-${item.id}`} aria-controls={`worker-panel-${item.id}`} tabIndex={role === item.id ? 0 : -1} aria-selected={role === item.id} className={role === item.id ? styles.workerActive : styles.workerTab} onClick={() => { setRole(item.id); setError(''); setNotice(''); }}><span className={styles.roleName}>{item.title}<span className={styles.roleCode}>{item.id[0].toUpperCase() + item.id.slice(1)}</span></span>{JSON.stringify(drafts[item.id]) !== JSON.stringify(workerDraft(snapshot.profile, item.id)) && <small>未保存</small>}</button>)}</div></div>
        <section className={styles.directWorker} role="tabpanel" id={`worker-panel-${role}`} aria-labelledby={`worker-tab-${role}`}><div className={styles.directActions}><span className={controls.hint}>{currentDirty ? '当前 Worker 有未保存修改' : '当前 Worker 已保存'}</span><button type="button" className={`${controls.button} ${controls.primary}`} disabled={saving || !currentDirty} onClick={() => void saveWorker()}>{saving ? '正在保存…' : '保存并应用'}</button></div><WorkerPrompts key={role} role={role} prompt={current.prompt} tools={current.tools} disabled={saving} onPromptChange={(prompt) => setDrafts({ ...drafts, [role]: { ...current, prompt } })} onToolsChange={(tools) => setDrafts({ ...drafts, [role]: { ...current, tools } })} />

        </section></div>
        <details className={styles.runtimeDetails}><summary>全局调度与执行环境{runtimeDirty ? ' · 未保存' : ''}</summary><div className={styles.formGrid}>
          {Object.entries(runtime.params).map(([key, value]) => key === 'derive_enabled' ? <label key={key} className={styles.checkRow}><input type="checkbox" checked={Boolean(value)} onChange={(event) => setRuntime({ ...runtime, params: { ...runtime.params, derive_enabled: event.target.checked } })} />启用主动并行推导（必要完成复核仍保留）</label> : typeof value === 'number' || key === 'close_reserve_ratio' ? <label className={controls.field} key={key}><span className={controls.label}>{paramLabels[key] ?? key}</span><input className={controls.input} type="number" min="0" step={key === 'close_reserve_ratio' ? 'any' : '1'} value={String(value)} onChange={(event) => setRuntime({ ...runtime, params: { ...runtime.params, [key]: Number(event.target.value) } })} /></label> : null)}
          <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>执行镜像</span><input className={controls.input} value={runtime.exec_image} onChange={(event) => setRuntime({ ...runtime, exec_image: event.target.value })} /></label>
          <label className={controls.field}><span className={controls.label}>CPU</span><input className={controls.input} type="number" min="0.1" step="any" value={runtime.exec_resources.cpus} onChange={(event) => setRuntime({ ...runtime, exec_resources: { ...runtime.exec_resources, cpus: Number(event.target.value) } })} /></label>
          <label className={controls.field}><span className={controls.label}>内存</span><input className={controls.input} value={runtime.exec_resources.mem} onChange={(event) => setRuntime({ ...runtime, exec_resources: { ...runtime.exec_resources, mem: event.target.value } })} /></label>
          <label className={controls.field}><span className={controls.label}>进程上限</span><input className={controls.input} type="number" min="1" step="1" value={runtime.exec_resources.pids} onChange={(event) => setRuntime({ ...runtime, exec_resources: { ...runtime.exec_resources, pids: Number(event.target.value) } })} /></label>
          <div className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>执行权限</span><p className={controls.hint}>Explore 可在任务容器内免密使用 sudo。修改共享文件前先复制到自己的工作目录；命令与提权记录随任务保存。环境更新在新建或续跑任务时生效。</p></div>
          <div className={`${styles.directActions} ${styles.wideField}`}><span className={controls.hint}>仅对新任务生效</span><button type="button" className={`${controls.button} ${controls.primary}`} disabled={saving || !runtimeDirty} onClick={() => void saveRuntime()}>{saving ? '正在保存…' : '保存全局设置'}</button></div>
        </div></details>
      </>}
    </>}
  </div>;
}
