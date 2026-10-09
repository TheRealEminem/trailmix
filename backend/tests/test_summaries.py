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

    def fake_generate(pid, cfg, prompt, system=None, keep_alive=None):
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
