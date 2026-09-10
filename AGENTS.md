# AgentChatRoom Development Rules

- Public product, setup, usage, architecture, security, and development guidance lives in the root `README.md`. The entire `docs/` directory is local-only and must remain ignored by Git.
- Implement only behavior aligned with the active public product scope in `README.md`; local notes under `docs/` are not a versioned product contract.
- Treat standardization, configuration, and no environment-specific hardcoding as mandatory engineering principles, not optional cleanup.
- Core domain logic must not depend on a specific agent vendor, model, project path, port, role name, Agent count, or UI theme.
- Runtime and policy values must come from validated configuration with documented defaults and precedence. Never embed user paths, secrets, ports, vendor names, or deployment-specific values in business code.
- Use shared, versioned domain models and state definitions across REST, MCP, CLI, persistence, and Web adapters. Adding an Agent, vendor, role, or project must not require a core-logic branch or a frontend rebuild.
- Reuse the same domain service from REST, MCP, and CLI adapters.
- Every behavior change must update `README.md` when public behavior changes and add or update tests.
- Treat task completion and independent verification as separate states.
- Preserve append-only event history; derived state may change, historical events may not be rewritten.
- Keep proof from isolated upstream POCs separate from proof for the AgentChatRoom mainline.

## AgentChatRoom project coordination

- Coordination is opt-in per checkout. This public file never contains a live Project,
  `project_key`, Session, Token, cursor, task, lease, or online-state snapshot.
- Local coordination settings and all Room data belong under the ignored
  `.agentchatroom/` directory. The backend/MCP generates an opaque Project key and
  writes the stable checkout registration to `.agentchatroom/project.json`;
  Agents must not edit it, supply a key or `logical_path`, infer identity from it,
  or invent another identity. For an explicit user-managed monorepo subproject,
  select the actual subdirectory as `project_path`; the backend derives its
  repository-relative logical path. When the file and database scope are both empty,
  the first Agent may ask the backend to create the Room.
- Select one mode before project work:
  - `OFF`: the request is unrelated to this workspace; do not call AgentChatRoom.
  - `OBSERVE`: read-only inspection; call `room_bootstrap` once, then inspect. Do not claim tasks or acquire leases.
  - `COORDINATE`: repository changes or multi-Agent work; call `room_bootstrap` once before work, use tasks and file leases, publish decisions or blockers, then submit evidence before declaring completion.
- Before inspecting or editing this repository in `OBSERVE` or `COORDINATE`, call `room_bootstrap` once. Local stdio resolves `.agentchatroom/project.json`; Agents do not supply a `project_key`. Keep the MCP/Bridge process alive. Presence from MCP startup is not conversation sync. Do not begin project work while disconnected.
- One installed Agent application represents one durable software identity in a Project. The MCP configuration injects that identity through validated `AGENTCHATROOM_SOFTWARE_KEY`, `AGENTCHATROOM_SOFTWARE_NAME`, and `AGENTCHATROOM_SOFTWARE_CLIENT` values; Agents must not supply, rename, infer, or invent an `agent_key` for a task, review, subtask, or runtime check. The backend generates the database identity.
- One software identity may have multiple active Sessions for parallel conversations in the same or different Projects. Each Session keeps its own task and lease ownership; joining never closes another Session or silently transfers work. Roles such as executor, reviewer, coordinator, and integrator belong to Session/Task context and never create another Agent identity.
- Independent verification requires a different software identity. A Codex execution, Codex subtask, or alias such as Codex Review is still Codex and cannot independently approve Codex work.
- Optional `room_bootstrap.model` is initial Session metadata, not the authoritative model for later messages. Use the exact client model code when available; otherwise explicitly use `unknown`. Never guess or pin a model name in project rules. `room_join` remains a compatibility entry for empty repository scope.
- Every Agent-authored `message_post` must include `model_display_name` using the exact model label currently shown in the client UI for that response. If the client exposes no model label, use `unknown`. The Room stores this value on that immutable message instead of inferring it from the Agent Session.
- The stdio MCP or remote Bridge process owns connection Presence.
  `session_heartbeat` only refreshes connection liveness; Task events record
  claimed, in-progress, blocked, reported, reviewed, and completed work. Do not
  use `room_sync` as a timer or manually claim real-time working/idle state.
- Treat `project_id`, `session_id`, Session Token, cursor, online state, tasks, and leases as live MCP data. Never persist those values here as current facts.
- Completion and independent verification are separate. A reviewer must return `approved` or `changes_requested` with evidence.

<!-- BEGIN AgentChatRoom managed coordination -->
## AgentChatRoom project coordination

- enabled: true
- Project name: `agentchatroom`. Treat this exact name as the Room identity for this workspace; do not select a similarly named Project.
- The backend owns the opaque Project key and keeps the checkout registration in ignored `.agentchatroom/project.json`. Agents must not edit it, supply a key, infer identity from it, or invent another key.
- Select one mode before project work:
  - `OFF`: the request is unrelated to this workspace; do not call AgentChatRoom.
  - `OBSERVE`: read-only inspection; bootstrap and sync, but do not claim tasks, acknowledge assignments, acquire leases, or write Room state.
  - `COORDINATE`: repository changes or delegated/review work; follow the workflow below and keep Room state current.
- At the start of every new Agent conversation in this workspace, call `room_bootstrap(project_name="agentchatroom")` once. Verify the returned Project name is exactly `agentchatroom` and the returned workspace/root matches this checkout. Do not begin project work while disconnected. If bootstrap fails or selects another Project, stop all Room writes and follow only its `required_action`.
- After bootstrap, call `room_sync` to read recent messages and current coordination state. Inspect targeted messages, assigned tasks, handoffs, and tasks awaiting independent review before starting uncoordinated work. Use `room_sync` again after a disconnection or when fresh Room state is required; do not poll it as a timer.
- For an assigned task, inspect it with `task_get` or `task_get_by_number`, then use `task_acknowledge`. Use `task_claim` for an unowned eligible task. If this conversation must resume unfinished work left by a disconnected Session of the same software identity, call `task_claim(reclaim=true)` explicitly; reclaim is rejected while the owner is connected or belongs to another identity. Before editing shared files, acquire an appropriate `lease_acquire` path lease; release it with `lease_release` when the work or handoff ends.
- Post concise progress, decisions, and blockers with `message_post`; acknowledge targeted requests with `message_acknowledge` when appropriate. Every Agent-authored message must include `model_display_name` using the exact model label shown by the client, or `unknown` when the client exposes none.
- Keep task state accurate with `task_update`. When implementation is ready, submit `work_report` with changed files, checks, and concrete evidence. A Work Report requests verification; it does not complete independent review.
- When accepting a review task, first confirm the implementation was produced by a different software identity. Read the task contract and Work Report, inspect the actual diff/artifacts, run independent checks, then call `review_submit` with `approved` or `changes_requested` and criterion-level evidence. The implementing identity may not approve its own work. Use `integration_submit` only after required verification and integration are actually complete.
- Use the MCP tool schemas and structured error `required_action` as the protocol contract. Do not inspect AgentChatRoom source code or guess IDs/arguments to discover how the Room works. If a required tool is unavailable, stop and report that connection/tooling problem.
- One installed Agent application is one durable software identity in this Project. The MCP configuration injects that identity; Agents must not supply, rename, or invent an `agent_key` for a task, review, or runtime check.
- A software identity may keep multiple active Sessions in the same or different Projects, including parallel conversations. Each Session keeps its own task and lease ownership; joining a new Session never closes another Session or silently transfers work.
- Optional `room_bootstrap.model` is initial Session metadata, not the authoritative model for later messages. Use the exact client model code when available; otherwise explicitly use `unknown`. Never guess or pin a model name in project rules.
- MCP connection Presence and task progress are different facts. `session_heartbeat` only refreshes connection liveness; Task events record claimed, in-progress, blocked, reported, reviewed, and completed work. Do not use heartbeats to represent task progress.
- Treat `project_id`, `session_id`, Session Token, cursor, online state, tasks, and leases as live MCP data. Never persist those values here as current facts.
- Completion and independent verification are separate. A reviewer must return `approved` or `changes_requested` with evidence.
<!-- END AgentChatRoom managed coordination -->
