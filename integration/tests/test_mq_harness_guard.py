"""The synthetic BE harness purges the three profiling queues, so it must refuse any broker that is not a
disposable local one (11-09 security review). A mistaken develop/production broker URL would otherwise erase the BE's
pending requests and results."""
import pytest

from integration.tests import mq_harness


@pytest.mark.parametrize("url", [
    "amqp://localhost:5672/gift", "amqp://127.0.0.1:5672/gift", "amqp://[::1]:5672/gift",
    "amqp://rabbitmq:5672/gift", "amqp://host.docker.internal:5672/gift", "amqp://synthetic.invalid/gift",
    "amqp://LOCALHOST:5672/gift",
])
def test_local_brokers_are_allowed(url):
    assert mq_harness.require_local(url) == url


@pytest.mark.parametrize("url", [
    "amqps://b-synthetic.mq.ap-northeast-2.amazonaws.com:5671/gift", "amqp://192.0.2.10:5672/gift",
    "amqp://mq.example.com:5672/gift", "amqp://localhost.example.com:5672/gift", "amqp:///gift",
])
def test_shared_or_unknown_brokers_are_refused(url):
    with pytest.raises(RuntimeError, match="local test broker"):
        mq_harness.require_local(url)


def test_broker_url_applies_the_guard(monkeypatch):
    monkeypatch.setenv("INTEGRATION_TEST_AMQP_URL", "amqp://192.0.2.10:5672/gift")
    with pytest.raises(RuntimeError, match="local test broker"):
        mq_harness.broker_url()
    monkeypatch.setenv("INTEGRATION_TEST_AMQP_URL", "amqp://127.0.0.1:5672/gift")
    assert mq_harness.broker_url() == "amqp://127.0.0.1:5672/gift"


def test_declare_refuses_before_connecting(monkeypatch):
    pika = pytest.importorskip("pika")
    monkeypatch.setattr(pika, "BlockingConnection", lambda *a, **k: pytest.fail("connected to a non-local broker"))
    with pytest.raises(RuntimeError, match="local test broker"):
        mq_harness.declare("amqp://mq.example.com:5672/gift")
