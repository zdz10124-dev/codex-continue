CodexContinue
=============

Purpose
-------
Automatically resumes a user-visible root Codex session after a 5-hour usage-limit interruption.
It is independent of the Codex Desktop UI and does not modify Codex config.toml or its existing notify hook.

How it works
------------
1. Read-only polling of ~/.codex/thread_history_1.sqlite.
2. Detect only the latest turn whose structured error is codexErrorInfo=usageLimitExceeded.
3. Ignore subagent threads and archived threads.
4. While a recovery is pending, poll account/rateLimits/read every 60 seconds so an early quota reset is detected quickly.
5. Before the failure's scheduled reset time, require 5 minutes of continuous ready state with an unchanged resetAt value before trusting an early reset.
6. If quota becomes unavailable again or resetAt changes during that 5-minute window, restart confirmation from zero.
7. Keep the reset time embedded in the actual usageLimitExceeded error as the authoritative fallback; transient API snapshots cannot overwrite it.
8. Cancel the plan if a newer turn appears or if the user has queued input.
9. Resume through the existing Codex Desktop app-server: codex queue --thread <thread-id> --message <continuation-prompt>
10. Run only one automatic resumed root thread at a time.

Safety
------
- Does not replace the existing Codex notify hook.
- Does not simulate mouse/keyboard UI actions.
- Does not retry while rateLimits reports 100% used.
- A new user turn makes the old pending recovery stale and cancels it.
- Existing queued user input prevents automatic injection.
- Subagents are never independently revived.

Files
-----
logs/watchdog.log     Main watchdog log
state.json       Pending/handled recovery state
logs/runs/*.jsonl  Output of each automatic codex exec resume run

Autostart
---------
Installed for the current Windows user through:
HKCU\Software\Microsoft\Windows\CurrentVersion\Run\CodexContinue
It starts silently at login and does not depend on the Codex Desktop window being open.

Stopping
--------
Run stop.cmd to stop the background watcher.
Stopping CodexContinue does not kill a Codex task that it already launched.
Run start.cmd to start it again. Use status.cmd to inspect the process and recent log.


