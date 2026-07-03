"""Unit: the REST chat input contract (attachment-only messages are valid).

The HTTP fallback must accept the same shapes the WS path does: text-only,
attachment-only (empty text), or both — but never both empty (422). client_msg_id is
optional and bounded so the widget's idempotency key survives the degraded channel too.
"""

import pytest
from pydantic import ValidationError

from app.api.chat.rest import MessageIn

_ATT = [{"url": "/api/chat/media/x.png", "name": "x.png", "kind": "image"}]


def test_text_only_message_is_valid():
    m = MessageIn(text="退货政策是什么？")
    assert m.text and m.attachments == []


def test_attachment_only_message_is_valid():
    m = MessageIn(text="", attachments=_ATT)
    assert m.text == "" and m.attachments == _ATT


def test_missing_text_with_attachments_is_valid():
    m = MessageIn(attachments=_ATT)  # text omitted entirely
    assert m.text == ""


def test_both_empty_rejected():
    with pytest.raises(ValidationError):
        MessageIn(text="", attachments=[])


def test_whitespace_text_without_attachments_rejected():
    with pytest.raises(ValidationError):
        MessageIn(text="   ")


def test_client_msg_id_accepted_and_bounded():
    m = MessageIn(text="hi", client_msg_id="c-123")
    assert m.client_msg_id == "c-123"
    with pytest.raises(ValidationError):
        MessageIn(text="hi", client_msg_id="x" * 65)
