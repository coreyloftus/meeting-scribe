# meeting-scribe — Desktop App & Integrations Spec

**Status:** Draft for implementation
**Author:** Corey (decisions) + Claude (spec)
**Date:** 2026-07-06
**Audience:** the engineering agent (Fable) that will implement this.

This spec expands `meeting-scribe` from a pure CLI into a macOS desktop
experience: a menu-bar badge, a window that lists every captured meeting,
start/stop from the UI, and pluggable "push this note somewhere" integrations
(Notion, Google Docs, Google Drive, …). It is written to be actionable — it
tells you what to build, how the pieces talk, and what to change in the existing
codebase — without prescribing every line.

---

## 0. Locked decisions (from the product owner)

These were decided up front; do not relitigate them.

| Area | Decision | Consequence |
|---|---|---|
| **UI stack** | **Native SwiftUI app + `NSStatusItem` menu-bar item** | Swift/SwiftUI front-end. Matches the existing `helper/syscap.swift`. Talks to the backend over HTTP. |
| **Backend architecture** | **Local daemon/service (`scribed`)** owns recording + a processing job queue, exposes a small localhost HTTP API. CLI and UI are both thin clients. | Enables live badge status, background processing, and a durable meetings index. |
| **Google auth** | **Desktop OAuth (loopback flow)** — browser consent, refresh token cached locally. | Needs a Google Cloud OAuth **client ID** (desktop type). Per-user, portable. |
| **Distribution** | **Personal / unsigned local build** | No App Sandbox, no notarization. The app may freely shell out to Homebrew `ffmpeg`/`whisper`/the daemon. Screen Recording permission is granted to the app (or the daemon process). |

### Smaller decisions made in this spec (recommended defaults — override if you disagree)

- **Daemon language: Python.** Reuse the existing `recorder`/`process`/`outputs`
  pipeline directly rather than reimplementing it. The daemon is a thin HTTP +
  job-queue layer over code that already works.
- **HTTP framework: FastAPI + uvicorn** (async, typed, trivial SSE). `aiohttp`
  or even stdlib `http.server` are acceptable if you want zero heavy deps, but
  FastAPI buys request validation and auto-docs cheaply. Add to
  `pyproject.toml` as an optional extra so the pure-CLI install stays lean.
- **Meetings index: SQLite** at `~/.local/state/meeting-scribe/meetings.db`,
  owned/written only by the daemon. It is a cache/index over the real artifacts
  on disk, and must be rebuildable by scanning the recordings dir.
- **Real-time UI updates: Server-Sent Events (SSE)** on `/events`, with polling
  of `/status` as a fallback. Recording timer + job progress push to the badge
  this way.
- **Daemon lifecycle: a `launchd` LaunchAgent** that runs `scribed` in the
  background and restarts it on crash/login. The app can also spawn it on demand
  if not running.
- **UI ↔ daemon auth: a localhost bearer token** written to a
  `600`-permission file the app reads. The daemon binds `127.0.0.1` only.

---

## 1. Goals & non-goals

### Goals
1. **Menu-bar badge** showing recording state (idle / recording w/ elapsed timer
   / processing), with a click menu to start, stop, open the window, and jump to
   recent meetings.
2. **Main window** listing all captured meetings with status, and a detail view
   showing the summary + transcript, with per-meeting "push to…" actions.
3. **Start/stop recording from the UI** (and keep the CLI working identically).
4. **Background processing** — stopping a meeting returns immediately; transcribe
   + summarize + output-writing run as a tracked job with visible progress.
5. **Pluggable outputs** — Notion (exists), **Google Docs**, **Google Drive
   file (md/txt)**, designed so a new destination is a small, isolated addition.
6. **Re-push / re-run** — send an already-processed meeting to a new destination
   without re-recording or re-transcribing.

### Non-goals (this round)
- App Store distribution / sandboxing / notarization.
- Windows/Linux UI (the daemon + CLI stay cross-platform-ish; the app is macOS).
- Real-time/live transcription during the meeting (still batch after stop).
- Multi-user / cloud sync. Everything is local to one Mac.
- Editing transcripts in-app (view-only is fine for v1; see Open Questions).

---

## 2. Target architecture

```
┌────────────────────────────────────────────────────────────────┐
│  SwiftUI app  (menu-bar NSStatusItem  +  main window + settings) │
│   • polls /status + subscribes to /events (SSE)                  │
│   • POST /start /stop /meetings/:id/push …                       │
│   • never touches ffmpeg/whisper directly                        │
└───────────────┬────────────────────────────────────────────────┘
                │  HTTP  127.0.0.1:<port>  (bearer token)
┌───────────────▼────────────────────────────────────────────────┐
│  scribed  (Python daemon, FastAPI/uvicorn)                       │
│   • owns the single recording session (start/stop)               │
│   • processing JOB QUEUE (background worker thread/task)         │
│   • meetings INDEX (SQLite) — status, paths, output links        │
│   • /events SSE bus  (state changes, job progress)               │
│   • reuses meeting_scribe.recorder / .process / .outputs         │
└───────────────┬────────────────────────────────────────────────┘
        spawns   │                     reuses
┌────────────────▼──────────┐   ┌──────────────────────────────────┐
│ bin/syscap (SCK, system)  │   │ meeting_scribe pipeline (unchanged │
│ ffmpeg (mic)              │   │ core): transcribe → llm → outputs  │
│ whisper-cli (transcribe)  │   │ outputs/: markdown, notion,        │
└───────────────────────────┘   │           gdocs, gdrive (new)      │
                                └──────────────────────────────────┘

CLI `scribe` becomes a thin client too: `scribe start` → POST /start, etc.
(with a `--local` escape hatch that runs in-process if the daemon is down).
```

**Key principle:** the daemon is the single source of truth for "what's
happening now" and "what meetings exist." The UI and CLI render its state; the
filesystem holds the durable artifacts; SQLite is a rebuildable index.

---

## 3. The `scribed` daemon

New package: `meeting_scribe/daemon/` (or a top-level `scribed` module). Entry
point `scribed` added to `[project.scripts]`.

### 3.1 Responsibilities
- Own the recording session (wrap `recorder.start`/`recorder.stop`). Only one
  active recording at a time (existing constraint).
- Maintain the meetings index (§4).
- Run a **background job queue** for processing. `stop` enqueues a `process`
  job and returns; the worker updates job/meeting status and emits events.
- Also enqueue: `reprocess` (existing audio → new transcript/summary) and
  `push` (existing note → one output target).
- Serve the HTTP API (§5) and the SSE event stream on `127.0.0.1`.
- Reconcile on startup: if `session.json` shows a live recording (PIDs alive),
  resume tracking it; if a job was interrupted, mark it `failed` (audio is safe,
  user can retry). Rebuild/refresh the index from disk.

### 3.2 Concurrency model
- FastAPI async endpoints; a **single background worker** (asyncio task or a
  worker thread) drains a job queue serially. Transcription is CPU-heavy — run
  the blocking pipeline in a thread (`run_in_executor`) so the event loop and
  `/status` stay responsive. One job at a time is fine for a personal app.
- Recording start/stop are quick subprocess ops; guard the session with a lock.

### 3.3 Lifecycle (launchd)
- Ship a `LaunchAgent` plist template + an installer command:
  `scribe daemon install` writes `~/Library/LaunchAgents/com.meetingscribe.scribed.plist`
  and `launchctl bootstrap`s it. `scribe daemon {start,stop,status,uninstall}`.
- Plist runs `scribed serve` with `KeepAlive` + `RunAtLoad`. Logs to
  `~/.local/state/meeting-scribe/scribed.log`.
- The SwiftUI app, on launch, checks `/status`; if unreachable it offers to
  start the daemon (spawn `scribed serve` or `launchctl kickstart`).
- **Screen Recording permission** must belong to whatever process spawns
  `syscap`. Since the daemon spawns it, the daemon (its launching binary — e.g.
  the Python framework or the terminal on first run) needs the grant. Document
  this clearly in `doctor`. (See Open Questions #2 — permission ergonomics.)

### 3.4 Security
- Bind `127.0.0.1` only. On startup generate a random token, write it to
  `~/.local/state/meeting-scribe/daemon.token` (`chmod 600`). Clients read that
  file and send `Authorization: Bearer <token>`. Reject requests without it.
  This prevents other local users / random localhost pages from driving it.
- Port: pick a fixed default (e.g. `48237`) but write the actual bound
  `host:port` to `~/.local/state/meeting-scribe/daemon.json` so clients discover
  it. Fall back to an ephemeral port if the default is taken.

---

## 4. Meetings index & data model

SQLite DB at `~/.local/state/meeting-scribe/meetings.db`, written only by the
daemon. Treat it as a **cache**: the recordings directory (wavs, logs) and the
markdown outputs are the durable truth; a `rebuild` scans disk and repopulates.

### 4.1 `meetings` table (proposed columns)

| column | type | notes |
|---|---|---|
| `id` | TEXT PK | the recording stamp/base name, e.g. `2026-07-06_10-00-00` |
| `base_path` | TEXT | absolute recording base (`…/<stamp>`) |
| `started_at` | TEXT | ISO8601 |
| `ended_at` | TEXT | ISO8601, null while recording |
| `status` | TEXT | see lifecycle below |
| `title` | TEXT | `"<date> <slug words>"` once summarized |
| `slug` | TEXT | from the LLM |
| `duration_sec` | INTEGER | derived |
| `system_wav` / `mic_wav` | TEXT | paths (nullable) |
| `transcript_path` | TEXT | cached transcript file (add one — see §6.2) |
| `summary_md` | TEXT | the summary markdown (also written to file) |
| `error` | TEXT | last failure message, if any |
| `created_at` / `updated_at` | TEXT | bookkeeping |

### 4.2 `outputs` table (one row per push attempt/result)

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `meeting_id` | TEXT FK | |
| `target` | TEXT | `markdown` \| `notion` \| `gdocs` \| `gdrive` |
| `status` | TEXT | `ok` \| `failed` |
| `url` | TEXT | e.g. Notion/Doc URL, or local file path |
| `detail` | TEXT | error text on failure |
| `created_at` | TEXT | |

This lets the UI show "Notion ✓ (open) · Google Doc ✓ (open) · Drive ✗ retry"
per meeting and supports idempotent re-push.

### 4.3 Status lifecycle
```
recording ─stop─▶ queued ─▶ transcribing ─▶ summarizing ─▶ writing_outputs ─▶ done
    │                                                                          
    └─(crash)                       any stage ──error──▶ failed  (audio safe;   
                                                          reprocess re-queues)   
```
- `recording`: session live.
- `queued`/`transcribing`/`summarizing`/`writing_outputs`: job phases (drive the
  processing progress bar + badge "processing" state).
- `done`: has a summary + at least the markdown output.
- `failed`: carries `error`; UI offers "Retry" (→ `reprocess`).

---

## 5. HTTP API (localhost)

JSON over HTTP, bearer-token auth. Versioned under `/v1`. Illustrative — refine
shapes as you build, but keep these capabilities.

| Method & path | Purpose |
|---|---|
| `GET /v1/status` | `{ recording: bool, session: {...}, active_job: {...}, daemon_version }`. Cheap; polled. |
| `GET /v1/events` | **SSE** stream of state changes: `recording_started`, `tick` (elapsed), `job_progress` (phase, pct), `meeting_updated`, `output_pushed`. |
| `POST /v1/start` | Begin recording. 409 if already recording. Returns the new meeting row. |
| `POST /v1/stop` | Stop recording, enqueue processing job. Returns `{ meeting_id, job_id }` immediately. |
| `GET /v1/meetings?limit=&offset=&q=` | List meetings (newest first) for the window, with their `outputs`. |
| `GET /v1/meetings/:id` | Full detail: summary_md, transcript, outputs, paths. |
| `POST /v1/meetings/:id/reprocess` | Re-transcribe/re-summarize existing audio (optionally `{ prompt_overrides }`). |
| `POST /v1/meetings/:id/push` | Body `{ target: "notion"|"gdocs"|"gdrive", options?: {...} }`. Push existing note to one destination; records an `outputs` row. |
| `DELETE /v1/meetings/:id?keep_audio=` | Remove from index (+ optionally delete artifacts). Confirm in UI. |
| `GET /v1/config` / `PUT /v1/config` | Read/update config (for the Settings pane). Redact secrets on read. |
| `GET /v1/integrations` | Which outputs are configured/connected (e.g. Google connected?, Notion token present?). |
| `POST /v1/integrations/google/connect` | Kick off desktop OAuth (§7.2); returns a URL to open or opens it. |
| `GET /v1/doctor` | Structured version of `scribe doctor` for the Settings "health" view. |

---

## 6. Changes to the existing Python code

The pipeline is well-factored; changes are mostly **making it callable/observable**
rather than rewrites.

### 6.1 `recorder.py`
- No functional change to capture. Expose a lightweight `session_status()` that
  returns liveness + elapsed for the daemon/`/status` without side effects
  (mostly exists via `load_session` + `_alive`).
- Consider recording `ended_at` on stop into the index.

### 6.2 `process.py`
- Refactor `process()` to accept an optional **progress callback**
  `on_phase(phase: str, pct: float | None)` so the daemon can emit `job_progress`
  events. Keep the current return-list behavior for the CLI, or have the CLI
  adapt the callback to prints.
- **Persist the transcript to a file** next to the audio (e.g.
  `<base>.transcript.txt`) and record its path in the index. Today the transcript
  only lives inside the written note; the UI detail view and re-push want it
  independently. Low effort, high value.
- Split "make the `Note`" from "write outputs" so `push` can rebuild a `Note`
  from stored `summary_md` + transcript and target a single output.

### 6.3 `outputs/` — formalize the plugin registry
Turn the ad-hoc `if name == …` in `write_all` into a small registry so adding a
destination is isolated and declarative.

```python
# outputs/base.py
class Output(Protocol):
    key: str                 # "notion", "gdocs", "gdrive", "markdown"
    label: str               # "Google Docs"
    def is_configured(self, cfg) -> bool: ...
    def write(self, cfg, note: Note, options: dict | None = None) -> OutputResult: ...
    # OutputResult(url|path, target, ok, detail)

REGISTRY: dict[str, Output] = { ... }   # populated by each module
```

- `write_all(cfg, note)` iterates enabled outputs from the registry.
- New: `write_one(cfg, note, target, options)` for the `push` endpoint.
- Existing `markdown` and `notion` writers adapt to this interface (thin shim;
  keep their current logic). The markdown→blocks converter in `notion.py` is
  reusable for other rich targets.

### 6.4 CLI (`cli.py`) becomes daemon-aware
- `scribe start`/`stop`/`process`/list → prefer talking to `scribed` over HTTP;
  fall back to in-process (`--local`) when the daemon is down, so nothing breaks
  for pure-CLI users.
- Add `scribe daemon {install,serve,start,stop,status,uninstall}`.
- `scribe doctor` gains: daemon reachable?, index present?, Google connected?

---

## 7. Integrations (pluggable outputs)

### 7.1 Notion — already implemented
Keep `outputs/notion.py` as-is behind the new registry interface. It already
converts markdown → Notion blocks and is fully portable (token + database_id).

### 7.2 Google Docs & Google Drive — new (`outputs/google.py` + auth helper)

**Auth (desktop OAuth loopback):**
- Add `meeting_scribe/integrations/google_auth.py`.
- Requires a Google Cloud project with the **Docs API** and **Drive API**
  enabled and an **OAuth client of type "Desktop app."** The client ID/secret
  ship in config (or are entered in Settings). *(These are not secrets in the
  strong sense for an installed app, but keep them in config, not the repo.)*
- Flow: open the consent URL in the browser → local loopback server on a random
  port catches the redirect + auth code → exchange for tokens →
  **cache the refresh token** in `~/.config/meeting-scribe/google_token.json`
  (`chmod 600`). Reuse `google-auth` + `google-auth-oauthlib` (add as optional
  extras). Refresh automatically; re-prompt only if revoked.
- Scopes: `documents` + `drive.file` (create-only Drive access is the least
  privilege that still lets us upload files and create docs).

**Google Docs output (`target: "gdocs"`):**
- Create a new Doc titled `note.title`. Convert the summary markdown into Docs
  API `batchUpdate` requests (headings, bullets, checkboxes→bullets or `[ ]`
  text, paragraphs, a divider, then the transcript). Reuse the markdown parsing
  shape from `notion.py`.
- Optionally place it in a configured Drive folder (`options.folder_id` or a
  `outputs.gdocs.folder_id` config). Return the Doc URL for the `outputs` table.

**Google Drive file output (`target: "gdrive"`):**
- Simpler: upload the note as a **`.md` or `.txt` file** (config
  `outputs.gdrive.format`, default `md`) into a configured folder
  (`outputs.gdrive.folder_id`). Use `note.full_markdown()`. Return the file's
  Drive URL.
- This is the "send a file to Google Drive in markdown or txt" ask and is the
  lowest-risk Google target — consider shipping it before Docs.

### 7.3 Extensibility contract (for future targets)
Anything implementing the `Output` protocol + registering itself becomes:
- selectable in Settings (enabled outputs on auto-run after `stop`), and
- a `push` target in the meeting detail view,
with **zero** UI code changes (the SwiftUI app renders targets from
`GET /v1/integrations`). Document this so adding, say, Slack or email later is a
single file.

---

## 8. SwiftUI app

New top-level `app/` (Swift Package or Xcode project) — `MeetingScribe.app`.
Talks only to the daemon over HTTP; owns no capture logic.

### 8.1 Menu-bar item (`NSStatusItem`)
- Icon reflects state: idle (outline mic), recording (filled/red + elapsed
  `12:04` as the button title), processing (spinner/●●●).
- Menu:
  - `● Recording 12:04` (disabled header) **or** `Idle`
  - **Start recording** / **Stop & process** (contextual)
  - ───
  - **Recent meetings ▸** submic-menu (last ~8; click opens detail/output)
  - **Open window**, **Settings…**
  - **Daemon: running/stopped** (with a restart action)
  - **Quit**
- Subscribes to `/events` (SSE) for the live timer and state transitions; falls
  back to polling `/status` every ~2s if SSE drops.

### 8.2 Main window
- **Sidebar/list:** all meetings, newest first, with status chip
  (recording/processing/done/failed) and per-target output badges. Search box
  (→ `/meetings?q=`).
- **Detail pane:** title, date, duration; rendered summary markdown; collapsible
  full transcript; an **outputs bar** with buttons per registered target
  ("Push to Notion", "Create Google Doc", "Save to Drive") showing ✓/✗ + open
  links; **Reprocess** and **Reveal in Finder / Open audio**.
- **Top bar:** big **Start/Stop** button mirroring the menu bar.

### 8.3 Settings
- Recording (dir, capture system/mic, mic device).
- Transcription (whisper model path, threads, language, speaker labels).
- LLM backend (api vs claude_cli, model, key entry — stored via `PUT /config`).
- Prompts (slug/summary — editable text).
- Integrations: Notion (token + db id, "test"), **Google ("Connect" → OAuth,
  shows connected account, folder pickers)**, choose which outputs auto-run on
  stop.
- Health: the `/doctor` view (deps, permissions, daemon).

### 8.4 App ↔ daemon bootstrap
- On launch: read `daemon.json` + `daemon.token`; GET `/status`.
- If unreachable: offer "Start background service" → install/kickstart launchd
  agent (or spawn `scribed serve`), then retry.
- Show a clear banner if Screen Recording permission is missing (link to System
  Settings), surfaced from `/doctor`.

---

## 9. Config additions

Extend `config.example.json` (all optional, backward compatible):

```jsonc
{
  "daemon": {
    "host": "127.0.0.1",
    "port": 48237
  },
  "outputs": {
    "markdown": { "enabled": true, "dir": "~/Documents/meeting-transcripts" },
    "notion":   { "enabled": false, "token": "", "database_id": "",
                  "title_property": "Name", "date_property": "Date" },
    "gdocs":    { "enabled": false, "folder_id": "" },
    "gdrive":   { "enabled": false, "folder_id": "", "format": "md" }
  },
  "google": {
    "client_id": "",           // Desktop OAuth client (or entered in Settings)
    "client_secret": "",       // installed-app "secret"
    "token_path": "~/.config/meeting-scribe/google_token.json"
  }
}
```

Keep secret precedence consistent with today: env wins
(`ANTHROPIC_API_KEY`, `NOTION_TOKEN`; add `GOOGLE_CLIENT_ID/SECRET` if you like).
Add `Config` properties/`enabled_outputs()` entries for `gdocs`/`gdrive`.

---

## 10. Packaging, build & dev workflow

- **Python:** add optional extras in `pyproject.toml`:
  `daemon` → `fastapi`, `uvicorn`; `google` → `google-api-python-client`,
  `google-auth`, `google-auth-oauthlib`. Pure-CLI install stays as-is.
  Add `scribed` to `[project.scripts]`.
- **Swift app:** a local Xcode project or Swift Package producing
  `MeetingScribe.app`. Since distribution is **personal/unsigned**, a simple
  `xcodebuild` (or "Product → Archive → copy to /Applications") is enough; add a
  `scripts/build_app.sh`. Ad-hoc code-signing (`codesign -s -`) is fine to keep
  Gatekeeper quiet locally.
- **Screen Recording:** grant to the daemon's launching process. Document that on
  first run the app/daemon triggers the permission prompt; then restart the
  daemon.
- **Dev loop:** run `scribed serve` in a terminal, run the SwiftUI app from
  Xcode against it. The CLI hitting the same daemon is a great integration test.

---

## 11. Backward compatibility & migration

- The CLI must keep working for someone who never installs the app or daemon:
  `scribe start/stop/process` fall back to in-process execution when `scribed`
  isn't reachable (`--local`).
- First daemon run builds the index by scanning `recording.dir` (pair
  `.system.wav`/`.mic.wav` by stamp) and the markdown output dir (match by
  `<date>-<slug>.md`) so existing meetings appear in the UI immediately.
- No changes to the on-disk recording format; existing recordings remain
  processable.

---

## 12. Suggested implementation phases

1. **Daemon core:** `scribed serve` with SQLite index, `/status`, `/start`,
   `/stop`, `/meetings*`, background job worker wrapping `process()`; index
   rebuild-from-disk. CLI repointed to it (with local fallback). *No UI yet —
   verify with curl + the CLI.*
2. **Outputs registry refactor:** registry + `write_one`; adapt markdown/notion;
   add `/meetings/:id/push`. Persist transcript to file.
3. **SwiftUI app:** menu-bar item + main window (list/detail) + start/stop +
   SSE live status. Settings basics (read/write config, doctor view).
4. **Google integration:** desktop OAuth helper; `gdrive` file upload first, then
   `gdocs`. Wire into registry + Settings "Connect Google."
5. **Polish:** launchd installer + app bootstrap, permission banners, reprocess,
   delete, recent-meetings submenu, search.

Each phase is independently testable and leaves the app usable.

---

## 13. Open questions / things to confirm before/while building

1. **Auto-run vs manual outputs.** After `stop`, should the daemon auto-push to
   every *enabled* output (current behavior for markdown/notion), or only write
   markdown automatically and leave Notion/Google as explicit one-click pushes?
   (Recommend: auto-run whatever the user marks "enabled"; everything is also
   available as manual push.)
2. **Screen Recording permission ergonomics.** With the daemon (not the terminal)
   spawning `syscap`, confirm which binary the permission attaches to under a
   launchd agent, and whether a first-run helper is needed. May require testing.
3. **Transcript editing.** v1 is view-only. Do you want in-app editing of the
   summary/transcript before pushing? (Adds an edit surface + "dirty" state.)
4. **Multiple/simultaneous recordings.** Staying single-session (existing
   constraint) — confirmed OK?
5. **Google OAuth client distribution.** For personal use you'll create one
   Desktop OAuth client in your own GCP project and paste the ID/secret into
   Settings. Confirm that's acceptable (vs. shipping a shared client).
6. **Delete semantics.** Should deleting a meeting remove the audio + markdown +
   remote Notion/Doc pages, or only the local index entry? (Recommend: local
   only by default, with an explicit "also delete audio/files" checkbox; never
   delete remote pages.)
7. **Menu-bar-only mode.** Is a Dock icon wanted, or should this be a pure
   menu-bar (`LSUIElement`) app with the window opened on demand? (Recommend:
   menu-bar-first, `LSUIElement`, window optional.)
```
