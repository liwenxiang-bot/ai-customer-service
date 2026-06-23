"""Unit: channel adapters (web event rendering + WeChat crypto round-trip).

Guards the §2 constraint that swapping/adding channels doesn't touch the agent core:
the adapters are the only translation layer and must map events faithfully.
"""

import base64

from app.agent.events import AgentEvent
from app.channels.web import web_adapter
from app.channels.wechat import inbound_from_wechat
from app.channels.wechat_crypto import WXBizMsgCrypt, parse_message_xml


# ----------------------------------------------------------------- web
def test_web_renders_text_chunk():
    out = web_adapter.render_event(AgentEvent(kind="text", text="你好"))
    assert out == {"type": "stream_chunk", "delta": "你好"}


def test_web_renders_tool_status_with_label():
    out = web_adapter.render_event(AgentEvent(kind="tool_status", tool="search_knowledge", status="running"))
    assert out["type"] == "tool_status"
    assert out["status"] == "running"
    assert "知识库" in out["label"]


def test_web_renders_done_as_message_end():
    out = web_adapter.render_event(
        AgentEvent(kind="done", data={"message_id": "m1", "session_id": "s1", "citations": [], "degraded": False})
    )
    assert out["type"] == "message_end"
    assert out["message_id"] == "m1"
    assert out["session_id"] == "s1"


def test_web_unknown_event_returns_none():
    assert web_adapter.render_event(AgentEvent(kind="heartbeat")) is None


# ----------------------------------------------------------------- wechat
def _crypto() -> WXBizMsgCrypt:
    key = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()[:43]
    return WXBizMsgCrypt("tok", key, "corp1")


def test_wechat_crypto_round_trip():
    c = _crypto()
    plain = "<xml><MsgType><![CDATA[text]]></MsgType><Content><![CDATA[退货政策]]></Content></xml>"
    full = c.encrypt_message(plain, "nonce1", "1700000000")
    import xml.etree.ElementTree as ET

    node = ET.fromstring(full)
    encrypt = node.find("Encrypt").text
    sig = node.find("MsgSignature").text
    decrypted = c.decrypt_message(encrypt, sig, "1700000000", "nonce1")
    assert parse_message_xml(decrypted)["Content"] == "退货政策"


def test_wechat_bad_signature_rejected():
    c = _crypto()
    full = c.encrypt_message("<xml><Content><![CDATA[hi]]></Content></xml>", "n", "1700000000")
    import xml.etree.ElementTree as ET

    encrypt = ET.fromstring(full).find("Encrypt").text
    try:
        c.decrypt_message(encrypt, "wrongsig", "1700000000", "n")
        assert False, "should have raised"
    except Exception:
        pass


def test_wechat_inbound_only_text():
    assert inbound_from_wechat({"MsgType": "image"}) is None
    msg = inbound_from_wechat({"MsgType": "text", "Content": "你好", "FromUserName": "u1"})
    assert msg is not None
    assert msg.text == "你好" and msg.end_user_id == "u1"
