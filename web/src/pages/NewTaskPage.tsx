import { Alert, Button, Checkbox, Collapse, Form, Input, Select } from 'antd';
import { ArrowLeftOutlined, DeleteOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, ApiError, type TaskCreateInput } from '../api/client';
import styles from './NewTaskPage.module.css';
import controls from '../styles/controls.module.css';
import TaskAttachments from '../components/TaskAttachments';
import { progressApi } from '../progress/api';
type AcceptanceDraft = {
    key: number;
    desc: string;
};
export default function NewTaskPage() {
    const navigate = useNavigate();
    const location = useLocation();
    const nextKey = useRef(2);
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
    const [pending, setPending] = useState(false);
    const [formError, setFormError] = useState('');
    const [privateVpnFile, setPrivateVpnFile] = useState<File | null>(null);
    const [uploadsBusy, setUploadsBusy] = useState(false);
    const [uploadsInvalid, setUploadsInvalid] = useState(false);
    const group = useRef<Promise<string> | null>(null);
    const [inputIds, setInputIds] = useState<string[]>([]);
    const created = useRef(false);
    const alive = useRef(true);
    const [groupExpired, setGroupExpired] = useState(false);
    const submitted = useRef<TaskCreateInput | null>(null);
    const [locked, setLocked] = useState(false);
    const attachmentsStatus = useRef((busy: boolean, invalid: boolean, ids: string[]) => { setUploadsBusy(busy); setUploadsInvalid(invalid); setInputIds(ids); }).current;
    async function ensureGroup(): Promise<string> {
        if (!group.current)
            group.current = api.createInputGroup().then((value) => value.id).catch((error) => { group.current = null; throw error; });
        return group.current;
    }
    function resetGroup() {
        group.current = null;
        setGroupExpired(false);
        setFormError('');
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
    const nativeDeployment = capabilities.data?.mode === 'native-ctf';
    const nativeEnvironment = capabilities.data?.native_ctf;
    const taskNetwork = nativeEnvironment?.vpn?.profile === 'openvpn-task-ipv4-tcp-v1';
    const sharedInputs = nativeEnvironment?.shared_inputs;
    const creationBlocked = !capabilities.isSuccess || nativeDeployment && !nativeEnvironment;
    const mode = nativeDeployment ? 'ctf' : 'blackboard';
    const models = useQuery({ queryKey: ['gateway-models'], queryFn: api.gatewayModels, refetchOnWindowFocus: true });
    const available = models.data?.status === 'ready' ? models.data.models.filter(item => item.tool_call) : [];
    const selectedModel = available.find(item => item.alias === modelId);
    const currencyLabel = 'USD';
    const authError = capabilities.error ?? models.error;
    const unauthorized = authError instanceof ApiError && authError.status === 401;
    useEffect(() => {
        if (!modelId && models.data?.status === 'ready' && models.data.models.filter(item => item.tool_call).length === 1)
            setModelId(models.data.models.find(item => item.tool_call)!.alias);
    }, [modelId, models.data]);
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
        if (!submitted.current && taskNetwork && privateVpnFile && privateVpnFile.size > 128 * 1024) {
            setFormError('VPN 配置不能超过 128 KiB。');
            return;
        }
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
                setFormError('请选择 AgentGateway 已应用的模型。');
                return;
            }
            if (nativeDeployment && ((!sharedInputs && inputIds.length) || agents > 20 || agents < 1)) {
                setFormError(!sharedInputs && inputIds.length ? '此环境不支持附件，请返回后新建任务。' : '原生 CTF 需要 1 至 20 个 Worker 席位。');
                return;
            }
            if (mode === 'blackboard' && progressEnabled && (!Number.isFinite(Number(progressCap)) || Number(progressCap) <= 0)) {
                setFormError('请填写大于0的独立成果整理上限。'); return;
            }
            const common = {
                name: name.trim() || null,
                goal: goal.trim(),
                domain_context: context.trim() || null,
                gateway_model: { alias: selectedModel.alias, catalog_revision: selectedModel.catalog_revision },
                egress_allowlist: nativeDeployment ? [] : [...new Set(allowlist.split(/[\n,]/).map((item) => item.trim()).filter(Boolean))],
            };
            input = mode === 'ctf' ? {
                ...common, mode: 'ctf', agent_profile: 'ctf',
                completion_requirements: completionRequirements.trim() || null,
                budget: { currency: "USD", max_cost: maxCost, max_minutes: minutes },
                ctf_options: { max_teammates: agents },
                ...(nativeEnvironment && { native_environment: { id: nativeEnvironment.id, revision: nativeEnvironment.revision, publication_stamp: nativeEnvironment.publication_stamp } }),
            } : {
                ...common, agent_profile: 'default',
                acceptance: descriptions.map((desc, index) => ({ id: `A${index + 1}`, desc })),
                budget: { currency: "USD", max_cost: maxCost, max_minutes: minutes, max_concurrent_agents: agents },
            };
        }
        setPending(true);
        try {
            const inputGroupId = submitted.current?.input_group_id ?? await ensureGroup();
            const privateSource = !submitted.current && taskNetwork && privateVpnFile
                ? await api.uploadPrivateVpnSource(inputGroupId, privateVpnFile) : undefined;
            const ready = submitted.current ?? { ...input, input_group_id: inputGroupId, input_file_ids: inputIds, ...(sharedInputs && inputIds.length ? { public_input_ids: inputIds } : {}), ...(privateSource && { native_vpn_source_id: privateSource.source_id }) };
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
            if (!uncertain && nativeDeployment && cause instanceof ApiError && cause.status === 409)
                void capabilities.refetch();
            setLocked(Boolean(uncertain));
            setFormError(uncertain ? '创建结果未确认。请重试同一请求，不会重复创建任务。' : cause instanceof ApiError ? cause.message : '任务创建失败，请稍后重试。');
        }
        finally {
            if (alive.current)
                setPending(false);
        }
    }
    return <div className={styles.page}>
      <header className={styles.heading}><div className={styles.headingIdentity}><Button type="text" icon={<ArrowLeftOutlined />} aria-label="返回任务" onClick={() => navigate('/tasks')} /><h1>创建任务</h1></div>{capabilities.isSuccess && <div className={styles.modePicker}>{nativeDeployment ? <span>原生 CTF 团队</span> : <span>黑板探索</span>}</div>}</header>
      {creationBlocked && !locked && <Alert
        type={capabilities.isPending ? 'info' : 'warning'}
        title={capabilities.isPending ? '正在读取创建选项' : capabilities.isError ? '无法读取创建选项' : '当前没有已发布的原生 CTF 环境'}
        action={!capabilities.isPending && <Button size="small" onClick={() => void capabilities.refetch()}>刷新</Button>}
      />}
      {(!creationBlocked || locked) && <form onSubmit={submit} className={styles.form} aria-busy={pending}>
        <Form component={false} layout="vertical" disabled={pending || locked}>
          <div className={styles.layout}>
            <section className={styles.editor} aria-label="任务内容">
              <Form.Item label="任务名" htmlFor="task-name"><Input id="task-name" value={name} maxLength={100} onChange={(event) => setName(event.target.value)} placeholder="为本次任务命名" /></Form.Item>
              <Form.Item label="任务目标" htmlFor="goal" required><Input.TextArea id="goal" autoSize={{ minRows: 4, maxRows: 10 }} value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="描述要解决的问题、范围和预期结果" required /></Form.Item>
              <Form.Item label="领域背景" htmlFor="domain-context"><Input.TextArea id="domain-context" autoSize={{ minRows: 2, maxRows: 6 }} value={context} onChange={(event) => setContext(event.target.value)} placeholder="已知信息、资料范围或执行边界（可选）" /></Form.Item>
              {mode === 'ctf' ? <Form.Item label="完成要求" htmlFor="completion-requirements"><Input.TextArea id="completion-requirements" autoSize={{ minRows: 3, maxRows: 8 }} value={completionRequirements} onChange={(event) => setCompletionRequirements(event.target.value)} placeholder="需要完成的题目、证据或交付内容" /></Form.Item> : <section className={styles.acceptance} aria-label="验收条件"><h2>验收条件</h2>{acceptance.map((item, index) => <div className={styles.acceptanceRow} key={item.key}><span>A{index + 1}</span><Input.TextArea aria-label={`验收条件 ${index + 1}`} id={`acceptance-${item.key}`} autoSize={{ minRows: 1, maxRows: 5 }} value={item.desc} onChange={(event) => updateAcceptance(item.key, event.target.value)} placeholder="说明如何判断这一项已完成" required /><Button type="text" icon={<DeleteOutlined />} aria-label={`删除验收条件 ${index + 1}`} disabled={pending || locked || acceptance.length === 1} onClick={() => setAcceptance((items) => items.filter((entry) => entry.key !== item.key))} /></div>)}<Button type="dashed" icon={<PlusOutlined />} onClick={addAcceptance}>添加验收条件</Button></section>}
              {nativeDeployment && !sharedInputs ? <p className={controls.hint}>此环境暂不支持初始附件。</p> : <section className={styles.attachments} aria-label="初始附件">
                {sharedInputs && <p className={controls.hint}>所选附件在启动前复制到共享工作区，同任务团队可读。单件 {sharedInputs.max_file_bytes / 1048576} MiB，合计 {sharedInputs.max_total_bytes / 1048576} MiB，最多 {sharedInputs.max_files} 件。</p>}
                <TaskAttachments ensureGroup={ensureGroup} resetGroup={resetGroup} groupExpired={groupExpired} disabled={pending || locked} limits={sharedInputs ?? undefined} onStatus={attachmentsStatus} />
              </section>}
            </section>
            <aside className={styles.sidebar} aria-label="任务设置"><h2>运行设置</h2>
              {taskNetwork && <section className={styles.attachments} aria-label="私有 VPN 配置">
                <label htmlFor="private-vpn-config">私有 VPN 配置（可选）</label>
                <input id="private-vpn-config" type="file" accept=".ovpn" disabled={pending || locked} onChange={(event) => setPrivateVpnFile(event.target.files?.[0] ?? null)} />
                <p className={controls.hint}>Lead 按任务说明选择默认网络或共享 VPN。可以在说明中提供无凭据的 HTTPS 下载地址；上传的配置不会放到共享工作区。</p>
              </section>}
              {nativeEnvironment && <Alert type="info" title={nativeEnvironment.label} description={taskNetwork ? '同一任务入口支持默认网络与 Lead 配置的共享 VPN，Agent 流量均按来源采集。' : nativeEnvironment.vpn ? '此环境由 Lead 配置已发布的 VPN，仅开放已配置的数字 IPv4 TCP 目标。断开后模型、平台控制与文件读取仍可用，求解命令不可用；凭据占用保留到原任务容器全部结束。' : '此环境使用管理员配置的 IPv4 TCP 目标。已封存的逐 Agent 流量可在任务内查看。'} />}
              <Form.Item label={`金额上限（${currencyLabel}）`} htmlFor="max-cost" required><Input id="max-cost" type="number" min="0.000001" step="any" inputMode="decimal" value={maxCost} onChange={(event) => setMaxCost(event.target.value)} required prefix="US$" /></Form.Item>
              <div className={styles.settingsGrid}><Form.Item label="时长上限（分钟）" htmlFor="max-minutes" required><Input id="max-minutes" type="number" min="1" step="1" value={maxMinutes} onChange={(event) => setMaxMinutes(event.target.value)} required /></Form.Item>
              {mode === 'ctf' ? <Form.Item label="队友上限（不含 Lead）" htmlFor="max-teammates"><Input id="max-teammates" type="number" min={nativeDeployment ? 1 : 0} max={nativeDeployment ? 20 : undefined} step="1" value={maxTeammates} onChange={(event) => setMaxTeammates(event.target.value)} required /></Form.Item> : <Form.Item label="并发 Agent 上限" htmlFor="max-agents" required><Input id="max-agents" type="number" min="1" step="1" value={maxAgents} onChange={(event) => setMaxAgents(event.target.value)} required /></Form.Item>}</div>
              <Form.Item label={<span className={styles.modelHeading}>AgentGateway 模型<Button type="text" size="small" icon={<ReloadOutlined />} aria-label="刷新模型" loading={models.isFetching} onClick={() => void models.refetch()} /></span>} htmlFor="model" required>
                <Select id="model" aria-label="AgentGateway 模型" value={selectedModel?.alias} placeholder="选择已应用的模型" loading={models.isLoading}
                  options={available.map(item => ({ value: item.alias, label: `${item.label} · ${item.alias}` }))} onChange={setModelId} />
              </Form.Item>
              {(models.isError || models.data?.status === 'unavailable') && !unauthorized && <Alert type="error" title="AgentGateway 模型目录暂不可用" action={<Button size="small" onClick={() => void models.refetch()}>重试</Button>} />}
              {models.data?.status === 'ready' && !available.length && <Alert type="warning" title="尚无可用于任务的 AgentGateway 模型" action={<Button href="/profiles?section=models">模型管理</Button>} />}
              {mode === 'blackboard' && <Form.Item label={<Checkbox checked={progressEnabled} onChange={event => setProgressEnabled(event.target.checked)}>自动整理成果树</Checkbox>}><Input prefix="额外 US$" suffix="独立上限" type="number" aria-label="创建任务成果整理上限" min="0.01" step="any" value={progressCap} disabled={!progressEnabled || pending || locked} onChange={event => setProgressCap(event.target.value)} /><span className={controls.hint}>独立于求解预算，触顶不影响探索。</span></Form.Item>}
              {!nativeDeployment && <Collapse ghost size="small" items={[{ key: 'network', label: '目标域名（可选）', children: <Input.TextArea aria-label="任务所需域名（记录）" autoSize={{ minRows: 2, maxRows: 5 }} value={allowlist} onChange={(event) => setAllowlist(event.target.value)} placeholder="每行一个域名，或用逗号分隔" /> }]} />}
              <footer className={styles.footer}>{formError && <Alert type="error" title={formError} showIcon />}<div className={styles.footerActions}><Button disabled={false} onClick={() => navigate('/tasks')}>取消</Button><Button aria-label="创建任务" aria-busy={pending} type="primary" htmlType="submit" loading={pending} disabled={pending || uploadsBusy || uploadsInvalid || (!locked && !selectedModel)}>创建任务</Button></div></footer>
            </aside>
          </div>

        </Form>
      </form>}
    </div>;
}
