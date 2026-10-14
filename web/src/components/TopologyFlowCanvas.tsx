import { AimOutlined, BulbOutlined, ExpandOutlined, FileTextOutlined, HistoryOutlined, MinusOutlined, PlusOutlined, PushpinOutlined, ReloadOutlined, SearchOutlined, ShareAltOutlined } from '@ant-design/icons';
import { Button, Dropdown, Tooltip } from 'antd';
import { Background, BackgroundVariant, Handle, MarkerType, MiniMap, Position, ReactFlow, ReactFlowProvider, applyNodeChanges, useNodesInitialized, useReactFlow, type NodeChange, type NodeProps } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { deriveGraph, type GraphEdge, type GraphNode } from '../board/graph';
import type { BoardEvent, BoardState } from '../board/types';
import { loadCanvasState, saveCanvasState } from './canvasState';
import { layoutGraph, NODE_SIZE, sameTopology } from './layout';
import BlackboardState from './BlackboardState';
import BlackboardConnection from './BlackboardConnection';
import styles from './TopologyFlowCanvas.module.css';
type Props = { taskId: string; state: BoardState; events?: BoardEvent[]; historical?: boolean; agentNumbers?: Record<string, number>; selectedId?: string | null; onSelect: (id: string | null) => void };
type Signal = { id: string; source?: string; target: string; kind: string; expires: number };
const Actions = createContext<{ select: (id: string) => void; release: (id: string) => void }>({ select: () => undefined, release: () => undefined });
const emptyNumbers: Record<string, number> = {};
const kindLabels: Record<string, string> = { observation: '观察', inference: '推断', structure: '结构' };
const intentLabels: Record<string, string> = { open: '待认领', claimed: '调查中', closed: '已关闭' };
const relationLabels: Record<string, string> = { derived_from: '推导自', based_on: '依据', resolves: '结论', disputes: '争议', retry_of: '重新尝试', claim: '认领', authored: '产出' };
function Shell({ id, data, selected, className, label, children }: NodeProps<GraphNode> & { className: string; label: string; children: React.ReactNode }) {
  const actions = useContext(Actions);
  return <div data-blackboard-activity={data.activity} className={`${styles.node} ${className} ${selected ? styles.selected : ''} ${data.related ? styles.related : ''} ${data.activity ? styles.activity : ''}`}>
    <Handle type="target" position={Position.Left} isConnectable={false} className={styles.handle} />
    <button className={styles.nodeButton} type="button" aria-label={label} aria-pressed={Boolean(selected)} onClick={event => { if (event.detail === 0) { event.stopPropagation(); actions.select(id); } }}>{children}</button>
    {data.pinned && <button className={`${styles.pin} nodrag nopan`} type="button" aria-label={`恢复 ${id} 自动布局`} title="手动位置已保存，点击恢复自动布局" onClick={event => { event.stopPropagation(); actions.release(id); }}><PushpinOutlined /></button>}
    <Handle type="source" position={Position.Right} isConnectable={false} className={styles.handle} />
  </div>;
}
function GoalNode(props: NodeProps<GraphNode>) {
  const { data } = props, acceptance = data.acceptance ?? [];
  return <Shell {...props} className={styles.goal} label={`目标：${data.label}`}><div className={styles.nodeTop}><span className={styles.nodeType}><AimOutlined />任务目标</span><BlackboardState kind="goal" status={data.status ?? 'created'} /></div><p className={styles.goalText} title={data.label}>{data.label}</p><div className={styles.nodeMeta}><span>Close 裁定</span><span>{acceptance.filter(item => item.status === 'met').length} / {acceptance.length} 已满足</span></div></Shell>;
}
function FactNode(props: NodeProps<GraphNode>) {
  const { id, data } = props;
  return <Shell {...props} className={`${styles.fact} ${data.status === 'disputed' ? styles.disputed : ''}`} label={`事实 ${id}，${data.status === 'disputed' ? '有争议' : '已提出'}：${data.label}`}><div className={styles.nodeTop}><span className={styles.nodeType}><FileTextOutlined />{kindLabels[data.kind ?? ''] ?? '事实'} <code>{id}</code></span><BlackboardState kind="fact" status={data.status ?? 'proposed'} /></div><p className={styles.statement} title={data.label}>{data.label}</p><div className={styles.nodeMeta}><span className={styles.author}><i />{data.ownerLabel ?? data.author ?? '来源未记录'}</span><span>{data.provenance === 'self_reported' ? '自述' : '工具记录'}{data.reliedBy ? ` · 引用 ${data.reliedBy}` : ''}</span></div></Shell>;
}
function IntentNode(props: NodeProps<GraphNode>) {
  const { id, data } = props;
  return <Shell {...props} className={`${styles.intent} ${data.status === 'claimed' ? styles.claimed : ''}`} label={`意图 ${id}，${intentLabels[data.status ?? 'open']}：${data.label}`}><div className={styles.nodeTop}><span className={styles.nodeType}><BulbOutlined />意图 <code>{id}</code></span><BlackboardState kind="intent" status={data.status ?? 'open'} result={data.result} /></div><p className={styles.statement} title={data.label}>{data.label}</p><div className={styles.nodeMeta}><span className={styles.author}><i />{data.ownerLabel ?? data.author ?? '来源未记录'}</span><span>{data.status === 'closed' ? '已关闭' : data.holder ? `${data.holder} 执行` : data.attempts ? `尝试 ${data.attempts} 次` : '未认领'}</span></div></Shell>;
}
function AgentNode(props: NodeProps<GraphNode>) {
  const { data } = props, role = data.isSeed ? '种子探索' : data.taskType === 'derive' ? '推导' : data.taskType === 'close' ? '裁定 / 终结' : '探索';
  return <Shell {...props} className={styles.agent} label={`${data.label}，${role}`}><span className={styles.agentMark} aria-hidden="true">{data.taskType === 'close' ? 'C' : data.taskType === 'derive' ? 'D' : 'E'}</span><span className={styles.agentText}><strong>{data.label}</strong><small>{role} · {data.status === 'running' ? '运行中' : data.status === 'concluding' ? '交接中' : data.status === 'failed' ? '失败' : '已结束'}</small></span></Shell>;
}
const nodeTypes = { goal: GoalNode, fact: FactNode, intent: IntentNode, agent: AgentNode };
const edgeTypes = { 'blackboard-connection': BlackboardConnection };
function GraphCanvas({ taskId, state, events = [], historical = false, agentNumbers = emptyNumbers, selectedId, onSelect }: Props) {
  const saved = useMemo(() => historical ? null : loadCanvasState('blackboard', taskId), [taskId, historical]);
  const graph = useMemo(() => deriveGraph(state, agentNumbers), [state, agentNumbers]);
  const latestGraph = useRef(graph); latestGraph.current = graph;
  const topology = graph.nodes.map(node => `${node.type}:${node.id}`).join('|') + ';' + graph.edges.map(edge => edge.id).join('|');
  const flow = useReactFlow<GraphNode, GraphEdge>(), initialized = useNodesInitialized();
  const [nodes, setNodes] = useState<GraphNode[]>([]), [allRelations, setAllRelations] = useState(false), [hovered, setHovered] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(saved?.collapsed ?? true), [saveError, setSaveError] = useState(false), [zoom, setZoom] = useState(saved?.viewport?.zoom ?? 1);
  const [signals, setSignals] = useState<Signal[]>([]), [revision, setRevision] = useState(0), [reduced, setReduced] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches);
  const snapshot = useRef<{ nodes: GraphNode[]; edges: GraphEdge[] }>({ nodes: [], edges: [] });
  const pins = useRef(new Set(saved?.pinned ?? [])), viewport = useRef(saved?.viewport), resetIds = useRef(new Set<string>());
  const userInteracted = useRef(Boolean(saved?.viewport)), viewReady = useRef(false), dragging = useRef(false), eventCursor = useRef<number | null>(null), collapsedRef = useRef(collapsed);
  const persist = useCallback(() => {
    if (historical || !viewReady.current) return;
    setSaveError(!saveCanvasState('blackboard', taskId, { positions: Object.fromEntries(snapshot.current.nodes.map(node => [node.id, node.position])), pinned: [...pins.current], viewport: viewport.current, collapsed: collapsedRef.current }));
  }, [taskId, historical]);
  useEffect(() => { collapsedRef.current = collapsed; persist(); }, [collapsed, persist]);
  useEffect(() => {
    const media = matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => { setReduced(media.matches); if (media.matches) setSignals([]); };
    media.addEventListener('change', update); return () => media.removeEventListener('change', update);
  }, []);
  useEffect(() => {
    const latest = events.at(-1)?.version ?? 0, prior = eventCursor.current; eventCursor.current = latest;
    if (prior === null || historical || reduced || document.hidden || latest <= prior) return;
    const ids = new Set(graph.nodes.map(node => node.id)), added: Signal[] = [];
    for (let index = events.length - 1; index >= 0 && events[index].version > prior && added.length < 16; index--) {
      const event = events[index], body = event.payload ?? {};
      const target = event.type === 'acceptance.judged' ? 'goal' : String(body.id ?? body.intent_id ?? body.fact_id ?? '');
      if (!ids.has(target) || !['fact.posted', 'fact.disputed', 'intent.posted', 'intent.claimed', 'intent.closed', 'acceptance.judged'].includes(event.type)) continue;
      const source = String(body.holder ?? body.author ?? event.actor);
      added.unshift({ id: `event:${event.version}`, source: ids.has(source) && source !== target ? source : undefined, target, kind: event.type, expires: Date.now() + 1900 });
    }
    if (added.length) setSignals(current => [...current.filter(signal => signal.expires > Date.now()), ...added].slice(-16));
  }, [events, historical, reduced, graph.nodes]);
  useEffect(() => {
    if (!signals.length) return;
    const timer = setTimeout(() => setSignals(current => current.filter(signal => signal.expires > Date.now())), Math.max(0, Math.min(...signals.map(signal => signal.expires)) - Date.now()));
    return () => clearTimeout(timer);
  }, [signals]);
  useEffect(() => {
    if (dragging.current) return;
    const graph = latestGraph.current;
    const before = snapshot.current;
    if (!before.nodes.length && saved) before.nodes = graph.nodes.filter(node => saved.positions[node.id]).map(node => ({ ...node, position: saved.positions[node.id] }));
    const released = new Set(resetIds.current);
    const commit = (placed: GraphNode[]) => {
      const current = new Map(snapshot.current.nodes.map(node => [node.id, node.position]));
      const latest = new Map(latestGraph.current.nodes.map(node => [node.id, node]));
      const next = placed.flatMap(node => latest.has(node.id) ? [{ ...latest.get(node.id)!, position: !released.has(node.id) && current.has(node.id) ? current.get(node.id)! : node.position }] : []);
      for (const id of released) resetIds.current.delete(id);
      snapshot.current = { nodes: next, edges: latestGraph.current.edges }; setNodes(next); persist();
    };
    if (!released.size && sameTopology(graph.nodes, graph.edges, before.nodes, before.edges)) { commit(graph.nodes); return; }
    let active = true;
    const timer = setTimeout(() => { void layoutGraph(graph.nodes, graph.edges.filter(edge => edge.data?.relation !== 'authored'), before.nodes.filter(node => !released.has(node.id))).then(next => { if (active) commit(next); }).catch(() => { if (active) commit(graph.nodes); }); }, before.nodes.length ? 120 : 0);
    return () => { active = false; clearTimeout(timer); };
  }, [topology, revision, persist, saved]);
  useEffect(() => {
    const latest = new Map(graph.nodes.map(node => [node.id, node]));
    const current = snapshot.current.nodes.flatMap(node => latest.has(node.id) ? [{ ...latest.get(node.id)!, position: node.position }] : []);
    if (current.length) { snapshot.current.nodes = current; setNodes(current); }
  }, [graph]);
  useEffect(() => {
    if (!initialized || !nodes.length || viewReady.current) return;
    viewReady.current = true;
    if (saved?.viewport) void flow.setViewport(saved.viewport, { duration: 0 });
    else if (!userInteracted.current) void flow.fitView({ padding: .16, minZoom: nodes.length > 16 ? .6 : .15, maxZoom: 1, duration: 0 });
  }, [initialized, nodes, saved, flow]);
  useEffect(() => () => { if (!historical && viewReady.current && snapshot.current.nodes.length) saveCanvasState('blackboard', taskId, { positions: Object.fromEntries(snapshot.current.nodes.map(node => [node.id, node.position])), pinned: [...pins.current], viewport: viewport.current, collapsed: collapsedRef.current }); }, [taskId, historical]);
  const focus = selectedId ?? hovered;
  const related = useMemo(() => new Set(graph.edges.filter(edge => edge.source === focus || edge.target === focus).flatMap(edge => [edge.source, edge.target])), [graph.edges, focus]);
  const shown = useMemo(() => nodes.map(node => ({ ...node, selected: node.id === selectedId, hidden: Boolean(collapsed && node.data.history && !related.has(node.id) && node.id !== selectedId && !signals.some(signal => signal.source === node.id || signal.target === node.id)), data: { ...node.data, pinned: pins.current.has(node.id), related: related.has(node.id), activity: signals.find(signal => signal.target === node.id)?.kind } })), [nodes, selectedId, collapsed, related, signals]);
  const edges = useMemo(() => {
    const hidden = new Set(shown.filter(node => node.hidden).map(node => node.id)), list = [...graph.edges];
    for (const signal of signals) if (signal.source && !list.some(edge => edge.source === signal.source && edge.target === signal.target)) list.push({ id: `activity:${signal.id}`, source: signal.source, target: signal.target, data: { relation: 'authored' } });
    return list.flatMap((edge): GraphEdge[] => {
      if (hidden.has(edge.source) || hidden.has(edge.target)) return [];
      const signal = signals.find(item => item.source === edge.source && item.target === edge.target), relation = edge.data?.relation ?? 'derived_from', emphasized = edge.source === focus || edge.target === focus || Boolean(signal);
      if (!allRelations && !emphasized && relation !== 'disputes' && relation !== 'claim') return [];
      const color = relation === 'disputes' ? '#b94656' : '#659aca';
      return [{ ...edge, type: 'blackboard-connection', markerEnd: { type: MarkerType.ArrowClosed, color, width: 11, height: 11 }, style: { stroke: color, strokeWidth: emphasized ? 1.5 : 1, strokeDasharray: ['based_on', 'retry_of', 'authored'].includes(relation) ? '3 5' : undefined, opacity: focus && !emphasized ? .2 : emphasized ? 1 : .55 }, data: { ...edge.data!, emphasized, dimmed: Boolean(focus && !emphasized), signal, label: relationLabels[relation] } }];
    });
  }, [shown, graph.edges, signals, focus, allRelations]);
  const select = useCallback((id: string) => { userInteracted.current = true; onSelect(id); }, [onSelect]);
  const release = useCallback((id: string) => { pins.current.delete(id); resetIds.current.add(id); setRevision(value => value + 1); }, []);
  const actions = useMemo(() => ({ select, release }), [select, release]), duration = reduced ? 0 : 220;
  const locate = (id: string) => { const node = snapshot.current.nodes.find(item => item.id === id); if (!node) return; userInteracted.current = true; if (node.data.history) onSelect(id); const size = NODE_SIZE[node.type ?? 'fact']; void flow.setCenter(node.position.x + size.width / 2, node.position.y + size.height / 2, { zoom: 1, duration }); };
  const changeNodes = (changes: NodeChange<GraphNode>[]) => setNodes(current => { const next = applyNodeChanges(changes, current); snapshot.current.nodes = next; return next; });
  const historyCount = nodes.filter(node => node.data.history).length;
  return <Actions.Provider value={actions}><div className={styles.canvas} tabIndex={-1} role="region" aria-label="黑板关系图">
    <ReactFlow<GraphNode, GraphEdge> nodes={shown} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} onNodesChange={changeNodes} onNodeClick={(_, node) => select(node.id)} onPaneClick={() => onSelect(null)} onNodeMouseEnter={(_, node) => setHovered(node.id)} onNodeMouseLeave={() => setHovered(null)} onNodeDragStart={(_, node) => { userInteracted.current = true; dragging.current = true; pins.current.add(node.id); }} onNodeDragStop={(_, node) => { dragging.current = false; snapshot.current.nodes = flow.getNodes().map(item => item.id === node.id ? { ...item, position: node.position } : item); setNodes(snapshot.current.nodes); persist(); setRevision(value => value + 1); }} onMoveStart={event => { if (event) userInteracted.current = true; }} onMove={(_, view) => { viewport.current = view; setZoom(view.zoom); }} onMoveEnd={persist} nodesConnectable={false} nodesFocusable={false} minZoom={.15} maxZoom={1.5} proOptions={{ hideAttribution: true }} onlyRenderVisibleElements colorMode="light">
      <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#d9e3ef" /><MiniMap pannable zoomable ariaLabel="黑板图谱缩略图" className={styles.minimap} style={{ width: 116, height: 76, margin: 10 }} onPointerDown={() => { userInteracted.current = true; }} nodeColor={node => typeof node.data.ownerAccent === 'string' ? node.data.ownerAccent : '#97acc8'} />
    </ReactFlow>
    {!nodes.length && <div className={styles.empty} role="status">等待黑板对象进入画布…</div>}
    <div className={styles.canvasActions}><Tooltip title="定位任务目标"><Button type="text" aria-label="定位任务目标" icon={<AimOutlined />} onClick={() => locate('goal')} /></Tooltip><Dropdown trigger={['click']} placement="bottomRight" styles={{ root: { maxHeight: 'min(70vh,560px)', overflowY: 'auto', width: 300, maxWidth: 'calc(100vw - 24px)' } }} menu={{ items: nodes.map(node => ({ key: node.id, label: <span className={styles.locateLabel} title={node.data.label}>{node.id === 'goal' ? '目标' : node.type === 'agent' ? node.data.label : node.id} · {node.data.label}</span> })), onClick: ({ key }) => { onSelect(key); locate(key); } }}><Button type="text" icon={<SearchOutlined />} aria-label="定位节点">定位</Button></Dropdown><Tooltip title={allRelations ? '按需显示关系' : '显示全部关系'}><Button type="text" icon={<ShareAltOutlined />} aria-label={allRelations ? '按需显示关系' : '显示全部关系'} aria-pressed={allRelations} onClick={() => setAllRelations(value => !value)} /></Tooltip><Tooltip title="清除手动位置，重新整理"><Button type="text" icon={<ReloadOutlined />} aria-label="重新整理画布" onClick={() => { pins.current.clear(); for (const node of nodes) resetIds.current.add(node.id); setRevision(value => value + 1); }} /></Tooltip></div>
    <div className={styles.bottomBar}><div className={styles.controls}><Button type="text" aria-label="放大画布" icon={<PlusOutlined />} onClick={() => { userInteracted.current = true; void flow.zoomIn({ duration }); }} /><Button type="text" aria-label="缩小画布" icon={<MinusOutlined />} onClick={() => { userInteracted.current = true; void flow.zoomOut({ duration }); }} /><Button type="text" aria-label="恢复可读视图" onClick={() => { userInteracted.current = true; void flow.zoomTo(1, { duration }); }}>{Math.round(zoom * 100)}%</Button><Button type="text" aria-label="适配全部节点" icon={<ExpandOutlined />} onClick={() => { userInteracted.current = true; void flow.fitView({ padding: .15, duration }); }} /></div>{historyCount > 0 && <Button icon={<HistoryOutlined />} onClick={() => setCollapsed(value => !value)}>{collapsed ? `历史 Agent ${historyCount}` : '收起历史 Agent'}</Button>}<span className={styles.saveStatus} role="status">{saveError ? '浏览器未能保存位置' : !historical && pins.current.size ? `已保存 ${pins.current.size} 个手动位置` : historical ? '历史布局不覆盖实时位置' : ''}</span></div>
    <div className={styles.legend} aria-label="连线说明"><span><i />依据 / 推导</span><span><i className={styles.disputeLine} />争议</span><span>选中查看关联</span></div>
  </div></Actions.Provider>;
}
export default function TopologyFlowCanvas(props: Props) { return <ReactFlowProvider key={`${props.taskId}:${props.historical ? 'history' : 'live'}`}><GraphCanvas {...props} /></ReactFlowProvider>; }
