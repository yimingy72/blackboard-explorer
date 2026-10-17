import { Drawer } from 'antd';
import { ApiOutlined } from '@ant-design/icons';
import { lazy, Suspense } from 'react';
import LoadingState from './LoadingState';
const PacketInspector = lazy(() => import('./PacketInspector'));
import styles from './TrafficDrawer.module.css';

type Props = {
  taskId: string;
  currentRun: number;
  runs: number[];
  agents: { id: string; name: string }[];
  open: boolean;
  initialAgentId?: string | null;
  onClose: () => void;
};

export default function TrafficDrawer(props: Props) {
  return <Drawer title={<span className={styles.title}><ApiOutlined aria-hidden="true" />任务流量</span>} open={props.open} onClose={props.onClose} size="min(1560px, 100vw)" resizable destroyOnHidden styles={{ body: { padding: 0, display: 'flex', flexDirection: 'column' } }}>
    <Suspense fallback={<LoadingState label="流量列表加载中" lines={8} />}><PacketInspector key={`${props.taskId}:${props.initialAgentId ?? 'all'}:${props.currentRun}`} {...props} /></Suspense>
  </Drawer>;
}
