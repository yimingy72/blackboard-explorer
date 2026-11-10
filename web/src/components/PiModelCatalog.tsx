import { Alert, App, Button, Checkbox, Collapse, Empty, Form, Input, Table, Tag } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api, type PiModel, type PiModelInput, type PiUsdRates } from '../api/client';
import GatewayCatalog from './GatewayCatalog';
import LoadingState from './LoadingState';
import ReviewFees from './ReviewFees';
import styles from '../pages/ProfilesPage.module.css';
import controls from '../styles/controls.module.css';

const rateLabels: Record<keyof PiUsdRates, string> = {
    input: '输入', output: '输出', cache_read: '缓存读取', cache_write: '缓存写入',
};
const emptyRates = (): PiUsdRates => ({ input: '', output: '', cache_read: '', cache_write: '' });
const emptyModel = (): PiModelInput => ({
    label: '', upstream_model: '', base_url: '', context_window: 32768, max_output_tokens: 4096,
    input_modalities: ['text'], reasoning: false, tool_call: true, usd_rates: null,
    pricing_source: '', confirmed_free: false, enabled: true,
});
function draftOf(model?: PiModel): PiModelInput {
    if (!model) return emptyModel();
    return {
        label: model.label, upstream_model: model.upstream_model, base_url: model.base_url,
        context_window: model.context_window, max_output_tokens: model.max_output_tokens,
        input_modalities: model.input_modalities, reasoning: model.reasoning, tool_call: model.tool_call,
        usd_rates: model.usd_rates, pricing_source: model.pricing_source, confirmed_free: model.confirmed_free,
        enabled: model.enabled, expected_revision: model.model_revision,
    };
}

export default function PiModelCatalog({ onDirtyChange, onBusyChange }: {
    onDirtyChange: (value: boolean) => void; onBusyChange: (value: boolean) => void;
}) {
    const directory = useQuery({ queryKey: ['pi-models'], queryFn: api.piModels });
    const client = useQueryClient();
    const { modal } = App.useApp();
    const [selected, setSelected] = useState<PiModel>();
    const [draft, setDraft] = useState<PiModelInput | null>(null);
    const [rates, setRates] = useState<PiUsdRates>(emptyRates);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [busy, setBusy] = useState(false);
    const dirty = Boolean(draft && (JSON.stringify(draft) !== JSON.stringify(draftOf(selected))
        || JSON.stringify(rates) !== JSON.stringify(selected?.usd_rates ?? emptyRates())));
    useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false); }, [dirty, onDirtyChange]);
    useEffect(() => { onBusyChange(busy); return () => onBusyChange(false); }, [busy, onBusyChange]);
    function choose(model?: PiModel, force = false) {
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
    function patch(value: Partial<PiModelInput>) {
        if (!draft) return;
        setDraft({ ...draft, ...value });
        setNotice('');
    }
    async function save() {
        if (!draft || busy) return;
        const anyRate = Object.values(rates).some(value => value.trim());
        if (!draft.label.trim() || !draft.upstream_model.trim() || !draft.base_url.trim()) {
            setError('请填写显示名称、上游模型和 HTTPS 地址。'); return;
        }
        if (anyRate && (Object.values(rates).some(value => !/^\d+(\.\d+)?$/.test(value.trim())) || !draft.pricing_source.trim())) {
            setError('请填写四项非负费率，并注明报价来源。'); return;
        }
        if (draft.confirmed_free !== (anyRate && Object.values(rates).every(value => Number(value) === 0))) {
            setError('四项费率均为零时，请明确确认该模型免费。'); return;
        }
        setBusy(true);
        setError('');
        setNotice('');
        try {
            const saved = await api.savePiModel({
                ...draft, label: draft.label.trim(), upstream_model: draft.upstream_model.trim(),
                base_url: draft.base_url.trim(), secret: draft.secret || undefined,
                usd_rates: anyRate ? rates : null,
            }, selected?.id);
            choose(saved, true);
            setNotice('已保存新版本。既有任务保留原模型和费率。');
            await client.invalidateQueries({ queryKey: ['pi-models'] });
        } catch (cause) {
            setError(cause instanceof Error ? cause.message : '保存失败，草稿已保留。');
        } finally { setBusy(false); }
    }
    if (directory.isLoading) return <LoadingState label="正在读取平台模型目录" />;
    if (directory.data?.enabled === false) return <GatewayCatalog />;
    const data = directory.data;
    return <section className={styles.catalogEditor} aria-label="平台模型">
        <div className={styles.contentHeader}>
            <h2 className={styles.sectionTitle}>平台模型</h2>
            <div className={styles.headerActions}>
                <Button onClick={() => void directory.refetch()} loading={directory.isFetching}>重新读取</Button>
                {data?.can_manage && <Button type="primary" icon={<PlusOutlined />} aria-label="添加模型" disabled={busy} onClick={() => choose()}>添加模型</Button>}
            </div>
        </div>
        <div className={styles.formSection}>
            {directory.isError && <Alert type="warning" title="暂时无法读取平台模型目录" />}
            {data && !data.can_manage && <p className={controls.hint}>此站点仅查看模型。连接、密钥和费率由主站管理员维护。</p>}
            {data && !data.models.length ? <Empty description="尚未登记模型" /> : <Table
                size="small" rowKey="id" pagination={false} scroll={{ x: 620 }} dataSource={data?.models}
                columns={[
                    { title: '模型', dataIndex: 'label', render: (label, model) => <>{label} <small>· v{model.model_revision}</small></> },
                    { title: '上游', dataIndex: 'upstream_model', ellipsis: true },
                    { title: '凭据', render: (_, model) => model.has_secret ? '已配置' : '待配置' },
                    { title: '状态', render: (_, model) => <Tag>{!model.enabled ? '已停用' : !model.usd_rates ? '费率待配置' : !model.has_secret ? '凭据待配置' : model.tool_call ? '可用于任务' : '未启用工具'}</Tag> },
                    ...(data?.can_manage ? [{ title: '操作', render: (_: unknown, model: PiModel) => <Button type="link" disabled={busy} onClick={() => choose(model)}>编辑</Button> }] : []),
                ]}
            />}
        </div>
        {draft && data?.can_manage && <Form layout="vertical" className={styles.formSection} onFinish={() => void save()} disabled={busy}>
            <div className={styles.contentHeader}>
                <h3 className={styles.sectionTitle}>{selected ? `编辑 ${selected.label}` : '添加模型'}</h3>
                <Button type="primary" htmlType="submit" loading={busy} disabled={!dirty || busy}>保存模型</Button>
            </div>
            {error && <Alert type="error" title={error} showIcon />}
            {notice && <Alert type="success" title={notice} showIcon />}
            <Form.Item label="显示名称" htmlFor="pi-label" required><Input id="pi-label" value={draft.label} onChange={e => patch({ label: e.target.value })} maxLength={160} /></Form.Item>
            <Form.Item label="上游模型" htmlFor="pi-upstream" required><Input id="pi-upstream" value={draft.upstream_model} onChange={e => patch({ upstream_model: e.target.value })} maxLength={512} /></Form.Item>
            <Form.Item label="HTTPS 地址" htmlFor="pi-url" required><Input id="pi-url" value={draft.base_url} onChange={e => patch({ base_url: e.target.value })} placeholder="https://provider.example/v1" /></Form.Item>
            <Form.Item label="上游 API 密钥" htmlFor="pi-key"><Input.Password id="pi-key" value={draft.secret ?? ''} autoComplete="off" disabled={draft.clear_secret} placeholder={selected?.has_secret ? '留空保留现有密钥' : '输入提供商密钥'} onChange={e => patch({ secret: e.target.value })} /></Form.Item>
            {selected?.has_secret && <Checkbox checked={Boolean(draft.clear_secret)} onChange={e => patch({ clear_secret: e.target.checked, secret: undefined })}>清除现有密钥</Checkbox>}
            <Collapse ghost items={[
                { key: 'pricing', label: '费率（USD / 百万 token）', children: <>
                    <p className={controls.hint}>按提供商或中转商实际报价配置；留空可保存模型，但有预算的任务不能使用它。</p>
                    {Object.entries(rateLabels).map(([key, label]) => <Form.Item key={key} label={label} htmlFor={`pi-rate-${key}`}>
                        <Input id={`pi-rate-${key}`} inputMode="decimal" value={rates[key as keyof PiUsdRates]} onChange={e => { setRates({ ...rates, [key]: e.target.value }); setNotice(''); }} />
                    </Form.Item>)}
                    <Form.Item label="报价来源" htmlFor="pi-price-source"><Input id="pi-price-source" value={draft.pricing_source} onChange={e => patch({ pricing_source: e.target.value })} /></Form.Item>
                    <Checkbox checked={draft.confirmed_free} onChange={e => patch({ confirmed_free: e.target.checked })}>已确认该模型免费</Checkbox>
                </> },
                { key: 'capabilities', label: '能力与限制', children: <>
                    <Form.Item label="上下文长度" htmlFor="pi-context"><Input id="pi-context" type="number" min={1024} value={draft.context_window} onChange={e => patch({ context_window: Number(e.target.value) })} /></Form.Item>
                    <Form.Item label="最大输出 token" htmlFor="pi-max-output"><Input id="pi-max-output" type="number" min={1} value={draft.max_output_tokens} onChange={e => patch({ max_output_tokens: Number(e.target.value) })} /></Form.Item>
                    <Checkbox checked={draft.tool_call} onChange={e => patch({ tool_call: e.target.checked })}>工具调用</Checkbox>
                    <Checkbox checked={draft.reasoning} onChange={e => patch({ reasoning: e.target.checked })}>推理</Checkbox>
                    <Checkbox checked={draft.input_modalities.includes('image')} onChange={e => patch({ input_modalities: e.target.checked ? ['text', 'image'] : ['text'] })}>图片输入</Checkbox>
                </> },
            ]} />
            <Checkbox checked={draft.enabled} onChange={e => patch({ enabled: e.target.checked })}>启用此模型</Checkbox>
        </Form>}
        <ReviewFees direct />
    </section>;
}
