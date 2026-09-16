"""Offline deployment fixtures; no NLTK network, services, signals or inference."""
from copy import deepcopy
import io
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from types import FunctionType, ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.request

from scripts.deployment import letta_bootstrap as boot
from scripts.deployment import letta_local as local


class NLTKStartupTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.urlopen = Mock(return_value=object())
        self.patcher = patch.object(urllib.request, "urlopen", self.urlopen)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.downloader = ModuleType("nltk.downloader")
        self.downloader.urlopen = self.urlopen
        update_index = FunctionType((lambda: None).__code__, {"urlopen": self.urlopen})
        self.downloader.Downloader = SimpleNamespace(_update_index=update_index)
        self.nltk = ModuleType("nltk")
        self.nltk.__path__ = []
        self.nltk.__version__ = "3.9.1"
        self.nltk.downloader = self.downloader
        self.nltk.tokenize = SimpleNamespace(PunktTokenizer=Mock(return_value=object()))

    def emit(self, event, **fields):
        self.events.append({"event": event, **fields})

    def install(self, original):
        self.nltk.download = original
        return boot.install_startup_bound(self.nltk, self.downloader, self.emit)

    def test_success_and_false_are_preserved_and_restore_immediately(self):
        for expected in (True, False, None):
            with self.subTest(returned=expected):
                self.events.clear()
                def original(*args, **kwargs):
                    self.assertIs(urllib.request.urlopen, self.urlopen)
                    self.downloader.urlopen("https://offline.fixture/index.xml")
                    return expected
                prior_default = socket.getdefaulttimeout()
                restore = self.install(original)
                self.assertIs(self.downloader.urlopen, self.urlopen)  # Not bounded until this call.
                self.assertIs(self.nltk.download("punkt_tab", quiet=True), expected)
                self.urlopen.assert_called_with("https://offline.fixture/index.xml", timeout=10.0)
                self.assertIs(self.downloader.urlopen, self.urlopen)
                self.assertIs(self.nltk.download, original)
                self.assertIs(urllib.request.urlopen, self.urlopen)
                self.assertEqual(socket.getdefaulttimeout(), prior_default)
                self.assertEqual([e for e in self.events if e["event"] == "download_returned"][0]["returned"], expected)
                restore()  # Idempotent cleanup.

    def test_timeout_and_other_exceptions_restore_and_propagate(self):
        for error in (TimeoutError("fixture timeout"), RuntimeError("fixture error"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.events.clear()
                original = Mock(side_effect=error)
                self.install(original)
                with self.assertRaises(type(error)):
                    self.nltk.download("punkt_tab", quiet=True)
                self.assertIs(self.downloader.urlopen, self.urlopen)
                self.assertIs(self.nltk.download, original)
                self.assertFalse(any(e["event"] == "download_returned" for e in self.events))
                self.assertEqual(self.events[-2]["error_type"], type(error).__name__)
                self.assertEqual(self.events[-1]["event"], "download_hooks_restored")

    def test_bounds_positional_timeout_and_preserves_shorter_timeout(self):
        def original(*args, **kwargs):
            self.downloader.urlopen("https://offline.fixture/a", None, 60, context="fixture-context")
            self.downloader.urlopen("https://offline.fixture/b", timeout=2)
            return False
        self.install(original)
        self.nltk.download("punkt_tab", quiet=True)
        self.assertEqual(self.urlopen.call_args_list[0].args,
                         ("https://offline.fixture/a", None, 10.0))
        self.assertEqual(self.urlopen.call_args_list[0].kwargs, {"context": "fixture-context"})
        self.assertEqual(self.urlopen.call_args_list[1].kwargs, {"timeout": 2.0})

    def test_missing_data_never_installs_hooks_or_calls_download(self):
        original = Mock()
        self.nltk.download = original
        self.nltk.tokenize.PunktTokenizer.side_effect = LookupError("fixture missing table")
        with self.assertRaises(LookupError):
            self.install(original)
        self.nltk.tokenize.PunktTokenizer.assert_called_once_with("english")
        original.assert_not_called()
        self.assertIs(self.nltk.download, original)
        self.assertIs(self.downloader.urlopen, self.urlopen)

    def test_version_or_binding_drift_rejected_before_install(self):
        original = Mock()
        self.nltk.__version__ = "3.9.2"
        with self.assertRaises(RuntimeError):
            self.install(original)
        self.nltk.__version__ = "3.9.1"
        self.downloader.urlopen = Mock()
        with self.assertRaises(RuntimeError):
            self.install(original)
        self.nltk.tokenize.PunktTokenizer.assert_not_called()
        original.assert_not_called()

    def test_unexpected_download_is_not_sent_and_restores(self):
        original = Mock()
        self.install(original)
        with self.assertRaises(RuntimeError):
            self.nltk.download("other-data", quiet=True)
        original.assert_not_called()
        self.assertIs(self.nltk.download, original)
        self.assertIs(self.downloader.urlopen, self.urlopen)

    def test_executes_original_cli_with_original_argv_and_restores(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cli = root / ".venv-letta/bin/letta"
            cli.parent.mkdir(parents=True)
            cli.write_text("# fixture entrypoint only\n", encoding="utf-8")
            argv = [str(cli), "server", "--host", "127.0.0.1", "--port", "8283"]
            before_argv, before_path = list(sys.argv), list(sys.path)
            original = Mock(return_value=False)
            self.nltk.download = original
            def entrypoint(path, run_name):
                self.assertEqual(path, str(cli))
                self.assertEqual(run_name, "__main__")
                self.assertEqual(sys.argv, argv)
                self.assertEqual(sys.path[0], str(cli.parent))
                self.assertFalse(self.nltk.download("punkt_tab", quiet=True))
                self.assertIs(self.downloader.urlopen, self.urlopen)
                raise SystemExit(7)  # A real CLI exit must not be swallowed.
            with patch.object(boot, "__file__", str(root / "scripts/deployment/letta_bootstrap.py")), \
                 patch.object(sys, "prefix", str(cli.parent.parent)), \
                 patch.dict(sys.modules, {"nltk": self.nltk, "nltk.downloader": self.downloader}), \
                 patch.object(boot.runpy, "run_path", side_effect=entrypoint) as run, \
                 patch.object(boot, "report"):
                with self.assertRaises(SystemExit) as caught:
                    boot.main(argv)
                self.assertEqual(caught.exception.code, 7)
                run.assert_called_once()
            self.assertEqual(sys.argv, before_argv)
            self.assertEqual(sys.path, before_path)
            self.assertIs(self.nltk.download, original)
            self.assertIs(self.downloader.urlopen, self.urlopen)


class LettaLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.deploy = local.Deployment(self.project)
        self.cli = self.deploy.venv / "bin/letta"
        self.cli.parent.mkdir(parents=True)
        self.interpreter = self.cli.parent / "python3"
        self.cli.write_text("#!" + str(self.interpreter) + "\n# fixture\n")
        (self.cli.parent / "python").write_text("# fixture interpreter, never executed\n")
        self.bootstrap = self.project / "scripts/deployment/letta_bootstrap.py"
        self.bootstrap.parent.mkdir(parents=True)
        self.bootstrap.write_text("# fixture source identity\n")
        self.cli_argv = [str(self.cli), "server", "--host", "127.0.0.1", "--port", "8283"]
        self.wrapper_argv = [str(self.cli.parent / "python"), "-u", str(self.bootstrap)] + self.cli_argv
        self.state = {"pid": 12345, "executable": str(self.cli), "port": 8283}

    def matches(self, argv, state=None):
        original = Path.read_bytes
        def read(path):
            if str(path) == "/proc/12345/cmdline":
                return b"\0".join(x.encode() for x in argv) + b"\0"
            return original(path)
        with patch.object(Path, "read_bytes", read):
            return self.deploy.process_matches(state or self.state)

    def test_old_direct_record_matches_exact_cli_with_its_shebang(self):
        self.assertTrue(self.matches([str(self.interpreter)] + self.cli_argv))
        self.assertTrue(self.matches(self.cli_argv))
        self.assertFalse(self.matches([]))
        for argv in (["/usr/bin/python3"] + self.cli_argv,
                     [str(self.interpreter)] + self.cli_argv + ["--extra"],
                     [str(self.interpreter)] + self.cli_argv[:-1] + ["9999"]):
            with self.subTest(argv=argv), self.assertRaises(RuntimeError):
                self.matches(argv)

    def test_wrapper_record_only_matches_exact_wrapper_command(self):
        state = dict(self.state, launch_mode="bounded_nltk_startup",
                     launch_argv=self.wrapper_argv, letta_argv=self.cli_argv)
        self.assertTrue(self.matches(self.wrapper_argv, state))
        for argv in (self.cli_argv, self.wrapper_argv + ["--extra"],
                     ["/usr/bin/python3"] + self.wrapper_argv[1:]):
            with self.subTest(argv=argv), self.assertRaises(RuntimeError):
                self.matches(argv, state)
        corrupted = deepcopy(state)
        corrupted["launch_argv"][2] = "/tmp/unrelated.py"
        with self.assertRaises(RuntimeError):
            self.matches(self.wrapper_argv, corrupted)
        with self.assertRaises(RuntimeError):
            self.matches(self.wrapper_argv)  # Old direct record cannot authorize wrapper signaling.

    def test_opt_in_launch_records_wrapper_hash_and_preserves_cli(self):
        process = SimpleNamespace(pid=12345, poll=lambda: None)
        opener = SimpleNamespace(open=lambda *_a, **_k: io.StringIO('{"status":"ok","version":"0.16.8"}'))
        with patch.object(self.deploy, "source_check"), patch.object(self.deploy, "db_config", return_value={}), \
             patch.object(self.deploy, "env", return_value={}), patch.object(local, "port_free", return_value=True), \
             patch.object(local.subprocess, "Popen", return_value=process) as popen, \
             patch.object(local.urllib.request, "build_opener", return_value=opener):
            result = self.deploy.start("http://127.0.0.1:8000/v1", 8283, bounded_nltk_startup=True)
        self.assertTrue(result["started"])
        self.assertEqual(popen.call_args.args[0], self.wrapper_argv)
        state = json.loads(self.deploy.process.read_text())
        record = json.loads((self.deploy.record / "launch.json").read_text())
        self.assertEqual(state["launch_argv"], self.wrapper_argv)
        self.assertEqual(record["letta_argv"], self.cli_argv)
        self.assertEqual(record["bootstrap_sha256"], local.hashlib.sha256(self.bootstrap.read_bytes()).hexdigest())
        self.assertTrue(record["nltk_result_not_yet_observed"])
        self.assertIsNone(record["nltk_total_timeout_seconds"])

    def test_default_launch_remains_direct(self):
        process = SimpleNamespace(pid=12345, poll=lambda: None)
        opener = SimpleNamespace(open=lambda *_a, **_k: io.StringIO('{"status":"ok","version":"0.16.8"}'))
        with patch.object(self.deploy, "source_check"), patch.object(self.deploy, "db_config", return_value={}), \
             patch.object(self.deploy, "env", return_value={}), patch.object(local, "port_free", return_value=True), \
             patch.object(local.subprocess, "Popen", return_value=process) as popen, \
             patch.object(local.urllib.request, "build_opener", return_value=opener):
            self.deploy.start("http://127.0.0.1:8000/v1", 8283)
        self.assertEqual(popen.call_args.args[0], self.cli_argv)
        self.assertEqual(json.loads(self.deploy.process.read_text())["launch_mode"], "direct")

    def test_stop_signals_only_verified_wrapper_and_preserves_record(self):
        state = dict(self.state, launch_mode="bounded_nltk_startup",
                     launch_argv=self.wrapper_argv, letta_argv=self.cli_argv)
        local.write_new(self.deploy.process, state)
        original = Path.read_bytes
        commands = iter([b"\0".join(x.encode() for x in self.wrapper_argv), b""])
        def read(path):
            return next(commands) if str(path) == "/proc/12345/cmdline" else original(path)
        with patch.object(Path, "read_bytes", read), patch.object(self.deploy, "db_config", return_value={}), \
             patch.object(self.deploy, "pg", return_value=SimpleNamespace(returncode=1)), \
             patch.object(local.os, "kill") as kill:
            result = self.deploy.stop()
        kill.assert_called_once_with(12345, 15)
        self.assertTrue(result["stopped"])
        self.assertFalse(self.deploy.process.exists())
        self.assertEqual(json.loads((self.deploy.record / "stopped-letta-process.json").read_text()), state)


if __name__ == "__main__":
    unittest.main()
