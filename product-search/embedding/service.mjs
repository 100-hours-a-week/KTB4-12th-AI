import readline from 'node:readline';
import {createEncoder} from './runtime.mjs';
const encoder = await createEncoder();
await encoder.encode(['상품 검색']);
console.log(JSON.stringify({ready:true,dimensions:768}));
for await (const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})) {
  try {
    const {text}=JSON.parse(line);
    if(typeof text!=='string'||text.length>2000) throw Error('invalid_query');
    const [vector]=await encoder.encode([text]);
    console.log(JSON.stringify({vector}));
  } catch (e) {console.log(JSON.stringify({error:String(e)}));}
}
