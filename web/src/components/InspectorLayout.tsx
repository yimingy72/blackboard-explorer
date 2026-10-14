import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Group, Panel, Separator, type OnPanelResize, type PanelImperativeHandle } from 'react-resizable-panels';
import { InspectorContext } from './inspectorContext';
import styles from './InspectorLayout.module.css';

export default function InspectorLayout({ children, inspector, label, onClose, hideOnMobile = false }: {
  children: ReactNode;
  inspector: ReactNode | null;
  label: string;
  onClose: () => void;
  hideOnMobile?: boolean;
}) {
  const [narrow, setNarrow] = useState(() => matchMedia('(max-width:899px)').matches);
  const [wide, setWide] = useState(false);
  const [renderWidth, setRenderWidth] = useState(440);
  const panel = useRef<PanelImperativeHandle>(null);
  const group = useRef<HTMLDivElement>(null);
  const width = useRef(440);
  const normalWidth = useRef(440);
  const hasInspector = Boolean(inspector);
  const covered = narrow && hasInspector && !hideOnMobile;
  // Take a new default only at mounting boundaries, never during a resize drag.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const defaultWidth = useMemo(() => width.current, [narrow, hasInspector]);
  const onResize = useCallback<NonNullable<OnPanelResize>>(size => {
    if (size.inPixels > 0) { width.current = size.inPixels; setRenderWidth(size.inPixels); }
  }, []);
  useEffect(() => {
    const media = matchMedia('(max-width:899px)');
    const update = () => setNarrow(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  useLayoutEffect(() => { if (group.current) group.current.inert = covered; }, [covered]);
  useEffect(() => {
    if (!hasInspector || narrow && hideOnMobile) return;
    const closeAfterDrag = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || document.activeElement !== document.body) return;
      const overlay = [...document.querySelectorAll('[role=dialog],[role=menu],[role=listbox]')].some((element) => element.getClientRects().length > 0 && (element.getAttribute('role') === 'dialog' || !element.closest('nav,[role=navigation]')));
      if (!overlay) onClose();
    };
    window.addEventListener('keydown', closeAfterDrag);
    return () => window.removeEventListener('keydown', closeAfterDrag);
  }, [hasInspector, narrow, hideOnMobile, onClose]);
  const toggleWide = () => {
    if (wide) panel.current?.resize(normalWidth.current);
    else { normalWidth.current = width.current; panel.current?.resize('68%'); }
    setWide(!wide);
  };
  return <InspectorContext.Provider value={{ wide, narrow, toggleWide }}><div className={styles.stage} onKeyDown={event => {
    if (event.key === 'Escape' && inspector && !event.defaultPrevented && !(event.target instanceof Element && event.target.closest('[role=menu],[role=listbox],[role=dialog]'))) { event.stopPropagation(); onClose(); }
  }}>
    <Group elementRef={group} aria-hidden={covered || undefined} orientation="horizontal" className={styles.group}>
      <Panel id="canvas" minSize={narrow ? 0 : 300} className={styles.canvas}>{children}</Panel>
      {!narrow && inspector && <><Separator className={styles.separator} aria-label="调整详情面板宽度" onDoubleClick={() => { panel.current?.resize(440); setWide(false); }} />
        <Panel id="inspector" aria-hidden defaultSize={defaultWidth} minSize={360} maxSize="70%" groupResizeBehavior="preserve-pixel-size" panelRef={panel} onResize={onResize} />
      </>}
    </Group>
    {/* Reserve desktop space separately so responsive layout never remounts the conversation. */}
    {inspector && <div className={styles.inspector} hidden={narrow && hideOnMobile} style={{ width: narrow ? '100%' : renderWidth }} data-inspector-panel={label}>{inspector}</div>}
  </div></InspectorContext.Provider>;
}
