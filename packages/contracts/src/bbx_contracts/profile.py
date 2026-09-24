"""Load a versionable Agent profile from a directory."""

from pathlib import Path

import yaml

from .models import AgentProfile


def load_profile(directory: Path) -> tuple[AgentProfile, list[str]]:
    models = yaml.safe_load((directory / "models.yaml").read_text(encoding="utf-8"))
    params = yaml.safe_load((directory / "params.yaml").read_text(encoding="utf-8"))
    profile_data = yaml.safe_load((directory / "profile.yaml").read_text(encoding="utf-8"))
    profile = AgentProfile.model_validate({"models": models, "params": params, **profile_data})
    for template in profile.prompts.model_dump().values():
        if not (directory / template).is_file():
            raise ValueError(f"Missing prompt template: {template}")
    notices = [
        f"{task_type} price table is incomplete; fill it from the official DeepSeek pricing page"
        for task_type in ("explore", "derive", "close")
        if not getattr(profile.models, task_type).price.is_complete()
    ]
    return profile, notices
