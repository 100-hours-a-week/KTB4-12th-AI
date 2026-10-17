"""Audit native tool targets and optionally omit explanatory prose from loss.

Both policies encode identical input tokens. ``native_decisions`` retains the
entire gift_context call and all show_gifts structure/product IDs, including
string delimiters. Only message/reason string contents lose supervision.
Ordinary replies, source-grounded facts and preservation replay retain their
full completion loss. This changes labels, never the model's input/history.
"""
import re

from gemma_runtime import NativeValueParser, parse_generation, render

POLICIES = ('completion_only', 'native_decisions')
STRING_MARKER = '<|"|>'


class SpanParser(NativeValueParser):
    """Record argument value character spans using the native value grammar."""
    def __init__(self, text):
        super().__init__(text)
        self.spans = {}

    def value(self, depth=0, path=()):
        if depth > 40:
            raise ValueError('native_argument_depth')
        self.space()
        start = self.index
        char = self.text[start:start+1]
        if char not in ('{', '['):
            result = super().value(depth)
        else:
            obj = char == '{'
            close = '}' if obj else ']'
            result = {} if obj else []
            self.index += 1
            self.space()
            if self.text[self.index:self.index+1] == close:
                self.index += 1
            else:
                while True:
                    self.space()
                    if obj:
                        if (self.text.startswith(STRING_MARKER, self.index)
                                or self.text[self.index:self.index+1] == '"'):
                            key = NativeValueParser.value(self, depth+1)
                        else:
                            match = re.match(r'[A-Za-z_][\w]*', self.text[self.index:])
                            if not match:
                                raise ValueError('invalid_native_key')
                            key = match.group()
                            self.index += len(key)
                        if not isinstance(key, str):
                            raise ValueError('invalid_native_key')
                        self.space()
                        if self.text[self.index:self.index+1] != ':':
                            raise ValueError('missing_native_colon')
                        self.index += 1
                        if key in result:
                            raise ValueError('duplicate_native_key')
                        result[key] = self.value(depth+1, path+(key,))
                    else:
                        result.append(self.value(depth+1, path+(len(result),)))
                    self.space()
                    separator = self.text[self.index:self.index+1]
                    self.index += 1
                    if separator == close:
                        break
                    if separator != ',':
                        raise ValueError('invalid_native_separator')
        self.spans[path] = (start, self.index)
        return result


def omitted_prose_spans(target):
    """Return exact interior spans; reject ambiguous native tool targets."""
    if not target.startswith('<|tool_call>'):
        if '<|tool_call>' in target:
            raise ValueError('mixed_text_and_tools')
        return [], []
    parsed = parse_generation(target)
    matches = list(re.finditer(r'<\|tool_call>call:([A-Za-z_][\w]*)', target))
    omitted, fields = [], []
    for match, call in zip(matches, parsed['toolCalls'], strict=True):
        if call['name'] not in ('gift_context', 'show_gifts', 'recall_gift', 'list_gifts', 'inspect_search'):
            raise ValueError('unregistered_decision_tool:'+call['name'])
        end = target.find('<tool_call|>', match.end())
        parser = SpanParser(target[match.end():end])
        args = parser.value()
        parser.space()
        if args != call['arguments'] or parser.index != len(parser.text):
            raise ValueError('span_parser_disagrees_with_native_parser')
        if call['name'] == 'inspect_search':
            if args: raise ValueError('invalid_inspect_search_shape')
            continue
        paths = []
        if call['name'] == 'list_gifts':
            if (set(args) - {'product_ids', 'format', 'one_per_brand', 'message'} or not isinstance(args.get('product_ids'), list)
                    or len(args['product_ids']) > 30 or any(not isinstance(x, str) or not x for x in args['product_ids'])
                    or args.get('format', 'names') not in ('names', 'brief')
                    or not isinstance(args.get('one_per_brand', False), bool)):
                raise ValueError('invalid_list_gifts_shape')
            paths = [('message',)] if 'message' in args else []
        elif call['name'] != 'show_gifts':
            continue
        elif 'product_ids' in args and not set(args) - {'product_ids', 'message'}:
            if not isinstance(args['product_ids'], list) or len(args['product_ids']) > 3 or any(not isinstance(x, str) or not x for x in args['product_ids']):
                raise ValueError('invalid_show_gifts_id_shape')
            paths = [('message',)] if 'message' in args else []
        else:
            if not isinstance(args.get('message'), str) or not isinstance(args.get('products'), list):
                raise ValueError('invalid_show_gifts_prose_shape')
            paths = [('message',)]
            for index, product in enumerate(args['products']):
                if not isinstance(product.get('reason'), str):
                    raise ValueError('invalid_show_gifts_reason')
                paths.append(('products', index, 'reason'))
        if ('message',) in paths and not isinstance(args['message'], str):
            raise ValueError('invalid_message_shape')
        for path in paths:
            start, stop = parser.spans[path]
            value_text = parser.text[start:stop]
            if value_text.startswith(STRING_MARKER) and value_text.endswith(STRING_MARKER):
                start += len(STRING_MARKER)
                stop -= len(STRING_MARKER)
            elif value_text.startswith('"') and value_text.endswith('"'):
                start += 1
                stop -= 1
            else:
                raise ValueError('prose_value_not_a_delimited_string')
            omitted.append((match.end()+start, match.end()+stop))
            fields.append({'tool':call['name'], 'path':list(path),
                           'start':match.end()+start, 'end':match.end()+stop})
    return omitted, fields


def encode_record(tokenizer, row, loss_policy='completion_only'):
    if loss_policy not in POLICIES:
        raise ValueError('unknown_loss_policy:'+loss_policy)
    prompt = render(tokenizer, row['prompt'], row['tools'], True)
    full = render(tokenizer, row['prompt']+row['completion'], row['tools'])
    if not full.startswith(prompt):
        raise ValueError('native_template_prefix_changed')
    target = full[len(prompt):]
    prefix_ids = tokenizer(prompt, add_special_tokens=False)['input_ids']
    encoded = tokenizer(full, add_special_tokens=False, return_offsets_mapping=True)
    ids = encoded['input_ids']
    split_boundary = ids[:len(prefix_ids)] != prefix_ids
    if split_boundary:
        suffix = tokenizer(target, add_special_tokens=False, return_offsets_mapping=True)
        ids = prefix_ids+suffix['input_ids']
        offsets = suffix['offset_mapping']
    else:
        offsets = [(start-len(prompt), stop-len(prompt))
                   for start,stop in encoded['offset_mapping'][len(prefix_ids):]]
    completion_ids = ids[len(prefix_ids):]
    if len(offsets) != len(completion_ids):
        raise ValueError('offset_length_mismatch')
    if any(start < 0 or stop < start or stop > len(target) for start,stop in offsets):
        raise ValueError('invalid_completion_offsets')
    labels = [-100]*len(prefix_ids)+completion_ids
    omitted, fields = (omitted_prose_spans(target)
                       if loss_policy == 'native_decisions' and not row.get('preservationReplay',False)
                       else ([], []))
    dropped = []
    boundary_retained = 0
    for index,(start,stop) in enumerate(offsets):
        if stop > start and any(a <= start and stop <= b for a,b in omitted):
            labels[len(prefix_ids)+index] = -100
            dropped.append(index)
        elif any(start < b and stop > a for a,b in omitted):
            # A token crossing a prose/grammar boundary remains supervised:
            # token masking cannot remove only half a token safely.
            boundary_retained += 1
    supervised = sum(value != -100 for value in labels)
    if len(ids) > 16384:
        raise ValueError('overlong_record:'+str(len(ids)))
    if not supervised:
        raise ValueError('empty_supervision')
    if labels[:len(prefix_ids)] != [-100]*len(prefix_ids):
        raise ValueError('prompt_loss_leak')
    record = {'input_ids':ids, 'attention_mask':[1]*len(ids), 'labels':labels}
    audit = {'inputTokens':len(ids), 'targetTokens':supervised,
             'completionTokens':len(completion_ids), 'droppedProseTokens':len(dropped),
             'maskedPromptTokens':len(prefix_ids), 'boundaryRetainedTokens':boundary_retained,
             'splitBpeBoundary':split_boundary, 'lossPolicy':loss_policy,
             'replay':row.get('preservationReplay', not row['tools']),
             'omittedFields':fields, 'promptTail':prompt[-200:], 'target':target,
             'droppedTokenPositions':dropped}
    return record, audit
