"""Optimization must keep original ordering even at a top-100 tie boundary."""
import numpy as np
import pytest
from search.service import SearchService


@pytest.mark.parametrize('count', [0,1,99,100,101,4231])
@pytest.mark.parametrize('distribution', ['equal','random','ties'])
def test_candidate_partition_matches_full_sort(count, distribution):
    service=SearchService.__new__(SearchService)
    rng=np.random.default_rng(25)
    keys=[f'source:{i}' for i in rng.permutation(count)]
    service.identity_ranks=np.empty(count,dtype=np.int64)
    order=sorted(range(count),key=lambda i:keys[i])
    service.identity_ranks[order]=np.arange(count)
    scores=(np.ones(count) if distribution=='equal' else rng.normal(size=count) if distribution=='random' else rng.integers(-2,3,size=count)).astype(np.float32)
    for mask in (np.ones(count,dtype=bool),rng.random(count)>.3,np.zeros(count,dtype=bool)):
        expected=sorted(np.flatnonzero(mask).tolist(),key=lambda i:(-float(scores[i]),keys[i]))[:100]
        assert service._top_candidates(scores,mask)==expected
