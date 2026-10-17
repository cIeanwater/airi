"""Check Windows desktop startup without models, Electron, or a terminal PATH."""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("local_voice_launcher", ROOT / "scripts/start-local-voice.py")
LAUNCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCHER)


def isolated_environment():
    environment = dict(os.environ)
    for key in list(environment):
        if key.upper() in ("PATH", "HUMANNING_NODE", "NODE_PATH", "ELECTRON_RUN_AS_NODE"):
            del environment[key]
    environment["PATH"] = ""
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def write_file(path, content=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def real_node():
    candidate = os.environ.get("HUMANNING_TEST_NODE")
    prepared = ROOT.parent / ".tools/node-v26.7.0-win-x64/node.exe"
    node = Path(candidate) if candidate else prepared if prepared.is_file() else Path(shutil.which("node") or "")
    if not node.is_file():
        raise RuntimeError("A real Node runtime is required. Set HUMANNING_TEST_NODE to its executable.")
    return node.resolve()


@unittest.skipUnless(os.name == "nt", "The prepared launcher supports Windows runtimes.")
class LauncherRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary_root = ROOT / ".temp"
        temporary_root.mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="launcher runtime ", dir=temporary_root)
        cls.root = Path(cls.temporary.name)
        cls.tools = cls.root / "tools with spaces"
        cls.desktop = cls.root / "desktop with spaces"
        cls.node = cls.tools / "node-v26.7.0-win-x64/node.exe"
        cls.cli = cls.desktop / "node_modules/electron-vite/bin/electron-vite.js"
        cls.environment = isolated_environment()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.source_node = real_node()
        cls.node.parent.mkdir(parents=True)
        try:
            os.link(cls.source_node, cls.node)
        except OSError:
            shutil.copy2(cls.source_node, cls.node)
        write_file(cls.cli, """const { spawnSync } = require('node:child_process')
const child = spawnSync('node', ['-p', 'process.execPath'], { encoding: 'utf8' })
if (child.error || child.status !== 0) {
  console.error(String(child.error || child.stderr))
  process.exit(1)
}
process.stdout.write(JSON.stringify({
  executable: process.execPath,
  childExecutable: child.stdout.trim(),
  arguments: process.argv.slice(2),
  profile: process.env.APP_USER_DATA_PATH,
  localMode: process.env.VITE_AIRI_LOCAL_MODE,
  devtools: process.env.AIRI_VUE_DEVTOOLS
}))
""")

    def test_prepared_runtime_runs_cli_and_child_node_with_empty_path_and_spaces(self):
        with patch.dict(os.environ, self.environment, clear=True):
            node, cli = LAUNCHER.resolve_desktop_runtime(self.tools, self.desktop)
        original_environment = {**self.environment, "Path": ""}
        before = dict(original_environment)
        environment = LAUNCHER.create_desktop_environment(original_environment, node, self.tools)
        result = subprocess.run([str(node), str(cli), "dev", "two words"], cwd=self.desktop,
                                env=environment, capture_output=True, text=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(Path(output["executable"]), self.node)
        self.assertEqual(Path(output["childExecutable"]), self.node)
        self.assertEqual(output["arguments"], ["dev", "two words"])
        self.assertEqual(Path(output["profile"]), self.tools / "airi-stage1-desktop-profile")
        self.assertEqual(output["localMode"], "true")
        self.assertEqual(output["devtools"], "false")
        self.assertEqual(original_environment, before)
        self.assertEqual([key for key in environment if key.upper() == "PATH"], ["PATH"])

    def test_explicit_runtime_takes_priority_over_prepared_runtime(self):
        with patch.dict(os.environ, self.environment, clear=True):
            node, cli = LAUNCHER.resolve_desktop_runtime(self.tools, self.desktop, self.source_node)
        self.assertEqual(node, self.source_node)
        self.assertEqual(cli, self.cli)

    def test_installed_runtime_works_without_prepared_runtime(self):
        environment = {**self.environment, "PATH": str(self.source_node.parent)}
        with patch.dict(os.environ, environment, clear=True):
            node, _cli = LAUNCHER.resolve_desktop_runtime(self.root / "unprepared tools", self.desktop)
        self.assertEqual(node, self.source_node)

    def test_invalid_explicit_runtime_does_not_fall_back_to_prepared_runtime(self):
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, r"Node\.js.*--node-path"):
                LAUNCHER.resolve_desktop_runtime(self.tools, self.desktop, self.root / "missing node.exe")

    def test_missing_runtime_has_an_actionable_error(self):
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, r"Node\.js.*--node-path"):
                LAUNCHER.resolve_desktop_runtime(self.root / "unprepared tools", self.desktop)

    def test_missing_desktop_dependency_names_the_install_command(self):
        with self.assertRaisesRegex(RuntimeError, r"electron-vite\.js.*pnpm install"):
            LAUNCHER.resolve_desktop_runtime(self.tools, self.root / "unprepared desktop", self.source_node)

    def test_service_runner_uses_the_resolved_node_without_a_command_shell(self):
        marker = self.root / "controlled desktop ready"
        cli = self.desktop / "controlled desktop cli.cjs"
        write_file(cli, self.cli.read_text(encoding="utf-8")
                   + "\nrequire('node:fs').writeFileSync(" + json.dumps(str(marker)) + ", 'ready')\n")
        models = {18086: "zipformer-small-zh-en-int8", 18085: "smart-turn-v3.2-cpu",
                  18083: "qwen3.5-2b-bf16", 18084: "moss-tts-nano-100m"}

        def health(port, _path="/health", _token=None):
            if port == 9250:
                return [{"url": "http://localhost:5173/"}] if marker.is_file() else None
            return {"status": "ready", "model": models[port], "message_recording": {"enabled": True}}

        logs = self.root / "controlled service logs"
        logs.mkdir()
        arguments = SimpleNamespace(services_only=False, no_message_recording=False)
        journal = io.StringIO()
        processes = []
        start_process = subprocess.Popen

        def run_process(*args, **kwargs):
            process = start_process(*args, **kwargs)
            processes.append(process)
            return process

        with patch.dict(os.environ, self.environment, clear=True), redirect_stdout(journal):
            with patch.object(LAUNCHER, "get_health", side_effect=health):
                with patch.object(LAUNCHER, "port_is_free", return_value=True):
                    with patch.object(LAUNCHER.subprocess, "Popen", side_effect=run_process):
                        try:
                            result = LAUNCHER.start_services(arguments, self.tools, self.root / "unused models", logs,
                                                             self.root / "unused llm.exe", self.root / "unused asr.exe",
                                                             self.root / "unused turn.exe", self.root / "unused tts.exe",
                                                             self.root / "unused asr", self.root / "unused turn",
                                                             self.root / "unused tts", self.desktop, self.node, cli)
                        except RuntimeError as error:
                            self.fail(str(error) + "\n" + (logs / "desktop.stderr.log").read_text(encoding="utf-8"))
                        finally:
                            for process in processes:
                                process.wait(timeout=5)
        self.assertEqual(result, 0)
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].args, [str(self.node), str(cli), "dev"])
        self.assertEqual((logs / "desktop.stderr.log").read_text(encoding="utf-8"), "")
        output = json.loads((logs / "desktop.stdout.log").read_text(encoding="utf-8"))
        self.assertEqual(Path(output["executable"]), self.node)
        self.assertEqual(Path(output["childExecutable"]), self.node)
        self.assertEqual(output["arguments"], ["dev"])
        events = [json.loads(line) for line in journal.getvalue().splitlines()]
        self.assertEqual([event["service"] for event in events if "service" in event],
                         ["asr", "turn", "llm", "tts", "desktop"])
        self.assertTrue(events[-1]["ready"])


@unittest.skipUnless(os.name == "nt", "The prepared launcher supports Windows runtimes.")
class LauncherPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / ".temp"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="humanning prepared tree ", dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tools = self.root / "tools with spaces"
        self.models = self.root / "models with spaces"
        self.entry = self.root / "scripts/start-local-voice.py"
        write_file(self.entry, (ROOT / "scripts/start-local-voice.py").read_text(encoding="utf-8"))
        files = [
            self.models / "airllm-cuda-venv/Scripts/python.exe",
            self.models / "qwen3.5-2b-bf16/config.json",
            self.models / "qwen3.5-2b-bf16/model.safetensors-00001-of-00001.safetensors",
            self.tools / "fast-asr/venv/Scripts/python.exe",
            self.tools / "smart-turn-v3.2/venv/Scripts/python.exe",
            self.tools / "smart-turn-v3.2/smart-turn-v3.2-cpu.onnx",
            self.tools / "moss-tts-nano/venv/Scripts/python.exe",
            self.tools / "moss-tts-nano/MOSS-TTS-Nano-main/ort_cpu_runtime.py",
            self.tools / "moss-tts-nano/models/MOSS-TTS-Nano-100M-ONNX/browser_poc_manifest.json",
            self.tools / "moss-tts-nano/models/MOSS-TTS-Nano-100M-ONNX/moss_tts_global_shared.data",
            self.tools / "moss-tts-nano/models/MOSS-TTS-Nano-100M-ONNX/moss_tts_local_shared.data",
            self.root / "apps/stage-tamagotchi/node_modules/electron-vite/bin/electron-vite.js",
        ]
        files.extend(self.tools / "fast-asr/zipformer-bilingual-small" / name for name in (
            "encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.onnx",
            "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt"))
        files.extend(self.root / "scripts" / name for name in (
            "serve-qwen35-2b-bf16.py", "serve-fast-asr.py", "serve-smart-turn.py", "serve-local-moss-tts.py"))
        for path in files:
            write_file(path)

    def run_entry(self, *arguments, environment=None):
        return subprocess.run([sys.executable, str(self.entry), "--tools-dir", str(self.tools),
                               "--models-dir", str(self.models), *arguments], cwd=self.root,
                              env=environment or isolated_environment(), capture_output=True, text=True,
                              timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_invalid_explicit_runtime_fails_before_any_service_start(self):
        result = self.run_entry("--node-path", str(self.root / "missing node.exe"))
        self.assertEqual(result.returncode, 1, result.stderr)
        output = json.loads(result.stdout)
        self.assertFalse(output["ready"])
        self.assertIn("Node.js", output["error"])
        self.assertIn("--node-path", output["error"])
        self.assertFalse((self.tools / "local-voice-logs").exists())

    def test_environment_override_is_checked_before_any_service_start(self):
        environment = {**isolated_environment(), "HUMANNING_NODE": str(self.root / "missing node.exe")}
        result = self.run_entry(environment=environment)
        self.assertEqual(result.returncode, 1, result.stderr)
        output = json.loads(result.stdout)
        self.assertFalse(output["ready"])
        self.assertIn("Node.js", output["error"])
        self.assertFalse((self.tools / "local-voice-logs").exists())

    def test_invalid_executable_fails_before_any_service_start(self):
        node = self.tools / "invalid node.exe"
        write_file(node, "This file is not a Node executable.")
        result = self.run_entry("--node-path", str(node))
        self.assertEqual(result.returncode, 1, result.stderr)
        output = json.loads(result.stdout)
        self.assertFalse(output["ready"])
        self.assertIn("Cannot run the Node.js runtime", output["error"])
        self.assertFalse((self.tools / "local-voice-logs").exists())

    def test_services_only_preflight_requires_neither_node_nor_desktop_dependencies(self):
        (self.root / "apps/stage-tamagotchi/node_modules/electron-vite/bin/electron-vite.js").unlink()
        result = self.run_entry("--services-only", "--check", "--node-path", str(self.root / "missing node.exe"))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["prepared_paths"])
        self.assertIsNone(output["desktop_node"])
        self.assertFalse((self.tools / "local-voice-logs").exists())

    def run_batch(self, *arguments):
        batch = self.root / "Start-Humanning.cmd"
        write_file(batch, (ROOT / "Start-Humanning.cmd").read_text(encoding="utf-8"))
        venv.EnvBuilder(with_pip=False).create(self.root / "local-models/airllm-cuda-venv")
        command = subprocess.list2cmdline([str(batch), "--tools-dir", str(self.tools),
                                           "--models-dir", str(self.models), *arguments])
        shell = subprocess.list2cmdline([os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), "/d", "/s", "/c"])
        return subprocess.run(shell + ' "' + command + '"', cwd=self.root,
                              env=isolated_environment(), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_batch_preflight_returns_zero_with_closed_input(self):
        result = self.run_batch("--check", "--node-path", str(real_node()))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('"prepared_paths": true', result.stdout)
        self.assertIn("Humanning is running", result.stdout)
        self.assertFalse((self.tools / "local-voice-logs").exists())

    def test_batch_preflight_returns_one_for_an_invalid_node(self):
        result = self.run_batch("--check", "--node-path", str(self.root / "missing node.exe"))
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn("Node.js", result.stdout)
        self.assertIn("Humanning could not start", result.stdout)
        self.assertFalse((self.tools / "local-voice-logs").exists())


if __name__ == "__main__":
    unittest.main()
