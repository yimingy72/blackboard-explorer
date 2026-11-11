import { useState, type ReactNode } from 'react';
import type { ChangeTracking } from './settingsChanges';
import { SettingsGuard } from './SettingsGuard';
import styles from './Settings.module.css';

export function SettingsHeading({ title, description, actions }: { title: string; description: string; actions?: ReactNode }) {
  return <header className={styles.pageHeading}><div><h1>{title}</h1><p>{description}</p></div>{actions && <div className={styles.headingActions}>{actions}</div>}</header>;
}

export function SettingsFrame({ children }: { children: ReactNode }) {
  return <div className={styles.frame}>{children}</div>;
}

export type RailItem = { id: string; title: string; caption: string; icon: ReactNode };
export function SettingsRail({ title, intro, note, items, active, onSelect, disabled }: {
  title: string; intro?: string; note?: ReactNode; items: RailItem[]; active: string; onSelect: (id: string) => void; disabled?: boolean;
}) {
  return <aside className={styles.rail} aria-label={title}><h2>{title}</h2>{intro && <p>{intro}</p>}
    <div className={styles.railList}>{items.map(item => <button type="button" key={item.id} disabled={disabled}
      aria-pressed={item.id === active} className={styles.railChoice} onClick={() => onSelect(item.id)}>
      <span className={styles.railIcon} aria-hidden="true">{item.icon}</span><span><strong>{item.title}</strong><small>{item.caption}</small></span>
      <span className={styles.railArrow} aria-hidden="true">›</span>
    </button>)}</div>{note && <div className={styles.railNote}>{note}</div>}
  </aside>;
}
export function DetailHeading({ icon, title, subtitle, description, actions }: {
  icon: ReactNode; title: string; subtitle?: string; description?: string; actions?: ReactNode;
}) {
  return <header className={styles.detailHeading}><span className={styles.emblem} aria-hidden="true">{icon}</span>
    <div className={styles.detailTitle}><h2>{title}{subtitle && <small>{subtitle}</small>}</h2>{description && <p>{description}</p>}</div>
    {actions && <div className={styles.detailActions}>{actions}</div>}
  </header>;
}
export function CatalogPage({ children }: { children: (tracking: ChangeTracking) => ReactNode }) {
  const [dirty, onDirtyChange] = useState(false), [busy, onBusyChange] = useState(false);
  return <><SettingsGuard dirty={dirty} busy={busy} />{children({ onDirtyChange, onBusyChange })}</>;
}
