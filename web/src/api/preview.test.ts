import { describe, expect, it } from 'vitest';
import { previewResponse } from './client';

const encode = (value: string) => new TextEncoder().encode(value);
function response(chunks: Uint8Array[]) {
  return new Response(new ReadableStream({ start(controller) { for (const chunk of chunks) controller.enqueue(chunk); controller.close(); } }));
}

describe('bounded evidence preview', () => {
  it('keeps complete short UTF-8 content without overlapping its head and tail', async () => {
    const original = '可复核的证据';
    const bytes = encode(original);
    const preview = await previewResponse(response([bytes.slice(0, 4), bytes.slice(4)]));
    expect(preview).toEqual({ text: original, size: bytes.length, truncated: false, binary: false });
  });
  it('retains the first and last bytes while discarding a large middle', async () => {
    const preview = await previewResponse(response([encode('a'.repeat(100)), encode('x'.repeat(5000)), encode('z'.repeat(100))]), 200);
    expect(preview.size).toBe(5200);
    expect(preview.truncated).toBe(true);
    expect(preview.text.startsWith('a'.repeat(100))).toBe(true);
    expect(preview.text.endsWith('z'.repeat(100))).toBe(true);
    expect(preview.text).not.toContain('xxx');
  });
  it('does not render binary payloads as text', async () => {
    expect((await previewResponse(response([new Uint8Array([1, 0, 255])]))).binary).toBe(true);
    const middleNull = response([encode('a'.repeat(100)), new Uint8Array([0]), encode('z'.repeat(100))]);
    expect((await previewResponse(middleNull, 200)).binary).toBe(true);
  });
});
