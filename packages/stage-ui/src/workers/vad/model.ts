import wasmModuleUrl from 'virtual:airi-transformers/ort-wasm-simd-threaded.jsep.mjs?url'
import wasmUrl from 'virtual:airi-transformers/ort-wasm-simd-threaded.jsep.wasm?url'

import { AutoModel, env, PretrainedConfig } from '@huggingface/transformers'

import modelUrl from '../../assets/vad/silero-vad.onnx?url'

/** Loads the bundled VAD and its matching runtime without a model-hub download. */
export function loadVadModel() {
  // Local model assets are supported policy. Other model loaders retain their remote settings.
  env.allowLocalModels = true
  if (env.backends.onnx.wasm) {
    env.backends.onnx.wasm.wasmPaths = { mjs: wasmModuleUrl, wasm: wasmUrl }
  }
  const separator = modelUrl.lastIndexOf('/')
  return AutoModel.from_pretrained(modelUrl.slice(0, separator), {
    config: new PretrainedConfig({ model_type: 'custom' }),
    model_file_name: modelUrl.slice(separator + 1, -'.onnx'.length),
    subfolder: '',
    local_files_only: true,
    dtype: 'fp32',
  })
}
