# Workflow Engine

Durable, dependency-aware execution of structured work. The planner decides
*what* belongs in a workflow; `WorkflowEngine` (`src/eurus/workflows/`)
coordinates *how* the graph runs. Nodes execute through the Automation
Engine; no node runs LLM reasoning inside the engine itself.

## 1. Definitions and versioning

Definitions are versioned documents (`eurus_workflow_definitions`, primary
key `(workflow_id, version)`). Registration validates the graph and stores
the next version; identical re-registration returns the stored version.
Instances pin the version they started with, so editing a definition never
mutates a running instance. A definition is active/inactive; starting an
inactive definition is rejected.

## 2. Node types

| Type | Category | Contract |
| --- | --- | --- |
| `agent_task` | action | `objective` (templates), optional `allowed_tools`, `max_steps`, `success_criteria`, `compensation` |
| `tool_task` | action | `tool` + `arguments`; execution is scoped to exactly that tool |
| `specialist_task` | action | `role` + `objective`; tools/budgets resolve from the specialist registry |
| `subworkflow` | action | `workflow_id` + `inputs`; completes with the child instance |
| `condition` | instant | `path`, `op`, `value` against instance context; output `true`/`false` |
| `validation` | instant | `assertions` list; `mode` all/any; failure fails the node |
| `transform` | instant | `set` mapping of templated values into `vars` |
| `notification` | instant | `message` template delivered via the notification sink |
| `parallel` | structural | fork barrier; completes immediately |
| `join` | structural | sync barrier; completes when all dependencies are terminal |
| `delay` | deferred | `seconds`; job scheduled at `now + seconds` |
| `schedule` | deferred | `run_at` + `objective`; job scheduled at the given time |
| `approval` | gate | `prompt`; waits for an explicit human decision |

## 3. Validation

`WorkflowDefinition.validate` rejects: duplicate or malformed node ids,
unknown node types, missing required config, unknown dependency references,
self-dependencies, cycles (iterative DFS), `only_if` entries that do not
reference a dependency, invalid tool names, out-of-range delays, malformed
`run_at`, invalid validation/transform shapes, and graphs over 64 nodes.
`start` additionally rejects missing `required_inputs`.

## 4. Execution model

- Instances carry a context document: `inputs`, `nodes` (per-node status,
  output, error), and `vars` (transform outputs). Templates
  (`{{inputs.x}}`, `{{nodes.n.output}}`, `{{vars.k}}`) resolve against it;
  unresolvable paths are hard errors.
- Dispatch is dependency-driven: a PENDING node runs when all dependencies
  are terminal, no dependency FAILED/CANCELLED/SKIPPED (otherwise the node
  is SKIPPED, propagating), and every `only_if` branch condition matches.
- Action nodes submit jobs with deterministic ids
  (`wf-<instance>-<node>-<attempt>`) and payloads carrying
  `workflow_instance_id`, `node_id`, `node_type`, resolved inputs, tool
  scopes, and budgets. Job completion (via the automation `on_job_finished`
  callback) updates node state and re-advances the instance.
- Deferred nodes hold `WAITING_SCHEDULE` until their future-dated job runs;
  approval nodes hold `WAITING_APPROVAL` until decided.
- Instance status mirrors blocking nodes: WAITING_APPROVAL,
  WAITING_SCHEDULE, RUNNING; PAUSED instances are skipped by ticks.

## 5. Recovery and idempotency

- All node state is persisted; after a restart, `recover()` walks RUNNING
  nodes, reconciles them against their (durable) jobs, and re-advances the
  instance — completed nodes are never re-executed.
- Deterministic job ids make double-dispatch after a crash a no-op: an
  existing queued/running job is reused, a finished one is reconciled.
- Workflow events are published with the instance's owner, and workflow
  control operations (pause/resume/cancel/approval decisions) verify owner
  scope at the storage boundary.
- Compensation: when an instance FAILS or is CANCELLED, SUCCEEDED nodes that
  declare `compensation` config get best-effort compensation jobs
  (`workflow_compensation` payload kind).

## 6. Dynamic generation

The Core Agent can register definitions from documents
(`register_definition_document` / the `eurus autonomy workflows define` CLI).
Because registration runs the same validator as execution, generated graphs
are structurally safe before any node runs; permission scopes are enforced
at execution time by the policy layer (allowed/blocked tools per node or
per specialist).
