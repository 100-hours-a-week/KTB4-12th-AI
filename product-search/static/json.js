// Preserve JSON integers outside JavaScript's safe Number range as BigInt.
// Ordinary JSON numbers and the API's small product IDs remain Numbers.
export function parseJSON(text) {
  let at=0;
  const fail=()=>{throw new SyntaxError(`잘못된 JSON입니다 (위치 ${at}).`);};
  const space=()=>{while(/[\t\n\r ]/.test(text[at]??'') && at<text.length)at++;};
  function string() {
    const start=at++;
    while(at<text.length){
      const ch=text[at++];
      if(ch==='"')return JSON.parse(text.slice(start,at));
      if(ch==='\\')at++;
    }
    fail();
  }
  function value() {
    space();const ch=text[at];
    if(ch==='"')return string();
    if(ch==='['){
      at++;space();const result=[];
      if(text[at]===']'){at++;return result;}
      while(true){result.push(value());space();if(text[at]===']'){at++;return result;}if(text[at++]!==',')fail();}
    }
    if(ch==='{'){
      at++;space();const result={};
      if(text[at]==='}'){at++;return result;}
      while(true){
        space();if(text[at]!=='"')fail();const key=string();space();if(text[at++]!==':')fail();
        Object.defineProperty(result,key,{value:value(),writable:true,enumerable:true,configurable:true});
        space();if(text[at]==='}'){at++;return result;}if(text[at++]!==',')fail();
      }
    }
    for(const [token,result] of [['true',true],['false',false],['null',null]]){
      if(text.startsWith(token,at)){at+=token.length;return result;}
    }
    const match=text.slice(at).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);
    if(!match)fail();
    const token=match[0];at+=token.length;
    if(/^-?\d+$/.test(token)){
      const integer=BigInt(token);
      if(integer>BigInt(Number.MAX_SAFE_INTEGER)||integer<BigInt(Number.MIN_SAFE_INTEGER))return integer;
    }
    return Number(token);
  }
  const result=value();space();if(at!==text.length)fail();return result;
}

export function stringifyJSON(value,space=0) {
  const indent=typeof space==='number'?' '.repeat(Math.min(10,Math.max(0,space))):String(space).slice(0,10);
  const seen=new Set();
  function encode(item,depth) {
    if(typeof item==='bigint')return String(item);
    if(item===null||typeof item!=='object')return JSON.stringify(item);
    if(seen.has(item))throw new TypeError('순환 참조는 JSON으로 변환할 수 없습니다.');
    seen.add(item);
    const array=Array.isArray(item),entries=array?Array.from(item,child=>encode(child,depth+1)??'null'):
      Object.keys(item).flatMap(key=>{const encoded=encode(item[key],depth+1);return encoded===undefined?[]:[JSON.stringify(key)+(indent?': ':':')+encoded];});
    seen.delete(item);
    const [open,close]=array?['[',']']:['{','}'];
    return open+(entries.length?(indent?'\n'+indent.repeat(depth+1)+entries.join(',\n'+indent.repeat(depth+1))+'\n'+indent.repeat(depth):entries.join(',')):'')+close;
  }
  return encode(value,0);
}

function parseId(text,label) {
  if(!/^[1-9]\d*$/.test(text))throw new Error(`${label} ID는 1 이상의 정수여야 합니다.`);
  const value=BigInt(text);
  if(value>9223372036854775807n)throw new Error(`${label} ID가 BIGINT 범위를 초과합니다.`);
  return value<=BigInt(Number.MAX_SAFE_INTEGER)?Number(value):value;
}
export const parseProductId=text=>parseId(text,'상품');
export const parseCategoryId=text=>parseId(text,'카테고리');
