import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, Outlet, useLocation, useNavigate, useNavigation } from 'react-router-dom';
import { App as AntApp, Alert, Avatar, Button, Drawer, Dropdown, Layout, Menu, Tooltip } from 'antd';
import { ApartmentOutlined, AppstoreOutlined, DownOutlined, LogoutOutlined, MenuFoldOutlined, MenuUnfoldOutlined, PlusOutlined, SettingOutlined, UserOutlined } from '@ant-design/icons';
import { api } from './api/client';
import { preloadPage } from './pages/routeModules';
import { tasksQueryOptions, taskQueryOptions } from './api/queries';
import { taskTitle } from './pages/format';
import styles from './styles/App.module.css';

function Shell() {
  const navigate = useNavigate(); const queryClient = useQueryClient();
  const { pathname } = useLocation();
  const pending = useNavigation().state === 'loading';
  const workbench = /^\/tasks\/[^/]+(?:\/progress)?$/.test(pathname) && pathname !== '/tasks/new';
  const routeKey = pathname.replace(/^(\/tasks\/[^/]+)\/progress$/, '$1');
  const [expanded, setExpanded] = useState(false);
  const [narrow, setNarrow] = useState(() => matchMedia('(max-width: 899px)').matches);
  const [logoutError, setLogoutError] = useState(''); const [loggingOut, setLoggingOut] = useState(false);
  const toggle = useRef<HTMLButtonElement | HTMLAnchorElement>(null);
  const navigationTrigger = useRef<HTMLElement | null>(null);
  const restoreNavigationFocus=useRef(false);
  const tasks = useQuery({ ...tasksQueryOptions(), enabled: expanded });
  useEffect(() => { document.getElementById('main-content')?.focus({ preventScroll: true }); }, [pathname]);
  useEffect(() => {
    const media = matchMedia('(max-width: 899px)');
    const update = () => { setNarrow(media.matches); if (media.matches) setExpanded(false); };
    media.addEventListener('change', update); return () => media.removeEventListener('change', update);
  }, []);
  async function logout() {
    if (loggingOut) return;
    setLogoutError(''); setLoggingOut(true);
    try { await api.logout(); queryClient.clear(); navigate('/login', { replace: true }); }
    catch { setLogoutError('退出失败，请重试。'); }
    finally { setLoggingOut(false); }
  }
  function preload(path: string) {
    preloadPage(path);
    const taskId = /^\/tasks\/([^/]+)(?:\/(?:report|progress))?$/.exec(path)?.[1];
    if (taskId && taskId !== 'new') void queryClient.prefetchQuery(taskQueryOptions(taskId));
  }
  const selected = pathname.startsWith('/profiles') ? 'profiles' : 'tasks';
  const items = [
    { key: 'tasks', icon: <AppstoreOutlined aria-hidden="true" />, label: <Link to="/tasks" aria-current={selected === 'tasks' ? 'page' : undefined}>任务</Link> },
    { key: 'profiles', icon: <SettingOutlined aria-hidden="true" />, label: <Link to="/profiles" aria-current={selected === 'profiles' ? 'page' : undefined}>Agent 配置</Link> },
  ];
  const navigation = (full: boolean, id: string) => <>
    <Tooltip title={full ? undefined : '新建任务'} placement="right"><Button className={styles.newTask} type="primary" icon={<PlusOutlined aria-hidden="true" />} aria-label="新建任务" onPointerEnter={() => preloadPage('/tasks/new')} onFocus={() => preloadPage('/tasks/new')} onClick={() => { if (narrow) {restoreNavigationFocus.current=false;setExpanded(false);} navigate('/tasks/new'); }}>{full ? '新建任务' : null}</Button></Tooltip>
    <nav id={id} aria-label="主导航"><Menu mode="inline" inlineCollapsed={!full} selectedKeys={[selected]} items={items} onClick={() => { if (narrow) {restoreNavigationFocus.current=false;setExpanded(false);} }} style={{ borderInlineEnd: 0 }} /></nav>
    {full && Boolean(tasks.data?.length) && <div className={styles.recent}><span>最近任务</span>{tasks.data?.slice(0, 3).map(task => <Link key={task.id} to={`/tasks/${task.id}`} title={taskTitle(task)} onClick={() => { if (narrow) {restoreNavigationFocus.current=false;setExpanded(false);} }}><i data-running={task.status === 'running'} /><span>{taskTitle(task)}</span></Link>)}</div>}
  </>;
  const closeNavigation = () => {restoreNavigationFocus.current=true;setExpanded(false);};
  return <Layout onPointerOver={event => { const link = (event.target as HTMLElement).closest<HTMLAnchorElement>('a[href]'); if (link?.origin === location.origin && !link.contains(event.relatedTarget as Node | null)) preload(link.pathname); }} onFocusCapture={event => { const link = (event.target as HTMLElement).closest<HTMLAnchorElement>('a[href]'); if (link?.origin === location.origin) preload(link.pathname); }} className={`${styles.app} ${workbench ? styles.workbench : ''}`}>
    <a href="#main-content" className={styles.skipLink}>跳到主要内容</a>
    <Layout.Sider className={styles.sidebar} width={208} collapsedWidth={60} collapsed={narrow || !expanded} trigger={null} theme="light" style={{transition:'none'}} styles={{ body: { display: 'flex', flexDirection: 'column', height: '100%' } }}>
      <Link to="/tasks" className={styles.brand} aria-label="黑板探索，前往任务列表"><ApartmentOutlined aria-hidden="true" />{!narrow && expanded && <span>黑板探索</span>}</Link>
      {navigation(!narrow && expanded, 'workspace-navigation')}
      <div className={styles.sidebarBottom}><Tooltip title={expanded ? '收起导航' : '展开导航'} placement="right"><Button ref={toggle} type="text" className={styles.toggle} icon={expanded ? <MenuFoldOutlined aria-hidden="true" /> : <MenuUnfoldOutlined aria-hidden="true" />} aria-label={expanded ? '收起导航' : '展开导航'} aria-expanded={expanded} aria-controls={narrow ? 'workspace-navigation-drawer' : 'workspace-navigation'} onClick={event => {navigationTrigger.current=event.currentTarget;restoreNavigationFocus.current=false;setExpanded(!expanded);}}>{expanded && !narrow ? '收起导航' : null}</Button></Tooltip></div>
    </Layout.Sider>
    <Layout className={styles.contentLayout}>
      <Layout.Header className={styles.topbar}><div className={styles.context}><Button className={styles.mobileToggle} type="text" icon={<MenuUnfoldOutlined aria-hidden="true" />} aria-label={expanded ? '收起导航' : '展开导航'} aria-expanded={expanded} aria-controls="workspace-navigation-drawer" onClick={event => {navigationTrigger.current=event.currentTarget;restoreNavigationFocus.current=false;setExpanded(!expanded);}} /><span>{pathname.startsWith('/profiles') ? '配置中心' : '工作空间'}</span></div><Dropdown trigger={['click']} menu={{ items: [{ key: 'logout', label: '退出登录', icon: <LogoutOutlined aria-hidden="true" />, disabled: loggingOut }], onClick: ({key}) => { if (key === 'logout') void logout(); } }}><Button type="text" className={styles.account} aria-label="账户菜单" loading={loggingOut}><Avatar size={26} icon={<UserOutlined aria-hidden="true" />} style={{ background: '#edf1f6', color: '#526580' }} /><DownOutlined aria-hidden="true" /></Button></Dropdown></Layout.Header>
      {logoutError && <Alert type="error" showIcon title={logoutError} />}
      <Layout.Content aria-busy={pending} data-navigating={pending || undefined} id="main-content" tabIndex={-1} className={styles.main}><div key={routeKey} className={styles.route}><Outlet /></div></Layout.Content>
    </Layout>
    <Drawer title="工作空间" placement="left" size={230} open={narrow && expanded} onClose={closeNavigation} afterOpenChange={open => {
      const trigger = navigationTrigger.current;
      const triggerHidden = Boolean(trigger && !trigger.getClientRects().length);
      if (!open && (restoreNavigationFocus.current || triggerHidden)) {
        (triggerHidden ? toggle.current : trigger)?.focus({ preventScroll: true });
        restoreNavigationFocus.current = false;
      }
    }} styles={{ body: { padding: 12 } }}>{navigation(true, 'workspace-navigation-drawer')}</Drawer>
  </Layout>;
}

export default function AppShell() { return <AntApp><Shell /></AntApp>; }
