"""Immutable, bounded initial inputs for the original native workspace."""

import json
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .models import ContractModel
from .runner import Digest

INPUT_PROFILE = "shared-inputs-v1"
INPUT_FILE_LIMIT = 16 * 1024 * 1024
INPUT_TOTAL_LIMIT = 24 * 1024 * 1024
INPUT_FILE_COUNT = 20
UUID_TEXT = r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"


class NativeSharedInputLimits(ContractModel):
    profile: Literal["shared-inputs-v1"] = INPUT_PROFILE
    max_file_bytes: int = INPUT_FILE_LIMIT
    max_total_bytes: int = INPUT_TOTAL_LIMIT
    max_files: int = INPUT_FILE_COUNT


class NativeInputFile(ContractModel):
    id: UUID
    filename: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=0, le=INPUT_FILE_LIMIT, strict=True)
    sha256: Digest

    @model_validator(mode="after")
    def canonical_workspace_path(self):
        if (
            self.filename in {".", ".."}
            or "/" in self.filename
            or "\\" in self.filename
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in self.filename)
            or len(self.filename.encode("utf-8")) > 255
            or self.path != f"/workspace/shared/inputs/{self.id}/{self.filename}"
        ):
            raise ValueError("Initial input must use its exact generated workspace path")
        return self


class NativeInputScope(ContractModel):
    task_id: UUID
    operation_id: UUID
    resource_generation: int = Field(ge=1, strict=True)
    workspace_pvc_uid: str = Field(pattern=UUID_TEXT)
    sandbox_id: str = Field(pattern=UUID_TEXT)
    sandbox_pod_uid: str = Field(pattern=UUID_TEXT)
    sandbox_container_id: str = Field(pattern=r"^containerd://[0-9a-f]{64}$")
    execution_context_id: Digest
    manifest_sha256: Digest


InputState = Literal["disabled", "pending", "claimed", "confirmed", "blocked"]


class NativeInputPlan(ContractModel):
    state: InputState
    scope: NativeInputScope | None = None
    files: list[NativeInputFile] | None = Field(default=None, max_length=INPUT_FILE_COUNT)


class NativeInputClaim(ContractModel):
    scope: NativeInputScope


class NativeInputClaimResult(ContractModel):
    launch_allowed: bool
    state: InputState


class NativeInputReceipt(NativeInputClaim):
    result: Literal[
        "materialized",
        "integrity_mismatch",
        "workspace_conflict",
        "space_unavailable",
        "unconfirmed",
    ]


def canonical_input_document(scope: dict, files: list[dict]) -> bytes:
    scope = {key: value for key, value in scope.items() if key != "manifest_sha256"}
    return json.dumps(
        {"scope": scope, "files": files},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
