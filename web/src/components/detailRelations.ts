import type { BoardFact, BoardIntent, BoardState } from '../board/types';

export function disputeChain(state: BoardState, factId: string): BoardFact[] {
  const seen = new Set([factId]);
  const pending = [factId];
  while (pending.length) {
    const id = pending.pop()!;
    for (const fact of Object.values(state.facts)) {
      if (fact.id === id) continue;
      if (!fact.disputes.includes(id) && !state.facts[id]?.disputes.includes(fact.id)) continue;
      if (!seen.has(fact.id)) { seen.add(fact.id); pending.push(fact.id); }
    }
  }
  return [...seen].map((id) => state.facts[id]).filter((fact): fact is BoardFact => Boolean(fact))
    .sort((a, b) => a.version - b.version);
}

export function retryChain(state: BoardState, intentId: string): BoardIntent[] {
  const seen = new Set([intentId]);
  const pending = [intentId];
  while (pending.length) {
    const id = pending.pop()!;
    for (const intent of Object.values(state.intents)) {
      if (intent.id !== id && (intent.retryOf === id || state.intents[id]?.retryOf === intent.id) && !seen.has(intent.id)) {
        seen.add(intent.id);
        pending.push(intent.id);
      }
    }
  }
  return [...seen].map((id) => state.intents[id]).filter((intent): intent is BoardIntent => Boolean(intent))
    .sort((a, b) => a.version - b.version);
}
