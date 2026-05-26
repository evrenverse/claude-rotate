import re

from claude_rotate import __version__


def test_version_is_set() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_main_module_invokes_cli_main(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`python -m claude_rotate` must dispatch to cli.main and propagate its exit code."""
    import runpy

    calls: dict[str, int] = {"count": 0}

    def _stub() -> int:
        calls["count"] += 1
        return 0

    monkeypatch.setattr("claude_rotate.cli.main", _stub)

    # __main__ raises SystemExit(main()) — confirm exit code is propagated.
    try:
        runpy.run_module("claude_rotate", run_name="__main__")
    except SystemExit as exc:
        assert exc.code == 0
    assert calls["count"] == 1
