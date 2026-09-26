import pytest

from csm.core import events


@pytest.fixture(autouse=True)
def _isolate_event_subscribers():
    """Keep one test's subscribers out of another's.

    Snapshot and restore rather than clear, so the subscribers the apps register
    at startup survive into the next test.
    """
    snapshot = {key: list(value) for key, value in events._subscribers.items()}
    yield
    events._subscribers.clear()
    events._subscribers.update(snapshot)


@pytest.fixture(autouse=True)
def _reset_pending_recalculations():
    """Clear the per-transaction recalculation guard between tests.

    ``health.signals`` keeps a module-level set so one transaction schedules at
    most one recalculation per account. In a test nothing ever commits, so the
    ``on_commit`` callback that would empty that set never runs and the entry
    leaks into the next test - where the next save is deduplicated against a
    transaction that ended long ago and silently does not rescore.
    """
    from csm.health import signals

    signals._pending.clear()
    yield
    signals._pending.clear()


@pytest.fixture
def csm_user(db):
    from tests.factories import UserFactory

    return UserFactory(csm=True, email="csm@dphish.com", full_name="Casey Morgan")


@pytest.fixture
def senior_csm_user(db):
    from tests.factories import UserFactory

    return UserFactory(senior_csm=True, email="senior@dphish.com", full_name="Sam Rivers")


@pytest.fixture
def acm_user(db):
    from tests.factories import UserFactory

    return UserFactory(acm=True, email="acm@dphish.com", full_name="Alex Chen")


@pytest.fixture
def manager_user(db):
    from tests.factories import UserFactory

    return UserFactory(manager=True, email="manager@dphish.com", full_name="Morgan Lee")
