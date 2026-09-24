import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type WorkspaceEntry } from '../api/client';
import controls from '../styles/controls.module.css';
import styles from './WorkbenchDrawer.module.css';

type TreeNode = WorkspaceEntry & { children: TreeNode[] };
function treeOf(entries: WorkspaceEntry[]): TreeNode[] {
  const roots: TreeNode[] = [];
  const paths = new Map<string, TreeNode>();
  for (const entry of entries) {
    const parts = entry.path.split('/');
    for (let index = 0; index < parts.length; index += 1) {
      const path = parts.slice(0, index + 1).join('/');
      if (paths.has(path)) continue;
      const leaf = index === parts.length - 1;
      const node: TreeNode = { path, kind: leaf ? entry.kind : 'directory', size: leaf ? entry.size : 0, children: [] };
      paths.set(path, node);
      const parent = paths.get(parts.slice(0, index).join('/'));
      (parent ? parent.children : roots).push(node);
    }
  }
  return roots;
}

export default function WorkspaceExplorer({ taskId, available }: { taskId: string; available: boolean }) {
  const [selection, setSelection] = useState<{ taskId: string; path: string } | null>(null);
  const treeQuery = useQuery({ queryKey: ['workspace-tree', taskId], queryFn: () => api.getWorkspaceTree(taskId), enabled: available });
  const selectedPath = available && selection?.taskId === taskId && treeQuery.data?.entries.some((entry) => entry.path === selection.path && entry.kind === 'file') ? selection.path : null;
  const fileQuery = useQuery({ queryKey: ['workspace-file', taskId, selectedPath], queryFn: () => api.getWorkspaceFile(taskId, selectedPath!), enabled: selectedPath !== null });
  const tree = useMemo(() => treeOf(treeQuery.data?.entries ?? []), [treeQuery.data]);
  function render(nodes: TreeNode[]) {
    return <ul className={styles.tree}>{nodes.map((node) => <li key={node.path}>
      {node.kind === 'directory' ? <details><summary>{node.path.split('/').at(-1)}/</summary>{render(node.children)}</details> : node.kind === 'link' ?
        <span className={styles.treeLink} title={node.path}>{node.path.split('/').at(-1)} · 链接不可预览</span> :
        <button type="button" aria-pressed={selectedPath === node.path} onClick={() => setSelection({ taskId, path: node.path })} title={node.path}>{node.path.split('/').at(-1)}</button>}
    </li>)}</ul>;
  }
  if (!available) return <p className={styles.empty}>当前版本尚未登记工作区归档。任务结束并完成归档后可查看。</p>;
  return <div className={styles.workspaceFiles}>
    <section aria-label="工作区目录" className={styles.fileTree}>
      {treeQuery.isPending ? <p role="status">正在读取归档目录…</p> : treeQuery.isError ? <div role="alert"><p>目录读取失败：{treeQuery.error.message}</p><button type="button" onClick={() => void treeQuery.refetch()}>重试</button></div> : tree.length ? render(tree) : <p className={styles.empty}>归档为空。</p>}
    </section>
    <section aria-label="文件预览" className={styles.filePreview}>
      {!selectedPath ? <p className={styles.empty}>选择一个文件查看只读内容。</p> : <>
        <h3>{selectedPath}</h3>
        {fileQuery.isPending ? <p role="status">正在读取文件…</p> : fileQuery.isError ? <div role="alert"><p>文件读取失败：{fileQuery.error.message}</p><button type="button" onClick={() => void fileQuery.refetch()}>重试</button></div> : fileQuery.data && <>
          <p className={styles.muted}>{fileQuery.data.size.toLocaleString()} 字节{fileQuery.data.truncated ? ' · 仅显示头尾 200 KiB' : ''}</p>
          {fileQuery.data.binary ? <p className={styles.muted}>这是二进制文件，无法显示文字预览。请下载完整归档查看。</p> : <pre tabIndex={0}>{fileQuery.data.text || '（空文件）'}</pre>}
        </>}
      </>}
      <a className={controls.button} href={`/api/tasks/${encodeURIComponent(taskId)}/workspace`}>下载完整归档</a>
    </section>
  </div>;
}
