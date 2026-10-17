"""CPU checks for next-study data; does not change frozen model grading."""
import collections
import hashlib
import json


def operation(row):
    calls=row['completion'][0].get('tool_calls',[])
    return calls[0]['function']['name'] if calls else 'text'


def structured_values(row):
    for message in row['prompt']+row['completion']:
        content=message.get('content') or ''
        if message['role']=='system' and 'STATE_JSON\n' in content:
            yield json.loads(content.split('STATE_JSON\n',1)[1])
        elif message['role']=='tool':
            yield json.loads(content)
        for call in message.get('tool_calls',[]):
            args=call['function']['arguments']
            yield json.loads(args) if isinstance(args,str) else args
    yield row['tools']


def identity_errors(row):
    """Every remembered display must resolve, including pre-dedup request IDs."""
    values=list(structured_values(row));known=set();references=[];errors=[]
    def walk(value,path):
        if isinstance(value,list):
            for i,x in enumerate(value):walk(x,f'{path}[{i}]')
        elif isinstance(value,dict):
            if isinstance(value.get('product_id'),str):known.add(value['product_id'])
            if 'last_listed_product_ids' in value and 'products' in value:
                ids={p['product_id'] for p in value['products']}
                missing=set(value['last_listed_product_ids'])-ids
                if missing:errors.append({'code':'unresolved_display_memory','path':path,'ids':sorted(missing)})
            for key,item in value.items():
                if key in {'product_id','product_ids'} and isinstance(item,dict):
                    known.update(item.get('enum',[]));known.update(item.get('items',{}).get('enum',[]))
                if (key=='candidate_ids' or key=='product_ids' or key.endswith('_product_ids')) and isinstance(item,list):
                    references.append((path+'.'+key,item))
                walk(item,path+'.'+key)
    for i,value in enumerate(values):walk(value,str(i))
    # Exclusions can legitimately refer to products not shown. Request and
    # display identities must resolve to some observed product/schema enum.
    for path,ids in references:
        if path.endswith(('.requested_product_ids','.last_listed_product_ids')):
            missing=set(ids)-known
            if missing:errors.append({'code':'unresolved_identity_reference','path':path,'ids':sorted(missing)})
    return errors


def prefix_origin(row):
    provenance=row.get('provenance')
    if provenance=='verified_actual_student_prefix':
        if row.get('exactStudentPrefix') and row.get('exactPrefixSha256') and row.get('verification')=='real_harness_continuation_passed':
            return 'actual_student_prefix'
        return 'actual_student_prefix' if row.get('sourceCall')==0 else 'teacher_continuation'
    if provenance=='verified_teacher_continuation':return 'teacher_continuation'
    return 'other'


def audit(rows):
    counts=collections.Counter();issues=[];seen={};domain_users=set();scopes=collections.Counter()
    for index,row in enumerate(rows):
        replay=bool(row['preservationReplay']);counts['generalRecords' if replay else 'domainRecords']+=1
        op=operation(row);counts[('general/' if replay else 'domain/')+op]+=1
        origin=prefix_origin(row);counts[origin]+=1
        if origin=='actual_student_prefix' and row.get('originalFailed'):counts['failed_actual_student_prefix']+=1
        errors=identity_errors(row)
        if errors:issues.append({'index':index,'sourceTrainCase':row.get('sourceTrainCase'),'errors':errors})
        names={t['function']['name'] for t in row['tools']}
        if op!='text' and op not in names:issues.append({'index':index,'errors':[{'code':'unavailable_target_tool'}]})
        key=hashlib.sha256(json.dumps([row['prompt'],row['tools']],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        target=json.dumps(row['completion'],sort_keys=True,ensure_ascii=False)
        if key in seen and seen[key]!=target:issues.append({'index':index,'errors':[{'code':'conflicting_exact_prefix'}]})
        seen[key]=target
        if not replay:
            domain_users.add(next(m['content'] for m in reversed(row['prompt']) if m['role']=='user'))
            state=json.loads(row['prompt'][0]['content'].split('STATE_JSON\n',1)[1])
            prepared=state.get('prepared_candidates',{})
            scope=prepared.get('scope','only') if prepared.get('count',0)>0 else 'unprepared'
            scopes[scope]+=1;counts['scope/'+scope+'/'+op]+=1
            repair=False
            for m in row['prompt']:
                if m['role']=='tool':
                    value=json.loads(m['content'])
                    repair|=isinstance(value,dict) and value.get('ok') is False
            counts['tool_error_feedback_records']+=repair
    return {'records':len(rows),'counts':dict(counts),'preparedScopes':dict(scopes),
            'uniqueLastDomainUserMessages':len(domain_users),'identityErrorRows':sum(bool(identity_errors(r)) for r in rows),
            'issues':issues,'modelLoaded':False,'localGpuUsed':False}


def require_next_study_quality(rows,requirements):
    report=audit(rows)
    if report['issues']:raise ValueError('Training data integrity errors')
    for key,minimum in requirements.get('minimumCounts',{}).items():
        if report['counts'].get(key,0)<minimum:raise ValueError('Insufficient coverage: '+key)
    for scope,minimum in requirements.get('minimumScopes',{}).items():
        if report['preparedScopes'].get(scope,0)<minimum:raise ValueError('Insufficient scope coverage: '+scope)
    return report
