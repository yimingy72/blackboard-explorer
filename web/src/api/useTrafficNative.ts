import { useCallback, useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { trafficApi } from './traffic';
import { ApiError } from './client';

export function useTrafficNative(taskId: string, run: number, agentId: string, expression: string, enabled: boolean, live: boolean) {
  const seen = useRef<string | null>(null);
  const created = useQuery({
    queryKey: ['traffic-native-open', taskId, run, agentId, expression],
    queryFn: ({ signal }) => trafficApi.openNativeQuery(taskId, run, agentId, expression, signal),
    enabled, retry: false, staleTime: 30000, gcTime: 60000, refetchOnWindowFocus: false,
  });
  const progress = useQuery({
    queryKey: ['traffic-native-query', taskId, created.data?.id],
    queryFn: ({ signal }) => trafficApi.nativeQuery(taskId, created.data!.id, seen.current, signal),
    enabled: enabled && Boolean(created.data?.id), retry: false, gcTime: 60000, refetchOnWindowFocus: false,
    refetchInterval: query => query.state.data?.status === 'building' ? 1000 : live && !query.state.error ? 4000 : false,
  });
  const data = progress.data ?? created.data;
  const queryId = created.data?.id;
  const { refetch: reopen } = created;
  const { refetch: refreshProgress } = progress;
  const expired = progress.error instanceof ApiError && progress.error.status === 404;
  useEffect(() => { if (enabled && expired) void reopen(); }, [enabled, expired, reopen]);
  useEffect(() => { seen.current = data?.revision ?? null; }, [data?.revision]);
  const refresh = useCallback(() => queryId && !expired ? refreshProgress() : reopen(), [queryId, expired, refreshProgress, reopen]);
  return { data, error: expired ? created.error : progress.error ?? created.error, refresh };
}
