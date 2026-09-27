/** Presentation-only layout for legacy prose; code and stored source stay intact. */
type Node = { type: string; value?: string; children?: Node[]; ordered?: boolean; start?: number; spread?: boolean };

function paragraph(children: Node[]): Node { return { type: 'paragraph', children }; }

function readableParagraph(node: Node): Node[] {
  const parts: Array<{ ordinal?: number; children: Node[] }> = [{ children: [] }];
  for (const child of node.children ?? []) {
    if (child.type !== 'text' || !child.value) { parts.at(-1)!.children.push(child); continue; }
    let offset = 0;
    for (const match of child.value.matchAll(/[（(]([1-9]\d?)[）)]\s*/g)) {
      const at = match.index;
      if (at > 0 && !/[\s：:；;。]/.test(child.value[at - 1])) continue;
      parts.at(-1)!.children.push({ type: 'text', value: child.value.slice(offset, at) });
      parts.push({ ordinal: Number(match[1]), children: [] });
      offset = at + match[0].length;
    }
    parts.at(-1)!.children.push({ type: 'text', value: child.value.slice(offset) });
  }
  const numbered = parts.slice(1);
  if (numbered.length >= 2 && numbered.every((part, index) => part.ordinal === index + 1)) {
    const prefix = parts[0].children;
    return [
      ...(prefix.some((item) => item.type !== 'text' || item.value?.trim()) ? [paragraph(prefix)] : []),
      { type: 'list', ordered: true, start: 1, spread: true, children: numbered.map((part) => ({ type: 'listItem', spread: false, children: [paragraph(part.children)] })) },
    ];
  }
  // Break only long prose at sentence boundaries, never inside inline code/links.
  if ((node.children ?? []).reduce((length, child) => length + (child.value?.length ?? 0), 0) < 320) return [node];
  const groups: Node[][] = [[]];
  let length = 0;
  for (const child of node.children ?? []) {
    if (child.type !== 'text' || !child.value) { groups.at(-1)!.push(child); continue; }
    for (const sentence of child.value.split(/(?<=[。！？])/u)) {
      if (!sentence) continue;
      groups.at(-1)!.push({ type: 'text', value: sentence });
      length += sentence.length;
      if (length >= 120 && /[。！？]$/.test(sentence)) { groups.push([]); length = 0; }
    }
  }
  return groups.filter((group) => group.length).map(paragraph);
}

export function readableProse() {
  return (tree: Node) => {
    function visit(node: Node) {
      if (!node.children || ['code', 'inlineCode', 'list', 'link'].includes(node.type)) return;
      node.children = node.children.flatMap((child) => {
        if (child.type === 'paragraph') return readableParagraph(child);
        visit(child);
        return [child];
      });
    }
    visit(tree);
  };
}
