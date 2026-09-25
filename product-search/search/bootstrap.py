from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import hashlib
import json
from .embedding import Encoder, SharedEmbedding
from .service import SearchService
from .snapshots import active_data_dir
from .reloading import ReloadingSearch
from .encoder_contract import encoder_contract


@asynccontextmanager
async def open_search(root: Path):
    """Create once at application startup; inject this shared instance into callers."""
    root=Path(root)
    directory=active_data_dir(root/'data')
    manifest=json.loads((directory/'manifest.json').read_text())
    if hashlib.sha256((root/'embedding/models/model_q4f16.onnx_data').read_bytes()).hexdigest()!=manifest['model_sha256']:
        raise ValueError('query_encoder_catalog_mismatch')
    fingerprint=await asyncio.to_thread(encoder_contract,root,manifest)
    if manifest.get('encoder_fingerprint') and manifest['encoder_fingerprint']!=fingerprint:
        raise ValueError('encoder_fingerprint_mismatch')
    def verify_encoder():
        if encoder_contract(root,manifest)!=fingerprint:
            raise RuntimeError('Encoder files changed; restart the search service')
    embedding=SharedEmbedding(Encoder(root/'embedding',verify=verify_encoder))
    service=None
    loading=None
    try:
        await embedding.start()
        loading=asyncio.create_task(asyncio.to_thread(SearchService,directory,embedding,encoder_fingerprint=fingerprint))
        initial=await asyncio.shield(loading)
        service=ReloadingSearch(root,initial,embedding,encoder_fingerprint=fingerprint)
        service.task=asyncio.create_task(service.watch())
        yield service
    finally:
        async def cleanup():
            try:
                if service is not None:
                    await service.close()
                elif loading is not None:
                    # Cancelling an await cannot stop the index-building thread.
                    # Retain its result until we can close the newly built index.
                    try:
                        initial=await loading
                    except Exception:
                        pass
                    else:
                        await asyncio.to_thread(initial.close)
            finally:
                await embedding.close()
        await asyncio.shield(asyncio.create_task(cleanup()))
