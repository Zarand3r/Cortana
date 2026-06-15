"""Step 1: the LLM backend seam. Only the fake is unit-tested (others are
native/network)."""

import pytest

from cortana.backends import Backend, FakeLLMBackend, LLMBackend, make_backend


def test_backend_enum_is_the_single_source_of_valid_names():
    assert {b.value for b in Backend} == {"fake", "ollama", "mlx"}


def test_llmbackend_is_abstract():
    with pytest.raises(TypeError):
        LLMBackend()  # cannot instantiate the abstract base


def test_fake_backend_is_an_llmbackend():
    assert isinstance(FakeLLMBackend(), LLMBackend)


def test_make_backend_returns_llmbackend_type():
    be = make_backend("fake")
    assert isinstance(be, LLMBackend)


def test_make_backend_accepts_enum_member():
    assert isinstance(make_backend(Backend.FAKE), FakeLLMBackend)


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
