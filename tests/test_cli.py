"""The operations CLI no longer manages group containers."""
import sys

import pytest

from homeshield import cli


def test_cli_prints_personal_user_link(deps, monkeypatch, capsys):
    user = deps.repos.users.get_or_create("cli:test")
    monkeypatch.setattr(cli.Settings, "load", lambda: deps.settings)
    monkeypatch.setattr(cli, "build_deps", lambda settings: deps)
    monkeypatch.setattr(sys, "argv", ["homeshield-cli", "link", "--user-id", str(user.id),
                                      "--base-url", "https://shield.test"])
    cli.main()
    assert user.entry_url("https://shield.test") in capsys.readouterr().out


def test_group_admin_commands_are_retired(deps, monkeypatch):
    monkeypatch.setattr(cli.Settings, "load", lambda: deps.settings)
    monkeypatch.setattr(cli, "build_deps", lambda settings: deps)
    monkeypatch.setattr(sys, "argv", ["homeshield-cli", "add-group", "--name", "old"])
    with pytest.raises(SystemExit): cli.main()
