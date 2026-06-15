"""Step 1: the LLM backend seam. Only the fake is unit-tested (others are
native/network)."""

import pytest

from cortana.backends import FakeLLMBackend, make_backend


def test_fake_backend_generate_returns_canned_and_counts():
    be = FakeLLMBackend(response="canned summary")
    assert be.calls == 0
    out = be.generate("any prompt")
    assert out == "canned summary"
    assert be.calls == 1
    be.generate("again")
    assert be.calls == 2


def test_fake_backend_has_model_name():
    be = FakeLLMBackend(response="x", model="fake-7b")
    assert be.model == "fake-7b"


def test_make_backend_fake():
    be = make_backend("fake")
    assert isinstance(be, FakeLLMBackend)


def test_make_backend_unknown_raises():
    with pytest.raises(ValueError):
        make_backend("nope")
