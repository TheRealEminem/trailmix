import json

import meeting_text


def test_your_notes_reach_the_ai_as_markdown_lines_with_their_moment():
    m = {"my_notes_json": json.dumps([{"t": 65, "text": "# Budget"}, {"t": 70, "text": "- ask Sam"},
                                      {"t": 72, "text": "  - by Friday"}, {"t": None, "text": "later thought "},
                                      {"t": 80, "text": "   "}])}
    assert meeting_text.my_notes_lines(m) == ["[01:05] # Budget", "[01:10] - ask Sam", "  [01:12] - by Friday",
                                              "later thought"]
