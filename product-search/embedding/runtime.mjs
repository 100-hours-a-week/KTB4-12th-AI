import {AutoTokenizer, env} from '@huggingface/transformers';
import * as ort from 'onnxruntime-node';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

export const ROOT = path.dirname(fileURLToPath(import.meta.url));
export const MODEL = 'jinaai/jina-embeddings-v5-text-nano-retrieval';
export const MAX_LENGTH = 1024;
export async function createEncoder() {
  env.allowRemoteModels = false;
  const tokenizer = await AutoTokenizer.from_pretrained(path.join(ROOT, 'models'), {local_files_only:true});
  const session = await ort.InferenceSession.create(path.join(ROOT,'models/model_q4f16.onnx'), {
    executionProviders:['cpu'], graphOptimizationLevel:'all', intraOpNumThreads:2, interOpNumThreads:1,
  });
  return {
    tokenCount(text) { return tokenizer.encode(text).length; },
    async encode(texts, prefix='Query: ') {
      const input = tokenizer(texts.map(t=>prefix+t), {padding:true, truncation:true, max_length:MAX_LENGTH});
      const feeds = Object.fromEntries(session.inputNames.map(name=>[name,new ort.Tensor('int64',input[name].data,input[name].dims)]));
      const out = (await session.run(feeds)).sentence_embedding;
      if (!out || out.dims[1]!==768) throw Error('invalid_embedding_output');
      return texts.map((_,i)=>Array.from(out.data.subarray(i*768,(i+1)*768)));
    }
  };
}
