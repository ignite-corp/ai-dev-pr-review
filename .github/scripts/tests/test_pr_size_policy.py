"""Tests for pr_size_policy.py: the size gate counts what the reviewers see (AT-3592).

The size gate used to compare GitHub's whole-PR additions+deletions, so
files `.github/lens-ignore` already keeps from the reviewers still counted:
aws-simple-deploy#234 was skipped as 10,763 lines while its reviewers would
have read 199. The count now subtracts files the rule file excludes at BOTH
the PR's base commit and its head: base, so a PR cannot add a rule and the
code it hides in the same change; head, so a PR that removes or narrows a
rule is counted for what the reviewers then receive.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pr_size_policy as policy

VENDOR_RULES = "frontend/vendor/ignite-design-system/**\n"
BOTH = {"base000": VENDOR_RULES, "head111": VENDOR_RULES}


def _file(path, additions, deletions, previous=None) -> policy.ChangedFile:
    return policy.ChangedFile(
        path=path, previous_path=previous, additions=additions, deletions=deletions
    )


def _like_234() -> list[policy.ChangedFile]:
    """The shape of aws-simple-deploy#234: a vendored tree plus its record."""
    return [
        _file("frontend/vendor/ignite-design-system/dist/index.js", 6000, 4564),
        _file("frontend/vendor-integrity/ignite-design-system/files.sha256", 90, 80),
        _file("frontend/vendor-integrity/ignite-design-system/SHA256SUMS", 1, 1),
        _file("frontend/src/components/NewServiceForm.test.js", 5, 4),
        _file("frontend/package-lock.json", 1, 1),
        _file("frontend/vendor/ignite-design-system/README.md", 8, 8),
    ]


def _measure(
    files,
    rules: str | None,
    *,
    head_differs: bool = False,
    head_rules: str | None = None,
    changed_files=None,
) -> policy.SizeMeasure:
    """Base and head carry the same rules unless `head_differs` says otherwise."""
    return policy.measure(
        additions=sum(f.additions for f in files),
        deletions=sum(f.deletions for f in files),
        changed_files=len(files) if changed_files is None else changed_files,
        files=files,
        base_rules=rules,
        head_rules=head_rules if head_differs else rules,
    )


def test_a_rule_the_head_removes_does_not_reduce_the_count():
    """The reverse bypass: the reviewers get what the head no longer excludes."""
    files = [
        _file(".github/lens-ignore", 0, 1),
        _file("frontend/vendor/ignite-design-system/big.txt", 5000, 0),
    ]

    measured = _measure(files, VENDOR_RULES, head_differs=True, head_rules=None)

    assert (measured.additions, measured.deletions) == (5000, 1)
    assert measured.excluded_paths == ()


def test_a_rule_the_head_narrows_counts_what_it_no_longer_excludes():
    files = [
        _file("frontend/vendor/ignite-design-system/dist/a.js", 100, 0),
        _file("frontend/vendor/ignite-design-system/src/b.js", 200, 0),
    ]

    measured = _measure(
        files,
        VENDOR_RULES,
        head_differs=True,
        head_rules="frontend/vendor/ignite-design-system/dist/**\n",
    )

    assert (measured.additions, measured.deletions) == (200, 0)
    assert measured.excluded_paths == (
        "frontend/vendor/ignite-design-system/dist/a.js",
    )


# ------------------------------------------------------------------ counting


def test_a_vendor_bump_is_measured_by_its_non_excluded_lines_only():
    measured = _measure(_like_234(), VENDOR_RULES)

    assert (measured.additions, measured.deletions) == (97, 86)
    assert (measured.github_additions, measured.github_deletions) == (6105, 4658)
    assert measured.excluded_lines == 6105 + 4658 - 97 - 86
    assert measured.excluded_paths == (
        "frontend/vendor/ignite-design-system/dist/index.js",
        "frontend/vendor/ignite-design-system/README.md",
    )
    assert measured.fallback == ""


def test_no_rule_file_at_base_counts_every_line():
    measured = _measure(_like_234(), None)

    assert (measured.additions, measured.deletions) == (6105, 4658)
    assert measured.excluded_paths == ()
    assert measured.fallback == ""


def test_a_rename_is_excluded_when_only_its_old_path_matches():
    files = [
        _file(
            "frontend/elsewhere/index.js",
            10,
            3,
            previous="frontend/vendor/ignite-design-system/index.js",
        ),
        _file("src/app.py", 1, 0),
    ]

    measured = _measure(files, VENDOR_RULES)

    assert (measured.additions, measured.deletions) == (1, 0)
    assert measured.excluded_paths == (
        "frontend/vendor/ignite-design-system/index.js -> frontend/elsewhere/index.js",
    )


def test_a_rename_is_excluded_when_only_its_new_path_matches():
    files = [
        _file(
            "frontend/vendor/ignite-design-system/index.js",
            10,
            3,
            previous="src/index.js",
        )
    ]

    measured = _measure(files, VENDOR_RULES)

    assert (measured.additions, measured.deletions) == (0, 0)
    assert measured.excluded_lines == 13


def test_the_rule_file_itself_is_always_counted():
    files = [_file(".github/lens-ignore", 40, 0), _file("src/a.py", 1, 0)]

    measured = _measure(files, ".github/**\n")

    assert (measured.additions, measured.deletions) == (41, 0)
    assert measured.excluded_paths == ()


# ----------------------------------------------------- fail closed: full count


def test_more_files_than_the_api_lists_falls_back_to_the_full_count():
    files = _like_234()

    measured = _measure(files, VENDOR_RULES, changed_files=policy.FILES_API_CAP + 1)

    assert (measured.additions, measured.deletions) == (6105, 4658)
    assert measured.excluded_paths == ()
    assert str(policy.FILES_API_CAP) in measured.fallback


def test_an_incomplete_file_list_falls_back_to_the_full_count():
    files = _like_234()

    measured = _measure(files, VENDOR_RULES, changed_files=len(files) + 1)

    assert (measured.additions, measured.deletions) == (6105, 4658)
    assert measured.fallback


def test_per_file_counts_that_disagree_with_the_pr_fall_back_to_the_full_count():
    files = _like_234()

    measured = policy.measure(
        additions=9000,
        deletions=4658,
        changed_files=len(files),
        files=files,
        base_rules=VENDOR_RULES,
        head_rules=VENDOR_RULES,
    )

    assert (measured.additions, measured.deletions) == (9000, 4658)
    assert measured.fallback


def test_an_unavailable_file_list_falls_back_to_the_full_count():
    measured = policy.measure(
        additions=100,
        deletions=5,
        changed_files=2,
        files=None,
        base_rules=VENDOR_RULES,
        head_rules=VENDOR_RULES,
        fallback="file list unreadable",
    )

    assert (measured.additions, measured.deletions) == (100, 5)
    assert measured.fallback == "file list unreadable"


# ------------------------------------------------------- reading from GitHub


class FakeGh:
    """`gh api` answers keyed by the endpoint; records every call."""

    def __init__(
        self,
        *,
        pr: dict[str, Any],
        files: list[dict[str, Any]],
        rules_by_ref: dict[str, str],
        files_error: bool = False,
    ):
        self.pr = pr
        self.files = files
        self.rules_by_ref = rules_by_ref
        self.files_error = files_error
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        endpoint = next(a for a in args if a.startswith("repos/"))
        if "/files" in endpoint:
            if self.files_error:
                return 1, "", "HTTP 502"
            return 0, "".join(json.dumps(f) + "\n" for f in self.files), ""
        if "/contents/" in endpoint:
            ref = next(a for a in args if a.startswith("ref=")).removeprefix("ref=")
            if ref not in self.rules_by_ref:
                return 1, "", "gh: Not Found (HTTP 404)"
            content = base64.b64encode(self.rules_by_ref[ref].encode()).decode()
            return 0, json.dumps({"encoding": "base64", "content": content}), ""
        return 0, json.dumps(self.pr), ""


def _api_files(files: list[policy.ChangedFile]) -> list[dict[str, Any]]:
    out = []
    for f in files:
        entry: dict[str, Any] = {
            "filename": f.path,
            "additions": f.additions,
            "deletions": f.deletions,
        }
        if f.previous_path:
            entry["previous_filename"] = f.previous_path
        out.append(entry)
    return out


def _pr(files: list[policy.ChangedFile], *, base_sha="base000", head_sha="head111"):
    return {
        "additions": sum(f.additions for f in files),
        "deletions": sum(f.deletions for f in files),
        "changed_files": len(files),
        "base": {"sha": base_sha},
        "head": {"sha": head_sha},
    }


def test_rules_are_read_from_the_base_and_the_head_commit():
    files = _like_234()
    gh = FakeGh(pr=_pr(files), files=_api_files(files), rules_by_ref=BOTH)

    measured = policy.measure_pr("o/r", "234", gh)

    assert (measured.additions, measured.deletions) == (97, 86)
    refs = {
        a
        for c in gh.calls
        if any("/contents/" in a for a in c)
        for a in c
        if a.startswith("ref=")
    }
    assert refs == {"ref=base000", "ref=head111"}


def test_a_base_rule_the_head_deleted_does_not_reduce_the_count():
    files = [
        _file(".github/lens-ignore", 0, 1),
        _file("frontend/vendor/ignite-design-system/big.txt", 5000, 0),
    ]
    gh = FakeGh(
        pr=_pr(files), files=_api_files(files), rules_by_ref={"base000": VENDOR_RULES}
    )

    measured = policy.measure_pr("o/r", "9", gh)

    assert (measured.additions, measured.deletions) == (5000, 1)
    assert measured.excluded_paths == ()


def test_a_rule_that_exists_only_at_the_head_does_not_reduce_the_count():
    """The bypass: one PR adds a rule and the code it would hide."""
    files = [
        _file(".github/lens-ignore", 1, 0),
        _file("src/hidden/payload.py", 5000, 0),
    ]
    gh = FakeGh(
        pr=_pr(files),
        files=_api_files(files),
        rules_by_ref={"head111": "src/hidden/**\n"},
    )

    measured = policy.measure_pr("o/r", "9", gh)

    assert (measured.additions, measured.deletions) == (5001, 0)
    assert measured.excluded_paths == ()


def test_an_overridden_rule_path_is_the_one_read_and_never_excluded():
    """The local driver's LENS_IGNORE_PATH: the gate reads what the filter reads."""
    files = [
        _file("ops/review-ignore", 1, 0),
        _file("frontend/vendor/ignite-design-system/big.txt", 5000, 0),
    ]
    gh = FakeGh(pr=_pr(files), files=_api_files(files), rules_by_ref=BOTH)

    measured = policy.measure_pr("o/r", "9", gh, rule_path="ops/review-ignore")

    contents = [a for c in gh.calls for a in c if "/contents/" in a]
    assert contents and all(a.endswith("/contents/ops/review-ignore") for a in contents)
    assert (measured.additions, measured.deletions) == (1, 0)


def test_the_overridden_rule_file_itself_always_counts():
    files = [_file("ops/review-ignore", 40, 0), _file("src/a.py", 1, 0)]

    measured = policy.measure(
        additions=41,
        deletions=0,
        changed_files=2,
        files=files,
        base_rules="ops/**\n",
        head_rules="ops/**\n",
        rule_path="ops/review-ignore",
    )

    assert (measured.additions, measured.deletions) == (41, 0)


def test_a_file_list_error_falls_back_to_the_full_count():
    files = _like_234()
    gh = FakeGh(
        pr=_pr(files),
        files=_api_files(files),
        rules_by_ref=BOTH,
        files_error=True,
    )

    measured = policy.measure_pr("o/r", "234", gh)

    assert (measured.additions, measured.deletions) == (6105, 4658)
    assert measured.fallback


def test_over_the_cap_the_file_list_is_not_even_requested():
    files = _like_234()
    pr = _pr(files) | {"changed_files": policy.FILES_API_CAP + 1}
    gh = FakeGh(pr=pr, files=_api_files(files), rules_by_ref=BOTH)

    measured = policy.measure_pr("o/r", "234", gh)

    assert (measured.additions, measured.deletions) == (6105, 4658)
    assert str(policy.FILES_API_CAP) in measured.fallback
    assert not any("/files" in " ".join(c) for c in gh.calls)


def test_the_cli_prints_the_measure_as_json(monkeypatch, capsys):
    files = _like_234()
    gh = FakeGh(pr=_pr(files), files=_api_files(files), rules_by_ref=BOTH)
    monkeypatch.setattr(policy, "gh_api", gh)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("PR_NUMBER", "234")

    policy.main()

    printed = json.loads(capsys.readouterr().out)
    assert printed["additions"] == 97
    assert printed["deletions"] == 86
    assert printed["github_additions"] == 6105
    assert printed["excluded_lines"] == 6105 + 4658 - 97 - 86
    assert printed["excluded_files"] == 2
    assert printed["fallback"] == ""
