from __future__ import annotations

from pathlib import Path

from homelab_deploy_mcp.target_wizard import _parse_existing, _render


def test_parse_empty_file_gives_empty_dicts(tmp_path: Path) -> None:
    targets, account_patterns = _parse_existing(tmp_path / "does-not-exist.conf")
    assert targets == {}
    assert account_patterns == {}


def test_render_then_parse_round_trips(tmp_path: Path) -> None:
    targets = {
        "mediaclipmakarr": {
            "dir": "/opt/targets/mediaclipmakarr",
            "branches": "main codex/*",
            "env_files": "prod.env staging.env",
        },
    }
    account_patterns = {"homelab-deploy-codex": "codex/*"}

    conf_path = tmp_path / "targets.conf"
    conf_path.write_text(_render(targets, account_patterns), encoding="utf-8")

    parsed_targets, parsed_accounts = _parse_existing(conf_path)
    assert parsed_targets == targets
    assert parsed_accounts == account_patterns


def test_adding_a_target_preserves_existing_ones(tmp_path: Path) -> None:
    conf_path = tmp_path / "targets.conf"
    existing = {
        "first": {"dir": "/opt/targets/first", "branches": "main", "env_files": "prod.env"},
    }
    conf_path.write_text(_render(existing, {}), encoding="utf-8")

    parsed_targets, parsed_accounts = _parse_existing(conf_path)
    parsed_targets["second"] = {
        "dir": "/opt/targets/second",
        "branches": "main",
        "env_files": "staging.env",
    }
    conf_path.write_text(_render(parsed_targets, parsed_accounts), encoding="utf-8")

    final_targets, _ = _parse_existing(conf_path)
    assert set(final_targets) == {"first", "second"}
    assert final_targets["first"] == existing["first"]


def test_preserves_account_patterns_the_wizard_does_not_manage(tmp_path: Path) -> None:
    conf_path = tmp_path / "targets.conf"
    conf_path.write_text(
        _render(
            {"onlytarget": {"dir": "/opt/targets/onlytarget", "branches": "main", "env_files": "prod.env"}},
            {"homelab-deploy-claude": "claude/*"},
        ),
        encoding="utf-8",
    )

    targets, account_patterns = _parse_existing(conf_path)
    # simulate re-running the wizard against the same target (regenerate)
    conf_path.write_text(_render(targets, account_patterns), encoding="utf-8")

    _, final_accounts = _parse_existing(conf_path)
    assert final_accounts == {"homelab-deploy-claude": "claude/*"}
