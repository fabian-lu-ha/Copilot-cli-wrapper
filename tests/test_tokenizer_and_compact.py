from copilot_cli.agent.transcript import Transcript
from copilot_cli.tokenizer import count_tokens


def test_count_tokens_nonempty():
    assert count_tokens("hello world") > 0
    assert count_tokens("") == 0


def test_count_tokens_scales_with_length():
    short = count_tokens("a")
    long = count_tokens("a " * 1000)
    assert long > short * 100


def test_transcript_compact_keeps_system_and_tail(tmp_path, monkeypatch):
    # Redirect sessions_dir to tmp.
    monkeypatch.setattr("copilot_cli.agent.transcript.sessions_dir", lambda: tmp_path)
    t = Transcript()
    t.add("system", "system prompt here")
    for i in range(10):
        t.add("user", f"user msg {i}")
        t.add("assistant", f"assistant reply {i}")
    pre_tokens = t.estimated_tokens()
    new_tokens = t.compact("SUMMARY: discussed N things", keep_last_turns=2)
    assert new_tokens < pre_tokens
    # First message is the original system prompt.
    assert t.messages[0].role == "system"
    assert t.messages[0].content == "system prompt here"
    # Second is the summary.
    assert t.messages[1].role == "system"
    assert "SUMMARY" in t.messages[1].content
    # Tail is the last 4 user/assistant messages (2 turns).
    tail_contents = [m.content for m in t.messages[-4:]]
    assert "user msg 8" in tail_contents
    assert "assistant reply 9" in tail_contents
    assert "user msg 0" not in tail_contents
