import { useEffect } from 'react';
import { create } from 'zustand';

import { api } from '../api/client';
import { openBoardStream, type StreamConnection } from '../api/events';
import { emptyBoard, orderedEvents, reduceBoard } from './reducer';
import type { BoardEvent, BoardState } from './types';

interface BoardStore {
  taskId: string | null;
  events: BoardEvent[];
  state: BoardState;
  loading: boolean;
  error: Error | null;
  connection: StreamConnection;
  reset: (taskId: string | null) => void;
  append: (taskId: string, events: BoardEvent[]) => void;
  setLoading: (taskId: string, loading: boolean) => void;
  setError: (taskId: string, error: Error) => void;
  setConnection: (taskId: string, connection: StreamConnection) => void;
}

export const useBoardStore = create<BoardStore>((set) => ({
  taskId: null,
  events: [],
  state: emptyBoard(),
  loading: false,
  error: null,
  connection: 'closed',
  reset: (taskId) =>
    set({
      taskId,
      events: [],
      state: emptyBoard(),
      loading: Boolean(taskId),
      error: null,
      connection: taskId ? 'connecting' : 'closed',
    }),
  append: (taskId, incoming) =>
    set((current) => {
      if (current.taskId !== taskId) return current;
      const events = orderedEvents([
        ...current.events,
        ...incoming.filter((event) => event.task_id === taskId),
      ]);
      if (events.length === current.events.length) return current;
      return { events, state: reduceBoard(events) };
    }),
  setLoading: (taskId, loading) =>
    set((current) => (current.taskId === taskId ? { loading } : current)),
  setError: (taskId, error) =>
    set((current) =>
      current.taskId === taskId ? { error, loading: false, connection: 'closed' } : current,
    ),
  setConnection: (taskId, connection) =>
    set((current) => (current.taskId === taskId ? { connection } : current)),
}));

export function useBoard(taskId: string | undefined): {
  state: BoardState;
  events: BoardEvent[];
  loading: boolean;
  error: Error | null;
  connection: StreamConnection;
} {
  const state = useBoardStore((board) => board.state);
  const events = useBoardStore((board) => board.events);
  const loading = useBoardStore((board) => board.loading);
  const error = useBoardStore((board) => board.error);
  const connection = useBoardStore((board) => board.connection);

  useEffect(() => {
    const id = taskId?.trim();
    const store = useBoardStore.getState();
    store.reset(id || null);
    if (!id) return;

    let disposed = false;
    let source: EventSource | null = null;
    api.getEvents(id, 0)
      .then((history) => {
        if (disposed) return;
        useBoardStore.getState().append(id, history);
        const current = useBoardStore.getState().events;
        const since = current.at(-1)?.version ?? 0;
        source = openBoardStream(id, since, {
          onEvent: (event) => useBoardStore.getState().append(id, [event]),
          onConnection: (status) => useBoardStore.getState().setConnection(id, status),
          onError: (streamError) => useBoardStore.getState().setError(id, streamError),
        });
        useBoardStore.getState().setLoading(id, false);
      })
      .catch((cause: unknown) => {
        if (!disposed) {
          useBoardStore.getState().setError(id, cause instanceof Error ? cause : new Error(String(cause)));
        }
      });

    return () => {
      disposed = true;
      source?.close();
      useBoardStore.getState().reset(null);
    };
  }, [taskId]);

  return { state, events, loading, error, connection };
}
