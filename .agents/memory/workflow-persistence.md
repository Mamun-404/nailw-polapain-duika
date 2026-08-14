---
name: Persistent Python workflows
description: Replit workflow behavior for long-running bot/watchdog processes
---

Long-running bot launchers must keep their supervisor loop in the foreground; backgrounding the watchdog makes the workflow appear finished and removes reliable process supervision.

**Why:** Replit tracks the workflow process itself. A launcher that forks and exits can leave an orphaned child while the workflow is no longer considered running.

**How to apply:** For console bots, run the watchdog directly from the workflow command, keep crash-restart logic inside that foreground process, and terminate child processes from a signal/exit cleanup handler.