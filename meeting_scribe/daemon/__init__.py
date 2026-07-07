"""scribed — the meeting-scribe local daemon.

Owns the recording session, the processing job queue, and the meetings index.
The SwiftUI app and the `scribe` CLI are both thin HTTP clients of this daemon
(127.0.0.1 only, bearer-token auth). See docs/desktop-app-spec.md.
"""

DAEMON_VERSION = "0.2.0"
