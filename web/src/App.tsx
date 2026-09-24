import { lazy, Suspense, useState } from 'react';
import { Link, NavLink, Navigate, Outlet, Route, Routes, useNavigate } from 'react-router-dom';
import { api } from './api/client';
import LoginPage from './pages/LoginPage';
import TasksPage from './pages/TasksPage';
import NewTaskPage from './pages/NewTaskPage';
import controls from './styles/controls.module.css';
import styles from './styles/App.module.css';

const TaskWorkbenchPage = lazy(() => import('./pages/TaskWorkbenchPage'));
const ProfilesPage = lazy(() => import('./pages/ProfilesPage'));
const ReportPage = lazy(() => import('./pages/ReportPage'));

function AppShell() {
  const navigate = useNavigate();
  const [logoutError, setLogoutError] = useState('');

  async function logout() {
    setLogoutError('');
    try {
      await api.logout();
      navigate('/login', { replace: true });
    } catch {
      setLogoutError('退出失败，请重试。');
    }
  }

  return (
    <div className={styles.app}>
      <header className={styles.topbar}>
        <div className={styles.topbarInner}>
          <Link to="/tasks" className={styles.brand} aria-label="黑板探索，前往任务列表">
            <span className={styles.brandMark} aria-hidden="true"><span /><span /><span /></span>
            <span>黑板探索</span>
          </Link>
          <nav className={styles.nav} aria-label="主导航">
            <NavLink to="/tasks" end className={({ isActive }) => `${styles.navLink} ${isActive ? styles.navActive : ''}`}>任务</NavLink>
            <NavLink to="/profiles" className={({ isActive }) => `${styles.navLink} ${isActive ? styles.navActive : ''}`}>Agent 配置</NavLink>
          </nav>
          <div className={styles.actions}>
            <Link to="/tasks/new" className={`${controls.button} ${controls.primary}`}>新建任务</Link>
            <button type="button" className={`${controls.button} ${controls.quiet} ${styles.logout}`} onClick={logout}>退出</button>
          </div>
        </div>
        {logoutError && <p className={styles.topbarError} role="alert">{logoutError}</p>}
      </header>
      <main className={styles.main}><Outlet /></main>
    </div>
  );
}

function NotFoundPage() {
  return (
    <div className={styles.notFound}>
      <h1>页面不存在</h1>
      <p>这个地址没有对应的工作台页面。</p>
      <Link to="/tasks" className={`${controls.button} ${controls.primary}`}>返回任务列表</Link>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/tasks" replace />} />
      <Route path="/login" element={<LoginPage />} />
      <Route element={<AppShell />}>
        <Route path="/tasks" element={<TasksPage />} />
        <Route path="/tasks/new" element={<NewTaskPage />} />
        <Route path="/tasks/:taskId" element={<Suspense fallback={<div className={styles.routeLoading} role="status">正在打开工作台…</div>}><TaskWorkbenchPage /></Suspense>} />
        <Route path="/tasks/:taskId/report" element={<Suspense fallback={<div className={styles.routeLoading} role="status">正在读取报告…</div>}><ReportPage /></Suspense>} />
        <Route path="/profiles" element={<Suspense fallback={<div className={styles.routeLoading} role="status">正在读取配置…</div>}><ProfilesPage /></Suspense>} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
