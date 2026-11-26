"""scripts/auto-promote.sh with a stand-in `gh`: frequent betas must not keep an older, tried-out one from going out."""
import json
import os
import shutil
import stat
import subprocess
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "auto-promote.sh"
pytestmark = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("jq")) or os.name == "nt",
                                reason="needs bash and jq")

GH = """#!/bin/bash
case "$1 $2" in
  "release list") cat "$FAKE/releases.json" ;;
  "issue list") cat "$FAKE/issues.tsv" ;;
  "release view") cat "$FAKE/assets-$3.txt" ;;
  "release edit") echo "$@" >> "$FAKE/edits.log" ;;
esac
"""


def run(tmp_path, betas: dict[str, float], stable="v0.18.2", issues="", assets=None, days="3"):
    """betas: tag -> days since published."""
    now = datetime.now(UTC)
    rel = [{"tagName": t, "isPrerelease": True, "isDraft": False,
            "publishedAt": (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")} for t, d in betas.items()]
    rel.append({"tagName": stable, "isPrerelease": False, "isDraft": False, "publishedAt": "2026-10-25T00:00:00Z"})
    (tmp_path / "releases.json").write_text(json.dumps(rel))
    (tmp_path / "issues.tsv").write_text(issues)
    for t in betas:
        v = t[1:]
        names = (assets or {}).get(t, [f"Trailmix-{v}-macos-arm64.dmg", f"Trailmix-{v}-windows-x64.exe"])
        (tmp_path / f"assets-{t}.txt").write_text("\n".join(names) + "\n")
    gh = tmp_path / "gh"
    gh.write_text(GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    out = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                         env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "FAKE": str(tmp_path),
                              "BETA_DAYS": days}).stdout
    log = tmp_path / "edits.log"
    return out, (log.read_text() if log.exists() else "")


def test_an_older_beta_that_has_waited_goes_out_while_a_newer_one_is_still_being_tried(tmp_path):
    out, edits = run(tmp_path, {"v0.23.7": 0.4, "v0.23.6": 1.9, "v0.23.2": 3.5})
    assert "release edit v0.23.2" in edits and "--latest" in edits
    assert "Promoted v0.23.2" in out


def test_the_newest_ready_beta_wins(tmp_path):
    _, edits = run(tmp_path, {"v0.23.7": 0.4, "v0.23.6": 4, "v0.23.2": 9})
    assert "release edit v0.23.6" in edits


def test_nothing_is_promoted_before_three_days(tmp_path):
    out, edits = run(tmp_path, {"v0.23.7": 0.4, "v0.23.6": 2.9})
    assert edits == "" and "Nothing to promote" in out


def test_a_blocks_me_report_skips_only_that_version(tmp_path):
    _, edits = run(tmp_path, {"v0.23.6": 4, "v0.23.2": 9}, issues="14\t[Beta 0.23.6] [blocks me] crash\n")
    assert "release edit v0.23.2" in edits


def test_a_beta_without_installers_is_skipped(tmp_path):
    _, edits = run(tmp_path, {"v0.23.6": 4, "v0.23.2": 9}, assets={"v0.23.6": ["notes.txt"]})
    assert "release edit v0.23.2" in edits


def test_a_beta_not_newer_than_the_release_is_not_promoted(tmp_path):
    out, edits = run(tmp_path, {"v0.18.1": 30}, stable="v0.18.2")
    assert edits == "" and "isn't newer" in out
