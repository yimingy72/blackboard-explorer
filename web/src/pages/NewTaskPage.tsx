import { Alert, Button, Collapse, Form, Input, Segmented, Select } from 'antd';
import { ArrowLeftOutlined, DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, ApiError, type TaskCreateInput } from '../api/client';
import styles from './NewTaskPage.module.css';
import controls from '../styles/controls.module.css';
import { reasoningOptions } from '../components/reasoningOptions';
import TaskAttachments from '../components/TaskAttachments';
type AcceptanceDraft = {
    key: number;
    desc: string;
};
export default function NewTaskPage() {
    const navigate = useNavigate();
    const location = useLocation();
    const nextKey = useRef(2);
    const [mode, setMode] = useState<'blackboard' | 'ctf'>('blackboard');
    const [maxTeammates, setMaxTeammates] = useState('4');
    const [completionRequirements, setCompletionRequirements] = useState('');
    const [goal, setGoal] = useState('');
    const [name, setName] = useState('');
    const [context, setContext] = useState('');
    const [acceptance, setAcceptance] = useState<AcceptanceDraft[]>([{ key: 1, desc: '' }]);
    const [maxCost, setMaxCost] = useState('10');
    const [maxMinutes, setMaxMinutes] = useState('60');
    const [maxAgents, setMaxAgents] = useState('5');
    const [allowlist, setAllowlist] = useState('');
    const [modelId, setModelId] = useState('');
    const [reasoningEffort, setReasoningEffort] = useState('');
    const [pending, setPending] = useState(false);
    const [formError, setFormError] = useState('');
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
    const models = useQuery({ queryKey: ['platform-models'], queryFn: api.listPlatformModels });
    const providers = useQuery({ queryKey: ['platform-providers'], queryFn: api.listProviders });
    const selectedModel = models.data?.find((item) => item.name === modelId);
    const provider = providers.data?.find((item) => item.id === selectedModel?.config.provider);
    const efforts = reasoningOptions(provider, selectedModel?.config.model ?? '');
    const currencyLabel = '人民币元';
    const unauthorized = models.error instanceof ApiError && models.error.status === 401;
    useEffect(() => {
        if (!modelId && models.data?.length)
            setModelId(models.data.find((item) => item.is_default && item.enabled)?.name ?? models.data.find((item) => item.enabled)?.name ?? '');
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
        if (pending || uploadsBusy)
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
            if (!selectedModel || !selectedModel.enabled) {
                setFormError('请选择可用的平台模型。');
                return;
            }
            if (reasoningEffort && (!provider?.supports_reasoning_effort || !['none', ...efforts].includes(reasoningEffort))) {
                setFormError('所选模型不支持当前推理强度，请重新选择。');
                return;
            }
            const common = {
                name: name.trim() || null,
                goal: goal.trim(),
                domain_context: context.trim() || null,
                model_id: selectedModel.name,
                model_version: selectedModel.version,
                egress_allowlist: [...new Set(allowlist.split(/[\n,]/).map((item) => item.trim()).filter(Boolean))],
            };
            input = mode === 'ctf' ? {
                ...common, mode: 'ctf', agent_profile: 'ctf',
                completion_requirements: completionRequirements.trim() || null,
                budget: { max_cost: maxCost, max_minutes: minutes },
                ctf_options: { max_teammates: agents },
            } : {
                ...common, agent_profile: 'default',
                acceptance: descriptions.map((desc, index) => ({ id: `A${index + 1}`, desc })),
                budget: { max_cost: maxCost, max_minutes: minutes, max_concurrent_agents: agents },
            };
            if (reasoningEffort)
                input.reasoning_effort = reasoningEffort;
        }
        setPending(true);
        try {
            const ready = submitted.current ?? { ...input, input_group_id: await ensureGroup(), input_file_ids: inputIds };
            if (!alive.current)
                return;
            submitted.current = ready;
            const result = await api.createTask(ready);
            created.current = true;
            if (alive.current)
                navigate(`/tasks/${result.id}`, { replace: true });
        }
        catch (cause) {
            const uncertain = submitted.current && (!(cause instanceof ApiError) || cause.status >= 500);
            if (!uncertain)
                submitted.current = null;
            if (!alive.current)
                return;
            if (!uncertain && cause instanceof ApiError && [404, 410].includes(cause.status))
                setGroupExpired(true);
            setLocked(Boolean(uncertain));
            setFormError(uncertain ? '创建结果未确认。请重试同一请求，不会重复创建任务。' : cause instanceof ApiError ? cause.message : '任务创建失败，请稍后重试。');
        }
        finally {
            if (alive.current)
                setPending(false);
        }
    }
    return <div className={styles.page}>
      <header className={styles.heading}><Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate('/tasks')}>返回任务</Button><h1>创建任务</h1></header>
      <form onSubmit={submit} className={styles.form} aria-busy={pending}>
        <Form component={false} layout="vertical" disabled={pending || locked}>
          <div className={styles.modePicker}><Segmented disabled={pending || locked} aria-label="协作模式" value={mode} options={[{ label: '黑板探索', value: 'blackboard' }, { label: 'CTF 团队', value: 'ctf' }]} onChange={(value) => setMode(value as typeof mode)} /></div>
          <div className={styles.layout}>
            <section className={styles.editor} aria-label="任务内容">
              <Form.Item label="任务名" htmlFor="task-name"><Input id="task-name" value={name} maxLength={100} onChange={(event) => setName(event.target.value)} placeholder="为本次任务命名" /></Form.Item>
              <Form.Item label="任务目标" htmlFor="goal" required><Input.TextArea id="goal" autoSize={{ minRows: 4, maxRows: 10 }} value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="描述要解决的问题、范围和预期结果" required /></Form.Item>
              <Form.Item label="领域背景" htmlFor="domain-context"><Input.TextArea id="domain-context" autoSize={{ minRows: 2, maxRows: 6 }} value={context} onChange={(event) => setContext(event.target.value)} placeholder="已知信息、资料范围或执行边界（可选）" /></Form.Item>
              {mode === 'ctf' ? <Form.Item label="完成要求" htmlFor="completion-requirements"><Input.TextArea id="completion-requirements" autoSize={{ minRows: 3, maxRows: 8 }} value={completionRequirements} onChange={(event) => setCompletionRequirements(event.target.value)} placeholder="需要完成的题目、证据或交付内容" /></Form.Item> : <section className={styles.acceptance} aria-label="验收条件"><h2>验收条件</h2>{acceptance.map((item, index) => <div className={styles.acceptanceRow} key={item.key}><span>A{index + 1}</span><Input.TextArea aria-label={`验收条件 ${index + 1}`} id={`acceptance-${item.key}`} autoSize={{ minRows: 1, maxRows: 5 }} value={item.desc} onChange={(event) => updateAcceptance(item.key, event.target.value)} placeholder="说明如何判断这一项已完成" required /><Button type="text" icon={<DeleteOutlined />} aria-label={`删除验收条件 ${index + 1}`} disabled={pending || locked || acceptance.length === 1} onClick={() => setAcceptance((items) => items.filter((entry) => entry.key !== item.key))} /></div>)}<Button type="dashed" icon={<PlusOutlined />} onClick={addAcceptance}>添加验收条件</Button></section>}
              <section className={styles.attachments} aria-label="初始附件"><TaskAttachments ensureGroup={ensureGroup} resetGroup={resetGroup} groupExpired={groupExpired} disabled={pending || locked} onStatus={attachmentsStatus} /></section>
            </section>
            <aside className={styles.sidebar} aria-label="任务设置"><h2>运行设置</h2>
              <Form.Item label={`金额上限（${currencyLabel}）`} htmlFor="max-cost" required><Input id="max-cost" type="number" min="0.000001" step="any" inputMode="decimal" value={maxCost} onChange={(event) => setMaxCost(event.target.value)} required prefix="¥" /></Form.Item>
              <div className={styles.settingsGrid}><Form.Item label="时长上限（分钟）" htmlFor="max-minutes" required><Input id="max-minutes" type="number" min="1" step="1" value={maxMinutes} onChange={(event) => setMaxMinutes(event.target.value)} required /></Form.Item>
              {mode === 'ctf' ? <Form.Item label="队友上限（不含 Lead）" htmlFor="max-teammates"><Input id="max-teammates" type="number" min="0" step="1" value={maxTeammates} onChange={(event) => setMaxTeammates(event.target.value)} required /></Form.Item> : <Form.Item label="并发 Agent 上限" htmlFor="max-agents" required><Input id="max-agents" type="number" min="1" step="1" value={maxAgents} onChange={(event) => setMaxAgents(event.target.value)} required /></Form.Item>}</div>
              <Form.Item label="模型" htmlFor="model" required><Select id="model" aria-label="平台模型" value={modelId || undefined} placeholder="选择模型" loading={models.isLoading} options={models.data?.filter((item) => item.enabled).map((item) => ({ value: item.name, label: item.label + (item.is_default ? ' · 默认' : '') })) ?? []} onChange={(value) => { setModelId(value); setReasoningEffort(''); }} /></Form.Item>
              {provider?.supports_reasoning_effort && <Form.Item label="推理强度" htmlFor="reasoning-effort"><Select id="reasoning-effort" aria-label="推理强度" value={reasoningEffort} onChange={setReasoningEffort} options={[{ value: '', label: `沿用模型配置（${selectedModel?.config.reasoning_effort === 'none' || selectedModel?.config.reasoning_effort === 'off' ? '不传参数' : selectedModel?.config.reasoning_effort}）` }, { value: 'none', label: '模型默认（不传参数）' }, ...efforts.map((value) => ({ value, label: value }))]} /></Form.Item>}
              {provider && !provider.supports_reasoning_effort && <p className={controls.hint}>该连接方式使用模型默认思考设置。</p>}
              {(models.isError || providers.isError) && !unauthorized && <Alert type="error" title="模型选项读取失败" action={<Button size="small" onClick={() => { void models.refetch(); void providers.refetch(); }}>重试</Button>} />}
              {!models.isLoading && !models.data?.some((item) => item.enabled) && <Alert type="warning" title="当前没有可用模型" action={<Button href="/profiles">添加模型</Button>} />}
              <Collapse ghost size="small" items={[{ key: 'network', label: '目标域名（可选）', children: <Input.TextArea aria-label="任务所需域名（记录）" autoSize={{ minRows: 2, maxRows: 5 }} value={allowlist} onChange={(event) => setAllowlist(event.target.value)} placeholder="每行一个域名，或用逗号分隔" /> }]} />
            </aside>
          </div>
          <footer className={styles.footer}>{formError && <Alert type="error" title={formError} showIcon />}<div className={styles.footerActions}><Button disabled={false} onClick={() => navigate('/tasks')}>取消</Button><Button type="primary" htmlType="submit" loading={pending} disabled={pending || uploadsBusy || uploadsInvalid || (!locked && !models.data?.some((item) => item.enabled))}>创建任务</Button></div></footer>
        </Form>
      </form>
    </div>;
}
