import { useRef, useState, type ReactNode } from 'react';
import { Button, Collapse, Modal, Tooltip } from 'antd';
import { FileTextOutlined, PaperClipOutlined, RightOutlined, UpOutlined } from '@ant-design/icons';
import ReactMarkdown from 'react-markdown';
import type { InitialAttachment } from '../api/client';
import { readableProse } from './readable';
import { formatBytes } from './evidence';
import EvidenceViewer from './EvidenceViewer';
import styles from './TaskGoal.module.css';
type Props = { open:boolean; goal:string; requirements?:ReactNode; context?:string|null; files?:InitialAttachment[]; onClose:()=>void; };
function Text({text}:{text:string}) {
  return <div className={styles.prose}><ReactMarkdown skipHtml remarkPlugins={[readableProse]} components={{h1:({children})=><h3>{children}</h3>,h2:({children})=><h3>{children}</h3>,img:({alt})=><span>图片：{alt||'未自动加载'}</span>,a:({href,children})=><a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,pre:({children})=><pre tabIndex={0}>{children}</pre>}}>{text}</ReactMarkdown></div>;
}
export default function TaskGoal({open,goal,requirements,context,files=[],onClose}:Props) {
  const [full,setFull]=useState(false);
  const [source,setSource]=useState(false);
  const [fileId,setFileId]=useState<string|null>(null);
  const fileTrigger=useRef<HTMLElement|null>(null);
  const closingFocus=useRef<Element|null>(null);
  const file=files.find(item=>item.id===fileId);
  const long=goal.length>280||goal.split('\n').length>4;
  return <div id="task-goal-content" className={styles.disclosure} data-open={open} onKeyDown={event=>{if(event.key==='Escape'&&!file){event.stopPropagation();onClose();}}}>
    <Collapse ghost activeKey={open?['goal']:[]} styles={{header:{display:'none'},body:{padding:0}}} items={[{key:'goal',label:'任务目标',children:<section className={styles.summary} aria-label="任务目标">
      <header className={styles.header}><h2><FileTextOutlined />任务目标</h2><div className={styles.actions}><Button type="text" size="small" aria-pressed={source} onClick={()=>setSource(!source)}>{source?'阅读':'原文'}</Button><Tooltip title="收起目标"><Button type="text" size="small" icon={<UpOutlined />} aria-label="收起目标" onClick={onClose} /></Tooltip></div></header>
      <div className={`${styles.grid} ${files.length?'':styles.noFiles}`}><div className={styles.main}>{source?<pre className={styles.raw}>{goal}</pre>:<Text text={long&&!full?goal.slice(0,280)+'…':goal} />}{long&&!source&&<Button type="link" size="small" className={styles.fullToggle} aria-expanded={full} onClick={()=>setFull(!full)}>{full?'收起全文':'展开全文'}</Button>}{requirements&&<div className={styles.requirements}><h3>完成要求</h3>{typeof requirements==='string'?<Text text={requirements} />:requirements}</div>}</div>
        {files.length>0&&<aside className={styles.files} aria-label="初始附件"><h3><PaperClipOutlined />初始附件<span>{files.length}</span></h3>{files.map(item=><Button type="text" key={item.id} className={styles.file} aria-label={`预览附件 ${item.filename}`} onClick={event=>{fileTrigger.current=event.currentTarget;setFileId(item.id);}}><FileTextOutlined /><span>{item.filename}</span><small>{formatBytes(item.size)}</small><RightOutlined /></Button>)}</aside>}
      </div>{context&&<Collapse ghost size="small" className={styles.context} items={[{key:'context',label:'背景与约束',children:<Text text={context} />}]} />}
    </section>}]} />
    <Modal title={file?.filename} open={Boolean(file)} onCancel={event=>{event.stopPropagation();closingFocus.current=document.activeElement;setFileId(null);}} focusable={{focusTriggerAfterClose:false}} afterClose={()=>{if(open&&(document.activeElement===document.body||document.activeElement===closingFocus.current))fileTrigger.current?.focus({preventScroll:true});}} footer={null} width={760} styles={{body:{maxHeight:'70dvh',overflow:'auto'}}}>{file&&<EvidenceViewer key={file.uri} evidence={{type:'file',summary:file.filename,uri:file.uri,path:file.path,size:file.size}} initiallyOpen />}</Modal>
  </div>;
}
