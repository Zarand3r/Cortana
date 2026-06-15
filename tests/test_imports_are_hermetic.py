"""P7: the package's pure logic imports with no PyObjC / Ollama installed."""


def test_imports_are_hermetic():
    import cortana.config  # noqa: F401
    import cortana.backends  # noqa: F401
    import cortana.perception  # noqa: F401
    import cortana.memory  # noqa: F401
