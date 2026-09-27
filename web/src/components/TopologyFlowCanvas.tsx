import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  useReactFlow,
  type NodeChange,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { deriveGraph, type GraphEdge, type GraphNode } from '../board/graph'
import type { BoardState } from '../board/types'
import { layoutGraph, sameTopology } from './layout'
import styles from './TopologyFlowCanvas.module.css'

type CanvasProps = {
  state: BoardState
  agentNumbers?: Record<string, number>
  selectedId?: string | null
  onSelect: (id: string | null) => void
}

const SelectContext = createContext<(id: string) => void>(() => undefined)

function NodeShell({
  id,
  selected,
  className,
  label,
  children,
}: {
  id: string
  selected: boolean
  className: string
  label: string
  children: React.ReactNode
}) {
  const onSelect = useContext(SelectContext)
  return (
    <div className={`${styles.node} ${className} ${selected ? styles.selected : ''}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} className={styles.handle} />
      <button
        className={`${styles.nodeButton} nodrag`}
        type="button"
        aria-label={label}
        onClick={(event) => {
          event.stopPropagation()
          onSelect(id)
        }}
      >
        {children}
      </button>
      <Handle type="source" position={Position.Right} isConnectable={false} className={styles.handle} />
    </div>
  )
}

const factKinds: Record<string, string> = {
  observation: '观察',
  inference: '推断',
  structure: '结构',
}

const intentStatuses: Record<string, string> = {
  open: '待认领',
  claimed: '调查中',
  closed: '已关闭',
}

const intentResults: Record<string, string> = {
  confirmed: '已确认',
  rejected: '已否定',
  inconclusive: '未定论',
}

function GoalNode({ id, data, selected }: NodeProps<GraphNode>) {
  const acceptance = data.acceptance ?? []
  return (
    <NodeShell id={id} selected={selected} className={styles.goal} label={`目标：${data.label}`}>
      <div className={styles.nodeTop}><span className={styles.nodeType}>任务目标</span><span className={styles.nodeId}>GOAL</span></div>
      <p className={styles.goalText} title={data.label}>{data.label}</p>
      <div className={styles.acceptance} aria-label="验收条件状态">
        {acceptance.map((item) => (
          <span key={item.id} className={`${styles.acceptanceBadge} ${item.status === 'met' ? styles.met : styles.unmet}`}>
            {item.id} · {item.status === 'met' ? '已满足' : '未满足'}
          </span>
        ))}
      </div>
    </NodeShell>
  )
}

function FactNode({ id, data, selected }: NodeProps<GraphNode>) {
  const disputed = data.status === 'disputed'
  const selfReported = data.provenance === 'self_reported'
  return (
    <NodeShell
      id={id}
      selected={selected}
      className={`${styles.fact} ${data.kind === 'inference' ? styles.inference : ''} ${data.kind === 'structure' ? styles.structure : ''} ${disputed ? styles.disputed : ''} ${selfReported ? styles.selfReported : ''}`}
      label={`事实 ${id}，${disputed ? '有争议' : '已提出'}：${data.label}`}
    >
      <div className={styles.nodeTop}>
        <span className={styles.nodeType}>{factKinds[data.kind ?? ''] ?? '事实'}</span>
        {data.ownerLabel && <span className={styles.author}>{data.ownerLabel}</span>}
        <span className={styles.nodeId}>{id}</span>
      </div>
      <p className={styles.statement} title={data.label}>{data.label}</p>
      <div className={styles.nodeMeta}>
        <span>{disputed ? '有争议' : '已提出'} · {selfReported ? '自述来源' : '工具记录'}</span>
        {(data.reliedBy ?? 0) > 0 && <span>被引用 {data.reliedBy}</span>}
      </div>
    </NodeShell>
  )
}

function IntentNode({ id, data, selected }: NodeProps<GraphNode>) {
  const status = data.status ?? 'open'
  return (
    <NodeShell
      id={id}
      selected={selected}
      className={`${styles.intent} ${status === 'open' ? styles.intentOpen : ''} ${status === 'claimed' ? styles.intentClaimed : ''} ${status === 'closed' ? styles.intentClosed : ''} ${status === 'closed' && data.result === 'confirmed' ? styles.confirmed : ''} ${status === 'closed' && data.result === 'rejected' ? styles.rejected : ''}`}
      label={`意图 ${id}，${intentStatuses[status] ?? status}：${data.label}`}
    >
      <div className={styles.nodeTop}>
        <span className={styles.nodeType}>调查意图</span>
        {data.ownerLabel && <span className={styles.author}>{data.ownerLabel}</span>}
        <span className={styles.nodeId}>{id}</span>
      </div>
      <p className={styles.statement} title={data.label}>{data.label}</p>
      <div className={styles.nodeMeta}>
        <span>{intentStatuses[status] ?? status}{data.result ? ` · ${intentResults[data.result] ?? data.result}` : ''}</span>
        <span>{data.holder ? `由 ${data.holder} 持有` : data.attempts ? `尝试 ${data.attempts} 次` : ''}</span>
      </div>
    </NodeShell>
  )
}

function AgentNode({ id, data, selected }: NodeProps<GraphNode>) {
  const concluding = data.status === 'concluding'
  return (
    <NodeShell
      id={id}
      selected={selected}
      className={`${styles.agent} ${concluding ? styles.concluding : styles.running}`}
      label={`${data.label}，${concluding ? '收尾中' : '运行中'}，已执行 ${data.steps ?? 0} 步`}
    >
      <span className={styles.agentDot} aria-hidden="true" />
      <span className={styles.agentText}>
        <strong>{data.label}</strong>
        <small>{data.isSeed ? '种子探索' : data.taskType ?? '探索'} · {concluding ? '收尾中' : '运行中'}</small>
      </span>
    </NodeShell>
  )
}

const nodeTypes = { goal: GoalNode, fact: FactNode, intent: IntentNode, agent: AgentNode }

const edgeStyle: Record<string, { color: string; dash?: string }> = {
  derived_from: { color: 'var(--color-muted)' },
  based_on: { color: 'var(--color-muted)', dash: '6 5' },
  resolves: { color: 'var(--color-success)' },
  disputes: { color: 'var(--color-danger)' },
  retry_of: { color: 'var(--color-subtle)', dash: '6 5' },
  claim: { color: 'var(--color-primary)', dash: '2 5' },
}

const edgeLabels: Record<string, string> = {
  derived_from: '推导自',
  based_on: '依据',
  resolves: '解决',
  disputes: '争议',
  retry_of: '重新尝试',
  claim: '认领',
}

const ariaLabelConfig = {
  'controls.zoomIn.ariaLabel': '放大画布',
  'controls.zoomOut.ariaLabel': '缩小画布',
  'controls.fitView.ariaLabel': '适配全部节点',
}

function styleEdges(edges: GraphEdge[], state: BoardState): GraphEdge[] {
  return edges.map((edge) => {
    const relation = edge.data?.relation ?? 'derived_from'
    const style = edgeStyle[relation]
    const result = relation === 'resolves' ? state.facts[edge.target]?.result : null
    const color = result === 'rejected' ? 'var(--color-danger)' : result === 'inconclusive' ? 'var(--color-muted)' : style.color
    return {
      ...edge,
      type: 'smoothstep',
      style: { stroke: color, strokeWidth: relation === 'disputes' ? 2 : 1.5, strokeDasharray: style.dash },
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 12, height: 12 },
      ariaLabel: `${edgeLabels[relation]}：${edge.source} → ${edge.target}`,
    }
  })
}

function GraphCanvas({ state, agentNumbers = {}, selectedId, onSelect }: CanvasProps) {
  const { fitView } = useReactFlow<GraphNode, GraphEdge>()
  const graph = useMemo(() => deriveGraph(state, agentNumbers), [state, agentNumbers])
  const [nodes, setNodes] = useState<GraphNode[]>([])
  const [edges, setEdges] = useState<GraphEdge[]>([])
  const [layoutRevision, setLayoutRevision] = useState(0)
  const snapshot = useRef<{ nodes: GraphNode[]; edges: GraphEdge[] }>({ nodes: [], edges: [] })
  const userInteracted = useRef(false)
  const markUserInteraction = () => { userInteracted.current = true }
  const selectNode = (id: string | null) => {
    markUserInteraction()
    onSelect(id)
  }

  useEffect(() => {
    const previous = snapshot.current
    if (sameTopology(graph.nodes, graph.edges, previous.nodes, previous.edges)) {
      const positions = new Map(previous.nodes.map((node) => [node.id, node.position]))
      const next = graph.nodes.map((node) => ({ ...node, position: positions.get(node.id) ?? node.position }))
      snapshot.current = { nodes: next, edges: graph.edges }
      setNodes(next)
      setEdges(styleEdges(graph.edges, state))
      return
    }

    let active = true
    const timer = window.setTimeout(() => {
      void layoutGraph(graph.nodes, graph.edges, previous.nodes, previous.edges)
        .then((next) => {
          if (!active) return
          snapshot.current = { nodes: next, edges: graph.edges }
          setNodes(next)
          setEdges(styleEdges(graph.edges, state))
          setLayoutRevision((revision) => revision + 1)
        })
        .catch(() => {
          if (!active) return
          snapshot.current = graph
          setNodes(graph.nodes)
          setEdges(styleEdges(graph.edges, state))
          setLayoutRevision((revision) => revision + 1)
        })
    }, 300)
    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [graph, state])

  useEffect(() => {
    if (!nodes.length || !layoutRevision || userInteracted.current) return
    const frame = window.requestAnimationFrame(() => {
      if (userInteracted.current) return
      void fitView({ padding: 0.18, maxZoom: 1, duration: 0 })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [fitView, layoutRevision, nodes.length])

  const shownNodes = useMemo(
    () => nodes.map((node) => ({ ...node, selected: node.id === selectedId })),
    [nodes, selectedId],
  )

  const onNodesChange = (changes: NodeChange<GraphNode>[]) => {
    setNodes((current) => {
      const next = applyNodeChanges(changes, current)
      snapshot.current.nodes = next
      return next
    })
  }

  return (
    <SelectContext.Provider value={selectNode}>
      <div className={styles.canvas} role="region" aria-label="黑板关系图">
        <ReactFlow<GraphNode, GraphEdge>
          nodes={shownNodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onNodeClick={(_, node) => selectNode(node.id)}
          onPaneClick={() => selectNode(null)}
          onNodeDragStart={markUserInteraction}
          onMoveStart={(event) => { if (event) markUserInteraction() }}
          nodesConnectable={false}
          minZoom={0.18}
          maxZoom={1.8}
          proOptions={{ hideAttribution: true }}
          colorMode="light"
          ariaLabelConfig={ariaLabelConfig}
        >
          <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--color-line)" />
          <Controls
            showInteractive={false}
            className={styles.controls}
            aria-label="画布缩放控制"
            onZoomIn={markUserInteraction}
            onZoomOut={markUserInteraction}
            onFitView={markUserInteraction}
          />
          <MiniMap
            pannable
            zoomable
            ariaLabel="黑板图谱缩略图"
            style={{ width: 128, height: 84, margin: 10 }}
            className={styles.minimap}
            onPointerDown={markUserInteraction}
            onWheel={markUserInteraction}
            nodeColor={(node) => (typeof node.data.ownerAccent === 'string' ? node.data.ownerAccent : undefined) ?? (node.type === 'goal' ? 'var(--color-primary)' : 'var(--color-line-strong)')}
          />
        </ReactFlow>
        {!nodes.length && <div className={styles.empty}>等待黑板对象进入画布…</div>}
      </div>
    </SelectContext.Provider>
  )
}

export default function TopologyFlowCanvas(props: CanvasProps) {
  return <ReactFlowProvider><GraphCanvas {...props} /></ReactFlowProvider>
}
