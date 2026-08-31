"""#2179: the ``portal:`` block in coordinator.yml."""

from __future__ import annotations

import pytest

from coord.config import ConfigError, PortalConfig, _parse_portal, has_unexpanded_env_var

BASE = {
    "enabled": True,
    "base_url": "https://intake.heurontech.com",
    "bridge_client_id": "id-123",
    "bridge_client_secret": "secret-456",
}


def test_absent_block_is_disabled_and_changes_nothing():
    cfg = _parse_portal(None)
    assert cfg == PortalConfig()
    assert cfg.enabled is False
    assert cfg.base_url is None
    assert cfg.bridge_client_id is None
    assert cfg.bridge_client_secret is None


def test_a_full_block_parses():
    cfg = _parse_portal({**BASE, "timeout_secs": 5.0, "max_retries": 4})
    assert cfg.enabled is True
    assert cfg.base_url == "https://intake.heurontech.com"
    assert cfg.bridge_client_id == "id-123"
    assert cfg.bridge_client_secret == "secret-456"
    assert cfg.timeout_secs == 5.0
    assert cfg.max_retries == 4


def test_base_url_trailing_slash_is_canonicalised():
    cfg = _parse_portal({**BASE, "base_url": "https://intake.heurontech.com/"})
    assert cfg.base_url == "https://intake.heurontech.com"


def test_unknown_option_is_refused():
    with pytest.raises(ConfigError, match="unknown portal option"):
        _parse_portal({"bass_url": "typo"})


def test_not_a_mapping_is_refused():
    with pytest.raises(ConfigError, match="must be a mapping"):
        _parse_portal(["not", "a", "mapping"])


def test_enabling_without_base_url_is_refused():
    with pytest.raises(ConfigError, match="base_url/bridge_client_id/bridge_client_secret"):
        _parse_portal({
            "enabled": True,
            "bridge_client_id": "id-123",
            "bridge_client_secret": "secret-456",
        })


def test_enabling_without_credentials_is_refused():
    """Half a credential is not a credential — coord-portal's
    isBridgeAuthorized fails closed on exactly this, and config parsing
    should refuse at parse time rather than 401-loop forever."""
    with pytest.raises(ConfigError, match="base_url/bridge_client_id/bridge_client_secret"):
        _parse_portal({"enabled": True, "base_url": "https://intake.heurontech.com"})


def test_disabled_block_may_omit_everything():
    cfg = _parse_portal({"enabled": False})
    assert cfg.enabled is False
    assert cfg.base_url is None


def test_env_var_expansion_for_secrets(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLIENT_ID", "id-from-env")
    monkeypatch.setenv("BRIDGE_CLIENT_SECRET", "secret-from-env")
    cfg = _parse_portal({
        "enabled": True,
        "base_url": "https://intake.heurontech.com",
        "bridge_client_id": "${BRIDGE_CLIENT_ID}",
        "bridge_client_secret": "${BRIDGE_CLIENT_SECRET}",
    })
    assert cfg.bridge_client_id == "id-from-env"
    assert cfg.bridge_client_secret == "secret-from-env"


def test_unset_env_var_leaves_the_placeholder_and_that_fails_ownership_check():
    """An unset ${VAR} is left literally as '${VAR}' (documented behaviour of
    _expand_env_vars) rather than silently becoming an empty/None secret —
    that placeholder string just won't match anything real on the portal
    side, which fails loudly at the first push rather than here."""
    cfg = _parse_portal({
        **BASE,
        "bridge_client_secret": "${DEFINITELY_NOT_SET_XYZ}",
    })
    assert cfg.bridge_client_secret == "${DEFINITELY_NOT_SET_XYZ}"


def test_empty_string_secret_is_a_typo_not_unset():
    with pytest.raises(ConfigError, match="non-empty string"):
        _parse_portal({**BASE, "bridge_client_id": "   "})


@pytest.mark.parametrize(
    "key,value",
    [
        ("timeout_secs", 0.0),
        ("timeout_secs", -1.0),
        ("max_retries", -1),
    ],
)
def test_out_of_range_numbers_are_refused(key, value):
    with pytest.raises(ConfigError):
        _parse_portal({**BASE, key: value})


def test_timeout_secs_must_be_a_number():
    with pytest.raises(ConfigError, match="positive number"):
        _parse_portal({**BASE, "timeout_secs": "soon"})


# ── #2336: has_unexpanded_env_var — the "did this actually resolve" check ──


def test_has_unexpanded_env_var_true_for_a_placeholder_left_as_is():
    assert has_unexpanded_env_var("${DEFINITELY_NOT_SET_XYZ}") is True


def test_has_unexpanded_env_var_false_for_a_resolved_value():
    assert has_unexpanded_env_var("id-from-env") is False


def test_has_unexpanded_env_var_false_for_none():
    assert has_unexpanded_env_var(None) is False


def test_has_unexpanded_env_var_false_for_empty_string():
    assert has_unexpanded_env_var("") is False


def test_max_retries_must_be_an_int_not_a_bool():
    with pytest.raises(ConfigError, match="non-negative integer"):
        _parse_portal({**BASE, "max_retries": True})


# ── #2903 (phase 1 of #2902): the draft gate's per-kind approval policy ─────


def test_absent_approval_block_gates_the_two_prose_kinds():
    """The whole acceptance bar of the default policy, in one assertion."""
    cfg = _parse_portal(None)
    assert cfg.approval.gates("design_round") is True
    assert cfg.approval.gates("question") is True
    assert cfg.approval.gates("status") is False
    assert cfg.approval.gates("preview") is False


def test_absent_approval_key_in_a_present_portal_block_is_still_the_default():
    cfg = _parse_portal({**BASE})
    assert cfg.approval.gates("design_round") is True
    assert cfg.approval.gates("status") is False


def test_a_present_block_merges_over_the_defaults():
    """Relaxing one kind must not silently open every other one."""
    cfg = _parse_portal({**BASE, "approval": {"question": False}})
    assert cfg.approval.gates("question") is False
    assert cfg.approval.gates("design_round") is True  # untouched


def test_a_mechanical_kind_can_be_gated_too():
    cfg = _parse_portal({**BASE, "approval": {"status": True}})
    assert cfg.approval.gates("status") is True


def test_an_empty_approval_block_is_the_default():
    cfg = _parse_portal({**BASE, "approval": None})
    assert cfg.approval.gates("design_round") is True


def test_an_unknown_kind_is_refused():
    """A typo'd kind that parsed would read as 'not gated' and mail a customer."""
    with pytest.raises(ConfigError, match="unknown portal.approval kind"):
        _parse_portal({**BASE, "approval": {"desgin_round": True}})


def test_a_non_boolean_value_is_refused():
    with pytest.raises(ConfigError, match="must be a boolean"):
        _parse_portal({**BASE, "approval": {"question": "yes"}})


def test_approval_must_be_a_mapping():
    with pytest.raises(ConfigError, match="portal.approval"):
        _parse_portal({**BASE, "approval": ["question"]})


def test_an_unknown_kind_is_not_gated_at_read_time():
    """`gates()` never fails closed on a kind this build has never heard of —
    there would be no CLI verb able to approve such a row."""
    cfg = _parse_portal(None)
    assert cfg.approval.gates("telepathy") is False


def test_portal_outbox_kinds_matches_portal_syncs_kind_constants():
    """The literal copy in config.py must not drift from portal_sync's KIND_*."""
    from coord import portal_sync
    from coord.config import PORTAL_OUTBOX_KINDS

    assert set(PORTAL_OUTBOX_KINDS) == {
        portal_sync.KIND_STATUS,
        portal_sync.KIND_DESIGN_ROUND,
        portal_sync.KIND_QUESTION,
        portal_sync.KIND_PREVIEW,
        portal_sync.KIND_RELAYED_ANSWER,
    }


def test_approval_survives_a_full_config_load(tmp_path):
    """End to end through `config.load()`, not just `_parse_portal`."""
    from coord import config as config_mod

    path = tmp_path / "coordinator.yml"
    path.write_text(
        "repos:\n"
        "  - name: coord\n"
        "    github: owner/coord\n"
        "machines:\n"
        "  - name: dellserver\n"
        "    host: dellserver\n"
        "    repos: [coord]\n"
        "portal:\n"
        "  approval:\n"
        "    design_round: false\n"
    )
    cfg = config_mod.load(path)
    assert cfg.portal.approval.gates("design_round") is False
    assert cfg.portal.approval.gates("question") is True
