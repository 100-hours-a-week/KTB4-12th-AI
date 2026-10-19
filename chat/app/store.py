"""PostgreSQL state and event transactions; no in-memory source of truth."""
import os
import base64
from datetime import datetime
import copy
import json
import time
import math
from decimal import Decimal
from psycopg.rows import dict_row
from psycopg.errors import InsufficientPrivilege, UndefinedColumn, UndefinedTable
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from app.errors import APIError

TERMINAL = {'COMPLETED', 'CANCELLED', 'FAILED'}


class Store:
 def __init__(self, url):
  self.pool=ConnectionPool(url,min_size=1,max_size=6,kwargs={'row_factory':dict_row,'connect_timeout':8})
  try:
   self.pool.wait()
   with self.pool.connection() as db:
    row=db.execute("SELECT version FROM schema_version WHERE singleton").fetchone()
    if not row or row['version']!='single-server/1':raise RuntimeError('Chat migration required')
    self.profile_lookup=bool(db.execute("SELECT to_regprocedure('ai_profile.chat_recipient_snapshot(uuid)') AS lookup").fetchone()['lookup'])
    # The review table is an additive /1 migration; validate before admitting work.
    try:db.execute('SELECT id,turn,note,created FROM turn_reviews LIMIT 0')
    except (UndefinedTable,UndefinedColumn,InsufficientPrivilege) as exc:
     raise RuntimeError('Chat review migration required') from exc
  except BaseException:
   self.pool.close()
   raise
 def close(self): self.pool.close()
 def health(self):
  with self.pool.connection() as db: db.execute('SELECT 1')
  return {'database':'postgresql'}
 def pause_admissions(self,paused):
  with self.pool.connection() as db:
   db.execute('SELECT paused FROM admission_control WHERE id=1 FOR UPDATE').fetchone()
   # Close admission first. Existing turns retain their lease and finish; waiting
   # for zero work before closing this gate races with every new admission.
   db.execute('UPDATE admission_control SET paused=%s WHERE id=1',(paused,))
  return {'paused':paused}
 def budget_import(self,account,committed,source_hash):
  # Operator-only migration; deliberately absent from authenticated RPC methods.
  amount=Decimal(str(committed))
  if not amount.is_finite() or not 0<=amount<=5 or len(source_hash)!=64:raise ValueError('Invalid ledger import')
  with self.pool.connection() as db:
   db.execute('INSERT INTO task_budget_accounts(id,limit_usd,imported_usd,import_hash) VALUES(%s,5,%s,%s) ON CONFLICT DO NOTHING',(account,amount,source_hash))
   row=db.execute('SELECT * FROM task_budget_accounts WHERE id=%s FOR UPDATE',(account,)).fetchone()
   if row['imported_usd']!=amount or row['import_hash']!=source_hash:raise ValueError('Existing ledger migration differs; no reset allowed')
  return self.budget_summary(account)
 def budget_summary(self,account):
  with self.pool.connection() as db:
   row=db.execute('SELECT * FROM task_budget_accounts WHERE id=%s',(account,)).fetchone()
   if row is None:raise APIError(503,'유료 테스트 예산 이관을 확인하고 있습니다.')
   extra=db.execute('SELECT coalesce(sum(coalesce(actual_usd,reserved_usd)),0) charged,count(*) attempts FROM task_budget_attempts WHERE account=%s',(account,)).fetchone()
   charged=row['imported_usd']+extra['charged']
   return {'limitUsd':float(row['limit_usd']),'committedUsd':float(charged),'remainingUsd':float(max(Decimal(0),row['limit_usd']-charged)),'attempts':extra['attempts'],'halted':row['halted']}
 def budget_reserve(self,account,ident,amount,model):
  value=Decimal(str(amount))
  if not value.is_finite() or not 0<value<=5:raise APIError(429,'이 요청은 남은 테스트 예산을 초과합니다.')
  with self.pool.connection() as db:
   row=db.execute('SELECT * FROM task_budget_accounts WHERE id=%s FOR UPDATE',(account,)).fetchone()
   if row is None:raise APIError(503,'유료 테스트 예산 이관을 확인하고 있습니다.')
   old=db.execute('SELECT * FROM task_budget_attempts WHERE id=%s',(ident,)).fetchone()
   if old:
    if old['account']!=account or old['reserved_usd']!=value or old['model']!=model:raise APIError(409,'예산 예약 요청이 다릅니다.')
    return True
   extra=db.execute('SELECT coalesce(sum(coalesce(actual_usd,reserved_usd)),0) charged FROM task_budget_attempts WHERE account=%s',(account,)).fetchone()['charged']
   if row['halted'] or row['imported_usd']+extra+value>row['limit_usd']:raise APIError(429,'누적 유료 테스트 예산이 부족합니다. 무료 연동 테스트를 이용해 주세요.')
   db.execute('INSERT INTO task_budget_attempts(id,account,reserved_usd,model) VALUES(%s,%s,%s,%s)',(ident,account,value,model))
  return True
 def budget_finish(self,account,ident,cost,status):
  value=Decimal(str(cost)) if type(cost) in (int,float) and math.isfinite(cost) and cost>=0 else None
  with self.pool.connection() as db:
   row=db.execute('SELECT * FROM task_budget_accounts WHERE id=%s FOR UPDATE',(account,)).fetchone()
   attempt=db.execute('SELECT * FROM task_budget_attempts WHERE id=%s AND account=%s FOR UPDATE',(ident,account)).fetchone()
   if row is None or attempt is None:raise APIError(404,'예산 예약이 없습니다.')
   if attempt['finished'] is not None:return True
   db.execute('UPDATE task_budget_attempts SET actual_usd=%s,status=%s,finished=now() WHERE id=%s',(value,'received' if status=='received' else 'unknown',ident))
   if value is not None and value>attempt['reserved_usd']:db.execute("UPDATE task_budget_accounts SET halted='cost_exceeded_reservation' WHERE id=%s",(account,))
  return True
 def lease(self,owner,release=False,recover=False,next_owner=None,claim=False,inspect=False):
  # Cancelling asyncio.to_thread does not stop its DB call. Bound the synchronous
  # release itself so an unavailable pool cannot consume the shutdown grace.
  connection=self.pool.connection(timeout=2) if release else self.pool.connection()
  with connection as db:
   if release:
    db.execute("SET LOCAL statement_timeout = '1s'")
    db.execute("SET LOCAL lock_timeout = '1s'")
   db.execute('SELECT pg_advisory_xact_lock(908427)')
   r=db.execute("SELECT owner, expires>now() AS active FROM runtime_lease WHERE id=1 FOR UPDATE").fetchone()
   if inspect:return {'owner':r['owner'] if r else None,'active':bool(r and r['active'])}
   if release:
    if next_owner is not None:
     # Retain a fresh tombstone so a delayed initial claim cannot resurrect us.
     from uuid import uuid4
     db.execute('UPDATE runtime_lease SET owner=%s,expires=now() WHERE id=1 AND owner=%s',('released:'+str(uuid4()),owner))
    else:db.execute('DELETE FROM runtime_lease WHERE id=1 AND owner=%s',(owner,))
    return True
   if next_owner is not None:
    # New callers always use exact CAS, including renewal (owner == next_owner).
    # Repeating a committed rotation renews it without sweeping new turns again.
    if r and r['owner']==next_owner:
     db.execute("UPDATE runtime_lease SET expires=now()+interval '40 seconds' WHERE id=1")
     return True
    if (r['owner'] if r else None)!=owner:return False
    if claim and r and r['active']:return False
    target=next_owner;fresh=True
   else:
    # Legacy tooling compatibility; production heartbeat uses explicit CAS above.
    if r and r['owner']!=owner and r['active']:return False
    target=owner;fresh=not r or r['owner']!=owner
   db.execute("INSERT INTO runtime_lease VALUES(1,%s,now()+interval '40 seconds') ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,expires=excluded.expires",(target,))
   if fresh or recover:
    for t in db.execute("SELECT id,state FROM turns WHERE state->>'status'='PROCESSING' FOR UPDATE").fetchall():
     state=t['state'];state.update(status='FAILED',error={'message':'서버가 재시작되어 답변이 중단됐어요. 다시 질문해 주세요.'});state.pop('progressMessage',None)
     self._event(db,str(t['id']),state,'turn.finished',None)
   return True
 def _guard(self,db,owner):
  r=db.execute('SELECT 1 FROM runtime_lease WHERE id=1 AND owner=%s AND expires>now() FOR SHARE',(owner,)).fetchone()
  if not r: raise APIError(503,'채팅 실행 서버가 준비되지 않았습니다.')
 def _conversation(self,db,cid,lock=False):
  r=db.execute('SELECT * FROM conversations WHERE id=%s'+(' FOR UPDATE' if lock else ''),(cid,)).fetchone()
  if not r:raise APIError(404,'대화를 찾을 수 없습니다.')
  return r
 def _turn(self,db,cid,tid,lock=False):
  r=db.execute('SELECT * FROM turns WHERE id=%s AND conversation=%s'+(' FOR UPDATE' if lock else ''),(tid,cid)).fetchone()
  if not r:raise APIError(404,'질문을 찾을 수 없습니다.')
  return r
 def _snapshot(self,db,cid):
  return {'conversationId':cid,'turns':[r['state'] for r in db.execute('SELECT state FROM turns WHERE conversation=%s ORDER BY ordinal',(cid,))]}
 def conversation(self,cid,sender,recipient,demo_profile=None,protocol_fixture=False):
  with self.pool.connection() as db:
   made=db.execute('INSERT INTO conversations(id,sender,recipient,demo_profile,protocol_fixture) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id',(cid,sender,recipient,Jsonb(demo_profile) if demo_profile is not None else None,protocol_fixture)).fetchone()
   r=self._conversation(db,cid)
   if r['protocol_fixture']!=protocol_fixture:raise APIError(409,'대화의 실행 모드는 변경할 수 없습니다.')
   if r['sender']!=sender or r['recipient']!=recipient:raise APIError(409,'같은 대화 ID에는 같은 사용자와 수신자를 사용해야 합니다.')
   if (r['demo_profile'] or {}).get('key')!=(demo_profile or {}).get('key'):raise APIError(409,'수신자를 바꾸려면 새 대화를 시작해 주세요.')
   return [self._snapshot(db,cid),bool(made)]
 def binding(self,cid):
  with self.pool.connection() as db:
   r=self._conversation(db,cid)
   return {"senderUserId":r["sender"],"recipientUserId":r["recipient"],"demoProfile":r["demo_profile"],"protocolFixture":r["protocol_fixture"],"teamSession":bool(db.execute("SELECT 1 FROM sessions WHERE sender=%s",(r["sender"],)).fetchone())}
 def read(self,cid):
  with self.pool.connection() as db:
   self._conversation(db,cid);return self._snapshot(db,cid)
 def existing_turn(self,cid,tid,message,model=None,recipient_context=None,prepared_context=None):
  """Read immutable request identity without requiring a live generation lease."""
  with self.pool.connection() as db:
   binding=self._conversation(db,cid)
   if prepared_context is not None:
    from app.models import PreparedContext
    prepared_context=PreparedContext.model_validate(prepared_context).model_dump(by_alias=True)
   if recipient_context is not None:
    from app.models import RecipientContext
    recipient_context=RecipientContext.model_validate(recipient_context).model_dump(by_alias=True)
    if recipient_context['recipientUserId']!=binding['recipient']:raise APIError(409,'대화에 연결된 수신자와 취향의 수신자가 다릅니다.')
   old=db.execute('SELECT * FROM turns WHERE id=%s',(tid,)).fetchone()
   if old is None:return None
   if str(old['conversation'])!=cid or old['state']['message']!=message or old['requested_model']!=model or old['recipient_context']!=recipient_context or old['prepared_context']!=prepared_context:raise APIError(409,'같은 질문 ID에는 같은 질문, 모델, 수신자 정보를 사용해야 합니다.')
   return old['state']
 def start(self,cid,tid,message,owner,model=None,recipient_context=None,prepared_context=None):
  with self.pool.connection() as db:
   self._guard(db,owner);binding=self._conversation(db,cid,True)
   if prepared_context is not None:
    from app.models import PreparedContext
    prepared_context=PreparedContext.model_validate(prepared_context).model_dump(by_alias=True)
   if recipient_context is not None:
    from app.models import RecipientContext
    recipient_context=RecipientContext.model_validate(recipient_context).model_dump(by_alias=True)
    if recipient_context['recipientUserId']!=binding['recipient']:raise APIError(409,'대화에 연결된 수신자와 취향의 수신자가 다릅니다.')
   db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(tid,))
   old=db.execute('SELECT * FROM turns WHERE id=%s',(tid,)).fetchone()
   if old:
    if str(old['conversation'])!=cid or old['state']['message']!=message or old['requested_model']!=model or old['recipient_context']!=recipient_context or old['prepared_context']!=prepared_context:raise APIError(409,'같은 질문 ID에는 같은 질문, 모델, 수신자 정보를 사용해야 합니다.')
    return [old['state'],False]
   if db.execute("SELECT 1 FROM turns WHERE conversation=%s AND state->>'status'='PROCESSING'",(cid,)).fetchone():raise APIError(409,'이전 답변이 끝난 뒤 질문해 주세요.')
   # Serialize global admission across distinct conversations.
   db.execute('SELECT pg_advisory_xact_lock(908428)')
   n=db.execute("SELECT count(*) AS n FROM turns WHERE state->>'status'='PROCESSING'").fetchone()['n']
   if db.execute("SELECT count(*) AS n FROM turns WHERE created>now()-interval '24 hours'").fetchone()['n']>=int(os.getenv('CHAT_DAILY_LIMIT','200')):raise APIError(429,'체험 서버의 일일 사용량을 모두 사용했습니다.')
   if n>=int(os.getenv('CHAT_MAX_ACTIVE','4')):raise APIError(429,'현재 요청이 많습니다. 잠시 후 다시 시도해 주세요.')
   if db.execute('SELECT paused FROM admission_control WHERE id=1 FOR SHARE').fetchone()['paused']:raise APIError(503,'배포를 위해 새 질문 접수를 잠시 중지했습니다. 같은 ID로 다시 시도해 주세요.')
   state={'turnId':tid,'status':'PROCESSING','message':message,'blocks':[],'lastEventId':0}
   context={'tracePersistence':{'version':1,'status':'pending'}}
   if self.profile_lookup and recipient_context is None:
    # Freeze the authorized recipient snapshot with admission; retries reuse it.
    context['recipientProfile']=db.execute('SELECT ai_profile.chat_recipient_snapshot(%s) AS snapshot',(cid,)).fetchone()['snapshot']
   db.execute('INSERT INTO turns(id,conversation,state,requested_model,recipient_context,prepared_context,context) VALUES(%s,%s,%s,%s,%s,%s,%s)',(tid,cid,Jsonb(state),model,Jsonb(recipient_context) if recipient_context is not None else None,Jsonb(prepared_context) if prepared_context is not None else None,Jsonb(context)))
   db.execute('UPDATE conversations SET touched=now() WHERE id=%s',(cid,))
   return [state,True]
 def recipient_context(self,cid,tid):
  with self.pool.connection() as db:return self._turn(db,cid,tid)['recipient_context']
 def _event(self,db,tid,state,kind,payload):
  if kind=='turn.finished':
   # Commit missing-trace evidence with the public terminal event. A lost
   # context-write acknowledgement must not overwrite an actually saved trace.
   db.execute("""UPDATE turns SET context=jsonb_set(context,'{tracePersistence}',
    '{"version":1,"status":"not_recorded"}'::jsonb)
    WHERE id=%s AND context->'tracePersistence'->>'status'='pending'""",(tid,))
  state['lastEventId']+=1
  db.execute('UPDATE turns SET state=%s WHERE id=%s',(Jsonb(state),tid))
  db.execute('INSERT INTO events VALUES(%s,%s,%s,%s)',(tid,state['lastEventId'],kind,Jsonb(state if kind=='turn.finished' else payload)))
 def emit(self,cid,tid,kind,payload,owner):
  with self.pool.connection() as db:
   self._guard(db,owner);r=self._turn(db,cid,tid,True);s=r['state']
   if s['status']!='PROCESSING':return False
   if kind=='progress':s['progressMessage']=payload['message']
   elif kind=='answer.delta':
    s.pop('progressMessage',None)
    if s['blocks'] and s['blocks'][-1]['type']=='text':s['blocks'][-1]['text']+=payload['delta']
    else:s['blocks'].append({'type':'text','text':payload['delta']})
   elif kind=='cards':
    if any(b['type']=='cards' for b in s['blocks']):raise APIError(409,'추천 카드는 한 번만 저장할 수 있습니다.')
    from app.card_contract import cards_payload
    payload=cards_payload(payload)
    s['blocks'].append({'type':'cards',**payload})
   elif kind=='turn.finished':
    if payload['status'] not in TERMINAL:raise APIError(400,'잘못된 종료 상태입니다.')
    s['status']=payload['status'];s.pop('progressMessage',None)
    if s['status']=='FAILED':s['error']={'message':payload['error']['message']}
   else:raise APIError(400,'지원하지 않는 이벤트입니다.')
   self._event(db,tid,s,kind,payload);return True
 def replay(self,cid,tid,after):
  with self.pool.connection() as db:
   # Lock state while reading its corresponding event prefix.
   r=self._turn(db,cid,tid,True);s=r['state']
   if after<0 or after>s['lastEventId']:raise APIError(409,'대화를 복구한 뒤 이벤트 번호를 다시 확인해 주세요.')
   rows=db.execute('SELECT seq,kind,payload FROM events WHERE turn=%s AND seq>%s ORDER BY seq',(tid,after)).fetchall()
   return [[{'id':r['seq'],'event':r['kind'],'data':r['payload']} for r in rows],s]
 def context(self,cid,tid,value=None):
  with self.pool.connection() as db:
   r=self._turn(db,cid,tid)
   if value is not None:
    value=dict(value)
    if r['context'].get('recipientProfile',{}).get('source')=='AI_PROFILE_STORE':
     value['recipientProfile']=r['context']['recipientProfile']
    db.execute('UPDATE turns SET context=%s WHERE id=%s',(Jsonb(value),tid));return True
   return r['context']
 def session(self,sid,sender=None):
  with self.pool.connection() as db:
   if sender is not None:db.execute('INSERT INTO sessions(id,sender) VALUES(%s,%s) ON CONFLICT DO NOTHING',(sid,sender))
   r=db.execute('SELECT sender FROM sessions WHERE id=%s',(sid,)).fetchone()
   if not r:raise APIError(401,'체험 세션이 만료됐습니다.')
   return r['sender']
 def owned(self,cid,sender):
  with self.pool.connection() as db:
   r=self._conversation(db,cid)
   if r['sender']!=sender:raise APIError(404,'대화를 찾을 수 없습니다.')
   return True
 def list_conversations(self,sender):
  with self.pool.connection() as db:
   rows=db.execute('SELECT c.id,(SELECT state->>\'message\' FROM turns t WHERE t.conversation=c.id ORDER BY ordinal LIMIT 1) AS title FROM conversations c WHERE sender=%s AND EXISTS (SELECT 1 FROM turns t WHERE t.conversation=c.id) ORDER BY touched DESC LIMIT 50',(sender,)).fetchall()
   return [{'id':str(r['id']),'title':r['title'] or '새 대화'} for r in rows]
 def feedback(self,cid,tid,fid,payload):
  with self.pool.connection() as db:
   self._turn(db,cid,tid)
   db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(fid,))
   if db.execute('SELECT 1 FROM conversation_reports WHERE id=%s',(fid,)).fetchone():raise APIError(409,'다른 의견에는 새 제출 ID를 사용해 주세요.')
   old=db.execute('SELECT turn,payload FROM feedback WHERE id=%s',(fid,)).fetchone()
   if old:
    if str(old['turn'])!=tid or old['payload']!=payload:raise APIError(409,'다른 의견에는 새 제출 ID를 사용해 주세요.')
   else:
    # New issue reports need a description; unchanged legacy retries stay valid.
    note=payload.get('note')
    if payload.get('verdict') in ('unhelpful','issue') and (not isinstance(note,str) or not 3<=len(note.strip())<=1500):raise APIError(400,'문제와 기대한 동작을 3~1500자로 적어 주세요.')
    from app.turn_report import report
    target=self._turn(db,cid,tid)
    if target['state']['status']=='PROCESSING':raise APIError(409,'답변이 끝난 뒤 의견을 보내 주세요.')
    rows=db.execute('SELECT state,context FROM turns WHERE conversation=%s AND ordinal<=%s ORDER BY ordinal DESC LIMIT 5',(cid,target['ordinal'])).fetchall()
    linked={'schema':'chat-feedback-context-v1','conversationId':cid,'turnId':tid,'requestId':tid,
            'precedingTurnIds':[r['state']['turnId'] for r in reversed(rows[1:])],
            'turns':[{k:v for k,v in report(cid,r['state'],r['context']).items() if k not in ('message','blocks','products')} for r in reversed(rows)]}
    db.execute('INSERT INTO feedback(id,turn,payload,context) VALUES(%s,%s,%s,%s)',(fid,tid,Jsonb(payload),Jsonb(linked)))
   return {'id':fid,'saved':True}
 def conversation_feedback(self,cid,fid,payload):
  with self.pool.connection() as db:
   self._conversation(db,cid)
   db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(fid,))
   if db.execute('SELECT 1 FROM feedback WHERE id=%s',(fid,)).fetchone():raise APIError(409,'다른 의견에는 새 제출 ID를 사용해 주세요.')
   old=db.execute('SELECT conversation,payload FROM conversation_reports WHERE id=%s',(fid,)).fetchone()
   if old:
    if str(old['conversation'])!=cid or old['payload']!=payload:raise APIError(409,'다른 의견에는 새 제출 ID를 사용해 주세요.')
   else:
    from app.turn_report import report
    rows=db.execute('SELECT state,context FROM turns WHERE conversation=%s ORDER BY ordinal DESC LIMIT 5',(cid,)).fetchall()
    if any(r['state']['status']=='PROCESSING' for r in rows):raise APIError(409,'답변이 끝난 뒤 의견을 보내 주세요.')
    context={'schema':'chat-feedback-context-v1','conversationId':cid,'linkStatus':'recent_turns' if rows else 'session_only',
             'turns':[{k:v for k,v in report(cid,r['state'],r['context']).items() if k not in ('message','blocks','products')} for r in reversed(rows)]}
    db.execute('INSERT INTO conversation_reports(id,conversation,payload,context) VALUES(%s,%s,%s,%s)',(fid,cid,Jsonb(payload),Jsonb(context)))
   return {'id':fid,'saved':True}
 def _report_query(self):
  return """SELECT f.id,f.payload,f.context,f.reviewed,f.created,t.conversation,c.sender,'turn' scope,
   COALESCE(f.workflow_status,CASE WHEN f.reviewed THEN 'reviewed' ELSE 'pending' END) status,
   COALESCE(f.workflow_updated,f.created) updated FROM feedback f JOIN turns t ON t.id=f.turn JOIN conversations c ON c.id=t.conversation
   UNION ALL SELECT f.id,f.payload,f.context,f.reviewed,f.created,f.conversation,c.sender,'conversation' scope,
   COALESCE(f.workflow_status,CASE WHEN f.reviewed THEN 'reviewed' ELSE 'pending' END),
   COALESCE(f.workflow_updated,f.created) FROM conversation_reports f JOIN conversations c ON c.id=f.conversation"""
 def _report_page(self,db,limit,cursor,status,direction,sender=None,fid=None,metadata=False):
  if not 1<=limit<=100 or status not in ('all','unresolved','pending','reviewed','triaged','resolved'):raise APIError(400,'잘못된 신고 조회 조건입니다.')
  clauses=[];args=[]
  if sender is not None:clauses.append('sender=%s');args.append(sender)
  if fid is not None:clauses.append('id=%s');args.append(fid)
  if status=='unresolved':clauses.append("status!='resolved'")
  elif status!='all':clauses.append('status=%s');args.append(status)
  if cursor:
   try:
    from uuid import UUID
    d,st,stamp,ident=json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    stamp=datetime.fromisoformat(stamp);ident=str(UUID(ident))
    if d!=direction or st!=status or stamp.tzinfo is None:raise ValueError()
   except Exception:raise APIError(400,'잘못된 신고 페이지 커서입니다.')
   clauses.append('(updated,id)'+('>' if direction=='ASC' else '<')+'(%s,%s::uuid)');args.extend([stamp,ident])
  columns="id,payload->>'category' category,status,created,updated" if metadata else '*'
  rows=db.execute('SELECT '+columns+' FROM ('+self._report_query()+') r'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY updated '+direction+',id '+direction+' LIMIT %s',args+[limit+1]).fetchall()
  more=len(rows)>limit;rows=rows[:limit]
  next_cursor=base64.urlsafe_b64encode(json.dumps([direction,status,rows[-1]['updated'].isoformat(),str(rows[-1]['id'])]).encode()).decode() if more else None
  return rows,next_cursor
 def report_items(self,sender=None,fid=None,reviewed=None,limit=50,cursor=None,status='all'):
  with self.pool.connection() as db:
   if reviewed is not None:
    changed=db.execute('UPDATE conversation_reports SET reviewed=%s,workflow_updated=now() WHERE id=%s RETURNING id',(reviewed,fid)).fetchone()
    if not changed:raise APIError(404,'의견을 찾을 수 없습니다.')
    return {'id':fid,'reviewed':reviewed,'resolved':False}
   rows,next_cursor=self._report_page(db,limit,cursor,status,'DESC',sender,fid)
   if fid is not None:
    if not rows:raise APIError(404,'의견을 찾을 수 없습니다.')
    if len(rows)!=1:raise APIError(409,'중복된 신고 ID를 확인해 주세요.')
   # Hydrate visible dialogue only after ownership/filter checks.
   for r in rows:
    for ref in r['context'].get('turns',[]):
     t=db.execute('SELECT state FROM turns WHERE id=%s AND conversation=%s',(ref['turnId'],r['conversation'])).fetchone()
     if t:ref['message']=t['state']['message'];ref['blocks']=t['state']['blocks']
   result=[{'id':str(r['id']),'conversationId':str(r['conversation']),'scope':r['scope'],'payload':r['payload'],'context':r['context'],'status':r['status'],'created':r['created'].isoformat(),'updated':r['updated'].isoformat()} for r in rows]
   if fid is not None:
    review_rows=db.execute('SELECT * FROM report_reviews WHERE report_id=%s ORDER BY created,id',(fid,)).fetchall()
    result[0]['reviews']=[{'reviewId':str(r['id']),'status':r['status'],'created':r['created'].isoformat(),**({'note':r['note'],'evidence':r['evidence']} if sender is None else {})} for r in review_rows]
    return result[0]
   return {'reports':result,'nextCursor':next_cursor}
 def report_summary(self,limit=50,cursor=None,status='unresolved'):
  allowed={'other','quality','constraints','latency','error'}
  with self.pool.connection() as db:
   rows,next_cursor=self._report_page(db,limit,cursor,status,'ASC',metadata=True)
   counts={r['status']:r['n'] for r in db.execute('SELECT status,count(*) n FROM ('+self._report_query()+') r GROUP BY status').fetchall()}
  return {'schema':'chat-report-summary-v2','total':sum(counts.values()),'pending':counts.get('pending',0),'unresolved':sum(v for k,v in counts.items() if k!='resolved'),'counts':counts,
          'priority':[{'id':str(r['id']),'category':r['category'] if r['category'] in allowed else 'other','status':r['status'],'created':r['created'].isoformat(),'updated':r['updated'].isoformat()} for r in rows],
          'nextCursor':next_cursor,'reportTextIncluded':False,'conversationTextIncluded':False,'viewMarksResolved':False}
 def review_report(self,fid,review_id,status,note,evidence=None):
  from app.review_models import ReportReview
  value=ReportReview.model_validate({'reviewId':review_id,'status':status,'note':note,'evidence':evidence}).model_dump(mode='json',by_alias=True)
  if status=='resolved' and fid not in value['evidence']['reportIds']:raise APIError(400,'해결 근거에 해당 신고 ID가 필요합니다.')
  with self.pool.connection() as db:
   db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(fid,))
   rows=db.execute("SELECT id,'turn' scope FROM feedback WHERE id=%s UNION ALL SELECT id,'conversation' FROM conversation_reports WHERE id=%s",(fid,fid)).fetchall()
   if not rows:raise APIError(404,'신고를 찾을 수 없습니다.')
   if len(rows)!=1:raise APIError(409,'중복된 신고 ID를 확인해 주세요.')
   db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(review_id,))
   old=db.execute('SELECT * FROM report_reviews WHERE id=%s',(review_id,)).fetchone()
   if old:
    if str(old['report_id'])!=fid or old['status']!=status or old['note']!=note or old['evidence']!=value['evidence']:raise APIError(409,'같은 검토 ID에는 같은 검토 내용을 사용해야 합니다.')
    return {'id':fid,'reviewId':review_id,'status':status,'saved':True,'duplicate':True}
   table='feedback' if rows[0]['scope']=='turn' else 'conversation_reports'
   db.execute('UPDATE '+table+' SET workflow_status=%s,workflow_updated=now() WHERE id=%s',(status,fid))
   db.execute('INSERT INTO report_reviews(id,report_id,scope,status,note,evidence) VALUES(%s,%s,%s,%s,%s,%s)',(review_id,fid,rows[0]['scope'],status,note,Jsonb(value['evidence']) if value['evidence'] is not None else None))
   return {'id':fid,'reviewId':review_id,'status':status,'saved':True,'duplicate':False}
 def own_feedback(self,sender,fid=None,limit=50,cursor=None):return self.report_items(sender=sender,fid=fid,limit=limit,cursor=cursor)
 def admin_reports(self,fid=None,reviewed=None,limit=50,cursor=None,status='all'):return self.report_items(fid=fid,reviewed=reviewed,limit=limit,cursor=cursor,status=status)
 def admin(self,fid=None,reviewed=None):
  with self.pool.connection() as db:
   if fid is not None:
    r=db.execute('UPDATE feedback SET reviewed=%s,workflow_updated=now() WHERE id=%s RETURNING id',(reviewed,fid)).fetchone()
    if not r:raise APIError(404,'의견을 찾을 수 없습니다.')
   rows=db.execute('SELECT f.*,t.state,t.context,t.conversation FROM feedback f JOIN turns t ON t.id=f.turn ORDER BY f.created DESC LIMIT 200').fetchall()
   return [{**dict(r),'id':str(r['id']),'turn':str(r['turn']),'conversation':str(r['conversation']),'created':r['created'].isoformat()} for r in rows]

 def admin_turn_review(self,cid,tid,review_id,note):
  from app.diagnostics import clean
  from app.review_models import TurnReview
  from pydantic import ValidationError
  try:note=TurnReview.model_validate({'note':note}).note
  except ValidationError:raise APIError(400,'평가 메모의 형식이나 길이를 확인해 주세요.') from None
  with self.pool.connection() as db:
   self._turn(db,cid,tid)
   db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(review_id,))
   old=db.execute('SELECT turn,note,created FROM turn_reviews WHERE id=%s',(review_id,)).fetchone()
   if old:
    if str(old['turn'])!=tid or old['note']!=note:raise APIError(409,'같은 평가 ID에는 같은 턴과 메모를 사용해야 합니다.')
    created=old['created']
   else:
    created=db.execute('INSERT INTO turn_reviews(id,turn,note) VALUES(%s,%s,%s) RETURNING created',(review_id,tid,note)).fetchone()['created']
  return clean({'conversationId':cid,'turnId':tid,'reviewId':review_id,'note':note,'created':created.isoformat(),'saved':True,'duplicate':old is not None})

 def admin_conversations(self,limit=20,cursor=None,status=None,since=None,until=None,q='',feedback_state='all',sort='created'):
  if not 1<=limit<=50:raise APIError(400,'잘못된 페이지 크기입니다.')
  if feedback_state not in {'all','any','pending','reviewed'} or sort not in {'created','feedback'}:raise APIError(400,'잘못된 의견 필터입니다.')
  clauses=[];args=[]
  order_stamp="COALESCE(qa.latest_feedback,'1970-01-01T00:00:00Z'::timestamptz)" if sort=='feedback' else 'c.created'
  if cursor:
   try:
    kind,state,stamp,ident=json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    from uuid import UUID
    ident=str(UUID(ident));stamp=datetime.fromisoformat(stamp)
    if kind!=sort or state!=feedback_state or stamp.tzinfo is None:raise ValueError('cursor does not match filters')
   except Exception:raise APIError(400,'잘못된 페이지 커서입니다.')
   clauses.append('('+order_stamp+',c.id)<(%s,%s::uuid)');args.extend([stamp,ident])
  conditions=['t.conversation=c.id']
  if status:
   if status not in TERMINAL|{'PROCESSING'}:raise APIError(400,'잘못된 상태입니다.')
   conditions.append("t.state->>'status'=%s");args.append(status)
  for value,op in [(since,'>='),(until,'<')]:
   if value:
    try:stamp=datetime.fromisoformat(value)
    except ValueError:raise APIError(400,'날짜를 확인해 주세요.')
    if stamp.tzinfo is None:raise APIError(400,'시간대가 필요합니다.')
    conditions.append('t.created'+op+'%s');args.append(stamp)
  if q:
   if len(q)>200:raise APIError(400,'검색어가 너무 깁니다.')
   conditions.append("position(lower(%s) in lower(t.state->>'message'))>0");args.append(q)
  if status or since or until or q:clauses.append('EXISTS (SELECT 1 FROM turns t WHERE '+' AND '.join(conditions)+')')
  if feedback_state!='all':clauses.append('qa.focus_turn IS NOT NULL')
  review_clause={'pending':' AND NOT f.reviewed','reviewed':' AND f.reviewed'}.get(feedback_state,'')
  query="""SELECT c.*,
   (SELECT state->>'message' FROM turns WHERE conversation=c.id ORDER BY ordinal LIMIT 1) title,
   (SELECT count(*) FROM turns WHERE conversation=c.id) turn_count,
   (SELECT count(*) FROM turns WHERE conversation=c.id AND state->>'status'='FAILED') failed_count,
   (SELECT count(*) FROM feedback f JOIN turns t ON t.id=f.turn WHERE t.conversation=c.id) feedback_count,
   (SELECT count(*) FROM feedback f JOIN turns t ON t.id=f.turn WHERE t.conversation=c.id AND NOT f.reviewed) pending_count,
   qa.latest_feedback,qa.focus_turn,"""+order_stamp+""" sort_stamp
   FROM conversations c LEFT JOIN LATERAL (
    SELECT f.created latest_feedback,t.id focus_turn FROM feedback f JOIN turns t ON t.id=f.turn
    WHERE t.conversation=c.id"""+review_clause+" ORDER BY f.created DESC,f.id DESC LIMIT 1) qa ON true"
  if clauses:query+=' WHERE '+' AND '.join(clauses)
  query+=' ORDER BY '+order_stamp+' DESC,c.id DESC LIMIT %s';args.append(limit+1)
  with self.pool.connection() as db:rows=db.execute(query,args).fetchall()
  more=len(rows)>limit;rows=rows[:limit];items=[]
  for r in rows:
   items.append({'id':str(r['id']),'senderUserId':r['sender'],'recipientUserId':r['recipient'],'created':r['created'].isoformat(),'title':r['title'] or '새 대화','turnCount':r['turn_count'],'failedCount':r['failed_count'],'feedbackCount':r['feedback_count'],'pendingFeedbackCount':r['pending_count'],'latestFeedbackAt':r['latest_feedback'].isoformat() if r['latest_feedback'] else None,'focusTurnId':str(r['focus_turn']) if r['focus_turn'] else None})
  next_cursor=base64.urlsafe_b64encode(json.dumps([sort,feedback_state,rows[-1]['sort_stamp'].isoformat(),str(rows[-1]['id'])]).encode()).decode() if more else None
  from app.diagnostics import clean
  return clean({'items':items,'nextCursor':next_cursor})
 def admin_conversation(self,cid,after=0,limit=20,focus_tid=None):
  if after<0 or not 1<=limit<=50:raise APIError(400,'잘못된 페이지입니다.')
  with self.pool.connection() as db:
   c=self._conversation(db,cid)
   if focus_tid:
    target=self._turn(db,cid,focus_tid)
    previous=db.execute('SELECT ordinal FROM turns WHERE conversation=%s AND ordinal<%s ORDER BY ordinal DESC OFFSET %s LIMIT 1',(cid,target['ordinal'],min(2,limit-1))).fetchone()
    after=previous['ordinal'] if previous else 0
   rows=db.execute('SELECT * FROM turns WHERE conversation=%s AND ordinal>%s ORDER BY ordinal LIMIT %s',(cid,after,limit+1)).fetchall()
   more=len(rows)>limit;rows=rows[:limit];items=[]
   for r in rows:
    feedback=db.execute('SELECT id,payload,reviewed,created FROM feedback WHERE turn=%s ORDER BY created,id',(r['id'],)).fetchall()
    reviews=db.execute('SELECT id,note,created FROM turn_reviews WHERE turn=%s ORDER BY created,id',(r['id'],)).fetchall()
    items.append({'turnId':str(r['id']),'created':r['created'].isoformat(),'state':r['state'],'context':{**r['context'],'recipientContext':r['recipient_context']},'feedback':[{**dict(f),'id':str(f['id']),'created':f['created'].isoformat()} for f in feedback],
                  'reviews':[{'reviewId':str(v['id']),'note':v['note'],'created':v['created'].isoformat()} for v in reviews]})
  from app.diagnostics import clean
  return clean({'conversationId':cid,'senderUserId':c['sender'],'recipientUserId':c['recipient'],'turns':items,'nextCursor':rows[-1]['ordinal'] if more else None,'startCursor':after,'focusTurnId':focus_tid})
