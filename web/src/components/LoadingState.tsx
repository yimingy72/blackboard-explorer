import { Skeleton } from 'antd';
import styles from './LoadingState.module.css';

export default function LoadingState({ label = '内容加载中', lines = 3, page = false, className = '' }: {
  label?: string;
  lines?: number;
  page?: boolean;
  className?: string;
}) {
  return <div className={`${styles.root} ${page ? styles.page : ''} ${className}`} role="status" aria-label={label} aria-busy="true">
    <Skeleton active={false} title={page ? { width: 160 } : false} paragraph={{ rows: lines, width: lines > 1 ? ['100%', ...Array.from({ length: lines - 2 }, () => '92%'), '64%'] : '78%' }} />
  </div>;
}
