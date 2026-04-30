import json

from copilot_cli.backend.substrate_capture import (
    RECORD_SEP,
    extract_assistant_text,
    extract_write_at_cursor,
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


def test_extract_write_at_cursor_returns_chunk():
    """Reproduces the live-Copilot streaming format observed 2026-04: an
    initial messages[].text frame followed by writeAtCursor chunks."""
    rec = {"type": 1, "target": "update", "arguments": [{"writeAtCursor": " is", "nonce": "x"}]}
    assert extract_write_at_cursor(rec) == " is"


def test_extract_write_at_cursor_returns_none_when_absent():
    rec = {"type": 1, "arguments": [{"messages": [{"author": "bot", "text": "hello"}]}]}
    assert extract_write_at_cursor(rec) is None
    assert extract_write_at_cursor({"type": 6}) is None
    assert extract_write_at_cursor({}) is None


def test_extract_write_at_cursor_ignores_empty_string():
    rec = {"type": 1, "arguments": [{"writeAtCursor": ""}]}
    assert extract_write_at_cursor(rec) is None


def test_search_results_preamble_is_skipped():
    """Substrate sends a meta-message ('OK, I'll search for X...') with
    contentType=SearchResults BEFORE the actual reply starts streaming. We
    must not surface that as the assistant text or it ends up prepended to
    the real answer."""
    rec = {
        "type": 1,
        "arguments": [{"messages": [{
            "author": "bot",
            "contentType": "SearchResults",
            "text": "OK, I'll search for 'test'...",
        }]}],
    }
    assert extract_assistant_text(rec) is None


def test_references_list_message_is_skipped():
    rec = {
        "type": 1,
        "arguments": [{"messages": [{
            "author": "bot",
            "messageType": "ReferencesListCompletedMessage",
            "text": "[references payload]",
        }]}],
    }
    assert extract_assistant_text(rec) is None


def test_real_reply_with_response_identifier_is_returned():
    rec = {
        "type": 1,
        "arguments": [{"messages": [{
            "author": "bot",
            "responseIdentifier": "Default",
            "text": "This",
        }]}],
    }
    assert extract_assistant_text(rec) == "This"


def test_full_streaming_sequence_recovered():
    """End-to-end: bootstrap text + 3 writeAtCursor chunks + final replay
    should reassemble to the complete sentence."""
    bootstrap = {"type": 1, "arguments": [{"messages": [{"author": "bot", "text": "This"}]}]}
    chunks = [
        {"type": 1, "arguments": [{"writeAtCursor": " is"}]},
        {"type": 1, "arguments": [{"writeAtCursor": " a single short sentence to"}]},
        {"type": 1, "arguments": [{"writeAtCursor": " demonstrate streaming behavior."}]},
    ]
    final = {
        "type": 1,
        "arguments": [{"messages": [{"author": "bot", "text": "This is a single short sentence to demonstrate streaming behavior."}]}],
    }

    assembled = extract_assistant_text(bootstrap)
    for c in chunks:
        chunk = extract_write_at_cursor(c)
        assert chunk is not None
        assembled += chunk
    final_full = extract_assistant_text(final)
    assert assembled == final_full == "This is a single short sentence to demonstrate streaming behavior."


def test_live_protocol_regression():
    """Replays a curated 7-frame sequence captured live from M365 Copilot
    on 2026-04-30 (see probe_artifacts/websocket_frames.jsonl). Guards
    against the parser regressing on:
      - SearchResults pre-amble bleeding into the answer
      - writeAtCursor chunks being silently dropped
      - the final replay frame being missed
    """
    captured = [
        # 1. throttling — no text payload, must be a no-op
        {"type": 1, "target": "update", "arguments": [
            {"nonce": "Q1QOJVroyg", "requestId": "abc", "throttling": {"maxNumUserMessagesInConversation": 600}},
        ]},
        # 2. search-results banner — meta, must be skipped
        {"type": 1, "target": "update", "arguments": [
            {"messages": [{"author": "bot", "contentType": "SearchResults", "text": "OK, I'll search for 'test'..."}]},
        ]},
        # 3. bootstrap reply token "This" with cursor + responseIdentifier=Default
        {"type": 1, "target": "update", "arguments": [
            {"cursor": {"j": "$['m'].text", "p": -1},
             "messages": [{"author": "bot", "responseIdentifier": "Default", "text": "This"}]},
        ]},
        # 4-6. writeAtCursor incremental chunks
        {"type": 1, "target": "update", "arguments": [{"writeAtCursor": " is", "nonce": "n1"}]},
        {"type": 1, "target": "update", "arguments": [{"writeAtCursor": " a single short sentence to", "nonce": "n2"}]},
        {"type": 1, "target": "update", "arguments": [{"writeAtCursor": " demonstrate streaming behavior.", "nonce": "n3"}]},
        # 7. references-list message — meta, must be skipped
        {"type": 1, "target": "update", "arguments": [
            {"messages": [{"author": "bot", "messageType": "ReferencesListCompletedMessage", "text": "[refs]"}]},
        ]},
        # 8. final replay
        {"type": 1, "target": "update", "arguments": [
            {"messages": [{"author": "bot", "responseIdentifier": "Default",
                           "text": "This is a single short sentence to demonstrate streaming behavior."}]},
        ]},
        # 9. end-of-turn
        {"type": 2, "invocationId": "0", "item": {"messages": []}},
    ]

    emitted: list[tuple[str, str]] = []
    saw_end = False
    for rec in captured:
        if is_end_record(rec):
            saw_end = True
            continue
        if rec.get("type") != 1:
            continue
        chunk = extract_write_at_cursor(rec)
        if chunk is not None:
            emitted.append(("chunk", chunk))
            continue
        text = extract_assistant_text(rec)
        if text:
            emitted.append(("full", text))

    assert saw_end is True
    # The throttling, SearchResults, and ReferencesList frames must NOT have
    # produced any emission. Only the actual reply frames should be present.
    assert emitted == [
        ("full", "This"),
        ("chunk", " is"),
        ("chunk", " a single short sentence to"),
        ("chunk", " demonstrate streaming behavior."),
        ("full", "This is a single short sentence to demonstrate streaming behavior."),
    ]
