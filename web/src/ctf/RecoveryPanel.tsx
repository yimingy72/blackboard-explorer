import { Button, Collapse, Input, Modal } from 'antd';
import { useRef, useState, type FormEvent } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { CtfChallenge, CtfState } from './types';
import { formatMoney } from '../pages/format';
import { ctfApi } from './api';
import { useAction } from './useAction';
import { recoveryBlocker } from './view';
import Markdown from './Markdown';
import controls from '../styles/controls.module.css';
import styles from './CtfWorkbench.module.css';
export default function RecoveryPanel({ taskId, task, challenges, onClose }: {
    taskId: string;
    task: CtfState['task'];
    challenges: CtfChallenge[];
    onClose: () => void;
}) {
    const queryClient = useQueryClient();
    const [resumeOpen, setResumeOpen] = useState(false);
    const pending = useRef<{
        signature: string;
        id: string;
    } | null>(null);
    const [cost, setCost] = useState('0');
    const [minutes, setMinutes] = useState('0');
    const action = useAction();
    const blocker = recoveryBlocker(task);
    const conclusion = task.ctf_conclusion;
    const failure = conclusion?.failure && typeof conclusion.failure === 'object' ? conclusion.failure as Record<string, unknown> : null;
    const cleanup = task.ctf_cleanup;
    const cleanupLabels = { archiving: '正在保存归档', destroying: '归档已保存，正在清理本机环境', failed: '归档或清理失败，需重试', complete: '本机清理已完成' };
    const reason = String(conclusion?.end_reason ?? '');
    const reasonLabels: Record<string, string> = { goal_claimed: 'Lead 声明完成', user_stop: '用户停止', budget_exhausted: '预算已用尽', system_failure: '系统执行失败', partial: '部分完成' };
    const targets = challenges.filter((challenge) => Object.keys(challenge.target ?? {}).length > 0);
    const runs = task.runs?.length ? task.runs : [{ run_number: task.run_number ?? 1, report_uri: task.report_uri, workspace_uri: task.workspace_uri }];
    async function resume(event: FormEvent) {
        event.preventDefault();
        if (blocker)
            return;
        const payload = { additional_cost: cost.trim(), additional_minutes: Number(minutes), refresh_tools: false };
        const signature = JSON.stringify(payload);
        if (pending.current?.signature !== signature)
            pending.current = { signature, id: crypto.randomUUID() };
        if (await action.run(async (signal) => {
            await ctfApi.resume(taskId, { ...payload, request_id: pending.current!.id }, signal);
            await queryClient.invalidateQueries({ queryKey: ['ctf', taskId] });
            await queryClient.invalidateQueries({ queryKey: ['task-mode', taskId] });
            await queryClient.invalidateQueries({ queryKey: ['tasks'] });
        })) {
            setResumeOpen(false);
            pending.current = null;
        }
    }
    return <section className={styles.conclusion} aria-label="收尾与恢复"><div className={styles.titleRow}><div><span className={styles.eyebrow}>更多</span><h2>收尾与恢复</h2></div><div className={styles.actions}><Button className={controls.button} onClick={onClose} htmlType={"button"}>关闭</Button><Button className={controls.button} onClick={() => setResumeOpen(true)} htmlType={"button"}>续跑任务</Button></div></div>
    <p className={styles.muted}>只读复盘费用：{formatMoney(task.ctf_review_usage?.cost ?? 0, task.cost_currency)} · 独立于执行预算</p><div className={styles.recoveryGrid}><div><h3>本轮结论</h3>{reason && <p>{reasonLabels[reason] ?? reason}</p>}{conclusion ? <><Markdown text={String(conclusion.summary ?? '')}/>{failure && <section className={styles.failureCard} aria-label="失败诊断"><h4>失败诊断</h4><p>{String(failure.summary ?? '系统执行失败，请查看追踪信息。')}</p><dl><div><dt>追踪 ID</dt><dd><code>{String(failure.correlation_id ?? '未记录')}</code></dd></div><div><dt>阶段</dt><dd>{String(failure.phase ?? '未记录')}</dd></div><div><dt>错误类型</dt><dd>{String(failure.error_type ?? '未记录')}</dd></div><div><dt>时间</dt><dd>{String(failure.occurred_at ?? '未记录')}</dd></div></dl></section>}{conclusion.lead_claim === true && <p className={styles.muted}>Lead 的完成声明独立保留，题目验证结果见任务板。</p>}{Array.isArray(conclusion.unresolved_items) && conclusion.unresolved_items.length > 0 && <><h4>未解事项</h4><ul>{conclusion.unresolved_items.map((item, index) => <li key={index}>{String(item)}</li>)}</ul></>}{Array.isArray(conclusion.evidence_refs) && conclusion.evidence_refs.length > 0 && <Collapse size={"small"} ghost items={[{ key: "content", label: <>结论引用</>, children: <><ul>{conclusion.evidence_refs.map((item, index) => <li key={index}><code>{String(item)}</code></li>)}</ul></> }]}/>}</> : <p>结论尚未保存，请查看会话与共享记录。</p>}</div>
      <div><h3>本机归档与清理</h3>{cleanup?.phase && <p role="status">{cleanupLabels[cleanup.phase]}</p>}{cleanup?.error && <p role="alert" className={styles.error}>{cleanup.error}</p>}<p role="status">{task.cleanup_ready ? '本机清理已完成。' : '本机清理尚未完成，暂不能续跑。'}</p>{blocker && <p className={styles.notice}>{blocker}</p>}<ul className={styles.runDownloads}>{runs.map((run) => <li key={run.run_number}><strong>第 {run.run_number} 轮</strong>{run.report_uri ? <a href={`/api/tasks/${encodeURIComponent(taskId)}/report?run=${run.run_number}`} target="_blank" rel="noopener noreferrer">查看报告</a> : <span>报告尚未就绪</span>}{run.workspace_uri ? <a href={`/api/tasks/${encodeURIComponent(taskId)}/workspace?run=${run.run_number}`} download>下载归档</a> : <span>归档尚未就绪</span>}</li>)}</ul><p className={styles.muted}>归档恢复已登记产物。成员会话与移除记录继续保留。</p></div>
      <div><h3>外部目标状态</h3><p className={styles.muted}>外部目标关闭与本机清理分别记录；本机清理完成不代表外部目标已关闭。</p>{targets.length ? <ul>{targets.map((challenge) => <li key={challenge.id}><strong>{challenge.title}</strong> · {challenge.target.test_only ? '模拟平台 · ' : ''}{String(challenge.target.status ?? '未确认')}<p className={styles.muted}>{String(challenge.target.summary ?? '')}</p></li>)}</ul> : <p>没有可信的目标关闭记录；外部目标状态待核实。</p>}</div></div>
    <Modal title="续跑 CTF 任务" open={resumeOpen} onCancel={() => { if (!action.busy) setResumeOpen(false); }} footer={null} closable={!action.busy} mask={{closable:!action.busy}} keyboard={!action.busy}><form onSubmit={(event) => void resume(event)}><p>保留团队与历史，追加预算后由 Lead 先核查连接和待办。已停止及已移除成员不会自动恢复。</p><label className={controls.field}><span>追加金额（{task.cost_currency ?? '任务币种'}）</span><Input className={controls.input} type="number" min="0" step="any" required disabled={action.busy} value={cost} onChange={(event) => setCost(event.target.value)}/></label><label className={controls.field}><span>追加运行分钟</span><Input className={controls.input} type="number" min="0" step="1" required disabled={action.busy} value={minutes} onChange={(event) => setMinutes(event.target.value)}/></label>{blocker && <p role="status" className={styles.notice}>{blocker}</p>}{action.error && <p role="alert" className={styles.error}>{action.error} 可重试本次申请。</p>}<div className={styles.actions}><Button className={controls.button} disabled={action.busy} onClick={() => setResumeOpen(false)} htmlType={"button"}>取消</Button><Button className={`${controls.button} ${controls.primary}`} disabled={action.busy || Boolean(blocker)} htmlType={"submit"} type={"primary"}>{action.busy ? '正在续跑…' : '确认续跑'}</Button></div></form></Modal>
  </section>;
}
