import { describe, expect, it } from 'vitest';
import type { BoardState } from '../board/types';
import { disputeChain, retryChain } from './detailRelations';

const fact = (id: string, version: number, disputes: string[] = []) => ({ id, version, disputes });
const intent = (id: string, version: number, retryOf: string | null = null) => ({ id, version, retryOf });

describe('historical detail relations', () => {
  it('walks a full multi-step dispute chain in version order', () => {
    const state = { facts: {
      F1: fact('F1', 1), F2: fact('F2', 2, ['F1']), F3: fact('F3', 3, ['F2']), F4: fact('F4', 4, ['F3']),
      F5: fact('F5', 5),
    } } as unknown as BoardState;
    expect(disputeChain(state, 'F2').map((item) => item.id)).toEqual(['F1', 'F2', 'F3', 'F4']);
  });

  it('uses only the supplied snapshot for retry history', () => {
    const state = { intents: {
      I1: intent('I1', 1), I2: intent('I2', 2, 'I1'), I3: intent('I3', 3, 'I2'),
    } } as unknown as BoardState;
    expect(retryChain(state, 'I2').map((item) => item.id)).toEqual(['I1', 'I2', 'I3']);
    delete state.intents.I3;
    expect(retryChain(state, 'I2').map((item) => item.id)).toEqual(['I1', 'I2']);
  });
});
