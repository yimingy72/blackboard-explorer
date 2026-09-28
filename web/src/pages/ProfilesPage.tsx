import WorkerPrompts from '../components/WorkerPrompts';
import PlatformCatalog from '../components/PlatformCatalog';
import { useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, ApiError, type ProfileInput } from '../api/client';
import { readDefaultProfile, saveDefaultProfile } from '../profiles/preferences';
import { parseProfileYaml, stringifyProfileYaml } from '../profiles/yaml';
import controls from '../styles/controls.module.css';
import { formatDate } from './format';
import styles from './ProfilesPage.module.css';

type View = 'view' | 'edit' | 'new';

function message(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败，请稍后重试。';
}

export default function ProfilesPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [selectedName, setSelectedName] = useState('');
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [section, setSection] = useState<'worker' | 'models' | 'mcp'>('worker');
  const [catalogDirty, setCatalogDirty] = useState(false);
  const [view, setView] = useState<View>('view');
  const [newName, setNewName] = useState('');
  const [draft, setDraft] = useState('');
  const [originalDraft, setOriginalDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [browserDefault, setBrowserDefault] = useState(readDefaultProfile);

  const profiles = useQuery({ queryKey: ['profiles'], queryFn: api.listProfiles });
  const versions = useQuery({
    queryKey: ['profile-versions', selectedName],
    queryFn: () => api.listProfileVersions(selectedName),
    enabled: Boolean(selectedName),
  });
  const document = useQuery({
    queryKey: ['profile', selectedName, selectedVersion],
    queryFn: () => api.getProfile(selectedName, selectedVersion!),
    enabled: Boolean(selectedName && selectedVersion),
  });
  const latest = profiles.data?.find((item) => item.name === selectedName)?.latest_version;
  const yaml = useMemo(
    () => document.data ? stringifyProfileYaml(document.data.profile as ProfileInput) : '',
    [document.data],
  );
  const dirty = (view === 'edit' || view === 'new') && draft !== originalDraft;
  const authError = [profiles.error, versions.error, document.error]
    .some((cause) => cause instanceof ApiError && cause.status === 401);

  useEffect(() => {
    if (!profiles.data?.length) return;
    if (!selectedName || !profiles.data.some((item) => item.name === selectedName)) {
      const preferred = profiles.data.find((item) => item.name === browserDefault?.name);
      setSelectedName(preferred?.name ?? profiles.data.find((item) => item.name === 'default')?.name ?? profiles.data[0].name);
      setSelectedVersion(preferred ? browserDefault!.version : null);
    }
  }, [profiles.data, selectedName, browserDefault]);

  useEffect(() => {
    if (!versions.data?.length) return;
    if (!selectedVersion || !versions.data.some((item) => item.version === selectedVersion)) {
      setSelectedVersion(versions.data[0].version);
    }
  }, [versions.data, selectedVersion]);

  useEffect(() => {
    if (authError) navigate('/login', { replace: true, state: { from: location.pathname } });
  }, [authError, location.pathname, navigate]);

  function canLeaveDraft(): boolean {
    return !dirty || window.confirm('当前配置尚未保存，确定放弃本次修改吗？');
  }

  function chooseName(name: string) {
    if (!canLeaveDraft()) return;
    setSelectedName(name);
    setSelectedVersion(null);
    setView('view');
    setError('');
    setNotice('');
  }

  function chooseVersion(version: number) {
    if (!canLeaveDraft()) return;
    setSelectedVersion(version);
    setView('view');
    setError('');
    setNotice('');
  }

  function edit(mode: 'edit' | 'new') {
    if (!canLeaveDraft() || !document.data) return;
    setOriginalDraft(yaml);
    setDraft(yaml);
    setNewName('');
    setError('');
    setNotice('');
    setView(mode);
  }

  async function publish() {
    const name = view === 'new' ? newName.trim() : selectedName;
    if (!name || name.includes('/')) {
      setError('请填写不含斜线的配置名称。');
      return;
    }
    if (view === 'new' && profiles.data?.some((item) => item.name === name)) {
      setError('这个配置名称已存在。请选择新名称，或在已有配置中发布新版本。');
      return;
    }
    let profile: ProfileInput;
    try {
      profile = parseProfileYaml(draft);
    } catch (cause) {
      setError(message(cause));
      return;
    }
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const result = await api.createProfileVersion(name, profile);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['profiles'] }),
        queryClient.invalidateQueries({ queryKey: ['profile-versions', name] }),
      ]);
      setSelectedName(name);
      setSelectedVersion(result.version);
      setView('view');
      setNotice(view === 'edit' && result.version === selectedVersion
        ? `内容未变化，仍为 ${name} v${result.version}。`
        : `已发布 ${name} v${result.version}。任务创建时可选择这个固定版本。`);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setSaving(false);
    }
  }

  async function rollback() {
    if (!document.data || !selectedVersion) return;
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const result = await api.createProfileVersion(selectedName, document.data.profile as ProfileInput);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['profiles'] }),
        queryClient.invalidateQueries({ queryKey: ['profile-versions', selectedName] }),
      ]);
      setSelectedVersion(result.version);
      setView('view');
      setNotice(result.version === latest
        ? '所选内容与当前最新版本一致，无需新增版本。'
        : `已把 v${selectedVersion} 的完整内容发布为 v${result.version}，原版本保留。`);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setSaving(false);
    }
  }

  function saveBrowserDefault() {
    if (!selectedName || !selectedVersion) return;
    try {
      saveDefaultProfile(selectedName, selectedVersion);
      setBrowserDefault({ name: selectedName, version: selectedVersion });
      setError('');
      setNotice(`已设 ${selectedName} v${selectedVersion} 为本浏览器的新建任务默认。`);
    } catch (cause) {
      setError(message(cause));
    }
  }

  const loading = profiles.isLoading || Boolean(selectedName && versions.isLoading)
    || Boolean(selectedVersion && document.isLoading);
  const queryError = profiles.error || versions.error || document.error;

  return (
    <div className={styles.page}>
      <header className={styles.pageHeader}>
        <div>
          <h1>Agent 配置</h1>
          <p>配置 Worker、复用平台模型并管理外部 MCP。已有任务固定使用创建时的版本。</p>
        </div>
      </header>
      <nav className={styles.topNav} aria-label="配置中心">
        {([['worker', 'Worker 配置'], ['models', '平台模型'], ['mcp', 'MCP 工具']] as const).map(([id, label]) => <button key={id} type="button" className={section === id ? styles.tabActive : styles.tab} aria-current={section === id ? 'page' : undefined} onClick={() => { if (id === section) return; if (canLeaveDraft() && (!catalogDirty || window.confirm('当前平台配置尚未保存，确定放弃本次修改吗？'))) { setCatalogDirty(false); setSection(id); } }}>{label}</button>)}
      </nav>
      {error && <p className={styles.errorBanner} role="alert">{error}</p>}
      {notice && <p className={styles.noticeBanner} role="status">{notice}</p>}
      {queryError && !authError && <div className={styles.errorBanner} role="alert">配置加载失败：{message(queryError)} <button type="button" onClick={() => {
        if (profiles.isError) void profiles.refetch();
        if (versions.isError) void versions.refetch();
        if (document.isError) void document.refetch();
      }}>重试</button></div>}

      {section === 'models' ? <PlatformCatalog key="models" kind="models" onDirtyChange={setCatalogDirty} /> : section === 'mcp' ? <PlatformCatalog key="mcp" kind="mcp" onDirtyChange={setCatalogDirty} /> : <div className={styles.layout}>
        <section className={styles.names} aria-labelledby="profiles-heading">
          <div className={styles.columnHeading}><h2 id="profiles-heading">配置</h2><span>{profiles.data?.length ?? 0}</span></div>
          {profiles.isLoading ? <div className={styles.skeletonList} role="status" aria-label="正在加载配置"><span className={controls.skeleton} /><span className={controls.skeleton} /></div> : profiles.data?.length ? (
            <ul className={styles.list}>{profiles.data.map((item) => <li key={item.name}>
              <button type="button" className={`${styles.listButton} ${item.name === selectedName ? styles.selected : ''}`} onClick={() => chooseName(item.name)} aria-current={item.name === selectedName ? 'true' : undefined}>
                <strong>{item.name}</strong><span>最新 v{item.latest_version}</span>
                {browserDefault?.name === item.name && <small>本浏览器默认 v{browserDefault.version}</small>}
              </button>
            </li>)}</ul>
          ) : <p className={styles.emptyColumn}>暂无配置。检查黑板服务的默认配置加载状态。</p>}
          <button type="button" className={`${controls.button} ${styles.newProfile}`} onClick={() => edit('new')} disabled={!document.data || saving}>＋ 新建配置名称</button>
        </section>

        <section className={styles.versions} aria-labelledby="versions-heading">
          <div className={styles.columnHeading}><h2 id="versions-heading">版本</h2><span>{versions.data?.length ?? 0}</span></div>
          {versions.isLoading ? <div className={styles.skeletonList} role="status" aria-label="正在加载版本"><span className={controls.skeleton} /><span className={controls.skeleton} /></div> : versions.data?.length ? (
            <ul className={styles.list}>{versions.data.map((item) => <li key={item.version}>
              <button type="button" className={`${styles.listButton} ${item.version === selectedVersion ? styles.selected : ''}`} onClick={() => chooseVersion(item.version)} aria-current={item.version === selectedVersion ? 'true' : undefined}>
                <strong>v{item.version}{item.version === latest ? ' · 最新' : ''}</strong>
                <span>{formatDate(item.created_at)}</span>
                {item.created_by && <small>{item.created_by}</small>}
              </button>
            </li>)}</ul>
          ) : <p className={styles.emptyColumn}>选择配置后查看版本。</p>}
        </section>

        <section className={styles.content} aria-labelledby="profile-detail-heading">
          <div className={styles.contentHeader}>
            <div>
              <h2 id="profile-detail-heading">{view === 'new' ? '新建配置' : selectedName && selectedVersion ? `${selectedName} · v${selectedVersion}` : '配置内容'}</h2>
              <p>{view === 'new' ? '从当前版本复制完整内容，并使用新的配置名称发布。' : '提示词正文、模型价格、参数与执行环境都保存在这个版本中。'}</p>
            </div>
            {view === 'view' && document.data && <span className={`${controls.badge} ${selectedVersion === latest ? controls.badgeInfo : ''}`}>{selectedVersion === latest ? '最新版本' : '历史版本'}</span>}
          </div>

          {loading ? <div className={styles.contentLoading} role="status" aria-label="正在加载配置正文"><span className={controls.skeleton} /><span className={controls.skeleton} /><span className={controls.skeleton} /></div> : !document.data ? (
            <div className={styles.emptyDetail}>选择一个配置版本查看完整内容。</div>
          ) : (
            <>
              <div className={styles.toolbar}>
                <div className={styles.tabs} role="group" aria-label="内容视图">
                  <button type="button" className={view === 'view' ? styles.tabActive : styles.tab} onClick={() => { if (canLeaveDraft()) setView('view'); }}>查看</button>
                </div>
                {view === 'view' && <div className={styles.actions}>
                  <button type="button" className={controls.button} onClick={saveBrowserDefault} disabled={saving || (browserDefault?.name === selectedName && browserDefault.version === selectedVersion)}>{browserDefault?.name === selectedName && browserDefault.version === selectedVersion ? '本浏览器默认' : '设为本浏览器默认'}</button>
                  {selectedVersion !== latest && <button type="button" className={controls.button} onClick={() => void rollback()} disabled={saving}>发布此旧版为新版本</button>}
                  <button type="button" className={`${controls.button} ${controls.primary}`} onClick={() => edit('edit')} disabled={saving}>编辑并发布新版本</button>
                </div>}
              </div>

              {view === 'new' || view === 'edit' ? (
                <div className={styles.editor}>
                  {view === 'new' && <div className={controls.field}><label className={controls.label} htmlFor="new-profile-name">新配置名称</label><input id="new-profile-name" className={controls.input} value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="例如：review-specialist" autoFocus disabled={saving} /></div>}
                  <WorkerPrompts source={draft} onChange={(value) => { setDraft(value); setNotice(''); }} disabled={saving} />
                  <p id="yaml-help" className={controls.hint}>保存会创建新版本；已有任务固定原配置，新任务可选择更新后的版本。</p>
                  <div className={styles.editorActions}>
                    <button type="button" className={controls.button} onClick={() => { try { parseProfileYaml(draft); setError(''); setNotice('YAML 格式和基本结构检查通过，仍需服务端校验。'); } catch (cause) { setError(message(cause)); } }} disabled={saving}>检查格式</button>
                    <button type="button" className={controls.button} onClick={() => { if (canLeaveDraft()) setView('view'); }} disabled={saving}>取消</button>
                    <button type="button" className={`${controls.button} ${controls.primary}`} onClick={() => void publish()} disabled={saving || !draft.trim()}>{saving ? '正在发布…' : '发布新版本'}</button>
                  </div>
                </div>
              ) : <WorkerPrompts source={yaml} />}
            </>
          )}
        </section>
      </div>}
    </div>
  );
}
