import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Dropdown, Tooltip } from 'antd';
import '@xyflow/react/dist/style.css';
import { AimOutlined, ApartmentOutlined, ExpandOutlined, HistoryOutlined, MinusOutlined, PlusOutlined, PushpinOutlined, ReloadOutlined, SearchOutlined, ShareAltOutlined, UnorderedListOutlined } from '@ant-design/icons';
import { Background, BackgroundVariant, Handle, MiniMap, Position, ReactFlow, ReactFlowProvider, applyNodeChanges, useNodesInitialized, useNodesState, useReactFlow, type NodeProps } from '@xyflow/react';
import type { CtfChallenge, CtfEvent, CtfMember, CtfRecord } from './types';
import { connectionPoint, ctfNodeSize, deriveCtfGraph, preserveCtfPositions, type CtfGraphEdge, type CtfGraphNode } from './graph';
import styles from './CtfBoardCanvas.module.css';
import CtfConnection from './CtfConnection';
import { diffBoard, loadBoardState, saveBoardState, snapshotBoard, type BoardSnapshot } from './boardState';
import { FileOutlined } from '@ant-design/icons';
import ChallengeStateTag from './ChallengeState';
import { challengeTitle } from './challengeView';

type Selection = { kind: 'agent' | 'challenge'; id: string };
type Props = { taskId: string; ready: boolean; events: CtfEvent[]; members: CtfMember[]; challenges: CtfChallenge[]; records: CtfRecord[]; selected: string | null; phase?: string; onSelect: (selection: Selection) => void; onOpenDispatch?: () => void };
const Actions = createContext<{ select: (selection: Selection) => void; dispatch?: () => void; release: (id: string) => void }>({ select: () => undefined, release: () => undefined });
function statusLabel(status: string): string {
  return ({ running: '工作中', stopping: '正在停止', idle: '等待任务', finished: '本轮已结束', stopped: '已停止', removed: '已移除', interrupted: '执行中断', failed: '执行失败', provisioning: '准备中', pending: '待领取', in_progress: '探索中', blocked: '需要增援', completed: '负责人完成', cancelled: '已取消', archived: '已归档' } as Record<string, string>)[status] ?? status;
}
function Ports() {
  return <>{[Position.Left, Position.Right, Position.Top, Position.Bottom].flatMap((position) => ['source', 'target'].map((type) => <Handle key={`${type}:${position}`} id={`${type}:${position}`} type={type as 'source' | 'target'} position={position} isConnectable={false} className={styles.handle} />))}</>;
}
function AgentNode({ data, selected }: NodeProps<CtfGraphNode>) {
  const actions = useContext(Actions), lead = data.role === 'lead';
  return <div className={`${styles.node} ${styles.agent} ${lead ? styles.lead : ''} ${selected ? styles.selected : ''} ${data.related ? styles.related : ''}`}>
    <Ports /><button className={styles.nodeButton} aria-label={`Agent：${data.label}`} aria-pressed={Boolean(selected)} type="button" onClick={(event) => { if (event.detail === 0) actions.select({ kind: 'agent', id: data.entityId }); }}>
      <span className={styles.agentMark} aria-hidden="true">{lead ? <ApartmentOutlined /> : data.label.slice(0, 1).toUpperCase()}</span><span className={styles.nodeCopy}><strong title={data.label}>{data.label}</strong><small title={lead ? data.detail : statusLabel(data.status)}>{lead ? '团队协调' : statusLabel(data.status)}</small></span>
    </button>{data.pinned && <button className={`${styles.pin} nodrag nopan`} aria-label={`恢复 ${data.label} 自动布局`} title="手动位置已保存，点击恢复自动布局" onClick={(event) => { event.stopPropagation(); actions.release(`agent:${data.entityId}`); }}><PushpinOutlined /></button>}{lead && actions.dispatch && <Tooltip title="调度记录"><button type="button" className={`${styles.dispatch} nodrag`} aria-label="查看 Lead 调度记录" onClick={(event) => { event.stopPropagation(); actions.dispatch?.(); }}><UnorderedListOutlined aria-hidden /></button></Tooltip>}
  </div>;
}
function TaskNode({ data, selected }: NodeProps<CtfGraphNode>) {
  const actions = useContext(Actions);
  const title = challengeTitle(data.label);
  return <div data-ctf-activity={data.activity} className={`${styles.node} ${styles.task} ${selected ? styles.selected : ''} ${data.related ? styles.related : ''} ${data.activity === 'accepted' ? styles.successFlash : data.activity ? styles.activityFlash : ''}`}>
    <Ports /><button className={styles.nodeButton} aria-label={`任务：${data.label}`} aria-pressed={Boolean(selected)} type="button" onClick={(event) => { if (event.detail === 0) actions.select({ kind: 'challenge', id: data.entityId }); }}>
      <div className={styles.taskHeader}><span className={styles.taskCategory}><FileOutlined />{title.metadata.join(' · ') || '题目'}</span><ChallengeStateTag state={data.challengeState ?? 'pending'} /></div>
      <strong className={styles.taskTitle} title={data.label}>{title.title}</strong><span className={styles.progress} title={data.progress || '尚无共享进展'}>{data.progress || '尚无共享进展'}</span>
      <span className={styles.taskFoot}><span className={styles.participantMarks} title={data.participants?.map(person => person.name).join('、') || '尚未分派'}>{data.participants?.slice(0, 3).map(person => <span key={person.id} title={person.name}>{person.name.slice(0, 1).toUpperCase()}</span>)}{(data.participantCount ?? 0) > 3 && <small>+{data.participantCount! - 3}</small>}{!data.participantCount && <small>未分配</small>}</span><span>{data.recordCount ?? 0} 进展</span><span><FileOutlined /> {data.artifactCount ?? 0}</span></span>
    </button>{data.pinned && <button className={`${styles.pin} nodrag nopan`} aria-label={`恢复 ${data.label} 自动布局`} title="手动位置已保存，点击恢复自动布局" onClick={(event) => { event.stopPropagation(); actions.release(`challenge:${data.entityId}`); }}><PushpinOutlined /></button>}
  </div>;
}
const nodeTypes = { 'ctf-agent': AgentNode, 'ctf-task': TaskNode };
const edgeTypes = { 'ctf-connection': CtfConnection };
type ActiveSignal = { id: string; source: string; target: string; kind: string; reverse: boolean; expires: number };
function CtfFlow({ taskId, ready, events, members, challenges, records, selected, phase, onSelect, onOpenDispatch }: Props) {
  const saved = useMemo(() => loadBoardState(taskId), [taskId]);
  const userInteracted = useRef(Boolean(saved?.viewport)), pinned = useRef(new Set(saved?.pinned ?? []));
  const [allRelations, setAllRelations] = useState(false), [hovered, setHovered] = useState<string | null>(null), [zoom, setZoom] = useState(saved?.viewport?.zoom ?? 1);
  const [collapsed, setCollapsed] = useState(saved?.collapsed ?? true), [saveError, setSaveError] = useState(false);
  const [reduced, setReduced] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [signals, setSignals] = useState<ActiveSignal[]>([]), [layoutRevision, setLayoutRevision] = useState(0);
  const graph = useMemo(() => {
    const result = deriveCtfGraph(members, challenges, records);
    if (phase && ['finished', 'failed', 'stopped'].includes(phase)) result.nodes = result.nodes.map((node) => node.type === 'ctf-agent' && node.data.status === 'idle' ? { ...node, data: { ...node.data, status: 'finished' } } : node);
    return result;
  }, [members, challenges, records, phase]);
  const [layoutNodes, setLayoutNodes] = useNodesState<CtfGraphNode>([]);
  const nodesRef = useRef(layoutNodes), previous = useRef<BoardSnapshot | null>(null), restored = useRef(false);
  const viewport = useRef(saved?.viewport), collapsedRef = useRef(collapsed), dragging = useRef(false), frame = useRef(0);
  const resetIds = useRef(new Set<string>()), viewReady = useRef(false);
  const movement = useRef<{ start: number; from: Map<string, { x: number; y: number }>; target: CtfGraphNode[] } | null>(null);
  const flow = useReactFlow<CtfGraphNode, CtfGraphEdge>(), canvasRef = useRef<HTMLDivElement>(null);
  const initialized = useNodesInitialized({ includeHiddenNodes: false });
  const persist = useCallback((nodes = nodesRef.current) => {
    const ids = new Set(nodes.map((node) => node.id));
    const state = { positions: Object.fromEntries(nodes.map((node) => [node.id, node.position])), pinned: [...pinned.current].filter((id) => ids.has(id)), viewport: viewport.current, collapsed: collapsedRef.current };
    setSaveError(!saveBoardState(taskId, state));
  }, [taskId]);
  useEffect(() => { collapsedRef.current = collapsed; if (restored.current) persist(); }, [collapsed, persist]);
  useEffect(() => {
    const media = matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => { setReduced(media.matches); if (media.matches) setSignals([]); };
    media.addEventListener('change', update); return () => media.removeEventListener('change', update);
  }, []);
  useEffect(() => {
    if (!signals.length) return;
    const timer = setTimeout(() => setSignals((current) => current.filter((signal) => signal.expires > Date.now())), Math.max(0, Math.min(...signals.map((signal) => signal.expires)) - Date.now()));
    return () => clearTimeout(timer);
  }, [signals]);
  useEffect(() => {
    if (dragging.current) return;
    let before = nodesRef.current;
    if (!restored.current && ready) {
      const existing = new Map(before.map((node) => [node.id, node]));
      before = graph.nodes.flatMap((node) => saved?.positions[node.id] ? [{ ...existing.get(node.id), ...node, position: saved.positions[node.id] }] : existing.has(node.id) ? [existing.get(node.id)!] : []);
      restored.current = true;
    }
    const snapshot = ready ? snapshotBoard(challenges, records) : null;
    const changes = snapshot && previous.current ? diffBoard(previous.current, snapshot, members) : { movedIds: [], signals: [] };
    if (snapshot) previous.current = snapshot;
    const moving = new Set([...changes.movedIds, ...resetIds.current]);
    resetIds.current.clear();
    const previousTargets = new Map(movement.current?.target.map((node) => [node.id, node.position]) ?? []);
    const stableBefore = before.map((node) => previousTargets.has(node.id) && !pinned.current.has(node.id) ? { ...node, position: previousTargets.get(node.id)! } : node);
    const previousNodes = new Map(before.map((node) => [node.id, node]));
    const target = preserveCtfPositions(graph.nodes, stableBefore, moving, pinned.current).map((node) => ({ ...previousNodes.get(node.id), ...node }));
    if (!reduced && !document.hidden && ['running', 'closing'].includes(phase ?? '') && changes.signals.length) {
      const next: ActiveSignal[] = changes.signals.map((signal) => ({ ...signal, expires: Date.now() + 1900 }));
      const lead = members.find((member) => member.role === 'lead');
      for (const signal of changes.signals.filter((item) => item.kind === 'claim')) {
        const challengeId = signal.target.slice('challenge:'.length);
        const challenge = challenges.find((item) => item.id === challengeId);
        const assignment = events.find((event) => {
          const result = event.payload.result as Record<string, unknown> | undefined;
          return event.type === 'ctf.challenge.updated' && result?.id === challengeId && result.revision === challenge?.revision && event.actor === lead?.id;
        });
        if (lead && assignment && signal.source !== `agent:${lead.id}`) next.push({ ...signal, id: `dispatch:${assignment.version}:${signal.source}`, source: `agent:${lead.id}`, target: signal.source, kind: 'dispatch', reverse: false, expires: Date.now() + 1900 });
      }
      setSignals((current) => [...current.filter((signal) => signal.expires > Date.now()), ...next].slice(-24));
    }
    cancelAnimationFrame(frame.current);
    if (!reduced && ready && viewReady.current && (moving.size > 0 || movement.current)) {
      movement.current = moving.size > 0 || !movement.current
        ? { start: performance.now(), from: new Map(before.map((node) => [node.id, node.position])), target }
        : { ...movement.current, target };
    } else movement.current = null;
    const draw = (now: number) => {
      const animation = movement.current;
      const progress = animation ? Math.min(1, (now - animation.start) / 450) : 1, eased = 1 - (1 - progress) ** 4;
      const next = target.map((node) => { const from = animation?.from.get(node.id) ?? node.position; return { ...node, position: { x: from.x + (node.position.x - from.x) * eased, y: from.y + (node.position.y - from.y) * eased } }; });
      nodesRef.current = next; setLayoutNodes(next);
      if (progress < 1) frame.current = requestAnimationFrame(draw);
      else { movement.current = null; if (ready) persist(next); }
    };
    if (movement.current) frame.current = requestAnimationFrame(draw); else draw(performance.now());
    return () => cancelAnimationFrame(frame.current);
  }, [graph, ready, challenges, records, members, events, phase, reduced, layoutRevision, persist, saved, setLayoutNodes]);
  const historyCount = graph.nodes.filter((node) => node.data.history).length;
  const focusId = selected ?? hovered;
  const related = useMemo(() => {
    const ids = new Set(graph.edges.filter((edge) => edge.source === focusId || edge.target === focusId).flatMap((edge) => [edge.source, edge.target]));
    if (focusId?.startsWith('challenge:')) for (const edge of graph.edges) if (edge.data?.relation === 'team' && ids.has(edge.target)) ids.add(edge.source);
    return ids;
  }, [graph.edges, focusId]);
  const visibleSignal = (id: string) => signals.some((signal) => signal.source === id || signal.target === id);
  const renderNodes = layoutNodes.map((node) => ({ ...node, style: ctfNodeSize(node), hidden: Boolean(collapsed && node.data.history && node.id !== selected && !visibleSignal(node.id)), selected: node.id === selected, data: { ...node.data, pinned: pinned.current.has(node.id), related: node.id !== focusId && related.has(node.id), activity: reduced ? undefined : signals.find((signal) => signal.target === node.id && signal.kind === 'accepted')?.kind ?? signals.find((signal) => signal.target === node.id)?.kind } }));
  useEffect(() => {
    if (!ready || !initialized || !graph.nodes.length || viewReady.current || !canvasRef.current?.clientWidth) return;
    if (saved?.viewport) { void flow.setViewport(saved.viewport, { duration: 0 }); viewReady.current = true; return; }
    const frameId = requestAnimationFrame(() => {
      if (userInteracted.current || !canvasRef.current) { viewReady.current = true; return; }
      const visible = flow.getNodes().filter((node) => !node.hidden);
      const lead = visible.find((node) => node.data.role === 'lead');
      const center = lead ? { x: lead.position.x + ctfNodeSize(lead).width / 2, y: lead.position.y + ctfNodeSize(lead).height / 2 } : { x: 0, y: 0 };
      const extentX = Math.max(1, ...visible.map((node) => Math.max(Math.abs(node.position.x - center.x), Math.abs(node.position.x + ctfNodeSize(node).width - center.x))));
      const extentY = Math.max(1, ...visible.map((node) => Math.max(Math.abs(node.position.y - center.y), Math.abs(node.position.y + ctfNodeSize(node).height - center.y))));
      const scale = Math.max(visible.length > 12 ? .7 : .15, Math.min(1, (canvasRef.current.clientWidth - 90) / (extentX * 2), (canvasRef.current.clientHeight - 110) / (extentY * 2)));
      void flow.setCenter(center.x, center.y, { zoom: scale, duration: 0 });
      viewReady.current = true;
    });
    return () => cancelAnimationFrame(frameId);
  }, [flow, ready, initialized, graph.nodes, saved]);
  const edges = useMemo(() => {
    const byId = new Map(layoutNodes.map((node) => [node.id, node]));
    const list = [...graph.edges];
    for (const signal of signals) if (!list.some((edge) => edge.source === signal.source && edge.target === signal.target) && byId.has(signal.source) && byId.has(signal.target)) list.push({ id: `activity:${signal.id}`, source: signal.source, target: signal.target, data: { relation: 'activity' } });
    return list.flatMap((edge): CtfGraphEdge[] => {
      const source = byId.get(edge.source), target = byId.get(edge.target);
      if (!source || !target) return [];
      const signal = reduced ? undefined : [...signals].reverse().find((item) => item.source === edge.source && item.target === edge.target);
      if (collapsed && target.data.history && edge.target !== selected && !signals.some((item) => item.target === edge.target)) return [];
      const active = edge.source === focusId || edge.target === focusId || Boolean(focusId?.startsWith('challenge:') && edge.data?.relation === 'team' && related.has(edge.target));
      if (edge.data?.relation === 'team' && !allRelations && !active && !signal) return [];
      const ss = ctfNodeSize(source), ts = ctfNodeSize(target), emphasized = active || Boolean(signal), dimmed = Boolean(focusId && !active);
      return [{ ...edge, type: 'ctf-connection', sourceHandle: 'source:right', targetHandle: 'target:left', markerEnd: undefined, style: { stroke: signal?.kind === 'accepted' ? '#329268' : emphasized ? '#659aca' : '#aebfcf', strokeWidth: emphasized ? 1.4 : 1, strokeDasharray: edge.data?.relation === 'collaborate' ? '3 5' : undefined, opacity: dimmed ? .2 : edge.data?.relation === 'team' ? .65 : .85 }, data: { ...edge.data!, from: connectionPoint(source, { x: target.position.x + ts.width / 2, y: target.position.y + ts.height / 2 }), to: connectionPoint(target, { x: source.position.x + ss.width / 2, y: source.position.y + ss.height / 2 }), emphasized, dimmed, signal } }];
    });
  }, [graph.edges, layoutNodes, allRelations, focusId, related, collapsed, selected, signals, reduced]);
  const select = useCallback((next: Selection) => { userInteracted.current = true; onSelect(next); }, [onSelect]);
  const duration = reduced ? 0 : 220;
  const locate = (id: string, open = false) => {
    const node = nodesRef.current.find((item) => item.id === id);
    if (!node) return;
    if (node.data.history) { setCollapsed(false); open = true; }
    const size = ctfNodeSize(node); userInteracted.current = true;
    void flow.setCenter(node.position.x + size.width / 2, node.position.y + size.height / 2, { zoom: 1, duration });
    if (open) select({ kind: node.type === 'ctf-agent' ? 'agent' : 'challenge', id: node.data.entityId });
  };
  const release = (id: string) => { pinned.current.delete(id); resetIds.current.add(id); setLayoutRevision((value) => value + 1); };
  const reset = () => { pinned.current.clear(); for (const node of graph.nodes) resetIds.current.add(node.id); setLayoutRevision((value) => value + 1); };
  const toggleHistory = () => { userInteracted.current = true; setCollapsed((value) => !value); };
  return <Actions.Provider value={{ select, dispatch: onOpenDispatch, release }}><div ref={canvasRef} className={styles.canvas} tabIndex={-1} role="region" aria-label="CTF 团队黑板画布">
    <ReactFlow<CtfGraphNode, CtfGraphEdge> nodes={renderNodes} onNodesChange={(changes) => setLayoutNodes((current) => { const next = applyNodeChanges(changes, current); nodesRef.current = next; return next; })} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} onMoveStart={(event) => { if (event) userInteracted.current = true; }} onMove={(_, view) => { viewport.current = view; setZoom(view.zoom); }} onMoveEnd={() => { if (restored.current && viewReady.current) persist(); }} onNodeDragStart={(_, node) => { userInteracted.current = true; dragging.current = true; pinned.current.add(node.id); movement.current = null; cancelAnimationFrame(frame.current); }} onNodeDragStop={(_, node) => { dragging.current = false; const next = flow.getNodes().map((item) => item.id === node.id ? { ...item, position: node.position } : item); nodesRef.current = next; persist(next); setLayoutRevision((value) => value + 1); }} onNodeMouseEnter={(_, node) => setHovered(node.id)} onNodeMouseLeave={() => setHovered(null)} onNodeClick={(_, node) => select({ kind: node.type === 'ctf-agent' ? 'agent' : 'challenge', id: node.data.entityId })} nodesFocusable={false} nodesConnectable={false} minZoom={.15} maxZoom={1.5} proOptions={{ hideAttribution: true }} colorMode="light">
      <Background variant={BackgroundVariant.Dots} gap={28} size={.6} color="#d9e2ed"/>
      <MiniMap onPointerDown={() => { userInteracted.current = true; }} onWheel={() => { userInteracted.current = true; }} style={{ width: 100, height: 64 }} pannable zoomable ariaLabel="CTF 画布缩略图" className={styles.minimap} nodeColor={(node) => node.data.role === 'lead' ? '#7194c9' : node.type === 'ctf-agent' ? '#a9bbd5' : '#e0e7f0'} />
    </ReactFlow>
    {!graph.nodes.length && <div className={styles.empty} role="status">等待成员和题目进入画布…</div>}
    <div className={styles.canvasActions}><Tooltip title="定位 Leader"><Button type="text" aria-label="定位 Leader" icon={<AimOutlined/>} onClick={() => { const lead = graph.nodes.find((node) => node.data.role === 'lead'); if (lead) locate(lead.id); }}/></Tooltip><Dropdown placement="bottomRight" align={{ overflow: { adjustX: true, adjustY: true, shiftX: true } }} trigger={['click']} styles={{ root: { maxHeight: 'min(70vh, 560px)', overflowY: 'auto', width: 300, maxWidth: 'calc(100vw - 24px)' } }} menu={{ items: [{ type: 'group', label: 'Agent', children: graph.nodes.filter((node) => node.type === 'ctf-agent').map((node) => ({ key: node.id, label: <span className={styles.locateLabel} title={node.data.label}>{node.data.label}</span> })) }, { type: 'group', label: '题目', children: graph.nodes.filter((node) => node.type === 'ctf-task').map((node) => ({ key: node.id, label: <span className={styles.locateLabel} title={node.data.label}>{node.data.label}</span> })) }], onClick: ({ key }) => locate(key) }}><Button type="text" icon={<SearchOutlined />} aria-label="定位节点">定位</Button></Dropdown><Tooltip title={allRelations ? '收起全部协作关系' : '显示全部协作关系'}><Button type="text" icon={<ShareAltOutlined />} aria-label={allRelations ? '收起全部协作关系' : '显示全部协作关系'} aria-pressed={allRelations} onClick={() => setAllRelations(!allRelations)} /></Tooltip><Tooltip title="清除手动位置，重新整理"><Button type="text" icon={<ReloadOutlined/>} aria-label="重新整理画布" onClick={reset}/></Tooltip></div>
    <div className={styles.bottomBar}><div className={styles.controls}><Button type="text" aria-label="放大画布" icon={<PlusOutlined />} onClick={() => { userInteracted.current = true; void flow.zoomIn({ duration }); }} /><Button type="text" aria-label="缩小画布" icon={<MinusOutlined />} onClick={() => { userInteracted.current = true; void flow.zoomOut({ duration }); }} /><Tooltip title="恢复可读视图"><Button type="text" aria-label="恢复可读视图" onClick={() => { userInteracted.current = true; void flow.zoomTo(1, { duration }); }}>{Math.round(zoom * 100)}%</Button></Tooltip><Tooltip title="全图总览"><Button type="text" aria-label="全图总览" icon={<ExpandOutlined />} onClick={() => { userInteracted.current = true; void flow.fitView({ padding: .1, duration }); }} /></Tooltip></div>{historyCount > 0 && <Button icon={<HistoryOutlined/>} aria-label={collapsed ? `显示已完成题目 (${historyCount})` : '折叠已完成题目'} onClick={toggleHistory}>{collapsed ? `已完成 ${historyCount}` : '收起已完成'}</Button>}<span className={styles.saveStatus} role="status">{saveError ? '浏览器未能保存位置' : pinned.current.size ? `已保存 ${pinned.current.size} 个手动位置` : ''}</span></div>
    <div className={styles.legend} aria-label="连线说明"><span><i className={styles.claimLine} />主责</span><span><i className={styles.collaborateLine} />协作</span><span>选中查看团队关系</span></div>
  </div></Actions.Provider>;
}
export default function CtfBoardCanvas(props: Props) { return <ReactFlowProvider key={props.taskId}><CtfFlow {...props} /></ReactFlowProvider>; }
