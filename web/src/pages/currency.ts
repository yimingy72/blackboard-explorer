import { createContext } from 'react';

/** A task keeps the currency of its pinned profile, including historical USD tasks. */
export const TaskCurrency = createContext<string | null>(null);
