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
  return Number.isFinite(amount) ? amount.toFixed(amount < 1 ? 3 : 2) : '—';
}
