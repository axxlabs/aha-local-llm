# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""Configuration (SPEC §3.7)."""


def test_every_setting_has_one_default_the_template_the_loader_and_config_agree(tmp_path):
    """Review: defaults were written three times (template, loader, dataclass); now only in SETTINGS."""
    from aha_local.config import DEFAULT_TOML, SETTINGS, Config, settings_toml

    text = DEFAULT_TOML.format(
        gateway_url="wss://g/v1/computer",
        pin="p",
        app_url="https://a",
        name="Home",
        timezone="Europe/London",
        language="en-GB",
        models_dir="/m",
        settings=settings_toml(),
    )
    path = tmp_path / "config.toml"
    path.write_text(text)
    loaded, built = Config.load(path), Config(gateway_url="x", gateway_pin="y")
    for s in SETTINGS:
        assert f"{s.key} = " in text, s.key
        assert getattr(loaded, s.field) == getattr(built, s.field), s.key
    path.write_text(text.replace("max_task_minutes = 0", "max_task_minutes = 15"))
    assert Config.load(path).max_task_seconds == 900


def test_install_when_idle_waits_for_quiet_and_only_one_waits(tmp_path, monkeypatch, capsys):
    """Review: deploy waiters stacked up and an old one restarted the agent later; now one, building at the end."""
    import fcntl
    from types import SimpleNamespace

    from aha_local import cli

    monkeypatch.setattr(cli, "HOME_DATA", tmp_path)
    monkeypatch.setattr(cli, "IDLE_POLL", 0)
    states = iter(
        [
            {"running": 1, "cards": 0, "idle": 999},  # a task running
            {"running": 0, "cards": 1, "idle": 999},  # a card waiting
            {"running": 0, "cards": 0, "idle": 10},  # just used
            {"running": 0, "cards": 0, "idle": 200},  # quiet: build
            {"running": 0, "cards": 0, "idle": 210},  # still quiet after the build: restart
        ]
    )
    built, restarted = [], []
    monkeypatch.setattr(cli, "admin", lambda req: next(states))
    monkeypatch.setattr(cli, "install", lambda: built.append(1))
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, check=True: restarted.append(cmd))
    cli.cmd_install(SimpleNamespace(when_idle=3))
    assert built == [1] and restarted == [["systemctl", "--user", "restart", "aha-agent.service"]]

    with open(tmp_path / "install.lock", "w") as held:  # another waiter holds the lock
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cli.cmd_install(SimpleNamespace(when_idle=3))
    assert "already waiting" in capsys.readouterr().out and built == [1]
