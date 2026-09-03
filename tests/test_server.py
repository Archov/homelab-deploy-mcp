from __future__ import annotations

from homelab_deploy_mcp.server import _branch_allowed


def test_exact_match_still_works() -> None:
    assert _branch_allowed("main", ("main", "feature/x"))
    assert not _branch_allowed("mainx", ("main",))
    assert not _branch_allowed("main", ("feature/x",))


def test_glob_pattern_matches_prefix() -> None:
    assert _branch_allowed("codex/my-feature", ("codex/*",))
    assert _branch_allowed("codex/my-feature", ("main", "codex/*"))


def test_glob_pattern_does_not_cross_match() -> None:
    assert not _branch_allowed("notcodex/my-feature", ("codex/*",))
    assert not _branch_allowed("codex", ("codex/*",))  # no trailing segment


def test_glob_pattern_is_case_sensitive() -> None:
    # fnmatch.fnmatch case-folds on Windows (via os.path.normcase), which
    # would make this pass on a Windows-hosted MCP server but not match
    # the Linux homelab host actually enforcing the same table -- must use
    # fnmatchcase instead, confirmed by this staying case-sensitive here.
    assert not _branch_allowed("Codex/Foo", ("codex/*",))
    assert not _branch_allowed("CODEX/foo", ("codex/*",))


def test_no_patterns_never_matches() -> None:
    assert not _branch_allowed("main", ())
