import { Checkbox, Collapse, Input } from 'antd';
import { type WorkerRole, type WorkerTools } from '../api/client';
import McpGrantPicker from './McpGrantPicker';
import { builtin, required } from './workerOptions';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';
export default function WorkerPrompts({ role, prompt, tools, onPromptChange, onToolsChange, disabled }: {
    role: WorkerRole;
    prompt: string;
    tools: WorkerTools;
    onPromptChange: (value: string) => void;
    onToolsChange: (value: WorkerTools) => void;
    disabled: boolean;
}) {
    return <div className={styles.workerContent}>
    <div className={styles.workerForm}><section className={styles.promptColumn}><div className={styles.promptLabel}><label className={controls.label} htmlFor={`prompt-${role}`}>完整系统提示词</label></div>
    <Input.TextArea id={`prompt-${role}`} className={controls.textarea} classNames={{textarea:styles.promptEditor}} styles={{textarea:{height:"42vh",minHeight:360}}} value={prompt} onChange={(event) => onPromptChange(event.target.value)} spellCheck={false} disabled={disabled}/></section><aside className={styles.toolsColumn}>

    <fieldset className={styles.toolFieldset}><legend>内置工具</legend><div className={styles.toolChecks}>{builtin[role].map((name) => <label key={name} className={styles.checkRow}><Checkbox checked={tools.builtin.includes(name)} disabled={disabled || required[role].includes(name)} onChange={(event) => onToolsChange({ ...tools, builtin: event.target.checked ? [...tools.builtin, name] : tools.builtin.filter((item) => item !== name) })}/>{name}{required[role].includes(name) && <small>必需</small>}</label>)}</div></fieldset>
    {role === 'explore' && <fieldset className={styles.toolFieldset}><legend>外部 MCP 工具</legend>
        <McpGrantPicker role="explore" selected={tools.mcp_servers ?? []} disabled={disabled}
            onChange={mcp_servers => onToolsChange({ ...tools, mcp_servers })} />
    </fieldset>}
    </aside></div><Collapse className={styles.variableHelp} size={"small"} ghost items={[{ key: "content", label: <>配置说明与模板变量</>, children: <><p>提示词下一次模型调用生效；工具仅对新任务生效。</p><p>通用变量：{'{{ goal }}'}、{'{{ domain_context }}'}、{'{{ acceptance_status }}'}、{'{{ agent_id }}'}。</p><p>Explore：current_intent、yaml_snapshot、seed_max_steps、conclude_grace_calls；Derive：facts_text、intents_text、previous_excluded；Close：mode、satisfies_facts、judgment_history、yaml_snapshot。</p></> }]}/>
  </div>;
}
