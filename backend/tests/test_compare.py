import compare

def test_words():
    assert compare.words("Hello, world! It's a test.") == ["hello", "world", "it's", "a", "test"]
    assert compare.words("") == []
    assert compare.words("   ") == []
    assert compare.words("!@#$") == []

def test_plain():
    segments = [{"text": " Hello "}, {"text": " world "}]
    assert compare.plain(segments) == "Hello world"
    assert compare.plain(None, "[00:00] A: Hello\n[00:01] B: world") == "Hello\nworld"
    assert compare.plain(None, "Just some text") == "Just some text"
    assert compare.plain([], "fallback") == "fallback"
    assert compare.plain(None, "") == ""

def test_speakers():
    segments = [{"speaker": "Alice"}, {"speaker": "Bob"}, {"speaker": "Alice"}, {"speaker": None}]
    assert compare.speakers(segments) == ["Alice", "Bob"]
    assert compare.speakers(None) == []
    assert compare.speakers([]) == []

def test_stats():
    segments = [{"text": "Hello", "speaker": "A"}, {"text": "world", "speaker": "B"}]
    assert compare.stats(segments, "") == {"words": 2, "lines": 2, "speakers": ["A", "B"]}

    assert compare.stats(None, "Line 1\nLine 2") == {"words": 4, "lines": 2, "speakers": []}
    assert compare.stats(None, "") == {"words": 0, "lines": 0, "speakers": []}

def test_diff():
    # Simple match
    assert compare.diff("a b c", "a b c")["agreement"] == 1.0

    # Differences
    d = compare.diff("a b c", "a d c")
    assert d["a_words"] == 3
    assert d["b_words"] == 3
    assert d["matched"] == 2
    assert d["agreement"] == round(2/3, 4)
    assert len(d["changes"]) == 1
    assert d["changes"][0] == {"op": "replace", "a": "b", "b": "d", "at": 1}

    # Insertions and deletions
    d2 = compare.diff("a b", "a x b")
    assert len(d2["changes"]) == 1
    assert d2["changes"][0]["op"] == "insert"

    d3 = compare.diff("a x b", "a b")
    assert len(d3["changes"]) == 1
    assert d3["changes"][0]["op"] == "delete"

    # Empty
    assert compare.diff("", "")["agreement"] == 0.0
    assert compare.diff("a", "")["agreement"] == 0.0

def test_inline():
    # Identical
    assert compare.inline("a b", "a b") == [{"op": "equal", "text": "a b"}]

    # Simple replace
    assert compare.inline("a b c", "a x c") == [
        {"op": "equal", "text": "a"},
        {"op": "delete", "text": "b"},
        {"op": "insert", "text": "x"},
        {"op": "equal", "text": "c"}
    ]

    # Chunk skipping
    long_text = " ".join([f"word{i}" for i in range(150)])
    a_text = f"start diff {long_text} middle a end diff2"
    b_text = f"start diffx {long_text} middle b end diff2x"

    res = compare.inline(a_text, b_text)
    # Check that a skip is present
    has_skip = any(op["op"] == "skip" for op in res)
    assert has_skip

    # Empty
    assert compare.inline("", "") == []
    assert compare.inline("a", "") == [{"op": "delete", "text": "a"}]
    assert compare.inline("", "b") == [{"op": "insert", "text": "b"}]
