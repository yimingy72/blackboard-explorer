import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type McpServer, type McpServerInput, type McpTool, type PlatformModel, type PlatformModelInput } from '../api/client';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';

const blankModel: PlatformModelInput = {
  label: '', provider: 'openai_compatible', model: '', base_url: '', reasoning_effort: 'none',
  price: { currency: 'CNY', cache_hit_per_m: '', cache_miss_per_m: '', output_per_m: '', off_peak: false },
  credential_source: 'stored', enabled: true,
};
const blankServer: McpServerInput = { label: '', url: '', auth_header: 'Authorization', auth_scheme: 'Bearer', enabled: true };

function errorText(error: unknown) { return error instanceof Error ? error.message : '操作失败。'; }

export default function PlatformCatalog({ kind, onDirtyChange }: { kind: 'models' | 'mcp'; onDirtyChange: (dirty: boolean) => void }) {
  const queryClient = useQueryClient();
  const models = useQuery({ queryKey: ['platform-models'], queryFn: api.listPlatformModels, enabled: kind === 'models' });
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
  const draft = kind === 'models' ? model : server;
  const dirty = Boolean(original) && (JSON.stringify(draft) !== original || name !== originalName);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  const items = kind === 'models' ? models.data : servers.data;

  function canLeave() { return !dirty || window.confirm('当前配置尚未保存，确定放弃本次修改吗？'); }
  function choose(item: PlatformModel | McpServer, force = false) {
    if (!force && !canLeave()) return;
    setName(item.name);
    setOriginalName(item.name);
    if ('config' in item) {
      const next: PlatformModelInput = {
        label: item.label, provider: item.config.provider as PlatformModelInput['provider'], model: item.config.model,
        base_url: item.config.base_url, reasoning_effort: item.config.reasoning_effort,
        price: { ...blankModel.price, ...item.config.price, currency: 'CNY' }, credential_source: item.credential_source, enabled: item.enabled,
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
    if (!canLeave()) return;
    setName(''); setOriginalName(''); setModel(blankModel); setServer(blankServer);
    setOriginal(JSON.stringify(kind === 'models' ? blankModel : blankServer));
    setTools(null); setError(''); setNotice('');
  }
  async function save() {
    if (!/^[a-z][a-z0-9_-]{0,63}$/.test(name.trim())) {
      setError('标识须以小写字母开头，仅含小写字母、数字、下划线或连字符，最多 64 位。'); return;
    }
    setSaving(true); setError(''); setNotice('');
    try {
      const saved = kind === 'models'
        ? await api.savePlatformModel(name.trim(), { ...model, api_key: model.api_key || undefined })
        : await api.saveMcpServer(name.trim(), { ...server, secret: server.secret || undefined });
      await queryClient.invalidateQueries({ queryKey: kind === 'models' ? ['platform-models'] : ['mcp-servers'] });
      choose(saved, true);
      setNotice(`已保存 ${saved.label} v${saved.version}。已有任务和固定版本保持原配置。`);
    } catch (cause) { setError(errorText(cause)); }
    finally { setSaving(false); }
  }
  async function inspect(item: McpServer) {
    setLoadingTools(true); setTools(null); setError('');
    try { setTools((await api.listMcpTools(item.name, item.version)).tools); }
    catch (cause) { setError(`工具清单读取失败：${errorText(cause)}`); }
    finally { setLoadingTools(false); }
  }
  const active = items?.find((item) => item.name === name);
  const setPrice = (key: 'cache_hit_per_m' | 'cache_miss_per_m' | 'output_per_m', value: string) =>
    setModel({ ...model, price: { ...model.price, [key]: value === '' ? '' : Number(value) } });

  return <div className={styles.catalog}>
    <section className={styles.catalogList} aria-label={kind === 'models' ? '平台模型列表' : 'MCP 服务列表'}>
      <div className={styles.columnHeading}><h2>{kind === 'models' ? '平台模型' : 'MCP 服务'}</h2><span>{items?.length ?? 0}</span></div>
      {(kind === 'models' ? models.isLoading : servers.isLoading) ? <p className={styles.emptyColumn}>正在加载…</p>
        : (kind === 'models' ? models.error : servers.error) ? <p className={controls.error} role="alert">加载失败：{errorText(kind === 'models' ? models.error : servers.error)}</p>
          : <ul className={styles.list}>{items?.map((item) => <li key={item.name}><button type="button" className={`${styles.listButton} ${item.name === name ? styles.selected : ''}`} onClick={() => choose(item)}><strong>{item.label}</strong><span>{item.name} · v{item.version}{item.enabled ? '' : ' · 已停用'}</span></button></li>)}</ul>}
      <button type="button" className={`${controls.button} ${styles.newProfile}`} onClick={create}>＋ 新增{kind === 'models' ? '模型' : 'MCP 服务'}</button>
    </section>
    <section className={styles.catalogEditor} aria-label={kind === 'models' ? '平台模型编辑' : 'MCP 服务编辑'}>
      {!original ? <p className={styles.emptyDetail}>选择左侧条目查看设置，或新增配置。</p> : <>
        <div className={styles.contentHeader}><div><h2>{active ? `编辑 ${active.label} · v${active.version}` : `新增${kind === 'models' ? '平台模型' : 'MCP 服务'}`}</h2><p>保存后创建不可变版本；密钥只可写入，不从服务端回显。</p></div></div>
        {error && <p className={styles.errorBanner} role="alert">{error}</p>}
        {notice && <p className={styles.noticeBanner} role="status">{notice}</p>}
        <form className={styles.formGrid} onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <label className={controls.field}><span className={controls.label}>标识</span><input className={controls.input} value={name} onChange={(event) => setName(event.target.value)} disabled={Boolean(active) || saving} required /></label>
          <label className={controls.field}><span className={controls.label}>显示名称</span><input className={controls.input} value={draft.label} onChange={(event) => kind === 'models' ? setModel({ ...model, label: event.target.value }) : setServer({ ...server, label: event.target.value })} required /></label>
          {kind === 'models' ? <>
            <label className={controls.field}><span className={controls.label}>Provider</span><select className={controls.select} value={model.provider} onChange={(event) => setModel({ ...model, provider: event.target.value as PlatformModelInput['provider'], credential_source: event.target.value === 'deepseek' ? model.credential_source : model.credential_source === 'environment' ? 'stored' : model.credential_source })}>{['deepseek', 'openai_chat', 'openai_responses', 'openai_compatible'].map((value) => <option key={value}>{value}</option>)}</select></label>
            <label className={controls.field}><span className={controls.label}>模型 ID</span><input className={controls.input} value={model.model} onChange={(event) => setModel({ ...model, model: event.target.value })} required /></label>
            <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>Base URL</span><input className={controls.input} type="url" value={model.base_url} onChange={(event) => setModel({ ...model, base_url: event.target.value })} required /><span className={controls.hint}>由服务容器访问；宿主机地址请使用 host.docker.internal，127.0.0.1 指容器自身。</span></label>
            <label className={controls.field}><span className={controls.label}>推理强度</span><select className={controls.select} value={model.reasoning_effort} onChange={(event) => setModel({ ...model, reasoning_effort: event.target.value })}>{['none', 'minimal', 'low', 'medium', 'high', 'xhigh'].map((value) => <option key={value}>{value}</option>)}</select></label>
            <label className={controls.field}><span className={controls.label}>凭据来源</span><select className={controls.select} value={model.credential_source} onChange={(event) => setModel({ ...model, credential_source: event.target.value as PlatformModelInput['credential_source'], api_key: '' })}><option value="stored">平台保存密钥</option><option value="environment" disabled={model.provider !== 'deepseek'}>现有 DeepSeek 环境密钥</option><option value="none">无认证</option></select></label>
            {model.credential_source === 'stored' && <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>API 密钥</span><input className={controls.input} type="password" autoComplete="new-password" value={model.api_key ?? ''} onChange={(event) => setModel({ ...model, api_key: event.target.value })} placeholder={active && 'has_secret' in active && active.has_secret ? '已保存；留空沿用当前密钥' : '输入密钥'} /><span className={controls.hint}>已有密钥不会显示在浏览器中。</span></label>}
            <div className={styles.wideField}><strong>人民币价格 · 每百万 token</strong><div className={styles.priceFields}>{([['cache_hit_per_m', '缓存命中输入'], ['cache_miss_per_m', '缓存未命中输入'], ['output_per_m', '输出']] as const).map(([key, label]) => <label className={controls.field} key={key}><span className={controls.label}>{label}</span><input className={controls.input} type="number" min="0" step="any" value={model.price[key] ?? ''} onChange={(event) => setPrice(key, event.target.value)} required /></label>)}</div></div>
            <label className={`${styles.checkRow} ${styles.wideField}`}><input type="checkbox" checked={Boolean(model.price.off_peak)} onChange={(event) => setModel({ ...model, price: { ...model.price, off_peak: event.target.checked } })} />此价格表为闲时价格（不自动切换）</label>
            <p className={`${controls.hint} ${styles.wideField}`}>费用始终按上方填写的单价计算。</p>
          </> : <>
            <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>Streamable HTTP URL</span><input className={controls.input} type="url" value={server.url} onChange={(event) => setServer({ ...server, url: event.target.value })} required /><span className={controls.hint}>连接从服务容器发出；访问宿主机服务请使用 host.docker.internal，127.0.0.1 指容器自身。</span></label>
            <label className={controls.field}><span className={controls.label}>认证 Header</span><input className={controls.input} value={server.auth_header} onChange={(event) => setServer({ ...server, auth_header: event.target.value })} /></label>
            <label className={controls.field}><span className={controls.label}>认证 Scheme</span><input className={controls.input} value={server.auth_scheme} onChange={(event) => setServer({ ...server, auth_scheme: event.target.value })} /></label>
            <label className={`${controls.field} ${styles.wideField}`}><span className={controls.label}>认证密钥</span><input className={controls.input} type="password" autoComplete="new-password" value={server.secret ?? ''} onChange={(event) => setServer({ ...server, secret: event.target.value })} placeholder={active && 'has_secret' in active && active.has_secret ? '已保存；留空沿用当前密钥' : '可留空'} /></label>
            {active && 'has_secret' in active && active.has_secret && <label className={`${styles.checkRow} ${styles.wideField}`}><input type="checkbox" checked={Boolean(server.clear_secret)} onChange={(event) => setServer({ ...server, clear_secret: event.target.checked })} />清除当前密钥</label>}
          </>}
          <label className={`${styles.checkRow} ${styles.wideField}`}><input type="checkbox" checked={draft.enabled} onChange={(event) => kind === 'models' ? setModel({ ...model, enabled: event.target.checked }) : setServer({ ...server, enabled: event.target.checked })} />启用</label>
          <div className={`${styles.editorActions} ${styles.wideField}`}><button className={`${controls.button} ${controls.primary}`} type="submit" disabled={saving}>{saving ? '正在保存…' : '保存新版本'}</button></div>
        </form>
        {kind === 'mcp' && active && 'url' in active && <div className={styles.toolList}><button type="button" className={controls.button} disabled={loadingTools} onClick={() => void inspect(active)}>{loadingTools ? '正在读取…' : `查看 v${active.version} 工具清单`}</button>{tools && (tools.length ? <ul>{tools.map((tool) => <li key={tool.name}><strong>{tool.name}</strong><p>{tool.description}</p></li>)}</ul> : <p>该服务没有公开工具。</p>)}</div>}
      </>}
    </section>
  </div>;
}
