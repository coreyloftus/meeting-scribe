# Dock app + one-click Notion connect

**Status:** Ready for implementation
**Date:** 2026-09-26
**Audience:** the engineering agent implementing this. You have none of the
conversation context that produced this document; everything you need is here.

---

## 0. Instructions to the executing agent

1. **Read this entire document before writing any code.** §3 lists things that
   must not be touched. Some of them look like dead code and are not.
2. **Work autonomously.** Make reasonable decisions without checking in.
   Recommended defaults may be overridden with good reason. **Locked decisions
   (§3.2) may not** — do not relitigate them.
3. **Read §3.4 (build toolchain) before your first build.** The SwiftUI app does
   not compile with the default toolchain on this machine.
4. **Do not deploy anything and do not create the Notion integration yourself.**
   Those are owner steps (§6). Build and verify the broker locally.
5. **Finish by** creating a fresh branch off `main`, committing with
   conventional-commit messages (one-line subjects), and opening a PR.
   **Base branch is `main`** — this repo has no `dev` branch. Two commits
   minimum: one for Part A (Dock), one for Part B (Notion OAuth).

---

## 1. Goal

Two changes to the macOS app in `app/`:

- **Part A — Dock app.** `MeetingScribe.app` is menu-bar-only today
  (`LSUIElement`). On the owner's MacBook the menu-bar icon hides under the
  notch, so there is no reliable way to open the window. When this is done, the
  app has a Dock icon. Clicking it opens (or brings forward) the main window at
  any time. The menu-bar badge stays for recording status.
- **Part B — One-click Notion.** Connecting Notion today means creating an
  internal integration, copying a token, sharing a database with it, and
  pasting a database ID. When this is done, the user clicks **Connect Notion**
  in Settings, approves access in the browser, and picks a database from a
  dropdown. No token or ID copying. A small hosted broker holds the Notion
  OAuth client secret, so the secret never ships inside the app.

---

## 2. Current architecture (what exists)

```
MeetingScribe.app (SwiftUI, app/Sources/MeetingScribe/)
   │  HTTP + SSE, Bearer token from ~/.local/state/meeting-scribe/daemon.token
   ▼
scribed daemon (FastAPI, meeting_scribe/daemon/server.py) on 127.0.0.1:48237
   │  reads/writes config.json (meeting_scribe/config.py)
   ▼
outputs (meeting_scribe/outputs/notion.py → Notion REST API with a token)
```

Files you will touch:

| File | Role today |
|---|---|
| `app/Sources/MeetingScribe/MeetingScribeApp.swift` | `@main`. `MenuBarExtra` + `Window("Meeting Scribe", id: "main")` + `Settings`. Header comment says "no Dock icon". |
| `app/Sources/MeetingScribe/MenuBarContent.swift` | Menu. `openMain()` calls `openWindow(id: "main")` + `NSApp.activate`. |
| `app/Sources/MeetingScribe/AppState.swift` | `ObservableObject`: daemon client, SSE loop, `refreshIntegrations()`. |
| `app/Sources/MeetingScribe/DaemonAPI.swift` | Codable models (`IntegrationsResponse`, …) + client methods (`connectGoogle()`, `putConfig()`). |
| `app/Sources/MeetingScribe/SettingsView.swift` | Integrations tab. Notion section = token `SecureField` + database ID `TextField`. |
| `scripts/build_app.sh` | `swiftc` build, writes `Info.plist` inline (contains `LSUIElement`), signs. |
| `meeting_scribe/daemon/server.py` | `/v1/integrations`, `/v1/integrations/google/connect`, `SECRET_PATHS`, `require_token`. |
| `meeting_scribe/daemon/events.py` | `EventBus.publish(type, **payload)` → SSE. |
| `meeting_scribe/outputs/notion.py` | `write()` creates a page in `outputs.notion.database_id` using `cfg.notion_token`. Notion API version `2022-06-28`. |
| `meeting_scribe/config.py` | `Config.notion_token` = `NOTION_TOKEN` env, else `outputs.notion.token`. |
| `meeting_scribe/cli.py` | `scribe google connect`, `scribe doctor` Notion line. |
| `README.md` | "Notion setup" section describes the manual token flow. |

The Google integration (`meeting_scribe/integrations/google_auth.py`,
`POST /v1/integrations/google/connect`, "Connect Google" button) is the sibling
pattern. Copy its shape where it fits.

---

## 3. Context & constraints

### 3.1 Stack

- Swift/SwiftUI, macOS 14+, arm64, compiled with `swiftc` (no SwiftPM, no Xcode).
- Python 3.12 daemon (FastAPI + `requests`), venv at `.venv/`.
- Broker (new): Vercel serverless functions, TypeScript, **no framework**, no
  runtime dependencies beyond Node's built-in `fetch`.

### 3.2 Locked decisions — do not relitigate

| Area | Decision |
|---|---|
| Dock + menu bar | **Keep both.** Add the Dock icon; keep `MenuBarExtra` exactly as it is. |
| Notion secret | **Hosted broker.** The Notion OAuth client secret lives only in the broker's environment. It never appears in the app bundle, the repo, `config.json`, or logs. |
| Broker scope | The broker is **stateless**. It does not store codes, tokens, or users. It only redirects and proxies the token exchange. |
| Token storage | The OAuth access token is written to `outputs.notion.token` in `config.json` — the same key the manual flow uses. `NOTION_TOKEN` env still wins. This keeps `notion.write()`, `is_configured()`, and the `SECRET_PATHS` redaction working unchanged. |
| Manual fallback | Keep the manual token + database ID fields, behind a collapsed "Advanced: use an internal integration token" disclosure. Existing users must not break. |
| Bundle identity | Do **not** change `CFBundleIdentifier` (`com.meetingscribe.app`) or the signing step. Both are tied to TCC grants (see §3.3). |

### 3.3 What NOT to touch

- `NSMicrophoneUsageDescription` in `scripts/build_app.sh`. Without it macOS
  kills the mic capture process instead of prompting. Keep it and its comment.
- `sign_with_local_identity` / `scripts/signing.sh` / `scripts/setup_signing.sh`.
  Ad-hoc signing silently revokes Screen Recording grants on every rebuild.
- The recording pipeline: `recorder.py`, `audio.py`, `transcribe.py`,
  `helper/`, `bin/`. Nothing in this spec needs them.
- The Google integration code paths, except to share a helper if that is
  clearly simpler.
- The Notion API version (`2022-06-28`) in `outputs/notion.py`. Do not migrate
  to the data-sources API in this round.

### 3.4 Build toolchain (known blocker)

The installed Command Line Tools (27.0 beta) ship no `SwiftUIMacros` plugin.
Every `@State` fails with "plugin for module 'SwiftUIMacros' not found", and
cascades into misleading "cannot find '$x' in scope" errors. Ignore those.
Build against the older SDK that is still installed:

```bash
SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk bash scripts/build_app.sh --install
```

Use `/usr/local/bin/python3.12` or `.venv/bin/python` for Python. Homebrew
`python3` (3.14) is broken on this machine.

---

## 4. Non-goals

- No Dock menu items (Start/Stop in the Dock right-click menu), no Dock badge,
  no "hide Dock icon" preference.
- No custom URL scheme (`meetingscribe://`) to bounce the browser back to the app.
- No "create a database for me" button. The user picks an existing database.
- No Notion data-sources API migration.
- No change to Google OAuth.
- No notarization, DMG, or distribution work (see `specs/bundle-distributable-app.md`).
- No deploy of the broker and no creation of the Notion public integration —
  owner steps, §6.

---

## 5. Implementation plan

### Part A — Dock app

**A1. Remove `LSUIElement`.** In `scripts/build_app.sh`, delete the
`LSUIElement` key and its comment from the inline `Info.plist`. Add
`<key>CFBundleIconFile</key><string>AppIcon</string>`. Update the header comment
in `MeetingScribeApp.swift` to say the app has a Dock icon and a menu-bar badge.

**A2. App icon.** Without an icon the Dock shows a generic app tile.

- Create `scripts/make_icon.swift`: render a 1024×1024 PNG — a rounded-rect
  background with the SF Symbol `waveform` (or `mic.fill`) centered in white.
  Use AppKit (`NSImage`, `NSGraphicsContext`), no dependencies.
- Create `scripts/make_icon.sh`: run the Swift script, build an `.iconset` with
  `sips` at the standard sizes (16…1024, @1x/@2x), run `iconutil -c icns`, and
  write `app/Resources/AppIcon.icns`.
- Commit the generated `app/Resources/AppIcon.icns`. Do not regenerate it on
  every build.
- In `scripts/build_app.sh`, copy `app/Resources/AppIcon.icns` into
  `Contents/Resources/` **before** the signing step.

**A3. Reopen the window from the Dock.** A SwiftUI `Window` scene does not
reliably reopen when the user clicks the Dock icon after closing it, and an
`NSApplicationDelegate` has no access to `openWindow`. Fix:

- Create `app/Sources/MeetingScribe/AppDelegate.swift` with
  `final class AppDelegate: NSObject, NSApplicationDelegate`:
  - `applicationShouldHandleReopen(_:hasVisibleWindows:)` → if no visible
    windows, call the stored open-window action; return `true`.
  - `applicationShouldTerminateAfterLastWindowClosed(_:)` → `false`. Closing
    the window must not quit the app.
- Store the action on `AppState`: `var openMainWindow: (() -> Void)?`.
- Capture it from a view that is always alive. `MenuBarLabel` (in
  `MeetingScribeApp.swift`) renders for the app's whole life. Add
  `@Environment(\.openWindow)` to it and, in `.onAppear`, set
  `state.openMainWindow = { openWindow(id: "main"); NSApp.activate(ignoringOtherApps: true) }`.
- Wire the delegate with `@NSApplicationDelegateAdaptor(AppDelegate.self)` in
  `MeetingScribeApp`. Give the delegate a reference to `AppState` (set it in
  `MeetingScribeApp.init` or on first appear — pick whichever compiles cleanly).
- Make `MenuBarContent.openMain()` call `state.openMainWindow` too, so there is
  one code path.

**A4. Open the window at launch.** A Dock app should show its window when
launched. In `applicationDidFinishLaunching`, call `state.openMainWindow` if it
is set; if it is not set yet, defer one run-loop tick
(`DispatchQueue.main.async`). Recommended default; override if SwiftUI already
opens the `Window` scene at launch once `LSUIElement` is gone — check first.

**A5. Check the app menu.** With a regular activation policy the app gets a
full menu bar. Confirm **Meeting Scribe → Settings…** (⌘,) opens the existing
`Settings` scene and **Window** lists "Meeting Scribe". No code expected here.

Commit: `feat(app): show Meeting Scribe in the Dock and reopen the window from it`

### Part B — One-click Notion connect

#### Flow

```
 App: [Connect Notion]
   │ POST /v1/integrations/notion/connect         (Bearer)
   ▼
 Daemon: state = "<port>.<nonce>", remember nonce (10 min TTL)
         open browser → BROKER/api/notion/authorize?state=…
   ▼
 Broker /authorize → 302 https://api.notion.com/v1/oauth/authorize
                        ?client_id&redirect_uri=BROKER/api/notion/callback
                        &response_type=code&owner=user&state
   ▼
 User approves in Notion, picks pages/databases to share
   ▼
 Broker /callback?code&state → 302 http://127.0.0.1:<port>/v1/integrations/notion/callback?code&state
   ▼
 Daemon /callback (no Bearer; state-checked)
   │ POST BROKER/api/notion/token {code}
   ▼
 Broker /token → POST https://api.notion.com/v1/oauth/token (Basic client_id:secret)
               ← {access_token, workspace_name, workspace_id, bot_id, refresh_token?, …}
   ▼
 Daemon: write outputs.notion.token (+ metadata), publish SSE "integrations_changed",
         return a small HTML page: "Notion connected — return to Meeting Scribe."
   ▼
 App: SSE → refreshIntegrations() → shows "Connected to <workspace>" + database picker
```

**B1. Broker.** Create `broker/` at the repo root as its own Vercel project:

| File | Behavior |
|---|---|
| `broker/package.json` | name `meeting-scribe-broker`, `"type": "module"`, devDependency `@vercel/node` for types only. |
| `broker/tsconfig.json` | Minimal, strict. |
| `broker/api/notion/authorize.ts` | `GET ?state=`. Validate `state` matches `^\d{2,5}\.[A-Za-z0-9_-]{16,}$`. 302 to Notion's authorize URL with `client_id`, `redirect_uri`, `response_type=code`, `owner=user`, `state`. 400 on bad state. |
| `broker/api/notion/callback.ts` | `GET ?code=&state=` (or `?error=`). Re-validate `state`, parse the port, 302 to `http://127.0.0.1:<port>/v1/integrations/notion/callback` with `code`, `state`, and `error` passed through. The redirect host is hard-coded to `127.0.0.1` — never take a host from input. |
| `broker/api/notion/token.ts` | `POST {code}` → Notion token endpoint, `grant_type=authorization_code`, same `redirect_uri`. `POST {refresh_token}` → `grant_type=refresh_token`. Return Notion's JSON and status as-is. Reject anything else with 400. |
| `broker/README.md` | Env vars, deploy command, the redirect URI to register in Notion. |

Env vars: `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`, `NOTION_REDIRECT_URI`
(e.g. `https://<broker-host>/api/notion/callback`). Never log the request body
or Notion's response body. Log only status codes.

**B2. Config.** In `meeting_scribe/config.py` add
`Config.notion_broker_url` → env `MEETING_SCRIBE_BROKER_URL`, else
`notion.broker_url`, else a module constant `DEFAULT_BROKER_URL`. Set the
constant to `""` for now; the owner fills it in after deploy (§6). Add to
`config.example.json`:

```json
"notion": { "broker_url": "" }
```

and under `outputs.notion` add `"workspace_name": ""`, `"refresh_token": ""`.
Add `("outputs", "notion", "refresh_token")` to `SECRET_PATHS` in `server.py`.

**B3. Daemon OAuth module.** Create `meeting_scribe/integrations/notion_auth.py`
(sibling of `google_auth.py`):

- `start(cfg, port) -> str`: make `nonce = secrets.token_urlsafe(24)`, store it
  in a module-level dict with an expiry (10 min), return the broker authorize
  URL with `state=f"{port}.{nonce}"`. Raise `NotionAuthError` if the broker URL
  is empty, with a message that names `notion.broker_url`.
- `finish(cfg, code, state) -> dict`: check and consume the nonce (single use),
  POST `{code}` to `<broker>/api/notion/token`, and on success merge into
  config: `outputs.notion.token`, `refresh_token` (if present),
  `workspace_name`, and `enabled: true`. Write config the same way
  `PUT /v1/config` does (reuse its merge + write; extract a helper if needed).
  Return `{workspace_name}`.
- `refresh(cfg) -> bool`: if a `refresh_token` is stored, POST it to the broker,
  save the new tokens, return `True`.
- `disconnect(cfg)`: clear `token`, `refresh_token`, `workspace_name`,
  `database_id`; set `enabled: false`.
- `list_databases(cfg) -> list[dict]`: `POST https://api.notion.com/v1/search`
  with `filter: {"property": "object", "value": "database"}`, page through
  `next_cursor`. Return `[{id, title, title_property, date_property}]`, where
  `title_property` is the name of the property with `type == "title"` and
  `date_property` is the name of the first `type == "date"` property (or `""`).

**B4. Daemon routes** in `meeting_scribe/daemon/server.py`, next to the Google
routes:

| Route | Auth | Behavior |
|---|---|---|
| `POST /v1/integrations/notion/connect` | Bearer | `url = notion_auth.start(cfg, port)`; `webbrowser.open(url)`; return `{ok, url}` immediately. Do **not** block like the Google route. |
| `GET /v1/integrations/notion/callback` | **none** — the browser cannot send the Bearer token. The single-use nonce is the guard. | On `error` or bad/expired state → HTML error page, status 400. Else `finish()`, `BUS.publish("integrations_changed")`, return HTML success page. Keep both pages plain and short. |
| `POST /v1/integrations/notion/disconnect` | Bearer | `disconnect()`, publish `integrations_changed`. |
| `GET /v1/integrations/notion/databases` | Bearer | `list_databases()`. 409 if not connected. |
| `PUT /v1/integrations/notion/database` | Bearer | Body `{id, title_property, date_property}` → write `outputs.notion.database_id`, `title_property`, `date_property`. |

The callback route must sit outside the router or dependency that applies
`require_token` (see how routes are registered around line 253). Check this
with a real request, not by reading the code.

Extend `GET /v1/integrations` with a `notion` block, like `google`:
`{"connected": bool(token), "workspace_name": str, "database_id": str,
"oauth_available": bool(broker_url)}`.

**B5. Notion writer.** In `meeting_scribe/outputs/notion.py`:

- If `date_property` is `""`, omit the date property from the payload (a
  database with no date column must still work).
- On a 401 from create-page, call `notion_auth.refresh(cfg)` once; if it returns
  `True`, reload config and retry once. Otherwise raise the existing
  `NotionError` with the message "Notion access expired — reconnect in Settings."

**B6. App client + models** in `app/Sources/MeetingScribe/DaemonAPI.swift`:

- `IntegrationsResponse.NotionInfo { connected, workspaceName, databaseId, oauthAvailable }`.
  Match the decoder's existing key strategy (the Google block decodes
  `client_configured` as `clientConfigured`, so snake-case conversion is on).
- `struct NotionDatabase: Codable, Identifiable { id, title, titleProperty, dateProperty }`.
- Methods: `connectNotion()`, `disconnectNotion()`, `notionDatabases()`,
  `setNotionDatabase(_:)`.

In `AppState.swift`, handle the `integrations_changed` SSE event by calling
`refreshIntegrations()`.

**B7. Settings UI** — replace the Notion section in `SettingsView.swift`:

- **Not connected, OAuth available:** a **Connect Notion** button. While
  waiting, show "Waiting for Notion in your browser…" with a Cancel button that
  only resets the local UI state.
- **Connected:** "Connected to <workspace_name>" (green check), a **Database**
  `Picker` filled from `notionDatabases()` (load on appear, plus a small
  refresh button), and a **Disconnect** button. Saving the picker calls
  `setNotionDatabase`. If the chosen database has no date property, show a
  one-line caption: "No date column — notes will be filed without a date."
- If the database list is empty, show: "No databases shared yet. Click Connect
  Notion again and select a database on the Notion page." (Reconnecting is how
  Notion lets a user share more pages.)
- **Advanced disclosure** (`DisclosureGroup`, collapsed): the existing token and
  database ID fields and "Save Notion Settings" button, unchanged. Also the
  only path shown when `oauthAvailable` is false.

**B8. CLI.** In `meeting_scribe/cli.py`, add `scribe notion connect` beside
`scribe google connect`: call the daemon's connect route via `client.py`, print
the URL, then poll `GET /v1/integrations` every 2 s for up to 5 min until
`notion.connected` is true. Update the `scribe doctor` Notion line to print the
workspace name when connected.

**B9. Docs.** In `README.md`, rewrite "Notion setup": step 1 is Settings →
Connect Notion → select your meetings database. Move the internal-integration
steps under "Manual setup (advanced)". Add one line to "Project layout" for
`broker/`.

Commit: `feat(notion): one-click OAuth connect via a stateless token broker`

---

## 6. Owner steps (do not do these — list them in the PR description)

1. In Notion → **My integrations** → create a **Public** integration. Capabilities:
   Read content, Insert content, Update content. Redirect URIs:
   `https://<broker-host>/api/notion/callback` and
   `http://localhost:3000/api/notion/callback` (for local testing).
2. `cd broker && vercel link && vercel env add NOTION_CLIENT_ID / NOTION_CLIENT_SECRET / NOTION_REDIRECT_URI`, then `vercel deploy --prod`.
3. Set `DEFAULT_BROKER_URL` in `meeting_scribe/config.py` to the deployed host
   (or `notion.broker_url` in `config.json`).

---

## 7. Verification

Prove each part end to end. Typecheck and lint alone are not enough.

### Part A

1. Build and install: `SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk bash scripts/build_app.sh --install`.
2. `plutil -p /Applications/MeetingScribe.app/Contents/Info.plist` — no
   `LSUIElement`; `CFBundleIconFile` = `AppIcon`; `CFBundleIdentifier` and
   `NSMicrophoneUsageDescription` unchanged.
3. `codesign -dr - /Applications/MeetingScribe.app` — the designated
   requirement still contains `certificate leaf = H"…"`, not `cdhash`.
4. `open /Applications/MeetingScribe.app`. Take a screenshot
   (`screencapture -x`) and confirm the icon is in the Dock and the main window
   is open.
5. Close the window with ⌘W. The app stays running (`pgrep -x MeetingScribe`).
   Click the Dock icon (or `open -a MeetingScribe` again, which triggers
   reopen). The window returns. Screenshot it.
6. ⌘, opens Settings. The menu-bar item still exists (check with
   `osascript -e 'tell application "System Events" to get menu bar items of menu bar 2 of process "MeetingScribe"'`).
7. Start and stop a short recording from the window. Confirm a meeting appears
   and no new TCC prompt fires (grants survived).

### Part B — without real Notion credentials

1. `cd broker && npx vercel dev --listen 3000` with dummy env vars. Then:
   - `curl -si 'localhost:3000/api/notion/authorize?state=48237.abcdefghijklmnopqrst'` → 302 to `api.notion.com/v1/oauth/authorize` with all params.
   - `curl -si 'localhost:3000/api/notion/authorize?state=evil.com'` → 400.
   - `curl -si 'localhost:3000/api/notion/callback?code=x&state=48237.abcdefghijklmnopqrst'` → 302 to `http://127.0.0.1:48237/v1/integrations/notion/callback?code=x&state=…`.
2. Daemon, with `notion.broker_url` pointed at a local fake broker (a
   20-line Python `http.server` whose `/api/notion/token` returns a canned
   `{"access_token": "fake", "workspace_name": "Test WS"}`):
   - `POST /v1/integrations/notion/connect` with the Bearer token → returns a URL; read the `state` from it.
   - `curl -s "http://127.0.0.1:48237/v1/integrations/notion/callback?code=x&state=<state>"` **without** a Bearer header → success HTML.
   - Replay the same request → 400 (nonce is single-use).
   - `GET /v1/integrations` → `notion.connected: true`, `workspace_name: "Test WS"`.
   - `GET /v1/config` → `outputs.notion.token` is `•••` (redacted).
   - `POST /v1/integrations/notion/disconnect` → `connected: false`.
3. In the app, open Settings with the fake broker connected and screenshot the
   Connected state; disconnect and screenshot the Connect button and the
   collapsed Advanced section.

### Part B — with real Notion credentials (only if the owner has done §6 step 1)

If `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` are available in the
environment, run the broker with `vercel dev` on port 3000, set
`notion.broker_url` to `http://localhost:3000`, click **Connect Notion**,
approve in the browser, pick a database, then run
`scribe process <an existing meeting id>` and confirm a page appears in that
database (open the returned URL). If the credentials are not available, say so
in the PR under "How tested" — do not fake this step.

### Regression

- `scribe doctor` runs clean.
- A config with a manual `outputs.notion.token` and `database_id` (no OAuth
  keys) still pushes to Notion unchanged.

---

## 8. Open questions (proceed with the recommendation)

| Question | Recommendation |
|---|---|
| Does Notion return a `refresh_token` / expiring tokens for public integrations? | Handle both. Store and use `refresh_token` if present; if absent, tokens are long-lived and B5's 401 path just tells the user to reconnect. |
| Should the public integration offer a Notion **template** (a ready "Meetings" database duplicated on consent)? | Not in code this round. Mention in the PR as a follow-up the owner can turn on in Notion's integration settings; the token response's `duplicated_template_id` would then be the default database. |
| Should the success page try `window.close()`? | Yes, attempt it; browsers often block it, so the page text must still say "You can close this tab." |
