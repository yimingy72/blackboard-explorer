import { Suspense, useEffect, useState, type ReactNode } from 'react';

export default function DeferredPanel({ active, children }: { active: boolean; children: ReactNode }) {
  const [visited, setVisited] = useState(false);
  useEffect(() => { if (active) setVisited(true); }, [active]);
  // Preserve the mounted dialog after first use so close motion and focus restoration run.
  return active || visited ? <Suspense fallback={null}>{children}</Suspense> : null;
}
