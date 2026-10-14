export function toolResultView(head: string): { text: string; failed: boolean } {
  let parsed: unknown;
  try { parsed = JSON.parse(head); } catch { return { text: head, failed: false }; }
  if (!Array.isArray(parsed) || !parsed.every(item => item && typeof item === 'object' && typeof item.type === 'string')) return { text: head, failed: false };
  const failed = parsed.some(item => item.type === 'error' || item.exception || typeof item.additional_properties?.bbx_tool_error === 'string');
  const text = parsed.map(item => {
    const body = item.type === 'text' ? item.text : item.type === 'error' ? item.message : null;
    if (typeof body !== 'string') return item.type === 'image' || item.type === 'data' ? '[图像内容]' : `[${item.type}]`;
    try { return JSON.stringify(JSON.parse(body), null, 2); } catch { return body; }
  }).join('\n');
  return { text, failed };
}
