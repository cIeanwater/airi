"""Download a pinned public LongMemEval split without third-party packages.

Call stack:

main
  -> read datasets.json
  -> download
    -> stream bytes and check SHA-256
    -> atomically publish the complete file
"""

import argparse
import hashlib
import json
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download(manifest, split, base_url):
    """Publish only bytes that match the official pinned size and SHA-256."""
    entry = manifest["splits"][split]
    data_root = ROOT / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    target = data_root / entry["filename"]
    if target.exists():
        if target.stat().st_size != entry["bytes"] or digest_file(target) != entry["sha256"]:
            raise ValueError(f"Existing file does not match the manifest: {target}")
        print(f"The pinned dataset is already present: {target}", flush=True)
        return

    parsed_url = urllib.parse.urlsplit(base_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.username:
        raise ValueError("The base URL must be an HTTPS origin without credentials")
    if parsed_url.path not in ("", "/") or parsed_url.query or parsed_url.fragment:
        raise ValueError("The base URL must not contain a path, query, or fragment")
    url = (
        f"{base_url.rstrip('/')}/datasets/{manifest['repository']}/resolve/"
        f"{manifest['revision']}/{entry['filename']}"
    )
    print(f"Download {entry['bytes']:,} bytes from {url}", flush=True)
    digest = hashlib.sha256()
    total = 0
    next_progress = 16 * 1024 * 1024
    temporary_path = None
    try:
        # Keep the temporary file on the target volume for an atomic final rename.
        with tempfile.NamedTemporaryFile(dir=data_root, suffix=".download", delete=False) as output:
            temporary_path = Path(output.name)
            with urllib.request.urlopen(url, timeout=30) as response:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > entry["bytes"]:
                        raise ValueError("The download exceeds the pinned dataset size")
                    digest.update(chunk)
                    output.write(chunk)
                    if total >= next_progress:
                        print(f"Downloaded {total:,}/{entry['bytes']:,} bytes", flush=True)
                        next_progress += 16 * 1024 * 1024
        if total != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
            raise ValueError("The downloaded dataset does not match the pinned size and SHA-256")
        temporary_path.replace(target)
        provenance = {
            "repository": manifest["repository"],
            "revision": manifest["revision"],
            "split": split,
            "source_url": url,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "bytes": total,
            "sha256": digest.hexdigest(),
        }
        target.with_suffix(".provenance.json").write_text(
            json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
        )
        print(f"The SHA-256 matches. Saved {target}", flush=True)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def main():
    """Read explicit download settings and keep all dataset files under data/."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["s", "oracle"], default="s")
    parser.add_argument(
        "--base-url", default="https://huggingface.co",
        help="Explicit HTTPS origin. A mirror must serve the same pinned bytes.",
    )
    arguments = parser.parse_args()
    manifest = json.loads((ROOT / "datasets.json").read_text(encoding="utf-8"))
    download(manifest, arguments.split, arguments.base_url)


if __name__ == "__main__":
    main()
