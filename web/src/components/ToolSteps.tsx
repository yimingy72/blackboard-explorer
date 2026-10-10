import { useId, useState, type ReactNode } from 'react';
import { Button } from 'antd';
import { ThoughtChain } from '@ant-design/x';
type Step={id:string;name:string;status?:'success'|'error'|'loading';label?:string;content:ReactNode};
export default function ToolSteps({steps}:{steps:Step[]}) {
  const [expanded,setExpanded]=useState<string[]>([]);const id=useId();
  return <ThoughtChain expandedKeys={expanded} onExpand={keys=>setExpanded(keys.map(String))} styles={{item:{paddingBlock:2,gap:8},itemHeader:{gap:2,lineHeight:1.45},itemContent:{marginBottom:6}}} items={steps.map((step,index)=>({key:step.id,status:step.status,collapsible:true,title:<Button type="text" size="small" style={{padding:0,height:'auto',maxWidth:'100%'}} aria-expanded={expanded.includes(step.id)} aria-controls={`${id}-${index}`} onClick={event=>{event.stopPropagation();setExpanded(current=>current.includes(step.id)?current.filter(key=>key!==step.id):[...current,step.id]);}}><code>{step.name}</code>{step.label&&<small style={{color:'var(--color-muted)',fontSize:11,marginLeft:8}}>{step.label}</small>}</Button>,content:<div id={`${id}-${index}`} role="region" aria-label={`${step.name} 参数与结果`}>{step.content}</div>}))} />;
}
