"""The streaming chat seam on the LLM backend contract. Only the fake is
unit-tested; Ollama/MLX chat streaming is native/network (pragma: no cover)."""

import pytest

from cortana.backends import FakeLLMBackend, Message, Role


def test_role_enum_is_the_single_source_of_valid_roles():
    assert {r.value for r in Role} == {"system", "user", "assistant"}


def test_role_coerces_wire_string_and_rejects_unknown():
    assert Role("user") is Role.USER
    with pytest.raises(ValueError):
        Role("robot")


def test_message_to_wire_round_trips():
    assert Message(Role.USER, "hi").to_wire() == {"role": "user", "content": "hi"}


def test_fake_chat_streams_tokens_that_reassemble_to_the_response():
    be = FakeLLMBackend(response="hello there friend")
    tokens = list(be.chat([Message(Role.USER, "hi")]))
    assert len(tokens) == 3                 # streamed token-by-token, not one blob
    assert "".join(tokens) == "hello there friend"


def test_fake_chat_counts_the_call():
    be = FakeLLMBackend(response="x")
    assert be.calls == 0
    list(be.chat([Message(Role.USER, "hi")]))
    assert be.calls == 1
