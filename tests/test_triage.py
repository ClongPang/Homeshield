import pytest

from homeshield.core.triage import ACK_TEXTS, classify


@pytest.mark.parametrize("text", sorted(ACK_TEXTS) + ["OK", "Ok", "okay", " ok "])
def test_exact_ack(text):
    assert classify(text) == "ack"


@pytest.mark.parametrize("text", [
    "好的，那我转了", "谢谢，但是有个事", "这个转账是真的吗",
    "谢谢 https://example.com", "好" * 21, "绑定 ABC123",
])
def test_uncertain_text_is_query(text):
    assert classify(text) == "query"


@pytest.mark.parametrize("text", ["新的", "新问题", " 新的 "])
def test_reset(text):
    assert classify(text) == "reset"
