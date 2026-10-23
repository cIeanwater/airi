"""Check QQ startup, credential boundaries, and voice launcher delegation without QQ accounts."""

import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("local_companion_launcher", ROOT / "scripts/start-local-companion.py")
LAUNCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCHER)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qq launcher ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "qq-startup.json"
        self.qq_path = self.root / "qq.json"
        executable = self.root / "runtime with spaces.exe"
        executable.write_bytes(b"")
        self.qq = {"enabled": True, "apiUrl": "http://127.0.0.1:5140/satori/v1", "token": "synthetic-test-secret", "platform": "onebot", "selfId": "123"}
        self.startup = {"enabled": True, "qqConfig": "qq.json", "waitSeconds": 1,
                        "napcat": {"command": [str(executable), "123"], "cwd": str(self.root)},
                        "koishi": {"command": [str(executable), "--koishi"], "cwd": str(self.root)}}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.startup), encoding="utf-8")
        self.qq_path.write_text(json.dumps(self.qq), encoding="utf-8")

    def test_unconfigured_or_disabled_startup_does_not_require_apps(self):
        self.assertIsNone(LAUNCHER.load_configuration(self.root / "missing.json"))
        self.path.write_text('{"enabled": false}', encoding="utf-8")
        self.assertIsNone(LAUNCHER.load_configuration(self.path))

    def test_relative_profile_and_paths_with_spaces(self):
        configuration = LAUNCHER.load_configuration(self.path)
        self.assertEqual(configuration["commands"]["napcat"]["cwd"], self.root)
        self.assertEqual(configuration["napcat_port"], 3001)
        self.assertEqual(configuration["satori_port"], 5140)

    def test_disabled_airi_connection_does_not_start_services(self):
        self.qq["enabled"] = False
        self.save()
        self.assertIsNone(LAUNCHER.load_configuration(self.path))

    def test_remote_or_credential_bearing_endpoints_are_rejected(self):
        for address in ("http://example.com/satori", "https://127.0.0.1/satori", "http://user:password@127.0.0.1/satori", "http://127.0.0.1/satori?token=secret"):
            with self.subTest(address=address):
                self.qq["apiUrl"] = address
                self.save()
                with self.assertRaises(RuntimeError) as result:
                    LAUNCHER.load_configuration(self.path)
                self.assertNotIn("synthetic-test-secret", str(result.exception))
                self.assertNotIn(address, str(result.exception))

    def test_invalid_commands_and_budget_fail_before_launch(self):
        for key, value in (("waitSeconds", 0), ("waitSeconds", True), ("napcat", {"command": "cmd /c anything", "cwd": str(self.root)})):
            with self.subTest(key=key, value=value):
                original = self.startup[key]
                self.startup[key] = value
                self.save()
                with self.assertRaises(RuntimeError):
                    LAUNCHER.load_configuration(self.path)
                self.startup[key] = original

    def test_koishi_process_match_includes_the_instance_cli(self):
        service = {"command": [str(self.root / "koishi.exe"), str(self.root / "instance-a/bin.js"), "start"]}
        other = {"ExecutablePath": service["command"][0], "CommandLine": str(self.root / "instance-b/bin.js")}
        self.assertFalse(LAUNCHER.process_matches(other, service))
        current = {**other, "CommandLine": subprocess.list2cmdline(service["command"])}
        self.assertTrue(LAUNCHER.process_matches(current, service))

    @unittest.skipUnless(os.name == "nt", "The prepared launcher supports Windows.")
    def test_process_inventory_timeout_has_no_command_or_credentials_in_error(self):
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("private-command", 15)):
            with self.assertRaises(RuntimeError) as result:
                LAUNCHER.running_processes()
        self.assertNotIn("private-command", str(result.exception))

    @unittest.skipUnless(os.name == "nt", "The prepared launcher supports Windows.")
    def test_check_starts_nothing_and_does_not_probe_login(self):
        with patch.object(sys, "argv", ["launcher", "--qq-only", "--check", "--qq-config", str(self.path)]), \
                patch.object(LAUNCHER, "probe_login") as probe, patch.object(subprocess, "Popen") as spawn, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(LAUNCHER.main(), 0)
        probe.assert_not_called()
        spawn.assert_not_called()
        self.assertIn('"online_checked": false', output.getvalue())

    @unittest.skipUnless(os.name == "nt", "The prepared launcher supports Windows.")
    def test_qq_failure_still_delegates_voice_arguments_and_exit_code(self):
        self.path.write_text('{"enabled": true}', encoding="utf-8")
        arguments = ["launcher", "--qq-config", str(self.path), "--context-size", "8192"]
        with patch.object(sys, "argv", arguments), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 7)) as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(LAUNCHER.main(), 7)
        self.assertEqual(run.call_args.args[0][-2:], ["--context-size", "8192"])
        self.assertIn("AIRI will still start", output.getvalue())
        self.assertNotIn(self.qq["token"], output.getvalue())

    @unittest.skipUnless(os.name == "nt", "The prepared launcher supports Windows.")
    def test_invalid_qq_preflight_returns_failure_even_when_voice_paths_pass(self):
        self.path.write_text('{"enabled": true}', encoding="utf-8")
        with patch.object(sys, "argv", ["launcher", "--check", "--qq-config", str(self.path)]), \
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), redirect_stdout(io.StringIO()):
            self.assertEqual(LAUNCHER.main(), 1)

    @unittest.skipUnless(os.name == "nt", "The prepared launcher supports Windows.")
    def test_online_connection_does_not_spawn_or_inspect_processes(self):
        configuration = LAUNCHER.load_configuration(self.path)
        with patch.object(LAUNCHER, "probe_login", return_value="online"), patch.object(subprocess, "Popen") as spawn, patch.object(LAUNCHER, "running_processes") as inventory:
            self.assertEqual(LAUNCHER.start_qq(configuration, self.root)["status"], "already_ready")
        spawn.assert_not_called()
        inventory.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "The prepared launcher supports Windows.")
    def test_running_apps_are_reused_while_waiting_for_login(self):
        configuration = LAUNCHER.load_configuration(self.path)
        processes = [{"ExecutablePath": service["command"][0]} for service in configuration["commands"].values()]
        with patch.object(LAUNCHER, "probe_login", side_effect=["login-required", "online"]), \
                patch.object(LAUNCHER, "running_processes", return_value=processes), patch.object(LAUNCHER, "port_open", return_value=False), \
                patch.object(subprocess, "Popen") as spawn, redirect_stdout(io.StringIO()):
            self.assertTrue(LAUNCHER.start_qq(configuration, self.root)["ready"])
        spawn.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "The prepared launcher supports Windows.")
    def test_missing_apps_start_without_a_shell_and_preserve_arguments(self):
        configuration = LAUNCHER.load_configuration(self.path)
        with patch.object(LAUNCHER, "probe_login", side_effect=["service-unavailable", "online"]), \
                patch.object(LAUNCHER, "running_processes", return_value=[]), patch.object(LAUNCHER, "port_open", return_value=False), \
                patch.object(subprocess, "Popen") as spawn, redirect_stdout(io.StringIO()):
            self.assertTrue(LAUNCHER.start_qq(configuration, self.root)["ready"])
        self.assertEqual(spawn.call_count, 2)
        self.assertEqual(spawn.call_args_list[0].args[0], configuration["commands"]["napcat"]["command"])
        for call in spawn.call_args_list:
            self.assertIs(call.kwargs["shell"], False)
            self.assertEqual(call.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    @unittest.skipUnless(os.name == "nt", "The prepared launcher supports Windows.")
    def test_login_timeout_does_not_claim_success_or_terminate_apps(self):
        configuration = LAUNCHER.load_configuration(self.path)
        with patch.object(LAUNCHER, "probe_login", return_value="login-required"), \
                patch.object(LAUNCHER, "running_processes", return_value=[]), patch.object(LAUNCHER, "port_open", return_value=True), \
                patch.object(LAUNCHER.time, "monotonic", side_effect=[0, 2]), patch.object(subprocess, "Popen") as spawn, redirect_stdout(io.StringIO()):
            result = LAUNCHER.start_qq(configuration, self.root)
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "login-required")
        spawn.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "The prepared launcher supports Windows.")
    def test_real_child_startup_and_repeated_launch_use_one_service_pair(self):
        ports = []
        for _index in range(2):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                ports.append(probe.getsockname()[1])
        napcat = self.root / "controlled napcat.py"
        napcat.write_text("import socket, sys\ns=socket.socket()\ns.bind(('127.0.0.1',int(sys.argv[1])))\ns.listen()\nwhile True:\n c,_=s.accept()\n c.close()\n", encoding="utf-8")
        koishi = self.root / "controlled koishi.py"
        koishi.write_text("from http.server import BaseHTTPRequestHandler,HTTPServer\nimport sys\nclass H(BaseHTTPRequestHandler):\n def do_POST(self):\n  self.send_response(200)\n  self.end_headers()\n  self.wfile.write(b'{\"status\":1,\"platform\":\"onebot\",\"user\":{\"id\":\"123\"}}')\n def log_message(self,*args): pass\nHTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()\n", encoding="utf-8")
        # Run the actual interpreter. The Windows venv redirector creates an extra process that outlives terminate().
        interpreter = str(Path(sys._base_executable).resolve())
        self.startup["napcat"] = {"command": [interpreter, str(napcat), str(ports[0])], "cwd": str(self.root), "port": ports[0]}
        self.startup["koishi"] = {"command": [interpreter, str(koishi), str(ports[1])], "cwd": str(self.root)}
        self.startup["waitSeconds"] = 10
        self.qq["apiUrl"] = f"http://127.0.0.1:{ports[1]}/satori/v1"
        self.save()
        configuration = LAUNCHER.load_configuration(self.path)
        children = []
        original_spawn = subprocess.Popen

        def spawn(*args, **kwargs):
            child = original_spawn(*args, **kwargs)
            children.append(child)
            return child

        # Only process inventory is isolated. Both child processes and the HTTP login probe are real.
        try:
            with patch.object(LAUNCHER, "running_processes", return_value=[]), patch.object(subprocess, "Popen", side_effect=spawn), redirect_stdout(io.StringIO()):
                self.assertTrue(LAUNCHER.start_qq(configuration, self.root)["ready"])
                self.assertEqual(LAUNCHER.start_qq(configuration, self.root)["status"], "already_ready")
            self.assertEqual(len(children), 2)
            self.assertTrue(all(child.poll() is None for child in children))
        finally:
            for child in children:
                child.terminate()
                child.wait(timeout=5)


class SatoriProbeTests(unittest.TestCase):
    def setUp(self):
        self.status = 200
        self.body = {"status": 1, "platform": "onebot", "user": {"id": "123"}}
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append((self.path, self.headers.get("Authorization"), self.headers.get("Satori-Platform")))
                self.send_response(owner.status)
                if owner.status == 302:
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/redirect-target")
                self.end_headers()
                self.wfile.write(json.dumps(owner.body).encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.qq = {"apiUrl": f"http://127.0.0.1:{self.server.server_port}/satori/v1", "token": "test-token-only", "platform": "onebot", "selfId": "123"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_authenticated_probe_checks_account_and_online_state(self):
        self.assertEqual(LAUNCHER.probe_login(self.qq), "online")
        self.assertEqual(self.requests, [("/satori/v1/login.get", "Bearer test-token-only", "onebot")])
        self.body["status"] = 0
        self.assertEqual(LAUNCHER.probe_login(self.qq), "login-required")
        self.body["user"]["id"] = "other"
        self.assertEqual(LAUNCHER.probe_login(self.qq), "account-mismatch")

    def test_rejected_token_is_reported_without_response_body(self):
        self.status = 403
        self.body = {"private": "never-print-this"}
        self.assertEqual(LAUNCHER.probe_login(self.qq), "authentication-failed")

    def test_redirect_does_not_forward_credentials(self):
        self.status = 302
        self.assertEqual(LAUNCHER.probe_login(self.qq), "api-unavailable")
        self.assertEqual(len(self.requests), 1)

    def test_system_proxy_does_not_receive_loopback_credentials(self):
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1", "no_proxy": "", "NO_PROXY": ""}):
            self.assertEqual(LAUNCHER.probe_login(self.qq), "online")


if __name__ == "__main__":
    unittest.main()
