const items=document.querySelector('#items'),controls=document.querySelector('#controls');
const el=(tag,text)=>{const node=document.createElement(tag);node.textContent=text;return node;};
let cursor=null,status='unresolved';
const state=document.createElement('select');for(const value of ['unresolved','all','pending','reviewed','triaged','resolved']){const o=el('option',value);o.value=value;state.append(o);}state.onchange=()=>{status=state.value;cursor=null;load();};
const ident=document.createElement('input');ident.placeholder='신고 UUID';const find=el('button','ID로 조회');find.onclick=()=>load(ident.value.trim());controls.append(state,ident,find);
async function load(id=null){
 const query=new URLSearchParams({limit:'50',status});if(cursor)query.set('cursor',cursor);
 const response=await fetch(id?'/admin/api/reports/'+encodeURIComponent(id):'/admin/api/reports?'+query);
 if(!response.ok){items.replaceChildren(el('p',response.status===401?'QA 관리자 로그인이 필요합니다.':'신고를 조회하지 못했습니다.'));return;}
 const data=await response.json();items.replaceChildren();
 for(const report of id?[data]:data.reports){
  const section=el('section','');section.append(el('h2',(report.payload.category||'QA')+' · '+report.status),el('p',report.payload.note||''),el('p',report.id+' · '+report.created+' · '+report.scope));
  for(const turn of report.context.turns||[]){const detail=el('details','');detail.append(el('summary',turn.message||'연결된 턴'));for(const block of turn.blocks||[])if(block.type==='text')detail.append(el('p',block.text));detail.append(el('pre',JSON.stringify(turn.metrics,null,2)));section.append(detail);}
  const button=el('button','원인 분류 기록');button.onclick=async()=>{const note=prompt('확인한 증상·다음 검증을 기록하세요. 열람만으로 해결 처리하지 않습니다.');if(!note||note.trim().length<3)return;const r=await fetch('/admin/api/reports/'+report.id+'/review',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewId:crypto.randomUUID(),status:'triaged',note:note.trim()})});if(r.ok)load(report.id);};section.append(button,el('p','해결 처리는 합성 재현·통과한 회귀 검증·현재 배포 revision 근거가 필요합니다. 운영 도구의 resolve 명령을 사용합니다.'));items.append(section);
 }
 if(!id&&data.nextCursor){const next=el('button','다음 페이지');next.onclick=()=>{cursor=data.nextCursor;load();};items.append(next);}
 if(!id&&!data.reports.length)items.append(el('p','해당 상태의 신고가 없습니다.'));
}load();
