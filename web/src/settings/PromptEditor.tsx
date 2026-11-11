import { EyeOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { Button, Drawer, Input } from 'antd';
import { useId, useState } from 'react';
import { mergeTextareaEdit, promptSections } from './promptSections';
import styles from './Settings.module.css';

export default function PromptEditor({ value, role, variables, onChange, disabled, resetDefault, isDefault }: {
  value: string; role: string; variables?: Record<string, string>; onChange?: (value: string) => void;
  disabled?: boolean; resetDefault?: () => void; isDefault?: boolean;
}) {
  const id = useId();
  const [whole, setWhole] = useState(false);
  const [active, setActive] = useState(0);
  const [preview, setPreview] = useState(false);
  const parts = promptSections(value);
  const selected = whole ? undefined : parts[active] ?? parts[0];
  const start = selected?.start ?? 0, end = selected?.end ?? value.length;
  const edit = value.slice(start, end);
  const label = selected?.label ?? '完整系统提示词';
  return <>
    <div className={styles.panelHeading}><div><h3>系统提示词</h3><p>{onChange
      ? parts.length ? '按标题分段编辑，也可以直接编辑全文。' : '此模板在全文模式编辑。保存保留模板语法与已有段落。'
      : '部署发布的系统提示词，当前页面只读。'}</p></div>
      <div className={styles.actions}>
        {!!parts.length && <Button onClick={() => setWhole(!whole)}>{whole ? '分段视图' : onChange ? '全文编辑' : '全文查看'}</Button>}
        <Button icon={<EyeOutlined aria-hidden="true" />} onClick={() => setPreview(true)}>完整预览</Button>
      </div>
    </div>
    {resetDefault && <div className={styles.note}><InfoCircleOutlined /><span>{isDefault ? '使用平台默认提示词。编辑后会为成果整理保存自定义提示词。' : '使用自定义提示词。'}</span>
      {!isDefault && <Button type="link" size="small" disabled={disabled} onClick={resetDefault}>恢复默认</Button>}</div>}
    <div className={selected ? styles.promptSplit : styles.promptFull}>
      {selected && <nav className={styles.promptNav} aria-label="提示词段落">{parts.map((part, i) =>
        <button key={part.label} type="button" aria-pressed={part === selected} onClick={() => setActive(i)}>{part.label}</button>)}</nav>}
      <div className={styles.promptEdit}>
        {onChange ? <><label htmlFor={id}>{label}</label>
          <Input.TextArea id={id} rows={selected ? 9 : 17} spellCheck={false} value={edit} disabled={disabled}
            onChange={event => onChange(value.slice(0, start) + mergeTextareaEdit(edit, event.target.value) + value.slice(end))} /></>
          : <><strong>{label}</strong><pre className={styles.promptText}>{edit}</pre></>}
      </div>
    </div>
    {variables && <details className={styles.help}><summary>模板变量说明</summary>
      <dl>{Object.entries(variables).map(([name, description]) => <div key={name}><dt><code>{'{{ ' + name + ' }}'}</code></dt><dd>{description}</dd></div>)}</dl>
    </details>}
    <Drawer title={role + ' · 完整系统提示词'} open={preview} onClose={() => setPreview(false)} size="large"
      styles={{ body: { padding: '16px 20px' } }}><pre className={styles.promptText}>{value}</pre></Drawer>
  </>;
}
