"""Versioned feedback events; the optional archive is authoritative for durability.

The SQLite schema stays compatible with old releases. Remote I/O happens outside
SQLite transactions. A stable submission ID makes retries after lost responses
idempotent, including after restoration into an empty local database.
"""
from datetime import datetime, timezone
import json
import threading
import uuid

FORMAT = 'product-search-feedback-event/1'
lock = threading.Lock()


class FeedbackError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code = status, code
        super().__init__(message)


def restore_event(db, event):
    if event.get('format') != FORMAT:
        raise ValueError('Unsupported feedback archive format')
    search, feedback = event['search'], event['feedback']
    # Validate before touching SQLite; unknown archive versions fail closed.
    from .models import FeedbackRequest
    body = FeedbackRequest.model_validate(feedback['request'])
    if body.search_id != search['id'] or body.submission_id != feedback['id']:
        raise ValueError('Feedback archive identity mismatch')
    with db() as con:
        con.execute('INSERT OR IGNORE INTO searches VALUES(?,?,?,?)',
                    (search['id'], search['createdAt'], json.dumps(search['request'], ensure_ascii=False),
                     json.dumps(search['response'], ensure_ascii=False)))
        con.execute('INSERT OR IGNORE INTO feedback VALUES(?,?,?,?,?,?)',
                    (feedback['id'], body.search_id, feedback['createdAt'], body.product_id, body.verdict, body.note))


def restore(db, archive):
    if archive is not None:
        with lock:
            for event in archive.events():
                restore_event(db, event)


def save(db, body, archive=None):
    submission = body.submission_id or uuid.uuid4().hex
    body = body.model_copy(update={'submission_id': submission})
    with lock:
        with db() as con:
            previous = con.execute('SELECT search_id,product_id,verdict,note FROM feedback WHERE id=?', (submission,)).fetchone()
            if previous:
                if previous != (body.search_id, body.product_id, body.verdict, body.note):
                    raise FeedbackError(409, 'FEEDBACK_ID_CONFLICT', '같은 제보 번호로 다른 의견을 저장할 수 없습니다.')
                return {'id': submission, 'saved': True}
            row = con.execute('SELECT created_at,request,response FROM searches WHERE id=?', (body.search_id,)).fetchone()
        if not row:
            raise FeedbackError(404, 'NOT_FOUND', '평가할 검색 기록이 없습니다. 다시 검색해 주세요.')
        result = json.loads(row[2])
        if body.verdict != 'missing' and body.product_id not in {p['productId'] for p in result['hits']}:
            raise FeedbackError(422, 'INVALID_REQUEST', '이 검색 결과에 포함된 상품만 평가할 수 있습니다.')
        if body.verdict == 'missing' and not body.note and not body.product_id:
            raise FeedbackError(422, 'INVALID_REQUEST', '누락된 상품명이나 의견을 입력해 주세요.')
        event = {
            'format': FORMAT,
            'search': {'id': body.search_id, 'createdAt': row[0], 'request': json.loads(row[1]), 'response': result},
            'feedback': {'id': submission, 'createdAt': datetime.now(timezone.utc).isoformat(),
                         'request': body.model_dump(by_alias=True)},
        }
        if archive is not None:
            try:
                archive.put(submission, event)
            except Exception:
                # Never expose credentials, signed URLs, or transport details.
                raise FeedbackError(503, 'FEEDBACK_STORAGE_UNAVAILABLE',
                                    '의견을 보관하지 못했습니다. 입력을 유지한 채 다시 시도해 주세요.') from None
        restore_event(db, event)
        return {'id': submission, 'saved': True}
