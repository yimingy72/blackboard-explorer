import { BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps } from '@xyflow/react';
import type { GraphEdge } from '../board/graph';
import styles from './TopologyFlowCanvas.module.css';
export default function BlackboardConnection(props: EdgeProps<GraphEdge>) {
  const [path, x, y] = getBezierPath({ sourceX: props.sourceX, sourceY: props.sourceY, sourcePosition: props.sourcePosition, targetX: props.targetX, targetY: props.targetY, targetPosition: props.targetPosition, curvature: .18 });
  const { signal, emphasized, dimmed, label } = props.data ?? {};
  const color = props.style?.stroke ?? '#659aca';
  return <>
    {emphasized && <path d={path} fill="none" stroke={color} strokeWidth={7} opacity={dimmed ? .02 : .065} strokeLinecap="round" />}
    <BaseEdge id={props.id} path={path} markerEnd={props.markerEnd} interactionWidth={12} style={{ ...props.style, strokeLinecap: 'round' }} />
    {signal && <g key={signal.id} data-blackboard-pulse={signal.kind} data-event-id={signal.id} pointerEvents="none" opacity="0" className={styles.pulse}>
      <animate attributeName="opacity" values={dimmed ? '0;.15;.15;0' : '0;1;1;0'} keyTimes="0;.08;.75;1" dur="1.6s" repeatCount="1" />
      <animateMotion path={path} dur="1.6s" keyPoints={signal.reverse ? '1;0' : '0;1'} keyTimes="0;1" calcMode="linear" repeatCount="1" fill="freeze" />
      <circle r={7} fill={color} opacity={.12} /><circle r={3} fill={color} />
    </g>}
    {emphasized && !dimmed && label && <EdgeLabelRenderer><span className={styles.edgeLabel} style={{ transform: `translate(-50%,-50%) translate(${x}px,${y}px)` }}>{label}</span></EdgeLabelRenderer>}
  </>;
}
