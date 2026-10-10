import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { api, ApiError, type TaskCreateInput } from '../api/client';
import controls from '../styles/controls.module.css';
import styles from './NewTaskPage.module.css';
import { reasoningOptions } from '../components/reasoningOptions';
import TaskAttachments from '../components/TaskAttachments';

type AcceptanceDraft = { key: number; desc: string };

function fitTextarea(element: HTMLTextAreaElement) {
  element.style.height = 'auto';
  element.style.height = `${element.scrollHeight}px`;
}

export default function NewTaskPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const nextKey = useRef(2);
  const formRef = useRef<HTMLFormElement>(null);
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
    if (!group.current) group.current = api.createInputGroup().then((value) => value.id).catch((error) => { group.current = null; throw error; });
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
      if (!created.current) void group.current?.then((id) => api.deleteInputGroup(id)).catch(() => {});
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
    if (!modelId && models.data?.length) setModelId(models.data.find((item) => item.is_default && item.enabled)?.name ?? models.data.find((item) => item.enabled)?.name ?? '');
  }, [modelId, models.data]);
  useEffect(() => {
    if (unauthorized) navigate('/login', { replace: true, state: { from: location.pathname } });
  }, [unauthorized, navigate, location.pathname]);
  useEffect(() => {
    const form = formRef.current;
    if (!form) return;
    let width = 0;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width === width) return;
      width = entry.contentRect.width;
      form.querySelectorAll('textarea').forEach(fitTextarea);
    });
    observer.observe(form);
    return () => observer.disconnect();
  }, []);

  function addAcceptance() {
    setAcceptance((items) => [...items, { key: nextKey.current++, desc: '' }]);
  }

  function updateAcceptance(key: number, desc: string) {
    setAcceptance((items) => items.map((item) => item.key === key ? { ...item, desc } : item));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending || uploadsBusy) return;
    setFormError('');
    if (uploadsInvalid) { setFormError('请重试或移除上传失败的附件。'); return; }
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
      if (reasoningEffort) input.reasoning_effort = reasoningEffort;
    }
    setPending(true);
    try {
      const ready = submitted.current ?? { ...input, input_group_id: await ensureGroup(), input_file_ids: inputIds };
      if (!alive.current) return;
      submitted.current = ready;
      const result = await api.createTask(ready);
      created.current = true;
      if (alive.current) navigate(`/tasks/${result.id}`, { replace: true });
    } catch (cause) {
      const uncertain = submitted.current && (!(cause instanceof ApiError) || cause.status >= 500);
      if (!uncertain) submitted.current = null;
      if (!alive.current) return;
      if (!uncertain && cause instanceof ApiError && [404, 410].includes(cause.status)) setGroupExpired(true);
      setLocked(Boolean(uncertain));
      setFormError(uncertain ? '创建结果未确认。请重试同一请求，不会重复创建任务。' : cause instanceof ApiError ? cause.message : '任务创建失败，请稍后重试。');
    } finally {
      if (alive.current) setPending(false);
    }
  }

  return (
    <div className={styles.page}>
      <nav className={styles.breadcrumb} aria-label="面包屑导航"><Link to="/tasks">任务</Link><span aria-hidden="true">/</span><span>新建</span></nav>
      <header className={styles.heading}>
        <h1>创建任务</h1>
      </header>
      <form ref={formRef} onSubmit={submit} className={styles.form} aria-busy={pending}>
        <fieldset className={styles.modePicker} disabled={pending || locked}>
          <legend>协作模式</legend>
          <label><input type="radio" name="mode" value="blackboard" checked={mode === 'blackboard'} onChange={() => setMode('blackboard')} /><span><strong>黑板探索</strong><small>围绕验收条件组织事实与探索方向</small></span></label>
          <label><input type="radio" name="mode" value="ctf" checked={mode === 'ctf'} onChange={() => setMode('ctf')} /><span><strong>CTF 团队</strong><small>Lead 统筹具名队友，按题协作与增援</small></span></label>
        </fieldset>
        <div className={styles.layout}>
          <div className={styles.editor}>
            <section className={styles.section} aria-label="任务内容">
              <div className={styles.writingFields}>
                <div className={controls.field}><label className={controls.label} htmlFor="task-name">任务名</label><input id="task-name" className={controls.input} value={name} maxLength={100} onChange={(event) => setName(event.target.value)} placeholder="简短名称，用于列表展示" disabled={pending || locked} /></div>
                <div className={controls.field}>
                  <label className={controls.label} htmlFor="goal">任务目标<span className={controls.required} aria-hidden="true">*</span></label>
                  <textarea id="goal" className={`${controls.textarea} ${styles.goalTextarea}`} rows={3} value={goal} onChange={(event) => { setGoal(event.target.value); fitTextarea(event.currentTarget); }} placeholder="例如：核对两份资料中的关键数据差异，说明差异来源并给出可复核的依据" required disabled={pending || locked} />
                </div>
                <div className={controls.field}>
                  <label className={controls.label} htmlFor="domain-context">领域背景</label>
                  <textarea id="domain-context" className={`${controls.textarea} ${styles.contextTextarea}`} rows={3} value={context} onChange={(event) => { setContext(event.target.value); fitTextarea(event.currentTarget); }} placeholder="补充资料范围、已知事实、术语定义或需要遵守的边界（可选）" disabled={pending || locked} />
                  <details className={styles.fieldHelp}><summary>填写背景的建议</summary><p>写入 Agent 理解任务所需的信息；调查结论和证据仍需在任务中核实。</p></details>
                </div>
              </div>
            </section>

            {mode === 'ctf' ? <section className={styles.section} aria-labelledby="completion-heading">
              <div className={controls.field}>
                <label id="completion-heading" className={controls.label} htmlFor="completion-requirements">完成要求</label>
                <textarea id="completion-requirements" className={controls.textarea} rows={4} value={completionRequirements} onChange={(event) => { setCompletionRequirements(event.target.value); fitTextarea(event.currentTarget); }} placeholder="说明需要完成的题目、证据或交付内容（可选）" disabled={pending || locked} />
                <p className={controls.hint}>Lead 会持续接收任务和插话，并结合这些要求组织收尾。</p>
              </div>
            </section> : <section className={styles.section} aria-labelledby="acceptance-heading">
              <div className={styles.sectionHeading}><h2 id="acceptance-heading">验收条件</h2></div>
              <div className={styles.acceptanceList}>{acceptance.map((item, index) => (
                <div className={styles.acceptanceRow} key={item.key}>
                  <span className={styles.acceptanceIndex} aria-hidden="true">A{index + 1}</span>
                  <label className={styles.srOnly} htmlFor={`acceptance-${item.key}`}>验收条件 {index + 1}</label>
                  <textarea id={`acceptance-${item.key}`} className={`${controls.textarea} ${styles.acceptanceTextarea}`} rows={2} value={item.desc} onChange={(event) => { updateAcceptance(item.key, event.target.value); fitTextarea(event.currentTarget); }} placeholder="例如：列出每处差异、对应资料位置及核对结论" required disabled={pending || locked} />
                  <button type="button" className={`${controls.button} ${controls.quiet} ${styles.remove}`} onClick={() => setAcceptance((items) => items.filter((entry) => entry.key !== item.key))} disabled={pending || locked || acceptance.length === 1} aria-label={`删除验收条件 ${index + 1}`}>删除</button>
                </div>
              ))}</div>
              <button type="button" className={`${controls.button} ${styles.add}`} onClick={addAcceptance} disabled={pending || locked}>＋ 添加验收条件</button>
            </section>}
            <section className={styles.section} aria-label="初始附件"><TaskAttachments ensureGroup={ensureGroup} resetGroup={resetGroup} groupExpired={groupExpired} disabled={pending || locked} onStatus={attachmentsStatus} /></section>
          </div>

          <aside className={styles.sidebar} aria-label="任务设置">
            <section className={styles.settingsSection} aria-labelledby="budget-heading">
              <div className={styles.settingsHeading}><h2 id="budget-heading">预算</h2></div>
              <div className={`${styles.settingsFields} ${styles.budgetFields}`}>
                <div className={controls.field}><label className={controls.label} htmlFor="max-cost">金额上限（{currencyLabel}）<span className={controls.required} aria-hidden="true">*</span></label><input id="max-cost" className={controls.input} type="number" min="0.000001" step="any" inputMode="decimal" value={maxCost} onChange={(event) => setMaxCost(event.target.value)} required disabled={pending || locked} /></div>
                <div className={controls.field}><label className={controls.label} htmlFor="max-minutes">时长上限（分钟）<span className={controls.required} aria-hidden="true">*</span></label><input id="max-minutes" className={controls.input} type="number" min="1" step="1" value={maxMinutes} onChange={(event) => setMaxMinutes(event.target.value)} required disabled={pending || locked} /></div>
                {mode === 'ctf' ? <div className={controls.field}><label className={controls.label} htmlFor="max-teammates">队友上限（不含 Lead）</label><input id="max-teammates" className={controls.input} type="number" min="0" step="1" value={maxTeammates} onChange={(event) => setMaxTeammates(event.target.value)} required disabled={pending || locked} /><p className={controls.hint}>默认建议 4 名；填 0 时由 Lead 独立执行。</p></div> : <div className={controls.field}><label className={controls.label} htmlFor="max-agents">并发 Agent 上限<span className={controls.required} aria-hidden="true">*</span></label><input id="max-agents" className={controls.input} type="number" min="1" step="1" value={maxAgents} onChange={(event) => setMaxAgents(event.target.value)} required disabled={pending || locked} /></div>}
              </div>
            </section>

            <section className={styles.settingsSection} aria-labelledby="model-heading">
              <div className={styles.settingsHeading}><h2 id="model-heading">模型</h2></div>
              <div className={styles.settingsFields}>
                <div className={controls.field}><label className={controls.label} htmlFor="model">平台模型<span className={controls.required} aria-hidden="true">*</span></label><select id="model" className={controls.select} value={modelId} onChange={(event) => { setModelId(event.target.value); setReasoningEffort(''); }} required disabled={pending || locked || models.isLoading}><option value="">{models.isLoading ? '正在加载…' : '选择模型'}</option>{models.data?.filter((item) => item.enabled).map((item) => <option key={item.name} value={item.name}>{item.label}{item.is_default ? ' · 平台默认' : ''}</option>)}</select></div>
                {provider?.supports_reasoning_effort && <div className={controls.field}><label className={controls.label} htmlFor="reasoning-effort">推理强度</label><select id="reasoning-effort" className={controls.select} value={reasoningEffort} onChange={(event) => setReasoningEffort(event.target.value)} disabled={pending || locked}><option value="">沿用模型配置（{selectedModel?.config.reasoning_effort === 'none' || selectedModel?.config.reasoning_effort === 'off' ? '不传参数' : selectedModel?.config.reasoning_effort}）</option><option value="none">模型默认（不传参数）</option>{efforts.map((effort) => <option value={effort} key={effort}>{effort}</option>)}</select></div>}
                {provider && !provider.supports_reasoning_effort && <p className={controls.hint}>该连接方式使用模型默认思考设置。</p>}
                {providers.isError && <p className={controls.error} role="alert">推理选项加载失败，仍可沿用模型配置。<button type="button" className={styles.textButton} onClick={() => void providers.refetch()} disabled={pending || locked}>重试</button></p>}
                {models.isError && !unauthorized && <p className={controls.error} role="alert">模型加载失败。<button type="button" className={styles.textButton} onClick={() => void models.refetch()} disabled={pending || locked}>重试</button></p>}
                {!models.isLoading && !models.data?.some((item) => item.enabled) && <p className={controls.error} role="status">当前没有可用模型，请先在 Agent 配置中添加。</p>}
              </div>
            </section>
            <details className={`${styles.settingsSection} ${styles.networkSettings}`}>
              <summary>目标域名 <span>可选</span></summary>
              <div className={controls.field}><label className={controls.label} htmlFor="allowlist">任务所需域名（记录）</label><textarea id="allowlist" className={`${controls.textarea} ${styles.allowlistTextarea}`} rows={3} value={allowlist} onChange={(event) => { setAllowlist(event.target.value); fitTextarea(event.currentTarget); }} placeholder="每行一个域名，或用逗号分隔" disabled={pending || locked} /><span className={controls.hint}>可选。执行容器默认直接出网；这里仅记录目标域名，不限制访问。显式代理隔离部署由全局白名单控制。</span></div>
            </details>
          </aside>
        </div>

        <div className={styles.footer}>
          {formError && <p className={controls.error} role="alert">{formError}</p>}
          <div className={styles.footerActions}><Link to="/tasks" className={controls.button}>取消</Link><button type="submit" className={`${controls.button} ${controls.primary}`} disabled={pending || uploadsBusy || uploadsInvalid || (!locked && !models.data?.some((item) => item.enabled))}>{pending ? '正在创建…' : '创建任务'}</button></div>
        </div>
      </form>
    </div>
  );
}
