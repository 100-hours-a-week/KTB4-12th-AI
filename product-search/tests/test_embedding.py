import shutil
from unittest.mock import Mock

import httpx
import numpy as np
import pytest

import app as server
from search.embedding import Encoder, SharedEmbedding


@pytest.fixture
def node_program(tmp_path):
    if shutil.which('node') is None:
        pytest.skip('Node is required for subprocess protocol regression tests')

    def write(program):
        (tmp_path / 'service.mjs').write_text(program)
        return tmp_path

    return write


@pytest.mark.parametrize('response', [
    'process.exit(7)',
    "console.log('not JSON')",
    "console.log('null')",
    "console.log(JSON.stringify({vector: ['not a number']}))",
    "console.log(JSON.stringify({error: 'inference failed'}))",
])
async def test_encoder_failures_are_503_and_next_query_can_restart(node_program, monkeypatch, response, service):
    directory = node_program(
        "import readline from 'node:readline';\n"
        "console.log(JSON.stringify({ready: true}));\n"
        "for await (const line of readline.createInterface({input: process.stdin})) {\n"
        + response + ';\n}\n'
    )
    encoder = Encoder(directory)
    embedding = SharedEmbedding(encoder)
    await embedding.start()
    failed_process = encoder.process

    monkeypatch.setattr(service, 'embedding', embedding)
    for name, value in {'search': service, 'active_searches': 0, 'active_profile_searches': 0}.items():
        monkeypatch.setattr(server.app.state, name, value, raising=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
            result = await client.post('/v1/search', json={'query': '정상 검색어'})
            assert result.status_code == 503
            assert encoder.process is None
            assert failed_process.poll() is not None
            assert failed_process.stdin.closed and failed_process.stdout.closed
            assert server.app.state.active_searches == 0
            assert server.app.state.active_profile_searches == 0

            node_program(
                "import readline from 'node:readline';\n"
                "console.log(JSON.stringify({ready: true}));\n"
                "for await (const line of readline.createInterface({input: process.stdin})) {\n"
                "console.log(JSON.stringify({vector: Array(768).fill(1)}));\n}\n"
            )
            result = await client.post('/v1/search', json={'query': '정상 검색어'})
            assert result.status_code == 200
            assert result.json()['hits']
            assert result.json()['snapshotId'] == service.snapshot_id
            assert np.isclose(np.linalg.norm(embedding.cache['정상 검색어']), 1)
    finally:
        await embedding.close()


@pytest.mark.parametrize('program', [
    'process.exit(7)',
    "console.log('not JSON'); setInterval(() => {}, 1000)",
    "console.log('null'); setInterval(() => {}, 1000)",
    "console.log(JSON.stringify({ready: false})); setInterval(() => {}, 1000)",
])
def test_start_failure_reaps_process_and_closes_pipes(node_program, monkeypatch, program):
    import search.embedding as module

    processes = []
    original = module.subprocess.Popen

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, 'Popen', capture)
    encoder = Encoder(node_program(program))
    with pytest.raises(RuntimeError):
        encoder.start()
    assert encoder.process is None
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


@pytest.mark.parametrize('operation', ['write', 'flush'])
def test_pipe_failure_becomes_execution_error_and_closes_process(tmp_path, operation):
    encoder = Encoder(tmp_path)
    process = Mock()
    process.poll.return_value = None
    getattr(process.stdin, operation).side_effect = BrokenPipeError('worker exited')
    encoder.process = process
    with pytest.raises(RuntimeError, match='통신 또는 응답 오류') as error:
        encoder.encode('정상 검색어')
    assert isinstance(error.value.__cause__, BrokenPipeError)
    assert encoder.process is None
    process.terminate.assert_called_once()
    process.wait.assert_called_once()
    process.stdin.close.assert_called_once()
    process.stdout.close.assert_called_once()


async def test_shared_start_failure_releases_executor(tmp_path):
    encoder = Encoder(tmp_path / 'missing-directory')
    embedding = SharedEmbedding(encoder)
    with pytest.raises(RuntimeError, match='모델 준비 실패'):
        await embedding.start()
    assert encoder.process is None
    assert embedding.task is None
    with pytest.raises(RuntimeError, match='shutdown'):
        embedding.pool.submit(lambda: None)


def test_encoder_restart_refuses_changed_model_files(tmp_path):
    from search.embedding import Encoder
    def reject():
        raise RuntimeError('encoder changed')
    encoder=Encoder(tmp_path,verify=reject)
    with pytest.raises(RuntimeError,match='encoder changed'):
        encoder.start()
    assert encoder.process is None
