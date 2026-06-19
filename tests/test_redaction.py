"""Phase 5: precision redaction. Tuned for FEW false positives — match only
unambiguous secret shapes; leave benign text alone."""

from cortana.redaction import redact


# --- positives: real secrets are removed ------------------------------------ #

def test_redacts_private_key_block():
    text = ("notes\n-----BEGIN RSA PRIVATE KEY-----\nMIIabc123\n"
            "-----END RSA PRIVATE KEY-----\nmore")
    out = redact(text)
    assert "PRIVATE KEY" not in out or "REDACTED" in out
    assert "MIIabc123" not in out


def test_redacts_aws_access_key():
    assert "AKIA" not in redact("key=AKIAIOSFODNN7EXAMPLE here")


def test_redacts_openai_style_key():
    out = redact("export OPENAI_API_KEY=sk-abcdEFGH1234567890ijklMNOP")
    assert "sk-abcdEFGH1234567890ijklMNOP" not in out


def test_redacts_github_token():
    out = redact("token ghp_" + "a" * 36)
    assert "ghp_" not in out


def test_redacts_bearer_token():
    out = redact("Authorization: Bearer abcDEF123456ghiJKL789mno")
    assert "abcDEF123456ghiJKL789mno" not in out


def test_redacts_ssn():
    assert "123-45-6789" not in redact("ssn 123-45-6789 on file")


def test_redacts_luhn_valid_card():
    # canonical valid test cards (Visa, then MasterCard whose Luhn doubling exceeds 9)
    assert "4111111111111111" not in redact("card 4111 1111 1111 1111 exp")
    assert "5555555555554444" not in redact("card 5555 5555 5555 4444 exp")


# --- negatives: benign text is untouched (the false-positive guard) --------- #

def test_keeps_benign_text():
    benign = "Reviewing the Q2 budget; 1,234 rows; call ext 5551234; order #12345678."
    assert redact(benign) == benign


def test_keeps_non_luhn_card_like_number():
    # 16 digits but not a valid card number -> not a card, leave it.
    text = "tracking 4111111111111112 shipped"
    assert redact(text) == text


def test_keeps_phone_number():
    # phone is 3-3-4; only the 3-2-4 SSN shape is redacted.
    text = "call me at 555-123-4567"
    assert redact(text) == text


def test_empty_text():
    assert redact("") == ""
