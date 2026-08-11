"""Commit-identity resolution: which identity a commit object should carry.

This decides what an **external maintainer** sees in `git log` on a repo forge does
not own. It has nothing to do with what forge's own audit trail records — every
audit entry is written with the real acting agent's ID regardless of what is
resolved here (see audit.write_audit_entry, which reads the process-wide agent ID
and never consults this module). Inverting that would turn a disclosure fix into an
accountability hole.

Background: `git_commit` hardcoded `<agent>-agent <agent@forge>` plus an
`agent-id:` trailer on every commit, including commits destined for third-party
public repos, where that discloses forge's internal agent naming into permanent
public git history. Fork commit e17739f7 in TadMSTR/claudecodeui already carries it.
(vikunja#310, id 321.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Same two forms git_remote accepts. Anything else is unparseable, and an
# unparseable remote is treated as undetermined rather than guessed at.
_SCHEME_REMOTE_RE = re.compile(
    r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*)://(?P<rest>.+)$",
)
_SCP_REMOTE_RE = re.compile(r"^(?:(?P<userinfo>[^/@]+)@)?(?P<host>[^/@:]+):(?P<path>[^:].*)$")

IDENTITY_AGENT = "agent"
IDENTITY_PUBLIC = "public"
IDENTITY_AUTO = "auto"

# A resolved "public" identity that still looks like a forge agent is not a public
# identity — it is the leak, wearing the other label. Most likely cause is a
# repo-local user.email left set to an agent address.
_AGENT_EMAIL_SUFFIX = "@forge"
_AGENT_NAME_SUFFIX = "-agent"


class IdentityUndetermined(ValueError):
    """Raised when the commit identity cannot be established safely.

    Refusing is the only safe action on ambiguity: defaulting to the agent identity
    leaks it into public history, and defaulting to the public identity silently
    breaks attribution on internal repos. Both are worse than an error the caller
    can resolve with an explicit `identity=` argument.
    """


@dataclass(frozen=True)
class RepoOwnership:
    """What the remotes say about who controls this repository."""

    mode: str  # IDENTITY_AGENT or IDENTITY_PUBLIC
    reason: str
    third_party_remotes: tuple[str, ...] = ()


def _parse_remote(url: str) -> tuple[str, str] | None:
    """Return ``(host, owner)`` for a remote URL, or None if it cannot be parsed.

    `owner` is the first path segment: the GitHub/Gitea account or organization
    that owns the repository.
    """
    if scheme_match := _SCHEME_REMOTE_RE.match(url):
        parts = urlsplit(url)
        host = parts.hostname or ""
        path = parts.path.lstrip("/")
        if not host or not path:
            return None
        del scheme_match
    elif scp_match := _SCP_REMOTE_RE.match(url):
        host = scp_match.group("host")
        path = scp_match.group("path").lstrip("/")
    else:
        return None

    owner = path.split("/", 1)[0]
    if not owner:
        return None
    return host.lower(), owner


def _is_local_path_remote(url: str) -> bool:
    """True for a bare filesystem-path remote (a local mirror, a bare repo on disk).

    Such a remote has no owner to classify, but it is not ambiguous either: it is a
    path on forge's own disk and cannot be a third-party publication target. Treated
    as forge-controlled rather than refused, so committing in a repo with a local
    mirror remote keeps working. git_remote will not create one of these — this is
    for remotes that already exist, added out-of-band.
    """
    return url.startswith(("/", "./", "../", "~")) or (
        len(url) > 2 and url[1] == ":" and url[2] in "\\/"  # C:\ or C:/ on Windows
    )


def _is_forge_controlled(url: str, forge_owners: list[str], gitea_host: str) -> bool:
    """True if this remote is on infrastructure or under an account forge controls.

    Two independent grounds. Anything hosted on forge's own Gitea is forge's,
    whatever the org — that host is not reachable by an external maintainer, so
    nothing published there is a disclosure. Otherwise the owner must be named in
    FORGE_OWNED_OWNERS. Owner comparison is case-insensitive because GitHub and
    Gitea both treat account names that way.

    Raises IdentityUndetermined if the URL cannot be parsed at all.
    """
    if _is_local_path_remote(url):
        return True
    parsed = _parse_remote(url)
    if parsed is None:
        raise IdentityUndetermined(
            f"Cannot determine the owner of remote URL '{url}'. Pass identity='agent' or "
            "identity='public' explicitly rather than having one guessed."
        )
    host, owner = parsed
    if gitea_host and host == gitea_host.lower():
        return True
    return owner.lower() in {o.lower() for o in forge_owners}


def resolve_ownership(
    remote_urls: dict[str, str],
    forge_owners: list[str],
    gitea_host: str,
) -> RepoOwnership:
    """Classify a repo from its remotes: agent identity, or public identity.

    Public identity wins if **any** remote is third-party, rather than reading the
    owner of `origin` alone. Which remote is `origin` is an artifact of how the
    clone was set up — for a fork-and-contribute checkout it is `origin=upstream,
    fork=ours` about as often as `origin=ours, upstream=theirs` — so an
    origin-only rule gives opposite answers for the same situation. It also gets
    the case that actually leaked wrong: a fork under a forge-controlled account
    (TadMSTR/claudecodeui) whose commits are bound for a third-party PR reads as
    forge-controlled by owner, and would keep the agent identity.

    A repo with no remotes at all keeps the agent identity: there is no
    publication target, so there is nothing to disclose to. If a third-party
    remote is added later, commits made after that point resolve to public.
    """
    if not remote_urls:
        return RepoOwnership(IDENTITY_AGENT, "no remotes configured")

    third_party = tuple(
        sorted(
            name
            for name, url in remote_urls.items()
            if not _is_forge_controlled(url, forge_owners, gitea_host)
        )
    )
    if third_party:
        return RepoOwnership(
            IDENTITY_PUBLIC,
            f"third-party remote(s): {', '.join(third_party)}",
            third_party,
        )
    return RepoOwnership(IDENTITY_AGENT, "all remotes are forge-controlled")


def resolve_public_identity(name: str, email: str) -> tuple[str, str]:
    """Validate the configured public identity, or refuse.

    Refuses an identity that still looks like a forge agent. Without this check the
    fallback to the repo's own git config could hand back an agent identity — a
    repo-local `user.email=developer@forge` is enough — and a commit would be
    labelled public while carrying exactly the identity this exists to keep out of
    public history.
    """
    if not name or not email:
        raise IdentityUndetermined(
            "This repository has a third-party remote, so the commit needs a public "
            "identity, but none is configured. Set GIT_PUBLIC_NAME and GIT_PUBLIC_EMAIL, "
            "or set user.name/user.email in git config."
        )
    if email.endswith(_AGENT_EMAIL_SUFFIX) or name.endswith(_AGENT_NAME_SUFFIX):
        raise IdentityUndetermined(
            f"The configured public identity ('{name} <{email}>') is a forge agent "
            "identity. Refusing to write it to a repository with a third-party remote — "
            "that is the disclosure this check exists to prevent. Set GIT_PUBLIC_NAME and "
            "GIT_PUBLIC_EMAIL to a genuinely public identity."
        )
    return name, email
