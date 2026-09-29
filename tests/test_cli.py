"""The operations CLI no longer manages group containers."""
import argparse
import sys
from types import SimpleNamespace

import pytest

from homeshield import cli
from homeshield.core.deps import build_deps


async def test_cli_prints_personal_user_link(deps, monkeypatch, capsys):
    user = await deps.repos.users.get_or_create("cli:test")
    monkeypatch.setattr(cli.Settings, "load", lambda: deps.settings)
    cli_deps = build_deps(deps.settings)
    monkeypatch.setattr(cli, "build_deps", lambda settings: cli_deps)
    args = argparse.Namespace(cmd="link", user_id=user.id, base_url="https://shield.test")
    await cli._run(args)
    assert user.entry_url("https://shield.test") in capsys.readouterr().out


async def test_cli_init_db_does_not_print_database_credentials(monkeypatch, capsys):
    class ClosedPool:
        async def close(self):
            pass

    database_url = "postgresql://user:secret@db.example.test/homeshield"
    settings = SimpleNamespace(database_url=database_url)
    deps = SimpleNamespace(settings=settings, pool=ClosedPool())

    async def initialize(_deps):
        return None

    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(cli, "build_deps", lambda _settings: deps)
    monkeypatch.setattr(cli, "initialize_deps", initialize)
    await cli._run(argparse.Namespace(cmd="init-db"))

    output = capsys.readouterr().out
    assert output.strip() == "db ready"
    assert database_url not in output


async def test_group_admin_commands_are_retired(deps, monkeypatch):
    monkeypatch.setattr(cli.Settings, "load", lambda: deps.settings)
    monkeypatch.setattr(cli, "build_deps", lambda settings: deps)
    monkeypatch.setattr(sys, "argv", ["homeshield-cli", "add-group", "--name", "old"])
    with pytest.raises(SystemExit): cli.main()
