"""Prepare the official Gemma 4 E2B QAT INT4 text model without loading a GPU."""

import argparse
import hashlib
import json
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "google/gemma-4-E2B-it-qat-q4_0-gguf"
REVISION = "675cff42a74c774d6cb76f76d8eacb49b48c9b93"
FILENAME = "gemma-4-E2B_q4_0-it.gguf"
SIZE = 3349516256
SHA256 = "fa401b55b07ee70a54c6dae3903c783a6e65064312529ea57175cb5f8dec6634"
MODEL_DIRECTORY = ROOT / "local-models/gemma4-e2b-int4"
URL = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{FILENAME}?download=true"


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def download_model(directory: Path) -> Path:
    """Resume a public download. Publish the file only after its SHA-256 matches."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / FILENAME
    if target.exists():
        if target.stat().st_size != SIZE or digest(target) != SHA256:
            raise ValueError("The existing Gemma model failed its publisher checksum. It was not changed.")
        return target
    partial = target.with_suffix(".gguf.partial")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > SIZE:
        raise ValueError("The partial Gemma download exceeds its publisher size. It was not changed.")
    if offset < SIZE:
        headers = {"User-Agent": "Humanning-local-model-setup"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(URL, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status == 206:
                if not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                    raise ValueError("The download server returned an unexpected byte range")
                mode = "ab"
            elif response.status == 200:
                offset = 0
                mode = "wb"
            else:
                raise ValueError(f"The download server returned HTTP {response.status}")
            last_report = 0.0
            with partial.open(mode) as output:
                for chunk in iter(lambda: response.read(8 * 1024 * 1024), b""):
                    output.write(chunk)
                    offset += len(chunk)
                    if offset > SIZE:
                        raise ValueError("The download exceeds its publisher size")
                    if time.monotonic() - last_report >= 10:
                        print(json.dumps({"status": "downloading", "bytes": offset, "total_bytes": SIZE}), flush=True)
                        last_report = time.monotonic()
    if partial.stat().st_size != SIZE or digest(partial) != SHA256:
        raise ValueError("The downloaded Gemma model failed its publisher checksum")
    partial.replace(target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Download and verify the pinned official text model")
    args = parser.parse_args()
    target = download_model(MODEL_DIRECTORY) if args.download else MODEL_DIRECTORY / FILENAME
    ready = target.is_file() and target.stat().st_size == SIZE and digest(target) == SHA256
    evidence = {
        "model_id": "gemma4-e2b-int4", "repository": REPOSITORY, "revision": REVISION,
        "filename": FILENAME, "bytes": SIZE, "sha256": SHA256, "verified": ready,
        "source": URL, "path": str(target), "modalities": ["text"],
    }
    if ready:
        (MODEL_DIRECTORY / "publisher-manifest.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence), flush=True)
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
