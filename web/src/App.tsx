import { Navigate, Route, RouterProvider, createBrowserRouter, createRoutesFromElements } from 'react-router-dom';
import { Button, Result } from 'antd';
import LoginPage from './pages/LoginPage';
import { routeModules } from './pages/routeModules';
import LoadingState from './components/LoadingState';

const router = createBrowserRouter(createRoutesFromElements(<><Route path="/" element={<Navigate to="/tasks" replace />} /><Route path="/login" element={<LoginPage />} />
    <Route lazy={async () => ({ Component: (await import('./AppShell')).default })}><Route path="/tasks" lazy={routeModules.tasks} /><Route path="/tasks/new" lazy={routeModules.create} />
      <Route path="/tasks/:taskId" lazy={routeModules.workbench} />
      <Route path="/tasks/:taskId/report" lazy={routeModules.report} />
      <Route path="/profiles" lazy={routeModules.profiles} />
      <Route path="*" element={<Result status="404" title="页面不存在" extra={<Button href="/tasks" type="primary">返回任务列表</Button>} />} />
    </Route></>));
export default function App() { return <RouterProvider fallbackElement={<LoadingState page lines={6} label="页面内容加载中" />} router={router} future={{ v7_startTransition: true }} />; }
