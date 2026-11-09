"""Both integrated Profiler runtimes must start MQ consumption only after every part is wrapped (11-08).

profiling.main.lifespan starts the consumer itself unless ``app.state.mq_defer_start`` is set. If an integration
runtime forgets the flag, messages are consumed with the unwrapped store/catalog (no generation guard, no
required-model check) for the first moments after boot.
"""
import inspect

import pytest


@pytest.mark.parametrize("module", ["integration.profiler_runtime", "integration.profiler_free_runtime"])
def test_runtime_defers_mq_until_parts_are_wrapped(module):
    source = inspect.getsource(__import__(module, fromlist=["lifespan"]).lifespan)
    defer = source.index("app.state.mq_defer_start = True")
    enter = source.index("main.lifespan(app)")
    wrap = source.index("RequiredStore(")
    start = source.index("app.state.mq.start()")
    assert defer < enter < wrap < start
    assert source.index("yield") > start
