import { useMemo, useState } from 'react';
import YAML from 'yaml';
import { parseProfileYaml, stringifyProfileYaml, type ProfileInput } from '../profiles/yaml';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';

const workers = [
  { id: 'explore', title: 'Explore · 探索', description: '执行调查、提交事实和意图。完整模板包含种子启动与认领意图后的工作规则。' },
  { id: 'derive', title: 'derive · 推导', description: '根据已有事实与验收缺口提出新意图，包含并行推导和静止推导使用的完整规则。' },
  { id: 'close', title: 'Close · 裁定与终结', description: '核对证据、裁定验收并生成报告。完整模板包含 judge 裁定与 final 终结两个分支。' },
] as const;

export default function WorkerPrompts({ source, onChange, disabled = false }: {
  source: string; onChange?: (value: string) => void; disabled?: boolean;
}) {
  const [worker, setWorker] = useState<(typeof workers)[number]['id']>('explore');
  const [advanced, setAdvanced] = useState(false);
  const [notice, setNotice] = useState('');
  const profile = useMemo(() => {
    try { return YAML.parse(source, { uniqueKeys: true, maxAliasCount: 100 }) as ProfileInput; }
    catch { return null; }
  }, [source]);
  const current = workers.find((item) => item.id === worker)!;
  const prompt = profile?.prompt_templates?.[worker];
  const valid = typeof prompt === 'string';
  const model = profile?.models?.[worker];

  function showWorkers() {
    try { parseProfileYaml(source); setAdvanced(false); setNotice(''); }
    catch { setNotice('请先修正高级 YAML 中的格式或必填内容，草稿已保留。'); }
  }
  function changePrompt(value: string) {
    if (profile && onChange) onChange(stringifyProfileYaml({
      ...profile, prompt_templates: { ...profile.prompt_templates, [worker]: value },
    }));
  }
  async function copy() {
    try { await navigator.clipboard.writeText(prompt ?? ''); setNotice('完整提示词已复制。'); }
    catch { setNotice('复制失败，请选中正文手动复制。'); }
  }

  return <div className={styles.workerContent}>
    <div className={styles.tabs} role="group" aria-label="配置编辑方式">
      <button type="button" className={!advanced ? styles.tabActive : styles.tab} onClick={showWorkers}>Worker 提示词</button>
      <button type="button" className={advanced ? styles.tabActive : styles.tab} onClick={() => { setAdvanced(true); setNotice(''); }}>高级 YAML</button>
    </div>
    {notice && <p role="status" className={controls.hint}>{notice}</p>}
    {advanced ? <>
      <label className={controls.label} htmlFor="profile-yaml">完整 AgentProfile YAML</label>
      <p className={controls.hint}>可同时修改模型价格、参数、执行环境与三个 worker 的提示词；与提示词编辑共享同一份草稿。</p>
      {onChange ? <textarea id="profile-yaml" className={`${controls.textarea} ${styles.yamlEditor}`} value={source} onChange={(event) => onChange(event.target.value)} spellCheck={false} disabled={disabled} /> : <pre className={styles.yamlView} tabIndex={0} aria-label="完整 AgentProfile YAML">{source}</pre>}
    </> : <>
      <div className={styles.workerTabs} role="group" aria-label="选择 worker">{workers.map((item) => <button type="button" key={item.id} aria-pressed={worker === item.id} onClick={() => { setWorker(item.id); setNotice(''); }} className={worker === item.id ? styles.workerActive : styles.workerTab}>{item.title}</button>)}</div>
      <div className={styles.workerHeading}><div><h3>{current.title}</h3><p>{current.description}</p></div><button type="button" className={controls.button} onClick={() => void copy()} disabled={!valid}>复制全文</button></div>
      {model && <p className={styles.modelSummary}>{model.model} · 推理 {model.reasoning_effort} · 计价 {model.price?.currency === 'CNY' ? '人民币 CNY' : model.price?.currency ?? '未配置'}</p>}
      <label className={controls.label} htmlFor={`prompt-${worker}`}>完整系统提示词 · {worker}</label>
      {!valid ? <p role="alert">提示词结构不完整，请切换高级 YAML 修正。草稿不会丢失。</p> : onChange ? <textarea id={`prompt-${worker}`} className={`${controls.textarea} ${styles.promptEditor}`} value={prompt} onChange={(event) => changePrompt(event.target.value)} spellCheck={false} disabled={disabled} aria-describedby="prompt-help" /> : <pre id={`prompt-${worker}`} className={styles.promptView} tabIndex={0}>{prompt}</pre>}
      <p id="prompt-help" className={controls.hint}>这里是该版本完整的系统提示词模板。目标、黑板、验收状态等变量在运行时注入；实际初始上下文可在任务的 Agent 对话中查看。</p>
      <details className={styles.variableHelp}><summary>模板变量与分支说明</summary><p>通用变量：{'{{ goal }}'}、{'{{ domain_context }}'}、{'{{ acceptance_status }}'}、{'{{ agent_id }}'}。</p><p>Explore：current_intent、yaml_snapshot、seed_max_steps、conclude_grace_calls；derive：facts_text、intents_text、previous_excluded；Close：mode、satisfies_facts、judgment_history、yaml_snapshot。</p><p>保留所需的 Jinja 变量和条件分支。种子复用 Explore，judge/final 复用 Close，无需另建 worker。</p></details>
    </>}
  </div>;
}
