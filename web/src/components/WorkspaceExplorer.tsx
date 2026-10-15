import LoadingState from './LoadingState';
import { Button, Tree } from 'antd';
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type WorkspaceEntry } from '../api/client';
import styles from './WorkbenchDrawer.module.css';
type TreeNode = WorkspaceEntry & {
    children: TreeNode[];
};
function treeOf(entries: WorkspaceEntry[]): TreeNode[] {
    const roots: TreeNode[] = [];
    const paths = new Map<string, TreeNode>();
    for (const entry of entries) {
        const parts = entry.path.split('/');
        for (let index = 0; index < parts.length; index += 1) {
            const path = parts.slice(0, index + 1).join('/');
            if (paths.has(path))
                continue;
            const leaf = index === parts.length - 1;
            const node: TreeNode = { path, kind: leaf ? entry.kind : 'directory', size: leaf ? entry.size : 0, children: [] };
            paths.set(path, node);
            const parent = paths.get(parts.slice(0, index).join('/'));
            (parent ? parent.children : roots).push(node);
        }
    }
    return roots;
}
export default function WorkspaceExplorer({ taskId, available, run = 1 }: {
    taskId: string;
    available: boolean;
    run?: number;
}) {
    const [selection, setSelection] = useState<{
        taskId: string;
        path: string;
    } | null>(null);
    const treeQuery = useQuery({ queryKey: ['workspace-tree', taskId, run], queryFn: () => api.getWorkspaceTree(taskId, run), enabled: available });
    const selectedPath = available && selection?.taskId === taskId && treeQuery.data?.entries.some((entry) => entry.path === selection.path && entry.kind === 'file') ? selection.path : null;
    const fileQuery = useQuery({ queryKey: ['workspace-file', taskId, run, selectedPath], queryFn: () => api.getWorkspaceFile(taskId, selectedPath!, run), enabled: selectedPath !== null });
    const tree = useMemo(() => treeOf(treeQuery.data?.entries ?? []), [treeQuery.data]);
    const nodes = (items:TreeNode[]): import('antd').TreeDataNode[] => items.map(node=>({key:node.path,title:node.path.split('/').at(-1)+(node.kind==='directory'?'/':node.kind==='link'?' · 链接不可预览':''),selectable:node.kind==='file',isLeaf:node.kind!=='directory',children:node.children.length?nodes(node.children):undefined}));
    if (!available)
        return <p className={styles.empty}>当前版本尚无可查看的任务归档。任务结束并完成归档后可查看已登记文件。</p>;
    return <div className={styles.workspaceFiles}>
    <section aria-label="工作区目录" className={styles.fileTree}>
      {treeQuery.isPending ? <LoadingState label="正在读取归档目录…" /> : treeQuery.isError ? <div role="alert"><p>目录读取失败：{treeQuery.error.message}</p><Button onClick={() => void treeQuery.refetch()} htmlType={"button"}>重试</Button></div> : tree.length ? <Tree aria-label="归档文件" treeData={nodes(tree)} selectedKeys={selectedPath?[selectedPath]:[]} defaultExpandedKeys={tree.map(node=>node.path)} onSelect={keys=>{if(keys[0])setSelection({taskId,path:String(keys[0])});}} /> : <p className={styles.empty}>本轮没有已登记文件。</p>}
    </section>
    <section aria-label="文件预览" className={styles.filePreview}>
      {!selectedPath ? <p className={styles.empty}>选择一个文件查看只读内容。</p> : <>
        <h3>{selectedPath}</h3>
        {fileQuery.isPending ? <LoadingState label="正在读取文件…" /> : fileQuery.isError ? <div role="alert"><p>文件读取失败：{fileQuery.error.message}</p><Button onClick={() => void fileQuery.refetch()} htmlType={"button"}>重试</Button></div> : fileQuery.data && <>
          <p className={styles.muted}>{fileQuery.data.size.toLocaleString()} 字节{fileQuery.data.truncated ? ' · 仅显示头尾 200 KiB' : ''}</p>
          {fileQuery.data.binary ? <p className={styles.muted}>这是二进制文件，无法显示文字预览。请下载本轮归档查看。</p> : <pre tabIndex={0}>{fileQuery.data.text || '（空文件）'}</pre>}
        </>}
      </>}
      <Button href={`/api/tasks/${encodeURIComponent(taskId)}/workspace?run=${run}`}>下载本轮归档</Button>
    </section>
  </div>;
}
