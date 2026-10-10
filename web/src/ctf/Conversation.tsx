import { Alert, Avatar, Button, Collapse, Empty, Modal, Spin } from 'antd';
import { Bubble, Sender } from '@ant-design/x';
import { useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ctfApi } from './api';
import type { CtfMember } from './types';
import { canSend, conversationEntries, deliveryLabel, isReviewPhase, memberStatus } from './view';
import { useAction } from './useAction';
import Markdown from './Markdown';
import InspectorActions from '../components/InspectorActions';
import ReasoningBlock from '../components/ReasoningBlock';
import ToolSteps from '../components/ToolSteps';
import {createAgentClientState,type AgentClientState} from '../components/agentClientState';
import styles from './CtfWorkbench.module.css';
export default function Conversation({ taskId, member, members, phase, onClose, draftValue, onDraftChange, clientState }: {
    taskId: string;
    member: CtfMember;
    members: CtfMember[];
    phase: string;
    onClose: () => void;
    clientState?:AgentClientState;
    draftValue?:string;
    onDraftChange?:(value:string)=>void;
}) {
    const queryClient = useQueryClient();
    const review = isReviewPhase(phase);
    const [localDraft,setLocalDraft]=useState('');
    const fallback=useRef(createAgentClientState());const client=clientState??fallback.current;
    const draft=draftValue??localDraft;const setDraft=(value:string)=>{client.draftRevision+=1;(onDraftChange??setLocalDraft)(value);};
    const [removing, setRemoving] = useState(false);
    const sendAction = useAction();
    const memberAction = useAction();
    const messages = useQuery({ queryKey: ['ctf', taskId, 'messages', member.id], queryFn: ({ signal }) => ctfApi.messages(taskId, member.id, signal), refetchInterval: 3000 });
    const session = useQuery({ queryKey: ['ctf', taskId, 'session', member.id], queryFn: ({ signal }) => ctfApi.session(taskId, member.id, signal), refetchInterval: 3000 });
    const entries = useMemo(() => conversationEntries(session.data, messages.data?.messages ?? [], member.id), [session.data, messages.data, member.id]);
    const label = (id: string) => id === 'user' ? '你' : id === 'system' ? '系统通知' : members.find((item) => item.id === id)?.display_name ?? id;
    async function send() {
        const content = draft.trim();
        if (!content || !canSend(phase, member))
            return;
        if(client.message?.content!==content)client.message={id:crypto.randomUUID(),content};
        const message=client.message;const revision=client.draftRevision;
        if (await sendAction.run(async (signal) => {
            await ctfApi.send(taskId, member.id, message, signal);
            await queryClient.invalidateQueries({ queryKey: ['ctf', taskId, 'messages', member.id] });
        })) {
            if(client.message===message)client.message=null;
            if(client.draftRevision===revision)setDraft('');
        }
    }
    async function manage(next: 'stop' | 'resume' | 'remove') {
        const key=`${next}:${member.generation}`;
        const id=client.actions[key]??=crypto.randomUUID();
        if (await memberAction.run(async (signal) => {
            await ctfApi.member(taskId, member.id, next, id, signal);
            await queryClient.invalidateQueries({ queryKey: ['ctf', taskId] });
        })) {
            delete client.actions[key];
            setRemoving(false);
        }
    }
    const error = messages.error ?? session.error;
    return <aside className={styles.conversation} aria-label={`${member.display_name} 会话`}>
      <header className={styles.panelHeader}><Avatar size={28} shape="square" style={{background:'#eaf0fb',color:'#386ac0'}}>{member.role==='lead'?'L':member.display_name.slice(0,1).toUpperCase()}</Avatar><div className={styles.panelIdentity}><h2 tabIndex={-1}>{member.display_name}</h2><p>{review?'本轮已结束 · 复盘问答':memberStatus(member)}</p></div><InspectorActions onClose={onClose} menu={member.role!=='lead'&&member.lifecycle!=='removed'&&phase==='running'?{items:[{key:'state',label:member.lifecycle==='stopped'?'恢复成员':'停止成员',disabled:memberAction.busy||member.run_state==='stopping'},{key:'remove',label:'移除',danger:true,disabled:memberAction.busy||member.run_state==='stopping'}],onClick:({key})=>key==='remove'?setRemoving(true):void manage(member.lifecycle==='stopped'?'resume':'stop')}:undefined} /></header>
      <Modal title={`移除 ${member.display_name}？`} open={removing} onCancel={() => { if (!memberAction.busy) setRemoving(false); }} onOk={() => void manage('remove')} okText="确认移除" cancelText="取消" confirmLoading={memberAction.busy} okButtonProps={{danger:true}} cancelButtonProps={{disabled:memberAction.busy}} closable={!memberAction.busy} mask={{closable:!memberAction.busy}} keyboard={!memberAction.busy}><p>系统会先停止执行，会话和记录继续保留。</p>{memberAction.error && <Alert type="error" title={memberAction.error} />}</Modal>
      {memberAction.error && !removing && <Alert type="error" title={memberAction.error} showIcon />}
      {error && <Alert type="error" title={error.message} action={<Button onClick={() => { void messages.refetch(); void session.refetch(); }}>重新读取会话</Button>} />}
      {(messages.isLoading || session.isLoading) && <div className={styles.empty} role="status"><Spin /> 正在读取会话…</div>}
      {!entries.length && !messages.isLoading && !session.isLoading && <div className={styles.conversationEmpty}><Empty description={member.role === 'lead' ? '与 Lead 开始协作' : '尚无对话记录'} /></div>}
      <Bubble.List className={styles.chatList} autoScroll role={{assistant:{variant:"borderless",styles:{root:{paddingBlock:6,paddingInlineEnd:0},content:{padding:0,minHeight:0},body:{gap:4},header:{marginBottom:2}}},user:{placement:"end",styles:{root:{paddingBlock:6,paddingInlineStart:"12%"},content:{padding:"7px 10px",minHeight:0},header:{marginBottom:2}}}}} items={entries.map((entry) => ({
        key: entry.id, role: entry.speaker === 'user' ? 'user' : 'assistant', typing: false, streaming: false,
        header: <div className={styles.entryMeta}><strong>{label(entry.speaker)}</strong>{entry.delivery && entry.delivery.sender_id === member.id && <span>→ {label(entry.delivery.recipient_id)}</span>}{entry.delivery?.created_at && <time dateTime={entry.delivery.created_at}>{new Date(entry.delivery.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</time>}</div>,
        content:<><div className={styles.reasoningGroup}>{entry.reasoning.map((reasoning,index)=><ReasoningBlock key={index} title={reasoning.kind==='summary'?'思考摘要':'思考 · 模型返回'}><Markdown text={reasoning.text} /></ReasoningBlock>)}</div>{entry.body&&(entry.speaker==='system'?<Collapse ghost size="small" items={[{key:'context',label:'系统上下文',children:<Markdown text={entry.body} />}]} />:<Markdown text={entry.body} />)}{entry.toolCalls.length>0&&<ToolSteps steps={entry.toolCalls.map(call=>({id:call.id,name:call.name,status:call.status==='completed'?'success':call.status==='failed'?'error':'loading',label:call.status==='completed'?'已完成':call.status==='failed'?'失败':'等待结果',content:<div className={styles.toolContent}><h3>参数</h3><pre tabIndex={0}>{typeof call.arguments==='string'?call.arguments:JSON.stringify(call.arguments,null,2)}</pre>{call.result!==undefined&&<><h3>结果</h3><pre tabIndex={0}>{typeof call.result==='string'?call.result:JSON.stringify(call.result,null,2)}</pre></>}</div>}))} />}</>,
        footer: entry.delivery ? <small className={styles.delivery}>{deliveryLabel(entry.delivery)}</small> : undefined,
      }))} />
      <div className={styles.composer}><Sender aria-label={`发给 ${member.display_name} 的消息`} value={draft} onChange={(value) => setDraft(value.slice(0,20000))} onSubmit={() => void send()} autoSize={{minRows:1,maxRows:4}} styles={{content:{padding:"8px 10px",gap:6},input:{fontSize:14,lineHeight:1.5}}} disabled={sendAction.busy || !canSend(phase,member)} placeholder={review ? '询问结论、证据或下一步建议…' : `发给 ${member.display_name}…`} onKeyDown={(event) => { if (event.key === 'Enter') { if (event.ctrlKey || event.metaKey) { event.preventDefault(); void send(); } return false; } }} suffix={(_, {components}) => <components.SendButton aria-label={sendAction.error ? '重试发送' : review ? '发送复盘消息' : '发送消息'} loading={sendAction.busy} disabled={sendAction.busy || !draft.trim() || !canSend(phase,member)} />} />
        <div className={styles.composerFooter}><span>{review ? '复盘问答' : phase === 'closing' ? '收尾后待处理' : member.lifecycle === 'stopped' ? '恢复后送达' : '直接发给此 Agent'}</span><span>Ctrl/⌘+Enter</span></div>{sendAction.error && <Alert type="error" title={`${sendAction.error} 消息内容已保留。`} />}
      </div>
    </aside>;
}
