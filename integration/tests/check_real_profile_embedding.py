"""Synthetic tags + actual catalog DB, encoder, vectors, percentile and MMR."""
import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa

from profiling import pipeline, selection
from profiling.catalog import DbCatalogReader
from profiling.embedding import ProductSearchEmbedding
from profiling.ports import TagReply
from profiling.stores import DbProfileRunStore, DbRecipientProfileStore
from profiling.types import ProfileRequest, RunStatus


class SyntheticTags:
    model = 'synthetic-no-network'
    def extract(self, system, prompt, schema):
        return TagReply(json.dumps({'tags': [{'axis': '카테고리', 'tag': '컵', 'ref': 'pref'}]}))


def check(root):
    engine = sa.create_engine(os.environ['FIXTURE_PROFILE_DB'])
    with engine.connect() as connection:
        if connection.execute(sa.text('SELECT count(*) FROM ai_catalog.integration_marker')).scalar_one() != 1:
            raise ValueError('Embedding test requires the isolated marked fixture database')
    embedding = ProductSearchEmbedding(root, engine)
    try:
        with patch.object(embedding, 'encode', wraps=embedding.encode) as encode, \
             patch.object(embedding, 'product_vectors', wraps=embedding.product_vectors) as vectors, \
             patch.object(selection, '_embedding_pick', wraps=selection._embedding_pick) as mmr:
            outcome = pipeline.profile(
                ProfileRequest(recipient_user_id=904, source_version=1, gift_preference='컵', reviews=[], disliked_categories=[]),
                catalog=DbCatalogReader(engine, poll_ttl_s=0), store=DbProfileRunStore(engine),
                recipient_store=DbRecipientProfileStore(engine), tagger=SyntheticTags(),
                embedding=embedding, pool_size=2)
            assert outcome.status is RunStatus.RESULT_READY, outcome
            assert encode.call_count and vectors.call_count and mmr.call_count
            assert outcome.search.product_ids
            assert len(outcome.search.product_ids) == len(set(outcome.search.product_ids))
            return {'passed': True, 'paidModelCalls': 0, 'syntheticTagger': True,
                    'realDBAndEncoderAndMMR': True, 'encodeCalls': encode.call_count,
                    'vectorCalls': vectors.call_count, 'mmrCalls': mmr.call_count,
                    'productIds': outcome.search.product_ids, 'preferredTags': outcome.validation.preferred_tags,
                    'catalogVersion': str(outcome.search.catalog_version_id)}
    finally:
        embedding.close()
        engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.runtime)))
