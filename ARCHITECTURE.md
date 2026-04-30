# agentchattr Architecture

This document describes the current architecture and functionality of this fork.
It is based on the implementation in the repository, not only the README.

## Purpose

agentchattr is a local coordination server for humans and AI coding agents. A
browser chat UI, CLI wrappers, and MCP tools share a single local state store so
humans and multiple agents can talk in channels, mention each other, work in job
threads, and run structured multi-agent sessions.

The core loop is:

1. A human or agent posts a chat message.
2. The server persists the message and broadcasts it to connected browsers.
3. The router detects `@agent` mentions and resolves them to concrete runtime
   instances.
4. The server writes trigger records into per-agent queue files.
5. Wrappers watching those queue files inject prompts into agent CLIs.
6. Agents read context and post responses through MCP tools.

## Runtime Topology

The main process is started by `run.py`, usually through the platform scripts in
`macos-linux/` or `windows/`.

Runtime services:

- FastAPI web server on `server.port` from `config.toml`, default `8300`.
- WebSocket endpoint at `/ws` for live browser updates.
- REST API under `/api/*` for browser actions and wrapper lifecycle calls.
- MCP streamable HTTP server on `mcp.http_port`, default `8200`.
- MCP SSE server on `mcp.sse_port`, default `8201`.
- Background threads for wrapper recovery, presence/activity expiry, crash
  deregistration, and scheduled messages.

Important entry points:

- `server_entry.py` launches `run.main()` in a fresh interpreter and defaults
  `AGENTCHATTR_NETWORK_CONFIRM=YES`.
- `run.py` loads config, creates the server session token, configures `app.py`,
  wires the MCP bridge to the same stores, starts MCP servers in background
  threads, mounts static files, injects the browser session token, and starts
  Uvicorn.
- `app.py` owns the FastAPI app, global stores, routing, WebSocket protocol,
  REST API, auto-start logic, and broadcast helpers.
- `download_links.py` scans outgoing chat messages for local file paths and
  mints short-lived download tokens for allowlisted files.
- `mcp_bridge.py` exposes MCP tools used by agents.
- `wrapper.py` runs real CLI agents and injects wake prompts into them.
- `wrapper_api.py` runs local OpenAI-compatible API agents without a terminal UI.

## Configuration

`config_loader.py` loads local `config.toml` and optionally merges
`config.local.toml`. If `config.toml` is missing, it is created from the
tracked `config.toml.example` template.

Only the `[agents]` section is merged from `config.local.toml`; local entries are
added only if they do not override built-in agent names. This prevents local
config from replacing the default Claude, Codex, Gemini, and Kimi definitions.

Main `config.toml` sections:

- `[server]`: host, port, data directory, and allowed origins.
- `[agents.*]`: command, working directory, display label, color, and optional
  MCP injection settings or API-agent settings.
- `[routing]`: default routing mode and max agent-to-agent hops.
- `[mcp]`: HTTP and SSE MCP ports.
- `[images]`: upload directory and max upload size.
- `[downloads]`: ephemeral download link behavior for local paths mentioned by
  agents, including enablement, allowlisted roots, token lifetime, max file
  size, and max links per message.

The example config binds the web server to `0.0.0.0`. `run.py` treats any
non-localhost host as risky and requires `--allow-network` plus confirmation.
Launcher scripts set confirmation automatically for convenience. The generated
local `config.toml` is ignored so machine-specific origins, ports, and agent
settings do not get committed.

## Server Initialization

`app.configure()` builds all server-side runtime components:

- `MessageStore` for JSONL chat persistence.
- `RuleStore` for shared agent rules.
- `SummaryStore` for per-channel summaries.
- `JobStore` for bounded job conversations.
- `ScheduleStore` for recurring or one-shot scheduled prompts.
- `RuntimeRegistry` for live agent instances.
- `ChannelBindings` for channel-to-agent-instance mappings.
- `Router` for mention parsing and loop guard state.
- `AgentTrigger` for queue-file trigger writes.
- `SessionStore` and `SessionEngine` for structured workflows.
- `ServerLauncher` for tmux-backed background wrapper startup on Unix-like
  systems.
- `DownloadLinkService` for ephemeral local artifact links generated from
  message text at client delivery time.

`configure()` also installs security middleware, loads persisted room settings
and hats, registers store callbacks, starts background maintenance threads, and
starts the schedule runner.

## Browser Client

The web UI is static HTML/CSS/JS served from `static/`.

Frontend modules:

- `static/index.html` defines the app shell: header, settings, channel bar,
  session bar, timeline, jobs panel, rules panel, composer, slash menus, and
  modals.
- `static/chat.js` is the main client controller. It connects the WebSocket,
  renders messages, handles settings, mentions, slash commands, attachments,
  ephemeral download cards, todos/pins, notification sounds, schedule UI, voice
  input, deletion, image lightbox, and status pills.
- `static/channels.js` handles channel tabs, create/rename/delete flows, channel
  switching, and channel-local visible history.
- `static/jobs.js` handles the jobs panel, job conversations, job messages,
  proposal cards, drag reorder, suggestions, job attachments, and job unread
  state.
- `static/rules-panel.js` handles rule creation, editing, status changes,
  archive/trash behavior, reminders, and proposal resolution.
- `static/sessions.js` handles session templates, active-session bar, cast
  previews, custom session drafts, draft revision flow, session launch/end, and
  output highlighting.
- `static/core.js` provides a small global `Hub` event bus.
- `static/store.js` provides a small global `Store` reactive state wrapper.

The browser receives a session token injected into the HTML by `run.py`, then
passes it in the WebSocket query string and `X-Session-Token` headers. If the
server restarts and the token changes, `/ws` closes with code `4003`; the client
reloads to fetch fresh HTML and a fresh token.

## WebSocket Protocol

`/ws` sends initial state after token validation:

- room settings
- agent config and base colors
- todos
- rules
- hats
- jobs
- schedules
- pending agent instances
- recent message history per channel
- per-channel history pagination state
- current status

Then it accepts browser events:

- `message`: persist a chat message, optional attachments and reply target.
- `delete`: delete selected timeline messages.
- `todo_add`, `todo_toggle`, `todo_remove`: manage pinned/todo state.
- rule events: propose, activate, deactivate, edit, delete, remind.
- `update_settings`: update room title, username, font, contrast, history
  limit, loop guard, and rule refresh interval.
- `rename_agent` and `name_pending`: human-managed instance identity changes.
- `channel_create`, `channel_rename`, `channel_delete`: channel management.

Server-to-browser events are JSON objects with a `type` field. Key event types
include `message`, `status`, `typing`, `settings`, `agents`, `base_colors`,
`todos`, `todo_update`, `rules`, `rule`, `jobs`, `job`, `schedules`,
`schedule`, `session`, `hats`, `delete`, `edit`, `clear`, `agent_renamed`,
`channel_renamed`, `history_state`, and `pending_instance`.

Before chat messages are sent to browsers, `app._decorate_message_for_client()`
may attach transient `metadata.downloads` entries. These entries are generated
for the current client delivery and are not persisted in the JSONL log.

## Message Storage

`store.py` implements `MessageStore`.

Storage model:

- Active log file: `data/agentchattr_log.jsonl`.
- Legacy fallback: `data/room_log.jsonl` if the new log does not exist.
- Each message has monotonically increasing `id`, `sender`, `text`, `type`,
  `timestamp`, display `time`, `attachments`, and `channel`.
- Optional fields include `reply_to` and `metadata`.
- Todos/pins are stored in `data/todos.json`.

Clear behavior is non-destructive. `/clear` inserts an internal
`clear_marker`, and read methods hide messages before the latest marker for
that channel. When the active JSONL file exceeds 5 MB, hidden pre-marker
messages are archived into numbered JSONL files.

Message deletion is destructive for selected message IDs. It rewrites the JSONL
file, removes associated todos, and deletes uploaded attachment files referenced
by deleted messages.

## Routing and Triggers

`router.py` parses mentions and enforces a per-channel loop guard.

Routing behavior:

- Human messages reset that channel's hop count and unpause routing.
- Agent messages only route when they explicitly mention another target.
- `@all` and `@both` expand to all known agent names.
- Agent-to-agent hops increment the channel hop counter.
- If the counter exceeds `max_agent_hops`, the channel is paused until a human
  uses `/continue`.

`app._handle_new_message()` is the central message pipeline. It broadcasts the
message, handles slash commands, transforms session draft blocks, computes
routing targets, checks session turn guards, optionally auto-starts agents, and
queues triggers.

`AgentTrigger` writes JSONL records to `data/{agent_name}_queue.jsonl`. Queue
entries include sender text, channel, optional custom prompt, optional direct
injection text, and optional `job_id`.

## Agent Registry and Identity

`registry.py` implements `RuntimeRegistry`, the single source of truth for live
agent instances.

Concepts:

- Base family: configured agent name such as `claude`, `codex`, `gemini`, or
  `kimi`.
- Instance: concrete live identity such as `claude`, `claude-2`, or
  `claude-general`.
- Slot: numeric position in an agent family, used for derived color variants.
- Token: per-instance bearer token used by wrappers and MCP requests.
- State: `active` or `pending`.

Registration modes:

- Channel-scoped wrappers can request deterministic names like
  `{agent}-{channel}`.
- Manually launched wrappers use slot-based names. If a second instance appears,
  the first base instance can be renamed from `claude` to `claude-1` to avoid
  ambiguity.
- Deregistered names are reserved briefly to reduce slot theft after restarts.
- Rename chains are persisted to `data/renames.json` so heartbeats can follow
  identity changes.

`mcp_bridge.py` stores associated runtime identity state:

- presence timestamps
- activity flags and timestamps
- per-agent/per-channel read cursors
- roles
- rename suppression markers

Renames migrate presence, activity, cursors, roles, and historical message
senders.

## Multi-Instance and Channel Binding

`channel_bindings.py` persists channel-to-family mappings in
`data/channel_agent_bindings.json`.

When a base family is mentioned in a channel, routing prefers:

1. An existing deterministic channel instance, e.g. `codex-general`.
2. A persisted channel binding.
3. Unix auto-start of a new channel-scoped wrapper.
4. A registered base/manual instance as fallback.

This lets different channels use separate instances of the same provider,
preserving independent CLI context and enabling parallel work.

## Agent Wrappers

`wrapper.py` is the terminal-agent bridge.

Wrapper lifecycle:

1. Load shared config.
2. Register with `/api/register` and receive concrete name plus bearer token.
3. Configure MCP access for the provider.
4. Start a heartbeat thread that reports presence and detects server-side
   renames.
5. Start a queue watcher that polls `data/{name}_queue.jsonl`.
6. Start an activity monitor that reports screen/output changes.
7. Launch the provider CLI through the platform runner.
8. Deregister on shutdown.

Provider MCP injection:

- Claude uses `--mcp-config` with a generated config file.
- Gemini uses an environment variable pointing at a generated settings file.
- Codex uses a local per-instance MCP proxy and CLI `-c` config arguments.
- Kimi uses `--mcp-config-file`.
- Custom agents can use `settings_file`, `env`, `flag`, or `proxy_flag`.

Queue watcher behavior:

- Groups normal triggers by channel.
- Preserves direct CLI injections separately.
- Fetches current role and active rules before wake prompts.
- Re-sends rules on first trigger, rule epoch changes, or configured refresh
  intervals.
- Injects one prompt per triggered channel, usually:
  `mcp read #channel - you were mentioned, take appropriate action`.
- If a job trigger includes `job_id`, injects a job-specific read prompt.

Platform runners:

- `wrapper_unix.py` runs agents inside tmux sessions and injects text via
  `tmux send-keys`.
- `wrapper_windows.py` runs agents directly and injects key events through
  `WriteConsoleInputW`.
- Both report activity by detecting terminal screen changes.

`wrapper_api.py` supports local OpenAI-compatible model endpoints. It registers
like a normal instance, watches the same queue files, reads recent chat through
REST, calls `/v1/chat/completions`, and posts responses through `/api/send`.

## MCP Bridge

`mcp_bridge.py` starts two FastMCP servers with the same tool set:

- streamable HTTP for Claude/Codex-style clients
- SSE for Gemini-style clients

Registered tools:

- `chat_send`: post timeline or job-thread messages, optionally with local
  image attachment.
- `chat_read`: read recent/new messages with per-agent cursors, or read a job
  thread with job metadata.
- `chat_resync`: explicitly fetch full recent context and reset cursor.
- `chat_join`: post online/join message.
- `chat_who`: list online agents.
- `chat_rules`: list or propose rules.
- `chat_decision`: backward-compatible alias for `chat_rules`.
- `chat_channels`: list channels.
- `chat_set_hat`: set an agent avatar hat SVG.
- `chat_claim`: confirm or reclaim an identity in multi-instance setups.
- `chat_summary`: read or write per-channel summaries.
- `chat_propose_job`: post a job proposal card for human approval.

Authentication and identity:

- Direct MCP clients can send bearer auth headers from generated config.
- Proxy-based clients use `McpIdentityProxy`, which forwards the instance token
  and stamps the correct `sender` or `name` into tool calls.
- If an authenticated token is present, tool identity is resolved from the token
  and agents cannot impersonate another sender.
- Base family names are rejected when authentication is required or when
  multiple active instances would make sender identity ambiguous.

`chat_read` uses persisted cursors in `data/mcp_cursors.json`. First read returns
recent context; later reads return new messages since the last cursor. Repeated
empty reads return increasingly explicit anti-polling hints.

## MCP Identity Proxy

`mcp_proxy.py` is a local threaded HTTP proxy for per-instance identity.

It forwards HTTP/SSE MCP traffic to the real MCP server, adds bearer auth
headers, rewrites SSE callback endpoints back through the proxy, and injects
the wrapper's current agent identity into supported tool calls.

This is mainly useful for clients whose config mechanism cannot reliably attach
dynamic headers or identity parameters itself.

## REST API

Key REST endpoints in `app.py`:

- `POST /api/upload`: normalize and store uploads.
- `GET /api/messages`: recent or since-id message reads.
- `GET /api/messages/page`: older-page pagination for a channel.
- `POST /api/send`: authenticated API-agent send endpoint.
- `GET /api/status`: current agent availability/busy/role status.
- `GET /api/settings`: room settings.
- `DELETE /api/hat/{agent_name}`: remove an avatar hat.
- `GET/POST/DELETE/PATCH /api/schedules...`: schedule management.
- `GET/POST/PATCH/DELETE /api/jobs...`: job and job-message management.
- `GET/POST /api/roles...`: role reads and updates.
- `GET/POST /api/rules...`: rules, reminders, and freshness.
- `POST /api/register`: wrapper registration.
- `POST /api/deregister/{name}`: wrapper deregistration.
- `POST /api/label/{name}`: rename agent identity/label.
- `POST /api/heartbeat/{agent_name}`: presence and activity heartbeat.
- `GET /api/platform`: browser path-format helper.
- `POST /api/open-path`: open local paths in native file manager.
- `GET/POST/DELETE /api/sessions...`: session templates and runs.
- `GET /api/version_check`: latest-release notifier.
- `GET /uploads/{filename}`: serve uploaded artifacts.
- `GET /downloads/{token}`: serve an ephemeral local file download by token.

## Attachments

`attachment_processor.py` normalizes uploads into browser-safe metadata and
agent-consumable text.

Supported categories:

- Inline images: PNG, JPG/JPEG, GIF, WebP, BMP.
- SVG: accepted only as a downloadable file; inline rendering is disabled.
- Text and Markdown.
- Code/config files, wrapped in fenced Markdown blocks.
- DOCX converted from `word/document.xml`.
- PDF converted with PyMuPDF if available, then fallback text extraction.

Text-like uploads produce:

- original file in `uploads/`
- normalized `.md` file in `uploads/`
- inline `markdown_text` preview capped at 20,000 characters
- short summary
- optional advisory security warnings

`upload_security_scanner.py` is dependency-free and advisory only. It scans
extracted text for prompt-injection phrases, invisible/deceptive Unicode, long
base64-like strings, shell substitutions in non-shell files, and suspicious
dynamic execution in non-Python files.

## Ephemeral Downloads

`download_links.py` implements `DownloadLinkService`, which turns local paths
mentioned in normal chat messages into short-lived browser download links.

Pipeline:

1. A chat message is persisted normally, without rewriting its text.
2. When the message is broadcast, returned from REST history, or sent during
   WebSocket initialization, `app._decorate_message_for_client()` asks the
   service to inspect the text.
3. The service extracts quoted and bare path-looking substrings, including
   absolute paths, explicit relative paths, and slash-containing relative
   artifact paths such as `plans/reports/output.zip`. It resolves them relative
   to the repo root, configured download roots, and one child directory under
   each configured root, then accepts only existing regular files under
   `[downloads].allowed_roots`.
4. Sensitive filename patterns such as `.env`, private keys, credentials, and
   secrets are rejected by default.
5. For each accepted file, the service stores an in-memory token record with
   path, filename, MIME type, size, and expiry.
6. The browser renders `metadata.downloads` in a collapsed details box below
   the message.
7. `GET /downloads/{token}` validates the token and expiry, re-checks that the
   file still exists, and returns a `FileResponse` as an attachment.

Token records are in memory only. They disappear on server restart and are
pruned opportunistically when new links are generated or when expired tokens are
requested. History reloads can mint fresh tokens for still-eligible paths.

TTL is configured with `[downloads].base_ttl_seconds` plus
`[downloads].extra_ttl_per_10mb_seconds` for each complete 10 MB of file size.

## Jobs

`jobs.py` implements bounded work conversations.

Job data is persisted in `data/jobs.json`. Each job contains title, body,
status, channel, creator, assignee, anchor message, sort order, timestamps, and
an embedded message list.

Supported behavior:

- Humans can create jobs manually.
- Agents can propose jobs through `chat_propose_job`; the UI renders proposal
  cards that humans can accept or dismiss.
- Timeline messages can be converted to job proposals by triggering an agent to
  rewrite the referenced message as a proposal.
- Job conversations are separate from the main timeline.
- Mentions inside a job thread trigger agents with `job_id` context.
- Agents can prefix job replies with `[suggestion]` to create Accept/Dismiss
  suggestion cards.
- Jobs can be reordered inside a status lane.

Status values in storage are `open`, `done`, and `archived`. The UI labels them
as To Do, Active, and Closed respectively.

## Rules

`rules.py` implements shared working-style rules.

Rules are persisted in `data/rules.json` with an epoch number. Active-rule
changes bump the epoch. Wrappers fetch active rules and inject them into wake
prompts when needed.

Rule states:

- `pending`: agent proposal awaiting human action.
- `draft`: saved but inactive.
- `active`: injected into agent prompts.
- `archived`: inactive archive.

Limits:

- Rule text: 160 chars.
- Reason: 240 chars.
- Active rules: 10 hard cap.
- UI soft warning at 7 active rules.
- Total rules: 50.

Agents can list and propose rules through MCP. Activation, editing, archiving,
and deletion are reserved for humans via the UI/API.

## Sessions

Sessions orchestrate structured multi-agent workflows with templates, roles,
phases, and turn-taking.

`session_store.py` persists runs in `data/session_runs.json`, loads built-in
templates from `session_templates/`, and stores custom templates in
`data/custom_templates.json`.

Built-in templates:

- Code Review
- Debate
- Design Critique
- Planning

`session_engine.py` observes new chat messages and advances session state when
the expected participant responds.

Session behavior:

- One active/waiting/paused session per channel.
- A template defines roles and sequential phases.
- A cast maps roles to agents or humans.
- The engine triggers the current agent with a phase-specific prompt.
- Human interruption while an agent is expected pauses the session.
- The expected agent's chat response advances the turn or phase.
- Phase banners are posted into the timeline.
- The output phase's message can be marked as the session output.
- Active sessions resume after server restart only if they were in `active`
  state; `waiting` sessions are not retriggered to avoid duplicates.

Agents can propose custom session templates by posting a fenced `session` JSON
block. The server validates role/phase counts, participant references, prompt
lengths, and exactly one output phase.

## Schedules

`schedules.py` stores scheduled messages in `data/schedules.json`.

Supported specs:

- `every Nm`
- `every Nh`
- `every Nd`
- `daily at HH:MM`

The app background schedule runner wakes every 30 seconds. Due schedules become
normal chat messages from the configured creator, with configured targets
prepended as mentions. Because scheduled messages go through `MessageStore.add`,
normal routing and agent triggering apply.

Schedules can be paused, resumed, deleted, recurring, or one-shot.

## Summaries

`summaries.py` stores one summary per channel in `data/summaries.json`.

Agents use `chat_summary(action='read')` to catch up and
`chat_summary(action='write')` to update a summary. Summaries are capped at
1000 characters and include author, timestamp, and latest message ID at write
time. Summary writes also post a timeline message of type `summary`.

## Hats, Pins, Slash Commands, and Utility Features

Hats:

- Stored in `data/hats.json`.
- Set by `chat_set_hat`.
- Sanitized by removing scripts, inline event handlers, and `javascript:`.
- Limited to 5 KB and must start with `<svg`.

Pins/todos:

- Stored in `data/todos.json`.
- UI cycles message state through unpinned, todo, done, and cleared.

Slash commands:

- `/continue`: resume routing after loop guard.
- `/clear`: clear visible history for the current channel.
- `/sleep`: stop auto-started terminals for the current channel.
- `/compact`: inject native `/compact` into active channel CLIs.
- `/all`: mention all online channel agents.
- `/hatmaking`, `/artchallenge`, `/roastreview`, `/poetry`: expand into
  agent-directed prompt messages.

## Startup Scripts

`macos-linux/start.sh` creates `.venv`, installs requirements, starts the server
in a background tmux session when tmux exists, otherwise uses `nohup`.

`macos-linux/start_*.sh` scripts start the server if needed, then run a specific
wrapper. Auto-approve variants set `AGENTCHATTR_AUTO_APPROVE=1` and pass
provider-specific unsafe approval flags.

`macos-linux/stop.sh` stops the background server, detached wrappers, and
`agentchattr-*` tmux sessions.

`windows/start.bat` creates `.venv`, installs requirements, and starts the server
in the background. Windows provider scripts start the corresponding wrapper.
Windows currently does not use the server-managed tmux auto-spawn path; agents
must be launched manually.

`macos-linux/start_wsl_lan.sh` and `windows/setup_wsl_lan.ps1` support WSL/LAN
access.

## Security Model

The app is designed for local or trusted-network use, not public internet
exposure.

Implemented controls:

- In-memory browser session token injected into HTML and required for WebSocket
  and protected REST requests.
- Origin checking for browser requests.
- Per-agent registration tokens for wrapper/API-agent auth.
- MCP tool identity resolution from bearer token where available.
- Refusal of ambiguous base-family senders in multi-instance contexts.
- Upload path traversal guard on `/uploads/{filename}`.
- Download links are random bearer tokens, scoped to configured allowlisted
  roots, short-lived, in-memory only, and revalidate file existence at request
  time.
- SVG hats are sanitized and size-limited.
- SVG file uploads are download-only, not inline-rendered.
- Network binding warning and confirmation in `run.py`.

Important limitation:

- There is no TLS. If bound to a LAN address, session tokens and agent actions
  are plaintext on the network. `run.py` explicitly warns that token compromise
  can allow remote triggering of agents, which may become remote code execution
  if agents run with auto-approve modes.
- Ephemeral download URLs are also bearer URLs. Anyone who receives one before
  it expires can download that file, so `[downloads].allowed_roots` should be
  narrow in LAN mode.

## Persistence Files

Under `data_dir`, usually `./data`:

- `agentchattr_log.jsonl`: active timeline log.
- `agentchattr_log.N.jsonl`: archived hidden history.
- `todos.json`: pinned/todo state.
- `settings.json`: room settings and channels.
- `hats.json`: avatar hats.
- `rules.json`: shared rules and epoch.
- `summaries.json`: channel summaries.
- `jobs.json`: jobs and job messages.
- `schedules.json`: scheduled prompts.
- `session_runs.json`: session run state.
- `custom_templates.json`: saved custom session templates.
- `mcp_cursors.json`: agent read cursors.
- `roles.json`: per-agent roles.
- `renames.json`: persisted rename chains.
- `channel_agent_bindings.json`: channel-to-instance bindings.
- `{agent}_queue.jsonl`: per-agent trigger queue files.
- `{agent}_recovered`: wrapper recovery flag files.
- `provider-config/`: generated MCP config files.
- `launcher-logs/`: server-managed wrapper logs.

Uploads are stored under the configured upload directory, usually `./uploads`.

## Main Data Flows

### Human Mentions an Agent

1. Browser sends WebSocket `message`.
2. `MessageStore.add()` persists it.
3. Store callback schedules `app._handle_new_message()`.
4. Server broadcasts the message.
5. Router extracts mentions and loop-guard state.
6. App resolves base mentions to concrete instances and may auto-start a wrapper.
7. `AgentTrigger` appends to the instance queue file.
8. Wrapper queue watcher injects an `mcp read` prompt into the CLI.
9. Agent calls MCP `chat_read`, does work, then calls `chat_send`.
10. `chat_send` persists another message, restarting the same pipeline.

### Agent Mentions Another Agent

1. Agent calls MCP `chat_send`.
2. MCP bridge authenticates and resolves sender identity.
3. Message is persisted and broadcast.
4. Router allows routing only if the agent explicitly mentioned another target.
5. The per-channel hop count increments.
6. If the hop limit is exceeded, routing pauses and a system notice asks a human
   to use `/continue`.

### Agent Mentions a Local File

1. Agent posts a normal chat message containing a local file path.
2. `MessageStore.add()` persists the message text unchanged.
3. Before the message is delivered to browsers, `DownloadLinkService` scans the
   text and resolves candidate paths.
4. Eligible files under `[downloads].allowed_roots` receive random in-memory
   tokens and `metadata.downloads` entries.
5. The browser renders a collapsed downloads box below the message.
6. Clicking a link calls `GET /downloads/{token}`, which validates expiry and
   returns the file as an attachment.

### Job Thread Mention

1. Browser or agent posts to `/api/jobs/{job_id}/messages` or MCP
   `chat_send(job_id=N)`.
2. `JobStore` embeds the message in the job.
3. Mentions are resolved using the job's channel.
4. Agent triggers include `job_id`.
5. Wrapper injects a prompt telling the agent to read the job thread.
6. Agent calls `chat_read(job_id=N)` and replies with `chat_send(job_id=N)`.

### Structured Session

1. Browser starts a session from a template or valid draft.
2. `SessionStore.create()` persists an active run.
3. `SessionEngine` triggers the first cast participant.
4. The expected participant posts a normal chat message.
5. The engine observes the message and advances turn or phase after a short
   delay.
6. Phase changes add system banners and trigger the next participant.
7. The output phase completes the session and marks the output message.

## Open Questions

These points are implementation ambiguities worth confirming with maintainers:

- `JobStore.create()` initializes new jobs with status `done`, while the UI labels
  `done` as Active. This may be intentional legacy naming, but the stored value
  is counterintuitive.
- Channel deletion removes the channel from settings and kills channel terminals
  but intentionally does not delete persisted history. `MessageStore.delete_channel()`
  exists but is not used by the current WebSocket delete-channel flow.
- `mcp_proxy.py` stamps sender identity for most tools, but `_SENDER_PARAMS` does
  not include newer tools such as `chat_summary` and `chat_propose_job`. Direct
  bearer auth still resolves identity server-side, but proxy-only behavior should
  be verified if those tools are used by a client that omits sender fields.
