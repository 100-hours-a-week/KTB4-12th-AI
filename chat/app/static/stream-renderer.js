// Reveal only text already received. History and reduced-motion updates are immediate.
window.ChatReveal = {
  create({write, initial = '', changed = () => {}, reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    frame = requestAnimationFrame, cancelFrame = cancelAnimationFrame, now = () => performance.now()}) {
    let pending = [], handle = null, deadline = 0, previous = 0, done = null, closed = false;
    const segmenter = typeof Intl.Segmenter === 'function' ? new Intl.Segmenter('ko', {granularity: 'grapheme'}) : null;
    const split = text => segmenter ? Array.from(segmenter.segment(text), part => part.segment) : Array.from(text);
    let displayed = initial.length;
    function draw(count) {
      const text = pending.splice(0, count).join('');
      if (text) { write(text); displayed += text.length; changed(); }
    }
    function complete() { if (!pending.length && done) { const callback = done; done = null; callback(); } }
    function tick() {
      handle = null; if (closed) return;
      const time = now(), elapsed = Math.max(1, time - previous); previous = time;
      draw(reduced() || time >= deadline ? pending.length : Math.max(1, Math.ceil(pending.length * elapsed / Math.max(elapsed, deadline - time + elapsed))));
      if (pending.length) handle = frame(tick); else complete();
    }
    function schedule() { if (handle === null && pending.length) handle = frame(tick); }
    return {
      append(text) {
        if (closed || !text) return;
        const time = now(), idle = !pending.length;
        pending.push(...split(text));
        if (reduced()) { draw(pending.length); return; }
        if (idle) { previous = time; deadline = time + 160; }
        else deadline = Math.max(deadline, time + 80);
        if (!displayed) draw(1);
        schedule();
      },
      finish(callback) {
        if (closed) return;
        done = callback; deadline = Math.min(deadline, now() + 100);
        if (reduced()) draw(pending.length);
        if (pending.length) schedule(); else complete();
      },
      cancel() { closed = true; done = null; pending = []; if (handle !== null) cancelFrame(handle); handle = null; }
    };
  }
};
