"""Tests for the consumer pin freshness judgement (AT-2007).

The fixture mirrors the real release history of this repo as of 2026-08-25,
including the floating ``v1`` tag that shares a commit with v1.5.0, so the
acceptance case (the v1.3.0 commit two MINORs behind) is exercised against
data the workflow actually sees.
"""

import json
import subprocess
import sys
from pathlib import Path

import pin_freshness

SCRIPT = Path(pin_freshness.__file__)

V1_3_0 = "36f97d4b1e232448deac9a4649b005a69299ee4d"
V1_4_0 = "167a65f4fef0f3c47af85f3b2f7da17ea6f8deb4"
V1_5_0 = "a185d351c646187d58f187e04d74357658d41edc"

TAGS = [
    {"name": "v1", "sha": V1_5_0},
    {"name": "v1.5.0", "sha": V1_5_0},
    {"name": "v1.4.0", "sha": V1_4_0},
    {"name": "v1.3.0", "sha": V1_3_0},
    {"name": "v1.2.0", "sha": "6b00160e003f24937d3229df4dd883df71555af0"},
    {"name": "v1.1.0", "sha": "1" * 40},
    {"name": "v1.0.6", "sha": "2" * 40},
]


def judge(pin, tags=None):
    versions, by_sha = pin_freshness.build_index(tags if tags is not None else TAGS)
    return pin_freshness.classify(pin, versions, by_sha)


def test_sha_pin_two_minors_behind_is_flagged():
    ok, message = judge(V1_3_0)
    assert not ok
    assert "v1.3.0" in message
    assert "2 minor releases behind latest v1.5.0" in message


def test_sha_pin_on_latest_release_passes():
    ok, message = judge(V1_5_0)
    assert ok
    assert "v1.5.0" in message


def test_sha_pin_one_minor_behind_passes():
    # One release cycle of slack: flagging the day a release ships would make
    # the check noisy enough to be ignored.
    ok, message = judge(V1_4_0)
    assert ok
    assert "v1.4.0 is 1 minor release behind latest v1.5.0 (tolerance 1)" in message


def test_uppercase_sha_pin_resolves():
    ok, message = judge(V1_3_0.upper())
    assert not ok
    assert "v1.3.0" in message


def test_sha_pin_matching_no_release_is_flagged_distinctly():
    ok, message = judge("0" * 40)
    assert not ok
    assert "matches no release tag" in message
    assert "arbitrary commit" in message


def test_floating_major_tag_passes():
    ok, message = judge("v1")
    assert ok
    assert "tracks latest v1.5.0" in message


def test_floating_tag_of_an_older_major_is_flagged():
    tags = TAGS + [{"name": "v2.0.0", "sha": "3" * 40}]
    ok, message = judge("v1", tags)
    assert not ok
    assert "v2.0.0" in message


def test_version_pin_below_the_moving_floor_is_flagged():
    # The old hardcoded floor was v1.0.6, so this pin used to pass silently.
    ok, message = judge("v1.1.0")
    assert not ok
    assert "4 minor releases behind latest v1.5.0" in message


def test_version_pin_within_tolerance_passes():
    ok, _ = judge("v1.4.0")
    assert ok


def test_version_pin_naming_no_release_is_flagged():
    ok, message = judge("v1.9.9")
    assert not ok
    assert "matches no release tag" in message


def test_patch_lag_inside_an_accepted_minor_passes():
    tags = TAGS + [{"name": "v1.5.3", "sha": "4" * 40}]
    ok, message = judge("v1.5.0", tags)
    assert ok
    # The message reports the real distance, not the tolerance constant.
    assert "0 minor releases behind latest v1.5.3" in message


def test_classify_fails_closed_on_an_empty_index():
    ok, message = pin_freshness.classify("v1", {}, {})
    assert not ok
    assert message == pin_freshness.EMPTY_INDEX_MESSAGE


def test_branch_or_short_sha_pin_is_flagged():
    for pin in ("main", "36f97d4", "refs/heads/main"):
        ok, message = judge(pin)
        assert not ok
        assert "neither a release tag nor a commit SHA" in message


def test_empty_pin_is_flagged():
    ok, message = judge("")
    assert not ok
    assert "no reusable workflow pin found" in message


def test_floor_moves_with_the_release_history():
    older = [t for t in TAGS if t["name"] not in ("v1", "v1.5.0", "v1.4.0")]
    ok, message = judge(V1_3_0, older)
    assert ok, message


def test_build_index_prefers_the_highest_tag_on_a_shared_commit():
    _, by_sha = pin_freshness.build_index(TAGS)
    assert by_sha[V1_5_0] == "v1.5.0"


def test_load_entries_accepts_json_lines_and_arrays():
    lines = "\n".join(json.dumps(t) for t in TAGS)
    assert pin_freshness.load_entries(lines) == TAGS
    assert pin_freshness.load_entries(json.dumps(TAGS)) == TAGS
    assert pin_freshness.load_entries("  ") == []


def _run(args, stdin):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_exit_codes_and_output():
    stdin = json.dumps(TAGS)
    stale = _run(["--tags", "-", "--pin", V1_3_0], stdin)
    assert stale.returncode == 1
    assert "2 minor releases behind latest v1.5.0" in stale.stdout

    fresh = _run(["--tags", "-", "--pin", "v1"], stdin)
    assert fresh.returncode == 0
    assert "tracks latest v1.5.0" in fresh.stdout

    latest = _run(["--tags", "-", "--latest"], stdin)
    assert latest.returncode == 0
    assert latest.stdout.strip() == "v1.5.0"


def test_cli_fails_closed_on_an_empty_tag_list():
    empty = _run(["--tags", "-", "--latest"], "[]")
    assert empty.returncode == 1
    assert "empty" in empty.stdout

    unjudgeable = _run(["--tags", "-", "--pin", "v1"], "[]")
    assert unjudgeable.returncode == 1


def test_cli_output_never_leaks_non_public_strings():
    # Public-repo invariant: the message repeats only the pin and this repo's
    # own tags, so the caller can echo it under index-only reporting.
    result = _run(["--tags", "-", "--pin", V1_3_0], json.dumps(TAGS))
    for token in result.stdout.split():
        assert token.strip(",.()-") in {
            "commit",
            "pin",
            V1_3_0,
            "resolves",
            "to",
            "v1.3.0",
            "is",
            "2",
            "minor",
            "releases",
            "behind",
            "latest",
            "v1.5.0",
            "",
        }
