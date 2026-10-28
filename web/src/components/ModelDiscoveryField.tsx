import { Alert, AutoComplete, Button, Collapse, InputNumber, Select, Tag } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState } from 'react';
import { api, type DiscoveredModel, type ModelCatalog, type PlatformModel, type PlatformModelInput } from '../api/client';
import controls from '../styles/controls.module.css';
import styles from '../pages/ProfilesPage.module.css';

const priceKeys = ['cache_hit_per_m', 'cache_miss_per_m', 'output_per_m'] as const;

export default function ModelDiscoveryField({ model, saved, onChange, disabled }: {
  model: PlatformModelInput; saved?: PlatformModel; onChange: (model: PlatformModelInput) => void; disabled: boolean;
}) {
  const [models, setModels] = useState<DiscoveredModel[]>([]), [loading, setLoading] = useState(false);
  const [error, setError] = useState(''), [warnings, setWarnings] = useState<string[]>([]);
  const [rate, setRate] = useState<number | null>(null), [appliedPrice, setAppliedPrice] = useState(false);
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null), [catalogLoading, setCatalogLoading] = useState(false), [catalogError, setCatalogError] = useState('');
  const [catalogProvider, setCatalogProvider] = useState(''), [catalogModel, setCatalogModel] = useState('');
  const [reference, setReference] = useState<DiscoveredModel | null>(null);
  const catalogSequence = useRef(0), catalogPending = useRef<AbortController | null>(null);
  const sequence = useRef(0), pending = useRef<AbortController | null>(null);
  // Keep credentials out of query keys, browser storage and diagnostic output.
  const connection = JSON.stringify([saved?.name, saved?.version, model.provider, model.base_url, model.provider_options, model.credentials]);
  useEffect(() => {
    sequence.current += 1; pending.current?.abort(); setModels([]); setLoading(false); setError(''); setWarnings([]); setAppliedPrice(false); setReference(null);
    return () => { pending.current?.abort(); };
  }, [connection]);
  useEffect(() => () => { catalogPending.current?.abort(); }, []);
  const selected = reference ?? models.find(item => item.id === model.model), price = selected?.price;
  async function discover() {
    if (disabled || !model.provider) return;
    pending.current?.abort(); const controller = new AbortController(); pending.current = controller;
    const current = ++sequence.current; setLoading(true); setError(''); setWarnings([]);
    try {
      const result = await api.discoverModels({ provider: model.provider, base_url: model.base_url, provider_options: model.provider_options, credentials: model.credentials, saved_name: saved?.name, saved_version: saved?.version }, controller.signal);
      if (current !== sequence.current || controller.signal.aborted) return;
      setModels(result.models); setWarnings(result.warnings.length ? result.warnings : result.models.length ? [] : ['接口没有返回模型，可手动填写模型 ID。']);
    } catch (cause) { if (current === sequence.current && !controller.signal.aborted) setError(`${cause instanceof Error ? cause.message : '模型列表获取失败'}；可继续手动填写。`); }
    finally { if (current === sequence.current && !controller.signal.aborted) setLoading(false); }
  }
  function select(id: string) {
    const metadata = models.find(item => item.id === id);
    setAppliedPrice(false); setReference(null);
    onChange({ ...model, model: id, label: model.label || metadata?.name || id,
      ...(metadata?.context_window != null ? { context_window: metadata.context_window } : {}),
      ...(metadata?.max_input_tokens != null ? { max_input_tokens: metadata.max_input_tokens } : {}),
      ...(metadata?.max_output_tokens != null ? { max_output_tokens: metadata.max_output_tokens } : {}),
      ...(metadata?.supports_vision != null ? { supports_vision: metadata.supports_vision } : {}),
    });
  }
  async function loadCatalog(provider?: string) {
    catalogPending.current?.abort(); const controller = new AbortController(); catalogPending.current = controller;
    const current = ++catalogSequence.current; setCatalogLoading(true); setCatalogError(''); setCatalogModel('');
    setCatalog(value => value ? { ...value, models: [] } : null);
    try {
      const value = await api.modelCatalog(provider, controller.signal);
      if (current === catalogSequence.current && !controller.signal.aborted) setCatalog(value);
    } catch (cause) { if (current === catalogSequence.current && !controller.signal.aborted) setCatalogError(cause instanceof Error ? cause.message : '目录读取失败，可手动填写。'); }
    finally { if (current === catalogSequence.current && !controller.signal.aborted) setCatalogLoading(false); }
  }
  function applyReference() {
    const value = catalog?.models.find(item => item.id === catalogModel);
    if (!value) return;
    onChange({ ...model,
      ...(value.context_window != null ? { context_window: value.context_window } : {}),
      ...(value.max_input_tokens != null ? { max_input_tokens: value.max_input_tokens } : {}),
      ...(value.max_output_tokens != null ? { max_output_tokens: value.max_output_tokens } : {}),
      ...(value.supports_vision != null ? { supports_vision: value.supports_vision } : {}),
    });
    setReference(value); setAppliedPrice(false);
  }
  function applyPrice() {
    if (!price?.currency || !['CNY', 'USD'].includes(price.currency)) return;
    const exchange = price.currency === 'CNY' ? 1 : rate;
    if (exchange == null || exchange <= 0) return;
    const converted = Object.fromEntries(priceKeys.filter(key => price[key] != null && Number.isFinite(Number(price[key]))).map(key => [key, String(Number((Number(price[key]) * exchange).toPrecision(12)))]));
    onChange({ ...model, price: { ...model.price, ...converted, currency: 'CNY', billing_mode: 'fixed', off_peak: false } }); setAppliedPrice(true);
  }
  return <div className={`${styles.wideField} ${styles.modelDiscovery}`}>
    <label htmlFor="catalog-model-id" className={controls.label}>模型 ID</label>
    <div className={styles.modelIdRow}>
      <AutoComplete id="catalog-model-id" aria-label="模型 ID" value={model.model} disabled={disabled} placeholder="获取模型后选择，或手动输入 ID" options={models.map(item => ({ value: item.id, label: item.name && item.name !== item.id ? `${item.id} · ${item.name}` : item.id }))} showSearch={{ filterOption: (input, option) => String(option?.label ?? '').toLowerCase().includes(input.toLowerCase()) }} onChange={value => { onChange({ ...model, model: value }); setAppliedPrice(false); setReference(null); }} onSelect={select} />
      <Button icon={<ReloadOutlined />} loading={loading} disabled={disabled || !model.provider} onClick={() => void discover()}>获取模型</Button>
    </div>
    {!!models.length && <div className={styles.discoveryMeta}><Tag>{models.length} 个可选模型</Tag>{selected && <span>{selected.metadata_source === 'provider_api' ? '接口返回' : '公共目录参考'}</span>}<span>可手动修改能力参数</span></div>}
    {error && <Alert type="warning" showIcon title={error} />}
    {warnings.length > 0 && <p className={controls.hint} role="status">{warnings.join(' ')}</p>}
    <Collapse ghost size="small" onChange={keys => { if (keys.includes('catalog') && !catalog && !catalogLoading) void loadCatalog(); }} items={[{ key: 'catalog', label: '从公共模型目录补充参数', children: <div className={styles.catalogReference}>
      <p className={controls.hint}>选择实际使用的参考模型。目录能力与美元价格仅供参考，网关可能有自己的限制和收费。</p>
      <div className={styles.referenceSelectors}>
        <Select aria-label="参考模型厂商" showSearch={{ optionFilterProp: 'label' }} placeholder="选择厂商" value={catalogProvider || undefined} loading={catalogLoading} disabled={disabled || catalogLoading} options={catalog?.providers.map(item => ({ value: item.id, label: item.name }))} onChange={value => { setCatalogProvider(value); void loadCatalog(value); }} />
        <Select aria-label="参考模型" showSearch={{ optionFilterProp: 'label' }} placeholder="选择参考模型" value={catalogModel || undefined} loading={catalogLoading} disabled={disabled || !catalogProvider || catalogLoading} options={catalog?.models.map(item => ({ value: item.id, label: item.name && item.name !== item.id ? `${item.id} · ${item.name}` : item.id }))} onChange={setCatalogModel} />
        <Button disabled={disabled || !catalogModel || catalogLoading} onClick={applyReference}>应用能力参数</Button>
      </div>
      {catalogError && <Alert type="warning" title={catalogError} action={<Button size="small" onClick={() => void loadCatalog(catalogProvider || undefined)}>重试</Button>} />}
      {reference && <span className={controls.hint}>已参考 models.dev · {reference.name ?? reference.id}；连接模型 ID 保持 {model.model || '手动填写'}。</span>}
    </div> }]} />
    {price?.currency && <div className={styles.referencePrice}>
      <span>参考基础价 · {price.currency} / 百万 token</span>
      <span>输入 {price.cache_miss_per_m ?? '未知'} · 输出 {price.output_per_m ?? '未知'} · 缓存命中 {price.cache_hit_per_m ?? '未知'}</span>
      <span>分档价格请核对服务商规则</span>
      {price.currency === 'USD' && <InputNumber aria-label="参考价格人民币汇率" placeholder="CNY / USD" min={0.000001} value={rate} onChange={setRate} />}
      {['CNY', 'USD'].includes(price.currency) && <Button size="small" disabled={disabled || (price.currency === 'USD' && !(rate != null && rate > 0))} onClick={applyPrice}>{appliedPrice ? '已填入人民币单价' : '填入人民币单价'}</Button>}
    </div>}
  </div>;
}
