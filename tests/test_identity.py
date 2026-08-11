"""Tests for commit-identity resolution (vikunja#310, id 321).

The failure this guards against is asymmetric: writing the agent identity into a
third-party public repo is permanent and public, while writing the public identity
into a forge repo just loses attribution in a place we control. The tests are
written from that direction.
"""

import pytest

from githost_mcp.identity import (
    IDENTITY_AGENT,
    IDENTITY_PUBLIC,
    IdentityUndetermined,
    resolve_ownership,
    resolve_public_identity,
)

FORGE_OWNERS = ["TadMSTR"]
GITEA_HOST = "gitea.example-forge.test"


def _resolve(remotes):
    return resolve_ownership(remotes, FORGE_OWNERS, GITEA_HOST)


def test_no_remotes_uses_agent_identity():
    """No publication target, so nothing to disclose to."""
    assert _resolve({}).mode == IDENTITY_AGENT


def test_all_forge_owned_remotes_use_agent_identity():
    result = _resolve({"origin": "https://github.com/TadMSTR/githost-mcp.git"})
    assert result.mode == IDENTITY_AGENT


def test_own_gitea_host_is_forge_controlled_whatever_the_org():
    result = _resolve({"origin": f"git@{GITEA_HOST}:host-forge/component-registry.git"})
    assert result.mode == IDENTITY_AGENT


def test_third_party_remote_uses_public_identity():
    result = _resolve({"origin": "https://github.com/siteboon/claudecodeui.git"})
    assert result.mode == IDENTITY_PUBLIC
    assert "origin" in result.third_party_remotes


def test_owner_match_is_case_insensitive():
    """GitHub and Gitea both treat account names case-insensitively."""
    assert _resolve({"origin": "https://github.com/tadmstr/githost-mcp.git"}).mode == IDENTITY_AGENT


# ---------------------------------------------------------------------------
# The layouts that motivated the any-third-party-remote rule. Which remote is
# named `origin` is an artifact of how the clone was made, so an origin-only rule
# gives opposite answers for the same situation.
# ---------------------------------------------------------------------------


def test_fork_layout_origin_upstream_uses_public_identity():
    """origin = our fork under a forge-owned account, upstream = theirs.

    This is the case an owner-of-origin rule gets wrong: TadMSTR is forge-owned, so
    origin alone reads as forge-controlled — and the commit, bound for an upstream
    PR, would carry the agent identity. That is the contamination in e17739f7.
    """
    result = _resolve(
        {
            "origin": "https://github.com/TadMSTR/claudecodeui.git",
            "upstream": "https://github.com/siteboon/claudecodeui.git",
        }
    )
    assert result.mode == IDENTITY_PUBLIC
    assert result.third_party_remotes == ("upstream",)


def test_fork_layout_origin_theirs_fork_ours_uses_public_identity():
    """The mirror-image layout — the one the real claudecodeui clone actually uses."""
    result = _resolve(
        {
            "origin": "https://github.com/siteboon/claudecodeui.git",
            "fork": "https://github.com/TadMSTR/claudecodeui.git",
        }
    )
    assert result.mode == IDENTITY_PUBLIC
    assert result.third_party_remotes == ("origin",)


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:siteboon/claudecodeui.git",
        "ssh://git@github.com/siteboon/claudecodeui.git",
        "https://github.com/siteboon/claudecodeui.git",
        "http://github.com/siteboon/claudecodeui.git",
    ],
)
def test_third_party_detected_across_url_forms(url):
    """A URL form we fail to parse as third-party is a silent leak, so every form
    githost-mcp accepts has to be classified."""
    assert _resolve({"origin": url}).mode == IDENTITY_PUBLIC


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:TadMSTR/githost-mcp.git",
        "ssh://git@github.com/TadMSTR/githost-mcp.git",
        "https://github.com/TadMSTR/githost-mcp.git",
    ],
)
def test_forge_owned_detected_across_url_forms(url):
    assert _resolve({"origin": url}).mode == IDENTITY_AGENT


def test_local_path_remote_is_forge_controlled():
    """A bare path is on forge's own disk — not a third-party publication target,
    and not ambiguous either."""
    assert _resolve({"mirror": "/srv/mirrors/repo.git"}).mode == IDENTITY_AGENT


def test_unparseable_remote_refuses_rather_than_guessing():
    """Refuse: agent identity would leak, public identity would break attribution."""
    with pytest.raises(IdentityUndetermined):
        _resolve({"weird": "not a url at all"})


def test_unparseable_remote_error_names_the_remote_but_not_the_url():
    """The message reaches the caller and the audit log. An unparseable URL is
    exactly the case redact_url_credentials cannot be trusted on, so the URL is not
    echoed at all — the remote's name is enough to find it in .git/config."""
    with pytest.raises(IdentityUndetermined) as excinfo:
        _resolve({"legacy": "ghp_hardcodedtokenvalue@@@garbage"})
    message = str(excinfo.value)
    assert "legacy" in message
    assert "ghp_hardcodedtokenvalue" not in message


def test_unparseable_remote_refuses_even_alongside_a_forge_remote():
    with pytest.raises(IdentityUndetermined):
        _resolve(
            {
                "origin": "https://github.com/TadMSTR/githost-mcp.git",
                "weird": "@@@",
            }
        )


# ---------------------------------------------------------------------------
# Public identity validation
# ---------------------------------------------------------------------------


def test_public_identity_accepted():
    name, email = resolve_public_identity("TadMSTR", "69825253+TadMSTR@users.noreply.github.com")
    assert name == "TadMSTR"
    assert email.endswith("@users.noreply.github.com")


@pytest.mark.parametrize(
    ("name", "email"),
    [
        ("developer-agent", "developer@forge"),
        ("TadMSTR", "developer@forge"),
        ("writer-agent", "someone@example.com"),
    ],
)
def test_public_identity_refuses_a_forge_agent_identity(name, email):
    """A repo-local user.email=developer@forge would otherwise be handed back as the
    'public' identity — the leak wearing the other label."""
    with pytest.raises(IdentityUndetermined):
        resolve_public_identity(name, email)


@pytest.mark.parametrize(("name", "email"), [("", "a@b.com"), ("Name", ""), ("", "")])
def test_public_identity_refuses_when_unconfigured(name, email):
    with pytest.raises(IdentityUndetermined):
        resolve_public_identity(name, email)
