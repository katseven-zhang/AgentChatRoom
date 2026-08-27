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
  `.agentchatroom/` directory. When no explicit Project key is configured, derive a
  stable default from the canonical repository directory name.
- Select one mode before project work:
  - `OFF`: the request is unrelated to this workspace; do not call AgentChatRoom.
  - `OBSERVE`: read-only inspection; use the locally configured Project key, call `room_join`, then `room_sync`, but do not claim tasks or acquire leases.
  - `COORDINATE`: repository changes or multi-Agent work; join and sync before work, use tasks and file leases, publish decisions or blockers, then submit evidence before declaring completion.
- Before inspecting or editing this repository in `OBSERVE` or `COORDINATE`, resolve the Project key from local ignored configuration, call `room_join`, keep the MCP/Bridge process alive, then call `room_sync`. Do not begin project work while disconnected.
- Every Agent must use a stable, project-scoped `room_join.agent_key` across task executions, for example `codex-main`, `workbuddy-main`, or `grok-build-main`. A new execution may create a new Session, but it must not invent a new Agent identity.
- `room_join.model` is required initial Session metadata, not the authoritative model for later messages. Use the exact client model code when available; otherwise explicitly use `unknown`. Never guess or pin a model name in project rules.
- Every Agent-authored `message_post` must include `model_display_name` using the exact model label currently shown in the client UI for that response. If the client exposes no model label, use `unknown`. The Room stores this value on that immutable message instead of inferring it from the Agent Session.
- The stdio MCP or remote Bridge process owns background Presence. Use `session_heartbeat` only when changing semantic state (`idle`, `working`, or `blocked`); do not use `room_sync` as a timer.
- Treat `project_id`, `session_id`, Session Token, cursor, online state, tasks, and leases as live MCP data. Never persist those values here as current facts.
- Completion and independent verification are separate. A reviewer must return `approved` or `changes_requested` with evidence.
