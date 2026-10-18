"""Prepare optional bright variants from the existing original synthetic references.

This changes reference pitch, not model size. Listen before selecting a voice.
It does not create or use a recording of a real person.
"""

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=ROOT.parent / ".tools/modern-voice-2026-10-17")
    parser.add_argument("--output-dir", type=Path, default=ROOT.parent / ".tools/local-voice-references")
    args = parser.parse_args()

    import librosa
    import numpy as np
    import soundfile as sf

    destination = args.output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    results = []
    for source_name, name, semitones in (("vivian-curious.wav", "anime-playful.wav", 4.0),
                                          ("serena-friendly.wav", "anime-soft.wav", 3.0)):
        source = (args.source_dir / source_name).resolve()
        samples, sample_rate = sf.read(source, dtype="float32", always_2d=True)
        seconds = samples.shape[0] / sample_rate
        if not 3 <= seconds <= 10 or not np.isfinite(samples).all():
            raise ValueError("The original synthetic reference must contain 3 to 10 seconds of finite audio")
        shifted = librosa.effects.pitch_shift(samples.mean(axis=1), sr=sample_rate, n_steps=semitones)
        peak = float(np.max(np.abs(shifted)))
        if peak < 1e-6:
            raise ValueError("The reference contains no audible signal")
        shifted *= min(1.0, 0.9 / peak)
        target = destination / name
        if target.exists():
            raise FileExistsError(f"A voice reference already exists. Use a new output directory: {target}")
        sf.write(target, shifted, sample_rate, subtype="PCM_16")
        results.append({"source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "output": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                        "pitch_shift_semitones": semitones, "sample_rate": sample_rate, "seconds": round(seconds, 3)})
    (destination / "synthetic-reference-manifest.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"prepared": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
