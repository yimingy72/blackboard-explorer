const taskStatus: Record<string, string> = {
  created: '待启动',
  provisioning: '准备中',
  running: '运行中',
  closing: '收尾中',
  finished: '已完成',
  failed: '失败',
  stopped: '已停止',
};

export function taskStatusLabel(status: string): string {
  return taskStatus[status] ?? status;
}

export function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '—' : new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }).format(date);
}

export function formatCost(value: unknown): string {
  const amount = Number(value ?? 0);
  if (amount > 0 && amount < 0.001) return '<0.001';
  return Number.isFinite(amount) ? amount.toFixed(amount < 1 ? 3 : 2) : '—';
}

export function formatMoney(value: unknown, currency?: string | null): string {
  return `${currency === 'CNY' ? '¥' : currency === 'USD' ? 'US$' : currency ? `${currency} ` : '币种未配置 '}${formatCost(value)}`;
}

export function taskTitle(task: { name?: string | null; goal?: string | null }): string {
  return task.name?.trim() || task.goal?.trim().replace(/\s+/g, ' ').slice(0, 100) || '探索任务';
}
