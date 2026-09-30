import type { ProviderSpec } from '../api/client';

export function reasoningOptions(provider: ProviderSpec | undefined, model: string): string[] {
  if (!provider?.supports_reasoning_effort) return [];
  if (provider.id === 'deepseek' || model.toLowerCase().includes('deepseek')) return ['low', 'high', 'max'];
  return provider.reasoning_efforts ?? ['minimal', 'low', 'medium', 'high', 'xhigh'];
}
