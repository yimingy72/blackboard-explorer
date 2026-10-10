import { useEffect, useRef, useState } from 'react';

export function useAction() {
  const controller = useRef(new AbortController());
  const lock = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    controller.current = new AbortController();
    return () => controller.current.abort();
  }, []);
  async function run(action: (signal: AbortSignal) => Promise<unknown>): Promise<boolean> {
    if (lock.current) return false;
    lock.current = true; setBusy(true); setError('');
    const signal = controller.current.signal;
    try { await action(signal); return !signal.aborted; }
    catch (cause) { if (!signal.aborted) setError(cause instanceof Error ? cause.message : '操作失败，请重试。'); return false; }
    finally { lock.current = false; if (!signal.aborted) setBusy(false); }
  }
  return { busy, error, run };
}
