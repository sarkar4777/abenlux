# Live demo. Two Claude Code sessions, watched in real time.

This runs the whole thing on your own machine so you can see it work while two VS Code windows code
with Claude. One command starts the collector and dashboard plus a gateway for each of two developers,
alice and bob. You point each window at its own gateway, open the dashboard, and watch the spend, the
value, and the collaboration appear as you code. Every call still goes to Anthropic, the gateway just
sits in the middle and keeps a content-free record.

## Start it

```
python examples/live-demo/start.py
```

It prints the exact steps. The short version is below.

## Watch it work

1. Open `http://127.0.0.1:8090` in a browser and sign in with the token `boss`. That is the manager
   view. Leave it open, it refreshes on its own.

2. In VS Code window one, open a terminal and run, then start Claude.

   ```
   $env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8101"
   $env:ANTHROPIC_API_KEY  = "<your anthropic api key>"
   claude
   ```

3. In VS Code window two, do the same but point at the other gateway.

   ```
   $env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8102"
   $env:ANTHROPIC_API_KEY  = "<your anthropic api key>"
   claude
   ```

4. Code in both windows. Ask Claude to work on the same kind of thing in both, so you can watch them
   match as collaborators. Name a branch like `feature/APP-100` so the spend ties to the Acme App goal.

5. To see a developer's own private view, sign in to the same dashboard with the token `alice` or `bob`.
   To tail the raw capture in a terminal, run `Get-Content -Wait examples/live-demo/.run/gateway-alice.log`.

## Notes

- Claude Code must use an Anthropic API key here. A subscription login does not route through a custom
  base url.
- Everything stays on your machine. Only content-free records leave each gateway, and they go to your
  own collector on `127.0.0.1`, nowhere else.
- Press Ctrl C in the start window to stop everything.
