import { useState, type FormEvent } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import shell from '../styles/App.module.css';
import controls from '../styles/controls.module.css';
import styles from './LoginPage.module.css';

export default function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError('');
    setPending(true);
    try {
      await api.login(username.trim(), password);
      const target = (location.state as { from?: string } | null)?.from;
      navigate(target?.startsWith('/') && !target.startsWith('//') ? target : '/tasks', { replace: true });
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '无法连接黑板服务，请稍后重试。');
    } finally {
      setPending(false);
    }
  }

  return (
    <main className={styles.page}>
      <div className={styles.frame}>
        <Link to="/tasks" className={styles.brand} aria-label="黑板探索，前往任务列表">
          <span className={shell.brandMark} aria-hidden="true"><span /><span /><span /></span>黑板探索
        </Link>
        <section className={styles.formPanel} aria-labelledby="login-heading">
          <h1 id="login-heading">进入工作台</h1>
          <p className={styles.intro}>查看探索任务的事实、意图和争议关系。</p>
          <form onSubmit={submit} className={styles.form}>
            <div className={controls.field}>
              <label className={controls.label} htmlFor="username">账号</label>
              <input id="username" name="username" className={controls.input} value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required autoFocus disabled={pending} />
            </div>
            <div className={controls.field}>
              <label className={controls.label} htmlFor="password">密码</label>
              <input id="password" name="password" type="password" className={controls.input} value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required disabled={pending} />
            </div>
            {error && <p className={`${controls.error} ${styles.error}`} role="alert">{error}</p>}
            <button type="submit" className={`${controls.button} ${controls.primary} ${styles.submit}`} disabled={pending || !username.trim() || !password}>{pending ? '正在登录…' : '登录'}</button>
          </form>
        </section>
        <p className={styles.footnote}>访问由当前黑板服务的管理员账号控制。</p>
      </div>
    </main>
  );
}
