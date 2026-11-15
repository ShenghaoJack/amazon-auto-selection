"""Generate contract-only OpenAPI, JSON Schema, TS types and test fixtures.

Run with the isolated workflow environment. --check detects drift without writes.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workflow.contracts import (SCHEMA_MODELS, LEGAL_COMMANDS, CommandType, NodeSnapshot, NodeStatus,
                                ReviewKind, ReviewRequest, RunSnapshot, RunStatus, WorkflowList)
from workflow.definition import STAGES, validate_mapping


def schemas() -> dict:
    result = {}
    for model in SCHEMA_MODELS:
        schema = model.model_json_schema(ref_template='#/components/schemas/{model}')
        definitions = schema.pop('$defs', {})
        for name, definition in {**definitions, model.__name__: schema}.items():
            if name in result and result[name] != definition:
                raise ValueError(f'Conflicting schema: {name}')
            result[name] = definition
    return dict(sorted(result.items()))


def ref(name: str) -> dict:
    return {'$ref': f'#/components/schemas/{name}'}


def document(components: dict) -> dict:
    paths = {}

    def add(path, method, milestone, output, request=None):
        parameters = []
        for part in ('run_id', 'request_id', 'artifact_id', 'evidence_id'):
            if '{' + part + '}' in path:
                parameters.append({'name': part, 'in': 'path', 'required': True,
                                   'schema': {'type': 'string', 'format': 'uuid'}})
        if method == 'post':
            parameters.append({'name': 'Idempotency-Key', 'in': 'header', 'required': True,
                               'schema': {'type': 'string', 'format': 'uuid'}})
        if path == '/api/v2/workflows' and method == 'get':
            parameters.extend([{'name': 'cursor', 'in': 'query', 'schema': {'type': 'string', 'maxLength': 512}},
                               {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20}},
                               {'name': 'status', 'in': 'query', 'schema': ref('RunStatus')}])
        success = '202' if method == 'post' else '200'
        operation = {
            'operationId': method + '_' + path.replace('/api/v2/', '').replace('/', '_').replace('{', '').replace('}', ''),
            'summary': f'{milestone}: {path}', 'x-milestone': milestone,
            'x-implementation-status': 'contract-only', 'parameters': parameters,
            'responses': {success: {'description': 'Accepted is not execution success.' if method == 'post' else 'Snapshot',
                                   'content': {'application/json': {'schema': output}}},
                          **{code: {'description': label, 'content': {'application/json': {'schema': ref('ApiError')}}}
                             for code, label in [('400', 'Invalid input'), ('404', 'Not found'), ('409', 'Version/state/idempotency conflict'),
                                                 ('503', 'Service not implemented or unavailable')]}}
        }
        if request:
            operation['requestBody'] = {'required': True, 'content': {'application/json': {'schema': ref(request)}}}
        paths.setdefault(path, {})[method] = operation

    add('/api/v2/workflows', 'get', 'M1', ref('WorkflowList'))
    add('/api/v2/workflows', 'post', 'M2', ref('CommandAccepted'), 'CreateWorkflowRequest')
    add('/api/v2/workflows/{run_id}', 'get', 'M1', ref('RunSnapshot'))
    add('/api/v2/workflows/{run_id}/nodes', 'get', 'M1', {'type': 'array', 'items': ref('NodeSnapshot')})
    add('/api/v2/workflows/{run_id}/artifacts', 'get', 'M1', {'type': 'array', 'items': ref('ArtifactSnapshot')})
    add('/api/v2/workflows/{run_id}/logs', 'get', 'M1', ref('LogPage'))
    add('/api/v2/scheduler/overview', 'get', 'M1', ref('SchedulerOverview'))
    add('/api/v2/resources', 'get', 'M2', {'type': 'array', 'items': ref('ResourceSnapshot')})
    add('/api/v2/workflows/{run_id}/resource-waits', 'get', 'M2', {'type': 'array', 'items': ref('ResourceWait')})
    add('/api/v2/workflows/{run_id}/commands', 'post', 'M2', ref('CommandAccepted'), 'CommandRequest')
    add('/api/v2/workflows/{run_id}/reviews/{request_id}/confirm', 'post', 'M3', ref('CommandAccepted'), 'ReviewConfirmation')
    add('/api/v2/reviews', 'get', 'M3', ref('ReviewList'))
    add('/api/v2/workflows/{run_id}/reviews/{request_id}/revisions', 'post', 'M3', ref('CommandAccepted'), 'SaveReviewRevision')
    add('/api/v2/workflows/{run_id}/evidence/{evidence_id}', 'get', 'M5', ref('EvidenceSnapshot'))
    add('/api/v2/workflows/{run_id}/opportunities', 'get', 'M5', {'type': 'array', 'items': ref('OpportunitySnapshot')})
    paths['/api/v2/workflows/{run_id}/events'] = {'get': {
        'operationId': 'stream_run_events', 'summary': 'M2: resumable per-run SSE', 'x-milestone': 'M2',
        'x-implementation-status': 'contract-only',
        'parameters': [{'name': 'run_id', 'in': 'path', 'required': True, 'schema': {'type': 'string', 'format': 'uuid'}},
                       {'name': 'Last-Event-ID', 'in': 'header', 'schema': {'type': 'string', 'pattern': '^[0-9]+$'}}],
        'responses': {'200': {'description': 'SSE id = run_seq; data = RunEvent; global_seq is a separate cursor.',
                              'content': {'text/event-stream': {'schema': {'type': 'string'}}}},
                      '409': {'description': 'Cursor expired; reload snapshot.', 'content': {'application/json': {'schema': ref('ApiError')}}}}
    }}
    paths['/api/v2/events'] = {'get': {
        'operationId': 'stream_global_events', 'summary': 'M2: resumable global summary SSE', 'x-milestone': 'M2',
        'x-implementation-status': 'contract-only',
        'parameters': [{'name': 'Last-Event-ID', 'in': 'header', 'schema': {'type': 'string', 'pattern': '^[0-9]+$'}}],
        'responses': {'200': {'description': 'SSE id = global_seq; data = GlobalEvent. Never reuse a run cursor.',
                              'content': {'text/event-stream': {'schema': {'type': 'string'}}}},
                      '409': {'description': 'Cursor expired; reload snapshot.', 'content': {'application/json': {'schema': ref('ApiError')}}}}
    }}
    paths['/api/v2/workflows/{run_id}/artifacts/{artifact_id}/download'] = {'get': {
        'operationId': 'download_artifact', 'summary': 'M3: run-scoped manifest-authorized download', 'x-milestone': 'M3',
        'x-implementation-status': 'contract-only',
        'parameters': [{'name': key, 'in': 'path', 'required': True, 'schema': {'type': 'string', 'format': 'uuid'}} for key in ('run_id', 'artifact_id')],
        'responses': {'200': {'description': 'File validated against the run manifest; no arbitrary path parameter.',
                              'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}},
                      '404': {'description': 'Artifact not found in task scope.', 'content': {'application/json': {'schema': ref('ApiError')}}}}
    }}
    return {'openapi': '3.1.0', 'info': {'title': 'Product Research Workflow API', 'version': '0.1.0',
            'description': 'M0 contract only. No endpoint is implemented or started by this file. Body schemas plus Python semantic policies are required.'},
            'servers': [{'url': '/'}], 'paths': paths, 'components': {'schemas': components},
            'x-node-definitions': [{'node_id': s.node_id, 'legacy_steps': list(s.legacy_steps), 'milestone': s.milestone,
                                    'reviews': list(s.reviews), 'actions': list(s.actions)} for s in STAGES]}


def ts_type(schema: dict) -> str:
    if '$ref' in schema:
        return schema['$ref'].rsplit('/', 1)[-1]
    if 'const' in schema:
        return json.dumps(schema['const'], ensure_ascii=False)
    if 'enum' in schema:
        return ' | '.join(json.dumps(v, ensure_ascii=False) for v in schema['enum'])
    for union in ('anyOf', 'oneOf'):
        if union in schema:
            return ' | '.join(ts_type(s) for s in schema[union])
    kind = schema.get('type')
    if kind == 'array':
        return f'Array<{ts_type(schema.get("items", {}))}>'
    if kind == 'object':
        if 'properties' not in schema:
            additional = schema.get('additionalProperties')
            return f'Record<string, {ts_type(additional) if isinstance(additional, dict) else "unknown"}>'
        required = schema.get('required', [])
        fields = [f'  {json.dumps(k)}{"" if k in required else "?"}: {ts_type(v)};' for k, v in schema['properties'].items()]
        return '{\n' + '\n'.join(fields) + '\n}'
    return {'integer': 'number', 'number': 'number', 'string': 'string', 'boolean': 'boolean', 'null': 'null'}.get(kind, 'unknown')


def fixtures() -> dict:
    run_id = UUID('12345678-1234-4234-8234-123456789001')
    manifest = UUID('12345678-1234-4234-8234-123456789002')
    nodes = [NodeSnapshot(node_id=s.node_id,
                         status=NodeStatus.SUCCEEDED if i < 3 else NodeStatus.WAITING_HUMAN if i == 3 else NodeStatus.PENDING,
                         attempt=1 if i <= 3 else 0, step_numbers=list(s.legacy_steps),
                         input_refs=[manifest], output_refs=[], started_at=None, finished_at=None, error_code=None)
             for i, s in enumerate(STAGES)]
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    run = RunSnapshot(run_id=run_id, category_id='Contract Demo-US-202610', workspace_id=manifest,
                      execution_id=None, template_id='agent-v3', mode='legacy-compatible',
                      status=RunStatus.WAITING_HUMAN, state_version=7, current_node_ids=['attribute'],
                      node_summary=nodes, allowed_actions=[CommandType.STOP, CommandType.CANCEL],
                      wait_reason='Waiting for vocabulary revision 7.', resource_refs=[],
                      input_manifest_id=manifest, updated_at=now, last_event_seq=12)
    review = ReviewRequest(request_id=UUID('12345678-1234-4234-8234-123456789003'), run_id=run_id,
                           artifact_id=UUID('12345678-1234-4234-8234-123456789004'), kind=ReviewKind.STANDARD_VOCABULARY,
                           revision=7, content_hash='a' * 64, input_manifest_id=manifest, status='OPEN', created_at=now)
    return {'run': run.model_dump(mode='json'), 'review': review.model_dump(mode='json'),
            'list': WorkflowList(items=[run], next_cursor=None, updated_at=now).model_dump(mode='json')}


def outputs() -> dict[Path, str]:
    validate_mapping()
    components = schemas()
    client_defs = json.loads(json.dumps(components).replace('#/components/schemas/', '#/$defs/'))
    schema_doc = {'$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': 'urn:workflow:contracts', '$defs': client_defs}
    dump = lambda value: json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    header = '// Generated by scripts/generate_workflow_contracts.py. Do not edit.\n\n'
    return {
        ROOT / 'api/v2/openapi.json': dump(document(components)),
        ROOT / 'frontend/src/contracts/schema.json': dump(schema_doc),
        ROOT / 'frontend/src/contracts/generated.ts': header + '\n\n'.join(f'export type {name} = {ts_type(schema)};' for name, schema in components.items())
            + '\n\nexport type ContractMap = {\n' + '\n'.join(f'  {name}: {name};' for name in components) + '\n};\n'
            + '\nexport const legalCommands = ' + json.dumps({s.value: sorted(c.value for c in commands) for s, commands in LEGAL_COMMANDS.items()}, indent=2)
            + ' as const satisfies Record<RunStatus, readonly CommandType[]>;\n',
        ROOT / 'api/v2/fixtures/contract-demo.json': dump(fixtures()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    changed = []
    for path, content in outputs().items():
        if path.exists() and path.read_text(encoding='utf-8') == content:
            continue
        changed.append(str(path.relative_to(ROOT)))
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
    if args.check and changed:
        print('Contract drift: ' + ', '.join(changed))
        return 1
    print('Contract artifacts current.' if not changed else 'Generated: ' + ', '.join(changed))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
