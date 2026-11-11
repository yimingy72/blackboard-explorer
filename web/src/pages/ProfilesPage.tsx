import { ApartmentOutlined, FlagOutlined, TeamOutlined } from '@ant-design/icons';
import { lazy, Suspense, useState } from 'react';
import { Link, useLocation, useSearchParams } from 'react-router-dom';
import LoadingState from '../components/LoadingState';
import { SettingsGuard } from '../settings/SettingsGuard';
import { SettingsHeading } from '../settings/SettingsShell';
import { useToolCatalog } from '../settings/toolCatalog';
import { useSettingsLogin } from '../settings/settingsChanges';
import styles from '../settings/Settings.module.css';

const WorkerSettings = lazy(() => import('../settings/WorkerSettings'));
const CtfSettings = lazy(() => import('../ctf/CtfSettings'));
const ProgressSettings = lazy(() => import('../progress/ProgressSettings'));
const groups = [
  ['worker', 'Worker', <TeamOutlined key="worker" aria-hidden="true" />],
  ['ctf', 'CTF', <FlagOutlined key="ctf" aria-hidden="true" />],
  ['progress', '成果整理', <ApartmentOutlined key="progress" aria-hidden="true" />],
] as const;

export default function ProfilesPage() {
  const [params] = useSearchParams(), location = useLocation();
  const raw = params.get('section');
  const section = raw === 'ctf' || raw === 'progress' ? raw : 'worker';
  const [dirty, onDirtyChange] = useState(false), [busy, onBusyChange] = useState(false);
  const query = useToolCatalog();
  useSettingsLogin(query.error);
  const tracking = { onDirtyChange, onBusyChange };
  return <div className={styles.page}>
    <SettingsGuard dirty={dirty} busy={busy} />
    <SettingsHeading title="Agent 配置" description="先了解角色职责与权限边界，再按需编辑配置。" />
    <nav className={styles.modeNav} aria-label="Agent 类型">
      {groups.map(([id, name, icon]) => <Link key={id} to={id === 'worker' ? '/profiles' : '/profiles?section=' + id}
        aria-current={id === section ? 'page' : undefined}>{icon}{name}</Link>)}
    </nav>
    <Suspense fallback={<LoadingState label="正在读取角色配置" lines={6} />}>
      {section === 'ctf' ? <CtfSettings key={location.search} query={query} />
        : section === 'progress' ? <ProgressSettings key={location.search} query={query} tracking={tracking} />
        : <WorkerSettings key={location.search} query={query} tracking={tracking}
          initialTool={params.get('source') === 'mcp' ? 'mcp' : undefined} />}
    </Suspense>
  </div>;
}
