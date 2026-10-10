import { useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { Alert, Button, Form, Input } from 'antd';
import { ApartmentOutlined } from '@ant-design/icons';
import { api, ApiError } from '../api/client';
import styles from './LoginPage.module.css';
export default function LoginPage() {
  const navigate = useNavigate(); const location = useLocation();
  const [pending, setPending] = useState(false); const [error, setError] = useState('');
  const [invalidCredentials, setInvalidCredentials] = useState(false);
  async function submit({ username, password }: { username: string; password: string }) {
    if (pending) return;
    setError(''); setInvalidCredentials(false); setPending(true);
    try { await api.login(username.trim(), password); const target = (location.state as { from?: string } | null)?.from; navigate(target?.startsWith('/') && !target.startsWith('//') ? target : '/tasks', { replace: true }); }
    catch (cause) { setInvalidCredentials(cause instanceof ApiError && [401,403].includes(cause.status)); setError(cause instanceof ApiError ? cause.message : '无法连接黑板服务，请稍后重试。'); }
    finally { setPending(false); }
  }
  return <main className={styles.page}><div className={styles.frame}>
    <Link to="/tasks" className={styles.brand} aria-label="黑板探索，前往任务列表"><ApartmentOutlined />黑板探索</Link>
    <section className={styles.formPanel} aria-labelledby="login-heading"><h1 id="login-heading">进入工作台</h1>
      <Form layout="vertical" onFinish={(values) => void submit(values)} disabled={pending} requiredMark={false}>
        <Form.Item name="username" label="账号" rules={[{ required: true, whitespace: true, message: '请输入账号' }]}><Input autoFocus aria-invalid={invalidCredentials} aria-describedby={error?"login-error":undefined} autoComplete="username" /></Form.Item>
        <Form.Item name="password" label="密码" rules={[{ required: true, message: '请输入密码' }]}><Input.Password aria-invalid={invalidCredentials} aria-describedby={error?"login-error":undefined} autoComplete="current-password" /></Form.Item>
        {error && <Alert id="login-error" type="error" showIcon title={error} className={styles.error} />}
        <Button type="primary" htmlType="submit" block loading={pending}>登录</Button>
      </Form>
    </section>
  </div></main>;
}
