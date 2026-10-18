import { useCallback, useEffect } from 'react';
import { create } from 'zustand';

import { api } from '../api/client';
import { openBoardStream, type StreamConnection } from '../api/events';
import { advanceBoard, emptyBoard, orderedEvents, reduceBoard } from './reducer';
import { boardFromSnapshot, type BoardSnapshot } from './snapshot';
import type { BoardEvent, BoardState } from './types';

interface BoardStore {
  taskId: string | null;
  generation: number;
  baseState: BoardState;
  events: BoardEvent[];
  state: BoardState;
  loading: boolean;
  error: Error | null;
  connection: StreamConnection;
  historyLoaded: boolean;
  historyLoading: boolean;
  reset: (taskId: string | null) => void;
  hydrate: (taskId: string, state: BoardState) => void;
  append: (taskId: string, events: BoardEvent[]) => void;
  ensureHistory: (taskId: string) => Promise<void>;
  setLoading: (taskId: string, loading: boolean) => void;
  setError: (taskId: string, error: Error) => void;
  setConnection: (taskId: string, connection: StreamConnection) => void;
}

let historyRequest: AbortController | null = null;

function hasVersion(events: readonly BoardEvent[], version: number): boolean {
  let lower = 0, upper = events.length;
  while (lower < upper) {
    const middle = (lower + upper) >>> 1;
    if (events[middle].version < version) lower = middle + 1;
    else upper = middle;
  }
  return events[lower]?.version === version;
}

export const useBoardStore = create<BoardStore>((set) => ({
  taskId: null,
  generation: 0,
  baseState: emptyBoard(),
  events: [],
  state: emptyBoard(),
  loading: false,
  error: null,
  connection: 'closed',
  historyLoaded: false,
  historyLoading: false,
  reset: (taskId) => {
    historyRequest?.abort();
    historyRequest = null;
    set(current => ({
      taskId,
      generation: current.generation + 1,
      baseState: emptyBoard(),
      events: [],
      state: emptyBoard(),
      loading: Boolean(taskId),
      error: null,
      connection: taskId ? 'connecting' : 'closed',
      historyLoaded: false,
      historyLoading: false,
    }));
  },
  hydrate: (taskId, state) =>
    set({
      taskId,
      baseState: state,
      events: [],
      state,
      loading: false,
      error: null,
      connection: 'connecting',
      historyLoaded: false,
      historyLoading: false,
    }),
  append: (taskId, incoming) =>
    set((current) => {
      if (current.taskId !== taskId) return current;
      const baseVersion = current.baseState.task?.version ?? 0;
      const batch = orderedEvents(incoming.filter((event) => event.task_id === taskId && event.version > baseVersion));
      const latest = current.events.at(-1)?.version ?? 0;
      if (!batch.length) return current;
      if (batch.every(event => event.version > latest)) {
        return { events: [...current.events, ...batch], state: advanceBoard(current.state, batch) };
      }
      // Reconnects may repeat events or supply a previously missing older version.
      const fresh = batch.filter(event => !hasVersion(current.events, event.version));
      if (!fresh.length) return current;
      const events = orderedEvents([...current.events, ...fresh]);
      return { events, state: advanceBoard(current.baseState, events) };
    }),
  ensureHistory: async (taskId) => {
    const current = useBoardStore.getState();
    if (current.taskId !== taskId || current.historyLoaded || current.historyLoading || historyRequest) return;
    const controller = new AbortController();
    historyRequest = controller;
    useBoardStore.setState({ historyLoading: true });
    try {
      const history = await api.getEvents(taskId, 0, controller.signal);
      useBoardStore.setState((latest) => {
        if (latest.taskId !== taskId || latest.generation !== current.generation) return latest;
        const events = orderedEvents([...history, ...latest.events]);
        return {
          events,
          state: reduceBoard(events),
          baseState: emptyBoard(),
          historyLoaded: true,
          historyLoading: false,
        };
      });
    } catch {
      useBoardStore.setState((latest) =>
        latest.taskId === taskId && latest.generation === current.generation ? { historyLoading: false } : latest,
      );
    } finally {
      if (historyRequest === controller) historyRequest = null;
    }
  },
  setLoading: (taskId, loading) =>
    set((current) => (current.taskId === taskId ? { loading } : current)),
  setError: (taskId, error) =>
    set((current) =>
      current.taskId === taskId ? { error, loading: false, connection: 'closed' } : current,
    ),
  setConnection: (taskId, connection) =>
    set((current) => (current.taskId === taskId ? { connection } : current)),
}));

export function connectBoard(taskId: string | undefined): (() => void) | undefined {
  const id = taskId?.trim();
  useBoardStore.getState().reset(id || null);
  if (!id) return;

  const generation = useBoardStore.getState().generation;
  const controller = new AbortController();
  let disposed = false;
  let source: EventSource | null = null;
  let pending: BoardEvent[] = [];
  let flushTimer: number | null = null;
  const active = () => !disposed && useBoardStore.getState().generation === generation;
  const flush = () => {
    flushTimer = null;
    if (!active() || !pending.length) return;
    const batch = pending;
    pending = [];
    useBoardStore.getState().append(id, batch);
  };
  api.getState<BoardSnapshot>(id, controller.signal)
    .then((snapshot) => {
      if (!active()) return;
      const hydrated = boardFromSnapshot(snapshot);
      useBoardStore.getState().hydrate(id, hydrated.state);
      source = openBoardStream(id, hydrated.version, {
        onEvent: (event) => {
          if (!active()) return;
          pending.push(event);
          if (flushTimer === null) flushTimer = window.setTimeout(flush, 50);
        },
        onConnection: (status) => { if (active()) useBoardStore.getState().setConnection(id, status); },
        onError: (error) => { if (active()) useBoardStore.getState().setError(id, error); },
      });
    })
    .catch((cause: unknown) => {
      if (active()) {
        useBoardStore.getState().setError(id, cause instanceof Error ? cause : new Error(String(cause)));
      }
    });

  return () => {
    disposed = true;
    controller.abort();
    if (flushTimer !== null) window.clearTimeout(flushTimer);
    source?.close();
    if (useBoardStore.getState().generation === generation) useBoardStore.getState().reset(null);
  };
}

export function useBoard(taskId: string | undefined): {
  taskId: string | null;
  state: BoardState;
  events: BoardEvent[];
  loading: boolean;
  error: Error | null;
  connection: StreamConnection;
  historyLoaded: boolean;
  historyLoading: boolean;
  ensureHistory: () => void;
} {
  const activeTaskId = useBoardStore((board) => board.taskId);
  const state = useBoardStore((board) => board.state);
  const events = useBoardStore((board) => board.events);
  const loading = useBoardStore((board) => board.loading);
  const error = useBoardStore((board) => board.error);
  const connection = useBoardStore((board) => board.connection);
  const historyLoaded = useBoardStore((board) => board.historyLoaded);
  const historyLoading = useBoardStore((board) => board.historyLoading);
  const ensureHistory = useCallback(() => {
    if (taskId?.trim()) void useBoardStore.getState().ensureHistory(taskId.trim());
  }, [taskId]);

  useEffect(() => connectBoard(taskId), [taskId]);

  return {
    taskId: activeTaskId,
    state,
    events,
    loading,
    error,
    connection,
    historyLoaded,
    historyLoading,
    ensureHistory,
  };
}
