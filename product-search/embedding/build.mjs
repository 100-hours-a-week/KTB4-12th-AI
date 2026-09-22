import {readFile,writeFile,mkdir,rename} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {ROOT,MODEL,MAX_LENGTH,createEncoder} from './runtime.mjs';
const dataRoot=path.join(ROOT,'../data');
const bytes=await readFile(path.join(dataRoot,'catalog.json'));
const catalog=JSON.parse(bytes), encoder=await createEncoder();
await mkdir(path.join(dataRoot,'vector-cache'),{recursive:true});
const all=new Float32Array(catalog.products.length*768);
const counts=[],start=performance.now();let computed=0;
for(let i=0;i<catalog.products.length;i++) {
  const p=catalog.products[i], cache=path.join(dataRoot,'vector-cache',p.document_hash+'.f32');
  let vector;
  try {const b=await readFile(cache);if(b.length!==3072)throw Error('size');vector=new Float32Array(b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength));}
  catch {const [v]=await encoder.encode([p.document],'Document: ');vector=new Float32Array(v);await writeFile(cache,Buffer.from(vector.buffer));computed++;}
  all.set(vector,i*768);counts.push(encoder.tokenCount('Document: '+p.document));
  if(i%100===0)console.log(JSON.stringify({done:i,total:catalog.products.length,computed,seconds:Math.round((performance.now()-start)/1000)}));
}
const vectorBytes=Buffer.from(all.buffer),sha=b=>createHash('sha256').update(b).digest('hex');
await writeFile(path.join(dataRoot,'vectors.f32.tmp'),vectorBytes);
await rename(path.join(dataRoot,'vectors.f32.tmp'),path.join(dataRoot,'vectors.f32'));
const manifest={model:MODEL,dimensions:768,precision:'q4f16',max_tokens:MAX_LENGTH,query_prefix:'Query: ',document_prefix:'Document: ',
  model_sha256:sha(await readFile(path.join(ROOT,'models/model_q4f16.onnx_data'))),
  catalog_sha256:sha(bytes),vectors_sha256:sha(vectorBytes),product_ids:catalog.products.map(p=>p.id),
  token_counts:counts,truncated_documents:counts.filter(n=>n>MAX_LENGTH).length,build_seconds:(performance.now()-start)/1000};
await writeFile(path.join(dataRoot,'manifest.json.tmp'),JSON.stringify(manifest));
await rename(path.join(dataRoot,'manifest.json.tmp'),path.join(dataRoot,'manifest.json'));
console.log(JSON.stringify({complete:true,count:counts.length,truncated:manifest.truncated_documents,seconds:manifest.build_seconds}));
