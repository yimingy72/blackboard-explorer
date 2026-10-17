import { useEffect, useState } from 'react';
import { activeDuration } from '../board/agents';

// Keep the ticking clock out of the workbench and graph render trees.
export default function ElapsedTime({ seconds, since, until }: {
  seconds: number; since?: string | null; until?: string | null;
}) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!since || until) return;
    let timer: ReturnType<typeof setInterval> | undefined;
    const update = () => {
      clearInterval(timer);
      if (document.hidden) return;
      setNow(Date.now());
      timer = setInterval(() => setNow(Date.now()), 1000);
    };
    update();
    document.addEventListener('visibilitychange', update);
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', update); };
  }, [since, until]);
  if (!Number.isFinite(seconds) || seconds < 0 || since && !Number.isFinite(Date.parse(since))) return <>—</>;
  return <>{activeDuration(seconds, since, until ? Math.min(Date.parse(until), Date.now()) : now)}</>;
}
