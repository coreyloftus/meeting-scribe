# Bundle MeetingScribe.app into a self-contained, distributable macOS app

**Status:** Ready for implementation
**Date:** 2026-07-30
**Audience:** the engineering agent implementing this. You have none of the
conversation context that produced this document; everything you need is here.

---

## 0. Instructions to the executing agent

1. **Read this entire document before writing any code.** Section 3 lists things
   that must not be touched — several are hard-won bug fixes that look like dead
   code and are not.
2. **Work autonomously.** Make reasonable decisions without checking in.
   Recommended defaults may be overridden with good reason. **Locked decisions
   (§2) may not** — do not relitigate them.
3. **Read §4 (Known blocker) before your first build.** The SwiftUI app does not
   currently compile on this machine's toolchain. You will hit this within
   minutes if you skip it.
4. **Finish by** creating a fresh branch off `main`, committing with
   conventional-commit messages, and opening a PR. **Base branch is `main`** —
   this repo has no `dev` branch.

---

## 1. Goal

Today `MeetingScribe.app` is a personal build: it only runs on a machine that
has this git repo checked out, a Python venv at `~/.local/bin/scribed`, and
Homebrew tools on `PATH`. Hand the `.app` to anyone else and it launches to a
dead UI, because the daemon and helper it shells out to do not exist.

When this work is done, there is a **`MeetingScribe.dmg`** that a user can
download, drag to `/Applications`, right-click → Open once, and use. The app
carries its own Python runtime, its own `syscap` helper, and its own copy of the
`meeting_scribe` package. On first launch it checks for the four Homebrew CLI
tools and the whisper model, and walks the user through installing whatever is
missing. No git checkout, no `pip install`, no venv, no `PATH` surgery.

---

## 2. Locked decisions

Decided by the product owner. Do not relitigate.

| Area | Decision | Consequence |
|---|---|---|
| **Signing** | **Self-signed, not notarized** | Free. Gatekeeper will say "unidentified developer"; the documented install step is right-click → Open (or `xattr -dr com.apple.quarantine`). Do **not** add a `$99/yr` Developer ID or notarization step. |
| **Python** | **Embed a standalone runtime in the bundle** | Vendor `python-build-standalone` into `Contents/Resources/`. Adds ~40MB. The app must never depend on system, Homebrew, or user Python. |
| **Homebrew CLI tools** | **User prerequisite, app-assisted** | `ffmpeg`, `ffprobe`, `whisper-cli`, `SwitchAudioSource` stay as Homebrew installs. The app detects them and offers to run `brew install`. |
| **No relocating Homebrew binaries** | **Do not copy brew binaries into the bundle** | `ffmpeg` alone has 56 dylib references hardcoded to `/opt/homebrew/Cellar/...`. Copying it out of a prefix and deleting the prefix breaks all 56. Rewriting them with `install_name_tool` is fragile and re-breaks the signature. This was explicitly evaluated and rejected. |
| **Signing identity** | **Reuse `scripts/setup_signing.sh`** | A stable identity is mandatory, not cosmetic — see §3. |

### Recommended defaults (override only with good reason)

- **Python runtime source:** `astral-sh/python-build-standalone`, CPython
  **3.12**, `aarch64-apple-darwin`, `install_only` variant. Pin the release tag
  in the build script; do not track "latest".
- **Architecture: `arm64` only.** `scripts/build_app.sh` already targets
  `arm64-apple-macos14.0`. Universal binaries are a non-goal (§5).
- **Whisper model:** download `ggml-base.en.bin` + `ggml-silero-v5.1.2.bin` from
  `huggingface.co/ggerganov/whisper.cpp` on first run, into the existing
  configured location `~/.local/share/whisper-cpp/models/`. ~150MB; show
  progress. Do **not** ship models inside the `.dmg`.
- **Disk image:** plain `hdiutil` DMG with an `/Applications` symlink. No
  fancy background art.

---

## 3. Context & constraints

### Stack

| Piece | Language | Source |
|---|---|---|
| Menu-bar UI | SwiftUI, built with direct `swiftc` | `app/Sources/MeetingScribe/*.swift` |
| System-audio helper | Swift + ScreenCaptureKit | `helper/syscap.swift` → `bin/syscap` |
| Daemon `scribed` | Python, FastAPI + uvicorn | `meeting_scribe/daemon/` |
| Pipeline | Python | `meeting_scribe/{recorder,process,transcribe,audio,llm}.py` |

The app talks to the daemon over authenticated localhost HTTP; the daemon owns
recording and the job queue. `meeting_scribe/daemon/server.py` writes
`~/.local/state/meeting-scribe/daemon.json` (host/port/pid) and `daemon.token`.

### The two path assumptions that block distribution

Both must be fixed:

```
meeting_scribe/config.py:19    REPO_ROOT = Path(__file__).resolve().parent.parent
meeting_scribe/recorder.py:28  HELPER_BIN = REPO_ROOT / "bin" / "syscap"
app/Sources/MeetingScribe/AppState.swift:85
                              nohup "~/.local/bin/scribed" serve
```

`REPO_ROOT` is also used for `config.example.json` (`config.py:21`) and config
discovery (`config.py:38`).

### External binaries the daemon shells out to

Enumerated in `meeting_scribe/daemon/server.py:519` and `meeting_scribe/cli.py:271`:

| Tool | Used by | Brew formula |
|---|---|---|
| `ffmpeg` | mic capture, resample/mix | `ffmpeg` |
| `ffprobe` | duration probing | `ffmpeg` |
| `whisper-cli` | transcription | `whisper-cpp` |
| `SwitchAudioSource` | default-input detection | `switchaudio-osx` |

The LLM step uses either the `claude` CLI or an Anthropic API key
(`meeting_scribe/llm.py:51`, `config.py`). Treat it as optional — a meeting must
still record and transcribe without it.

### DO NOT TOUCH

These look wrong and are not. Changing them re-introduces fixed bugs.

- **`recorder.py:_stop_pid` sends SIGTERM twice, then SIGKILL.** Homebrew ffmpeg 8
  absorbs the first signal during avfoundation capture. Collapsing this to one
  signal regresses stop latency from ~1s to 20s+.
- **`recorder.py` captures with `-flush_packets 1`.** Without it the force-exit
  path drops the last seconds of the meeting.
- **`helper/syscap.swift` sets `config.excludesCurrentProcessAudio = false`.**
  `true` yields pure silence on macOS 15+ via a broken per-process tap.
- **`Info.plist` must keep `NSMicrophoneUsageDescription`.** ffmpeg captures the
  mic as a child of the daemon, so TCC attributes the request to the app bundle.
  With no usage string macOS **kills** ffmpeg instead of prompting, and
  Microphone has no "+" button in System Settings — so it becomes ungrantable.
  Symptom is distinctive: `.ffmpeg.log` holds only the version banner, no error.
- **The app must be signed with the stable identity from
  `scripts/setup_signing.sh`, never ad-hoc.** Ad-hoc signing produces
  `designated => cdhash H"..."`, so macOS pins the Screen Recording grant to the
  exact binary and **every rebuild silently revokes it** — while System Settings
  still displays the app as allowed. The self-signed identity yields
  `designated => identifier "com.meetingscribe.app" and certificate leaf = H"..."`,
  which survives rebuilds. `scripts/build_app.sh` and `scripts/build_helper.sh`
  already do this and warn on fallback. Preserve that behavior.
- **`meeting_scribe/daemon/server.py` self-prepends Homebrew dirs to `PATH` at
  startup.** GUI-spawned daemons inherit a bare `PATH`.

---

## 4. Known blocker — read before your first build

**The SwiftUI app does not compile on this machine right now.** Command Line
Tools 27.0 beta 4 (installed 2026-07-27) made `@State` a macro, and bare CLT
ships no `SwiftUIMacros` plugin:

```
$ ls /Library/Developer/CommandLineTools/usr/lib/swift/host/plugins/
libObservationMacros.dylib  libSwiftMacros.dylib  testing
```

Every `@State` fails with *"external macro implementation type
'SwiftUIMacros.StateMacro' could not be found"* (~48 errors), and downstream
`$binding` uses cascade into misleading *"cannot find '$x' in scope"* errors.
**Those cascade errors are noise — do not chase them.**

Resolve by installing full Xcode (ships the plugin) or downgrading CLT, then
verify with:

```bash
bash scripts/build_app.sh   # must succeed before you proceed
```

There is one uncommitted, not-yet-compiled change in
`app/Sources/MeetingScribe/AppState.swift`: a `CGPreflightScreenCaptureAccess`
guard in `startRecording()`. It parses clean but has never been compiled.
Validate it as part of your first successful build.

Note that **re-signing and `Info.plist` edits need no toolchain** — only Swift
changes do. If you cannot resolve the toolchain, you can still complete every
Python, packaging, and build-script step in this spec; say so explicitly in the
PR rather than silently skipping the Swift work.

---

## 5. Non-goals

- Notarization, Developer ID, App Sandbox, Mac App Store.
- Universal (`x86_64`) builds. `arm64` only.
- Replacing `ffmpeg` with native AVFoundation capture, or `whisper-cli` with a
  linked library. Both are worth doing later; not now.
- Auto-update / Sparkle.
- Bundling Homebrew itself, or a private Homebrew prefix.
- Any change to the recording, transcription, or LLM pipeline behavior. This is
  a packaging change. If you find a pipeline bug, note it in the PR; don't fix
  it here.

---

## 6. Implementation plan

Each step should leave the repo in a working state.

### Step 1 — Bundle-aware resource resolution

**Files:** `meeting_scribe/config.py`, `meeting_scribe/recorder.py`

Introduce a single resolver that works in both dev (git checkout) and bundled
modes. Add to `config.py`:

```python
# Set by the app to Contents/Resources when running from a bundle.
_BUNDLE = os.environ.get("MEETING_SCRIBE_RESOURCES")
RESOURCE_ROOT = Path(_BUNDLE) if _BUNDLE else REPO_ROOT
```

Keep `REPO_ROOT` exported (other modules import it), but repoint the
resource-derived constants at `RESOURCE_ROOT`:

- `EXAMPLE_CONFIG` (`config.py:21`)
- config discovery candidates (`config.py:38`)
- `HELPER_BIN` (`recorder.py:28`) → `RESOURCE_ROOT / "bin" / "syscap"`

Add a fallback in `recorder.py`: if `HELPER_BIN` is missing, try
`shutil.which("syscap")` before raising, so a dev checkout still works.

**Verify:** `MEETING_SCRIBE_RESOURCES=/tmp/foo python -c "from meeting_scribe.recorder import HELPER_BIN; print(HELPER_BIN)"` prints `/tmp/foo/bin/syscap`.

### Step 2 — Vendor the Python runtime

**Files:** `scripts/fetch_python.sh` (new)

Download and unpack `python-build-standalone` (CPython 3.12,
`aarch64-apple-darwin`, `install_only`) into `vendor/python/` (gitignored). Pin
the release tag and verify the published SHA256 — fail loudly on mismatch.
Make it idempotent: skip if `vendor/python/bin/python3` already exists.

Add `vendor/` to `.gitignore`.

### Step 3 — Build the bundle payload

**Files:** `scripts/build_app.sh`

Target layout:

```
MeetingScribe.app/Contents/
├── Info.plist
├── MacOS/
│   ├── MeetingScribe            SwiftUI app
│   └── syscap                   moved from bin/syscap
└── Resources/
    ├── python/                  vendored runtime
    ├── lib/                     meeting_scribe + pip deps
    ├── bin/syscap               symlink → ../../MacOS/syscap
    └── config.example.json
```

`bin/syscap` as a symlink keeps Step 1's `RESOURCE_ROOT / "bin" / "syscap"`
working while the real executable stays in `MacOS/` where macOS expects it.

Install the package and deps into `Resources/lib` with the vendored interpreter:

```bash
vendor/python/bin/python3 -m pip install --target "$RES/lib" ".[daemon,google]"
```

**Sign inner executables before the outer bundle** — nested code must be signed
first or the outer seal is invalid. Order: `syscap` → any `.dylib`/`.so` under
`Resources` → `MeetingScribe.app`. Then verify:

```bash
codesign --verify --deep --strict /Applications/MeetingScribe.app
```

Keep the existing identity lookup and ad-hoc fallback warning.

### Step 4 — Launch the bundled daemon

**Files:** `app/Sources/MeetingScribe/AppState.swift`

Replace the hardcoded `~/.local/bin/scribed` spawn (`startDaemon()`, ~line 80)
with the bundled runtime. Resolve `Bundle.main.resourceURL` and exec:

```
<Resources>/python/bin/python3 -m meeting_scribe.daemon serve
```

with environment:

| Var | Value |
|---|---|
| `PYTHONPATH` | `<Resources>/lib` |
| `MEETING_SCRIBE_RESOURCES` | `<Resources>` |
| `PYTHONHOME` | `<Resources>/python` |
| `PATH` | `/opt/homebrew/bin:/usr/local/bin:$PATH` |

**Ordering matters, and today's code has it backwards.** `startDaemon()`
currently tries `launchctl kickstart` *first* and only falls back to spawning
scribed itself. Invert that: the bundled spawn becomes the primary path, and
the `launchctl kickstart` branch stays only as a fallback for users who already
installed the LaunchAgent.

**Why:** TCC resolves the responsible process up the spawn chain, so the app's
Screen Recording and Microphone grants cover `syscap`/`ffmpeg` **only when the
app spawns the daemon**. Under launchd the daemon is a child of launchd and
carries its own grants; a daemon started from a terminal inherits the
*terminal's* identity. All three are workable, but only the app-spawned path is
the one the bundle can set up on the user's behalf, so it must be preferred.

Because those three cases exist, **never gate recording on the app's own
preflight** — `CGPreflightScreenCaptureAccess()` describes this app, not
whichever process TCC holds responsible for `syscap`. Treat a missing grant as
a hint attached to a failure, never as a precondition (see
`AppState.nudgeScreenRecordingAccess`).

### Step 5 — First-run setup window

**Files:** `app/Sources/MeetingScribe/SetupView.swift` (new),
`app/Sources/MeetingScribe/AppState.swift`

On launch, if required tools are missing, show a setup window instead of the
normal UI. Reuse the existing doctor endpoint (`server.py:519`) rather than
reimplementing detection — but note it needs the daemon running, so also do a
cheap pre-daemon check in Swift for the four binaries.

Per row: tool name, found/missing, resolved path. A single **Install missing
dependencies** button runs:

```bash
brew install ffmpeg whisper-cpp switchaudio-osx
```

streaming output into a scrollable text view. If `brew` itself is absent, do
**not** try to install it (needs sudo + interactive); link to
`https://brew.sh` with a copyable command.

Add a **Re-check** button. When all green, dismiss to the normal UI.

### Step 6 — Whisper model bootstrap

**Files:** `meeting_scribe/transcribe.py`, setup UI from Step 5

`config.example.json` already points at
`~/.local/share/whisper-cpp/models/ggml-base.en.bin` and
`ggml-silero-v5.1.2.bin`. Neither ships with the brew formula.

Add a model-presence check to the setup flow and a **Download models** action
that fetches both from `huggingface.co/ggerganov/whisper.cpp` with a progress
indicator. Download to a `.part` file and rename on completion so an interrupted
download can't leave a truncated model that fails cryptically at transcribe time.

### Step 7 — First-run config

**Files:** `meeting_scribe/config.py`

If no config exists at either candidate location, copy
`RESOURCE_ROOT/config.example.json` to `~/.config/meeting-scribe/config.json` on
first daemon start. The app must be usable with zero config for
record → transcribe; LLM/Notion/Google features stay opt-in and must degrade
gracefully when unconfigured.

### Step 8 — DMG packaging

**Files:** `scripts/package_dmg.sh` (new)

Build the app, stage it with an `/Applications` symlink, and produce
`dist/MeetingScribe-<version>.dmg` via `hdiutil create -srcfolder`. Read the
version from `Info.plist` (`CFBundleShortVersionString`, currently `0.2.0`).
Sign the `.app` before imaging.

### Step 9 — Documentation

**Files:** `README.md`, `docs/INSTALL.md` (new)

`docs/INSTALL.md` for end users:

1. Download and open the DMG, drag to Applications.
2. **Right-click → Open** on first launch, then confirm — required because the
   app is self-signed. Include the `xattr -dr com.apple.quarantine
   /Applications/MeetingScribe.app` alternative.
3. Grant **Screen Recording** when prompted, then **relaunch** — macOS only
   applies the grant to processes started afterwards.
4. Grant **Microphone** (prompts with an Allow button; no relaunch needed).
5. Install Homebrew deps via the setup window.
6. Download models.

Add a troubleshooting table: "app moved to Trash / damaged" → quarantine;
"recording fails, screen prompt keeps reappearing" → app was rebuilt without the
stable signing identity; "ffmpeg log has only a banner" → missing
`NSMicrophoneUsageDescription`.

Update `README.md` to distinguish the developer setup (git + venv) from the
end-user install, and mark `docs/desktop-app-spec.md:26`'s "Personal / unsigned
local build" as superseded.

---

## 7. Verification

Typechecking and linting are not sufficient. Prove it end-to-end.

### Build

```bash
bash scripts/setup_signing.sh
bash scripts/fetch_python.sh
bash scripts/build_helper.sh
bash scripts/build_app.sh --install
bash scripts/package_dmg.sh
```

Then confirm the bundle is self-contained and correctly signed:

```bash
# No reference to the dev checkout or ~/.local
grep -rl "$(pwd)" /Applications/MeetingScribe.app || echo "OK: no repo paths"

# Signature valid including nested code
codesign --verify --deep --strict /Applications/MeetingScribe.app && echo "OK: signed"

# Stable, cert-anchored requirement — NOT cdhash
codesign -d -r- /Applications/MeetingScribe.app 2>&1 | grep designated
# expect: identifier "com.meetingscribe.app" and certificate leaf = H"..."

# Mic usage string present
/usr/libexec/PlistBuddy -c "Print :NSMicrophoneUsageDescription" \
  /Applications/MeetingScribe.app/Contents/Info.plist
```

### Clean-machine simulation

The real test. Create a **fresh macOS user account**, log in as that user, and
install only from the DMG. That account has no repo, no venv, no
`~/.local/bin/scribed`, no config, and no TCC grants — which is exactly the
state you are shipping into. Confirm:

1. Right-click → Open works past Gatekeeper.
2. The setup window correctly reports missing tools and models.
3. `brew install` from the button succeeds and Re-check turns everything green.
4. Model download completes and survives an interrupted retry.

### Recording end-to-end

With the daemon started **from the app's menu** (not a terminal — see Step 4):

```bash
PORT=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.local/state/meeting-scribe/daemon.json')))['port'])")
TOKEN=$(cat ~/.local/state/meeting-scribe/daemon.token)
curl -s -X POST "http://127.0.0.1:$PORT/v1/start" -H "Authorization: Bearer $TOKEN"
sleep 15
curl -s -X POST "http://127.0.0.1:$PORT/v1/stop" -H "Authorization: Bearer $TOKEN"
```

Expected: `HTTP 200` on start (**not** a 500), and both WAVs growing:

```bash
ls -l ~/Recordings/meetings/<id>.system.wav ~/Recordings/meetings/<id>.mic.wav
```

**Prove the audio is not silence** — this project has a history of
silent-capture regressions:

```bash
ffmpeg -i <id>.system.wav -af volumedetect -f null - 2>&1 | grep mean_volume
ffmpeg -i <id>.mic.wav    -af volumedetect -f null - 2>&1 | grep mean_volume
```

Both `mean_volume` values must be finite and well above `-91 dB` (digital
silence). A healthy reference from a real session: system `-23.4 dB`, mic
`-36.0 dB`.

Then let the job queue finish and confirm a transcript file is produced with
real text.

### Rebuild-survival test

The regression this whole effort must not undo:

```bash
bash scripts/build_app.sh --install   # rebuild after granting permissions
# Record again — must NOT re-prompt for Screen Recording.
```

If the permission prompt returns, the stable signing identity was lost. Do not
ship.

---

## 8. Open questions

Each has a recommended answer. Proceed with it; flag the choice in the PR.

1. **Should the daemon keep running when the app quits?**
   *Recommended: no* — terminate it on app exit, mainly so a stale daemon can't
   outlive an upgrade.
   **Verify before relying on the TCC argument.** It is tempting to say a daemon
   outliving its parent "loses the responsibility chain," but responsibility is
   assigned at exec time and is not obviously re-evaluated when the parent
   exits — today's `nohup … &` spawn deliberately detaches and appears to work.
   Test it (quit the app mid-recording, then record again) before writing any
   code that depends on either answer.
2. **Where should recordings live for a bundled user?**
   *Recommended: keep `~/Recordings/meetings`,* configurable via
   `config.json`. Changing the default would strand existing users' data.
3. **What if the user has an Intel Mac?**
   *Recommended: detect `arch` at launch and show a clear "Apple Silicon
   required" message* rather than crashing. Universal builds are a non-goal.
4. **Should `bin/syscap` stay in the repo?**
   *Recommended: yes,* gitignored as today, so the dev workflow is unchanged.
   The bundle gets its own copy at build time.
