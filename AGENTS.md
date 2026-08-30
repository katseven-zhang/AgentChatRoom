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
  - `OBSERVE`: read-only inspection; call `room_join`, then `room_sync`, but do not claim tasks or acquire leases.
  - `COORDINATE`: repository changes or multi-Agent work; join and sync before work, use tasks and file leases, publish decisions or blockers, then submit evidence before declaring completion.
- Before inspecting or editing this repository in `OBSERVE` or `COORDINATE`, call `room_join` for the checkout. Local stdio resolves `.agentchatroom/project.json`; Agents do not supply a `project_key`. Keep the MCP/Bridge process alive, then call `room_sync`. Do not begin project work while disconnected.
- One installed Agent application represents one durable software identity in a Project. The MCP configuration injects that identity through validated `AGENTCHATROOM_SOFTWARE_KEY`, `AGENTCHATROOM_SOFTWARE_NAME`, and `AGENTCHATROOM_SOFTWARE_CLIENT` values; Agents must not supply, rename, infer, or invent an `agent_key` for a task, review, subtask, or runtime check. The backend generates the database identity.
- One software identity may have only one active Session. Rejoining replaces the previous Session and transfers unfinished owned tasks, active leases, pending targeted assignments, and pending handoffs. Roles such as executor, reviewer, coordinator, and integrator belong to Session/Task context and never create another Agent identity.
- Independent verification requires a different software identity. A Codex execution, Codex subtask, or alias such as Codex Review is still Codex and cannot independently approve Codex work.
- `room_join.model` is required initial Session metadata, not the authoritative model for later messages. Use the exact client model code when available; otherwise explicitly use `unknown`. Never guess or pin a model name in project rules.
- Every Agent-authored `message_post` must include `model_display_name` using the exact model label currently shown in the client UI for that response. If the client exposes no model label, use `unknown`. The Room stores this value on that immutable message instead of inferring it from the Agent Session.
- The stdio MCP or remote Bridge process owns connection Presence.
  `session_heartbeat` only refreshes connection liveness; Task events record
  claimed, in-progress, blocked, reported, reviewed, and completed work. Do not
  use `room_sync` as a timer or manually claim real-time working/idle state.
- Treat `project_id`, `session_id`, Session Token, cursor, online state, tasks, and leases as live MCP data. Never persist those values here as current facts.
- Completion and independent verification are separate. A reviewer must return `approved` or `changes_requested` with evidence.
