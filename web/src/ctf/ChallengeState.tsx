import { CheckCircleOutlined, ClockCircleOutlined, ExclamationCircleOutlined, InboxOutlined, MinusCircleOutlined, SyncOutlined } from '@ant-design/icons';
import { Tag } from 'antd';
import { challengeStateLabels, type ChallengeState } from './challengeView';
import styles from './ChallengeState.module.css';

const icons = { pending: <ClockCircleOutlined />, working: <SyncOutlined />, blocked: <ExclamationCircleOutlined />, delivered: <InboxOutlined />, solved: <CheckCircleOutlined />, cancelled: <MinusCircleOutlined />, archived: <InboxOutlined /> };
export default function ChallengeStateTag({ state }: { state: ChallengeState }) {
  return <Tag className={`${styles.chip} ${styles[state]}`} variant="outlined" icon={icons[state]}>{challengeStateLabels[state]}</Tag>;
}
