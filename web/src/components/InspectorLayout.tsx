import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Drawer } from 'antd';
import { Group, Panel, Separator, type PanelImperativeHandle } from 'react-resizable-panels';
import { InspectorContext } from './inspectorContext';
import styles from './InspectorLayout.module.css';
export default function InspectorLayout({children,inspector,label,onClose,hideOnMobile=false}:{children:ReactNode;inspector:ReactNode|null;label:string;onClose:()=>void;hideOnMobile?:boolean}) {
  const [narrow,setNarrow]=useState(()=>matchMedia('(max-width:899px)').matches);
  const [wide,setWide]=useState(false);
  const panel=useRef<PanelImperativeHandle>(null);
  const width=useRef(440);const normalWidth=useRef(440);
  useEffect(()=>{const media=matchMedia('(max-width:899px)');const update=()=>setNarrow(media.matches);media.addEventListener('change',update);return()=>media.removeEventListener('change',update);},[]);
  const toggleWide=()=>{if(wide)panel.current?.resize(normalWidth.current);else{normalWidth.current=width.current;panel.current?.resize('68%');}setWide(!wide);};
  return <InspectorContext.Provider value={{wide,narrow,toggleWide}}><div className={styles.stage} onKeyDown={event=>{if(event.key==='Escape'&&inspector&&!event.defaultPrevented&&!(event.target instanceof Element&&event.target.closest('[role=menu],[role=listbox],[role=dialog]'))){event.stopPropagation();onClose();}}}>
    <Group orientation="horizontal" className={styles.group}><Panel id="canvas" minSize={narrow?0:300} className={styles.canvas}>{children}</Panel>{!narrow&&inspector&&<><Separator className={styles.separator} aria-label="调整详情面板宽度" onDoubleClick={()=>{panel.current?.resize(440);setWide(false);}} /><Panel id="inspector" defaultSize={width.current} minSize={360} maxSize="70%" groupResizeBehavior="preserve-pixel-size" panelRef={panel} onResize={size=>{width.current=size.inPixels;}}><div className={styles.inspector} data-inspector-panel={label}>{inspector}</div></Panel></>}</Group>
    {narrow&&<Drawer autoFocus={false} aria-label={label} open={Boolean(inspector)} onClose={onClose} mask={false} getContainer={false} size="100%" closable={false} focusable={{trap:false,focusTriggerAfterClose:false}} rootStyle={{position:'absolute',display:hideOnMobile?'none':undefined}} styles={{body:{padding:0,display:'flex'},section:{boxShadow:'none'}}}><div className={styles.inspector} data-inspector-panel={label}>{inspector}</div></Drawer>}
  </div></InspectorContext.Provider>;
}
