import { Alert, App, Button, Checkbox, Empty, Form, Input, Select, Switch, Tag } from 'antd';
import { DatabaseOutlined, ExperimentOutlined, PlusOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api, ApiError, piModelCanTest, type PiModel, type PiModelInput, type PiUsdRates } from '../api/client';
import PiModelTest from './PiModelTest';
import LoadingState from './LoadingState';
import { DetailHeading, SettingsFrame, SettingsHeading, SettingsRail } from '../settings/SettingsShell';
import { useSettingsLogin, useTrackSettings, type ChangeTracking } from '../settings/settingsChanges';
import styles from '../settings/Settings.module.css';

const rateLabels: Record<keyof PiUsdRates, string> = {
    input: '输入', output: '输出', cache_read: '缓存读取', cache_write: '缓存写入',
};
const emptyRates = (): PiUsdRates => ({ input: '', output: '', cache_read: '', cache_write: '' });
type ModelDraft = Omit<PiModelInput, 'context_window' | 'max_output_tokens' | 'tool_call' | 'usd_rates'> & {
    context_window: number | null; max_output_tokens: number | null; tool_call: boolean | null;
};
function draftOf(model?: PiModel): ModelDraft {
    if (!model) return {
        label: '', upstream_model: '', base_url: '', context_window: 32768, max_output_tokens: 4096,
        input_modalities: ['text'], reasoning: false, tool_call: true, enabled: true,
    };
    return {
        label: model.label, upstream_model: model.upstream_model, base_url: model.base_url,
        context_window: model.context_window, max_output_tokens: model.max_output_tokens,
        input_modalities: model.input_modalities, reasoning: model.reasoning, tool_call: model.tool_call,
        enabled: model.enabled, expected_revision: model.model_revision,
    };
}

function modelStatus(model: PiModel) {
    if (!model.enabled) return '已停用';
    if (!model.has_secret && !model.usd_rates) return '密钥与费率待配置';
    if (!model.has_secret) return '凭据待配置';
    if (model.context_window == null || model.max_output_tokens == null || model.tool_call == null) return '能力待完善';
    if (!model.usd_rates) return '费率待配置';
    return model.tool_call ? '可用于任务' : '未启用工具';
}

export default function PiModelCatalog(tracking: ChangeTracking) {
    const directory = useQuery({ queryKey: ['pi-models'], queryFn: api.piModels });
    useSettingsLogin(directory.error);
    const client = useQueryClient();
    const { modal } = App.useApp();
    const [selected, setSelected] = useState<PiModel>();
    const [testModel, setTestModel] = useState<PiModel>();
    const [draft, setDraft] = useState<ModelDraft | null>(null);
    const [rates, setRates] = useState<PiUsdRates>(emptyRates);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [busy, setBusy] = useState(false);
    const [testBusy, setTestBusy] = useState(false);
    const inFlight = useRef(false);
    const data = directory.data;
    const canManage = data?.enabled && data.can_manage && !directory.isError;
    const dirty = Boolean(draft && (JSON.stringify(draft) !== JSON.stringify(draftOf(selected))
        || JSON.stringify(rates) !== JSON.stringify(selected?.usd_rates ?? emptyRates())));
    useTrackSettings(dirty, busy || testBusy, tracking);
    useEffect(() => {
        const first = data?.models[0];
        if (!draft && first) { setSelected(first); setDraft(draftOf(first)); setRates(first.usd_rates ?? emptyRates()); }
    }, [data, draft]);
    function choose(model?: PiModel, force = false) {
        if (inFlight.current && !force) return;
        const apply = () => {
            setSelected(model);
            setDraft(draftOf(model));
            setRates(model?.usd_rates ?? emptyRates());
            setError('');
            setNotice('');
        };
        if (dirty && !force) modal.confirm({ title: '放弃尚未保存的修改？', okText: '放弃修改', cancelText: '继续编辑', onOk: apply });
        else apply();
    }
    function patch(value: Partial<ModelDraft>) {
        if (!draft) return;
        setDraft({ ...draft, ...value });
        setNotice('');
    }
    async function save() {
        if (!draft || !canManage || inFlight.current) return;
        const anyRate = Object.values(rates).some(value => value.trim());
        if (!draft.label.trim() || !draft.upstream_model.trim() || !/^https:\/\//i.test(draft.base_url.trim())) {
            setError('请填写显示名称、上游模型和 HTTPS 地址。'); return;
        }
        if (draft.context_window == null || draft.max_output_tokens == null || draft.tool_call == null || !draft.input_modalities.includes('text')) {
            setError('请在能力与限制中填写已确认的上下文长度、最大输出，确认文本输入及是否启用工具。'); return;
        }
        if (anyRate && Object.values(rates).some(value => !/^\d+(\.\d+)?$/.test(value.trim()))) {
            setError('请填写四项非负费率。'); return;
        }
        inFlight.current = true;
        setBusy(true);
        setError('');
        setNotice('');
        try {
            const saved = await api.savePiModel({
                ...draft, context_window: draft.context_window, max_output_tokens: draft.max_output_tokens, tool_call: draft.tool_call,
                label: draft.label.trim(), upstream_model: draft.upstream_model.trim(),
                base_url: draft.base_url.trim(), secret: draft.secret || undefined,
                usd_rates: anyRate ? rates : null,
            }, selected?.id);
            choose(saved, true);
            setNotice('模型已保存。');
            await client.invalidateQueries({ queryKey: ['pi-models'] });
        } catch (cause) {
            setError(cause instanceof ApiError && cause.status === 409
                ? '模型版本已更新。草稿已保留，可复制后重新读取。'
                : cause instanceof Error ? cause.message : '保存失败，草稿已保留。');
        } finally { inFlight.current = false; setBusy(false); }
    }
    async function reload() {
        if (inFlight.current || testBusy) return;
        if (dirty && !await new Promise<boolean>(resolve => modal.confirm({
            title: '放弃草稿并重新读取？', okText: '放弃并读取', cancelText: '继续编辑', onOk: () => resolve(true), onCancel: () => resolve(false),
        }))) return;
        if (inFlight.current) return;
        inFlight.current = true; setBusy(true);
        try {
            const result = await directory.refetch({ throwOnError: true });
            const models = result.data?.models ?? [];
            const next = models.find(model => model.id === selected?.id) ?? models[0];
            if (next) choose(next, true);
            else { setDraft(null); setSelected(undefined); setRates(emptyRates()); setError(''); setNotice(''); }
        } catch (cause) { setError(cause instanceof Error ? cause.message : '无法读取模型目录。'); }
        finally { inFlight.current = false; setBusy(false); }
    }

    return <div className={styles.page}>
      <SettingsHeading title="模型管理" description="集中管理任务和 Agent 可引用的模型连接、能力与费率。" actions={<>
        <Button disabled={busy || testBusy} onClick={() => void reload()}>重新读取</Button>
        {canManage && <Button icon={<PlusOutlined aria-hidden="true" />} disabled={busy || testBusy} onClick={() => choose()}>添加模型</Button>}
      </>} />
      {directory.isLoading ? <LoadingState label="正在读取平台模型目录" /> : <>
        {directory.isError && <Alert className={styles.feedback} type="error" showIcon title="无法读取平台模型目录" />}
        {data?.enabled === false && <Alert className={styles.feedback} type="warning" showIcon title="平台模型目录暂不可用" />}
        {data?.enabled && !data.can_manage && <Alert className={styles.feedback} type="info" showIcon title="此站点只读，连接、密钥和费率由主站管理员维护。" />}
        <SettingsFrame>
          <SettingsRail title="模型列表" intro="连接、能力限制与计费参数" note="模型可供任务和角色引用。"
            items={(data?.models ?? []).map(model => ({ id: model.id, title: model.label, caption: model.upstream_model, icon: <DatabaseOutlined /> }))}
            active={selected?.id ?? ''} disabled={busy || testBusy}
            onSelect={id => { const model = data?.models.find(item => item.id === id); if (model && id !== selected?.id) choose(model); }} />
          <section className={styles.main} aria-label="模型配置编辑">
            {!draft ? <Empty className={styles.emptyPanel} description="尚未登记模型" /> : <>
              <DetailHeading icon={<DatabaseOutlined />} title={selected?.label ?? '添加模型'} description="OpenAI 兼容接口"
                actions={<>
                  {selected && <Tag color={modelStatus(selected) === '可用于任务' ? 'processing' : selected.enabled ? 'warning' : 'default'}>{modelStatus(selected)}</Tag>}
                  {selected && data?.can_test && <Button icon={<ExperimentOutlined aria-hidden="true" />} disabled={busy || !piModelCanTest(selected)}
                    title={piModelCanTest(selected) ? '测试已保存版本' : '请启用模型、配置密钥和文本能力后测试'}
                    onClick={() => setTestModel(selected)}>测试模型</Button>}
                  {canManage && <Button type="primary" form="model-config" htmlType="submit" loading={busy} disabled={!dirty || busy}>保存模型</Button>}
                </>} />
              <Form id="model-config" layout="vertical" className={styles.body} onFinish={() => void save()} disabled={busy || !canManage}>
                {error && <Alert className={styles.feedback} type="error" title={error} showIcon />}
                {notice && <Alert className={styles.feedback} role="status" type="success" title={notice} showIcon />}
                <section className={styles.formBlock}><h3>连接信息</h3><div className={`${styles.fields} ${styles.modelConnection}`}>
                  <Form.Item label="显示名称" htmlFor="pi-label" required><Input id="pi-label" value={draft.label} onChange={e => patch({ label: e.target.value })} maxLength={160} /></Form.Item>
                  <Form.Item label="上游模型" htmlFor="pi-upstream" required><Input id="pi-upstream" value={draft.upstream_model} onChange={e => patch({ upstream_model: e.target.value })} maxLength={512} /></Form.Item>
                  <Form.Item label="HTTPS Base URL" htmlFor="pi-url" required><Input id="pi-url" value={draft.base_url} onChange={e => patch({ base_url: e.target.value })} placeholder="https://provider.example/v1" /></Form.Item>
                  <Form.Item label="上游 API 密钥" htmlFor="pi-key"><Input.Password id="pi-key" value={draft.secret ?? ''} autoComplete="new-password"
                    disabled={draft.clear_secret || !canManage || busy} placeholder={selected?.has_secret ? '留空保留现有密钥' : '待配置'}
                    onChange={e => patch({ secret: e.target.value })} /></Form.Item>
                </div>
                {selected?.has_secret && canManage && <Checkbox checked={Boolean(draft.clear_secret)} onChange={e => {
                    if (!e.target.checked) patch({ clear_secret: undefined });
                    else modal.confirm({ title: '清除现有密钥？', content: '保存后，新任务将无法使用此模型，直到重新配置密钥。', okText: '确认清除', cancelText: '取消',
                        onOk: () => patch({ clear_secret: true, secret: undefined }) });
                }}>清除现有密钥</Checkbox>}
                </section>
                <section className={styles.formBlock}><h3>能力与限制</h3><div className={`${styles.fields} ${styles.three}`}>
                  <Form.Item label="上下文长度（token）" htmlFor="pi-context"><Input id="pi-context" type="number" min={1024}
                    value={draft.context_window ?? ''} placeholder="待确认" onChange={e => patch({ context_window: e.target.value ? Number(e.target.value) : null })} /></Form.Item>
                  <Form.Item label="最大输出 token" htmlFor="pi-max-output"><Input id="pi-max-output" type="number" min={1}
                    value={draft.max_output_tokens ?? ''} placeholder="待确认" onChange={e => patch({ max_output_tokens: e.target.value ? Number(e.target.value) : null })} /></Form.Item>
                  <Form.Item label="工具调用" htmlFor="pi-tool-call"><Select id="pi-tool-call" value={draft.tool_call == null ? undefined : String(draft.tool_call)}
                    placeholder="待确认" options={[{ value: 'true', label: '启用' }, { value: 'false', label: '不启用' }]}
                    onChange={value => patch({ tool_call: value === 'true' })} /></Form.Item>
                </div><div className={styles.actions}>
                  <Checkbox checked={draft.input_modalities.includes('text')} onChange={e => patch({ input_modalities: e.target.checked ? ['text'] : [] })}>文本输入</Checkbox>
                  <Checkbox disabled={!draft.input_modalities.includes('text') || !canManage || busy} checked={draft.input_modalities.includes('image')}
                    onChange={e => patch({ input_modalities: e.target.checked ? ['text', 'image'] : ['text'] })}>图片输入</Checkbox>
                  <Checkbox checked={draft.reasoning} onChange={e => patch({ reasoning: e.target.checked })}>推理</Checkbox>
                  <label className={styles.switchLabel}><Switch size="small" aria-label="启用此模型" checked={draft.enabled} onChange={enabled => patch({ enabled })} />启用此模型</label>
                </div></section>
                <section className={styles.formBlock}><h3>计费费率 <small>USD / 百万 token</small></h3>
                  <div className={`${styles.fields} ${styles.four}`}>{Object.entries(rateLabels).map(([key, label]) =>
                    <Form.Item key={key} label={label} htmlFor={`pi-rate-${key}`}><Input id={`pi-rate-${key}`} inputMode="decimal"
                      placeholder="待配置" value={rates[key as keyof PiUsdRates]} onChange={e => { setRates({ ...rates, [key]: e.target.value }); setNotice(''); }} /></Form.Item>)}</div>
                  <p className={styles.formHint}>四项均填 0 表示免费。</p>
                </section>
              </Form>
            </>}
          </section>
        </SettingsFrame>
      </>}
      {testModel && <PiModelTest model={testModel} onClose={() => setTestModel(undefined)} onBusyChange={setTestBusy} />}
    </div>;
}
