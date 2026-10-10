import { useId, useState, type ReactNode } from 'react';
import { Button } from 'antd';
import { Think } from '@ant-design/x';
export default function ReasoningBlock({title,children}:{title:string;children:ReactNode}) {
  const [expanded,setExpanded]=useState(false);const id=useId();
  return <Think expanded={expanded} onExpand={setExpanded} styles={{root:{marginBlock:'2px 6px'},status:{gap:6,lineHeight:1.45},content:{marginTop:6,paddingInlineStart:10}}} title={<Button type="text" size="small" style={{padding:0,height:'auto'}} aria-expanded={expanded} aria-controls={id} onClick={event=>{event.stopPropagation();setExpanded(!expanded);}}>{title}</Button>}><div id={id} role="region" aria-label={title}>{children}</div></Think>;
}
