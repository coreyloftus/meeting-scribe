# meeting-scribe

Record a meeting on your Mac, transcribe it **locally** with whisper.cpp, then use
Claude to turn it into labelled notes, action items, and key decisions — filed
automatically into Notion and/or a local markdown vault.

- **No virtual audio device, no menu-bar app.** System audio is captured with
  Apple's **ScreenCaptureKit** (macOS 13+) — so there's nothing living in your
  menu bar, and your volume keys keep working. (No BlackHole, no Background Music.)
- **Speaker labels for free.** Your mic and the system audio are recorded to
  separate files, so the transcript is a real `Me:` / `Them:` dialogue.
- **Local transcription.** Audio never leaves your machine for transcription —
  whisper.cpp runs on-device. Only the (text) transcript is sent to Claude.
- **Config-driven.** Model, API keys, prompts, and output destinations all live in
  a `config.json`. Bring your own keys.

---

## How it works

```
            ┌──────────────────────────┐
 system  →  │ bin/syscap (Swift / SCK)  │ →  <stamp>.system.wav  ┐
            └──────────────────────────┘                        │  resample each
            ┌──────────────────────────┐                        │  independently
 mic     →  │ ffmpeg (avfoundation)     │ →  <stamp>.mic.wav     ┘  → 16 kHz mono
            └──────────────────────────┘                        │
                                                                ▼
                              whisper.cpp per channel  →  Me: … / Them: … transcript
                                                                ▼
                                  Claude (Anthropic API)  →  slug + action-items summary
                                                                ▼
                                       outputs:  Notion API  +  local markdown
```

---

## Prerequisites

macOS 13 (Ventura) or newer, plus:

```bash
# CLI tools
brew install ffmpeg whisper-cpp switchaudio-osx
xcode-select --install   # provides swiftc to build the helper

# A whisper model (English base is a good default; medium is more accurate)
mkdir -p ~/.local/share/whisper-cpp/models
curl -L -o ~/.local/share/whisper-cpp/models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

You'll also need an **Anthropic API key** (<https://console.anthropic.com>), and —
if you want Notion output — a **Notion integration token** and a database ID.

---

## Install

```bash
git clone <this repo> meeting-scribe && cd meeting-scribe

# 1. Build the system-audio helper (compiles bin/syscap)
bash scripts/build_helper.sh

# 2. Install the CLI (creates the `scribe` command)
pip install -e .          # or: pipx install .

# 3. Create your config and add your keys
scribe config --init      # writes ~/.config/meeting-scribe/config.json
$EDITOR ~/.config/meeting-scribe/config.json

# 4. Sanity-check everything
scribe doctor
```

### Grant Screen Recording permission

ScreenCaptureKit needs **Screen Recording** permission for whatever terminal you
run `scribe` from (Terminal, iTerm, VS Code, …):

> System Settings → Privacy & Security → Screen Recording → enable your terminal,
> then fully quit and reopen it.

The first `scribe start` will fail with a clear message if this isn't granted.

---

## Usage

```bash
scribe start      # begins recording system audio + mic
# … have your meeting …
scribe stop       # stops, transcribes, summarises, and files the notes

# Reprocess a past recording (or any audio file) without re-recording:
scribe process ~/Recordings/meetings/2026-06-13_10-00-00
scribe process ~/Downloads/some-call.m4a
```

`scribe stop` prints where each note landed (markdown path, Notion URL). If
processing fails for any reason, your raw audio is kept — just rerun
`scribe process <base-path>`.

### Using AirPods (or any Bluetooth headset)

When macOS uses a Bluetooth headset's *microphone*, the link drops into
hands-free (HFP) mode and the mic is captured at telephone quality (≤24 kHz).
If `recording.mic_device` is `null`, scribe follows the system default input —
so the moment your AirPods connect, your side of the transcript degrades.

The fix costs nothing: what the *call* uses and what *scribe records* are
independent. Pin scribe to the built-in mic and keep wearing the AirPods:

```jsonc
"recording": { "mic_device": "MacBook Pro Microphone" }
```

Your meeting app keeps using the AirPods both ways; scribe records your voice
acoustically at 48 kHz from the built-in mic. There's no crosstalk — the other
side plays into your headphones, so the room mic only hears you. The one
trade-off: if you walk out of the room, your side goes quiet in the recording.

---

## Desktop app & daemon (Granola-style)

The menu-bar app and the `scribed` daemon add live status, background
processing, a meetings browser, live in-meeting notes, and one-click pushes.
See `docs/desktop-app-spec.md` for the full design.

```bash
# install python deps for the daemon (+ Google outputs)
.venv/bin/pip install -e ".[daemon,google]"

# build & run the menu-bar app (starts the daemon itself if needed)
bash scripts/build_app.sh --install
open /Applications/MeetingScribe.app

# or drive everything from the CLI — it talks to the daemon when it's running
scribe daemon serve            # foreground daemon (dev)
scribe daemon install          # launchd LaunchAgent (see note below)
scribe daemon status
scribe list                    # meetings index, statuses, output links
scribe process <meeting-id>    # reprocess via the daemon (background job)
scribe start --local           # bypass the daemon entirely (original behavior)
```

- **Live notes:** while recording, type rough bullets in the app's notes pane —
  they're saved next to the audio (`<base>.notes.md`) and woven into the Claude
  summary as high-signal anchors. Edit notes later + **Reprocess** to re-summarize.
- **Everything is rebuildable:** the SQLite index at
  `~/.local/state/meeting-scribe/meetings.db` is a cache over the recordings
  dir + markdown notes; delete it and the daemon re-scans on next start.
- **API:** `http://127.0.0.1:48237/v1/…` with `Authorization: Bearer
  $(cat ~/.local/state/meeting-scribe/daemon.token)`; SSE stream on `/v1/events`.
- **launchd caveat:** under `scribe daemon install`, macOS folder-privacy
  prompts can't appear (the daemon may not be able to read your recordings
  folder). Prefer letting the app start the daemon, or grant Python
  Full Disk Access if you want the LaunchAgent.

### Google Docs & Drive outputs

One-time setup: create a GCP project, enable the **Drive API**, create an
OAuth client of type **Desktop app**, then:

```bash
scribe config             # put client_id/client_secret under "google" in config.json
scribe google connect     # browser consent; token cached at ~/.config/meeting-scribe/google_token.json
```

Enable `outputs.gdrive` (md/txt file upload) and/or `outputs.gdocs` (native
Google Doc via Drive's markdown import) in config or the app's Settings —
they then run on every `stop` and appear as push buttons per meeting.

---

## Configuration

`config.json` is discovered in this order: `$MEETING_SCRIBE_CONFIG`, then
`./config.json`, then `~/.config/meeting-scribe/config.json`. Secrets can live in
the file **or** in the environment (`ANTHROPIC_API_KEY`, `NOTION_TOKEN` — env wins).

```jsonc
{
  "anthropic": {
    "backend": "api",                    // "api" (key) or "claude_cli" (local Claude Code)
    "api_key": "",                       // or set ANTHROPIC_API_KEY  (backend "api" only)
    "claude_cli": "claude",              // command to run            (backend "claude_cli" only)
    "model": "claude-haiku-4-5-20251001",// haiku = cheap/fast; sonnet/opus = higher quality
    "max_tokens": 4096
  },
  "recording": {
    "dir": "~/Recordings/meetings",
    "capture_system_audio": true,
    "capture_mic": true,
    "mic_device": null                   // null = current default input; or "MacBook Pro Microphone"
  },
  "transcription": {
    "whisper_cli": "whisper-cli",
    "whisper_model": "~/.local/share/whisper-cpp/models/ggml-base.en.bin",
    "language": "auto",
    "threads": 8,
    "speaker_labels": true,
    "me_label": "Me",
    "them_label": "Them"
  },
  "prompts": {
    "slug": "…",                          // customise the filename/title slug prompt
    "summary": "…"                        // customise the summary/action-items format
  },
  "outputs": {
    "markdown": { "enabled": true,  "dir": "~/Documents/meeting-transcripts" },
    "notion":   { "enabled": false, "token": "", "database_id": "",
                  "title_property": "Name", "date_property": "Date" }
  }
}
```

### Which Claude backend? (billing)

`anthropic.backend` chooses how the summary is generated:

- **`"api"`** — calls the Anthropic API with `anthropic.api_key` (or `ANTHROPIC_API_KEY`).
  Billed as **pay-as-you-go API usage** at console.anthropic.com, separate from any
  Claude.ai subscription. This is the portable option — anyone with a key can run it.
- **`"claude_cli"`** — shells out to your local **`claude`** command (Claude Code) with
  `claude -p`. It runs against **whatever that CLI is logged in with** — so if you're
  signed into Claude Code on a Pro/Max plan, this uses your **subscription** instead of
  API credits, and no API key is needed. For this backend, set `model` to a Claude Code
  alias (`haiku`, `sonnet`, `opus`) or a full model ID.

Either way, only the text transcript is sent to Claude — never the audio.

### Notion setup

1. Create an internal integration at <https://www.notion.com/my-integrations> and
   copy its token (`ntn_…` / `secret_…`).
2. Open the target database → **⋯ → Connections → add your integration**.
3. Copy the database ID from its URL
   (`notion.so/<workspace>/<DATABASE_ID>?v=…`).
4. Set `outputs.notion.enabled: true`, fill in `token` and `database_id`, and make
   sure `title_property` / `date_property` match your database's column names.

The Notion writer talks to the Notion REST API directly — it does **not** depend on
Claude Code or any MCP server, so it's fully portable.

---

## AirPods (and other Bluetooth headsets)

When AirPods are used as a **microphone**, macOS switches them into Bluetooth
hands-free (HFP) mode — which drops everything to 24 kHz and noticeably degrades
audio quality. Because meeting-scribe captures system audio via ScreenCaptureKit
(not the AirPods mic), the fix is simple: **set your input to the built-in
MacBook microphone and leave AirPods as output only.** Your AirPods stay in
high-quality A2DP mode and the mic stays at full quality.

`scribe` will warn you after a recording if it detects the mic was captured below
32 kHz.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `System-audio helper exited immediately` | Grant Screen Recording permission to your terminal, then restart it. |
| `swiftc: ... SDK is not supported by the compiler` | Your Command Line Tools are out of date/mismatched. `softwareupdate -i "Command Line Tools for Xcode <version>"` (or reinstall via `xcode-select --install`), then re-run `scripts/build_helper.sh`. |
| `whisper model not found` | Download a `ggml-*.bin` model and point `transcription.whisper_model` at it. |
| System channel is silent | Make sure something was actually playing through your **system output** during the meeting. |
| Other person sounds slow / underwater | Shouldn't happen anymore — but if it does, you're on an old recording; re-record. The new pipeline resamples each channel independently. |

---

## Project layout

```
helper/syscap.swift        ScreenCaptureKit system-audio capture (compiles to bin/syscap)
scripts/build_helper.sh    Builds the helper
scripts/build_app.sh       Builds MeetingScribe.app (menu-bar SwiftUI app)
app/                       SwiftUI app: menu-bar badge, meetings window, live notes
meeting_scribe/
  cli.py                   `scribe` entry point (daemon-aware; --local fallback)
  client.py                HTTP client for the daemon (used by the CLI)
  recorder.py              Starts/stops the two capture processes; session state
  audio.py                 ffmpeg resample + level/quality checks (the bug fix lives here)
  transcribe.py            whisper.cpp per-channel + Me/Them speaker labelling
  llm.py                   Anthropic API: slug + summary (+ user-notes weaving)
  process.py               Pipeline stages: transcribe → summarise → write outputs
  outputs/                 Plugin registry: markdown, notion, gdrive, gdocs
  integrations/            google_auth.py — desktop OAuth + Drive upload helper
  daemon/                  scribed: FastAPI server, SQLite index, job queue, SSE, launchd
config.example.json        Template config
```

---

## Privacy

Audio is transcribed entirely on your machine. Only the resulting **text**
transcript is sent to the Anthropic API (for the summary) and, if enabled, to
Notion. Nothing is uploaded unless you configure an output that does so.
