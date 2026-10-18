"""Start the prepared local Humanning voice services and desktop on Windows.

This entry does not install packages, download weights, or change Windows policy.
The original start-local-airi.ps1 entry remains available for its separate setup.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def resolve_desktop_runtime(tools, desktop_root, explicit_node=None):
    """Resolve prepared Node first. Explorer does not inherit a coding terminal's PATH."""
    cli = desktop_root / "node_modules/electron-vite/bin/electron-vite.js"
    if not cli.is_file():
        raise RuntimeError(f"Desktop dependencies are missing: {cli}. Run pnpm install in this checkout.")
    if explicit_node is not None:
        node = explicit_node.expanduser().resolve()
    else:
        prepared = tools / "node-v26.7.0-win-x64/node.exe"
        installed = shutil.which("node")
        node = prepared.resolve() if prepared.is_file() else Path(installed).resolve() if installed else None
    if node is None or not node.is_file():
        raise RuntimeError("Node.js runtime was not found. Prepare .tools/node-v26.7.0-win-x64 in the parent folder, or use --node-path PATH_TO_NODE_EXE.")
    try:
        result = subprocess.run([str(node), "--version"], capture_output=True, text=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Cannot run the Node.js runtime: {node}.") from error
    version = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
    supported = False
    if version:
        major, minor, _patch = map(int, version.groups())
        supported = major > 22 or (major == 22 and minor >= 12) or (major == 20 and minor >= 19)
    if result.returncode != 0 or not supported:
        raise RuntimeError(f"Unsupported Node.js runtime: {node}. Use Node 20.x from 20.19, or Node 22.12 and later.")
    return node, cli


def create_desktop_environment(environment, node, tools):
    """Keep runtime paths inside the child process. Do not change the user's system PATH."""
    result = dict(environment)
    path_keys = [key for key in result if key.upper() == "PATH"]
    previous_path = next((result[key] for key in path_keys), "")
    for key in path_keys:
        del result[key]
    result.update({"PATH": str(node.parent) + (os.pathsep + previous_path if previous_path else ""),
                   "APP_USER_DATA_PATH": str(tools / "airi-stage1-desktop-profile"),
                   "APP_REMOTE_DEBUG": "true", "APP_REMOTE_DEBUG_PORT": "9250", "APP_REMOTE_DEBUG_NO_OPEN": "true",
                   "VITE_AIRI_LOCAL_MODE": "true", "AIRI_VUE_DEVTOOLS": "false"})
    return result


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
    parser.add_argument("--llm", choices=("gemma", "qwen"), default="gemma")
    parser.add_argument("--tts", choices=("gpt-sovits", "moss"), default="gpt-sovits")
    parser.add_argument("--context-size", type=int, default=16384)
    parser.add_argument("--node-path", type=Path, default=os.environ.get("HUMANNING_NODE") or None,
                        help="Use an explicit Node.js executable for the desktop")
    parser.add_argument("--services-only", action="store_true")
    parser.add_argument("--check", action="store_true", help="Check prepared paths and the Node.js version without starting services")
    parser.add_argument("--no-message-recording", action="store_true", help="Keep chat history but disable full model request JSON records")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This launcher uses the prepared Windows runtimes. Start each service directly on other systems.")
    if not 4096 <= args.context_size <= 131072:
        parser.error("context-size must be between 4096 and 131072 tokens")
    tools = args.tools_dir.resolve()
    models = args.models_dir.resolve()
    asr_root = tools / "fast-asr"
    tts_root = tools / ("gpt-sovits-v2proplus" if args.tts == "gpt-sovits" else "moss-tts-nano")
    turn_root = tools / "smart-turn-v3.2"
    llm_python = models / "airllm-cuda-venv/Scripts/python.exe"
    tts_python = (tools / "gpt-sovits-venv/Scripts/python.exe" if args.tts == "gpt-sovits"
                  else tts_root / "venv/Scripts/python.exe")
    asr_python = asr_root / "venv/Scripts/python.exe"
    turn_python = turn_root / "venv/Scripts/python.exe"
    desktop_root = ROOT / "apps/stage-tamagotchi"
    desktop_node = desktop_cli = None
    if not args.services_only:
        desktop_node, desktop_cli = resolve_desktop_runtime(tools, desktop_root, args.node_path)
    required = [
        llm_python, tts_python, asr_python, turn_python,
        ROOT / "scripts/serve-qwen35-2b-bf16.py",
        ROOT / "scripts/serve-fast-asr.py",
        ROOT / "scripts/serve-smart-turn.py",
        ROOT / "scripts/local-message-context.py",
        turn_root / "smart-turn-v3.2-cpu.onnx",
    ]
    if args.tts == "gpt-sovits":
        required.extend([ROOT / "scripts/serve-local-gpt-sovits.py",
                         tools / "modern-voice-2026-10-17/vivian-curious.wav",
                         tts_root / "publisher-manifest.json",
                         tts_root / "GPT_SoVITS/TTS_infer_pack/TTS.py"])
        required.extend(tts_root / "GPT_SoVITS/pretrained_models" / name for name in (
            "s1v3.ckpt", "v2Pro/s2Gv2ProPlus.pth", "sv/pretrained_eres2netv2w24s4ep4.ckpt",
            "chinese-roberta-wwm-ext-large/pytorch_model.bin", "chinese-roberta-wwm-ext-large/config.json",
            "chinese-roberta-wwm-ext-large/tokenizer.json", "chinese-hubert-base/pytorch_model.bin",
            "chinese-hubert-base/config.json", "chinese-hubert-base/preprocessor_config.json", "fast_langdetect/lid.176.bin"))
        required.extend(tts_root / "GPT_SoVITS/text/G2PWModel" / name for name in (
            "g2pW.onnx", "config.py", "POLYPHONIC_CHARS.txt", "MONOPHONIC_CHARS.txt",
            "char_bopomofo_dict.json", "bopomofo_to_pinyin_wo_tune_dict.json"))
    else:
        required.extend([ROOT / "scripts/serve-local-moss-tts.py",
                         tts_root / "MOSS-TTS-Nano-main/ort_cpu_runtime.py",
                         tts_root / "models/MOSS-TTS-Nano-100M-ONNX/browser_poc_manifest.json",
                         tts_root / "models/MOSS-TTS-Nano-100M-ONNX/moss_tts_global_shared.data",
                         tts_root / "models/MOSS-TTS-Nano-100M-ONNX/moss_tts_local_shared.data"])
    if args.llm == "gemma":
        required.extend([ROOT / "scripts/serve-local-gemma.py",
                         models / "runtime/llama.cpp-b11146-vulkan/llama-server.exe",
                         models / "gemma4-e2b-int4/gemma-4-E2B_q4_0-it.gguf"])
    else:
        required.extend([models / "qwen3.5-2b-bf16/config.json",
                         models / "qwen3.5-2b-bf16/model.safetensors-00001-of-00001.safetensors"])
    required.extend(asr_root / "zipformer-bilingual-small" / name for name in (
        "encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.onnx",
        "joiner-epoch-99-avg-1.int8.onnx", "tokens.txt"))
    if not args.services_only:
        required.extend([desktop_node, desktop_cli])
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        print(json.dumps({"ready": False, "missing_files": missing}, ensure_ascii=False, indent=2))
        return 1
    if args.check:
        print(json.dumps({"prepared_paths": True, "files_checked": len(required),
                          "desktop_node": str(desktop_node) if desktop_node else None,
                          "llm": args.llm, "context_size": args.context_size, "tts": args.tts,
                          "note": "This checks file presence and the desktop Node version. Each model service checks its weights at startup."}))
        return 0

    logs = tools / "local-voice-logs"
    logs.mkdir(parents=True, exist_ok=True)
    with startup_lock(logs):
        return start_services(args, tools, models, logs, llm_python, asr_python, turn_python, tts_python,
                              asr_root, turn_root, tts_root, desktop_root, desktop_node, desktop_cli)


def start_services(args, tools, models, logs, llm_python, asr_python, turn_python, tts_python,
                   asr_root, turn_root, tts_root, desktop_root, desktop_node, desktop_cli):
    environment = {**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
                   "HF_HUB_DISABLE_TELEMETRY": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    if args.llm == "gemma":
        llm_arguments = [str(llm_python), "-u", str(ROOT / "scripts/serve-local-gemma.py"),
                         "--port", "18083", "--upstream", "http://127.0.0.1:18088", "--warmup"]
        llm_id = "gemma4-e2b-int4"
    else:
        llm_arguments = [str(llm_python), "-u", str(ROOT / "scripts/serve-qwen35-2b-bf16.py"),
                         "--model-path", str(models / "qwen3.5-2b-bf16"), "--port", "18083", "--release-cuda-cache"]
        llm_id = "qwen3.5-2b-bf16"
    if not args.no_message_recording:
        llm_arguments.extend(["--record-messages-dir", str(tools / "airi-message-records")])
    if args.tts == "gpt-sovits":
        tts_arguments = [str(tts_python), "-u", str(ROOT / "scripts/serve-local-gpt-sovits.py"),
                         "--runtime-dir", str(tts_root), "--reference",
                         str(tools / "modern-voice-2026-10-17/vivian-curious.wav"),
                         "--port", "18084", "--threads", "4"]
        tts_id = "gpt-sovits-v2proplus"
    else:
        tts_arguments = [str(tts_python), "-u", str(ROOT / "scripts/serve-local-moss-tts.py"),
                         "--runtime-dir", str(tts_root), "--port", "18084", "--threads", "2"]
        tts_id = "moss-tts-nano-100m"
    services = [
        ("asr", 18086, [str(asr_python), "-u", str(ROOT / "scripts/serve-fast-asr.py"), "--model-dir",
                        str(asr_root / "zipformer-bilingual-small"), "--port", "18086", "--ws-port", "18087", "--threads", "2"], ROOT, environment, "zipformer-small-zh-en-int8"),
        ("turn", 18085, [str(turn_python), "-u", str(ROOT / "scripts/serve-smart-turn.py"), "--model",
                         str(turn_root / "smart-turn-v3.2-cpu.onnx"), "--port", "18085", "--threads", "2"], ROOT, environment, "smart-turn-v3.2-cpu"),
    ]
    if args.llm == "gemma":
        services.append(("llm-engine", 18088, [str(models / "runtime/llama.cpp-b11146-vulkan/llama-server.exe"),
            "--model", str(models / "gemma4-e2b-int4/gemma-4-E2B_q4_0-it.gguf"),
            "--host", "127.0.0.1", "--port", "18088", "--alias", llm_id,
            "--ctx-size", str(args.context_size), "--parallel", "1", "--gpu-layers", "all",
            "--flash-attn", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
            "--reasoning", "off"], ROOT, environment, llm_id))
    services.extend([
        ("llm", 18083, llm_arguments, ROOT, environment, llm_id),
        ("tts", 18084, tts_arguments, ROOT, environment, tts_id),
    ])
    if not args.services_only:
        desktop_environment = create_desktop_environment(environment, desktop_node, tools)
        services.append(("desktop", 9250, [str(desktop_node), str(desktop_cli), "dev"], desktop_root, desktop_environment, None))

    started = []
    try:
        for name, port, command, cwd, child_environment, expected_model in services:
            path = "/json/list" if name == "desktop" else "/health"

            def ready():
                health = get_health(port, path)
                if name == "llm-engine":
                    catalog = get_health(port, "/v1/models")
                    return (isinstance(health, dict) and health.get("status") == "ok"
                            and isinstance(catalog, dict) and any(model.get("id") == expected_model for model in catalog.get("data", [])))
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
