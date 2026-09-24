import type { BoardEvent, BoardEventType } from '../board/types';

export type StreamConnection = 'connecting' | 'open' | 'reconnecting' | 'closed';

export const BOARD_EVENT_TYPES = [
  'task.created',
  'task.provisioning',
  'task.running',
  'task.closing',
  'task.finished',
  'task.failed',
  'task.stopped',
  'task.archived',
  'fact.posted',
  'fact.disputed',
  'fact.undisputed',
  'intent.posted',
  'intent.claimed',
  'intent.released',
  'intent.closed',
  'agent.spawned',
  'agent.progress',
  'agent.finished',
  'agent.conclude_requested',
  'derive.result',
  'acceptance.judged',
  'acceptance.reverted',
  'task.report',
  'budget.updated',
  'tool_call.recorded',
] as const satisfies readonly BoardEventType[];

export interface StreamHandlers {
  onEvent: (event: BoardEvent) => void;
  onConnection: (status: StreamConnection) => void;
  onError?: (error: Error) => void;
}

export function openBoardStream(
  taskId: string,
  since: number,
  handlers: StreamHandlers,
  createSource: (url: string) => EventSource = (url) =>
    new EventSource(url, { withCredentials: true }),
): EventSource {
  handlers.onConnection('connecting');
  const url = `/api/tasks/${encodeURIComponent(taskId)}/stream?since=${since}`;
  const source = createSource(url);
  source.addEventListener('open', () => handlers.onConnection('open'));
  source.addEventListener('error', () => {
    if (source.readyState === 2) {
      handlers.onConnection('closed');
      handlers.onError?.(new Error('实时连接已关闭，请重新连接或登录。'));
    } else {
      handlers.onConnection('reconnecting');
    }
  });
  for (const type of BOARD_EVENT_TYPES) {
    source.addEventListener(type, (raw) => {
      try {
        const event = JSON.parse((raw as MessageEvent<string>).data) as BoardEvent;
        if (event.task_id !== taskId || !Number.isInteger(event.version) || event.version < 1) {
          throw new Error('Invalid board event');
        }
        handlers.onEvent(event);
      } catch {
        handlers.onError?.(new Error('Could not read board event'));
      }
    });
  }
  return source;
}
