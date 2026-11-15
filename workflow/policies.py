"""Pure precondition checks, not a scheduler, a command store or an executor."""
from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any
from uuid import UUID

from .contracts import CommandRequest, LEGAL_COMMANDS, ReviewConfirmation, ReviewRequest, RunSnapshot


class ContractConflict(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_command(run: RunSnapshot, request: CommandRequest) -> None:
    if request.expected_state_version != run.state_version:
        raise ContractConflict('STALE_STATE_VERSION', 'Task snapshot has changed; reload it first.')
    if request.command not in LEGAL_COMMANDS[run.status] or request.command not in run.allowed_actions:
        raise ContractConflict('COMMAND_NOT_ALLOWED', 'Command is not allowed by this snapshot.')


def validate_confirmation(run: RunSnapshot, review: ReviewRequest, request: ReviewConfirmation) -> None:
    if request.expected_state_version != run.state_version:
        raise ContractConflict('STALE_STATE_VERSION', 'Task snapshot has changed.')
    if review.run_id != run.run_id or run.status != 'WAITING_HUMAN' or review.status != 'OPEN':
        raise ContractConflict('REVIEW_NOT_OPEN', 'Review is not open for this waiting task.')
    if (request.artifact_id, request.revision, request.content_hash, request.input_manifest_id) != (
            review.artifact_id, review.revision, review.content_hash, review.input_manifest_id):
        raise ContractConflict('STALE_REVIEW', 'Review version or input snapshot has changed.')
    if review.input_manifest_id != run.input_manifest_id:
        raise ContractConflict('STALE_REVIEW', 'Upstream input snapshot has changed.')


def payload_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def tool_call_key(run_id: UUID, node_id: str, tool_id: str, tool_version: str, input_fingerprint: str) -> str:
    # execution_id / retry attempt intentionally excluded: recovery reuses the same logical call.
    return payload_fingerprint({'run_id': str(run_id), 'node_id': node_id, 'tool_id': tool_id,
                                'tool_version': tool_version, 'input_fingerprint': input_fingerprint})


class LegacyDecision(StrEnum):
    EXECUTE = 'EXECUTE'
    REUSE = 'REUSE'
    RECONCILE = 'RECONCILE'
    EXPLICIT_RERUN_REQUIRED = 'EXPLICIT_RERUN_REQUIRED'


def decide_legacy_call(*, step_status: str, same_input: bool, artifacts_valid: bool,
                       receipt_status: str | None) -> LegacyDecision:
    if step_status not in {'PENDING', 'RUNNING', 'DONE', 'SKIP', 'FAILED', 'INTERRUPTED'}:
        return LegacyDecision.RECONCILE
    if receipt_status in {'STARTED', 'EXTERNAL_PENDING', 'RESULT_UNKNOWN'} or step_status == 'RUNNING':
        return LegacyDecision.RECONCILE
    if receipt_status == 'SUCCEEDED' and same_input and artifacts_valid:
        return LegacyDecision.REUSE
    if step_status in {'DONE', 'SKIP'}:
        return LegacyDecision.REUSE if same_input and artifacts_valid else LegacyDecision.EXPLICIT_RERUN_REQUIRED
    if receipt_status not in {None, 'FAILED', 'NOT_STARTED'}:
        return LegacyDecision.RECONCILE
    return LegacyDecision.EXECUTE
