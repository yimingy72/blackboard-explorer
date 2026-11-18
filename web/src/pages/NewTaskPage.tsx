import { Alert, Button, Checkbox, Collapse, Form, Input, Segmented, Select } from 'antd';
import { ArrowLeftOutlined, DeleteOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, ApiError, piModelCanRunTask, type CtfTaskCreateInput, type TaskCreateInput } from '../api/client';
import styles from './NewTaskPage.module.css';
import controls from '../styles/controls.module.css';
import TaskAttachments from '../components/TaskAttachments';
import { progressApi } from '../progress/api';
type TaskMode = 'blackboard' | 'ctf';
type Thinking = 'inherit' | Exclude<NonNullable<CtfTaskCreateInput['reasoning_effort']>, 'none'>;
const thinkingOptions: { value: Thinking; label: string }[] = [
    { value: 'inherit', label: '沿用角色配置' }, { value: 'off', label: '关闭' },
    { value: 'minimal', label: '最低' }, { value: 'low', label: '低' },
    { value: 'medium', label: '中' }, { value: 'high', label: '高' },
];
type AcceptanceDraft = {
    key: number;
    desc: string;
};
export default function NewTaskPage() {
    const navigate = useNavigate();
    const location = useLocation();
    const nextKey = useRef(2);
    const [selectedMode, setSelectedMode] = useState<TaskMode>();
    const [maxTeammates, setMaxTeammates] = useState('4');
    const [completionRequirements, setCompletionRequirements] = useState('');
    const [goal, setGoal] = useState('');
    const [name, setName] = useState('');
    const [context, setContext] = useState('');
    const [acceptance, setAcceptance] = useState<AcceptanceDraft[]>([{ key: 1, desc: '' }]);
    const [maxCost, setMaxCost] = useState('');
    const [maxMinutes, setMaxMinutes] = useState('60');
    const [maxAgents, setMaxAgents] = useState('5');
    const [progressEnabled, setProgressEnabled] = useState(false);
    const [progressCap, setProgressCap] = useState('');
    const progressChoice = useRef<{ enabled: boolean; max_cost: string; currency: "USD" } | null>(null);
    const [allowlist, setAllowlist] = useState('');
    const [modelId, setModelId] = useState('');
    const [thinking, setThinking] = useState<Thinking>('inherit');
    const [pending, setPending] = useState(false);
    const [formError, setFormError] = useState('');
    const [uploadsBusy, setUploadsBusy] = useState(false);
    const [uploadsInvalid, setUploadsInvalid] = useState(false);
    const group = useRef<Promise<string> | null>(null);
    const [inputIds, setInputIds] = useState<string[]>([]);
    const [privateSourceId, setPrivateSourceId] = useState<string>();
    const created = useRef(false);
    const alive = useRef(true);
    const [groupExpired, setGroupExpired] = useState(false);
    const submitted = useRef<TaskCreateInput | null>(null);
    const [locked, setLocked] = useState(false);
    const attachmentsStatus = useRef((busy: boolean, invalid: boolean, ids: string[], sourceId?: string) => { setUploadsBusy(busy); setUploadsInvalid(invalid); setInputIds(ids); setPrivateSourceId(sourceId); }).current;
    async function ensureGroup(): Promise<string> {
        setSelectedMode(current => current ?? mode);
        if (!group.current)
            group.current = api.createInputGroup().then((value) => value.id).catch((error) => { group.current = null; throw error; });
        return group.current;
    }
    function resetGroup() {
        group.current = null;
        setGroupExpired(false);
        setFormError('');
    }
    function discardAttachments() {
        if (pending || locked) return;
        const previous = group.current;
        resetGroup();
        setInputIds([]);
        setPrivateSourceId(undefined);
        setUploadsBusy(false);
        setUploadsInvalid(false);
        void previous?.then((id) => api.deleteInputGroup(id)).catch(() => { });
    }
    useEffect(() => {
        alive.current = true;
        return () => {
            alive.current = false;
            if (!created.current)
                void group.current?.then((id) => api.deleteInputGroup(id)).catch(() => { });
        };
    }, []);
    const capabilities = useQuery({ queryKey: ['task-creation-capabilities'], queryFn: api.taskCreationCapabilities });
    const publication = capabilities.data;
    const blackboardAvailable = publication?.blackboard?.available ?? (publication?.mode === 'legacy');
    const mode = selectedMode ?? (publication?.mode === 'native-ctf' && !blackboardAvailable ? 'ctf' : 'blackboard');
    const nativeTask = mode === 'ctf';
    const nativeEnvironment = nativeTask ? publication?.native_ctf : null;
    const sharedInputs = nativeEnvironment?.shared_inputs;
    const privateVpn = nativeEnvironment?.vpn?.profile === 'openvpn-task-ipv4-tcp-v1';
    const creationBlocked = !capabilities.isSuccess || (nativeTask ? !nativeEnvironment : !blackboardAvailable);
    const attachmentsPreventSwitch = uploadsBusy || uploadsInvalid || inputIds.length > 0 || Boolean(privateSourceId);
    function changeMode(next: TaskMode) {
        setSelectedMode(next);
        setFormError('');
    }
    const piDirectory = useQuery({ queryKey: ['pi-models'], queryFn: api.piModels, refetchOnWindowFocus: true });
    const available = (piDirectory.data?.models ?? []).filter(piModelCanRunTask)
        .map(item => ({ id: item.id, label: item.label, reasoning: item.reasoning, reference: { pi_model: { model_id: item.id, model_revision: item.model_revision } } }));
    const selectedModel = available.find(item => item.id === modelId);
    const supportsThinking = Boolean(selectedModel?.reasoning);
    const currencyLabel = 'USD';
    const modelPending = piDirectory.isLoading;
    const modelError = piDirectory.error;
    const modelUnavailable = Boolean(modelError) || piDirectory.data?.enabled === false;
    const refreshModels = () => void piDirectory.refetch();
    const authError = capabilities.error ?? modelError;
    const unauthorized = authError instanceof ApiError && authError.status === 401;
    const onlyModel = available.length === 1 ? available[0].id : undefined;
    useEffect(() => {
        if (!modelId && onlyModel) setModelId(onlyModel);
    }, [modelId, onlyModel]);
    useEffect(() => {
        if (!supportsThinking) {
            setThinking(current => current === 'off' ? 'off' : 'inherit');
        }
    }, [supportsThinking]);
    useEffect(() => {
        if (unauthorized)
            navigate('/login', { replace: true, state: { from: location.pathname } });
    }, [unauthorized, navigate, location.pathname]);
    function addAcceptance() {
        setAcceptance((items) => [...items, { key: nextKey.current++, desc: '' }]);
    }
    function updateAcceptance(key: number, desc: string) {
        setAcceptance((items) => items.map((item) => item.key === key ? { ...item, desc } : item));
    }
    async function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (pending || uploadsBusy || (!submitted.current && creationBlocked))
            return;
        setFormError('');
        if (uploadsInvalid) {
            setFormError('请重试或移除上传失败的附件。');
            return;
        }
        let input = submitted.current;
        if (!input) {
            const descriptions = acceptance.map((item) => item.desc.trim());
            if (!goal.trim() || (mode === 'blackboard' && descriptions.some((value) => !value))) {
                setFormError(mode === 'ctf' ? '请填写任务目标。' : '请填写任务目标和每一项验收条件。');
                return;
            }
            const cost = Number(maxCost);
            const minutes = Number(maxMinutes);
            const agents = Number(mode === 'ctf' ? maxTeammates : maxAgents);
            if (!Number.isFinite(cost) || cost <= 0 || !Number.isInteger(minutes) || minutes <= 0 || !Number.isInteger(agents) || agents < (mode === 'ctf' ? 0 : 1)) {
                setFormError(mode === 'ctf' ? '预算金额和时长必须大于 0，队友数必须是非负整数。' : '预算金额必须大于 0，时长和并发数必须是正整数。');
                return;
            }
            if (!selectedModel) {
                setFormError('请选择已配置费率且可用于任务的模型。');
                return;
            }
            if (nativeTask && ((!sharedInputs && inputIds.length) || agents > 20 || agents < 1)) {
                setFormError(!sharedInputs && inputIds.length ? '此环境不支持附件，请返回后新建任务。' : '原生 CTF 需要 1 至 20 个 Worker 席位。');
                return;
            }
            if (nativeTask && privateSourceId && !privateVpn) {
                setFormError('此环境不再支持已选的私有 .ovpn 文件，请移除后重试。');
                return;
            }
            if (mode === 'blackboard' && progressEnabled && (!Number.isFinite(Number(progressCap)) || Number(progressCap) <= 0)) {
                setFormError('请填写大于0的独立成果整理上限。'); return;
            }
            const common = {
                name: name.trim() || null,
                goal: goal.trim(),
                ...selectedModel.reference,
                egress_allowlist: nativeTask ? [] : [...new Set(allowlist.split(/[\n,]/).map((item) => item.trim()).filter(Boolean))],
            };
            input = mode === 'ctf' ? {
                ...common, mode: 'ctf', agent_profile: 'ctf',
                completion_requirements: completionRequirements.trim() || null,
                budget: { currency: "USD", max_cost: maxCost, max_minutes: minutes },
                ctf_options: { max_teammates: agents },
                ...(thinking !== 'inherit' && (selectedModel.reasoning || thinking === 'off') && { reasoning_effort: thinking }),
                ...(privateSourceId && { native_vpn_source_id: privateSourceId }),
                ...(nativeEnvironment && { native_environment: { id: nativeEnvironment.id, revision: nativeEnvironment.revision, publication_stamp: nativeEnvironment.publication_stamp } }),
            } : {
                ...common, mode: 'blackboard', agent_profile: 'default',
                domain_context: context.trim() || null,
                acceptance: descriptions.map((desc, index) => ({ id: `A${index + 1}`, desc })),
                budget: { currency: "USD", max_cost: maxCost, max_minutes: minutes, max_concurrent_agents: agents },
            };
        }
        setPending(true);
        try {
            const inputGroupId = submitted.current?.input_group_id ?? await ensureGroup();
            const ready = submitted.current ?? { ...input, input_group_id: inputGroupId, input_file_ids: inputIds, ...(sharedInputs && inputIds.length ? { public_input_ids: inputIds } : {}) };
            if (!alive.current)
                return;
            submitted.current = ready;
            progressChoice.current ??= { enabled: mode === 'blackboard' && progressEnabled, max_cost: progressCap, currency: "USD" };
            const result = await api.createTask(ready);
            created.current = true;
            if (progressChoice.current.enabled) {
                try { await progressApi.configure(result.id, progressChoice.current); }
                catch { if (alive.current) navigate(`/tasks/${result.id}/progress`, { replace: true, state: { progressSetupError: '任务已创建，成果整理尚未启用。可在此重试启用。' } }); return; }
            }
            if (alive.current)
                navigate(`/tasks/${result.id}`, { replace: true });
        }
        catch (cause) {
            const uncertain = submitted.current && (!(cause instanceof ApiError) || cause.status >= 500);
            if (!uncertain) {
                submitted.current = null;
                progressChoice.current = null;
            }
            if (!alive.current)
                return;
            if (!uncertain && cause instanceof ApiError && [404, 410].includes(cause.status))
                setGroupExpired(true);
            if (!uncertain && nativeTask && cause instanceof ApiError && cause.status === 409)
                void capabilities.refetch();
            setLocked(Boolean(uncertain));
            setFormError(uncertain ? '创建结果未确认。请重试同一请求，不会重复创建任务。' : cause instanceof ApiError ? cause.message : '任务创建失败，请稍后重试。');
        }
        finally {
            if (alive.current)
                setPending(false);
        }
    }
    const modelLabel = (item: typeof available[number]) =>
        nativeTask && available.filter(model => model.label === item.label).length === 1 ? item.label : `${item.label} · ${item.id}`;
    const modelSelect = <Select id="model" aria-label="任务模型" value={selectedModel?.id} placeholder="选择可用于任务的模型" loading={modelPending}
      options={available.map(item => ({ value: item.id, label: modelLabel(item) }))} onChange={setModelId} />;
    const refreshButton = <Button type="text" size={nativeTask ? 'middle' : 'small'} icon={<ReloadOutlined />} aria-label="刷新模型" loading={piDirectory.isFetching} onClick={refreshModels} />;
    return <div className={styles.page}>
      <header className={styles.heading}><div className={styles.headingIdentity}><Button type="text" icon={<ArrowLeftOutlined />} aria-label="返回任务" onClick={() => navigate('/tasks')} /><h1>创建任务</h1></div>{capabilities.isSuccess && <div className={styles.modePicker}>
        <Segmented<TaskMode> aria-label="任务模式" value={mode} onChange={changeMode} disabled={pending || locked || attachmentsPreventSwitch}
          options={[{ value: 'blackboard', label: '黑板模式' }, { value: 'ctf', label: 'CTF 模式' }]} />
        {attachmentsPreventSwitch && !pending && !locked && <p className={controls.hint}>{uploadsBusy ? '附件处理完成后可切换模式。' : '移除已选附件后可切换模式。'}</p>}
      </div>}</header>
      {creationBlocked && !locked && <Alert
        type={capabilities.isPending ? 'info' : 'warning'}
        title={capabilities.isPending ? '正在读取创建选项' : capabilities.isError ? '无法读取创建选项' : nativeTask ? '当前没有已发布的原生 CTF 环境' : '黑板模式暂不可用'}
        description={!nativeTask && capabilities.isSuccess ? publication?.blackboard?.reason : undefined}
        action={!capabilities.isPending && (attachmentsPreventSwitch
          ? <Button size="small" disabled={pending} onClick={discardAttachments}>移除附件</Button>
          : <Button size="small" onClick={() => void capabilities.refetch()}>刷新</Button>)}
      />}
      {(!creationBlocked || locked) && <form onSubmit={submit} className={styles.form} aria-busy={pending}>
        <Form component={false} layout="vertical" disabled={pending || locked}>
          <div className={nativeTask ? `${styles.layout} ${styles.ctfLayout}` : styles.layout}>
            <section className={styles.editor} aria-label="任务内容">
              {nativeTask && <h2 className={styles.panelTitle}>任务内容</h2>}
              <Form.Item label="任务名" htmlFor="task-name"><Input id="task-name" value={name} maxLength={100} onChange={(event) => setName(event.target.value)} placeholder="为本次任务命名" /></Form.Item>
              <Form.Item label="任务目标" htmlFor="goal" required><Input.TextArea id="goal" autoSize={{ minRows: 4, maxRows: 10 }} value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="描述要解决的问题、范围和预期结果" required /></Form.Item>
              {!nativeTask && <Form.Item label="领域背景" htmlFor="domain-context"><Input.TextArea id="domain-context" autoSize={{ minRows: 2, maxRows: 6 }} value={context} onChange={(event) => setContext(event.target.value)} placeholder="已知信息、资料范围或执行边界（可选）" /></Form.Item>}
              {mode === 'ctf' ? <Form.Item label="完成要求" htmlFor="completion-requirements"><Input.TextArea id="completion-requirements" autoSize={{ minRows: 3, maxRows: 8 }} value={completionRequirements} onChange={(event) => setCompletionRequirements(event.target.value)} placeholder="需要完成的题目、证据或交付内容" /></Form.Item> : <section className={styles.acceptance} aria-label="验收条件"><h2>验收条件</h2>{acceptance.map((item, index) => <div className={styles.acceptanceRow} key={item.key}><span>A{index + 1}</span><Input.TextArea aria-label={`验收条件 ${index + 1}`} id={`acceptance-${item.key}`} autoSize={{ minRows: 1, maxRows: 5 }} value={item.desc} onChange={(event) => updateAcceptance(item.key, event.target.value)} placeholder="说明如何判断这一项已完成" required /><Button type="text" icon={<DeleteOutlined />} aria-label={`删除验收条件 ${index + 1}`} disabled={pending || locked || acceptance.length === 1} onClick={() => setAcceptance((items) => items.filter((entry) => entry.key !== item.key))} /></div>)}<Button type="dashed" icon={<PlusOutlined />} onClick={addAcceptance}>添加验收条件</Button></section>}
              {nativeTask && !sharedInputs && !privateVpn && !attachmentsPreventSwitch ? <p className={controls.hint}>此环境暂不支持初始附件。</p> : <section className={styles.attachments} aria-label="初始附件">
                <TaskAttachments ensureGroup={ensureGroup} resetGroup={resetGroup} groupExpired={groupExpired} disabled={pending || locked}
                  limits={sharedInputs ?? undefined} allowShared={!nativeTask || Boolean(sharedInputs)} privateVpn={nativeTask ? privateVpn : undefined} onStatus={attachmentsStatus}
                  description={nativeTask ? [
                      sharedInputs ? `共享附件对任务团队可见。单件最多 ${sharedInputs.max_file_bytes / 1048576} MiB，合计 ${sharedInputs.max_total_bytes / 1048576} MiB，最多 ${sharedInputs.max_files} 件。` : '此环境暂不支持共享附件。',
                      privateVpn && '可另添加一个私有 .ovpn 文件，连接时校验配置。',
                  ].filter(Boolean).join(' ') : undefined} />
              </section>}
            </section>
            <aside className={styles.sidebar} aria-label="任务设置"><h2 className={styles.panelTitle}>运行设置</h2>
              {nativeTask && <Form.Item label="平台模型" htmlFor="model" required>
                <div className={styles.modelField}>{modelSelect}{refreshButton}</div>
              </Form.Item>}
              {nativeTask && <Form.Item label="思考强度" htmlFor="thinking-effort" extra={selectedModel && !selectedModel.reasoning ? '所选模型不支持调整思考强度。' : undefined}>
                <Select<Thinking> id="thinking-effort" aria-label="思考强度" value={thinking} onChange={setThinking} disabled={pending || locked || !selectedModel}
                  options={thinkingOptions.map(option => ({ ...option, disabled: !selectedModel?.reasoning && !['inherit', 'off'].includes(option.value) }))} />
              </Form.Item>}
              <Form.Item label={`金额阈值（${currencyLabel}）`} htmlFor="max-cost" required
                extra={nativeTask ? <span className={styles.budgetHint} id="task-budget-hint">达到阈值后停止发起新调用；在途调用仍会计费，可能超额。</span> : undefined}>
                <Input id="max-cost" type="number" min="0.000001" step="any" inputMode="decimal" value={maxCost} onChange={(event) => setMaxCost(event.target.value)} required prefix="US$" aria-describedby={nativeTask ? 'task-budget-hint' : undefined} />
              </Form.Item>
              <div className={styles.settingsGrid}><Form.Item label="时长上限（分钟）" htmlFor="max-minutes" required><Input id="max-minutes" type="number" min="1" step="1" value={maxMinutes} onChange={(event) => setMaxMinutes(event.target.value)} required /></Form.Item>
              {mode === 'ctf' ? <Form.Item label="队友上限（不含 Lead）" htmlFor="max-teammates"><Input id="max-teammates" type="number" min={1} max={20} step="1" value={maxTeammates} onChange={(event) => setMaxTeammates(event.target.value)} required /></Form.Item> : <Form.Item label="并发 Agent 上限" htmlFor="max-agents" required><Input id="max-agents" type="number" min="1" step="1" value={maxAgents} onChange={(event) => setMaxAgents(event.target.value)} required /></Form.Item>}</div>
              {!nativeTask && <>
                <Form.Item label={<span className={styles.modelHeading}>平台模型{refreshButton}</span>} htmlFor="model" required>{modelSelect}</Form.Item>
                <p className={controls.hint}>到达金额阈值后停止新调用；已在途的调用仍会计费，实际金额可能超过阈值。</p>
              </>}
              {modelUnavailable && !unauthorized && <Alert type="error" title="模型目录暂不可用" action={<Button size="small" onClick={refreshModels}>重试</Button>} />}
              {!modelPending && !modelUnavailable && !available.length && <Alert type="warning" title="尚无可用于任务的模型" description="任务模型需要启用、配置上游密钥和费率，并支持工具调用。" action={<Button href="/models" target="_blank" rel="noopener">模型管理</Button>} />}
              {mode === 'blackboard' && <Form.Item label={<Checkbox checked={progressEnabled} onChange={event => setProgressEnabled(event.target.checked)}>自动整理成果树</Checkbox>}><Input prefix="额外 US$" suffix="独立上限" type="number" aria-label="创建任务成果整理上限" min="0.01" step="any" value={progressCap} disabled={!progressEnabled || pending || locked} onChange={event => setProgressCap(event.target.value)} /><span className={controls.hint}>独立于求解预算，触顶不影响探索。</span></Form.Item>}
              {!nativeTask && <Collapse ghost size="small" items={[{ key: 'network', label: '目标域名（可选）', children: <Input.TextArea aria-label="任务所需域名（记录）" autoSize={{ minRows: 2, maxRows: 5 }} value={allowlist} onChange={(event) => setAllowlist(event.target.value)} placeholder="每行一个域名，或用逗号分隔" /> }]} />}
              <footer className={styles.footer}>{formError && <Alert type="error" title={formError} showIcon />}<div className={styles.footerActions}><Button disabled={false} onClick={() => navigate('/tasks')}>取消</Button><Button aria-label="创建任务" aria-busy={pending} type="primary" htmlType="submit" loading={pending} disabled={pending || uploadsBusy || uploadsInvalid || (!locked && !selectedModel)}>创建任务</Button></div></footer>
            </aside>
          </div>

        </Form>
      </form>}
    </div>;
}
