"""Summarize reviewed evaluation runs without hiding incomplete evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean

HIGHER = {
    "recall",
    "precision",
    "reproducibility",
    "interface_coverage",
    "cache_hit_rate",
    "satisfies_hit_rate",
}
LOWER = {"elapsed_seconds", "cost"}
LABELS = {
    "recall": "召回率",
    "precision": "精确率",
    "reproducibility": "已选证据复现率",
    "interface_coverage": "接口覆盖",
    "cache_hit_rate": "缓存命中率",
    "satisfies_hit_rate": "satisfies 命中率",
    "elapsed_seconds": "耗时（秒）",
    "cost": "账本金额",
}


def summarize(runs: list[dict]) -> dict:
    summary = {}
    for metric in sorted(HIGHER | LOWER):
        values = [
            run.get("metrics", {}).get(metric)
            for run in runs
            if type(run.get("metrics", {}).get(metric)) in {int, float}
        ]
        summary[metric] = {
            "n": len(values),
            "mean": mean(values) if values else None,
            "worst": (min(values) if metric in HIGHER else max(values)) if values else None,
        }
    return summary


def render(runs: list[dict]) -> str:
    groups = {
        name: [run for run in runs if run.get("profile") == name] for name in ("default", "single")
    }
    stats = {name: summarize(rows) for name, rows in groups.items()}
    lines = [
        "# 多 Agent 与单 Agent 评估对比",
        "",
        "金额来自固定 Profile 价格表的账本估算。缺失值不当作零；每项单独列出可用样本数。",
        "",
        "| 指标 | 多 Agent 平均 / 最差 / n | 单 Agent 平均 / 最差 / n |",
        "|---|---|---|",
    ]
    for key in sorted(HIGHER | LOWER):
        cells = []
        for name in groups:
            item = stats[name][key]
            cells.append(
                "未测 / 未测 / 0"
                if not item["n"]
                else f"{item['mean']:.4f} / {item['worst']:.4f} / {item['n']}"
            )
        lines.append(f"| {LABELS[key]} | {' | '.join(cells)} |")
    pending = sum(bool(run.get("provisional") or run.get("review_pending")) for run in runs)
    lines += [
        "",
        f"运行数量：多 Agent {len(groups['default'])}，单 Agent {len(groups['single'])}。"
        f"其中 {pending} 次仍有待复核结果。",
        "",
    ]
    complete = all(len(rows) >= 5 for rows in groups.values()) and not pending
    recalls = [stats[name]["recall"]["mean"] for name in groups]
    complete = complete and all(stats[name]["recall"]["n"] == len(groups[name]) for name in groups)
    if not complete or any(value is None for value in recalls):
        lines.append("结论：样本或人工复核尚未完成，不能判断多 Agent 是否达到设计目标。")
    elif recalls[0] - recalls[1] >= 1 / 6 - 1e-9:
        lines.append(
            "结论：多 Agent 的平均召回率至少提高 1/6，相当于平均多找到一个问题，"
            "达到召回率判断标准。"
        )
    elif abs(recalls[0] - recalls[1]) < 1e-9:
        times = [stats[name]["elapsed_seconds"]["mean"] for name in groups]
        if all(value is not None and value > 0 for value in times) and times[0] < times[1]:
            lines.append(
                f"结论：平均召回率持平，多 Agent 耗时减少 {(1 - times[0] / times[1]) * 100:.1f}%；"
                "设计未定义“明显更短”的数值门槛，需结合最差耗时人工判断。"
            )
        else:
            lines.append("结论：平均召回率持平，当前耗时数据未证明速度优势。")
    else:
        lines.append(
            "结论：当前平均召回提升不足一个问题，未达到召回率判断标准。先检查遗漏接口、裁定质量和重复劳动，不据此直接改动提示词。"
        )
    lines += [
        "",
        "## 过程指标",
        "",
        "| Profile / 任务 | 争议 | 裁定 | Agent 结束原因 |",
        "|---|---:|---:|---|",
    ]
    for row in runs:
        metric = row.get("metrics", {})
        name = f"{row.get('profile', '?')} / {row.get('task_id', '?')}".replace("|", "\\|").replace(
            "\n", " "
        )
        reasons = json.dumps(metric.get("end_reasons", {}), ensure_ascii=False).replace("|", "\\|")
        lines.append(
            f"| {name} | {metric.get('disputes', '—')} | {metric.get('judgments', '—')} | "
            f"{reasons} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成评估对比报告，区分缺失与未复核结果")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    runs = [json.loads(path.read_text()) for path in sorted(args.directory.rglob("score.json"))]
    if not runs:
        parser.error("No score.json files; run eval.runner.score first")
    path = args.directory / "comparison.md"
    path.write_text(render(runs))
    print(path)


if __name__ == "__main__":
    main()
