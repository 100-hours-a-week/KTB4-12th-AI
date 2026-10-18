"""Check current product visibility before first send and every stored resend."""
import httpx
from profiling.ports import NoActiveCatalog


class VisibleProductsBackend:
    def __init__(self, backend, url, token, *, transport=None):
        self.backend = backend
        self.http = httpx.Client(base_url=url.rstrip('/'), headers={'Authorization':'Bearer '+token},
                                 timeout=10, trust_env=False, follow_redirects=False, transport=transport)

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def send_profile_callback(self, outcome):
        ids = list(outcome.search.product_ids[:30]) if outcome.search else []
        if ids:
            try:
                response = self.http.post('/v1/products', json={'ids':ids})
                response.raise_for_status()
                data = response.json()
                visible = {p['productId'] for p in data['products']}
                if data['missingIds'] or visible != set(ids):
                    raise ValueError('PRODUCT_NOT_VISIBLE')
            except Exception:
                # Leave the original durable payload/status intact. No callback
                # occurred, so do not count an attempt or silently filter its IDs.
                raise NoActiveCatalog('CALLBACK_PRODUCT_VISIBILITY_NOT_CONFIRMED') from None
        return self.backend.send_profile_callback(outcome)

    def close(self):
        try:self.http.close()
        finally:self.backend.close()
