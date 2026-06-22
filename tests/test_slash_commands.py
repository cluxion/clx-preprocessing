from __future__ import annotations

from cluxion_agentplugin_preprocessing.slash_commands import LOOPAUTO_HELP, handle_loopauto


def test_loopauto_help_without_args() -> None:
    assert "Autonomous queue drain" in handle_loopauto("")
    assert handle_loopauto("help") == LOOPAUTO_HELP