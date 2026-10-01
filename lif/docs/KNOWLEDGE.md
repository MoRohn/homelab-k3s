# Persistent knowledge layer

The Local Intelligence Fabric keeps **typed, linked, versioned knowledge** next to the models it runs.

- Decisions keep their evidence, assumptions and alternatives.
- Assumptions keep track of what depends on them.
- Agent work leaves structured traces that the next session resumes from.

None of it lives in chat history.

| | |
|---|---|
| Canonical store | Typed Markdown and YAML type definitions in **agent repos** (Git): `lif/knowledge/` and `private/knowledge/` *(private, local only)* |
| Index | SQLite under `lif/knowledge/.knowledge/` (git-ignored). Derived: `knowledge rebuild` recreates it |
| Code | `lif/lif/knowledge/` |
| Interfaces | `knowledge` CLI · MCP server (`.mcp.json`) · HTTP `/v1/knowledge/*` · Control Center Knowledge pages · Python (`Knowledge.open()`) |
| Tests | `tests/test_knowledge.py`: acceptance tests A–P, the full loop, shipped-package type and skill tests |

## Quickstart

```bash
cd lif && .venv/bin/pip install -e .         # adds the `knowledge` command next to `local-ai`
knowledge --root knowledge status            # health (root defaults to $LIF_KNOWLEDGE_ROOT or lif/knowledge)
knowledge --root knowledge why cpu-tiers-for-local-inference
knowledge --root knowledge context "change the gateway fallback" --project local-intelligence-fabric
knowledge --root knowledge session resume local-intelligence-fabric
knowledge --root knowledge validate          # exit 1 on error diagnostics
```

Claude Code picks up the MCP server from `.mcp.json` at the repo root, as agent `claude-code`. For Codex or other MCP clients, run
`lif/.venv/bin/knowledge --root lif/knowledge mcp --agent <name>` over stdio. Use the console script: from the repo root,
`python -m lif.…` resolves `lif` to the top-level `lif/` directory instead of the package.

## Architecture

```
agent repos (Git, typed Markdown) ──► compiler ──► SQLite index ──► graph · search · context · views
   repos/<project>/                    parse (incremental, per-file cache)        │
   registry/<pkg>/<ver>/               type check (namespaces, inheritance)       ├─► CLI  knowledge …
   private/knowledge/<repo>/           resolve [[links]] (cross-repo, typed)      ├─► MCP  knowledge mcp --agent NAME
                                       semantic rules → diagnostics, review queue ├─► API  /v1/knowledge/* (lif.knowledge.app)
LIF controller /v1/activity ──► event sink ──► private events repo               └─► Control Center Knowledge pages
Decision Fabric ◄── knowledge decisions (profile, passage type, review priority, duplicates)
```

| Module | Job |
|---|---|
| `types.py` | Type system: `*.type.yaml`, namespaces (`pkg::type`), `extends`, field grammar, compatibility diff |
| `parse.py` | Typed Markdown: front matter, `[[id]]` `[[pkg::id]]` `[[id^sub]]` `[[^sub]]`, embedded records, `^pN` passages |
| `repo.py` | Agent repos (`repo.yaml`), workspace, local registry, semver resolution, `knowledge.lock` hashes |
| `compiler.py` | Incremental compile, reference resolution, diagnostics K001–K023, reconsideration queue |
| `store.py` | SQLite schema, FTS5, recursive-CTE traversal |
| `graph.py` | `get` · `why` · `lineage` · `impact` · `trace_claim` · `decision_history` · `find` · `health` · semantic `diff` |
| `search.py` | Hybrid retrieval: BM25 + graph importance + recency + pinning (+ optional embeddings) |
| `context.py` | Context assembly with budget, profiles and provenance · `checkpoint` · `resume` · `bootstrap` |
| `writer.py` | Validated writes. A write that adds an error on the object is rolled back |
| `packages.py` | Install, update with compatibility analysis, lock, publish, extract, migrations, type tests |
| `skills.py` / `views.py` | Skill checks and tests · views and apps over the shared graph |
| `learn.py` | Incident → lesson → method revision → skill check · source ingestion and extraction |
| `events.py` / `sink.py` | Platform events → knowledge · sink for other LIF components (opt-in) |
| `decisions.py` | Knowledge decisions through the Decision Fabric (`config/decisions/knowledge.yaml`) |
| `mcp.py` / `app.py` / `cli.py` | Interfaces |

## Typed Markdown

```markdown
---
type: decision                      # or pkg::decision when ambiguous
status: accepted
date: 2026-10-01
evidence: ["[[src-gpu-scheduling-doc]]", "[[src-architecture-doc^p3]]"]   # a passage of a source counts as that source
assumptions: ["[[gpu-admission-too-small-for-llms]]"]
affects: ["[[fast-fallback-model]]"]                # inverse field: the target depends on this decision
review_when: ["[[gpu-tiers-question]]"]             # review is queued when the question is answered
grounds:                                            # embedded records (cite as [[^g1]])
  - {id: g1, source: "[[src-gpu-scheduling-doc]]", stance: supports, basis: measured}
---
Prose may cite [[other]], [[pkg::other]], or a passage [[source^p2]].
A paragraph that ends with a block anchor becomes a citable passage. ^p1
```

- The object id is `id:`, or the file name. A `SKILL.md` takes its directory name.
- A file without front matter is a `document`. Adoption is incremental: raw → indexed → typed → linked.
- Bare ids are accepted in reference-typed fields. `[[…]]` is preferred.
- Field type grammar: a scalar (`string text int float bool date url enum any map`) or a type reference, plus `[]` for a list or `?` for optional. Options: `required`, `min`, `max`, `values`, `inverse`, `propagates`, `acyclic`.
- Edge convention: `src → dst` means *src depends on dst*. Impact analysis is the reverse walk over `propagates` edges.

## Diagnostics

| Code | Severity | Meaning |
|---|---|---|
| K001 | error | Unknown type |
| K002 | error | Missing required field |
| K003 | error | Invalid enum value |
| K004 | error | Bad scalar value (date, int, list where one value is expected) |
| K005 | error (field) / warning (prose) | Broken reference |
| K006 | error | Wrong reference type: `INVALID REFERENCE … expected evidence, received task` |
| K007 | error | Duplicate id |
| K008 | error | Cardinality (`min` / `max`) |
| K009 | warning | Unknown field (types with `strict: true`) |
| K010 | error | Cycle through an `acyclic` field (for example `supersedes`) |
| K011 | info | Orphaned object |
| K012 | warning | Stale: `review_after` / `expires_after` passed, or past the type's `review_after_days` |
| K013 | warning | RECONSIDERATION REQUIRED (see below) |
| K014 | warning | Decision cites no evidence |
| K015 | info | Unverified claim (nothing supports it) |
| K016 | warning | Unresolved contradiction (supported and contradicted, no `resolution`) |
| K017 | warning | Captured source file changed since capture (`sha256`) |
| K018 | error | Package or dependency problem (unresolved, outside the constraint, lock hash mismatch) |
| K019 | error | Ambiguous type name (qualify it) |
| K020 | info | Optional repo absent (a public clone without `private/`) |
| K021 | warning | Evidence without a traceable origin |
| K022 | error | Parse error (YAML, id) |
| K023 | error | Cross-repo link to a repo that is not a dependency |

## Reconsideration (never auto-reversal)

| Trigger | Example |
|---|---|
| An object becomes `invalidated` / `challenged` / `superseded` / `deprecated` / `retracted` / `refuted` | An assumption is invalidated by an incident |
| New contradicting evidence (a grounding with `stance: contradicts`, or `contradicted_by`) | A load measurement contradicts "the primary workload is idle at night" |
| `review_when` met | A question listed there is answered |
| `refresh_when: [kind]` and an `event` of that kind newer than the object | `CONFIG_CHANGED`, `MODEL_PROMOTED` |

- Dependents are found by walking the reverse graph transitively.
- Every decision, task, deployment, method, skill, commitment… on that path is queued with its chain and a priority.
- A `review` object (`reviews`, `trigger`, `outcome`) closes the entry. Nothing else is edited.
- Priority is deterministic (type, dependents). `knowledge-review-priority` exists for the Decision Fabric.

## Decision hierarchy (who answers what)

| Level | Used for |
|---|---|
| Type system | Required fields, enums, reference types, cardinality |
| Deterministic graph rules | Dependents, contradictions, staleness, reconsideration, lineage, evidence traces, skill checks |
| Jev (Decision Fabric) | Context profile when keywords are not decisive · passage → object type during ingestion · review priority · likely duplicates |
| Local fast / reasoning model | Not wired in yet (see the last table) |
| Human | Reviews, approvals (installs, upgrades, migrations, deletes, publishing — enforced at MCP and HTTP) |

- Knowledge state is INTERNAL or CONFIDENTIAL, so under the default `privacy.jev_allowed: [PUBLIC]` the fabric answers with the **rules** in `lif/knowledge/decisions.py`.
- Allowing INTERNAL in `jev_allowed` lets Jev rank and classify internal knowledge.

## Storage

The decision record is `[[knowledge-index-sqlite]]`, with alternatives and rationale; `knowledge why knowledge-index-sqlite` shows it.

| Data | Where | Backed up by |
|---|---|---|
| Public knowledge (objects, types, methods, skills, views, packages) | Git working tree `lif/knowledge` | Git remote |
| Private knowledge (`lif-operations`: events, incidents, production numbers) | `private/knowledge` (git-ignored) | **Nothing yet** — see "Not built yet" |
| Index (objects, edges, FTS, diagnostics, reconsiderations) | `lif/knowledge/.knowledge/index.db`, or emptyDir in the cluster | Not backed up: rebuilt |
| Runtime logs (`query_log`, `audit`, `embeddings`, event cursor) | Same SQLite file | Not knowledge. Losing them loses usage counts and the cursor; events re-consume idempotently |
| Large artifacts, PDFs, datasets | Object storage (MinIO), referenced by `location` | MinIO backups |
| Operational telemetry | Prometheus, registry SQLite | Existing |

## Packages

| Command | What it does |
|---|---|
| `knowledge package list` / `search` / `inspect NAME` | Local registry `lif/knowledge/registry/<name>/<version>/` |
| `knowledge package install NAME@^1.0 --repo R --yes` | Adds the dependency, writes `knowledge.lock`, compiles. Rolled back if it adds errors |
| `knowledge package update NAME --repo R [--migrate] [--yes]` | Compiles the workspace against the candidate in memory first. Reports breaking type changes, objects that would break, migrations shipped |
| `knowledge package lock --repo R` | Pins version, content hash, type versions, skill versions and tool names |
| `knowledge package publish PATH --yes` | Copies a `kind: package` repo into the registry. Versions are immutable |
| `knowledge package test NAME` | Type tests: `tests/types/valid/*.md` compile clean; `invalid/<CODE>-*.md` raise CODE |
| `packages.extract()` | Turns project-proven methods and skills into a package source tree. Project-local links become `provenance` text |
| `knowledge migrate FILE [--apply --yes]` | Dry-run by default: rewrites in a scratch copy and reports diagnostics first |

Shipped packages, all at 1.0.0:

| Package | Contents |
|---|---|
| knowledge-governance | Epistemic types and the semantic-review method and skill |
| agent-workflows | Task, session, change… and the context profiles |
| ai-infrastructure-core | Infrastructure types, the review-change skill, system views |
| incident-response | The incident type and the incident-learning loop |
| gpu-operations | Admission method and GPU views |
| model-evaluation | Methodology, promotion requirements, evaluate / compare / canary skills, with tests |
| model-lifecycle | Manual-first promotion and the lifecycle chain |
| kubernetes-operations | Cluster and namespace types, the resources method |

## Agents

### MCP tools (`knowledge mcp`)

| Tool | Scope |
|---|---|
| `search_objects`, `get_object`, `query_graph`, `trace_relationship`, `get_dependencies`, `get_dependents`, `change_impact`, `get_evidence`, `why_decision`, `get_decision_history`, `run_diagnostics`, `list_reconsiderations`, `assemble_context`, `resume_session`, `list_skills`, `list_views`, `render_view`, `knowledge_health`, `suggest_improvements` | read:knowledge |
| `create_object`, `update_object`, `link_objects`, `record_review`, `checkpoint_session` | write:knowledge |
| `execute_skill` | execute:skills |
| `query_<name>`, one per saved query (`queries/*.query.yaml`) | read:knowledge |
| Resource `knowledge://bootstrap` | Project summary, manifest, locked dependencies, types in use, skills, tools, critical diagnostics, recent decisions, open work |

- Scopes per agent come from `workspace.yaml` → `permissions`. `write_repos` limits which repos an agent may write, on every write path.
- Calls are recorded in the `audit` table.
- Through MCP and HTTP, approval-gated operations (installs, upgrades, migrations, deletes, publishing) are refused with the command a human must run.
- The CLI trusts its caller (it runs as `human`). An agent with a shell can pass `--yes` itself, so the gate is at MCP and HTTP, not at the CLI.

### Context assembly (`knowledge context`, `assemble_context`)

- Seeds: explicit `[[refs]]` (3.0), caller pins (3.0), pinned objects (1.5), hybrid search (≤ 2.0).
- Expansion: two hops in both directions, ×0.5 per hop.
- Score: profile type weight × recency × criticality (challenged or open ×1.4, superseded ×0.5, accepted decision ×1.2).
- Reconsiderations touching the set are always included.
- Items are taken greedily until the token budget (≈ chars/4) is used.
- Every item carries provenance: key, repo@version, file:line, updated, updated_by, project, and the evidence keys it rests on.
- Every item carries a `data_class`: the object's field, else its repo's `data_class` in `repo.yaml`, else CONFIDENTIAL. The package carries the maximum. Callers that put context into decision state must raise that state's class to the package's `data_class` before anything can leave the box.
- Profiles (`profiles/*.profile.yaml`): engineering, research, operations, product, incident-response, model-evaluation.

### Sessions

| | |
|---|---|
| Start | `knowledge session resume PROJECT` / `resume_session`. Returns the last checkpoint, changes since it, decisions with their evidence, open tasks and questions, changed assumptions, pending reviews, diagnostics and recent artifacts |
| End | `knowledge session checkpoint PROJECT --summary … --decisions … --next …` / `checkpoint_session`. Writes a `session` object into the repo, linking the work |
| Context recovery rate | Share of resumed sessions whose checkpoint says `recovered: true`. Shown in `knowledge status` |
| Skills | `end-of-work-capture` and `session-resume` (agent-workflows) |

## Platform events → knowledge (`events.py`)

| Registry activity | Knowledge |
|---|---|
| `benchmark_completed` | `benchmark` (evidence), linked to the `model` |
| `state_changed` → PRODUCTION | `decision` (`kind: model-promotion`, evidence = latest benchmark) + `deployment` + `event MODEL_PROMOTED` |
| `state_changed` → REJECTED / FAILED / QUARANTINED | `model.state` + event |
| `alias_rollback` | `event MODEL_ROLLBACK`, and the affected deployment becomes `superseded` |
| `setting_changed` (behavior keys) | `event CONFIG_CHANGED` |

- Ids come from the activity sequence number, so re-consuming is idempotent.
- Writes go only to `events_repo` (`lif-operations`, private). Without one, the sink refuses.
- Run it with `knowledge events consume --registry-db PATH`, or with the service tail (`LIF_CONTROLLER_URL` + `LIF_EVENTS_ADMIN_KEY`).
- Other components record durable facts through `lif.knowledge.sink.KnowledgeSink`, which is off unless `LIF_KNOWLEDGE_SINK=1`.

## HTTP API (`lif.knowledge.app`, version 1)

| Endpoint | |
|---|---|
| `GET /v1/knowledge/health` · `/bootstrap` · `/types` · `/repos` | |
| `GET /v1/knowledge/objects?type=&status=&depends_on=&links_to=&where=…` · `/objects/{key}` | Structured query, one object |
| `POST /objects` · `PATCH /objects/{key}` · `POST /links` · `POST /reviews` | Validated writes (422 with diagnostics) |
| `GET /search?q=` · `/why/{key}` · `/lineage/{key}` · `/impact/{key}` · `/evidence/{key}` · `/history/{key}` | |
| `GET /diagnostics` · `/reconsiderations` | |
| `POST /context` · `GET /session/resume` · `POST /session/checkpoint` | |
| `GET /skills` · `POST /skills/{name}/run` · `GET /views` · `/views/{name}` · `GET/POST /queries` | |
| `GET /packages` · `/packages/{name}` · `POST /packages/{name}/check-update` | |
| `GET /model-context/{model}` | Knowledge-aware model refresh: prior benchmarks, incidents, lessons, decisions, constraints |

- Auth: admin keys act as human. Agent keys (`LIF_KNOWLEDGE_KEYS`, `name:key`) get their workspace scopes.
- The service also serves the Control Center at `/`.

## Deployment

| Step | Status |
|---|---|
| Local: CLI, MCP, `knowledge serve` | Works now |
| Cluster: `lif/deploy/k8s/knowledge/knowledge.yaml` | Written, **not** in `lif/kustomization.yaml` and **not applied**. It mounts the host Git tree, keeps the index on an emptyDir (rebuilt at start), runs as uid 1000 so files keep the owner's uid, and routes `lif.tiny-dgx.lan/v1/knowledge` to the service |

## Observability

| Metric | |
|---|---|
| `lif_knowledge_compile_seconds` | Compile duration (histogram) |
| `lif_knowledge_objects`, `lif_knowledge_diagnostics{severity}` | Graph size and health |
| `lif_knowledge_query_seconds`, `lif_knowledge_context_seconds` | Search and context-assembly latency |
| `lif_knowledge_mcp_call_seconds{tool}` | MCP calls |

Alerts are in the manifest: `LIFKnowledgeErrors`, `LIFKnowledgeCompileSlow`.

## Measured (2026-10-01, this DGX, CPU)

| Workspace | Operation | Result |
|---|---|---|
| Dogfood (64 objects, 39 embedded, 113 edges) | Full compile | median 117 ms (n=5) |
| | No-op incremental compile | median 58 ms (n=5) |
| | Context assembly | median 8.6 ms (n=5) |
| | Session resume | median 3.2 ms (n=5) |
| Synthetic (6,004 objects, 2,000 passages, 10,000 edges) | Full compile | 1.39 s (n=1) |
| | One-file change (recompile) | 0.34 s (n=1) |
| | Search | 16 ms (n=1) |
| | Context assembly | 42 ms (n=1) |
| | Impact walk | 1 ms (n=1) |
| Context eval (`knowledge eval`, 4 cases) | Critical recall | 1.0 |
| | Mean precision | 0.42 |
| | Mean tokens | 1,755 (n=1) |

## Recovery

| Scenario | Procedure | Tested by |
|---|---|---|
| Index lost or corrupt | Delete `.knowledge/index.db*`, then `knowledge rebuild` (or just restart: open compiles) | `test_N_graph_rebuilds_from_canonical_repos` (identical objects, edges and diagnostics; corrupt file) |
| Registry package edited in place | K018 lock hash mismatch. Restore from Git, or publish a new version | `test_lock_hash_detects_modified_package` |
| Bad schema migration | Dry run first (scratch copy plus diagnostics); after apply, `git checkout` the files | `test_I_migration_dry_run_then_apply` |
| Broken package dependency | K018 / K023 diagnostics name the repo and the fix | `test_H_*` |
| Event cursor lost | Re-consume from 0: deterministic ids make it idempotent | `test_L_*` |
| Offline (no internet, no gateway) | Search drops to lexical + graph; decisions use rules | `test_O_usable_without_internet` |
| K3s restart / DGX reboot | Nothing to do locally. In the cluster the pod rebuilds its index from the mounted repos | Deployed 2026-10-01; not yet tested across a restart |

## Acceptance tests

| Test | pytest |
|---|---|
| A: typed decision linked to evidence and assumptions | `test_A_typed_decision_linked_to_evidence_and_assumptions` |
| B: missing required field → diagnostic | `test_B_missing_required_field_is_a_diagnostic`, `test_B_writer_rejects_a_write_that_adds_errors` |
| C: wrong reference type → diagnostic | `test_C_wrong_reference_type_is_a_diagnostic`, `test_C_enum_unknown_type_duplicate_cycle` |
| D: later session reconstructs why | `test_D_later_session_reconstructs_why` |
| E: assumption change → dependent decisions | `test_E_changing_an_assumption_finds_dependent_decisions` |
| F: claim → source evidence | `test_F_trace_claim_to_source_passage`, `test_F_contradiction_is_surfaced_not_resolved` |
| G: skill installed from a package | `test_G_install_skill_from_package` |
| H: cross-repo relationships | `test_H_cross_repo_links_resolve_with_types_and_versions` |
| I: package update finds incompatible schema changes | `test_I_package_update_detects_incompatible_schema_changes`, `test_I_migration_dry_run_then_apply` |
| J: MCP agent queries project knowledge | `test_J_mcp_agent_can_query_project_knowledge` (spawns the server, speaks JSON-RPC) |
| K: view and agent read the same objects | `test_K_view_uses_same_graph_objects_as_agent` |
| L: model promotion → knowledge | `test_L_model_promotion_generates_knowledge` (real `Registry` activity) |
| M: GPU incident → lesson → method → check | `test_M_gpu_incident_creates_lesson_method_change_and_check` (synthetic incident fixture) |
| N: rebuild from canonical data | `test_N_graph_rebuilds_from_canonical_repos` |
| O: usable without internet | `test_O_usable_without_internet` |
| P: resume in a fresh process | `test_P_fresh_process_resumes_without_transcript` |
| Full loop (§112) | `test_full_loop_work_to_reusable_method`: source → extraction → assumption → decision → work → contradicting evidence → reconsideration → review → lesson → method → skill → package → reuse in a second project |

## Not built yet

| Spec area | State | Next step |
|---|---|---|
| Local-LLM extraction and semantic review (§19, §58 step 5) | Extraction uses rules / Jev passage classification only. Semantic review is a method and skill with deterministic checks | Add a `local_llm` fallback to `knowledge-object-type`; a reviewer that reads passages with `local/default` |
| Workspace editor, graph canvas, custom app runtime (§43, §44, §80) | UI pages list, search, inspect, trace lineage and impact, and record reviews. Apps are YAML bundles of views; no in-browser editor or graph canvas | Editor that writes through `PATCH /objects` |
| Knowledge-aware discovery and Jev (§65, §66) | `/v1/knowledge/model-context/{model}` exists | Call it from the discovery screening state (task `discovery-uses-model-history`) |
| Event sink live (§62, §64) | Code and tests done | Deploy the service and give it `LIF_EVENTS_ADMIN_KEY` (task `enable-event-sink`) |
| Semantic history above Git (§51) | `knowledge diff REV` compiles a Git revision and diffs semantically. There is no per-object history browser | `knowledge history KEY` over `git log -- path` |
| Object storage for large sources (§53) | Ingestion copies originals into `sources/originals/` | MinIO upload for files > 1 MiB, with `location: s3://…` |
| Cluster deployment (§91 restore drill) | Manifest written, not applied | Owner's go-ahead |
| External package sharing (§77) | Local registry only, by design | Later |
| Backup of `private/knowledge` (§91) | Not backed up by anything in this repo. It is git-ignored, and the offsite job covers MinIO buckets, not the host tree | Add it to an existing private backup, or make it a private Git repo with its own remote |
| Read scoping per repo (§87) | Scopes and `write_repos` are enforced. Any agent with `read:knowledge` reads every repo in the workspace, including private ones | Filter graph, search and context by `permissions.<agent>.repos` |
| IDE integration for diagnostics (§8) | CLI (`knowledge validate`, file:line output), API and MCP only | An LSP shim or problem-matcher over `knowledge diagnostics --json` |
| Workspace search across views, packages and tools (§81) | `search` covers objects (skills are objects). Views, packages and saved queries are listed, not searched | Add them to the FTS index |
