"""Offline control-flow check for the lab deployment script template.

The real lab script is only a delivery artifact, so this module builds an
EXECUTABLE PROBE with the same control flow and runs it as a real subprocess. The
probe replaces only the leaf commands (network check, Letta, proxy, watchdog,
RUN, audit) with tiny stand-ins that record what happened, so the ordering the
material promises is really exercised:

  * the network check runs FIRST and a failure stops before the proxy or the RUN;
  * the proxy is started and waited for readiness BEFORE the RUN;
  * cleanup TERMs the proxy and WAITS for it, so its journal is sealed, and only
    THEN runs the audit;
  * cleanup also stops Letta, on success AND on a signal;
  * every exit code is preserved in its own file.

No experimental service, no network and no model is started: the stand-ins are
local processes that sleep, write a marker and exit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results/ae-cloud-re-multiturn-original-r3"

#: The control flow the deployment material promises, as a runnable probe. The
#: stand-in binaries are created next to it by `_write_probe`.
PROBE = r'''#!/usr/bin/env bash
set -uo pipefail
umask 077
ops="$1"; mode="${2:-ok}"
mkdir -p "$ops"
log="$ops/order.log"
say() { printf '%s\n' "$1" >> "$log"; }
bin="$ops/bin"
mkdir -p "$bin"

cleanup() {
  if [ -n "${proxy_pid:-}" ]; then
    say "cleanup:proxy-term"
    kill -TERM "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
    say "cleanup:proxy-waited"
    proxy_pid=''
  fi
  say "cleanup:letta-stop"
  "$bin/letta_local" stop --project "$ops/project" > "$ops/stop.log" 2>&1
  letta_rc=$?
  echo "$letta_rc" > "$ops/stop-exit.txt"
  # Explicit: a function returns its last command's status otherwise.
  return "$letta_rc"
}
on_exit() { rc=$?; trap - EXIT INT TERM HUP; set +e; cleanup; exit "$rc"; }
trap on_exit EXIT INT TERM HUP

say "network-check"
"$bin/network" "$ops" || { say "network-failed"; exit 3; }
say "letta-start"
"$bin/letta_local" start --project "$ops/project" \
  --bounded-nltk-startup --multicall-manifest "$ops/manifest.json" \
  --multicall-support "$ops/project" > "$ops/letta-start.log" 2>&1 || exit 4
say "proxy-plan"
"$bin/proxy" --config "$ops/transport.json" > "$ops/proxy-plan.log" 2>&1
say "proxy-serve"
"$bin/proxy" --serve --journal "$ops/run.private.jsonl" --listen-port 8000 \
  > "$ops/proxy-console.log" 2>&1 &
proxy_pid=$!
say "proxy-ready-wait"
"$bin/ready" 8000 || { say "proxy-not-ready"; exit 5; }
say "run"
"$bin/watchdog" --max-seconds 28800 -- "$bin/leo" run > "$ops/runner.log" 2>&1
runner_rc=$?
echo "$runner_rc" > "$ops/runner-exit.txt"
if [ "$mode" = "signal" ]; then
  say "signal-raised"
  kill -TERM $$
fi
if [ "$mode" = "runfail" ]; then exit 7; fi

cleanup; cleanup_rc=$?
trap - EXIT INT TERM HUP
echo "$cleanup_rc" > "$ops/cleanup-exit.txt"
say "audit"
"$bin/audit" --run-dir "$ops/run" --proxy-journal "$ops/run.private.jsonl" \
  --output "$ops/input-audit.json" > "$ops/input-audit-stdout.log" 2>&1
audit_rc=$?
echo "$audit_rc" > "$ops/audit-exit.txt"
exit "$(( runner_rc || audit_rc || cleanup_rc ))"
'''


def _write_probe(tmp: Path) -> tuple[Path, Path]:
    """Create the probe script and its leaf stand-ins; return (script, ops dir)."""
    ops = Path(tmp) / "ops"
    bin_dir = ops / "bin"
    bin_dir.mkdir(parents=True)
    (ops / "manifest.json").write_text('{"schema": "probe"}', encoding="utf-8")
    (ops / "transport.json").write_text('{"profile": "probe"}', encoding="utf-8")
    (ops / "project").mkdir()

    def write(name, body):
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
        path.chmod(0o755)

    write("network", '''ops="$1"
if [ -f "$ops/network-fail" ]; then
  printf '%s\\n' '{"passed": false}' > "$ops/network-before-run.json"
  exit 1
fi
printf '%s\\n' '{"passed": true, "http_status": "401"}' > "$ops/network-before-run.json"
''')
    write("letta_local", '''printf '%s\\n' "$*" >> "$(dirname "$0")/../letta-invocations.log"
echo '{"started": true}'
''')
    write("proxy", '''if printf '%s' "$*" | grep -q -- "--serve"; then
  printf '%s\\n' "$$" > "$(dirname "$0")/../proxy-started.pid"
  printf '%s\\n' ready > "$(dirname "$0")/../proxy-ready"
  # Sleep in short slices so a TERM is honoured promptly; `wait` in cleanup
  # then returns quickly.
  for _ in $(seq 1 600); do sleep 0.1; done
else
  echo '{"plan": true}'
fi
''')
    write("ready", '''# Poll for the proxy's own readiness marker; portable POSIX sh.
ready_file="$(dirname "$0")/../proxy-ready"
for _ in $(seq 1 100); do
  [ -f "$ready_file" ] && exit 0
  sleep 0.05
done
exit 1
''')
    write("watchdog", '''# The real watchdog takes options then `--` then the command.
while [ $# -gt 0 ]; do
  case "$1" in
    --) shift; break;;
    --events|--sidecar|--max-seconds|--grace-seconds) shift 2;;
    *) shift;;
  esac
done
"$@"
''')
    write("leo", '''echo '{"status": "RE_MULTITURN_COMPLETED_AUDIT_PENDING"}'
exit 0
''')
    write("audit", '''out=""
while [ $# -gt 0 ]; do case "$1" in --output) out="$2"; shift 2;; *) shift;; esac; done
printf '%s\\n' '{"status": "VALID"}' > "$out"
exit 0
''')
    script = Path(tmp) / "run-on-lab-probe.sh"
    script.write_text(PROBE, encoding="utf-8")
    script.chmod(0o755)
    return script, ops


def _order(ops: Path) -> list[str]:
    path = ops / "order.log"
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _run(script: Path, ops: Path, mode="ok", *, send_signal=False):
    process = subprocess.Popen(["bash", str(script), str(ops), mode],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, start_new_session=True)
    if send_signal:
        # Wait until the RUN step has started, then signal the process group the
        # way an operator's Ctrl-C / TERM would.
        for _ in range(200):
            if (ops / "order.log").is_file() and "run" in _order(ops):
                break
            time.sleep(0.05)
        os.killpg(process.pid, signal.SIGTERM)
    out, _ = process.communicate(timeout=60)
    return process.returncode, out


class DeploymentControlFlowTests(unittest.TestCase):
    """The deployment material's control flow, exercised as a real subprocess."""

    def test_the_happy_path_orders_the_steps_and_seals_the_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, ops = _write_probe(Path(tmp))
            code, out = _run(script, ops)
            self.assertEqual(code, 0, out)
            order = _order(ops)
            self.assertEqual(order[0], "network-check")
            self.assertLess(order.index("proxy-ready-wait"), order.index("run"),
                            "the RUN must start only after the proxy is listening")
            self.assertLess(order.index("run"), order.index("audit"),
                            "the audit must run after the RUN")
            # The journal is sealed before the audit: the proxy was TERMed, waited
            # for, and only then was the audit run.
            self.assertLess(order.index("cleanup:proxy-term"), order.index("audit"))
            self.assertLess(order.index("cleanup:proxy-waited"), order.index("audit"))
            self.assertIn("cleanup:letta-stop", order)
            self.assertTrue((ops / "input-audit.json").is_file())
            for name in ("runner-exit.txt", "cleanup-exit.txt", "audit-exit.txt",
                         "stop-exit.txt"):
                self.assertTrue((ops / name).is_file(), name)
            self.assertEqual((ops / "runner-exit.txt").read_text().strip(), "0")
            self.assertEqual((ops / "audit-exit.txt").read_text().strip(), "0")

    def test_a_network_failure_stops_before_the_proxy_and_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, ops = _write_probe(Path(tmp))
            (ops / "network-fail").write_text("", encoding="utf-8")
            code, out = _run(script, ops)
            self.assertEqual(code, 3, out)
            order = _order(ops)
            self.assertIn("network-failed", order)
            self.assertNotIn("proxy-serve", order)
            self.assertNotIn("run", order)
            self.assertFalse((ops / "run.private.jsonl").exists())
            # Cleanup still ran, so nothing is left behind.
            self.assertIn("cleanup:letta-stop", order)

    def test_a_signal_also_cleans_up_proxy_and_letta(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, ops = _write_probe(Path(tmp))
            code, out = _run(script, ops, send_signal=True)
            self.assertNotEqual(code, 0, out)
            order = _order(ops)
            self.assertIn("cleanup:proxy-term", order)
            self.assertIn("cleanup:proxy-waited", order)
            self.assertIn("cleanup:letta-stop", order)
            self.assertNotIn("audit", order,
                             "a signalled run must not pretend to have been audited")
            self.assertTrue((ops / "stop-exit.txt").is_file())

    def test_the_material_uses_the_real_launcher_and_no_missing_script(self):
        """Every command and flag the material names must really exist."""
        text = (RESULTS / "LAB-COMMANDS.md").read_text(encoding="utf-8")
        # The phantom script of the r2 material must not appear in any runnable
        # command block; the correction table may still name it as the defect.
        runnable = "\n".join(block for block in text.split("```bash")[1:])
        self.assertNotIn("ae_network_precheck.py", runnable)
        self.assertIn("curl", runnable)
        # The real launcher path and its required flag combination.
        self.assertIn("scripts/deployment/letta_local.py start", text)
        self.assertIn("--bounded-nltk-startup", text)
        self.assertIn("--multicall-manifest", text)
        self.assertIn("--multicall-support", text)
        self.assertIn("--multicall-profile ae-multicall-receive-compat-1", text)
        # The real runtime CLI accepts exactly this combination.
        help_text = subprocess.run(
            [sys.executable, str(ROOT / "scripts/deployment/letta_local.py"), "--help"],
            capture_output=True, text=True).stdout
        for flag in ("--bounded-nltk-startup", "--multicall-manifest",
                     "--multicall-support", "--multicall-profile", "--explicit-llm-config"):
            self.assertIn(flag, help_text, flag)
        # No credential or key material is embedded in the material.
        self.assertNotIn("siliconflow.key\"", text.split("--key-file")[0][-200:]
                         if "--key-file" in text else text)
        # The cleanup order the material promises is the one its cleanup block
        # documents: proxy TERM, proxy wait, Letta stop - and the audit comes
        # after the cleanup block, not before it.
        cleanup_start = text.index("cleanup() {")
        cleanup_end = text.index("on_exit() {")
        block = text[cleanup_start:cleanup_end]
        self.assertLess(block.index('kill -TERM "$proxy_pid"'),
                        block.index('wait "$proxy_pid"'))
        self.assertLess(block.index('wait "$proxy_pid"'),
                        block.index("letta_local.py stop"))
        self.assertLess(cleanup_end,
                        text.index("scripts/ae_01_cloud_re_multiturn_input_audit.py"))
