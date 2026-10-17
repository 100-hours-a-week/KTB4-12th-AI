// Shared error handling for JSON APIs and streaming responses. Never replays writes.
window.ChatHTTP = {
  async check(response, onUnauthorized) {
    if (response.ok) return response;
    if (response.status === 401) onUnauthorized?.();
    let body;
    try { body = await response.json(); } catch {}
    const fallback = response.status === 401
      ? '접속 시간이 만료됐습니다. 체험 코드로 다시 접속하세요.'
      : [502, 503, 504].includes(response.status)
        ? '서버에 잠시 연결할 수 없습니다. 조금 뒤 다시 시도하세요.'
        : response.status === 429
          ? '요청이 많아 잠시 기다려야 합니다. 조금 뒤 다시 시도하세요.'
          : '요청을 처리하지 못했습니다. 다시 시도해 주세요.';
    const error = new Error(typeof body?.message === 'string' ? body.message : fallback);
    error.status = response.status;
    error.traceId = body?.error?.traceId;
    throw error;
  },
  async json(path, options = {}, onUnauthorized) {
    let response;
    try { response = await fetch(path, options); }
    catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new Error('네트워크 연결을 확인한 뒤 다시 시도해 주세요.');
    }
    await this.check(response, onUnauthorized);
    try { return await response.json(); }
    catch { throw new Error('서버 응답을 읽지 못했습니다. 다시 연결해 보세요.'); }
  }
};
