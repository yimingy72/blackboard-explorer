import { CheckCircleOutlined, ClockCircleOutlined, CloseCircleOutlined, ExclamationCircleOutlined, FileTextOutlined, MinusCircleOutlined, QuestionCircleOutlined, SyncOutlined } from '@ant-design/icons';
import { Tag } from 'antd';
import styles from './BlackboardState.module.css';
type Tone = 'neutral' | 'active' | 'warning' | 'danger' | 'success';
function stateDescriptor(kind: 'fact' | 'intent' | 'agent' | 'goal', status: string, result?: string | null): { label: string; tone: Tone; icon: React.ReactNode } {
  if (kind === 'fact') return status === 'disputed' ? { label: '有争议', tone: 'danger', icon: <ExclamationCircleOutlined /> } : { label: '已提出', tone: 'neutral', icon: <FileTextOutlined /> };
  if (kind === 'intent') {
    if (status === 'open') return { label: '待认领', tone: 'neutral', icon: <ClockCircleOutlined /> };
    if (status === 'claimed') return { label: '调查中', tone: 'active', icon: <SyncOutlined /> };
    if (result === 'confirmed') return { label: '已确认', tone: 'success', icon: <CheckCircleOutlined /> };
    if (result === 'rejected') return { label: '已否定', tone: 'danger', icon: <CloseCircleOutlined /> };
    return { label: result === 'inconclusive' ? '未定论' : '已关闭', tone: 'neutral', icon: <QuestionCircleOutlined /> };
  }
  const labels: Record<string, string> = { running: '运行中', concluding: '交接中', closing: '收尾中', finished: '已结束', failed: '失败', stopped: '已停止', created: '待启动', provisioning: '准备中' };
  const tone: Tone = status === 'failed' ? 'danger' : ['running', 'provisioning'].includes(status) ? 'active' : ['concluding', 'closing'].includes(status) ? 'warning' : 'neutral';
  return { label: labels[status] ?? status, tone, icon: tone === 'danger' ? <ExclamationCircleOutlined /> : tone === 'active' ? <SyncOutlined /> : tone === 'warning' ? <ClockCircleOutlined /> : <MinusCircleOutlined /> };
}
export default function BlackboardState({ kind, status, result }: { kind: 'fact' | 'intent' | 'agent' | 'goal'; status: string; result?: string | null }) {
  const state = stateDescriptor(kind, status, result);
  return <Tag variant="outlined" icon={state.icon} className={`${styles.chip} ${styles[state.tone]}`}>{state.label}</Tag>;
}
