#!/usr/bin/env bash
# Verify the Linux background agent and the native desktop toast end to end, inside a container.
#
#   docker build -t abenlux-linux examples/agent-verification
#   docker run --rm -v "$PWD:/repo:ro" abenlux-linux bash /repo/examples/agent-verification/verify_agent.sh
#
# Proves: (1) `abenlux agent install` writes a correct systemd --user unit, (2) `abenlux agent run`
# actually starts the capture agent, (3) a nudge fires a real notify-send that a notification daemon
# (dunst) receives and displays - the same path that pops a toast on a developer's Linux desktop.
set -u
cp -r /repo /work/abenlux && cd /work/abenlux
echo "== installing abenlux =="
pip install -e . -q 2>&1 | tail -1
export HOME=/root ABEN_NOTIFY=1 ABEN_DB=/tmp/a.db
fail=0

echo ""
echo "== background agent: install (systemd --user unit) =="
python -m abenlux.cli agent install --port 8077 | head -1
unit=/root/.config/systemd/user/com.abenlux.agent.service
grep -q "ExecStart=.*agent run --port 8077" "$unit" && echo "  OK unit ExecStart" || { echo "  FAIL unit ExecStart"; fail=1; }
grep -q "WantedBy=default.target" "$unit" && echo "  OK unit [Install] WantedBy" || { echo "  FAIL [Install]"; fail=1; }
grep -q "EnvironmentFile=-/root/.abenlux/agent.env" "$unit" && echo "  OK unit EnvironmentFile" || { echo "  FAIL EnvironmentFile"; fail=1; }

echo ""
echo "== background agent: it actually runs =="
python -m abenlux.cli agent run --port 8077 >/tmp/agent.log 2>&1 &
up=0; for i in $(seq 1 25); do curl -s http://127.0.0.1:8077/health >/dev/null 2>&1 && { up=1; break; }; sleep 1; done
[ "$up" = 1 ] && echo "  OK capture agent serving on :8077" || { echo "  FAIL agent did not start"; cat /tmp/agent.log | tail -3; fail=1; }

echo ""
echo "== native toast: notify-send -> dunst over D-Bus =="
eval "$(dbus-launch --sh-syntax)"
Xvfb :99 -screen 0 200x200x16 >/dev/null 2>&1 & export DISPLAY=:99; sleep 1
dunst >/tmp/dunst.log 2>&1 & sleep 2
python -c "from abenlux.developer.notify import notify; notify('cache-inefficiency on ACME-90, recoverable by enabling prompt caching')"
sleep 1
displayed=$(dunstctl count | awk '/displayed/ {print $NF}')
[ "${displayed:-0}" -ge 1 ] && echo "  OK toast received by the daemon (displayed=$displayed)" || { echo "  FAIL toast not received"; fail=1; }
dunstctl history 2>/dev/null | python3 -c "
import sys, json
try:
    n = json.loads(sys.stdin.read() or '{}').get('data', [[]])[0]
    print('  daemon got ->', n[0]['summary']['data'], '::', n[0]['body']['data']) if n else print('  (still in the displayed queue)')
except Exception:
    print('  (toast received; history readback skipped)')
"

echo ""
[ "$fail" = 0 ] && echo "ALL GOOD" || echo "FAILURES ABOVE"
exit "$fail"
