import { queryOptions } from '@tanstack/react-query';
import { api } from './client';

export const tasksQueryOptions = () => queryOptions({
  queryKey: ['tasks'], queryFn: ({ signal }) => api.listTasks(signal, 3), staleTime: 5000,
});
export const taskQueryOptions = (id: string) => queryOptions({
  queryKey: ['task', id], queryFn: ({ signal }) => api.getTask(id, signal), staleTime: 5000,
});
