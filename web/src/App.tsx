import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, Navigate, Outlet, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { Alert, Avatar, Button, Drawer, Dropdown, Layout, Menu, Result, Spin, Tooltip } from 'antd';
import { ApartmentOutlined, AppstoreOutlined, DownOutlined, LogoutOutlined, MenuFoldOutlined, MenuUnfoldOutlined, PlusOutlined, SettingOutlined, UserOutlined } from '@ant-design/icons';
import { api } from './api/client';
import LoginPage from './pages/LoginPage';
import TasksPage from './pages/TasksPage';
import NewTaskPage from './pages/NewTaskPage';
import { taskTitle } from './pages/format';
import styles from './styles/App.module.css';
const TaskWorkbenchPage = lazy(() => import('./pages/TaskWorkbenchPage'));
const ProfilesPage = lazy(() => import('./pages/ProfilesPage'));
const ReportPage = lazy(() => import('./pages/ReportPage'));

function AppShell() {
  const navigate = useNavigate(); const queryClient = useQueryClient();
  const { pathname } = useLocation();
  const workbench = /^\/tasks\/[^/]+$/.test(pathname) && pathname !== '/tasks/new';
  const [expanded, setExpanded] = useState(false);
  const [narrow, setNarrow] = useState(() => matchMedia('(max-width: 899px)').matches);
  const [logoutError, setLogoutError] = useState(''); const [loggingOut, setLoggingOut] = useState(false);
  const toggle = useRef<HTMLButtonElement | HTMLAnchorElement>(null);
  const restoreNavigationFocus=useRef(false);
  const tasks = useQuery({ queryKey: ['tasks'], queryFn: api.listTasks });
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
  const selected = workbench ? 'canvas' : pathname.startsWith('/profiles') ? 'profiles' : pathname === '/tasks/new' ? 'new' : 'tasks';
  const items = [
    { key: 'tasks', icon: <AppstoreOutlined />, label: <Link to="/tasks" aria-current={selected === 'tasks' ? 'page' : undefined}>任务</Link> },
    ...(workbench ? [{ key: 'canvas', icon: <ApartmentOutlined />, label: <Link to={pathname} aria-current="page">协作画布</Link> }] : []),
    { key: 'profiles', icon: <SettingOutlined />, label: <Link to="/profiles" aria-current={selected === 'profiles' ? 'page' : undefined}>Agent 配置</Link> },
  ];
  const navigation = (full: boolean, id: string) => <>
    <Tooltip title={full ? undefined : '新建任务'} placement="right"><Button className={styles.newTask} type="primary" icon={<PlusOutlined />} aria-label="新建任务" onClick={() => { if (narrow) {restoreNavigationFocus.current=false;setExpanded(false);} navigate('/tasks/new'); }}>{full ? '新建任务' : null}</Button></Tooltip>
    <nav id={id} aria-label="主导航"><Menu mode="inline" inlineCollapsed={!full} selectedKeys={[selected]} items={items} onClick={() => { if (narrow) {restoreNavigationFocus.current=false;setExpanded(false);} }} style={{ borderInlineEnd: 0 }} /></nav>
    {full && Boolean(tasks.data?.length) && <div className={styles.recent}><span>最近任务</span>{tasks.data?.slice(0, 3).map(task => <Link key={task.id} to={`/tasks/${task.id}`} title={taskTitle(task)} onClick={() => { if (narrow) {restoreNavigationFocus.current=false;setExpanded(false);} }}><i data-running={task.status === 'running'} /><span>{taskTitle(task)}</span></Link>)}</div>}
  </>;
  const closeNavigation = () => {restoreNavigationFocus.current=true;setExpanded(false);};
  return <Layout className={`${styles.app} ${workbench ? styles.workbench : ''}`}>
    <a href="#main-content" className={styles.skipLink}>跳到主要内容</a>
    <Layout.Sider className={styles.sidebar} width={208} collapsedWidth={60} collapsed={narrow || !expanded} trigger={null} theme="light" styles={{ body: { display: 'flex', flexDirection: 'column', height: '100%' } }}>
      <Link to="/tasks" className={styles.brand} aria-label="黑板探索，前往任务列表"><ApartmentOutlined />{!narrow && expanded && <span>黑板探索</span>}</Link>
      {navigation(!narrow && expanded, 'workspace-navigation')}
      <div className={styles.sidebarBottom}><Tooltip title={expanded ? '收起导航' : '展开导航'} placement="right"><Button ref={toggle} type="text" className={styles.toggle} icon={expanded ? <MenuFoldOutlined /> : <MenuUnfoldOutlined />} aria-label={expanded ? '收起导航' : '展开导航'} aria-expanded={expanded} aria-controls={narrow ? 'workspace-navigation-drawer' : 'workspace-navigation'} onClick={() => {restoreNavigationFocus.current=false;setExpanded(!expanded);}}>{expanded && !narrow ? '收起导航' : null}</Button></Tooltip></div>
    </Layout.Sider>
    <Layout className={styles.contentLayout}>
      <Layout.Header className={styles.topbar}><span>{pathname.startsWith('/profiles') ? '配置中心' : '工作空间'}</span><Dropdown trigger={['click']} menu={{ items: [{ key: 'logout', label: '退出登录', icon: <LogoutOutlined />, disabled: loggingOut }], onClick: ({key}) => { if (key === 'logout') void logout(); } }}><Button type="text" className={styles.account} aria-label="账户菜单" loading={loggingOut}><Avatar size={26} icon={<UserOutlined />} style={{ background: '#edf1f6', color: '#526580' }} /><DownOutlined /></Button></Dropdown></Layout.Header>
      {logoutError && <Alert type="error" showIcon title={logoutError} />}
      <Layout.Content id="main-content" tabIndex={-1} className={styles.main}><Outlet /></Layout.Content>
    </Layout>
    <Drawer title="工作空间" placement="left" size={230} open={narrow && expanded} onClose={closeNavigation} afterOpenChange={open => { if (!open && narrow && restoreNavigationFocus.current) {toggle.current?.focus({preventScroll:true});restoreNavigationFocus.current=false;} }} styles={{ body: { padding: 12 } }}>{navigation(true, 'workspace-navigation-drawer')}</Drawer>
  </Layout>;
}
function Loading({ text }: { text: string }) { return <div className={styles.routeLoading} role="status"><Spin /><span>{text}</span></div>; }
export default function App() {
  return <Routes><Route path="/" element={<Navigate to="/tasks" replace />} /><Route path="/login" element={<LoginPage />} />
    <Route element={<AppShell />}><Route path="/tasks" element={<TasksPage />} /><Route path="/tasks/new" element={<NewTaskPage />} />
      <Route path="/tasks/:taskId" element={<Suspense fallback={<Loading text="正在打开工作台…" />}><TaskWorkbenchPage /></Suspense>} />
      <Route path="/tasks/:taskId/report" element={<Suspense fallback={<Loading text="正在读取报告…" />}><ReportPage /></Suspense>} />
      <Route path="/profiles" element={<Suspense fallback={<Loading text="正在读取配置…" />}><ProfilesPage /></Suspense>} />
      <Route path="*" element={<Result status="404" title="页面不存在" extra={<Button href="/tasks" type="primary">返回任务列表</Button>} />} />
    </Route></Routes>;
}
