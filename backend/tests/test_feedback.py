import pytest

import feedback

# Cases written by a second model (Gemini) to catch what the patterns miss. Each must lose everything listed
# in "gone" with the patterns alone (no AI), and keep what developers need.
PATTERN_CASES = [
    ("Authorization: Bearer grn_8f3k29xLa009zPq14vB71kc\nError: Ollama isn't reachable at localhost:11434\nModel: qwen2.5:7b",
     ["grn_8f3k29xLa009zPq14vB71kc"], ["Ollama isn't reachable at localhost:11434", "qwen2.5:7b"]),
    ("Active token: sk-proj-99aA8bC4471049ff82bA11bcD4ee9001XYZa894\nModel: whisper-large-v3-turbo\nStatus: Transcribing",
     ["sk-proj-99aA8bC4471049ff82bA11bcD4ee9001XYZa894"], ["whisper-large-v3-turbo", "Transcribing"]),
    ("Follow up with partner at sarah.chen@sequoiacap.com regarding terms.\n[Button: Summarize] v1.4.2",
     ["sarah.chen@sequoiacap.com", "sequoiacap"], ["Summarize", "v1.4.2"]),
    ("2026-10-09 11:57:00 [ERROR] Failed to load: /Users/jsmith/Library/Application Support/Trailmix/trailmix.db: permission denied",
     ["jsmith"], ["2026-10-09 11:57:00", "[ERROR]", "/Library/Application Support/Trailmix/trailmix.db: permission denied"]),
    ("reach out to our support desk at +44 20 7946 0958 immediately.\nDuration: 1:48:58",
     ["+44 20 7946 0958", "7946"], ["Duration: 1:48:58"]),
    ("call me on (541) 555-0199 or 541.555.0199 tomorrow at 10:30",
     ["555-0199", "555.0199"], ["10:30"]),
    ("Venue booked at 742 Evergreen Terrace, Springfield, OR 97477 for kickoff.\nStatus: Waiting for memory",
     ["742 Evergreen Terrace", "97477"], ["Waiting for memory"]),
    ("Meeting URL: https://zoom.us/j/98234110921?pwd=V1k5b2c0YjFpZ1Q1dz09\nModel: whisper-large-v3-turbo\nDuration: 00:45:10",
     ["zoom.us/j", "98234110921", "V1k5b2c0YjFpZ1Q1dz09"], ["whisper-large-v3-turbo", "Duration: 00:45:10"]),
    ("2026-10-09 11:57:00 [FATAL] Node sync timed out contacting relay 100.101.12.7:8080.\nError: Ollama isn't reachable at localhost:11434",
     ["100.101.12.7"], ["2026-10-09 11:57:00", "localhost:11434"]),
    ("Engine at http://127.0.0.1:8765/api/health, models from https://huggingface.co/mlx-community/whisper",
     ["huggingface.co/mlx-community"], ["http://127.0.0.1:8765/api/health"]),
]


@pytest.mark.parametrize("text, gone, kept", PATTERN_CASES)
def test_patterns_remove_contact_details_keys_and_places(text, gone, kept):
    out = feedback.scrub_patterns(text)
    for g in gone:
        assert g not in out, out
    for k in kept:
        assert k in out, out


def test_versions_times_dates_and_model_names_are_not_mistaken_for_phone_numbers():
    text = "Trailmix 0.9.0 · 2026-10-09 11:57:00 · 1:48:58 · qwen2.5:7b · 16 GB · 123 meetings · v1.4.2 · 00:45:10.250"
    assert feedback.scrub_patterns(text) == text


def test_known_names_go_whole_and_in_parts(monkeypatch):
    names = ["Dana Whitfield", "Cold Connect", "José Núñez", "Dana", "Whitfield", "José", "Núñez"]
    out = feedback.scrub_known("Dana Whitfield said José Núñez joined Cold Connect; Dana agreed. Danalytics is fine.", names)
    assert out == "[private] said [private] joined [private]; [private] agreed. Danalytics is fine."


def test_the_report_link_carries_title_and_text():
    url = feedback.issue_url("[Beta 0.9.0] [blocks me] Recording stops", "line one\nline two & more")
    assert url.startswith("https://github.com/TheRealEminem/trailmix/issues/new?title=%5BBeta%200.9.0%5D")
    assert "line%20one%0Aline%20two%20%26%20more" in url
