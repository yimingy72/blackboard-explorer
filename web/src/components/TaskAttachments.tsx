import { Button, Upload as AntUpload } from 'antd';
import Icon from './Icon';
import { useEffect, useRef, useState } from 'react';
import { api, ApiError, type InitialAttachment } from '../api/client';
import controls from '../styles/controls.module.css';
import styles from '../pages/NewTaskPage.module.css';
type Upload = {
    key: string;
    file: File;
    status: 'queued' | 'uploading' | 'ready' | 'error' | 'deleting';
    attachment?: InitialAttachment;
    error?: string;
};
const LIMIT = 50 * 1024 * 1024;
const sizeLabel = (size: number) => size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;
export default function TaskAttachments({ ensureGroup, resetGroup, groupExpired, disabled, onStatus }: {
    ensureGroup: () => Promise<string>;
    resetGroup: () => void;
    groupExpired: boolean;
    disabled: boolean;
    onStatus: (busy: boolean, invalid: boolean, ids: string[]) => void;
}) {
    const [uploads, setUploads] = useState<Upload[]>([]);
    const [error, setError] = useState('');
    const [expired, setExpired] = useState(false);
    const entries = useRef<Upload[]>([]);
    const alive = useRef(true);
    const processing = useRef(false);
    const controller = useRef<AbortController | null>(null);
    const busy = uploads.some((file) => ['queued', 'uploading', 'deleting'].includes(file.status));
    useEffect(() => { onStatus(busy, expired || groupExpired || uploads.some((file) => file.status === 'error'), uploads.flatMap((file) => file.status === 'ready' && file.attachment ? [file.attachment.id] : [])); }, [busy, uploads, expired, groupExpired, onStatus]);
    useEffect(() => { alive.current = true; return () => { alive.current = false; controller.current?.abort(); }; }, []);
    function publish(next: Upload[]) { entries.current = next; if (alive.current)
        setUploads(next); }
    function update(key: string, patch: Partial<Upload>) { publish(entries.current.map((file) => file.key === key ? { ...file, ...patch } : file)); }
    async function processQueue() {
        if (processing.current)
            return;
        processing.current = true;
        try {
            while (alive.current) {
                const next = entries.current.find((file) => file.status === 'queued');
                if (!next)
                    break;
                update(next.key, { status: 'uploading', error: undefined });
                let group: string | undefined;
                try {
                    group = await ensureGroup();
                    if (!alive.current)
                        break;
                    // A prior lost response can leave a registered file. Reconcile the
                    // group's ready list before retrying the same selected file.
                    const known = new Set(entries.current.flatMap((file) => file.attachment ? [file.attachment.id] : []));
                    const snapshot = await api.getInputGroup(group);
                    const candidates = snapshot.files.filter((file) => !known.has(file.id) && file.filename === next.file.name && file.size === next.file.size);
                    let recovered: InitialAttachment | undefined;
                    if (candidates.length) {
                        const bytes = await crypto.subtle.digest('SHA-256', await next.file.arrayBuffer());
                        const sha = Array.from(new Uint8Array(bytes), (value) => value.toString(16).padStart(2, '0')).join('');
                        recovered = candidates.find((file) => file.sha256 === sha);
                    }
                    if (!alive.current)
                        break;
                    controller.current = new AbortController();
                    const attachment = recovered ?? await api.uploadInput(group, next.file, controller.current.signal);
                    update(next.key, { status: 'ready', attachment });
                }
                catch (cause) {
                    if (alive.current) {
                        const message = cause instanceof Error ? cause.message : '上传失败';
                        if (cause instanceof ApiError && [404, 410].includes(cause.status)) {
                            setExpired(true);
                            publish(entries.current.map((file) => ['queued', 'uploading'].includes(file.status) ? { ...file, status: 'error', error: message } : file));
                            break;
                        }
                        update(next.key, { status: 'error', error: message });
                    }
                }
                finally {
                    controller.current = null;
                }
            }
        }
        finally {
            processing.current = false;
        }
    }
    function choose(files: FileList | File[] | null) {
        if (!files)
            return;
        setError('');
        const next = [...entries.current];
        for (const file of Array.from(files)) {
            if (file.size > LIMIT || next.length >= 20 || next.reduce((total, item) => total + item.file.size, 0) + file.size > 200 * 1024 * 1024) {
                setError('每个文件最多 50 MB，最多 20 个文件，总量 200 MB。');
                break;
            }
            next.push({ key: crypto.randomUUID(), file, status: 'queued' });
        }
        publish(next);
        void processQueue();
    }
    async function remove(item: Upload) {
        if (!item.attachment) {
            publish(entries.current.filter((file) => file.key !== item.key));
            return;
        }
        update(item.key, { status: 'deleting', error: undefined });
        try {
            await api.deleteInput(await ensureGroup(), item.attachment.id);
            publish(entries.current.filter((file) => file.key !== item.key));
        }
        catch (cause) {
            if (cause instanceof ApiError && [404, 410].includes(cause.status))
                setExpired(true);
            update(item.key, { status: 'error', error: cause instanceof Error ? cause.message : '移除失败' });
        }
    }
    function restartUploads() {
        resetGroup();
        setExpired(false);
        setError('');
        publish(entries.current.map(({ key, file }) => ({ key, file, status: 'queued' })));
        void processQueue();
    }
    return <div>
    <div className={styles.attachmentHeading}><h2>初始附件</h2><AntUpload multiple fileList={[]} showUploadList={false} disabled={disabled || busy || expired || groupExpired} beforeUpload={(file, files) => { if (file === files[0]) choose(files); return false; }}><Button icon={<Icon name="plus" />} disabled={disabled || busy || expired || groupExpired}>添加初始附件</Button></AntUpload></div>
    {(expired || groupExpired) && <p className={controls.error} role="alert">附件已过期。<Button className={controls.button} disabled={disabled || busy} onClick={restartUploads} htmlType={"button"}>重新上传附件</Button></p>}
    {error && <p className={controls.error} role="alert">{error}</p>}
    {uploads.length ? <ul className={styles.uploads}>{uploads.map((item) => <li key={item.key}>
      <div className={styles.fileInfo}><strong title={item.file.name}>{item.file.name}</strong><small>{sizeLabel(item.file.size)} · {item.status === 'ready' ? '已上传' : item.status === 'error' ? '失败' : item.status === 'deleting' ? '正在移除…' : '上传中…'}</small>{item.error && <span className={controls.error} role="alert">{item.error}</span>}</div>
      <div className={styles.fileActions}>{item.status === 'error' && !item.attachment && <Button className={`${controls.button} ${controls.quiet}`} disabled={disabled || busy} onClick={() => { update(item.key, { status: 'queued' }); void processQueue(); }} htmlType={"button"} type={"text"}>重试上传</Button>}<Button className={`${controls.button} ${controls.quiet}`} disabled={disabled || item.status === 'uploading' || item.status === 'deleting'} aria-label={`移除附件 ${item.file.name}`} onClick={() => void remove(item)} htmlType={"button"} type={"text"}>移除</Button></div>
    </li>)}</ul> : <p className={styles.attachmentEmpty}>可添加文档、图片、脚本或数据文件。</p>}
  </div>;
}
