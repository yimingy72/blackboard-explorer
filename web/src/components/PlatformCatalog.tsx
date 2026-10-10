import { App, Button, Checkbox, Collapse, Alert, Empty, Form, Input, Select, Switch, Tag } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type McpServer, type McpServerInput, type McpTool, type PlatformModel, type PlatformModelInput } from '../api/client';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';
import { PlusOutlined } from '@ant-design/icons';
import { reasoningOptions } from './reasoningOptions';
const blankModel: PlatformModelInput = {
    label: '', provider: '', model: '', base_url: '', reasoning_effort: 'none',
    supports_vision: null, context_window: null,
    price: { currency: 'CNY', cache_hit_per_m: '', cache_miss_per_m: '', output_per_m: '', off_peak: false, billing_mode: 'fixed' },
    provider_options: {}, credentials: {}, enabled: true,
};
const blankServer: McpServerInput = { label: '', url: '', auth_header: 'Authorization', auth_scheme: 'Bearer', enabled: true };
function errorText(error: unknown) { return error instanceof Error ? error.message : '操作失败。'; }
function modelPayload(model: PlatformModelInput): PlatformModelInput {
    return {
        ...model,
        credentials: Object.fromEntries(Object.entries(model.credentials ?? {}).filter(([, value]) => value)),
    };
}
function serverPayload(server: McpServerInput): McpServerInput {
    return { ...server, secret: server.secret || undefined };
}
export default function PlatformCatalog({ kind, onDirtyChange, onBusyChange }: {
    kind: 'models' | 'mcp';
    onDirtyChange: (dirty: boolean) => void;
    onBusyChange?: (busy: boolean) => void;
}) {
    const { modal } = App.useApp();
    const queryClient = useQueryClient();
    const models = useQuery({ queryKey: ['platform-models'], queryFn: api.listPlatformModels, enabled: kind === 'models' });
    const providers = useQuery({ queryKey: ['platform-providers'], queryFn: api.listProviders, enabled: kind === 'models' });
    const servers = useQuery({ queryKey: ['mcp-servers'], queryFn: api.listMcpServers, enabled: kind === 'mcp' });
    const [name, setName] = useState('');
    const [model, setModel] = useState<PlatformModelInput>(blankModel);
    const [server, setServer] = useState<McpServerInput>(blankServer);
    const [original, setOriginal] = useState('');
    const [originalName, setOriginalName] = useState('');
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [tools, setTools] = useState<McpTool[] | null>(null);
    const [loadingTools, setLoadingTools] = useState(false);
    const inspectSequence = useRef(0);
    const draft = kind === 'models' ? model : server;
    const dirty = Boolean(original) && (JSON.stringify(draft) !== original || name !== originalName);
    useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
    const items = kind === 'models' ? models.data : servers.data;
    async function canLeave() { return !dirty || await new Promise<boolean>((resolve) => { modal.confirm({ title: '当前配置尚未保存，确定放弃本次修改吗？', okText: '放弃修改', cancelText: '继续编辑', onOk: () => resolve(true), onCancel: () => resolve(false) }); }); }
    async function choose(item: PlatformModel | McpServer, force = false) {
        if (!force && (saving || !await canLeave()))
            return;
        inspectSequence.current += 1;
        setLoadingTools(false);
        setName(item.name);
        setOriginalName(item.name);
        if ('config' in item) {
            const next: PlatformModelInput = {
                label: item.label, provider: item.config.provider as PlatformModelInput['provider'], model: item.config.model,
                base_url: item.config.base_url, reasoning_effort: item.config.reasoning_effort,
                supports_vision: item.config.supports_vision ?? null, context_window: item.config.context_window ?? null,
                price: { ...blankModel.price, ...item.config.price, currency: 'CNY' }, provider_options: item.config.provider_options ?? {}, credentials: {}, enabled: item.enabled,
            };
            setModel(next);
            setOriginal(JSON.stringify(next));
        }
        else {
            const next: McpServerInput = { label: item.label, url: item.url, auth_header: item.auth_header,
                auth_scheme: item.auth_scheme, enabled: item.enabled };
            setServer(next);
            setOriginal(JSON.stringify(next));
        }
        setTools(null);
        setError('');
        setNotice('');
    }
    async function create() {
        if (saving || !await canLeave())
            return;
        inspectSequence.current += 1;
        setLoadingTools(false);
        setName('');
        setOriginalName('');
        setModel(blankModel);
        setServer(blankServer);
        setOriginal(JSON.stringify(kind === 'models' ? blankModel : blankServer));
        setTools(null);
        setError('');
        setNotice('');
    }
    async function save() {
        if (saving)
            return;
        const validation = kind === 'models' ? validateModel() : validateServer();
        if (validation) { setError(validation); return; }
        setSaving(true); onBusyChange?.(true);
        setError('');
        setNotice('');
        try {
            let saved: PlatformModel | McpServer;
            if (kind === 'models') {
                if (name)
                    saved = await api.savePlatformModel(name, modelPayload(model));
                else
                    saved = await api.createPlatformModel(modelPayload(model));
            }
            else if (name) {
                saved = await api.saveMcpServer(name, serverPayload(server));
            }
            else {
                saved = await api.createMcpServer(serverPayload(server));
            }
            await queryClient.invalidateQueries({ queryKey: kind === 'models' ? ['platform-models'] : ['mcp-servers'] });
            choose(saved, true);
            setNotice(`已保存 ${saved.label}。已有任务保持创建时的配置。`);
        }
        catch (cause) {
            setError(errorText(cause));
        }
        finally {
            setSaving(false); onBusyChange?.(false);
        }
    }
    async function inspect(item: McpServer) {
        const sequence = ++inspectSequence.current;
        setLoadingTools(true);
        setTools(null);
        setError('');
        try {
            const result = await api.listMcpTools(item.name, item.version);
            if (inspectSequence.current === sequence)
                setTools(result.tools);
        }
        catch (cause) {
            if (inspectSequence.current === sequence)
                setError(`工具清单读取失败：${errorText(cause)}`);
        }
        finally {
            if (inspectSequence.current === sequence)
                setLoadingTools(false);
        }
    }
    const active = items?.find((item) => item.name === name);
    const configuredCredentials = active && 'config' in active && active.config.provider === model.provider ? active.configured_credentials ?? [] : [];
    const provider = providers.data?.find((item) => item.id === model.provider);
    const efforts = reasoningOptions(provider, model.model);
    const showBaseUrl = Boolean(provider && (provider.base_url_required || provider.default_base_url !== 'provider-default'));
    async function setDefault() {
        if (!name || kind !== 'models' || saving)
            return;
        setSaving(true); onBusyChange?.(true);
        setError('');
        try {
            await api.setDefaultPlatformModel(name);
            await queryClient.invalidateQueries({ queryKey: ['platform-models'] });
            setNotice('已设为平台默认模型，新建任务会自动选中。');
        }
        catch (cause) {
            setError(errorText(cause));
        }
        finally {
            setSaving(false); onBusyChange?.(false);
        }
    }
    const setPrice = (key: 'cache_hit_per_m' | 'cache_miss_per_m' | 'output_per_m', value: string) => setModel({ ...model, price: { ...model.price, [key]: value === '' ? '' : Number(value) } });
    function validUrl(value: string) { try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol); } catch { return false; } }
    function validateModel(): string {
        if (!model.label.trim()) return '请填写显示名称。';
        if (!provider) return '请选择有效的 Provider。';
        if (!model.model.trim()) return '请填写模型 ID。';
        if ((provider.base_url_required || showBaseUrl && Boolean(model.base_url)) && !validUrl(model.base_url)) return '请填写有效的 Base URL。';
        if (model.context_window != null && (!Number.isInteger(model.context_window) || model.context_window < 1)) return '上下文大小必须为正整数。';
        for (const field of provider.options_fields) if (field.required && !model.provider_options?.[field.name]?.trim()) return `请填写 ${field.label ?? field.name}。`;
        for (const field of provider.credential_fields) if (field.required && !provider.allow_no_auth && !configuredCredentials.includes(field.name) && !model.credentials?.[field.name]?.trim()) return `请填写 ${field.label ?? field.name}。`;
        for (const key of ['cache_hit_per_m','cache_miss_per_m','output_per_m'] as const) if (model.price[key] === '' || !Number.isFinite(Number(model.price[key])) || Number(model.price[key]) < 0) return '请填写完整的非负计费单价。';
        return '';
    }
    function validateServer(): string {
        if (!server.label.trim()) return '请填写显示名称。';
        if (!validUrl(server.url)) return '请填写有效的 Streamable HTTP URL。';
        return '';
    }
    // Initialize the editor once; polling must never replace an unsaved draft.
    useEffect(() => { if (!original && items?.length) void choose(items.find(item => 'is_default' in item && item.is_default) ?? items[0], true); });
    const prices = <div className={styles.formGrid}>
      {([['cache_hit_per_m','缓存命中输入'],['cache_miss_per_m','缓存未命中输入'],['output_per_m','输出']] as const).map(([key,label]) => <label className={controls.field} key={key}><span className={controls.label}>{label} / 百万 token</span><Input aria-label={label} type="number" min="0" step="any" value={model.price[key] ?? ''} onChange={event=>setPrice(key,event.target.value)} prefix="¥" /></label>)}
      <label className={controls.field}><span className={controls.label}>费用估算方式</span><Select aria-label="费用估算方式" value={model.price.billing_mode ?? 'fixed'} onChange={value=>setModel({...model,price:{...model.price,billing_mode:value}})} options={[{value:'fixed',label:'固定费率'},{value:'deepseek_schedule',label:'DeepSeek 官方峰谷时段'}]} /></label>
      <Checkbox checked={Boolean(model.price.off_peak)} onChange={event=>setModel({...model,price:{...model.price,off_peak:event.target.checked}})}>上方录入的是闲时单价</Checkbox>
      <div className={styles.wideField}><p className={controls.hint}>{model.price.billing_mode === 'deepseek_schedule' ? '按官方端点请求开始时刻估算峰谷价；网关请使用固定费率。' : '按固定单价估算。'} 实际扣款以服务商账单为准。</p></div>
    </div>;
    return <section className={styles.catalogEditor} aria-label={kind === 'models' ? '模型配置编辑' : 'MCP 服务编辑'}>
      <Form component={false} disabled={saving}>
      <form className={styles.catalogForm} noValidate onSubmit={event=>{event.preventDefault();void save();}}>
        <header className={styles.contentHeader}>
          <div className={styles.connectionPicker}><Select aria-label={kind==='models'?'模型连接':'MCP 服务'} className={styles.resourceSelect} value={name || undefined} placeholder={original ? `新增${kind==='models'?'模型':'服务'}` : '选择连接'} loading={kind==='models'?models.isLoading:servers.isLoading} options={items?.map(item=>({value:item.name,label:item.label})) ?? []} onChange={value=>{const item=items?.find(entry=>entry.name===value);if(item)void choose(item);}} />{active && 'is_default' in active && active.is_default && <Tag>默认</Tag>}<Button type="text" icon={<PlusOutlined />} onClick={()=>void create()} disabled={saving}>新增{kind==='models'?'模型':'MCP 服务'}</Button></div>
          {original && <div className={styles.headerActions}><Switch aria-label={kind==='models'?'启用此模型':'启用此服务'} size="small" checked={draft.enabled} onChange={checked=>kind==='models'?setModel({...model,enabled:checked}):setServer({...server,enabled:checked})} /><span className={controls.hint}>启用</span>{kind==='models'&&active&&'is_default' in active&&!active.is_default&&<Button size="small" disabled={saving||dirty||!active.enabled} onClick={()=>void setDefault()}>设为默认模型</Button>}<Button size="small" type="primary" htmlType="submit" loading={saving} disabled={saving||!dirty}>保存</Button></div>}
        </header>
        {error && <Alert type="error" title={error} showIcon className={styles.feedback} />}{notice && <Alert role="status" type="success" title={notice} showIcon className={styles.feedback} />}
        {(kind==='models'?models.isError:servers.isError) && <Alert type="error" title={`加载失败：${errorText(kind==='models'?models.error:servers.error)}`} action={<Button onClick={()=>{void (kind==='models'?models.refetch():servers.refetch());}}>重试</Button>} />}
        {!original ? <Empty className={styles.emptyDetail} description={kind==='models'?'添加或选择模型':'添加或选择 MCP 服务'} /> : <div className={styles.catalogFields}>
          {kind==='models'?<>
          <div className={styles.formSection}><div className={styles.formGrid}>
            <label className={controls.field}><span className={controls.label}>显示名称</span><Input aria-label="显示名称" value={model.label} onChange={event=>setModel({...model,label:event.target.value})} /></label>
            <label className={controls.field}><span className={controls.label}>Provider</span><Select aria-label="Provider" value={model.provider || undefined} placeholder="选择 Provider" loading={providers.isLoading} disabled={saving||providers.isLoading} options={providers.data?.map(item=>({value:item.id,label:item.label})) ?? []} onChange={value=>{const next=providers.data?.find(item=>item.id===value);setModel({...model,provider:value,price:{...model.price,billing_mode:value==='deepseek'?'deepseek_schedule':'fixed'},supports_vision:null,base_url:next?.default_base_url==='provider-default'?'':next?.default_base_url??'',reasoning_effort:next?.supports_reasoning_effort?model.reasoning_effort:'none',provider_options:{},credentials:{}});}} /></label>
            <label className={controls.field}><span className={controls.label}>模型 ID</span><Input aria-label="模型 ID" value={model.model} onChange={event=>setModel({...model,model:event.target.value})} /></label>
            {showBaseUrl && <label className={`${controls.field} ${styles.spanTwo}`}><span className={controls.label}>Base URL</span><Input aria-label="Base URL" value={model.base_url==='provider-default'?'':model.base_url} onChange={event=>setModel({...model,base_url:event.target.value})} /></label>}
            {provider?.credential_fields.map(field=><label className={`${controls.field} ${field.name.endsWith('_json')?styles.wideField:''}`} key={field.name}><span className={controls.label}>{field.label??field.name}</span>{field.name.endsWith('_json')?<Input.TextArea aria-label={field.label??field.name} autoSize={{minRows:2,maxRows:6}} value={model.credentials?.[field.name]??''} onChange={event=>setModel({...model,credentials:{...model.credentials,[field.name]:event.target.value}})} />:<Input.Password aria-label={field.label??field.name} autoComplete="new-password" value={model.credentials?.[field.name]??''} placeholder={configuredCredentials.includes(field.name)?'已保存；留空沿用当前凭据':provider.allow_no_auth?'可留空':'输入凭据'} onChange={event=>setModel({...model,credentials:{...model.credentials,[field.name]:event.target.value}})} />}</label>)}
            {provider?.options_fields.map(field=><label className={controls.field} key={field.name}><span className={controls.label}>{field.label??field.name}</span><Input aria-label={field.label??field.name} value={model.provider_options?.[field.name]??''} onChange={event=>setModel({...model,provider_options:{...model.provider_options,[field.name]:event.target.value}})} /></label>)}
            <label className={controls.field}><span className={controls.label}>上下文大小（token）</span><Input aria-label="上下文大小（token）" type="number" min="1" step="1" placeholder="留空沿用全局阈值" value={model.context_window??''} onChange={event=>setModel({...model,context_window:event.target.value===''?null:Number(event.target.value)})} /></label>
            {provider?.supports_reasoning_effort?<label className={controls.field}><span className={controls.label}>推理强度</span><Select aria-label="推理强度" value={model.reasoning_effort} onChange={value=>setModel({...model,reasoning_effort:value})} options={[{value:'none',label:'模型默认（不传参数）'},...(model.reasoning_effort!=='none'&&!efforts.includes(model.reasoning_effort)?[{value:model.reasoning_effort,label:`${model.reasoning_effort}（当前兼容设置）`}]:[]),...efforts.map(value=>({value,label:value}))]} /></label>:<label className={controls.field}><span className={controls.label}>推理强度</span><Input value="模型默认" readOnly disabled /></label>}
            <label className={controls.field}><span className={controls.label}>图片输入</span><Select aria-label="图片输入" value={model.supports_vision==null?'auto':String(model.supports_vision)} onChange={value=>setModel({...model,supports_vision:value==='auto'?null:value==='true'})} options={[{value:'auto',label:'由模型接口判断'},{value:'true',label:'支持看图'},{value:'false',label:'仅文本'}]} /></label>
          </div>{providers.isError&&<Alert type="error" title="Provider 目录读取失败" action={<Button onClick={()=>void providers.refetch()}>重试</Button>} />}</div>
          <Collapse className={styles.catalogAdvanced} ghost size="small" items={[{key:'prices',label:'人民币计费',forceRender:true,children:prices},{key:'help',label:'连接与上下文说明',children:<>{model.provider==='openai_compatible'&&<p className={controls.hint}>推理强度需兼容接口支持；若不支持，请选模型默认。</p>}<p className={controls.hint}>宿主机服务使用 host.docker.internal。凭据仅写入，留空保留已保存的值。</p><p className={controls.hint}>{model.context_window!=null&&Number.isInteger(model.context_window)&&model.context_window>0?`新任务交接阈值 ${Math.max(1,Math.floor(model.context_window*4/5)).toLocaleString()} token（80%），其余 20% 留给输出与黑板增量。`:'未设置时沿用全局交接阈值（默认 128,000 token）。'} 修改不会改变已有任务。</p></>}]} />
          </>:<div className={styles.formSection}><div className={styles.formGrid}>
            <label className={controls.field}><span className={controls.label}>显示名称</span><Input aria-label="显示名称" value={server.label} onChange={event=>setServer({...server,label:event.target.value})} /></label>
            <label className={`${controls.field} ${styles.spanTwo}`}><span className={controls.label}>Streamable HTTP URL</span><Input aria-label="Streamable HTTP URL" value={server.url} onChange={event=>setServer({...server,url:event.target.value})} /></label>
            <label className={controls.field}><span className={controls.label}>认证 Header</span><Input aria-label="认证 Header" value={server.auth_header} onChange={event=>setServer({...server,auth_header:event.target.value})} /></label>
            <label className={controls.field}><span className={controls.label}>认证 Scheme</span><Input aria-label="认证 Scheme" value={server.auth_scheme} onChange={event=>setServer({...server,auth_scheme:event.target.value})} /></label>
            <label className={controls.field}><span className={controls.label}>认证密钥</span><Input.Password aria-label="认证密钥" autoComplete="new-password" value={server.secret??''} placeholder={active&&'has_secret' in active&&active.has_secret?'已保存；留空沿用当前密钥':'可留空'} onChange={event=>setServer({...server,secret:event.target.value})} /></label>
            {active&&'has_secret' in active&&active.has_secret&&<Checkbox className={styles.wideField} checked={Boolean(server.clear_secret)} onChange={event=>setServer({...server,clear_secret:event.target.checked})}>清除当前密钥</Checkbox>}
          </div>{active&&'url' in active&&<Collapse ghost size="small" items={[{key:'tools',label:'工具清单',children:<><Button loading={loadingTools} disabled={saving||loadingTools} onClick={()=>void inspect(active)}>查看工具清单</Button>{tools&&(tools.length?<ul className={styles.toolList}>{tools.map(tool=><li key={tool.name}><strong>{tool.name}</strong><p>{tool.description}</p></li>)}</ul>:<p className={controls.hint}>该服务没有公开工具。</p>)}</>}]} />}</div>}
        </div>}
      </form></Form>
    </section>;
}
