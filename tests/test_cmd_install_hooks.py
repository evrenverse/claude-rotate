from __future__ import annotations

import json

from claude_rotate.commands.install_hooks import HOOK_SPEC, install, remove


def test_install_adds_all_hooks_idempotently(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "existing"}]}]
                }
            }
        )
    )

    install(settings)
    install(settings)  # second run must not duplicate

    data = json.loads(settings.read_text())
    cmds = [h["command"] for groups in data["hooks"].values() for g in groups for h in g["hooks"]]
    # our hook commands present exactly once each, plus the pre-existing one kept
    for _event, command in HOOK_SPEC:
        assert cmds.count(command) == 1
    assert "existing" in cmds


def test_remove_strips_only_our_hooks(tmp_path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "existing"}]}]
                }
            }
        )
    )
    install(settings)
    remove(settings)

    data = json.loads(settings.read_text())
    cmds = [
        h["command"]
        for groups in data.get("hooks", {}).values()
        for g in groups
        for h in g["hooks"]
    ]
    assert "existing" in cmds
    assert not any(c.startswith("claude-rotate __heartbeat") for c in cmds)
