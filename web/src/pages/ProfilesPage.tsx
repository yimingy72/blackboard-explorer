import { Alert, App, Button, Checkbox, Collapse, Form, Input, Spin, Tabs, Segmented } from 'antd';
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
type WorkerDraft = {
    prompt: string;
    tools: WorkerTools;
};
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
    const { modal } = App.useApp();
    const confirmDiscard = (title: string) => new Promise<boolean>((resolve) => { modal.confirm({ title, okText: "放弃修改", cancelText: "继续编辑", onOk: () => resolve(true), onCancel: () => resolve(false) }); });
    const navigate = useNavigate();
    const location = useLocation();
    const settings = useQuery({ queryKey: ['worker-settings'], queryFn: api.getWorkerSettings, refetchOnWindowFocus: false });
    const [snapshot, setSnapshot] = useState<WorkerSettings | null>(null);
    const [drafts, setDrafts] = useState<Drafts | null>(null);
    const [runtime, setRuntime] = useState<RuntimeInput | null>(null);
    const [role, setRole] = useState<WorkerRole>('explore');
    const [section, setSection] = useState<'worker' | 'ctf' | 'models' | 'mcp'>('worker');
    const [catalogBusy, setCatalogBusy] = useState(false);
  const [catalogDirty, setCatalogDirty] = useState(false);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    useEffect(() => {
        if (settings.data && !snapshot) {
            setSnapshot(settings.data);
            setDrafts(draftsFrom(settings.data.profile));
            setRuntime(runtimeDraft(settings.data.profile));
        }
    }, [settings.data, snapshot]);
    useEffect(() => {
        if (settings.error instanceof ApiError && settings.error.status === 401)
            navigate('/login', { replace: true, state: { from: location.pathname } });
    }, [settings.error, navigate, location.pathname]);
    const workerDirty = Boolean(snapshot && drafts && roles.some((item) => JSON.stringify(drafts[item]) !== JSON.stringify(workerDraft(snapshot.profile, item))));
    const runtimeDirty = Boolean(snapshot && runtime && JSON.stringify(runtime) !== JSON.stringify(runtimeDraft(snapshot.profile)));
    const dirty = workerDirty || runtimeDirty;
    useEffect(() => {
        if (!dirty && !catalogDirty)
            return;
        const prevent = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
        window.addEventListener('beforeunload', prevent);
        return () => window.removeEventListener('beforeunload', prevent);
    }, [dirty, catalogDirty]);
    async function chooseSection(next: typeof section) {
        if (saving || catalogBusy || next === section)
            return;
        if ((dirty || catalogDirty) && !await confirmDiscard('当前配置尚未保存，确定放弃本次修改吗？'))
            return;
        if (dirty && snapshot) {
            setDrafts(draftsFrom(snapshot.profile));
            setRuntime(runtimeDraft(snapshot.profile));
        }
        setCatalogDirty(false);
        setSection(next);
        setError('');
        setNotice('');
    }
    async function saveWorker() {
        if (!snapshot || !drafts)
            return;
        setSaving(true);
        setError('');
        setNotice('');
        try {
            const saved = await api.saveWorkerSettings(role, snapshot.revision, drafts[role].prompt, drafts[role].tools);
            setSnapshot(saved);
            setDrafts((current) => current ? { ...current, [role]: workerDraft(saved.profile, role) } : null);
            setNotice('已保存并应用。提示词将在运行中 Agent 的下一次模型调用生效；工具设置对新任务生效。');
        }
        catch (cause) {
            setError(cause instanceof ApiError && cause.status === 409 ? '配置已在其他窗口更新。当前草稿已保留，请复制内容后重新读取。' : message(cause));
        }
        finally {
            setSaving(false);
        }
    }
    async function saveRuntime() {
        if (!snapshot || !runtime)
            return;
        setSaving(true);
        setError('');
        setNotice('');
        try {
            const saved = await api.saveRuntimeSettings(snapshot.revision, runtime);
            setSnapshot(saved);
            setRuntime(runtimeDraft(saved.profile));
            setNotice('全局调度与执行环境已保存，将对新任务生效。');
        }
        catch (cause) {
            setError(cause instanceof ApiError && cause.status === 409 ? '配置已在其他窗口更新。当前草稿已保留，请复制内容后重新读取。' : message(cause));
        }
        finally {
            setSaving(false);
        }
    }
    async function reload() {
        if (saving || catalogBusy) return;
        if ((dirty || catalogDirty) && !await confirmDiscard('当前草稿尚未保存，确定放弃并重新读取吗？'))
            return;
        const result = await settings.refetch();
        if (result.data) {
            setSnapshot(result.data);
            setDrafts(draftsFrom(result.data.profile));
            setRuntime(runtimeDraft(result.data.profile));
            setError('');
            setNotice('已重新读取。');
        }
    }
    const current = drafts?.[role];
    const currentDirty = Boolean(snapshot && current && JSON.stringify(current) !== JSON.stringify(workerDraft(snapshot.profile, role)));
    return <div className={styles.page}>
      <header className={styles.pageHeader}><h1>Agent 配置</h1></header>
      <Tabs activeKey={section} onChange={(value) => void chooseSection(value as typeof section)} items={[{ key: 'worker', label: 'Worker 配置', disabled: saving || catalogBusy }, { key: 'ctf', label: 'CTF 配置', disabled: saving || catalogBusy }, { key: 'models', label: '模型配置', disabled: saving || catalogBusy }, { key: 'mcp', label: 'MCP 工具', disabled: saving || catalogBusy }]} />
      {section === 'ctf' ? <CtfSettings onDirtyChange={setCatalogDirty} onBusyChange={setCatalogBusy} /> : section === 'models' ? <PlatformCatalog key="models" kind="models" onDirtyChange={setCatalogDirty} onBusyChange={setCatalogBusy} /> : section === 'mcp' ? <PlatformCatalog key="mcp" kind="mcp" onDirtyChange={setCatalogDirty} onBusyChange={setCatalogBusy} /> : <>
        {error && <Alert type="error" showIcon title={error} action={<Button onClick={() => void reload()}>重新读取</Button>} className={styles.feedback} />}
        {notice && <Alert role="status" type="success" showIcon title={notice} className={styles.feedback} />}
        {settings.isLoading && !snapshot && <Spin description="正在读取 Worker 配置…"><div className={styles.loading} /></Spin>}
        {settings.isError && !snapshot && <Alert type="error" title={`配置读取失败：${message(settings.error)}`} action={<Button onClick={() => void reload()}>重试</Button>} />}
        {snapshot && drafts && runtime && current && <>
          <section className={styles.directWorker}>
            <div className={styles.directActions}><Segmented aria-label="Worker" value={role} disabled={saving} options={workers.map(item=>({value:item.id,label:item.id[0].toUpperCase()+item.id.slice(1)}))} onChange={value=>{setRole(value as WorkerRole);setError('');setNotice('');}} /><div className={styles.headerActions}><span className={controls.hint}>{currentDirty?'未保存':'下一次调用生效'}</span><Button size="small" type="primary" loading={saving} disabled={saving||!currentDirty} onClick={()=>void saveWorker()}>保存并应用</Button></div></div>
            <WorkerPrompts key={role} role={role} prompt={current.prompt} tools={current.tools} disabled={saving} onPromptChange={prompt=>setDrafts({...drafts,[role]:{...current,prompt}})} onToolsChange={tools=>setDrafts({...drafts,[role]:{...current,tools}})} />
          </section>
          <Collapse className={styles.runtimeDetails} items={[{ key: 'runtime', label: `全局调度与执行环境${runtimeDirty ? ' · 未保存' : ''}`, children: <Form component={false} disabled={saving}><div className={styles.formGrid}>
            {Object.entries(runtime.params).map(([key, value]) => key === 'derive_enabled' ? <Checkbox key={key} checked={Boolean(value)} onChange={(event) => setRuntime({ ...runtime, params: { ...runtime.params, derive_enabled: event.target.checked } })}>启用主动并行推导</Checkbox> : typeof value === 'number' || key === 'close_reserve_ratio' ? <label className={controls.field} key={key}><span className={controls.label}>{paramLabels[key] ?? key}</span><Input type="number" min="0" step={key === 'close_reserve_ratio' ? 'any' : '1'} value={String(value)} onChange={(event) => setRuntime({ ...runtime, params: { ...runtime.params, [key]: Number(event.target.value) } })} /></label> : null)}
            <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>执行镜像</span><Input value={runtime.exec_image} onChange={(event) => setRuntime({ ...runtime, exec_image: event.target.value })} /></label>
            <label className={controls.field}><span className={controls.label}>CPU</span><Input type="number" min="0.1" step="any" value={runtime.exec_resources.cpus} onChange={(event) => setRuntime({ ...runtime, exec_resources: { ...runtime.exec_resources, cpus: Number(event.target.value) } })} /></label>
            <label className={controls.field}><span className={controls.label}>内存</span><Input value={runtime.exec_resources.mem} onChange={(event) => setRuntime({ ...runtime, exec_resources: { ...runtime.exec_resources, mem: event.target.value } })} /></label>
            <label className={controls.field}><span className={controls.label}>进程上限</span><Input type="number" min="1" step="1" value={runtime.exec_resources.pids} onChange={(event) => setRuntime({ ...runtime, exec_resources: { ...runtime.exec_resources, pids: Number(event.target.value) } })} /></label>
            <div className={`${styles.editorActions} ${styles.wideField}`}><span className={controls.hint}>新任务生效</span><Button type="primary" loading={saving} disabled={saving||!runtimeDirty} onClick={() => void saveRuntime()}>保存全局设置</Button></div>
          </div></Form> }]} />
        </>}
      </>}
    </div>;
}
