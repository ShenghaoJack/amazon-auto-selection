# Workflow v2 · M0 foundation

M0 defines and verifies the integration boundary. There is no production scheduler, resource lease manager, HTTP command handler or business Agent runtime in this package yet. Importing workflow does not launch tasks.

## Environments

- Legacy CLI regression checks: existing Python 3.13.9 environment, existing root dependencies unchanged.
- Workflow control plane smoke checks: isolated Python 3.12.14 at work/m0-venv. Validated LangGraph 1.2.16, checkpoint 4.3.0, SQLite checkpointer 3.1.2 and Pydantic 2.14.1.
- requirements.in defines direct runtime dependencies; requirements-dev.in adds tests; requirements.lock records the complete validated M0 environment, including test dependencies. Production dependency trimming is a later packaging task.
- Existing agent/ dependencies remain separate. M2 Workers must explicitly select the legacy interpreter rather than silently mixing environments.

Create a dedicated environment and install the lock:

~~~powershell
python -m venv work/m0-venv
work/m0-venv/Scripts/python.exe -m pip install -r workflow/requirements.lock
work/m0-venv/Scripts/python.exe -m pip check
~~~

On this Windows sandbox, pip's default temporary directory was not writable. Installation succeeded with TMP and TEMP set only for the installer process to an existing project-local work/m0-pip-temp directory. No system pip configuration was changed.

## Contract generation

~~~powershell
work/m0-venv/Scripts/python.exe scripts/generate_workflow_contracts.py
work/m0-venv/Scripts/python.exe scripts/generate_workflow_contracts.py --check
work/m0-venv/Scripts/python.exe -m pytest tests/workflow -q
~~~

contracts.py is authoritative for wire models. definition.py defines node/step mapping and the two review kinds. Generation writes OpenAPI, frontend JSON Schema, TypeScript types, allowed-command tables and shared fixtures. Check generated files into the same change; --check fails on drift.

policies.py contains pure checks for state versions, allowed commands, review version/hash/scope, stable logical tool keys and conservative legacy reuse decisions. These are not persistent idempotency or concurrency implementations. M2/M3 must call them inside the corresponding transactional command/review services.

## Execution and replay boundaries

- run_id remains stable across recovery and is the graph thread_id; execution_id changes for each execution attempt. workspace_id identifies the writable data domain. A legacy-compatible run refers to its actual shared workspace, not a fabricated independent workspace.
- Idempotency keys are scoped by caller + method/route + intent. Store canonical payload fingerprints and return the same command receipt for an exact retry; a reused key with a different payload is a conflict.
- Tool logical keys include run, node, tool/version and the complete business-input fingerprint. execution_id is deliberately excluded so crash recovery can reuse receipts.
- DONE/SKIP only permits reuse when input and artifact checks match. Unknown/in-flight receipts require reconciliation. Changed or invalid completed output requires an explicit rerun decision; never blindly forward recovery to run_single.
- resume is not approval. An open human request still requires the matching artifact revision, hash and input manifest before a graph approval response can be sent.
- A 202 receipt means ACCEPTED only. Stop remains STOPPING until the correct Worker exits. M0 does not implement that process lifecycle.
- Snapshot node_summary contains all eight business nodes. Legacy-only templates may mark future analysis nodes SKIPPED; they may not pretend those Agents ran.
- Success requires every reported node to be terminal and the quality node to succeed. Expanded evidence/quality gates are M5 work; M0 only fixes legacy monitor coverage.

## Process-restart smoke

tests/workflow/test_checkpoint_m0.py invokes checkpoint_smoke.py in two different Python processes. The first process runs two distinct graph threads to an interrupt in a shared SQLite file. The second process reopens it, resumes both threads and checks each prepare stub ran once.

This checks persistence, basic concurrent access and replay placement. It is not a production load test or proof of external side-effect exactly-once execution. No browser, LLM, customer config, real input data or production checkpoint store is used.

See ../docs/M0开发交付与接口约定.md for the Chinese handoff and outstanding legacy test mismatches.
