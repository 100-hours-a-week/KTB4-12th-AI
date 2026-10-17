"""Synthetic callback endpoint only; not the team's Backend implementation."""
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()
calls = {}
modes = {}


@app.get('/health')
def health():
    return {'synthetic': True}


@app.put('/fixture/mode/{recipient}/{status}')
def mode(recipient: int, status: int):
    modes[recipient] = status
    return {'synthetic': True}


@app.get('/fixture/calls')
def all_calls():
    return calls


@app.post('/api/internal/v1/recipients/{recipient}/profile')
def callback(recipient: int, body: dict):
    calls.setdefault(recipient, []).append(body)
    code = modes.get(recipient, 200)
    return JSONResponse({'message': 'synthetic callback', 'error': {'code': 'FIXTURE'}}, status_code=code)
