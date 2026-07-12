from pathlib import Path

from cluxion_runtime.adapters.grok_build import (
    GROK_BUILD_MODEL,
    GROK_COMPOSER_25_FAST,
    build_grok_composer_command,
)


def test_grok_45_is_default_and_composer_remains_explicit() -> None:
    default = build_grok_composer_command("inspect", cwd=Path("/repo"))
    composer = build_grok_composer_command(
        "inspect", cwd=Path("/repo"), model=GROK_COMPOSER_25_FAST
    )

    assert GROK_BUILD_MODEL == "grok-4.5"
    assert default[default.index("-m") + 1] == "grok-4.5"
    assert composer[composer.index("-m") + 1] == "grok-composer-2.5-fast"
