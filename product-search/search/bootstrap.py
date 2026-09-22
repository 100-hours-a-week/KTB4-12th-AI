from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import hashlib
import json
from .embedding import Encoder, SharedEmbedding
from .service import SearchService


@asynccontextmanager
async def open_search(root: Path):
    """Create once at application startup; inject this shared instance into callers."""
    root=Path(root)
    manifest=json.loads((root/'data/manifest.json').read_text())
    if hashlib.sha256((root/'embedding/models/model_q4f16.onnx_data').read_bytes()).hexdigest()!=manifest['model_sha256']:
        raise ValueError('query_encoder_catalog_mismatch')
    embedding=SharedEmbedding(Encoder(root/'embedding'))
    await embedding.start()
    service=None
    try:
        service=await asyncio.to_thread(SearchService,root/'data',embedding)
        yield service
    finally:
        await embedding.close()
        if service is not None:
            await asyncio.to_thread(service.close)
