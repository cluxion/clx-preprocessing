from __future__ import annotations

from cluxion_agentplugin_preprocessing import hermes_deliver_patch


def test_patch_status_applied_on_local_hermes() -> None:
    root = hermes_deliver_patch.resolve_hermes_agent_root()
    if root is None:
        return
    status = hermes_deliver_patch.patch_status(root)
    assert status.status in {"applied", "missing", "partial"}


def test_ensure_applied_idempotent() -> None:
    root = hermes_deliver_patch.resolve_hermes_agent_root()
    if root is None:
        return
    first = hermes_deliver_patch.ensure_applied(hermes_root=root)
    second = hermes_deliver_patch.ensure_applied(hermes_root=root)
    assert first.status == "applied"
    assert second.changed is False