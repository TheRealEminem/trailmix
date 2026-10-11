from datetime import date

import meeting_date


def test_clues_keep_the_opening_and_lines_with_dates():
    lines = [f"[{i}:00] small talk {i}" for i in range(60)]
    lines[40] = "[40:00] the study session on December 15th"
    lines[50] = "[50:00] our work plan for 2026"
    picked = meeting_date.clues("\n".join(lines)).splitlines()
    assert picked[:3] == lines[:3]
    assert "[40:00] the study session on December 15th" in picked and "[50:00] our work plan for 2026" in picked
    assert "[45:00] small talk 45" not in picked


def test_clues_fit_the_budget():
    text = "\n".join(f"line {i} in December" for i in range(5000))
    assert len(meeting_date.clues(text, budget=2000)) <= 2000


def test_parse_reply():
    got = meeting_date.parse('Sure! {"date": "2025-11-17", "sure_of": "month", "why": "Mentions December 15th."}',
                             date(2026, 10, 10))
    assert got == {"date": "2025-11-17", "sure_of": "month", "why": "Mentions December 15th."}


def test_parse_rejects_nonsense_and_the_future():
    latest = date(2026, 10, 10)
    assert meeting_date.parse("no idea", latest)["date"] is None
    assert meeting_date.parse('{"date": "2031-01-01", "why": "x"}', latest)["date"] is None
    assert meeting_date.parse('{"date": "Nov 2025", "why": "x"}', latest)["date"] is None
    none = meeting_date.parse('{"date": null, "why": "No dates are mentioned"}', latest)
    assert none == {"date": None, "sure_of": None, "why": "No dates are mentioned"}


def test_a_future_guess_moves_back_to_the_last_time_that_day_came_round():
    latest = date(2026, 10, 10)
    got = meeting_date.parse('{"clues": ["Thanksgiving week"], "date": "2026-11-24", "sure_of": "day", '
                             '"why": "Thanksgiving week points to November 24, 2026"}', latest)
    assert got["date"] == "2025-11-24" and got["sure_of"] == "month" and "year" in got["why"]
    assert meeting_date.parse('{"date": "2026-10-11"}', latest)["date"] == "2025-10-11"
    assert meeting_date.parse('{"date": "2028-02-29"}', date(2028, 2, 1))["date"] == "2027-02-28"


def test_sure_of_the_day_needs_a_clue_naming_it():
    latest = date(2026, 10, 10)
    deadline = '{"clues": ["study session on December 15th"], "date": "2025-12-05", "sure_of": "day"}'
    assert meeting_date.parse(deadline, latest)["sure_of"] == "month"
    named = '{"clues": ["today is December 5th"], "date": "2025-12-05", "sure_of": "day"}'
    assert meeting_date.parse(named, latest)["sure_of"] == "day"
