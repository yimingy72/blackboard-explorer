import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type McpServer, type McpServerInput, type McpTool, type PlatformModel, type PlatformModelInput } from '../api/client';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';
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

export default function PlatformCatalog({ kind, onDirtyChange }: { kind: 'models' | 'mcp'; onDirtyChange: (dirty: boolean) => void }) {
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

  function canLeave() { return !dirty || window.confirm('当前配置尚未保存，确定放弃本次修改吗？'); }
  function choose(item: PlatformModel | McpServer, force = false) {
    if (!force && (saving || !canLeave())) return;
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
      setModel(next); setOriginal(JSON.stringify(next));
    } else {
      const next: McpServerInput = { label: item.label, url: item.url, auth_header: item.auth_header,
        auth_scheme: item.auth_scheme, enabled: item.enabled };
      setServer(next); setOriginal(JSON.stringify(next));
    }
    setTools(null); setError(''); setNotice('');
  }
  function create() {
    if (saving || !canLeave()) return;
    inspectSequence.current += 1;
    setLoadingTools(false);
    setName(''); setOriginalName(''); setModel(blankModel); setServer(blankServer);
    setOriginal(JSON.stringify(kind === 'models' ? blankModel : blankServer));
    setTools(null); setError(''); setNotice('');
  }
  async function save() {
    if (saving) return;
    setSaving(true); setError(''); setNotice('');
    try {
      let saved: PlatformModel | McpServer;
      if (kind === 'models') {
        if (name) saved = await api.savePlatformModel(name, modelPayload(model));
        else saved = await api.createPlatformModel(modelPayload(model));
      } else if (name) {
        saved = await api.saveMcpServer(name, serverPayload(server));
      } else {
        saved = await api.createMcpServer(serverPayload(server));
      }
      await queryClient.invalidateQueries({ queryKey: kind === 'models' ? ['platform-models'] : ['mcp-servers'] });
      choose(saved, true);
      setNotice(`已保存 ${saved.label}。已有任务保持创建时的配置。`);
    } catch (cause) { setError(errorText(cause)); }
    finally { setSaving(false); }
  }
  async function inspect(item: McpServer) {
    const sequence = ++inspectSequence.current;
    setLoadingTools(true); setTools(null); setError('');
    try { const result = await api.listMcpTools(item.name, item.version); if (inspectSequence.current === sequence) setTools(result.tools); }
    catch (cause) { if (inspectSequence.current === sequence) setError(`工具清单读取失败：${errorText(cause)}`); }
    finally { if (inspectSequence.current === sequence) setLoadingTools(false); }
  }
  const active = items?.find((item) => item.name === name);
  const configuredCredentials = active && 'config' in active && active.config.provider === model.provider ? active.configured_credentials ?? [] : [];
  const provider = providers.data?.find((item) => item.id === model.provider);
  const efforts = reasoningOptions(provider, model.model);
  const showBaseUrl = Boolean(provider && (provider.base_url_required || provider.default_base_url !== 'provider-default'));
  async function setDefault() {
    if (!name || kind !== 'models' || saving) return;
    setSaving(true);
    setError('');
    try {
      await api.setDefaultPlatformModel(name);
      await queryClient.invalidateQueries({ queryKey: ['platform-models'] });
      setNotice('已设为平台默认模型，新建任务会自动选中。');
    } catch (cause) { setError(errorText(cause)); }
    finally { setSaving(false); }
  }
  const setPrice = (key: 'cache_hit_per_m' | 'cache_miss_per_m' | 'output_per_m', value: string) =>
    setModel({ ...model, price: { ...model.price, [key]: value === '' ? '' : Number(value) } });

  return <div className={styles.catalog}>
    <section className={styles.catalogList} aria-label={kind === 'models' ? '模型配置列表' : 'MCP 服务列表'}>
      <div className={styles.columnHeading}><h2>{kind === 'models' ? '模型配置' : 'MCP 服务'}</h2><span>{items?.length ?? 0}</span></div>
      {(kind === 'models' ? models.isLoading : servers.isLoading) ? <p className={styles.emptyColumn}>正在加载…</p>
        : (kind === 'models' ? models.error : servers.error) ? <p className={controls.error} role="alert">加载失败：{errorText(kind === 'models' ? models.error : servers.error)}</p>
          : <ul className={styles.list}>{items?.map((item) => <li key={item.name}><button type="button" disabled={saving} className={`${styles.listButton} ${item.name === name ? styles.selected : ''}`} onClick={() => choose(item)}><strong>{item.label}</strong>{'config' in item && <span className={styles.modelId}>{item.config.model}</span>}<span>{'is_default' in item && item.is_default ? '平台默认模型' : item.enabled ? '已启用' : '已停用'}</span></button></li>)}</ul>}
      <button type="button" className={`${controls.button} ${styles.newProfile}`} disabled={saving} onClick={create}>＋ 新增{kind === 'models' ? '模型' : 'MCP 服务'}</button>
    </section>
    <section className={styles.catalogEditor} aria-label={kind === 'models' ? '模型配置编辑' : 'MCP 服务编辑'}>
      {!original ? <div className={styles.emptyDetail}><h2>{kind === 'models' ? '选择任务使用的模型' : '连接外部工具'}</h2><p>{kind === 'models' ? '在左侧选择模型，管理连接、上下文容量和计费。默认模型会在创建任务时自动选中。' : '选择服务管理连接和认证，保存后可读取工具清单，再到 Worker 配置中启用。'}</p><button type="button" className={controls.button} onClick={create}>{kind === 'models' ? '添加模型' : '添加 MCP 服务'}</button></div> : <>
        <div className={styles.contentHeader}><div><h2>{active ? `编辑 ${active.label}` : `新增${kind === 'models' ? '模型配置' : 'MCP 服务'}`}</h2><p>{kind === 'models' ? '连接、上下文与计费统一管理，保存后用于新任务。' : '通过 Streamable HTTP 连接外部工具服务。'}</p></div></div>
        {error && <p className={styles.errorBanner} role="alert">{error}</p>}
        {notice && <p className={styles.noticeBanner} role="status">{notice}</p>}
        <form className={styles.catalogForm} onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <fieldset className={styles.catalogFields} disabled={saving} aria-label={kind === 'models' ? '模型连接设置' : 'MCP 连接设置'}>
          <div className={styles.catalogFooter}><label className={`${styles.checkRow} ${styles.wideField}`}><input type="checkbox" checked={draft.enabled} onChange={(event) => kind === 'models' ? setModel({ ...model, enabled: event.target.checked }) : setServer({ ...server, enabled: event.target.checked })} />启用此{kind === 'models' ? '模型' : '服务'}</label>
          <div className={`${styles.editorActions} ${styles.wideField}`}>{kind === 'models' && active && 'is_default' in active && !active.is_default && <button className={controls.button} type="button" disabled={saving || dirty || !active.enabled} onClick={() => void setDefault()}>设为默认模型</button>}<button className={`${controls.button} ${controls.primary}`} type="submit" disabled={saving || !dirty}>{saving ? '正在保存…' : '保存'}</button></div></div>
          {kind === 'models' ? <>
          <fieldset className={styles.formSection}><legend>基本连接</legend><div className={styles.formGrid}>
          <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>显示名称</span><input className={controls.input} value={draft.label} onChange={(event) => setModel({ ...model, label: event.target.value })} required /></label>
            <label className={controls.field}><span className={controls.label}>Provider</span><select className={controls.select} value={model.provider} onChange={(event) => { const next = providers.data?.find((item) => item.id === event.target.value); setModel({ ...model, provider: event.target.value, price: { ...model.price, billing_mode: event.target.value === 'deepseek' ? 'deepseek_schedule' : 'fixed' }, supports_vision: null, base_url: next?.default_base_url === 'provider-default' ? '' : next?.default_base_url ?? '', reasoning_effort: next?.supports_reasoning_effort ? model.reasoning_effort : 'none', provider_options: {}, credentials: {} }); }} disabled={providers.isLoading} required><option value="">选择 Provider</option>{providers.data?.map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}</select></label>
            <label className={controls.field}><span className={controls.label}>模型 ID</span><input className={controls.input} value={model.model} onChange={(event) => setModel({ ...model, model: event.target.value })} required /></label>
            {showBaseUrl && <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>Base URL</span><input className={controls.input} type="url" value={model.base_url === 'provider-default' ? '' : model.base_url} onChange={(event) => setModel({ ...model, base_url: event.target.value })} required={Boolean(provider?.base_url_required)} /><span className={controls.hint}>由服务容器访问；宿主机地址请使用 host.docker.internal。</span></label>}
            {providers.isError && <p className={`${controls.error} ${styles.wideField}`} role="alert">Provider 目录读取失败：{errorText(providers.error)} <button type="button" onClick={() => void providers.refetch()}>重试</button></p>}
            {provider?.options_fields.map((field) => <label className={controls.field} key={field.name}><span className={controls.label}>{field.label ?? field.name}</span><input className={controls.input} value={model.provider_options?.[field.name] ?? ''} onChange={(event) => setModel({ ...model, provider_options: { ...model.provider_options, [field.name]: event.target.value } })} required={field.required} /></label>)}
          </div></fieldset>
          <fieldset className={styles.formSection}><legend>上下文与能力</legend><p className={styles.sectionHint}>按模型实际能力填写，决定 Agent 何时交接上下文。</p><div className={styles.formGrid}>
            <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>上下文大小（token）</span><input className={controls.input} type="number" min="1" step="1" placeholder="留空沿用全局交接阈值" value={model.context_window ?? ''} onChange={(event) => setModel({ ...model, context_window: event.target.value === '' ? null : Number(event.target.value) })} /><span className={controls.hint}>{model.context_window != null && Number.isInteger(model.context_window) && model.context_window > 0 ? `新任务交接阈值 ${Math.max(1, Math.floor(model.context_window * 4 / 5)).toLocaleString()} token（80%），其余 20% 留给输出与黑板增量。` : '未设置时沿用全局交接阈值（默认 128,000 token）。'} 请勿超过服务商实际容量；修改不会改变已有任务。</span></label>
            {provider?.supports_reasoning_effort ? <label className={controls.field}><span className={controls.label}>推理强度</span><select className={controls.select} value={model.reasoning_effort} onChange={(event) => setModel({ ...model, reasoning_effort: event.target.value })}><option value="none">模型默认（不传参数）</option>{model.reasoning_effort !== 'none' && !efforts.includes(model.reasoning_effort) && <option value={model.reasoning_effort}>{model.reasoning_effort}（当前兼容设置）</option>}{efforts.map((value) => <option key={value} value={value}>{value}</option>)}</select><span className={controls.hint}>设置新任务的默认强度；创建任务时可以单独选择。</span>{model.provider === 'openai_compatible' && <span className={controls.hint}>推理强度需兼容接口支持；若不支持，请选模型默认。</span>}</label> : provider && <p className={`${controls.hint} ${styles.wideField}`}>该 Provider 当前使用模型默认推理设置，不提供推理强度选项。</p>}
            <label className={controls.field}><span className={controls.label}>图片输入</span><select className={controls.select} value={model.supports_vision == null ? 'auto' : String(model.supports_vision)} onChange={(event) => setModel({ ...model, supports_vision: event.target.value === 'auto' ? null : event.target.value === 'true' })}><option value="auto">由模型接口判断</option><option value="true">支持看图</option><option value="false">仅文本</option></select><span className={controls.hint}>需同时启用 Worker 的 view_image 工具；该选项不会让文本模型获得视觉能力。</span></label>
          </div></fieldset>
          <fieldset className={styles.formSection}><legend>访问凭据</legend><p className={styles.sectionHint}>凭据由平台加密保存，浏览器不会回显。留空可保留已有凭据。</p><div className={styles.formGrid}>
            {provider && !provider.credential_fields.length && <p className={styles.sectionHint}>该连接方式无需填写凭据。</p>}
            {provider?.credential_fields.map((field) => <label className={`${controls.field} ${styles.wideField}`} key={field.name}><span className={controls.label}>{field.label ?? field.name}</span>{field.name.endsWith('_json') ? <textarea className={controls.textarea} value={model.credentials?.[field.name] ?? ''} onChange={(event) => setModel({ ...model, credentials: { ...model.credentials, [field.name]: event.target.value } })} required={field.required && !provider.allow_no_auth && !configuredCredentials.includes(field.name)} /> : <input className={controls.input} type="password" autoComplete="new-password" value={model.credentials?.[field.name] ?? ''} onChange={(event) => setModel({ ...model, credentials: { ...model.credentials, [field.name]: event.target.value } })} required={field.required && !provider.allow_no_auth && !configuredCredentials.includes(field.name)} placeholder={configuredCredentials.includes(field.name) ? '已保存；留空沿用当前凭据' : provider.allow_no_auth ? '可留空' : '输入凭据'} />}<span className={controls.hint}>已有凭据不会显示在浏览器中。</span></label>)}
          </div></fieldset>
          <fieldset className={styles.formSection}><legend>人民币计费</legend><p className={styles.sectionHint}>每百万 token 的人民币单价，用于任务预算与费用估算。</p><div className={styles.formGrid}>
            <div className={styles.wideField}><div className={styles.priceFields}>{([['cache_hit_per_m', '缓存命中输入'], ['cache_miss_per_m', '缓存未命中输入'], ['output_per_m', '输出']] as const).map(([key, label]) => <label className={controls.field} key={key}><span className={controls.label}>{label}</span><input className={controls.input} type="number" min="0" step="any" value={model.price[key] ?? ''} onChange={(event) => setPrice(key, event.target.value)} required /></label>)}</div></div>
            <label className={controls.field}><span className={controls.label}>费用估算方式</span><select className={controls.select} value={model.price.billing_mode ?? 'fixed'} onChange={(event) => setModel({ ...model, price: { ...model.price, billing_mode: event.target.value as 'fixed' | 'deepseek_schedule' } })}><option value="fixed">固定费率</option><option value="deepseek_schedule">DeepSeek 官方峰谷时段</option></select></label>
            <label className={`${styles.checkRow} ${styles.wideField}`}><input type="checkbox" checked={Boolean(model.price.off_peak)} onChange={(event) => setModel({ ...model, price: { ...model.price, off_peak: event.target.checked } })} />上方录入的是闲时单价</label>
            <p className={`${controls.hint} ${styles.wideField}`}>{model.price.billing_mode === 'deepseek_schedule' ? '仅适用官方 DeepSeek 端点：按请求开始时刻切换峰谷价，闲时为高峰的一半。北京时间工作日 9–12 时、14–18 时为高峰，周末及已知节假日除外；当前日历覆盖 2026 年，其他年份工作日峰时按高峰保守估算。' : '按上方固定单价估算，不随时间切换。'} 估算包含推理输出，实际扣款以服务商账单为准。</p>
          </div></fieldset>
          </> : <>
          <fieldset className={styles.formSection}><legend>基本连接</legend><div className={styles.formGrid}>
          <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>显示名称</span><input className={controls.input} value={draft.label} onChange={(event) => setServer({ ...server, label: event.target.value })} required /></label>
            <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>Streamable HTTP URL</span><input className={controls.input} type="url" value={server.url} onChange={(event) => setServer({ ...server, url: event.target.value })} required /><span className={controls.hint}>连接从服务容器发出；访问宿主机服务请使用 host.docker.internal，127.0.0.1 指容器自身。</span></label>
          </div></fieldset>
          <fieldset className={styles.formSection}><legend>访问凭据</legend><div className={styles.formGrid}>
            <label className={controls.field}><span className={controls.label}>认证 Header</span><input className={controls.input} value={server.auth_header} onChange={(event) => setServer({ ...server, auth_header: event.target.value })} /></label>
            <label className={controls.field}><span className={controls.label}>认证 Scheme</span><input className={controls.input} value={server.auth_scheme} onChange={(event) => setServer({ ...server, auth_scheme: event.target.value })} /></label>
            <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>认证密钥</span><input className={controls.input} type="password" autoComplete="new-password" value={server.secret ?? ''} onChange={(event) => setServer({ ...server, secret: event.target.value })} placeholder={active && 'has_secret' in active && active.has_secret ? '已保存；留空沿用当前密钥' : '可留空'} /></label>
            {active && 'has_secret' in active && active.has_secret && <label className={`${styles.checkRow} ${styles.wideField}`}><input type="checkbox" checked={Boolean(server.clear_secret)} onChange={(event) => setServer({ ...server, clear_secret: event.target.checked })} />清除当前密钥</label>}
          </div></fieldset>
          </>}

          </fieldset>
        </form>
        {kind === 'mcp' && active && 'url' in active && <div className={styles.toolList}><button type="button" className={controls.button} disabled={loadingTools} onClick={() => void inspect(active)}>{loadingTools ? '正在读取…' : '查看工具清单'}</button>{tools && (tools.length ? <ul>{tools.map((tool) => <li key={tool.name}><strong>{tool.name}</strong><p>{tool.description}</p></li>)}</ul> : <p>该服务没有公开工具。</p>)}</div>}
      </>}
    </section>
  </div>;
}
