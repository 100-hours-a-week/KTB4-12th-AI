"""Bound JSON buffering before FastAPI parses input or writes QA records."""
from starlette.responses import JSONResponse

MAX_REQUEST_BYTES = 64 * 1024


class RequestBodyLimit:
    def __init__(self, app, max_bytes=MAX_REQUEST_BYTES):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith(('/api/', '/v1/')):
            return await self.app(scope, receive, send)

        async def reject():
            await JSONResponse(
                {'message': '요청 본문 크기가 제한을 초과했습니다.',
                 'error': {'code': 'REQUEST_TOO_LARGE'}},
                status_code=413, headers={'Cache-Control': 'no-store'},
            )(scope, receive, send)

        lengths = [v for k, v in scope['headers'] if k.lower() == b'content-length']
        if any(v.isdigit() and (len(v) > 20 or int(v) > self.max_bytes) for v in lengths):
            return await reject()
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > self.max_bytes:
                return await reject()
            body.extend(chunk)
            if not message.get('more_body', False):
                break

        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {'type': 'http.request', 'body': bytes(body), 'more_body': False}

        await self.app(scope, replay, send)
