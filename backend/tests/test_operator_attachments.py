"""Operator (坐席) attachments: /conversations/{id}/reply accepts only files uploaded for that
session, rebuilds the descriptor server-side, and pushes attachments to the customer."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.admin.conversations as conv
import app.services.takeover as tk
from app.core import storage


def _session(channel_type="web"):
    return SimpleNamespace(id=uuid.uuid4(), channel_type=channel_type, tenant_id=uuid.uuid4())


class _DB:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for o in self.added:
            o.id = o.id or uuid.uuid4()

    async def commit(self):
        self.commits += 1


@pytest.fixture
def wired(monkeypatch):
    session = _session()
    published = []

    async def _get(db, sid):
        return session

    async def _next_seq(db, s):
        return 7

    async def _publish(sid, payload):
        published.append(payload)

    monkeypatch.setattr(conv, "_get_session_or_404", _get)
    monkeypatch.setattr("app.services.conversation.next_seq", _next_seq)
    monkeypatch.setattr(tk, "publish", _publish)
    return SimpleNamespace(session=session, published=published, db=_DB())


def _att(key, **kw):
    return conv.ReplyAttachmentIn(key=key, name=kw.get("name", "a.png"),
                                  content_type=kw.get("ct", "image/png"), size=kw.get("size", 10))


async def test_reply_with_own_attachment_is_persisted_and_pushed(wired):
    key = f"agent/{wired.session.id}/abc.png"
    body = conv.ReplyIn(content="", attachments=[_att(key)])
    res = await conv.reply(str(wired.session.id), body, wired.db, user=None)

    expected = storage.attachment_descriptor(key, "a.png", "image/png", 10)
    assert res["attachments"] == [expected]
    msg = wired.db.added[0]
    assert msg.attachments == [expected] and msg.model == "human"
    assert wired.published[-1]["attachments"] == [expected]
    assert wired.db.commits == 1


@pytest.mark.parametrize(
    "key",
    [
        "chat/default/abc.png",            # customer upload, not this session
        f"agent/{uuid.uuid4()}/abc.png",  # another session's file
        "agent/../secret.png",
    ],
)
async def test_reply_rejects_foreign_keys(wired, key):
    body = conv.ReplyIn(content="hi", attachments=[_att(key)])
    with pytest.raises(HTTPException) as e:
        await conv.reply(str(wired.session.id), body, wired.db, user=None)
    assert e.value.status_code == 400
    assert not wired.published


async def test_reply_rejects_disallowed_type(wired):
    key = f"agent/{wired.session.id}/x.html"
    body = conv.ReplyIn(content="", attachments=[_att(key, ct="text/html")])
    with pytest.raises(HTTPException):
        await conv.reply(str(wired.session.id), body, wired.db, user=None)


async def test_reply_requires_text_or_attachment(wired):
    with pytest.raises(HTTPException) as e:
        await conv.reply(str(wired.session.id), conv.ReplyIn(content="  "), wired.db, user=None)
    assert e.value.status_code == 400


async def test_attachments_refused_on_unsupported_channel(wired):
    wired.session.channel_type = "feishu"
    key = f"agent/{wired.session.id}/abc.png"
    with pytest.raises(HTTPException) as e:
        await conv.reply(str(wired.session.id), conv.ReplyIn(attachments=[_att(key)]), wired.db, user=None)
    assert e.value.status_code == 400


async def test_text_reply_still_works_on_unsupported_channel(wired):
    wired.session.channel_type = "feishu"
    res = await conv.reply(str(wired.session.id), conv.ReplyIn(content="您好"), wired.db, user=None)
    assert res["ok"] and res["attachments"] == []
