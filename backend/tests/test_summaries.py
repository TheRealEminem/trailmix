import json
import llm_engine
import meeting_text
import templates


def test_tidy_strips_preamble_fences_and_closers():
    raw = """Sure! Here are the meeting notes you asked for:

```markdown
## Overview
We planned the launch.

## Action Items
- [ ] Dana: fix the login bug (Wednesday)
```

Let me know if you'd like any changes!"""
    assert llm_engine.tidy(raw) == "## Overview\nWe planned the launch.\n\n## Action Items\n- [ ] Dana: fix the login bug (Wednesday)"


def test_tidy_drops_a_none_next_to_real_items():
    raw = "## Decisions\n- None\n\n## Action Items\n- [ ] Eli: send the deck\n- None"
    assert llm_engine.tidy(raw) == "## Decisions\n- None\n\n## Action Items\n- [ ] Eli: send the deck"


def test_tidy_keeps_text_without_headings():
    assert llm_engine.tidy("Just a paragraph.") == "Just a paragraph."


def test_split_lines_respects_the_limit_and_keeps_everything():
    text = "\n".join(f"[00:{i:02d}] You: line number {i}" for i in range(60))
    parts = llm_engine.split_lines(text, 200)
    assert all(len(p) <= 200 for p in parts)
    assert "\n".join(parts) == text


def test_instructions_come_after_the_transcript():
    system, prompt = templates.final_prompt("general", "", "[00:01] You: hello", False, [], "About.", 30)
    assert prompt.index("[00:01] You: hello") < prompt.index("Use these sections")
    assert "first heading" in prompt and system


def test_long_transcripts_are_summarized_in_parts(monkeypatch):
    calls = []

    def fake_generate(pid, cfg, prompt, system=None, keep_alive=None, on_text=None):
        calls.append(prompt)
        return "## Overview\nNotes.\n\n## Action Items\n- None" if "TRANSCRIPT (PART" not in prompt else "- point"

    monkeypatch.setattr(llm_engine, "generate", fake_generate)
    monkeypatch.setattr(llm_engine, "unload", lambda pid, cfg: None)
    monkeypatch.setattr(llm_engine, "context_chars", lambda pid, cfg: 5000)
    cfg = {"summary_provider": "ollama", "summary_fallback": "none", "custom_template": ""}
    transcript = "\n".join(f"[{i // 60:02d}:{i % 60:02d}] You: " + "word " * 30 for i in range(400))
    summary, used, title = llm_engine.summarize(transcript, cfg, "auto", "general", [], want_title=False)
    assert summary.startswith("## Overview") and used == "ollama"
    assert len(calls) > 2 and all(len(c) < 5000 + 3000 for c in calls)
    final = calls[-1]
    assert "NOTES ON [00:00] TO [" in final and "PART" not in final.split("---")[0]


def test_the_draft_shows_each_part_as_written_then_the_write_up(monkeypatch):
    def fake_generate(pid, cfg, prompt, system=None, keep_alive=None, on_text=None):
        reply = "- a point\n- another point" if "TRANSCRIPT (PART" in prompt else "## Overview\nWe met.\n\n## Action Items\n- None"
        for end in range(1, len(reply) + 1):  # streamed a character at a time
            on_text(reply[:end])
        return reply

    monkeypatch.setattr(llm_engine, "generate", fake_generate)
    monkeypatch.setattr(llm_engine, "unload", lambda pid, cfg: None)
    monkeypatch.setattr(llm_engine, "context_chars", lambda pid, cfg: 5000)
    drafts = []
    cfg = {"summary_provider": "ollama", "summary_fallback": "none", "custom_template": ""}
    transcript = "\n".join(f"[{i // 60:02d}:{i % 60:02d}] You: " + "word " * 30 for i in range(400))
    summary, _, _ = llm_engine.summarize(transcript, cfg, "auto", "general", [], want_title=False, on_draft=drafts.append)

    part_notes = [d for d in drafts if d.startswith("## Notes on")]
    assert part_notes[0].startswith("## Notes on 00:00 to ") and "PART" not in part_notes[-1]
    assert part_notes[-1].count("## Notes on") > 1  # earlier parts stay while the next one is written
    assert drafts[-1] == summary  # then the write-up, which ends as the finished notes


def test_ollama_streams_its_reply(monkeypatch):
    lines = ['{"response":"## Over"}', '{"response":"view\\nWe met."}', '{"response":"","done":true}']
    monkeypatch.setattr(llm_engine, "_stream_lines", lambda url, **kw: iter(lines))
    monkeypatch.setattr(llm_engine, "model_for", lambda pid, cfg: "qwen2.5:7b")
    seen = []
    text = llm_engine._ollama({"ollama_url": "http://x"}, "p", None, "0", on_text=seen.append)
    assert text == "## Overview\nWe met." and seen == ["## Over", "## Overview\nWe met."]


def test_ollama_stream_errors_fail_the_call(monkeypatch):
    monkeypatch.setattr(llm_engine, "_stream_lines", lambda url, **kw: iter(['{"error":"model ran out of memory"}']))
    monkeypatch.setattr(llm_engine, "model_for", lambda pid, cfg: "qwen2.5:7b")
    try:
        llm_engine._ollama({"ollama_url": "http://x"}, "p", None, "0", on_text=lambda t: None)
    except llm_engine.LLMError as e:
        assert "out of memory" in str(e)
    else:
        raise AssertionError("expected an LLMError")


def test_openai_compatible_streams_server_sent_events(monkeypatch):
    lines = [': keep-alive', 'data: {"choices":[{"delta":{"role":"assistant"}}]}',
             'data: {"choices":[{"delta":{"content":"Hello"}}]}', 'data: {"choices":[{"delta":{"content":" there"}}]}',
             'data: [DONE]', 'data: {"choices":[{"delta":{"content":"ignored"}}]}']
    monkeypatch.setattr(llm_engine, "_stream_lines", lambda url, **kw: iter(lines))
    seen = []
    text = llm_engine._openai_compatible("http://x/v1", "", "m", "p", None, "custom", on_text=seen.append)
    assert text == "Hello there" and seen == ["Hello", "Hello there"]


def test_gemini_streams_and_skips_thoughts(monkeypatch):
    lines = ['data: {"candidates":[{"content":{"parts":[{"text":"pondering","thought":true}]}}]}',
             'data: {"candidates":[{"content":{"parts":[{"text":"## Overview"}]}}]}',
             'data: {"candidates":[{"content":{"parts":[{"text":"\\nWe met."}]}}]}']
    monkeypatch.setattr(llm_engine, "_stream_lines", lambda url, **kw: iter(lines))
    text = llm_engine._gemini({"gemini_api_key": "k", "gemini_model": "g"}, "p", None, on_text=lambda t: None)
    assert text == "## Overview\nWe met."


def test_missing_action_items_are_asked_for(monkeypatch):
    replies = iter(["## Overview\nWe met.", "## Action Items\n- [ ] Dana: send the deck"])
    monkeypatch.setattr(llm_engine, "generate", lambda *a, **k: next(replies))
    monkeypatch.setattr(llm_engine, "unload", lambda pid, cfg: None)
    cfg = {"summary_provider": "ollama", "summary_fallback": "none", "custom_template": ""}
    summary, _, _ = llm_engine.summarize("[00:01] You: Dana will send the deck.", cfg, "auto", "general", [], want_title=False)
    assert summary == "## Overview\nWe met.\n\n## Action Items\n- [ ] Dana: send the deck"


def test_preferred_model_picks_an_instruction_model_over_alphabetical():
    tags = [
        {"name": "dolphin3:8b", "details": {"parameter_size": "8.0B"}},
        {"name": "nomic-embed-text:latest", "details": {"parameter_size": "137M"}},
        {"name": "qwen2.5:3b", "details": {"parameter_size": "3.1B"}},
        {"name": "qwen2.5:7b", "details": {"parameter_size": "7.6B"}},
    ]
    assert llm_engine.preferred_ollama_model(tags) == "qwen2.5:7b"
    assert llm_engine.preferred_ollama_model(tags[:1]) == "dolphin3:8b"


def test_turns_join_consecutive_lines():
    segs = [
        {"start": 0, "end": 2, "speaker": "You", "text": "Hi."},
        {"start": 2.5, "end": 4, "speaker": "You", "text": "Shall we start?"},
        {"start": 4.2, "end": 5, "speaker": "Them", "text": "Yes."},
        {"start": 12, "end": 13, "speaker": "Them", "text": "One more thing."},
    ]
    assert [t["text"] for t in meeting_text.turns(segs)] == ["Hi. Shall we start?", "Yes.", "One more thing."]


def test_excerpt_keeps_the_relevant_lines_in_order():
    lines = [f"[00:{i:02d}] You: filler talk number {i}" for i in range(50)]
    lines[30] = "[00:30] Them: the budget for the pilot is twelve thousand"
    text = meeting_text.excerpt("\n".join(lines), "What is the pilot budget?", 200)
    assert "twelve thousand" in text and len(text) <= 200 + 10
    assert text.index("[00:29]") < text.index("[00:30]") < text.index("[00:31]")


def test_flagged_moments_come_from_the_real_flags_not_the_model():
    import llm_engine

    written = ("## Flagged Moments\n- [ ] 00:00: Mark introduces Jeff.\n- [ ] 00:11: Mark mentions sunsets.\n\n"
               "## Overview\nThe commission met.\n\n## Action Items\n- [ ] Mark: send the agenda")
    notes = llm_engine.place_moments(written, ["[10:06] Them: Let's postpone the code amendment"])
    assert "introduces Jeff" not in notes
    assert notes.index("## Overview") < notes.index("## Flagged Moments") < notes.index("## Action Items")
    assert "- [10:06] Them: Let's postpone the code amendment" in notes
    assert "Flagged Moments" not in llm_engine.place_moments(written, [])  # no flags, no section


def test_your_own_notes_are_worked_in_and_their_links_kept(monkeypatch):
    prompts = []

    def fake_generate(pid, cfg, prompt, system=None, keep_alive=None, on_text=None):
        prompts.append(prompt)
        return "## Overview\nWe picked the venue.\n\n## Action Items\n- [ ] Mark: book it"

    monkeypatch.setattr(llm_engine, "generate", fake_generate)
    monkeypatch.setattr(llm_engine, "unload", lambda pid, cfg: None)
    cfg = {"summary_provider": "ollama", "summary_fallback": "none", "custom_template": ""}
    m = {"my_notes_json": json.dumps([{"t": 754, "text": "Venue deposit is $500, see https://example.com/venue."},
                                      {"t": None, "text": "  "}])}
    mine = meeting_text.my_notes_lines(m)
    assert mine == ["[12:34] Venue deposit is $500, see https://example.com/venue."]
    summary, _, _ = llm_engine.summarize("[00:01] You: Let's book the venue.", cfg, "auto", "general", [],
                                         want_title=False, mine=mine)
    assert "NOTES TYPED BY THE PERSON WHO RECORDED THE MEETING" in prompts[0] and "[12:34] Venue deposit is $500" in prompts[0]
    # the model left the note out, so it's kept word for word (link included)
    assert summary.endswith("## Your Notes\n- Venue deposit is $500, see https://example.com/venue.")


def test_links_already_in_the_notes_are_not_repeated():
    notes = "## Overview\nSee [the deck](https://example.com/deck)."
    assert llm_engine.keep_mine(notes, ["deck: https://example.com/deck"]) == notes


def test_notes_worked_in_are_not_repeated_but_missing_links_are_added():
    notes = "## Discussion\n- Mark wants pilot pricing at $2,000 per month."
    mine = ["[03:10] Pilot pricing: aim for $2,000/month. Deck: https://example.com/pilot-deck"]
    assert llm_engine.keep_mine(notes, mine) == notes + "\n\n## Links\n- https://example.com/pilot-deck"


def test_workspace_note_style_is_used_unless_one_was_picked(monkeypatch):
    import pipeline
    monkeypatch.setattr(pipeline.db, "get_workspace", lambda i: {"template": "minutes"})
    cfg = {"summary_template": "general"}
    assert pipeline._template({"workspace_id": 3, "requested_template": ""}, cfg) == "minutes"
    assert pipeline._template({"workspace_id": 3, "requested_template": "sales"}, cfg) == "sales"
    assert pipeline._template({"workspace_id": None, "requested_template": None}, cfg) == "general"
