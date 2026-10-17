"""Fingerprint the actual installed document/query encoder and tokenizer."""
import hashlib
import json


def initial_manifest(root):
    lock = json.loads((root/'embedding/model.lock.json').read_text())
    return {'model': lock['model'], 'dimensions': 768, 'precision': 'q4f16',
            'max_tokens': 1024, 'query_prefix': 'Query: ', 'document_prefix': 'Document: ',
            'model_sha256': next(f['sha256'] for f in lock['files']
                                 if f['path'] == 'model_q4f16.onnx_data')}


def encoder_contract(root, manifest):
    if manifest['model'] != 'jinaai/jina-embeddings-v5-text-nano-retrieval' or manifest['dimensions'] != 768 or manifest['precision'] != 'q4f16' or manifest['max_tokens'] != 1024 or manifest['query_prefix'] != 'Query: ' or manifest['document_prefix'] != 'Document: ':
        raise ValueError('Unsupported local encoder contract')
    models = root/'embedding/models'
    if hashlib.sha256((models/'model_q4f16.onnx_data').read_bytes()).hexdigest() != manifest['model_sha256']:
        raise ValueError('Encoder weight mismatch')
    files = [root/'embedding/runtime.mjs',root/'embedding/documents.mjs',*sorted(p for p in models.iterdir() if p.is_file())]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''): digest.update(block)
    return digest.hexdigest()
