// One batch process used by the sync CLI. Query serving keeps its own encoder.
import {readFile,writeFile} from 'node:fs/promises';
import {createEncoder} from './runtime.mjs';
const [input,output]=process.argv.slice(2);
const documents=JSON.parse(await readFile(input,'utf8'));
const encoder=await createEncoder(), counts=[];
const vectors=new Float32Array(documents.length*768);
for(let i=0;i<documents.length;i++) {
  const text=documents[i];
  if(typeof text!=='string')throw Error('invalid_document');
  const [vector]=await encoder.encode([text],'Document: ');
  vectors.set(vector,i*768);
  counts.push(encoder.tokenCount('Document: '+text));
}
await writeFile(output,Buffer.from(vectors.buffer));
await writeFile(output+'.json',JSON.stringify(counts));
