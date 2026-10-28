import { AimOutlined, CheckCircleOutlined, ClockCircleOutlined, CloseCircleOutlined, ExclamationCircleOutlined, ExpandOutlined, FileTextOutlined, MinusOutlined, PlusOutlined, ReloadOutlined, TeamOutlined, WarningOutlined } from '@ant-design/icons';
import { Button } from 'antd';
import { Background, BackgroundVariant, Handle, MiniMap, Position, ReactFlow, ReactFlowProvider, applyNodeChanges, useNodesInitialized, useReactFlow, type Edge, type Node, type NodeChange, type NodeProps } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { loadCanvasState, saveCanvasState } from '../components/canvasState';
import { defaultExpanded, layoutProgressTree, progressViewportForNode, progressTree, PROGRESS_NODE_SIZE, visibleProgressNodes, type Point } from './layout';
import type { ProgressNode } from './types';
import styles from './ProgressCanvas.module.css';

type Props = { taskId: string; nodes: ProgressNode[]; selectedId: string | null; onSelect: (id: string | null) => void; historical?: boolean };
type TreeNode = Node<{ progress: ProgressNode; childCount: number; expanded: boolean; changed: boolean }, 'progress'>;
const Actions = createContext<{ select: (id: string) => void; toggle: (id: string) => void }>({ select: () => undefined, toggle: () => undefined });
const kinds = { goal: '任务目标', direction: '探索方向', finding: '阶段发现', attempt: '关键尝试', gap: '待解问题' };
const states = {
  exploring: { label: '探索中', icon: ClockCircleOutlined, tone: 'blue' },
  supported: { label: '已有依据', icon: CheckCircleOutlined, tone: 'green' },
  needs_evidence: { label: '待补证', icon: ExclamationCircleOutlined, tone: 'amber' },
  excluded: { label: '已排除', icon: CloseCircleOutlined, tone: 'gray' },
  sources_changed: { label: '依据有变化', icon: WarningOutlined, tone: 'amber' },
  achieved: { label: '已达成', icon: CheckCircleOutlined, tone: 'green' },
};
export function ProgressStatus({ node }: { node: ProgressNode }) {
  const value = node.effective_state === 'achieved' && node.kind !== 'goal' ? 'supported' : node.effective_state ?? 'exploring';
  const state = states[value], StatusIcon = state.icon;
  return <span className={`${styles.status} ${styles[state.tone]}`}><StatusIcon />{state.label}</span>;
}
function ProgressTreeNode({ id, data, selected }: NodeProps<TreeNode>) {
  const { progress: node } = data, actions = useContext(Actions);
  return <div className={`${styles.node} ${node.kind === 'goal' ? styles.root : ''} ${node.kind === 'direction' ? styles.direction : ''} ${node.effective_state === 'excluded' ? styles.excluded : ''} ${selected ? styles.selected : ''} ${data.changed ? styles.changed : ''}`}>
    {node.parent_id && <Handle type="target" position={Position.Left} isConnectable={false} className={styles.handle} />}
    <button type="button" className={styles.nodeBody} aria-label={`${kinds[node.kind]}：${node.title}。${node.summary || node.question}`} aria-pressed={Boolean(selected)} onClick={event => { if (event.detail === 0) { event.stopPropagation(); actions.select(id); } }}>
      <span className={styles.nodeTop}><span className={styles.kind}>{node.kind === 'goal' && <AimOutlined />}{kinds[node.kind]}</span><ProgressStatus node={node} /></span>
      <strong className={styles.title} title={node.title}>{node.title}</strong>
      <span className={styles.summary} title={node.summary || node.question}>{node.summary || node.question}</span>
      <span className={styles.nodeMeta}><span><FileTextOutlined />{node.source_refs.length} 条依据</span><span><TeamOutlined />{node.contributors?.length ? `${node.contributors.length} 位参与者` : '参与者待归纳'}</span></span>
    </button>
    {data.childCount > 0 && <><Handle type="source" position={Position.Right} isConnectable={false} className={styles.handle} /><Button size="small" shape="circle" className={`${styles.collapse} nodrag nopan`} aria-label={`${data.expanded ? '收起' : '展开'}「${node.title}」的 ${data.childCount} 个子节点`} aria-expanded={data.expanded} icon={data.expanded ? <MinusOutlined /> : <PlusOutlined />} onClick={event => { event.stopPropagation(); actions.toggle(id); }} /><span className={styles.childCount}>{!data.expanded && data.childCount}</span></>}
  </div>;
}
const nodeTypes = { progress: ProgressTreeNode };
function Canvas({ taskId, nodes: source, selectedId, onSelect, historical = false }: Props) {
  const saved = useMemo(() => historical ? null : loadCanvasState('progress-tree', taskId), [taskId, historical]);
  const flow = useReactFlow<TreeNode, Edge>(), initialized = useNodesInitialized();
  const [expanded, setExpanded] = useState(() => new Set(saved?.expanded ?? defaultExpanded(source)));
  const [nodes, setNodes] = useState<TreeNode[]>([]), [zoom, setZoom] = useState(saved?.viewport?.zoom ?? 1), [saveError, setSaveError] = useState(false);
  const [changed, setChanged] = useState(new Set<string>());
  const [reduced, setReduced] = useState(() => typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches);
  const positions = useRef<Record<string, Point>>(saved?.positions ?? {}), pins = useRef(new Set(saved?.pinned ?? []));
  const viewport = useRef(saved?.viewport), ready = useRef(false), expansion = useRef(expanded), hadNodes = useRef(Boolean(source.length));
  const container = useRef<HTMLDivElement>(null);
  const signatures = useRef(new Map(source.map(node => [node.id, JSON.stringify(node)])));
  expansion.current = expanded;
  const persist = useCallback(() => {
    if (historical || !ready.current) return;
    setSaveError(!saveCanvasState('progress-tree', taskId, { positions: positions.current, pinned: [...pins.current], viewport: viewport.current, collapsed: true, expanded: [...expansion.current] }));
  }, [taskId, historical]);
  useEffect(() => {
    const media = matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setReduced(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  useEffect(() => {
    if ((!hadNodes.current || (signatures.current.size === 1 && source.length > 1)) && source.length && !saved?.expanded?.length) setExpanded(defaultExpanded(source));
    hadNodes.current = Boolean(source.length);
    const next = new Map(source.map(node => [node.id, JSON.stringify(node)]));
    const updated = [...next].filter(([id, value]) => signatures.current.get(id) !== value).map(([id]) => id);
    const hadPrevious = signatures.current.size > 0;
    signatures.current = next;
    if (historical || reduced || document.hidden || !hadPrevious || !updated.length) { setChanged(new Set()); return; }
    setChanged(new Set(updated));
    const timer = setTimeout(() => setChanged(new Set()), 1100);
    return () => clearTimeout(timer);
  }, [source, historical, reduced, saved]);
  const visible = useMemo(() => visibleProgressNodes(source, expanded), [source, expanded]);
  const children = useMemo(() => progressTree(source).children, [source]);
  useEffect(() => {
    const liveIds = new Set(source.map(node => node.id));
    positions.current = Object.fromEntries(Object.entries(positions.current).filter(([id]) => liveIds.has(id)));
    positions.current = { ...positions.current, ...layoutProgressTree(visible, positions.current) };
    setNodes(visible.map(node => ({ id: node.id, type: 'progress', position: positions.current[node.id] ?? { x: 0, y: 0 }, ...PROGRESS_NODE_SIZE, data: { progress: node, childCount: children.get(node.id)?.length ?? 0, expanded: expanded.has(node.id), changed: changed.has(node.id) } })));
    persist();
  }, [visible, children, expanded, changed, source, persist]);
  useEffect(() => {
    if (!initialized || !nodes.length || ready.current) return;
    ready.current = true;
    if (saved?.viewport) void flow.setViewport(saved.viewport, { duration: 0 });
    else void flow.fitView({ padding: .13, minZoom: .55, maxZoom: 1, duration: 0 });
  }, [initialized, nodes.length, saved, flow]);
  useEffect(() => {
    const element = container.current;
    if (!element || !selectedId) return;
    let frame = 0;
    const reveal = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (matchMedia('(max-width:899px)').matches) return;
        const point = positions.current[selectedId];
        if (!point) return;
        const size = element.getBoundingClientRect(), current = flow.getViewport();
        const next = progressViewportForNode(point, current, size);
        if (next !== current) void flow.setViewport(next, { duration: reduced ? 0 : 160 });
      });
    };
    const observer = new ResizeObserver(reveal);
    observer.observe(element);
    reveal();
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [selectedId, flow, reduced]);
  useEffect(() => () => {
    if (!historical && ready.current) saveCanvasState('progress-tree', taskId, { positions: positions.current, pinned: [...pins.current], viewport: viewport.current, collapsed: true, expanded: [...expansion.current] });
  }, [taskId, historical]);
  const toggle = useCallback((id: string) => setExpanded(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; }), []);
  const actions = useMemo(() => ({ select: (id: string) => onSelect(id), toggle }), [onSelect, toggle]);
  const shown = useMemo(() => nodes.map(node => ({ ...node, selected: node.id === selectedId })), [nodes, selectedId]);
  const edges = useMemo(() => {
    const ids = new Set(visible.map(node => node.id));
    return visible.flatMap(node => node.parent_id && ids.has(node.parent_id) ? [{ id: `${node.parent_id}:${node.id}`, source: node.parent_id, target: node.id, type: 'smoothstep', style: { stroke: selectedId === node.id || selectedId === node.parent_id ? '#477fd2' : '#b8c9dd', strokeWidth: selectedId === node.id || selectedId === node.parent_id ? 1.7 : 1.25, opacity: selectedId && selectedId !== node.id && selectedId !== node.parent_id ? .4 : 1 } }] : []);
  }, [visible, selectedId]);
  const changeNodes = useCallback((changes: NodeChange<TreeNode>[]) => {
    setNodes(current => applyNodeChanges(changes, current));
    for (const change of changes) if (change.type === 'position' && change.position) positions.current[change.id] = change.position;
  }, []);
  const duration = reduced ? 0 : 200;
  return <Actions.Provider value={actions}><div ref={container} className={styles.canvas} role="region" aria-label="成果树画布">
    <ReactFlow<TreeNode, Edge> nodes={shown} edges={edges} nodeTypes={nodeTypes} onNodesChange={changeNodes} onNodeClick={(_, node) => onSelect(node.id)} onPaneClick={() => onSelect(null)} onNodeDragStop={(_, node) => { positions.current[node.id] = node.position; pins.current.add(node.id); persist(); }} onMove={(_, next) => { viewport.current = next; setZoom(next.zoom); }} onMoveEnd={persist} nodesConnectable={false} nodesFocusable={false} edgesFocusable={false} deleteKeyCode={null} minZoom={.15} maxZoom={1.5} onlyRenderVisibleElements proOptions={{ hideAttribution: true }} colorMode="light">
      <Background variant={BackgroundVariant.Dots} gap={24} size={.8} color="#cfd9e6" />
      <MiniMap className={styles.minimap} style={{ width: 112, height: 76, margin: 18 }} ariaLabel="成果树缩略图" pannable zoomable nodeColor={node => (node as TreeNode).data.progress.kind === 'goal' ? '#5885c7' : '#dae5f5'} />
    </ReactFlow>
    {!source.length && <div className={styles.empty} role="status">等待第一条可整理的黑板记录</div>}
    {!!source.length && <div className={styles.context}>目标 → 探索方向 → 发现、尝试与缺口<span>{visible.length} / {source.filter(node => !node.archived).length} 个节点</span></div>}
    <div className={styles.note}>拖动节点整理位置<span>点击节点查看依据与关键过程</span></div>
    <div className={styles.controls}>
      <Button type="text" className={styles.control} aria-label="放大成果树" icon={<PlusOutlined />} onClick={() => { void flow.zoomIn({ duration }); }} />
      <Button type="text" className={styles.control} aria-label="缩小成果树" icon={<MinusOutlined />} onClick={() => { void flow.zoomOut({ duration }); }} />
      <Button type="text" className={`${styles.control} ${styles.zoom}`} aria-label="恢复原始缩放" onClick={() => { void flow.zoomTo(1, { duration }); }}>{Math.round(zoom * 100)}%</Button>
      <Button type="text" className={styles.control} aria-label="适配全部可见节点" icon={<ExpandOutlined />} onClick={() => { void flow.fitView({ padding: .13, duration }); }} />
      <Button type="text" className={styles.control} aria-label="重新整理成果树位置" icon={<ReloadOutlined />} onClick={() => { positions.current = {}; pins.current.clear(); const next = layoutProgressTree(visible); positions.current = next; setNodes(current => current.map(node => ({ ...node, position: next[node.id] ?? node.position }))); persist(); }} />
    </div>
    <span className={styles.saveStatus} role="status">{saveError ? '浏览器未能保存位置' : historical ? '历史布局' : pins.current.size ? '手动位置已保存' : ''}</span>
  </div></Actions.Provider>;
}
export default function ProgressCanvas(props: Props) {
  return <ReactFlowProvider key={`${props.taskId}:${props.historical ? 'history' : 'live'}`}><Canvas {...props} /></ReactFlowProvider>;
}
