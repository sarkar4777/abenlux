# Background agent + toast verification (Linux, Docker)

Reproduce the README's claim that the background agent and native desktop toasts work on Linux. The
container ships a real notification stack (a `dunst` daemon, `notify-send`, D-Bus, a virtual display),
so the toast is delivered exactly as it would be on a developer's Linux desktop.

```bash
docker build -t abenlux-linux examples/agent-verification
docker run --rm -v "$PWD:/repo:ro" abenlux-linux bash /repo/examples/agent-verification/verify_agent.sh
```

Expected output:

```
== background agent: install (systemd --user unit) ==
installed systemd --user unit at /root/.config/systemd/user/com.abenlux.agent.service and started it
  OK unit ExecStart
  OK unit [Install] WantedBy
  OK unit EnvironmentFile
== background agent: it actually runs ==
  OK capture agent serving on :8077
== native toast: notify-send -> dunst over D-Bus ==
  OK toast received by the daemon (displayed=1)
  daemon got -> Abenlux :: cache-inefficiency on ACME-90, recoverable by enabling prompt caching
ALL GOOD
```

What this exercises:

- **`abenlux agent install`** writes a correct **systemd `--user`** unit (a launchd LaunchAgent on
  macOS, a Startup-folder launcher on Windows — those run on their own OS).
- **`abenlux agent run`** loads the snapshotted config and actually starts the capture agent.
- A nudge fires a real **`notify-send`** that a notification daemon (**dunst**) receives and displays —
  the same D-Bus path that pops a toast on a Linux desktop. The agent runs as a **user** unit precisely
  so this session-bound notification path is available.

The macOS and Windows agents were verified on their own platforms (the Windows Startup launcher was
confirmed to start the agent at login on a real Windows host). This harness covers the Linux path.
