"""Synthetic text only; existing local CPU encoder assets, no network or model download."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

import numpy as np
from profiling.embedding import ProductSearchEmbedding
from search.embedding import Encoder
from search.encoder_contract import encoder_contract, initial_manifest

ROOT=Path(__file__).resolve().parents[2]
ASSETS=Path(os.environ['INTEGRATION_ENCODER_ASSETS'])


def main():
    os.environ['HF_HUB_OFFLINE']='1'
    with tempfile.TemporaryDirectory(prefix='encoder-parity-') as directory:
        root=Path(directory);code=root/'embedding';code.mkdir()
        for name in ('service.mjs','runtime.mjs','documents.mjs','model.lock.json'):
            (code/name).write_bytes((ROOT/'product-search/embedding'/name).read_bytes())
        for name in ('models','node_modules'):(code/name).symlink_to(ASSETS/name,target_is_directory=True)
        manifest=initial_manifest(root);signature=encoder_contract(root,manifest)
        texts=['합성 선물 검증용 머그컵', 'Synthetic TAG Case', '합성 책상 정리']
        old=object.__new__(ProductSearchEmbedding);old._dir=code;old._proc=None;old._lock=threading.Lock()
        try:legacy=old.encode(texts)
        finally:old.close()
        encoder=Encoder(code,verify=lambda:encoder_contract(root,manifest))
        try:
            encoder.start()
            current=np.stack([encoder.encode(text) for text in texts])
            current/=np.linalg.norm(current,axis=1,keepdims=True)
        finally:encoder.close()
        np.testing.assert_allclose(current,legacy,rtol=1e-6,atol=1e-7)
        result={'passed':True,'syntheticTextCount':len(texts),'dimensions':768,
            'model':manifest['model'],'modelSha256':manifest['model_sha256'],
            'encoderFingerprint':signature,'maxAbsDifference':float(np.max(np.abs(current-legacy))),
            'legacyVectorsSha256':hashlib.sha256(legacy.tobytes()).hexdigest(),
            'sharedVectorsSha256':hashlib.sha256(current.tobytes()).hexdigest(),
            'distance':'cosine','preprocessing':'raw text + Query: prefix + 1024 truncation + L2',
            'actualCPUEncoder':True,'externalCalls':0,'privateInputs':False}
        print(json.dumps(result))


if __name__=='__main__':main()
