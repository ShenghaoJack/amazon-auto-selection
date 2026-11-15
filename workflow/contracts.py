"""Authoritative wire models. Generated OpenAPI and TypeScript must match these."""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Self, get_args
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, model_validator

PositiveInt = Annotated[StrictInt, Field(ge=1)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Token = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
NodeId = Literal['collect', 'keyword', 'market', 'attribute', 'voc', 'opportunity', 'quality', 'report']


class WireModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class RunStatus(StrEnum):
    CREATED = 'CREATED'
    QUEUED = 'QUEUED'
    RUNNING = 'RUNNING'
    WAITING_RESOURCE = 'WAITING_RESOURCE'
    WAITING_HUMAN = 'WAITING_HUMAN'
    PAUSING = 'PAUSING'
    PAUSED = 'PAUSED'
    STOPPING = 'STOPPING'
    INTERRUPTED = 'INTERRUPTED'
    RECONCILING = 'RECONCILING'
    FAILED = 'FAILED'
    SUCCEEDED = 'SUCCEEDED'
    CANCELLED = 'CANCELLED'


class NodeStatus(StrEnum):
    PENDING = 'PENDING'
    READY = 'READY'
    RUNNING = 'RUNNING'
    WAITING_RESOURCE = 'WAITING_RESOURCE'
    WAITING_HUMAN = 'WAITING_HUMAN'
    PAUSED = 'PAUSED'
    INTERRUPTED = 'INTERRUPTED'
    RECONCILING = 'RECONCILING'
    FAILED = 'FAILED'
    SUCCEEDED = 'SUCCEEDED'
    SKIPPED = 'SKIPPED'


class CommandType(StrEnum):
    START = 'start'
    PAUSE = 'pause'
    STOP = 'stop'
    RESUME = 'resume'
    RETRY = 'retry'
    CANCEL = 'cancel'


# The maximum legal command set; runtime authorization/capability checks may narrow it.
LEGAL_COMMANDS: dict[RunStatus, frozenset[CommandType]] = {
    RunStatus.CREATED: frozenset({CommandType.START, CommandType.CANCEL}),
    RunStatus.QUEUED: frozenset({CommandType.PAUSE, CommandType.STOP, CommandType.CANCEL}),
    RunStatus.RUNNING: frozenset({CommandType.PAUSE, CommandType.STOP}),
    RunStatus.WAITING_RESOURCE: frozenset({CommandType.PAUSE, CommandType.STOP, CommandType.CANCEL}),
    RunStatus.WAITING_HUMAN: frozenset({CommandType.STOP, CommandType.CANCEL}),
    RunStatus.PAUSING: frozenset({CommandType.STOP}),
    RunStatus.PAUSED: frozenset({CommandType.RESUME, CommandType.CANCEL}),
    RunStatus.STOPPING: frozenset(),
    RunStatus.INTERRUPTED: frozenset({CommandType.RESUME, CommandType.CANCEL}),
    RunStatus.RECONCILING: frozenset(),
    RunStatus.FAILED: frozenset({CommandType.RETRY, CommandType.CANCEL}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class NodeSnapshot(WireModel):
    node_id: NodeId
    status: NodeStatus
    attempt: NonNegativeInt
    step_numbers: list[Annotated[StrictInt, Field(ge=0, le=20)]]
    input_refs: list[UUID]
    output_refs: list[UUID]
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    error_code: Token | None


class RunSnapshot(WireModel):
    run_id: UUID
    category_id: Annotated[str, Field(min_length=1, max_length=200)]
    workspace_id: UUID
    execution_id: UUID | None
    template_id: Literal['legacy-v1', 'agent-v3']
    mode: Literal['legacy-compatible', 'task-isolated']
    status: RunStatus
    state_version: PositiveInt
    current_node_ids: list[NodeId]
    node_summary: list[NodeSnapshot]
    allowed_actions: list[CommandType]
    wait_reason: Annotated[str, Field(max_length=2000)] | None
    resource_refs: list[Token]
    input_manifest_id: UUID
    updated_at: AwareDatetime
    last_event_seq: NonNegativeInt

    @model_validator(mode='after')
    def coherent_snapshot(self) -> Self:
        ids = [node.node_id for node in self.node_summary]
        if len(ids) != len(set(ids)) or len(self.current_node_ids) != len(set(self.current_node_ids)):
            raise ValueError('node ids must be unique')
        if not set(self.current_node_ids).issubset(ids):
            raise ValueError('current node must exist in node_summary')
        if set(ids) != set(get_args(NodeId)):
            raise ValueError('snapshot must include all eight business nodes')
        if any(node.node_id in self.current_node_ids and node.status in {NodeStatus.SUCCEEDED, NodeStatus.SKIPPED} for node in self.node_summary):
            raise ValueError('current node cannot already be finished')
        if len(self.allowed_actions) != len(set(self.allowed_actions)):
            raise ValueError('allowed_actions must be unique')
        if not set(self.allowed_actions).issubset(LEGAL_COMMANDS[self.status]):
            raise ValueError('allowed_actions contradict run status')
        if self.status == RunStatus.SUCCEEDED:
            if self.current_node_ids or any(n.status not in {NodeStatus.SUCCEEDED, NodeStatus.SKIPPED} for n in self.node_summary) or not any(n.node_id == 'quality' and n.status == NodeStatus.SUCCEEDED for n in self.node_summary):
                raise ValueError('success requires final monitor success and no active nodes')
        return self


class WorkflowList(WireModel):
    items: list[RunSnapshot]
    next_cursor: Annotated[str, Field(max_length=512)] | None
    updated_at: AwareDatetime

    @model_validator(mode='after')
    def unique_runs(self) -> Self:
        if len({run.run_id for run in self.items}) != len(self.items):
            raise ValueError('Run ids must be unique in a list')
        return self


class CreateWorkflowRequest(WireModel):
    category_id: Annotated[str, Field(min_length=1, max_length=200)]
    config_ref: Token
    input_manifest_id: UUID
    mode: Literal['legacy-compatible', 'task-isolated']
    template_id: Literal['legacy-v1', 'agent-v3']


class CommandRequest(WireModel):
    command: CommandType
    expected_state_version: PositiveInt


class CommandAccepted(WireModel):
    command_id: UUID
    run_id: UUID
    status: Literal['ACCEPTED']
    received_at: AwareDatetime


class ReviewKind(StrEnum):
    ATTRIBUTE_SPEC = 'attribute_spec'
    STANDARD_VOCABULARY = 'standard_vocabulary'


class ReviewRequest(WireModel):
    request_id: UUID
    run_id: UUID
    artifact_id: UUID
    kind: ReviewKind
    revision: PositiveInt
    content_hash: Sha256
    input_manifest_id: UUID
    status: Literal['OPEN', 'CONFIRMED', 'SUPERSEDED']
    created_at: AwareDatetime


class ReviewConfirmation(WireModel):
    artifact_id: UUID
    revision: PositiveInt
    content_hash: Sha256
    input_manifest_id: UUID
    expected_state_version: PositiveInt


class ArtifactSnapshot(WireModel):
    artifact_id: UUID
    run_id: UUID
    node_id: NodeId
    name: Annotated[str, Field(min_length=1, max_length=240)]
    revision: PositiveInt
    content_hash: Sha256
    media_type: Annotated[str, Field(min_length=1, max_length=160)]
    size_bytes: NonNegativeInt
    # Paths are deliberately absent. The server resolves artifact_id in a run-scoped manifest.
    ready: bool


class RunEvent(WireModel):
    event_id: UUID
    run_id: UUID
    run_seq: PositiveInt
    execution_id: UUID | None
    type: Token
    occurred_at: AwareDatetime
    state_version: PositiveInt
    payload: dict[str, Any]


class GlobalEvent(WireModel):
    global_seq: PositiveInt
    event: RunEvent


class SchedulerOverview(WireModel):
    running: NonNegativeInt
    waiting_human: NonNegativeInt
    waiting_resource: NonNegativeInt
    queued: NonNegativeInt
    workflow_capacity: PositiveInt
    worker_capacity: PositiveInt
    updated_at: AwareDatetime


class ResourceSnapshot(WireModel):
    resource_key: Token
    status: Literal['FREE', 'HELD', 'RECONCILING']
    owner_run_id: UUID | None
    execution_id: UUID | None
    lease_epoch: NonNegativeInt
    expires_at: AwareDatetime | None
    waiting_run_ids: list[UUID]


class ResourceWait(WireModel):
    run_id: UUID
    node_id: NodeId
    resource_keys: list[Token]
    waiting_since: AwareDatetime
    reason: Annotated[str, Field(max_length=2000)]


class LogEntry(WireModel):
    seq: PositiveInt
    occurred_at: AwareDatetime
    level: Literal['DEBUG', 'INFO', 'WARN', 'ERROR']
    message: Annotated[str, Field(max_length=8000)]


class LogPage(WireModel):
    run_id: UUID
    items: list[LogEntry]
    next_cursor: Annotated[str, Field(max_length=512)] | None


class ReviewList(WireModel):
    items: list[ReviewRequest]
    next_cursor: Annotated[str, Field(max_length=512)] | None


class SaveReviewRevision(WireModel):
    expected_state_version: PositiveInt
    expected_revision: PositiveInt
    input_manifest_id: UUID
    content: dict[str, Any]


class EvidenceSnapshot(WireModel):
    evidence_id: UUID
    run_id: UUID
    artifact_id: UUID
    kind: Literal['review', 'listing', 'market', 'keyword', 'derived']
    source_ref: Annotated[str, Field(min_length=1, max_length=512)]
    location: Annotated[str, Field(min_length=1, max_length=512)]
    quote: Annotated[str, Field(max_length=8000)] | None
    source_hash: Sha256


class OpportunitySnapshot(WireModel):
    opportunity_id: UUID
    run_id: UUID
    kind: Literal['feature', 'configuration', 'product_line']
    title: Annotated[str, Field(min_length=1, max_length=200)]
    status: Literal['NEEDS_EVIDENCE', 'READY_FOR_REVIEW', 'ACCEPTED', 'DEFERRED']
    evidence_ids: list[UUID]
    hypotheses: list[str]
    validation_plan: list[str]


class ApiError(WireModel):
    code: Literal['INVALID_INPUT', 'NOT_FOUND', 'STALE_STATE_VERSION', 'COMMAND_NOT_ALLOWED',
                  'IDEMPOTENCY_CONFLICT', 'STALE_REVIEW', 'REVIEW_NOT_OPEN',
                  'RESOURCE_IDENTITY_CONFLICT', 'RESYNC_REQUIRED', 'NOT_IMPLEMENTED', 'SERVICE_UNAVAILABLE']
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    request_id: UUID


SCHEMA_MODELS = (RunSnapshot, WorkflowList, CreateWorkflowRequest, CommandRequest,
                 CommandAccepted, ReviewRequest, ReviewConfirmation, ArtifactSnapshot,
                 RunEvent, GlobalEvent, SchedulerOverview, ApiError, ResourceSnapshot,
                 ResourceWait, LogPage, ReviewList, SaveReviewRevision, EvidenceSnapshot, OpportunitySnapshot)
