"""Start the prepared QQ services, then run the local AIRI voice launcher on Windows."""

import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_json(path, label):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Cannot read {label}. See docs/ai/qq-one-click-startup.zh-CN.md.") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid {label}. An object is required.")
    return value


def local_path(value, base):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("The QQ startup configuration contains an empty path.")
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def load_configuration(path):
    if not path.is_file():
        return None
    startup = read_json(path, "QQ startup configuration")
    if startup.get("enabled") is False:
        return None
    if startup.get("enabled") is not True:
        raise RuntimeError("Set enabled to true or false in the QQ startup configuration.")
    qq_path = local_path(startup.get("qqConfig"), path.parent)
    qq = read_json(qq_path, "AIRI qq.json")
    if qq.get("enabled") is not True:
        return None
    if not isinstance(qq.get("apiUrl"), str):
        raise RuntimeError("Set a loopback apiUrl in the local AIRI qq.json.")
    address = urlsplit(qq["apiUrl"])
    if (address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"}
            or address.username or address.password or address.query or address.fragment):
        raise RuntimeError("The Satori API must use a loopback HTTP address without credentials or query parameters.")
    if not all(isinstance(qq.get(key), str) and qq[key].strip() for key in ("token", "platform", "selfId")):
        raise RuntimeError("Set token, platform, and selfId in the local AIRI qq.json.")
    wait = startup.get("waitSeconds", 90)
    if type(wait) is not int or not 1 <= wait <= 300:
        raise RuntimeError("QQ waitSeconds must be an integer from 1 to 300.")
    commands = {}
    for name in ("napcat", "koishi"):
        service = startup.get(name)
        if not isinstance(service, dict):
            raise RuntimeError(f"Set the {name} command and cwd in the QQ startup configuration.")
        command = service.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise RuntimeError(f"The {name} command must be an array of nonempty strings.")
        executable = local_path(command[0], path.parent)
        cwd = local_path(service.get("cwd"), path.parent)
        if not executable.is_file() or executable.suffix.lower() != ".exe" or not cwd.is_dir():
            raise RuntimeError(f"The {name} executable or working directory is unavailable. Update the local startup configuration.")
        commands[name] = {"command": [str(executable), *command[1:]], "cwd": cwd}
        if len(command) > 1 and Path(command[1]).is_absolute() and not Path(command[1]).is_file():
            raise RuntimeError(f"The {name} CLI file is unavailable. Update the local startup configuration.")
    napcat_port = startup["napcat"].get("port", 3001)
    if type(napcat_port) is not int or not 1 <= napcat_port <= 65535:
        raise RuntimeError("The NapCat port must be an integer from 1 to 65535.")
    return {"qq": qq, "commands": commands, "wait": wait, "napcat_port": napcat_port,
            "satori_port": address.port or 80, "satori_host": address.hostname}


def probe_login(qq):
    request = Request(qq["apiUrl"].rstrip("/") + "/login.get", data=b"{}", method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {qq['token']}",
        "Satori-Platform": qq["platform"], "Satori-User-ID": qq["selfId"],
    })
    try:
        # Local service traffic never uses a system proxy or follows a redirect with credentials.
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=2) as response:
            login = json.loads(response.read(65537))
        if not isinstance(login, dict) or not isinstance(login.get("user"), dict):
            return "invalid-response"
        if str(login["user"].get("id")) != qq["selfId"] or login.get("platform") != qq["platform"]:
            return "account-mismatch"
        return "online" if login.get("status") == 1 else "login-required"
    except HTTPError as error:
        return "authentication-failed" if error.code in (401, 403) else "api-unavailable"
    except (OSError, URLError, ValueError):
        return "service-unavailable"


def port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def running_processes():
    try:
        result = subprocess.run([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); Get-CimInstance Win32_Process | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Cannot inspect running QQ services. No duplicate process was started.") from error
    if result.returncode:
        raise RuntimeError("Cannot inspect running QQ services. No duplicate process was started.")
    try:
        processes = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError("Cannot inspect running QQ services. No duplicate process was started.") from error
    if not isinstance(processes, list):
        raise RuntimeError("Cannot inspect running QQ services. No duplicate process was started.")
    return processes


def process_matches(process, service):
    normalize = lambda value: str(value or "").replace("\\", "/").casefold()
    command = service["command"]
    if normalize(process.get("ExecutablePath")) != normalize(command[0]):
        return False
    # Koishi uses one runtime for multiple instances. Match its CLI path as well.
    if len(command) > 1 and Path(command[1]).is_absolute():
        return normalize(command[1]) in normalize(process.get("CommandLine"))
    return True


def start_qq(configuration, logs):
    current = probe_login(configuration["qq"])
    if current == "online":
        return {"service": "qq", "ready": True, "status": "already_ready"}
    if current in {"authentication-failed", "account-mismatch"}:
        return {"service": "qq", "ready": False, "status": current}
    processes = running_processes()
    for name, host, port in (("napcat", "127.0.0.1", configuration["napcat_port"]),
                             ("koishi", configuration["satori_host"], configuration["satori_port"])):
        service = configuration["commands"][name]
        if port_open(host, port) or any(process_matches(process, service) for process in processes):
            print(json.dumps({"service": name, "status": "already_running"}), flush=True)
            continue
        with (logs / f"{name}.stdout.log").open("ab") as stdout, (logs / f"{name}.stderr.log").open("ab") as stderr:
            subprocess.Popen(service["command"], cwd=service["cwd"], stdin=subprocess.DEVNULL,
                             stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW, shell=False)
        print(json.dumps({"service": name, "status": "started"}), flush=True)
    print(json.dumps({"service": "qq", "status": "waiting_for_login", "wait_seconds": configuration["wait"]}), flush=True)
    deadline = time.monotonic() + configuration["wait"]
    while True:
        current = probe_login(configuration["qq"])
        if current == "online":
            return {"service": "qq", "ready": True, "status": "ready"}
        if current in {"authentication-failed", "account-mismatch"} or time.monotonic() >= deadline:
            return {"service": "qq", "ready": False, "status": current,
                    "help": "Open NapCat WebUI and confirm QQ login. Then run Start-Humanning.cmd again."}
        time.sleep(1)


def voice_module():
    spec = importlib.util.spec_from_file_location("companion_voice_launcher", ROOT / "scripts/start-local-voice.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog="Other arguments pass to scripts/start-local-voice.py.")
    parser.add_argument("--tools-dir", type=Path, default=ROOT.parent / ".tools")
    parser.add_argument("--qq-config", type=Path, help="Local QQ startup paths. Default: TOOLS_DIR/qq-startup.json")
    parser.add_argument("--skip-qq-startup", action="store_true", help="Skip QQ service startup. Existing QQ connections remain enabled.")
    parser.add_argument("--qq-only", action="store_true", help="Start or inspect QQ services without starting AIRI or models")
    parser.add_argument("--check", action="store_true", help="Inspect prepared paths without connecting or starting services")
    args, forwarded = parser.parse_known_args()
    if os.name != "nt":
        parser.error("This launcher uses the prepared Windows runtimes.")
    tools = args.tools_dir.expanduser().resolve()
    path = (args.qq_config or tools / "qq-startup.json").expanduser().resolve()
    result = {"service": "qq", "ready": False, "status": "not-configured"}
    try:
        if args.skip_qq_startup or "--services-only" in forwarded:
            result["status"] = "skipped"
        else:
            configuration = load_configuration(path)
            if configuration:
                if args.check:
                    result = {"service": "qq", "prepared_paths": True, "status": "paths_checked", "online_checked": False}
                else:
                    logs = tools / "local-qq-logs"
                    logs.mkdir(parents=True, exist_ok=True)
                    with voice_module().startup_lock(logs):
                        result = start_qq(configuration, logs)
    except (OSError, RuntimeError, ValueError) as error:
        # Configuration and network errors never include tokens, response bodies, or command arguments.
        result = {"service": "qq", "ready": False, "status": "unavailable", "help": str(error) if isinstance(error, RuntimeError)
                  else "QQ startup failed. Inspect local-qq-logs and the local startup configuration."}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.qq_only:
        return 0 if result.get("ready") or result.get("prepared_paths") else 1
    if not result.get("ready") and not args.check:
        print("QQ is not confirmed online. AIRI will still start. No QQ success is implied.", flush=True)
    command = [sys.executable, "-B", str(ROOT / "scripts/start-local-voice.py"), "--tools-dir", str(tools), *forwarded]
    if args.check:
        command.append("--check")
    voice_result = subprocess.run(command, check=False).returncode
    return voice_result or (1 if args.check and result["status"] == "unavailable" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
