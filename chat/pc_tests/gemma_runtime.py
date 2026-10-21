"""Pure serialization/parser helpers from the PC capture; model runtime omitted."""
import copy
import json
import re

def normalize_messages(messages):
    rows=copy.deepcopy(messages)
    for message in rows:
        for call in message.get('tool_calls',[]):
            args=call['function']['arguments']
            if isinstance(args,str):call['function']['arguments']=json.loads(args)
    return rows

def native_tools(tools):
    def visit(value):
        if isinstance(value,list):return [visit(x) for x in value]
        if not isinstance(value,dict):return value
        value=copy.deepcopy(value)
        union=value.get('anyOf')
        if union and len(union)==2 and any(x.get('type')=='null' for x in union):
            nonnull=next(x for x in union if x.get('type')!='null')
            value.pop('anyOf');value.update(nonnull);value['nullable']=True
        return {k:visit(v) for k,v in value.items()}
    return visit(tools)

def render(tokenizer,messages,tools,generate=False):
    return tokenizer.apply_chat_template(normalize_messages(messages), tools=native_tools(tools) or None,
        enable_thinking=False, tokenize=False, add_generation_prompt=generate)

class NativeValueParser:
    def __init__(self,text):self.text=text;self.index=0
    def space(self):
        while self.index<len(self.text) and self.text[self.index].isspace():self.index+=1
    def value(self,depth=0):
        if depth>40:raise ValueError('native_argument_depth')
        self.space();s=self.text;i=self.index
        if s.startswith('<|"|>',i):
            end=s.find('<|"|>',i+5)
            if end<0:raise ValueError('unterminated_native_string')
            self.index=end+5;return s[i+5:end]
        if i>=len(s):raise ValueError('truncated_native_value')
        if s[i]=='"':
            value,length=json.JSONDecoder().raw_decode(s[i:]);self.index+=length;return value
        if s[i] in '[{':
            obj=s[i]=='{';close='}' if obj else ']';self.index+=1;out={} if obj else []
            self.space()
            if self.text[self.index:self.index+1]==close:self.index+=1;return out
            while True:
                if obj:
                    self.space()
                    if self.text.startswith('<|"|>',self.index) or self.text[self.index:self.index+1]=='"':key=self.value(depth+1)
                    else:
                        m=re.match(r'[A-Za-z_][\w]*',self.text[self.index:])
                        if not m:raise ValueError('invalid_native_key')
                        key=m.group();self.index+=len(key)
                    self.space()
                    if self.text[self.index:self.index+1]!=':':raise ValueError('missing_native_colon')
                    self.index+=1
                    if key in out:raise ValueError('duplicate_native_key')
                    out[key]=self.value(depth+1)
                else:out.append(self.value(depth+1))
                self.space();char=self.text[self.index:self.index+1];self.index+=1
                if char==close:return out
                if char!=',':raise ValueError('invalid_native_separator')
        for literal,value in [('true',True),('false',False),('null',None)]:
            if s.startswith(literal,i):self.index+=len(literal);return value
        m=re.match(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?',s[i:])
        if not m:raise ValueError('invalid_native_value')
        self.index+=len(m.group());return json.loads(m.group())

def parse_generation(raw):
    # Returned reasoning is recorded, but never converted into training targets.
    reasoning=''
    if '<|channel>thought' in raw:
        before,rest=raw.split('<|channel>thought',1)
        if '<channel|>' not in rest:raise ValueError('unfinished_thought')
        reasoning,after=rest.split('<channel|>',1);raw=before+after
    matches=list(re.finditer(r'<\|tool_call>call:([A-Za-z_][\w]*)',raw))
    if matches:
        if raw[:matches[0].start()].strip():raise ValueError('mixed_text_and_tools')
        calls=[];previous_end=matches[0].start()
        for match in matches:
            if raw[previous_end:match.start()].strip():raise ValueError('mixed_text_and_tools')
            end=raw.find('<tool_call|>',match.end())
            if end<0:raise ValueError('unfinished_tool_call')
            parser=NativeValueParser(raw[match.end():end]);args=parser.value();parser.space()
            if parser.index!=len(parser.text) or not isinstance(args,dict):raise ValueError('invalid_argument_tail')
            calls.append({'name':match.group(1),'arguments':args})
            previous_end=end+len('<tool_call|>')
        if raw[previous_end:].strip() not in ('','<|tool_response>','<turn|>'):
            raise ValueError('unexpected_tool_tail')
        return {'content':None,'toolCalls':calls,'reasoning':reasoning}
    text=raw.replace('<turn|>','').replace('<|tool_response>','').strip()
    if '<|' in text or '|>' in text:raise ValueError('unexpected_native_control_token')
    return {'content':text,'toolCalls':[],'reasoning':reasoning}
