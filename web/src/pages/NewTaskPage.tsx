import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { api, ApiError, type TaskCreateInput } from '../api/client';
import { readDefaultProfile } from '../profiles/preferences';
import controls from '../styles/controls.module.css';
import styles from './NewTaskPage.module.css';

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
  const browserDefault = useRef(readDefaultProfile());
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
  const effectiveVersion = Number(profileVersion) || profiles.data?.find((item) => item.name === profileName)?.latest_version;
  const profile = useQuery({ queryKey: ['profile', profileName, effectiveVersion], queryFn: () => api.getProfile(profileName, effectiveVersion!), enabled: Boolean(profileName && effectiveVersion) });
  const currency = profile.data?.profile.models.explore.price.currency;
  const currencyLabel = currency === 'CNY' ? '人民币元' : currency === 'USD' ? '美元 USD' : currency ?? '读取币种中';
  const unauthorized = (profile.error instanceof ApiError && profile.error.status === 401) || (profiles.error instanceof ApiError && profiles.error.status === 401) || (versions.error instanceof ApiError && versions.error.status === 401);

  useEffect(() => {
    if (!profiles.data?.length || profiles.data.some((item) => item.name === profileName)) return;
    const preferred = profiles.data.find((item) => item.name === browserDefault.current?.name);
    setProfileName(preferred?.name ?? profiles.data.find((item) => item.name === 'default')?.name ?? profiles.data[0].name);
    setProfileVersion(preferred ? String(browserDefault.current?.version ?? '') : '');
  }, [profileName, profiles.data]);
  useEffect(() => {
    if (versions.data && profileVersion && !versions.data.some((item) => item.version === Number(profileVersion))) {
      setProfileVersion('');
    }
  }, [profileVersion, versions.data]);
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
    if (!profile.data || !currency) {
      setFormError('模型计价币种尚未加载或未配置，请检查所选版本后重试。');
      return;
    }
    const input: TaskCreateInput = {
      goal: goal.trim(),
      domain_context: context.trim() || null,
      acceptance: descriptions.map((desc, index) => ({ id: `A${index + 1}`, desc })),
      budget: { max_cost: maxCost, max_minutes: minutes, max_concurrent_agents: agents },
      agent_profile: profileName,
      profile_version: profile.data.version,
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
      <form ref={formRef} onSubmit={submit} className={styles.form} aria-busy={pending}>
        <div className={styles.layout}>
          <div className={styles.editor}>
            <section className={styles.section} aria-label="任务内容">
              <div className={styles.writingFields}>
                <div className={controls.field}>
                  <label className={controls.label} htmlFor="goal">任务目标<span className={controls.required} aria-hidden="true">*</span></label>
                  <textarea id="goal" className={`${controls.textarea} ${styles.goalTextarea}`} rows={3} value={goal} onChange={(event) => { setGoal(event.target.value); fitTextarea(event.currentTarget); }} placeholder="例如：核对两份资料中的关键数据差异，说明差异来源并给出可复核的依据" required disabled={pending} />
                </div>
                <div className={controls.field}>
                  <label className={controls.label} htmlFor="domain-context">领域背景</label>
                  <textarea id="domain-context" className={`${controls.textarea} ${styles.contextTextarea}`} rows={5} value={context} onChange={(event) => { setContext(event.target.value); fitTextarea(event.currentTarget); }} placeholder="补充资料范围、已知事实、术语定义或需要遵守的边界（可选）" disabled={pending} />
                  <details className={styles.fieldHelp}><summary>填写背景的建议</summary><p>写入 Agent 理解任务所需的信息；调查结论和证据仍需在任务中核实。</p></details>
                </div>
              </div>
            </section>

            <section className={styles.section} aria-labelledby="acceptance-heading">
              <div className={styles.sectionHeading}><h2 id="acceptance-heading">验收条件</h2><p>至少填写一项。每项写明最终需要交付或证明的内容。</p></div>
              <div className={styles.acceptanceList}>{acceptance.map((item, index) => (
                <div className={styles.acceptanceRow} key={item.key}>
                  <span className={styles.acceptanceIndex} aria-hidden="true">A{index + 1}</span>
                  <label className={styles.srOnly} htmlFor={`acceptance-${item.key}`}>验收条件 {index + 1}</label>
                  <textarea id={`acceptance-${item.key}`} className={`${controls.textarea} ${styles.acceptanceTextarea}`} rows={2} value={item.desc} onChange={(event) => { updateAcceptance(item.key, event.target.value); fitTextarea(event.currentTarget); }} placeholder="例如：列出每处差异、对应资料位置及核对结论" required disabled={pending} />
                  <button type="button" className={`${controls.button} ${controls.quiet} ${styles.remove}`} onClick={() => setAcceptance((items) => items.filter((entry) => entry.key !== item.key))} disabled={pending || acceptance.length === 1} aria-label={`删除验收条件 ${index + 1}`}>删除</button>
                </div>
              ))}</div>
              <button type="button" className={`${controls.button} ${styles.add}`} onClick={addAcceptance} disabled={pending}>＋ 添加验收条件</button>
            </section>
          </div>

          <aside className={styles.sidebar} aria-label="任务设置">
            <section className={styles.settingsSection} aria-labelledby="budget-heading">
              <div className={styles.settingsHeading}><h2 id="budget-heading">预算</h2><p>达到额度时任务会收尾。</p></div>
              <div className={`${styles.settingsFields} ${styles.budgetFields}`}>
                <div className={controls.field}><label className={controls.label} htmlFor="max-cost">金额上限（{currencyLabel}）<span className={controls.required} aria-hidden="true">*</span></label><input id="max-cost" className={controls.input} type="number" min="0.000001" step="any" inputMode="decimal" value={maxCost} onChange={(event) => setMaxCost(event.target.value)} required disabled={pending} /><span className={controls.hint}>{currency === 'CNY' ? '按固定人民币价格表估算；默认配置采用高峰价，不等同于账单实扣。' : `所选历史或自定义配置以 ${currencyLabel} 计价；使用人民币请选择最新 CNY 版本。`}</span></div>
                <div className={controls.field}><label className={controls.label} htmlFor="max-minutes">时长上限（分钟）<span className={controls.required} aria-hidden="true">*</span></label><input id="max-minutes" className={controls.input} type="number" min="1" step="1" value={maxMinutes} onChange={(event) => setMaxMinutes(event.target.value)} required disabled={pending} /></div>
                <div className={controls.field}><label className={controls.label} htmlFor="max-agents">并发 Agent 上限<span className={controls.required} aria-hidden="true">*</span></label><input id="max-agents" className={controls.input} type="number" min="1" step="1" value={maxAgents} onChange={(event) => setMaxAgents(event.target.value)} required disabled={pending} /></div>
              </div>
            </section>

            <section className={styles.settingsSection} aria-labelledby="profile-heading">
              <div className={styles.settingsHeading}><h2 id="profile-heading">Agent 配置</h2><p>创建后使用选定的配置版本。</p></div>
              <div className={styles.settingsFields}>
                <div className={controls.field}><label className={controls.label} htmlFor="profile">配置<span className={controls.required} aria-hidden="true">*</span></label><select id="profile" className={controls.select} value={profileName} onChange={(event) => { setProfileName(event.target.value); setProfileVersion(''); }} required disabled={pending || profiles.isLoading || !profiles.data?.length}><option value="">{profiles.isLoading ? '正在加载…' : '选择配置'}</option>{profiles.data?.map((profile) => <option key={profile.name} value={profile.name}>{profile.name}</option>)}</select></div>
                <div className={controls.field}><label className={controls.label} htmlFor="profile-version">版本</label><select id="profile-version" className={controls.select} value={profileVersion} onChange={(event) => setProfileVersion(event.target.value)} disabled={pending || !profileName || versions.isLoading}><option value="">最新版本{profiles.data?.find((item) => item.name === profileName)?.latest_version ? ` · v${profiles.data.find((item) => item.name === profileName)?.latest_version}` : ''}</option>{versions.data?.map((version) => <option key={version.version} value={version.version}>v{version.version}</option>)}</select></div>
                {(profiles.isError || versions.isError) && !unauthorized && <p className={controls.error} role="alert">配置加载失败。<button type="button" className={styles.textButton} onClick={() => void (profiles.isError ? profiles.refetch() : versions.refetch())} disabled={pending}>重试</button></p>}
                {!profiles.isLoading && profiles.data?.length === 0 && <p className={controls.error} role="status">当前没有可用的 Agent 配置，暂时无法创建任务。</p>}
              </div>
            </section>

            <details className={`${styles.settingsSection} ${styles.networkSettings}`}>
              <summary>目标域名 <span>可选</span></summary>
              <div className={controls.field}><label className={controls.label} htmlFor="allowlist">任务所需域名（记录）</label><textarea id="allowlist" className={`${controls.textarea} ${styles.allowlistTextarea}`} rows={3} value={allowlist} onChange={(event) => { setAllowlist(event.target.value); fitTextarea(event.currentTarget); }} placeholder="每行一个域名，或用逗号分隔" disabled={pending} /><span className={controls.hint}>可选。执行容器默认直接出网；这里仅记录目标域名，不限制访问。显式代理隔离部署由全局白名单控制。</span></div>
            </details>
          </aside>
        </div>

        <div className={styles.footer}>
          {formError && <p className={controls.error} role="alert">{formError}</p>}
          <div className={styles.footerActions}><Link to="/tasks" className={controls.button}>取消</Link><button type="submit" className={`${controls.button} ${controls.primary}`} disabled={pending || !profiles.data?.length}>{pending ? '正在创建…' : '创建任务'}</button></div>
        </div>
      </form>
    </div>
  );
}
