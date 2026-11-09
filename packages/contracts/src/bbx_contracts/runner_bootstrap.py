"""Trusted Task resource bootstrap requests, separate from Runner capabilities."""

import hashlib
import ipaddress
import json
import re
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .models import ContractModel
from .native_gateway import (
    NativeBudgetCurrency,
    NativeBudgetExchange,
    require_native_budget_currency,
)
from .runner import Digest, JournalCursor, RunnerCommand, RunnerId
from .runner_inputs import NativeSharedInputLimits
from .runner_vpn import VPN_DYNAMIC_PROFILE, NativeVpnCapability

RUNTIME_LOCK_KEY = 0x42425852554E
RUNTIME_FENCE_SEQUENCE = "native_runtime_controller_fence"
BINDING_CAPTURE_CONFIG_FIELDS = frozenset(
    {
        "traffic_targets",
        "traffic_target_namespace",
        "traffic_sandbox_image",
    }
)


def validate_binding_capture_deployment(config: dict) -> None:
    """Both publication and Runtime admit the same fixed capture targets and image."""
    for key in (
        "source_id",
        "kubeconfig",
        "context",
        "cluster_uid",
        "environment_revision",
        "sandbox_server_url",
        "storage_class",
        "system_namespace",
    ):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError("Trusted Assignment traffic deployment is incomplete")
    labels = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
    if not labels.fullmatch(config["system_namespace"]):
        raise ValueError("Invalid trusted resource name")
    network = config.get("sandbox_network")
    if (
        not isinstance(network, dict)
        or set(network) != {"namespace", "network_identity", "ipv4"}
        or any(
            not isinstance(network[key], str) or not labels.fullmatch(network[key])
            for key in ("namespace", "network_identity")
        )
        or not isinstance(network["ipv4"], str)
    ):
        raise ValueError("A frozen Sandbox Assignment network is required")
    ipaddress.IPv4Address(network["ipv4"])
    server = urlsplit(config["sandbox_server_url"])
    if (
        server.scheme not in {"http", "https"}
        or not server.hostname
        or server.username
        or server.password
        or server.query
        or server.fragment
        or server.path not in {"", "/"}
    ):
        raise ValueError("A fixed native Sandbox Server origin is required")
    if execd := config.get("sandbox_host_execd_origin"):
        if not isinstance(execd, str) or any(char.isspace() for char in execd):
            raise ValueError("The native execd ingress authority is invalid")
        authority = urlsplit("http://" + execd)
        if (
            not authority.hostname
            or authority.username
            or authority.password
            or authority.path
            or authority.query
            or authority.fragment
        ):
            raise ValueError("The native execd ingress authority is invalid")
    if type(config.get("journal_writer_lock", False)) is not bool:
        raise ValueError("Journal writer locking requires an explicit boolean")
    vpn = config.get("vpn")
    dynamic_vpn = isinstance(vpn, dict) and vpn.get("profile") == VPN_DYNAMIC_PROFILE
    targets = config.get("traffic_targets")
    namespace = config.get("traffic_target_namespace")
    image = config.get("traffic_sandbox_image")
    base = config.get("sandbox_image")
    if (
        config.get("runner_profile") != "node-sandbox-host-v1"
        or not isinstance(targets, list)
        or not (targets == [] if dynamic_vpn else 1 <= len(targets) <= 8)
        or not isinstance(namespace, str)
        or not labels.fullmatch(namespace)
        or namespace
        in {"default", "kube-system", "kube-public", "kube-node-lease", config["system_namespace"]}
        or not isinstance(base, str)
        or not isinstance(image, str)
        or not all(
            isinstance(value, str) and re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", value)
            for value in (base, image, config.get("runner_image"), config.get("sandbox_host_image"))
        )
        or image.rsplit("@", 1)[-1] == base.rsplit("@", 1)[-1]
    ):
        raise ValueError("Trusted Assignment traffic deployment is incomplete")
    pairs = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"ipv4", "port"}:
            raise ValueError("Invalid Assignment network target")
        ip, port = target["ipv4"], target["port"]
        if not isinstance(ip, str) or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid Assignment network target")
        address = ipaddress.IPv4Address(ip)
        pair = (address, port)
        if (
            str(address) != ip
            or address in ipaddress.IPv4Network("198.19.0.0/16")
            or address.is_loopback
            or address.is_multicast
            or address.is_link_local
            or address.is_unspecified
            or address.is_reserved
            or pair in pairs
        ):
            raise ValueError("Invalid Assignment network target")
        pairs.add(pair)


class NativeCtfEnvironmentRef(ContractModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    revision: int = Field(ge=1, strict=True)
    publication_stamp: Digest


class NativeCtfEnvironment(NativeCtfEnvironmentRef):
    label: str = Field(min_length=1, max_length=100)
    shared_inputs: NativeSharedInputLimits | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    vpn: NativeVpnCapability | None = Field(default=None, exclude_if=lambda value: value is None)


class TaskCreationCapabilities(ContractModel):
    mode: Literal["legacy", "native-ctf"]
    native_ctf: NativeCtfEnvironment | None = None


def management_document_digest(value: dict) -> str:
    """Hash a JSON management document independently of object key order."""
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def retirement_manifest_digest(manifest: dict) -> str:
    return management_document_digest(manifest)


class NativeRunnerLaunch(ContractModel):
    request_id: UUID
    model_alias: str = Field(min_length=1, max_length=512)
    exchange_rate: NativeBudgetExchange = None
    helper_foreground: bool = False
    helper_background: bool = False
    traffic_profile: Literal["binding-pcap-v1"] | None = None
    budget_currency: NativeBudgetCurrency = None

    @model_validator(mode="after")
    def exact_currency(self):
        require_native_budget_currency(self.exchange_rate, self.budget_currency)
        return self


def native_pause_state(task):
    return ((task.get("ctf_control") or {}).get("native_pause") or {}).get("state")


def native_recovery_frozen(task):
    return bool((task.get("ctf_control") or {}).get("native_recovery"))


def native_resume_pending(task):
    return native_pause_state(task) == "resuming" or bool(
        (task.get("ctf_control") or {}).get("native_recovery_rebuilding")
    )


class NativePauseRequest(ContractModel):
    request_id: UUID


class NativeResumeRequest(ContractModel):
    request_id: UUID
    pause_id: UUID
    instruction: str | None = Field(default=None, min_length=1, max_length=1500)


class NativeBudgetIncreaseRequest(ContractModel):
    request_id: UUID
    pause_id: UUID
    additional_cny: Decimal | None = Field(
        default=None, gt=0, max_digits=16, decimal_places=9, exclude_if=lambda value: value is None
    )
    additional_usd: Decimal | None = Field(
        default=None, gt=0, max_digits=16, decimal_places=9, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def exact_currency(self):
        if (self.additional_cny is None) == (self.additional_usd is None):
            raise ValueError("Specify exactly one budget amount")
        return self


class NativeBudgetIncreaseStatus(ContractModel):
    request_id: UUID
    pause_id: UUID
    threshold_cny: Decimal | None = Field(default=None, exclude_if=lambda value: value is None)
    threshold_usd: Decimal
    budget_currency: Literal["USD"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class NativePauseStatus(ContractModel):
    pause_id: UUID | None = None
    state: Literal["running", "draining", "paused", "resuming", "resumed", "stopping", "stopped"]
    blocker: str | None = None


class NativeRecoveryRequest(ContractModel):
    request_id: UUID
    incident_id: UUID
    instruction: str | None = Field(default=None, min_length=1, max_length=1500)


class NativeRecoveryIntent(ContractModel):
    request_id: UUID
    incident_id: UUID


class NativeRecoveryBinding(ContractModel):
    binding_id: RunnerId
    agent_id: RunnerId
    state: str
    assignment_id: RunnerId | None = None
    sdk_session_id: RunnerId | None = None
    confirmed_cursor: JournalCursor | None = Field(
        default=None,
        description="Last database confirmation; this projection does not read journal bytes.",
    )
    sdk_start_unconfirmed: bool


class NativeRecoveryStatus(ContractModel):
    """Recovery progress and the original execution ledgers, without journal contents."""

    state: Literal[
        "not_frozen", "frozen", "requested", "rebuilding", "resumed", "stopping", "stopped"
    ]
    blocker: str | None = None
    request_id: UUID | None = None
    replacement_epoch: int | None = None
    incident_id: UUID | None = None
    operation_id: UUID | None = None
    reason: str | None = None
    captured_at: AwareDatetime | None = None
    original_epoch: int | None = None
    controller_generation: int | None = None
    sandbox_create_claimed: bool = False
    resources: dict[str, str] = Field(default_factory=dict)
    bindings: list[NativeRecoveryBinding] = Field(default_factory=list, max_length=32)
    bindings_truncated: bool = False
    ledger: dict[str, dict[str, int]] = Field(default_factory=dict)


class HelperGoal(ContractModel):
    goal: str = Field(min_length=1, max_length=2000)
    input_refs: list[RunnerId] = Field(default_factory=list, max_length=8)
    expected_output: str = Field(min_length=1, max_length=1000)


class HelperStatus(ContractModel):
    state: Literal[
        "reserved", "launch_claimed", "accepted", "draining", "saved", "unavailable", "unknown"
    ]
    invocation_id: Digest
    child_id: RunnerId | None = None
    binding_id: RunnerId | None = None
    deadline_ms: int | None = None
    reason: str | None = None
    result_json: str | None = None
    result_digest: Digest | None = None
    child_deadline_ms: int | None = Field(default=None, ge=1, le=9007199254740991)
    background_state: Literal["scheduled", "running", "draining", "finished", "unknown"] | None = (
        None
    )
    outcome_json: str | None = None
    outcome_digest: Digest | None = None
    delivery: Literal["queued", "historical", "applied"] | None = None


class HelperAccepted(ContractModel):
    child_id: RunnerId
    binding_id: RunnerId
    payload_digest: Digest


class HelperTarget(ContractModel):
    child_run_id: RunnerId


class HelperLaunch(HelperStatus):
    launch_allowed: bool = False
    command: RunnerCommand | None = None
    credentials: dict[str, Any] | None = None


class HelperTerminal(ContractModel):
    child_id: RunnerId
    binding_id: RunnerId
    plugin_run_id: RunnerId
    sdk_session_id: RunnerId
    cursor: JournalCursor
    sdk_idle: Literal[True]
    disposed: Literal[True]
    journal_confirmed: Literal[True]


class HelperResult(ContractModel):
    result_json: str = Field(min_length=1, max_length=16000)


class NativeRunnerClaim(ContractModel):
    controller_id: RunnerId
    controller_fence: int = Field(ge=1)
    controller_pid: int = Field(ge=1)
    controller_started_at: AwareDatetime
    resource_config: dict[str, Any] = Field(max_length=32)
    retirement_single_apiserver: dict[str, str] = Field(default_factory=dict, max_length=2)


class NativeRunnerAction(ContractModel):
    operation_id: UUID
    controller_id: RunnerId
    controller_generation: int = Field(ge=1)
    resource_generation: int = Field(default=1, ge=1)
    action: Literal[
        "sandbox_create",
        "observation",
        "material",
        "activate",
        "renew",
        "stop",
        "cleanup",
        "error",
        "retire_prepare",
        "retire_plan",
        "retire_admit",
        "retire_complete",
        "sandbox_host_material",
        "sandbox_host_ready",
        "vpn_material",
        "vpn_peer_ready",
        "vpn_prepare_source",
        "network_default_ready",
        "recovery_exit",
        "recovery_prepare",
        "recovery_verifier_admit",
        "recovery_verifier_observe",
        "recovery_replace",
        "recovery_blocked",
    ]
    observation: dict[str, Any] = Field(default_factory=dict, max_length=18)
    proof: dict[str, Any] = Field(default_factory=dict, max_length=8)
    error_code: (
        Literal[
            "native_exit_unconfirmed",
            "native_resource_unknown",
            "native_control_unavailable",
            "native_bootstrap_unavailable",
        ]
        | None
    ) = None
