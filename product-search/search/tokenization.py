"""Shared document/query analysis for lexical retrieval, independent of dense tokens."""
from functools import lru_cache
import re
import unicodedata

from kiwipiepy import Kiwi, Match

TOKENIZER_ID = 'kiwi0.23.2-cong-v1'
TOKENIZER_INFO = {
    'name': 'kiwi', 'version': '0.23.2', 'modelVersion': '0.23.0',
    'modelType': 'cong', 'workers': 2, 'policy': TOKENIZER_ID,
}
CONTENT_TAGS = frozenset({
    'NNG', 'NNP', 'NNB', 'NR', 'NP', 'VV', 'VA', 'VX',
    'XR', 'MAG', 'MAJ', 'SL', 'SH', 'SN',
})
MATCH_OPTIONS = (Match.ALL | Match.JOIN_NOUN_PREFIX | Match.JOIN_NOUN_SUFFIX
                 | Match.JOIN_VERB_SUFFIX | Match.JOIN_ADJ_SUFFIX)
IDENTIFIERS = re.compile(r'[a-z0-9]+(?:[-_.][a-z0-9]+)*')


def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', str(text)).casefold().split())


@lru_cache(maxsize=1)
def _kiwi():
    # Index construction warms this instance before concurrent search requests.
    return Kiwi(num_workers=2, model_type='cong', integrate_allomorph=True)


def _terms(text, analyzed):
    forms = [normalize(t.form) for t in analyzed
             if t.tag.split('-')[0] in CONTENT_TAGS and t.form.strip()]
    # Preserve complete model codes/units (e.g. WH-1000XM5, 500ml) when split
    # by morphology. Repeated surface occurrences retain their term frequency.
    present = set(forms)
    forms.extend(code for code in IDENTIFIERS.findall(text) if code not in present)
    return tuple(forms)


@lru_cache(maxsize=4096)
def _query_tokens(text):
    return _terms(text, _kiwi().tokenize(text, match_options=MATCH_OPTIONS))


def tokens(text):
    return _query_tokens(normalize(text))


def batch_tokens(texts):
    """Use the same policy as queries, deduplicate fields and consume Kiwi's pool."""
    texts = [normalize(text) for text in texts]
    unique = list(dict.fromkeys(texts))
    analyzed = _kiwi().tokenize(unique, match_options=MATCH_OPTIONS)
    by_text = {text: _terms(text, result) for text, result in zip(unique, analyzed, strict=True)}
    return [by_text[text] for text in texts]
