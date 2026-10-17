# Silero VAD

This directory contains the full-precision Silero VAD model used before local speech transcription.
Vite bundles this model and the installed Transformers.js WASM runtime.
First use does not download a model or runtime from the internet.

- Source: [onnx-community/silero-vad](https://huggingface.co/onnx-community/silero-vad/tree/e71cae966052b992a7eca6b17738916ce0eca4ec).
- Revision: `e71cae966052b992a7eca6b17738916ce0eca4ec`.
- Upstream file: `onnx/model.onnx`.
- Local file: `silero-vad.onnx`.
- Size: `2243022` bytes.
- SHA256: `a4a068cd6cf1ea8355b84327595838ca748ec29a25bc91fc82e6c299ccdc5808`.
- License: MIT. The upstream license is in `LICENSE`.

The runtime files come from the same installed `@huggingface/transformers` distribution as its JavaScript loader.
They are build outputs and are not copied into this directory.
