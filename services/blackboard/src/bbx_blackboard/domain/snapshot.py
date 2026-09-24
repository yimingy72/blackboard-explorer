"""Compact YAML snapshot of a board state."""

import json
from typing import Any


def snapshot(state: dict[str, Any], max_lines: int) -> str:
    if max_lines < 1:
        raise ValueError("max_lines must be positive")

    facts = state["facts"]
    intents = state["intents"]
    all_items = [("fact", key, value) for key, value in facts.items()] + [
        ("intent", key, value) for key, value in intents.items()
    ]

    def omission(count: int) -> str:
        return f"# omitted {count} item{'s' if count != 1 else ''}"

    if max_lines == 1:
        omitted = " " + omission(len(all_items)) if all_items else ""
        return '{"facts": [], "intents": []}' + omitted + "\n"
    if max_lines == 2:
        omitted = " " + omission(len(all_items)) if all_items else ""
        return "facts: []\nintents: []" + omitted + "\n"

    def recent(item):
        return (item[2]["version"], item[1])

    def short(value: str) -> str:
        return value if len(value) <= 80 else value[:79] + "…"

    def line(item) -> str:
        kind, key, value = item
        if kind == "fact":
            fields = {
                "id": key,
                "kind": value["kind"],
                "status": value["status"],
                "relied_by": value["relied_by"],
                "by": value["author"],
                "s": short(value["statement"]),
            }
        else:
            fields = {
                "id": key,
                "status": value["status"],
                "result": value.get("result"),
                "holder": value.get("holder"),
                "based_on": value["based_on"],
                "s": short(value["statement"]),
            }
        return "  - " + json.dumps(fields, ensure_ascii=False)

    if len(all_items) + 2 <= max_lines:
        selected = all_items
    else:
        active = sorted(
            (
                item
                for item in all_items
                if item[0] == "intent" and item[2]["status"] in {"open", "claimed"}
            ),
            key=recent,
            reverse=True,
        )
        needed_facts = {fid for _, _, intent in active for fid in intent["based_on"]}
        based_on = sorted(
            (item for item in all_items if item[0] == "fact" and item[1] in needed_facts),
            key=recent,
            reverse=True,
        )
        used = {(kind, key) for kind, key, _ in active + based_on}
        others = sorted(
            (item for item in all_items if (item[0], item[1]) not in used),
            key=recent,
            reverse=True,
        )[:50]
        selected = (active + based_on + others)[: max_lines - 3]

    chosen = {(kind, key) for kind, key, _ in selected}
    lines = ["facts:" if any(kind == "fact" for kind, _ in chosen) else "facts: []"]
    lines.extend(
        line(item) for item in all_items if item[0] == "fact" and (item[0], item[1]) in chosen
    )
    lines.append("intents:" if any(kind == "intent" for kind, _ in chosen) else "intents: []")
    lines.extend(
        line(item) for item in all_items if item[0] == "intent" and (item[0], item[1]) in chosen
    )
    omitted = len(all_items) - len(selected)
    if omitted:
        lines.append(omission(omitted))
    return "\n".join(lines) + "\n"
