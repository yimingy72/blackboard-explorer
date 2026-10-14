export function taskListUrl(state: unknown): string {
  return state && typeof state === 'object' && 'returnTo' in state && typeof state.returnTo === 'string' && /^\/tasks(?:\?|$)/.test(state.returnTo)
    ? state.returnTo : '/tasks';
}
