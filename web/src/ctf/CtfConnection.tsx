import { BaseEdge, EdgeLabelRenderer, type EdgeProps } from '@xyflow/react';
import type { CtfGraphEdge } from './graph';
import styles from './CtfBoardCanvas.module.css';

export default function CtfConnection({ id, data, style }: EdgeProps<CtfGraphEdge>) {
  if (!data?.from || !data.to) return null;
  const { from, to, signal, emphasized, dimmed } = data;
  const dx = to.x - from.x, dy = to.y - from.y, distance = Math.max(1, Math.hypot(dx, dy));
  const bend = Math.min(20, distance * .07) * (dy < 0 ? -1 : 1);
  const mid = { x: (from.x + to.x) / 2 - dy / distance * bend, y: (from.y + to.y) / 2 + dx / distance * bend };
  const path = `M ${from.x} ${from.y} Q ${mid.x} ${mid.y} ${to.x} ${to.y}`;
  const color = signal?.kind === 'accepted' ? '#2e875e' : '#4288cc';
  const label = data.relation === 'claim' ? '主责' : data.relation === 'collaborate' ? '协作' : signal?.kind === 'dispatch' ? '分配' : signal?.kind === 'progress' ? '进展' : '';
  return <>
    {emphasized && <path d={path} fill="none" stroke={color} strokeWidth={7} opacity={dimmed ? .02 : .065} strokeLinecap="round"/>}
    <BaseEdge id={id} path={path} interactionWidth={12} style={{ ...style, strokeLinecap: 'round' }}/>
    {emphasized && <><circle cx={from.x} cy={from.y} r={2} fill={color}/><circle cx={to.x} cy={to.y} r={2} fill="#fff" stroke={color}/></>}
    {signal && <g key={signal.id} data-ctf-pulse={signal.kind} data-event-id={signal.id} pointerEvents="none" opacity="0" className={styles.pulse}>
      <animate attributeName="opacity" values={dimmed ? '0;.15;.15;0' : '0;1;1;0'} keyTimes="0;.08;.75;1" dur="1.6s" repeatCount="1"/>
      <animateMotion path={path} dur="1.6s" keyPoints={signal.reverse ? '1;0' : '0;1'} keyTimes="0;1" calcMode="linear" repeatCount="1" fill="freeze"/>
      <circle r={7} fill={color} opacity={.12}/><circle r={3} fill={color}/>
    </g>}
    {emphasized && !dimmed && label && <EdgeLabelRenderer><span className={styles.edgeLabel} style={{ transform: `translate(-50%,-50%) translate(${mid.x}px,${mid.y}px)` }}>{label}</span></EdgeLabelRenderer>}
  </>;
}
