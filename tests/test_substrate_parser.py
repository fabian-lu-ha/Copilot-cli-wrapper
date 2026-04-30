import json

from copilot_cli.backend.substrate_capture import (
    RECORD_SEP,
    extract_assistant_text,
    is_end_record,
    is_substrate_url,
    parse_signalr_payload,
)


def _frame(*records: dict) -> str:
    return RECORD_SEP.join(json.dumps(r) for r in records) + RECORD_SEP


def test_substrate_url_match():
    assert is_substrate_url("wss://substrate.office.com/m365chat/SecuredChathub/abc")
    assert is_substrate_url("wss://example.cloud.microsoft/.../chathub")
    assert not is_substrate_url("wss://outlook.office.com/owa/something-else")
    assert not is_substrate_url("wss://login.microsoftonline.com/foo")


def test_parse_handshake_then_record():
    payload = "{}" + RECORD_SEP + json.dumps({"type": 6}) + RECORD_SEP
    records = list(parse_signalr_payload(payload))
    assert {"type": 6} in records


def test_parse_bytes_payload():
    payload = (json.dumps({"type": 1, "arguments": [{"text": "hi"}]}) + RECORD_SEP).encode()
    records = list(parse_signalr_payload(payload))
    assert records and records[0]["type"] == 1


def test_parse_multiple_records_in_one_frame():
    p = _frame({"type": 1, "arguments": [{"text": "a"}]}, {"type": 1, "arguments": [{"text": "ab"}]})
    records = list(parse_signalr_payload(p))
    assert len(records) == 2
    assert records[1]["arguments"][0]["text"] == "ab"


def test_parse_skips_invalid_json():
    p = "garbage" + RECORD_SEP + json.dumps({"type": 6}) + RECORD_SEP
    records = list(parse_signalr_payload(p))
    assert records == [{"type": 6}]


def test_extract_text_from_arguments_text():
    rec = {"type": 1, "arguments": [{"text": "hello world"}]}
    assert extract_assistant_text(rec) == "hello world"


def test_extract_text_from_arguments_messages_bot_author():
    rec = {
        "type": 1,
        "arguments": [
            {"messages": [
                {"author": "user", "text": "ignored"},
                {"author": "bot", "text": "answer"},
            ]}
        ],
    }
    assert extract_assistant_text(rec) == "answer"


def test_extract_text_from_assistant_role():
    rec = {
        "type": 1,
        "arguments": [{"messages": [{"role": "assistant", "content": "via role"}]}],
    }
    assert extract_assistant_text(rec) == "via role"


def test_extract_text_nested_item():
    rec = {
        "type": 1,
        "arguments": [{"item": {"messages": [{"author": "bot", "text": "deep"}]}}],
    }
    assert extract_assistant_text(rec) == "deep"


def test_extract_returns_none_when_no_text():
    rec = {"type": 6}
    assert extract_assistant_text(rec) is None
    rec2 = {"type": 1, "arguments": []}
    assert extract_assistant_text(rec2) is None


def test_is_end_record():
    assert is_end_record({"type": 2})
    assert is_end_record({"type": 3})
    assert not is_end_record({"type": 1})
    assert not is_end_record({"type": 6})


def test_user_messages_are_filtered_out():
    rec = {
        "type": 1,
        "arguments": [{"messages": [{"author": "user", "text": "what i asked"}]}],
    }
    assert extract_assistant_text(rec) is None
