import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from workflow.contracts import (CommandRequest, ReviewConfirmation, ReviewRequest, RunSnapshot, WorkflowList)
from workflow.definition import validate_mapping
from workflow.policies import (ContractConflict, LegacyDecision, decide_legacy_call,
                               payload_fingerprint, tool_call_key, validate_command, validate_confirmation)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / 'api/v2/fixtures/contract-demo.json').read_text(encoding='utf-8'))


def model(cls, value):
    return cls.model_validate_json(json.dumps(value))


def test_shared_fixture_and_mapping():
    validate_mapping()
    assert model(WorkflowList, FIXTURE['list']).items[0].state_version == 7
    assert model(ReviewRequest, FIXTURE['review']).revision == 7


@pytest.mark.parametrize('change', [
    {'state_version': '7'}, {'state_version': True}, {'state_version': 0},
    {'allowed_actions': ['resume']}, {'current_node_ids': ['market']},
    {'updated_at': '2026-10-17T00:00:00'}, {'secret': 'not accepted'},
    {'status': 'SUCCEEDED', 'allowed_actions': [], 'current_node_ids': []},
])
def test_invalid_snapshot_rejected(change):
    with pytest.raises(ValidationError):
        model(RunSnapshot, {**FIXTURE['run'], **change})


def test_command_checks_version_and_waiting_state():
    run = model(RunSnapshot, FIXTURE['run'])
    validate_command(run, CommandRequest(command='stop', expected_state_version=7))
    for command, version, code in [('stop', 6, 'STALE_STATE_VERSION'), ('resume', 7, 'COMMAND_NOT_ALLOWED')]:
        with pytest.raises(ContractConflict) as error:
            validate_command(run, CommandRequest(command=command, expected_state_version=version))
        assert error.value.code == code


def test_review_checks_version_hash_scope_and_upstream():
    run = model(RunSnapshot, FIXTURE['run'])
    review = model(ReviewRequest, FIXTURE['review'])
    fields = {key: FIXTURE['review'][key] for key in ('artifact_id', 'revision', 'content_hash', 'input_manifest_id')}
    valid = model(ReviewConfirmation, {**fields, 'expected_state_version': 7})
    validate_confirmation(run, review, valid)
    for change in ({'revision': 6}, {'content_hash': 'b' * 64}, {'artifact_id': run.workspace_id}):
        changed = valid.model_copy(update=change)
        with pytest.raises(ContractConflict):
            validate_confirmation(run, review, changed)
    with pytest.raises(ContractConflict):
        validate_confirmation(run, review.model_copy(update={'status': 'CONFIRMED'}), valid)
    with pytest.raises(ContractConflict):
        validate_confirmation(run, review.model_copy(update={'run_id': run.workspace_id}), valid)
    with pytest.raises(ContractConflict):
        validate_confirmation(run.model_copy(update={'input_manifest_id': review.artifact_id}), review, valid)


def test_tool_key_is_stable_across_recovery_but_scoped_to_input_and_task():
    digest = payload_fingerprint({'b': 2, 'a': 1})
    assert digest == payload_fingerprint({'a': 1, 'b': 2})
    run = UUID(FIXTURE['run']['run_id'])
    key = tool_call_key(run, 'market', 'step12', '1', digest)
    assert key == tool_call_key(run, 'market', 'step12', '1', digest)
    assert key != tool_call_key(UUID(FIXTURE['run']['workspace_id']), 'market', 'step12', '1', digest)
    assert key != tool_call_key(run, 'market', 'step12', '2', digest)


@pytest.mark.parametrize('status,same,valid,receipt,decision', [
    ('DONE', True, True, 'SUCCEEDED', LegacyDecision.REUSE),
    ('DONE', False, True, 'SUCCEEDED', LegacyDecision.EXPLICIT_RERUN_REQUIRED),
    ('DONE', True, False, 'SUCCEEDED', LegacyDecision.EXPLICIT_RERUN_REQUIRED),
    ('PENDING', True, True, 'RESULT_UNKNOWN', LegacyDecision.RECONCILE),
    ('PENDING', True, True, 'EXTERNAL_PENDING', LegacyDecision.RECONCILE),
    ('RUNNING', True, True, None, LegacyDecision.RECONCILE),
    ('PENDING', False, False, 'NOT_STARTED', LegacyDecision.EXECUTE),
    ('PENDING', True, True, 'unrecognized', LegacyDecision.RECONCILE),
    ('UNKNOWN', True, True, None, LegacyDecision.RECONCILE),
])
def test_legacy_guard_never_blindly_repeats_paid_or_running_calls(status, same, valid, receipt, decision):
    assert decide_legacy_call(step_status=status, same_input=same, artifacts_valid=valid, receipt_status=receipt) == decision


def test_generated_files_are_current():
    from scripts.generate_workflow_contracts import outputs
    for path, expected in outputs().items():
        assert path.read_text(encoding='utf-8') == expected, path


def test_openapi_refs_and_body_schemas_resolve():
    from jsonschema import Draft202012Validator
    schema = json.loads((ROOT / 'frontend/src/contracts/schema.json').read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    for name, payload in [('RunSnapshot', FIXTURE['run']), ('ReviewRequest', FIXTURE['review']), ('WorkflowList', FIXTURE['list'])]:
        validator = Draft202012Validator({**schema, '$ref': f'#/$defs/{name}'})
        validator.validate(payload)
    document = json.loads((ROOT / 'api/v2/openapi.json').read_text(encoding='utf-8'))

    def walk(value):
        if isinstance(value, dict):
            if '$ref' in value:
                current = document
                for key in value['$ref'].removeprefix('#/').split('/'):
                    current = current[key]
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(document)
