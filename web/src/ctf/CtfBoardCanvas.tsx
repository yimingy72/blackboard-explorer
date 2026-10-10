import { useEffect, useMemo, useRef, useState } from 'react';
import { Background, BackgroundVariant, Controls, MiniMap, ReactFlow, ReactFlowProvider, Handle, Position, useNodesInitialized, useNodesState, useReactFlow, type NodeProps } from '@xyflow/react';
import type { CtfChallenge, CtfMember, CtfRecord } from './types';
import { deriveCtfGraph, type CtfGraphNode, type CtfGraphNodeData } from './graph';
import styles from './CtfBoardCanvas.module.css';
function statusLabel(status: string): string {
    return ({ running: '工作中', idle: '等待任务', finished: '本轮已结束', stopped: '已停止', removed: '已移除', active: '活动中', pending: '待领取', in_progress: '进行中', blocked: '需要增援', completed: '已完成', cancelled: '已取消', candidate: '候选答案', accepted: '已通过', rejected: '未通过', unknown: '待核实' } as Record<string, string>)[status] ?? status;
}
function NodeShell({ children, className, label, selected, onClick }: {
    children: React.ReactNode;
    className: string;
    label: string;
    selected: boolean;
    onClick: () => void;
}) {
    return <div className={`${styles.node} ${className} ${selected ? styles.selected : ''}`}>
    <Handle type="target" position={Position.Left} isConnectable={false} className={styles.handle}/>
    <button className={styles.nodeButton} aria-label={label} aria-pressed={selected} onClick={onClick} type="button">{children}</button>
    <Handle type="source" position={Position.Right} isConnectable={false} className={styles.handle}/>
  </div>;
}
function AgentNode({ data, selected }: NodeProps<CtfGraphNode>) {
    const member = data as CtfGraphNodeData;
    return <NodeShell className={`${styles.agent} ${member.role === 'lead' ? styles.lead : ''}`} selected={Boolean(selected)} label={`Agent：${member.label}`} onClick={() => undefined}>
    <span className={styles.agentMark} aria-hidden="true">{member.role === 'lead' ? 'L' : member.label.slice(0, 1)}</span><span className={styles.nodeCopy}><strong title={member.label}>{member.label}</strong><small>{member.role === 'lead' ? 'Lead · ' : '队友 · '}{statusLabel(member.status)}</small></span><span className={styles.nodeCount} aria-label={member.detail}>{member.status === 'running' ? 'LIVE' : '·'}</span>
  </NodeShell>;
}
function TaskNode({ data, selected }: NodeProps<CtfGraphNode>) {
    const task = data as CtfGraphNodeData;
    return <NodeShell className={styles.task} selected={Boolean(selected)} label={`任务：${task.label}`} onClick={() => undefined}>
    <div className={styles.taskHeader}><span className={styles.nodeType}>任务</span><span className={styles.taskStatus}>{statusLabel(task.status)}</span></div><strong className={styles.taskTitle} title={task.label}>{task.label}</strong><span className={styles.taskDetail} title={task.detail}>{task.detail}</span><span className={`${styles.verification} ${task.verification === 'accepted' ? styles.accepted : task.verification === 'rejected' ? styles.rejected : ''}`}>验证：{task.verificationText ?? statusLabel(task.verification)}</span>{task.candidate && <span className={styles.candidate} title={task.candidate}>候选：{task.candidate}</span>}
  </NodeShell>;
}
const nodeTypes = { 'ctf-agent': AgentNode, 'ctf-task': TaskNode };
function CtfFlow({ members, challenges, records, selected, phase, onSelect }: {
    members: CtfMember[];
    challenges: CtfChallenge[];
    records: CtfRecord[];
    selected: string | null;
    phase?: string;
    onSelect: (selection: {
        kind: 'agent' | 'challenge';
        id: string;
    }) => void;
}) {
    const userInteracted = useRef(false);
    const [compact, setCompact] = useState(() => typeof window !== 'undefined' && window.innerWidth <= 650);
    useEffect(() => {
        const update = () => setCompact(window.innerWidth <= 650);
        window.addEventListener('resize', update);
        return () => window.removeEventListener('resize', update);
    }, []);
    const graph = useMemo(() => {
        const base = deriveCtfGraph(members, challenges, records);
        if (phase && ['finished', 'failed', 'stopped'].includes(phase))
            base.nodes = base.nodes.map((node) => node.type === 'ctf-agent' && node.data.status === 'idle' ? { ...node, data: { ...node.data, status: 'finished' } } : node);
        if (!compact)
            return base;
        const agentNodes = base.nodes.filter((node) => node.type === 'ctf-agent');
        const agentIndex = new Map(agentNodes.map((node, index) => [node.id, index]));
        const taskIndex = new Map(base.nodes.filter((node) => node.type === 'ctf-task').map((node, index) => [node.id, index]));
        return {
            ...base,
            nodes: base.nodes.map((node) => node.type === 'ctf-agent'
                ? { ...node, position: { x: 48, y: 34 + (agentIndex.get(node.id) ?? 0) * 104 } }
                : { ...node, position: { x: 48, y: 34 + agentNodes.length * 104 + 30 + (taskIndex.get(node.id) ?? 0) * 146 } }),
        };
    }, [members, challenges, records, compact, phase]);
    const nodes = useMemo(() => graph.nodes.map((node) => ({ ...node, selected: node.id === selected })), [graph.nodes, selected]);
    const [renderNodes, setRenderNodes, onNodesChange] = useNodesState<CtfGraphNode>([]);
    const { fitView } = useReactFlow<CtfGraphNode, typeof graph.edges[number]>();
    const canvasRef = useRef<HTMLDivElement>(null);
    const nodesInitialized = useNodesInitialized({ includeHiddenNodes: true });
    useEffect(() => {
        setRenderNodes((current) => nodes.map((node) => { const previous = current.find((item) => item.id === node.id); return { ...previous, ...node, position: userInteracted.current ? previous?.position ?? node.position : node.position }; }));
    }, [nodes, setRenderNodes]);
    const layoutKey = graph.nodes.map((node) => node.id).join('|') + (compact ? ':compact' : ':wide');
    useEffect(() => {
        if (!graph.nodes.length || !nodesInitialized || userInteracted.current)
            return;
        const frame = requestAnimationFrame(() => { if (!userInteracted.current && canvasRef.current?.clientWidth && canvasRef.current.clientHeight)
            void fitView({ padding: 0.16, duration: 0 }); });
        return () => cancelAnimationFrame(frame);
    }, [fitView, nodesInitialized, layoutKey, graph.nodes.length]);
    useEffect(() => {
        const element = canvasRef.current;
        if (!element || !nodesInitialized || typeof ResizeObserver === 'undefined')
            return;
        let frame = 0;
        const observer = new ResizeObserver(() => {
            if (userInteracted.current)
                return;
            cancelAnimationFrame(frame);
            frame = requestAnimationFrame(() => { if (!userInteracted.current && element.clientWidth && element.clientHeight)
                void fitView({ padding: 0.16, duration: 0 }); });
        });
        observer.observe(element);
        return () => { observer.disconnect(); cancelAnimationFrame(frame); };
    }, [fitView, nodesInitialized]);
    return <div ref={canvasRef} className={styles.canvas} tabIndex={-1} role="region" aria-label="CTF 团队黑板画布"><ReactFlow<CtfGraphNode, typeof graph.edges[number]> nodes={renderNodes} onNodesChange={onNodesChange} edges={graph.edges} nodeTypes={nodeTypes} onMoveStart={(event) => { if (event)
        userInteracted.current = true; }} onNodeDragStart={() => { userInteracted.current = true; }} onNodeClick={(_, node) => { onSelect(node.type === 'ctf-agent' ? { kind: 'agent', id: node.data.entityId } : { kind: 'challenge', id: node.data.entityId }); }} nodesConnectable={false} fitView fitViewOptions={{ padding: 0.16 }} minZoom={0.35} maxZoom={1.5} proOptions={{ hideAttribution: true }} colorMode="light" ariaLabelConfig={{ 'controls.zoomIn.ariaLabel': '放大画布', 'controls.zoomOut.ariaLabel': '缩小画布' }}>
    <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--color-line)"/><Controls onZoomIn={() => { userInteracted.current = true; }} onZoomOut={() => { userInteracted.current = true; }} onFitView={() => { userInteracted.current = true; }} fitViewOptions={{ padding: 0.16, duration: 0 }} showInteractive={false} className={styles.controls} aria-label="画布缩放控制"/><MiniMap onPointerDown={() => { userInteracted.current = true; }} onWheel={() => { userInteracted.current = true; }} style={{ width: 112, height: 70 }} pannable zoomable ariaLabel="CTF 画布缩略图" className={styles.minimap} nodeColor={(node) => node.type === 'ctf-agent' ? 'var(--color-primary)' : 'var(--color-info)'}/>
  </ReactFlow>{!graph.nodes.length && <div className={styles.empty} role="status">等待成员和题目进入画布…</div>}<div className={styles.legend} aria-label="连线说明"><span><i className={styles.claimLine}/>认领</span><span><i className={styles.collaborateLine}/>协作</span></div></div>;
}
export default function CtfBoardCanvas(props: {
    members: CtfMember[];
    challenges: CtfChallenge[];
    records: CtfRecord[];
    selected: string | null;
    phase?: string;
    onSelect: (selection: {
        kind: 'agent' | 'challenge';
        id: string;
    }) => void;
}) {
    return <ReactFlowProvider><CtfFlow {...props}/></ReactFlowProvider>;
}
