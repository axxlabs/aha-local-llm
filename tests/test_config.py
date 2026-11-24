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
