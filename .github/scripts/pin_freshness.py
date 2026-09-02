#!/usr/bin/env python3
"""Judge whether a consumer's pin to this repo's reusable workflows is current.

consumer-health.yml used to accept any 40-hex SHA as "immutable, therefore
fine". Immutable is not the same as current: a SHA pin never expires on its
own, so three consumers sat on the v1.3.0 commit for weeks while the nightly
check reported green and two released fixes (v1.4.0 fail-closed status
contract, v1.5.0 bot-guard unblock) never reached them. This helper closes that
hole by resolving a SHA back to the release it belongs to and then applying the
same currency rule a version pin gets.

Currency rule (MINOR tolerance)
    A pin is current when it names the latest release or the one MINOR before
    it, within the latest MAJOR. Requiring the exact latest would flag every
    consumer the day a release ships; a check that is red by default is a check
    people stop reading, which is the failure this ticket exists to fix. One
    MINOR of slack gives a consumer a full release cycle to update, while
    anything two MINORs back has demonstrably missed a whole cycle -- that is
    rot, not lag. PATCH lag inside an accepted MINOR is not flagged: the
    tolerance is defined at MINOR granularity on purpose. Floating major tags
    are the one exception -- see below for why slack makes no sense there.

Unresolvable commit pins
    A SHA that matches no release tag is FLAGged, with wording distinct from
    the stale-release case. The check exists to answer "is this consumer
    receiving what we ship"; a commit that was never released is not something
    we ship, and its currency cannot be judged at all. Staying silent there
    would leave the original blind spot open under a new name -- any arbitrary
    commit would pass forever, exactly as any SHA did before.

Floating major tags
    A ``vN`` pin is judged by the commit it points at, never by its name. The
    name only ever proves which major the consumer asked for; whether the alias
    still stands on the newest release is a separate fact, and move-major-tag.yml
    fires on ``release: published`` rather than on tag push, so a tag pushed
    without a published release (or a failed run of that workflow) leaves ``vN``
    on the previous commit while every ``@vN`` consumer keeps running the old
    code. Judging the name alone reported that state as current -- the same
    blind spot AT-2007 closed for SHA pins, where the notation was checked and
    the content was not.

    The MINOR tolerance deliberately does not apply here. It exists to give a
    human a release cycle to bump a pin they maintain by hand; nobody maintains
    ``vN`` by hand, so a float that lags at all means the automation did not
    run, and that is precisely what this check has to surface. A float on a
    commit that matches no release, or missing from the tag list entirely, is
    FLAGged for the same reason a SHA that resolves to nothing is: an
    unresolvable pin cannot be called current.

Version floor
    There is no hardcoded floor. The floor is derived from the newest release
    tag on every run, so it moves with the fleet instead of freezing at the
    value someone typed once (the old ``v1.0.6``).

Public-log safety: this script sees a pin and this repo's own tag list, never a
consumer name. Every string it prints is public information about the public
base repo, so the caller can echo its output verbatim under index-only
reporting.

Usage:
    pin_freshness.py --tags TAGS [--pin PIN | --latest]

TAGS is a file (``-`` for stdin) holding either a JSON array of
``{"name": ..., "sha": ...}`` objects or one such object per line, as produced
by ``gh api --paginate 'repos/OWNER/REPO/tags' --jq '...'``.

With --pin: prints a one-line verdict; exit 0 = current, 1 = flag.
With --latest: prints the newest release tag; exit 0, or 1 when the tag list
holds no release tag at all (fail closed -- the caller must not evaluate pins
against an empty index).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_VERSION_RE = re.compile(r"^v(\d+)\.(\d+)(?:\.(\d+))?$")
_FLOATING_TAG_RE = re.compile(r"^v(\d+)$")

# How many MINOR releases behind the newest a pin may sit and still pass.
MINOR_TOLERANCE = 1

EMPTY_INDEX_MESSAGE = "release tag list is empty; cannot judge pin freshness"

Version = tuple[int, int, int]
# One entry of the tags listing; values arrive from JSON, so only the keys are
# known to be strings.
TagEntry = Mapping[str, object]


def parse_version(tag: str) -> Version | None:
    """Return ``(major, minor, patch)`` for a release tag, else ``None``.

    A bare ``vN`` floating tag is not a release: it is an alias that moves, so
    it carries no version of its own and is handled separately.
    """
    m = _VERSION_RE.match(tag.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def build_index(
    entries: Sequence[TagEntry],
) -> tuple[dict[str, Version], dict[str, str], dict[str, str]]:
    """Build ``tag -> version``, ``sha -> tag`` and ``floating tag -> sha`` maps.

    Several tags can point at one commit (``v1`` and ``v1.5.0`` do, and a
    re-tagged release would too). The SHA map keeps the highest version, so a
    pin resolves to the most favourable release its commit represents.

    A floating ``vN`` tag has no version of its own, so it stays out of
    ``versions`` -- letting it in would make it a release and skew ``latest``.
    Its commit is kept in a third map instead: the SHA is the only evidence
    that the alias actually moved, and it arrives in the same listing, so no
    extra request is needed to obtain it.
    """
    versions: dict[str, Version] = {}
    by_sha: dict[str, str] = {}
    floating: dict[str, str] = {}
    for entry in entries:
        name = str(entry.get("name") or "")
        sha = str(entry.get("sha") or "").lower()
        version = parse_version(name)
        if version is None:
            if _FLOATING_TAG_RE.match(name) and _SHA_RE.match(sha):
                floating[name] = sha
            continue
        versions[name] = version
        if not _SHA_RE.match(sha):
            continue
        current = by_sha.get(sha)
        if current is None or versions[current] < version:
            by_sha[sha] = name
    return versions, by_sha, floating


def load_entries(raw: str) -> list[TagEntry]:
    """Parse a JSON array or JSON-lines tag listing into a list of dicts."""
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(parsed, dict):
        parsed = [parsed]
    return [item for item in parsed if isinstance(item, dict)]


def _format(version: Version) -> str:
    return "v%d.%d.%d" % version


def judge_release(tag: str, versions: dict[str, Version], latest: Version) -> tuple[bool, str]:
    """Apply the MINOR-tolerance currency rule to a resolved release tag."""
    version = versions[tag]
    if version >= latest:
        return True, "%s is the latest release" % tag
    if version[0] != latest[0]:
        return False, "%s is a major release behind latest %s" % (tag, _format(latest))
    gap = latest[1] - version[1]
    if gap <= MINOR_TOLERANCE:
        # Report the real distance, not the tolerance: a pin that is only
        # patch-behind is 0 minors behind, and saying otherwise overstates how
        # far a healthy consumer has drifted.
        return True, "%s is %d minor %s behind latest %s (tolerance %d)" % (
            tag,
            gap,
            "release" if gap == 1 else "releases",
            _format(latest),
            MINOR_TOLERANCE,
        )
    return False, "%s is %d minor releases behind latest %s" % (tag, gap, _format(latest))


def classify(
    pin: str,
    versions: dict[str, Version],
    by_sha: dict[str, str],
    floating_shas: dict[str, str],
) -> tuple[bool, str]:
    """Classify one pin against the release index. Returns ``(current, message)``."""
    pin = pin.strip()
    if not versions:
        # Fail closed here as well, so a direct caller gets the same verdict
        # shape as every other unjudgeable case instead of an exception.
        return False, EMPTY_INDEX_MESSAGE
    latest = max(versions.values())
    if not pin:
        return False, "no reusable workflow pin found"

    floating_match = _FLOATING_TAG_RE.match(pin)
    if floating_match:
        major = int(floating_match.group(1))
        if major != latest[0]:
            # The consumer named a major we no longer ship from. That is the
            # consumer's pin to fix, so it keeps its own wording.
            return False, "floating tag %s tracks major v%d, latest release is %s" % (
                pin,
                major,
                _format(latest),
            )
        sha = floating_shas.get(pin)
        if sha is None:
            return False, (
                "floating tag %s is absent from the tag list "
                "(cannot confirm what it points at; latest release is %s)"
                % (pin, _format(latest))
            )
        tag = by_sha.get(sha)
        if tag is None:
            return False, (
                "floating tag %s points at a commit that matches no release tag "
                "(latest release is %s)" % (pin, _format(latest))
            )
        if versions[tag] >= latest:
            return True, "floating tag %s resolves to %s -- %s is the latest release" % (
                pin,
                tag,
                tag,
            )
        return False, (
            "floating tag %s still resolves to %s; the major tag has not moved to "
            "latest %s" % (pin, tag, _format(latest))
        )

    if _SHA_RE.match(pin):
        tag = by_sha.get(pin.lower())
        if tag is None:
            return False, (
                "commit pin %s matches no release tag "
                "(arbitrary commit; latest release is %s)" % (pin, _format(latest))
            )
        current, message = judge_release(tag, versions, latest)
        return current, "commit pin %s resolves to %s -- %s" % (pin, tag, message)

    if _VERSION_RE.match(pin):
        if pin not in versions:
            return False, "pin %s matches no release tag (latest release is %s)" % (
                pin,
                _format(latest),
            )
        return judge_release(pin, versions, latest)

    return False, "pin %s is neither a release tag nor a commit SHA" % pin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tags", required=True, help="tag listing file, or - for stdin")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pin", help="the pin string found in a consumer workflow")
    group.add_argument(
        "--latest",
        action="store_true",
        help="print the newest release tag instead of judging a pin",
    )
    args = parser.parse_args(argv)

    if args.tags == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(args.tags).read_text(encoding="utf-8")
    versions, by_sha, floating_shas = build_index(load_entries(raw))
    if not versions:
        print(EMPTY_INDEX_MESSAGE)
        return 1

    if args.latest:
        print(_format(max(versions.values())))
        return 0

    current, message = classify(args.pin, versions, by_sha, floating_shas)
    print(message)
    return 0 if current else 1


if __name__ == "__main__":
    sys.exit(main())
