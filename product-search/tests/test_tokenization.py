from concurrent.futures import ThreadPoolExecutor

import pytest

from search.tokenization import batch_tokens, tokens


def test_particles_are_removed_without_arbitrary_character_fragments():
    assert tokens('텀블러를') == tokens('텀블러') == ('텀블러',)
    assert '보온병' in tokens('보온병은')
    assert not {'은', '를', '텀블', '블러', '러를'} & set(tokens('텀블러를 보온병은'))


def test_inflected_adjectives_share_a_stem():
    assert '예쁘' in set(tokens('예쁜 컵')) & set(tokens('예쁘다'))


@pytest.mark.parametrize('code', ['WH-1000XM5', 'ＷＨ－１０００ＸＭ５', 'SM-S928N'])
def test_model_identifiers_survive_normalization_and_token_splitting(code):
    expected = 'sm-s928n' if code == 'SM-S928N' else 'wh-1000xm5'
    assert expected in tokens(code)
    assert '500ml' in tokens('500ml')


def test_document_batch_and_concurrent_queries_use_identical_analysis():
    texts = ['스탠리 텀블러를', '머그컵 500ml', 'WH-1000XM5', '', '!!!', '😀한😀글😀']
    expected = batch_tokens(texts)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(tokens, texts)) == expected
    assert tokens('!!!') == ()
