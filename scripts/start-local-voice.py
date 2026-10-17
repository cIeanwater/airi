"""Start the prepared local Humanning voice services and desktop on Windows.

This entry does not install packages, download weights, or change Windows policy.
The original start-local-airi.ps1 entry remains available for its separate setup.
"""

import argparse
import json
import os
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def get_health(port, path="/health", token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with urlopen(Request(f"http://127.0.0.1:{port}{path}", headers=headers), timeout=2) as response:
            data = response.read(1024 * 1024).decode("utf-8")
            return json.loads(data) if data.startswith(("{", "[")) else data
    except (URLError, TimeoutError, OSError, ValueError):
        return None


def port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


@contextmanager
def startup_lock(logs):
    import msvcrt

    with (logs / "launcher.lock").open("a+b") as lock:
        if lock.seek(0, os.SEEK_END) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("Another launcher is starting Humanning. Wait for it to finish, then try again.") from error
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-dir", type=Path, default=ROOT.parent / ".tools")
    parser.add_argument("--models-dir", type=Path, default=ROOT / "local-models")
    parser.add_argument("--services-only", action="store_true")
    parser.add_argument("--check", action="store_true", help="Check prepared file paths without starting a process")
    parser.add_argument("--no-message-recording", action="store_true", help="Keep chat history but disable full model request JSON records")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This launcher uses the prepared Windows runtimes. Start each service directly on other systems.")
    tools = args.tools_dir.resolve()
    models = args.models_dir.resolve()
    asr_root = tools / "fast-asr"
    tts_root = tools / "moss-tts-nano"
    turn_root = tools / "smart-turn-v3.2"
    llm_python = models / "airllm-cuda-venv/Scripts/python.exe"
    tts_python = tts_root / "venv/Scripts/python.exe"
    asr_python = asr_root / "venv/Scripts/python.exe"
    turn_python = turn_root / "venv/Scripts/python.exe"
    desktop_root = ROOT / "apps/stage-tamagotchi"
    desktop_command = desktop_root / "node_modules/.bin/electron-vite.cmd"
    required = [
        llm_python, tts_python, asr_python, turn_python,
        ROOT / "scripts/serve-qwen35-2b-bf16.py",
        ROOT / "scripts/serve-fast-asr.py",
        ROOT / "scripts/serve-smart-turn.py",
        ROOT / "scripts/serve-local-moss-tts.py",
        models / "qwen3.5-2b-bf16/config.json",
        models / "qwen3.5-2b-bf16/model.safetensors-00001-of-00001.safetensors",
        tts_root / "MOSS-TTS-Nano-main/ort_cpu_runtime.py",
        tts_root / "models/MOSS-TTS-Nano-100M-ONNX/browser_poc_manifest.json",
        tts_root / "models/MOSS-TTS-Nano-100M-ONNX/moss_tts_global_shared.data",
        tts_root / "models/MOSS-TTS-Nano-100M-ONNX/moss_tts_local_shared.data",
        turn_root / "smart-turn-v3.2-cpu.onnx",
    ]
    required.extend(asr_root / "zipformer-bilingual-small" / name for name in (
        "encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.onnx",
        "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt"))
    if not args.services_only:
        required.append(desktop_command)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        print(json.dumps({"ready": False, "missing_files": missing}, ensure_ascii=False, indent=2))
        return 1
    if args.check:
        print(json.dumps({"prepared_paths": True, "files_checked": len(required),
                          "note": "This checks file presence. Each model service checks its weights at startup."}))
        return 0

    logs = tools / "local-voice-logs"
    logs.mkdir(parents=True, exist_ok=True)
    with startup_lock(logs):
        return start_services(args, tools, models, logs, llm_python, asr_python, turn_python, tts_python,
                              asr_root, turn_root, tts_root, desktop_root, desktop_command)


def start_services(args, tools, models, logs, llm_python, asr_python, turn_python, tts_python,
                   asr_root, turn_root, tts_root, desktop_root, desktop_command):
    environment = {**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
                   "HF_HUB_DISABLE_TELEMETRY": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    llm_arguments = [str(llm_python), "-u", str(ROOT / "scripts/serve-qwen35-2b-bf16.py"),
                     "--model-path", str(models / "qwen3.5-2b-bf16"), "--port", "18083", "--release-cuda-cache"]
    if not args.no_message_recording:
        llm_arguments.extend(["--record-messages-dir", str(tools / "airi-message-records")])
    services = [
        ("asr", 18086, [str(asr_python), "-u", str(ROOT / "scripts/serve-fast-asr.py"), "--model-dir",
                        str(asr_root / "zipformer-bilingual-small"), "--port", "18086", "--ws-port", "18087", "--threads", "2"], ROOT, environment, "zipformer-small-zh-en-int8"),
        ("turn", 18085, [str(turn_python), "-u", str(ROOT / "scripts/serve-smart-turn.py"), "--model",
                         str(turn_root / "smart-turn-v3.2-cpu.onnx"), "--port", "18085", "--threads", "2"], ROOT, environment, "smart-turn-v3.2-cpu"),
        ("llm", 18083, llm_arguments, ROOT, environment, "qwen3.5-2b-bf16"),
        ("tts", 18084, [str(tts_python), "-u", str(ROOT / "scripts/serve-local-moss-tts.py"), "--runtime-dir",
                        str(tts_root), "--port", "18084", "--threads", "2"], ROOT, environment, "moss-tts-nano-100m"),
    ]
    if not args.services_only:
        desktop_environment = {**environment, "APP_USER_DATA_PATH": str(tools / "airi-stage1-desktop-profile"),
                               "APP_REMOTE_DEBUG": "true", "APP_REMOTE_DEBUG_PORT": "9250", "APP_REMOTE_DEBUG_NO_OPEN": "true",
                               "VITE_AIRI_LOCAL_MODE": "true", "AIRI_VUE_DEVTOOLS": "false"}
        services.append(("desktop", 9250, [str(desktop_command), "dev"], desktop_root, desktop_environment, None))

    started = []
    try:
        for name, port, command, cwd, child_environment, expected_model in services:
            path = "/json/list" if name == "desktop" else "/health"

            def ready():
                health = get_health(port, path)
                if expected_model and isinstance(health, dict):
                    return health.get("status") == "ready" and health.get("model_id", health.get("model")) == expected_model
                if name == "desktop":
                    return isinstance(health, list) and any("localhost:5173/" in page.get("url", "") for page in health)
                return health is not None

            if ready():
                if name == "llm":
                    health = get_health(port)
                    if not isinstance(health, dict):
                        raise RuntimeError("The existing LLM stopped responding during its health check. Try starting again.")
                    if health.get("message_recording", {}).get("enabled") == args.no_message_recording:
                        raise RuntimeError("The existing LLM uses a different message recording setting. Restart it with the requested setting.")
                print(json.dumps({"service": name, "status": "already_ready", "port": port}), flush=True)
                continue
            if not port_is_free(port):
                raise RuntimeError(f"Port {port} is occupied by a service that did not pass the {name} health check.")
            if name == "asr" and not port_is_free(18087):
                raise RuntimeError("Port 18087 is occupied. The local ASR needs this WebSocket port.")
            with (logs / f"{name}.stdout.log").open("ab") as stdout, (logs / f"{name}.stderr.log").open("ab") as stderr:
                process = subprocess.Popen(command, cwd=cwd, env=child_environment, stdin=subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
            started.append(process)
            deadline = time.monotonic() + 180
            while not ready():
                if process.poll() is not None:
                    raise RuntimeError(f"{name} exited with code {process.returncode}. Read {logs / (name + '.stderr.log')}.")
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{name} did not become ready within 180 seconds. Read {logs}.")
                time.sleep(1)
            print(json.dumps({"service": name, "status": "ready", "port": port, "launcher_pid": process.pid}), flush=True)
    except (OSError, RuntimeError):
        print(json.dumps({"started_launcher_pids": [process.pid for process in started], "logs": str(logs),
                          "note": "Services that passed their health checks remain running."}), flush=True)
        raise
    llm_health = get_health(18083)
    print(json.dumps({"ready": True, "logs": str(logs),
                      "message_recording": llm_health.get("message_recording") if isinstance(llm_health, dict) else None}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(json.dumps({"ready": False, "error": str(error)}, ensure_ascii=False), flush=True)
        raise SystemExit(1)
