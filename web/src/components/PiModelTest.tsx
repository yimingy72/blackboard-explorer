import { Alert, Button, Form, Input, Modal, Space } from 'antd';
import { useRef, useState } from 'react';
import { api, ApiError, type PiModel, type PiModelTestView } from '../api/client';
import controls from '../styles/controls.module.css';

export default function PiModelTest({ model, onClose }: { model: PiModel; onClose: () => void }) {
    const [prompt, setPrompt] = useState('');
    const [requestId, setRequestId] = useState<string>();
    const [result, setResult] = useState<PiModelTestView>();
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const inFlight = useRef(false);
    const hasPrompt = prompt.trim().length > 0 && new TextEncoder().encode(prompt).length <= 8192;
    async function run(read: boolean) {
        if (inFlight.current || (read ? !requestId : requestId || !hasPrompt)) return;
        inFlight.current = true;
        const id = requestId ?? crypto.randomUUID();
        setRequestId(id);
        setBusy(true);
        setError('');
        try {
            setResult(read ? await api.readPiModelTest(id) : await api.testPiModel({
                request_id: id, model_id: model.id, model_revision: model.model_revision, prompt,
            }));
        } catch (cause) {
            if (!read && cause instanceof ApiError && [400, 401, 403, 404, 422].includes(cause.status)) {
                setRequestId(undefined);
                setError('演练未登记。请确认模型版本和测试权限后重新发送。');
            } else {
                setError('未收到本次演练的确认。请读取原请求结果。');
            }
        } finally {
            inFlight.current = false;
            setBusy(false);
        }
    }
    return <Modal title={`测试模型：${model.label} · v${model.model_revision}`} open footer={null}
        onCancel={onClose} closable={!busy} keyboard={!busy} maskClosable={false}>
        <p className={controls.hint}>发送一轮文本，由供应商计费。费用独立于任务和复盘。没有费率时仍可测试，费用显示为未知。</p>
        <Form layout="vertical" onFinish={() => void run(false)}>
            <Form.Item label="测试消息" htmlFor="pi-test-prompt" required>
                <Input.TextArea id="pi-test-prompt" value={prompt} autoSize={{ minRows: 3, maxRows: 8 }}
                    maxLength={8192} disabled={busy || Boolean(requestId)} onChange={event => setPrompt(event.target.value)} />
            </Form.Item>
            <Space wrap>
                <Button type="primary" htmlType="submit" loading={busy && !result} disabled={busy || Boolean(requestId) || !hasPrompt}>发送测试</Button>
                {requestId && <Button disabled={busy} onClick={() => void run(true)}>读取原请求结果</Button>}
                {(result?.state === 'complete' || result?.state === 'not_sent') && <Button disabled={busy} onClick={() => {
                    setRequestId(undefined); setResult(undefined); setError('');
                }}>新建测试</Button>}
            </Space>
        </Form>
        {requestId && <p className={controls.hint}>请求编号：<code>{requestId}</code></p>}
        {error && <Alert type="warning" title={error} showIcon />}
        {result && <div role="status" className={controls.hint}>
            <p>{result.state === 'complete' ? '已收到答复' : result.state === 'unknown' ? '结果待核，未重新发送'
                : result.state === 'not_sent' ? '未发送给提供商' : '已登记，等待原请求结果'}</p>
            {result.result_text && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{result.result_text}</p>}
            {result.usage && <p>观察用量：输入 {result.usage.input} · 输出 {result.usage.output} · 缓存读取 {result.usage.cacheRead} · 缓存写入 {result.usage.cacheWrite}</p>}
            {result.state === 'complete' && <p>{result.amount_usd == null ? '费用未知' : `估算费用：USD ${result.amount_usd}`}</p>}
        </div>}
    </Modal>;
}
