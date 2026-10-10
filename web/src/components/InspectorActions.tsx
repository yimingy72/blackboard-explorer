import { useRef, useState } from 'react';
import { Button, Dropdown, Tooltip, type MenuProps } from 'antd';
import { CloseOutlined, EllipsisOutlined, FullscreenExitOutlined, FullscreenOutlined } from '@ant-design/icons';
import { useInspector } from './inspectorContext';
import styles from './InspectorLayout.module.css';

export default function InspectorActions({ onClose, closeLabel = '关闭详情', menu }: {
  onClose: () => void;
  closeLabel?: string;
  menu?: MenuProps;
}) {
  const { wide, narrow, toggleWide } = useInspector();
  const [menuOpen, setMenuOpen] = useState(false);
  const moreButton = useRef<HTMLButtonElement | HTMLAnchorElement>(null);
  const widthLabel = wide ? '还原面板宽度' : '放大面板';
  return <div className={styles.actions} onKeyDown={event => {
    if (event.key === 'Escape' && menuOpen) {
      event.preventDefault();
      event.stopPropagation();
      setMenuOpen(false);
      moreButton.current?.focus({ preventScroll: true });
    }
  }}>
    {!narrow && <Tooltip title={widthLabel}><Button type="text" size="small" icon={wide ? <FullscreenExitOutlined /> : <FullscreenOutlined />} aria-label={widthLabel} onClick={toggleWide} /></Tooltip>}
    {menu && <Dropdown autoFocus trigger={['click']} open={menuOpen} onOpenChange={setMenuOpen} menu={{ ...menu, onClick: info => { setMenuOpen(false); menu.onClick?.(info); } }}>
      <Tooltip title="更多操作"><Button ref={moreButton} type="text" size="small" icon={<EllipsisOutlined />} aria-label="更多会话操作" aria-haspopup="menu" aria-expanded={menuOpen} /></Tooltip>
    </Dropdown>}
    <Tooltip title={closeLabel}><Button type="text" size="small" icon={<CloseOutlined />} aria-label={closeLabel} onClick={onClose} /></Tooltip>
  </div>;
}
