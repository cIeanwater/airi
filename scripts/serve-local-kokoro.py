"""Start the installed local Kokoro runtime without a PowerShell script.

Call stack:
  main -> installed Python -> uvicorn -> Kokoro FastAPI
"""

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / ".tools" / "Kokoro-FastAPI-0.8.1",
        help="Directory containing the installed Kokoro runtime and its local models",
    )
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--check", action="store_true", help="Check local files without starting the server")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    runtime = args.runtime_dir.resolve()
    python = runtime / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    required = (
        python,
        runtime / "api/src/main.py",
        runtime / "api/src/models/v1_0/config.json",
        runtime / "api/src/models/v1_0/kokoro-v1_0.pth",
        runtime / "api/src/voices/v1_0/zf_xiaoxiao.pt",
    )
    for path in required:
        if not path.is_file():
            print(f"Required local file is missing: {path}", file=sys.stderr)
            return 1
    if args.check:
        print(f"Local Kokoro files are ready: {runtime}")
        return 0

    environment = os.environ.copy()
    environment.update({
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "USE_GPU": "false",
        "DEVICE_TYPE": "cpu",
        "OMP_NUM_THREADS": "4",
        "MKL_NUM_THREADS": "4",
        "MODEL_DIR": str(runtime / "api/src/models"),
        "VOICES_DIR": str(runtime / "api/src/voices/v1_0"),
        "HF_HOME": str(runtime.parent / "voice-models/huggingface"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "DEFAULT_VOICE": "zf_xiaoxiao",
        "ENABLE_WEB_PLAYER": "false",
        "ENABLE_DEBUG_ENDPOINTS": "false",
        "ALLOW_DEV_UNLOAD": "false",
        "ENABLE_VOICE_TAGS": "false",
        "ENABLE_SSML": "false",
        "MAX_INPUT_LENGTH": "8000",
        "CORS_ORIGINS": '["http://localhost:5173","http://127.0.0.1:5173","http://127.0.0.1:5174","http://localhost:4173","http://127.0.0.1:4173"]',
        "API_LOG_LEVEL": "INFO",
    })
    os.chdir(runtime)
    os.execve(python, [str(python), "-m", "uvicorn", "api.src.main:app", "--host", "127.0.0.1", "--port", str(args.port)], environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
