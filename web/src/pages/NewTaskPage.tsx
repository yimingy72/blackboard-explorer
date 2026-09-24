import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { api, ApiError, type TaskCreateInput } from '../api/client';
import controls from '../styles/controls.module.css';
import styles from './NewTaskPage.module.css';

type AcceptanceDraft = { key: number; desc: string };

export default function NewTaskPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const nextKey = useRef(2);
  const [goal, setGoal] = useState('');
  const [context, setContext] = useState('');
  const [acceptance, setAcceptance] = useState<AcceptanceDraft[]>([{ key: 1, desc: '' }]);
  const [maxCost, setMaxCost] = useState('10');
  const [maxMinutes, setMaxMinutes] = useState('60');
  const [maxAgents, setMaxAgents] = useState('5');
  const [allowlist, setAllowlist] = useState('');
  const [profileName, setProfileName] = useState('');
  const [profileVersion, setProfileVersion] = useState('');
  const [pending, setPending] = useState(false);
  const [formError, setFormError] = useState('');

  const profiles = useQuery({ queryKey: ['profiles'], queryFn: api.listProfiles });
  const versions = useQuery({ queryKey: ['profile-versions', profileName], queryFn: () => api.listProfileVersions(profileName), enabled: Boolean(profileName) });
  const unauthorized = (profiles.error instanceof ApiError && profiles.error.status === 401) || (versions.error instanceof ApiError && versions.error.status === 401);

  useEffect(() => {
    if (!profileName && profiles.data?.length) {
      setProfileName(profiles.data.some((item) => item.name === 'default') ? 'default' : profiles.data[0].name);
    }
  }, [profileName, profiles.data]);
  useEffect(() => {
    if (unauthorized) navigate('/login', { replace: true, state: { from: location.pathname } });
  }, [unauthorized, navigate, location.pathname]);

  function addAcceptance() {
    setAcceptance((items) => [...items, { key: nextKey.current++, desc: '' }]);
  }

  function updateAcceptance(key: number, desc: string) {
    setAcceptance((items) => items.map((item) => item.key === key ? { ...item, desc } : item));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError('');
    const descriptions = acceptance.map((item) => item.desc.trim());
    if (!goal.trim() || descriptions.some((value) => !value)) {
      setFormError('请填写任务目标和每一项验收条件。');
      return;
    }
    const cost = Number(maxCost);
    const minutes = Number(maxMinutes);
    const agents = Number(maxAgents);
    if (!Number.isFinite(cost) || cost <= 0 || !Number.isInteger(minutes) || minutes <= 0 || !Number.isInteger(agents) || agents <= 0) {
      setFormError('预算金额必须大于 0，时长和并发数必须是正整数。');
      return;
    }
    if (!profileName) {
      setFormError('请选择 Agent 配置。');
      return;
    }
    const input: TaskCreateInput = {
      goal: goal.trim(),
      domain_context: context.trim() || null,
      acceptance: descriptions.map((desc, index) => ({ id: `A${index + 1}`, desc })),
      budget: { max_cost: maxCost, max_minutes: minutes, max_concurrent_agents: agents },
      agent_profile: profileName,
      ...(profileVersion ? { profile_version: Number(profileVersion) } : {}),
      egress_allowlist: [...new Set(allowlist.split(/[\n,]/).map((item) => item.trim()).filter(Boolean))],
    };
    setPending(true);
    try {
      const created = await api.createTask(input);
      navigate(`/tasks/${created.id}`, { replace: true });
    } catch (cause) {
      setFormError(cause instanceof ApiError ? cause.message : '任务创建失败，请稍后重试。');
    } finally {
      setPending(false);
    }
  }

  return (
    <div className={styles.page}>
      <nav className={styles.breadcrumb} aria-label="面包屑导航"><Link to="/tasks">任务</Link><span aria-hidden="true">/</span><span>新建</span></nav>
      <header className={styles.heading}>
        <h1>创建探索任务</h1>
        <p>先写清目标和验收条件，启动后 Agent 才会围绕同一张黑板协作。</p>
      </header>
      <form onSubmit={submit} className={styles.form}>
        <section className={styles.section} aria-labelledby="goal-heading">
          <div className={styles.sectionHeading}><h2 id="goal-heading">目标</h2><p>用一句可判断完成与否的话描述任务。</p></div>
          <div className={styles.sectionBody}>
            <div className={controls.field}>
              <label className={controls.label} htmlFor="goal">任务目标<span className={controls.required} aria-hidden="true">*</span></label>
              <textarea id="goal" className={controls.textarea} rows={3} value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="例如：找出网关 502 的直接触发条件，并提供可复核的依据" required disabled={pending} />
            </div>
            <div className={controls.field}>
              <label className={controls.label} htmlFor="domain-context">领域背景</label>
              <textarea id="domain-context" className={controls.textarea} rows={4} value={context} onChange={(event) => setContext(event.target.value)} placeholder="补充系统背景、已知边界或调查时需要注意的信息" disabled={pending} />
              <span className={controls.hint}>可选。作为 Agent 理解任务的背景，不代替证据。</span>
            </div>
          </div>
        </section>

        <section className={styles.section} aria-labelledby="acceptance-heading">
          <div className={styles.sectionHeading}><h2 id="acceptance-heading">验收条件</h2><p>至少一项。每项说明最后需要证明什么。</p></div>
          <div className={styles.sectionBody}>
            <div className={styles.acceptanceList}>{acceptance.map((item, index) => (
              <div className={styles.acceptanceRow} key={item.key}>
                <span className={styles.acceptanceIndex} aria-hidden="true">A{index + 1}</span>
                <label className={styles.srOnly} htmlFor={`acceptance-${item.key}`}>验收条件 {index + 1}</label>
                <input id={`acceptance-${item.key}`} className={controls.input} value={item.desc} onChange={(event) => updateAcceptance(item.key, event.target.value)} placeholder="例如：说明触发路径并附可复现证据" required disabled={pending} />
                <button type="button" className={`${controls.button} ${controls.quiet} ${styles.remove}`} onClick={() => setAcceptance((items) => items.filter((entry) => entry.key !== item.key))} disabled={pending || acceptance.length === 1} aria-label={`删除验收条件 ${index + 1}`}>删除</button>
              </div>
            ))}</div>
            <button type="button" className={`${controls.button} ${styles.add}`} onClick={addAcceptance} disabled={pending}>＋ 添加验收条件</button>
          </div>
        </section>

        <section className={styles.section} aria-labelledby="budget-heading">
          <div className={styles.sectionHeading}><h2 id="budget-heading">预算与配置</h2><p>额度决定任务何时收尾，配置版本在创建后固定。</p></div>
          <div className={styles.sectionBody}>
            <div className={styles.budgetGrid}>
              <div className={controls.field}><label className={controls.label} htmlFor="max-cost">金额上限<span className={controls.required} aria-hidden="true">*</span></label><input id="max-cost" className={controls.input} type="number" min="0.000001" step="any" inputMode="decimal" value={maxCost} onChange={(event) => setMaxCost(event.target.value)} required disabled={pending} /><span className={controls.hint}>按所选配置的模型价格币种计算</span></div>
              <div className={controls.field}><label className={controls.label} htmlFor="max-minutes">时长上限（分钟）<span className={controls.required} aria-hidden="true">*</span></label><input id="max-minutes" className={controls.input} type="number" min="1" step="1" value={maxMinutes} onChange={(event) => setMaxMinutes(event.target.value)} required disabled={pending} /></div>
              <div className={controls.field}><label className={controls.label} htmlFor="max-agents">并发 Agent 上限<span className={controls.required} aria-hidden="true">*</span></label><input id="max-agents" className={controls.input} type="number" min="1" step="1" value={maxAgents} onChange={(event) => setMaxAgents(event.target.value)} required disabled={pending} /></div>
            </div>
            <div className={styles.profileGrid}>
              <div className={controls.field}><label className={controls.label} htmlFor="profile">Agent 配置<span className={controls.required} aria-hidden="true">*</span></label><select id="profile" className={controls.select} value={profileName} onChange={(event) => { setProfileName(event.target.value); setProfileVersion(''); }} required disabled={pending || profiles.isLoading || !profiles.data?.length}><option value="">{profiles.isLoading ? '正在加载…' : '选择配置'}</option>{profiles.data?.map((profile) => <option key={profile.name} value={profile.name}>{profile.name}</option>)}</select></div>
              <div className={controls.field}><label className={controls.label} htmlFor="profile-version">版本</label><select id="profile-version" className={controls.select} value={profileVersion} onChange={(event) => setProfileVersion(event.target.value)} disabled={pending || !profileName || versions.isLoading}><option value="">最新版本{profiles.data?.find((item) => item.name === profileName)?.latest_version ? ` · v${profiles.data.find((item) => item.name === profileName)?.latest_version}` : ''}</option>{versions.data?.map((version) => <option key={version.version} value={version.version}>v{version.version}</option>)}</select></div>
            </div>
            {(profiles.isError || versions.isError) && !unauthorized && <p className={controls.error} role="alert">配置加载失败。<button type="button" className={styles.textButton} onClick={() => void (profiles.isError ? profiles.refetch() : versions.refetch())}>重试</button></p>}
            {!profiles.isLoading && profiles.data?.length === 0 && <p className={controls.error} role="status">当前没有可用的 Agent 配置，暂时无法创建任务。</p>}
            <div className={controls.field}><label className={controls.label} htmlFor="allowlist">允许访问的域名</label><textarea id="allowlist" className={controls.textarea} rows={2} value={allowlist} onChange={(event) => setAllowlist(event.target.value)} placeholder="每行一个域名，或用逗号分隔" disabled={pending} /><span className={controls.hint}>可选。留空时不添加外网白名单。</span></div>
          </div>
        </section>

        <div className={styles.footer}>
          {formError && <p className={controls.error} role="alert">{formError}</p>}
          <div className={styles.footerActions}><Link to="/tasks" className={controls.button}>取消</Link><button type="submit" className={`${controls.button} ${controls.primary}`} disabled={pending || !profiles.data?.length}>{pending ? '正在创建…' : '创建任务'}</button></div>
        </div>
      </form>
    </div>
  );
}
