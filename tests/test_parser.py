from copilot_cli.agent.parser import FinalAnswer, StreamingParser, ToolCall, strip_copilot_noise


def test_extracts_complete_tool_call():
    p = StreamingParser()
    p.feed('Sure. <tool_use>\n  <name>read_file</name>\n  <args>{"path": "a.txt"}</args>\n</tool_use>')
    ev = p.pop_complete()
    assert isinstance(ev, ToolCall)
    assert ev.name == "read_file"
    assert ev.args == {"path": "a.txt"}


def test_hides_partial_tool_use_from_visible_text():
    p = StreamingParser()
    p.feed("Let me check. <tool_use>\n  <name>read_file</name>")
    assert p.visible_text == "Let me check. "
    assert p.pop_complete() is None


def test_visible_text_when_no_tool():
    p = StreamingParser()
    p.feed("hello world")
    assert p.visible_text == "hello world"


def test_final_answer():
    p = StreamingParser()
    p.feed("All done. <final>summary text</final>")
    ev = p.pop_complete()
    assert isinstance(ev, FinalAnswer)
    assert ev.text == "summary text"


def test_args_with_code_fence_is_recovered():
    p = StreamingParser()
    p.feed('<tool_use><name>x</name><args>```json\n{"a": 1}\n```</args></tool_use>')
    ev = p.pop_complete()
    assert isinstance(ev, ToolCall)
    assert ev.args == {"a": 1}


def test_streaming_chunked_feed():
    p = StreamingParser()
    chunks = ['<tool', '_use><na', 'me>read_file</name><args>{"path":"x"}</args></tool', '_use>']
    for c in chunks:
        p.feed(c)
    ev = p.pop_complete()
    assert isinstance(ev, ToolCall)
    assert ev.name == "read_file"


def test_skip_malformed_example_block_and_find_real_one():
    """Models (especially Copilot) often quote the format as an example —
    'emit <tool_use>...</tool_use>' — and then emit a real call later.
    The parser must skip the example and find the real one, not crash.
    """
    p = StreamingParser()
    p.feed(
        "I'll respond with <tool_use>...</tool_use> like this. "
        '<tool_use><name>read_file</name><args>{"path":"a.txt"}</args></tool_use>'
    )
    ev = p.pop_complete()
    assert isinstance(ev, ToolCall)
    assert ev.name == "read_file"
    assert ev.args == {"path": "a.txt"}


def test_only_malformed_example_returns_none():
    p = StreamingParser()
    p.feed("Reply with <tool_use>...</tool_use> when ready.")
    assert p.pop_complete() is None


def test_args_with_trailing_extra_brace_is_recovered():
    """Models occasionally emit a stray '}' after the args object — usually
    when echoing a SEARCH/REPLACE block that ends with '>>>>>>> REPLACE"}}'.
    The brace-balanced extractor should recover the well-formed object."""
    p = StreamingParser()
    p.feed(
        '<tool_use><name>edit_file</name><args>'
        '{"path":"a.txt","edits":"x"}}</args></tool_use>'
    )
    ev = p.pop_complete()
    assert isinstance(ev, ToolCall)
    assert ev.name == "edit_file"
    assert ev.args == {"path": "a.txt", "edits": "x"}


def test_args_with_leading_prose_is_recovered():
    p = StreamingParser()
    p.feed(
        '<tool_use><name>read_file</name><args>'
        'here is the json: {"path":"a.txt"}'
        '</args></tool_use>'
    )
    ev = p.pop_complete()
    assert isinstance(ev, ToolCall)
    assert ev.args == {"path": "a.txt"}


def test_falls_through_to_final_when_only_malformed_tool_block():
    """If the only tool_use is a malformed example AND there's a real
    <final>, we should return the FinalAnswer."""
    p = StreamingParser()
    p.feed(
        "Quick demo of the format: <tool_use>...</tool_use>. "
        "<final>just answering directly</final>"
    )
    ev = p.pop_complete()
    assert isinstance(ev, FinalAnswer)
    assert ev.text == "just answering directly"


def test_strip_copilot_noise_chain_of_thought():
    """M365 Copilot wraps internal reasoning in Show**...**Hide. Strip it."""
    text = "Show**Considering file instructions**\nI'll read the file.**Hide<tool_use>"
    cleaned = strip_copilot_noise(text)
    assert "Show**" not in cleaned
    assert "**Hide" not in cleaned
    assert "<tool_use>" in cleaned


def test_strip_copilot_noise_code_interpreter_banner():
    text = (
        "Coding and executing```python\nprint('noop')\n```"
        '{"executedCode":"print(\'noop\')","result":"","status":"Success",'
        '"stdout":"noop\\n","stderr":"","outputFiles":[]}<tool_use>'
    )
    cleaned = strip_copilot_noise(text)
    assert "Coding and executing" not in cleaned
    assert "executedCode" not in cleaned
    assert "<tool_use>" in cleaned


def test_strip_copilot_noise_search_preamble_and_loading():
    text = "OK, I'll search for 'secret.txt'...\nLining things up...\nGenerating response\nactual content"
    cleaned = strip_copilot_noise(text)
    assert "search for" not in cleaned
    assert "Lining things up" not in cleaned
    assert "Generating response" not in cleaned
    assert "actual content" in cleaned


def test_visible_text_strips_noise_around_tool_use():
    p = StreamingParser()
    p.feed("Show**Thinking**Hide<tool_use><name>x</name><args>{}</args></tool_use>")
    # The full event still parses correctly.
    ev = p.pop_complete()
    assert isinstance(ev, ToolCall)
    # And the visible text we'd render shows nothing of the chain-of-thought.
    p2 = StreamingParser()
    p2.feed("Show**Thinking**Hidesomething before <tool_use>")
    assert "Show**" not in p2.visible_text
    assert "**Hide" not in p2.visible_text
