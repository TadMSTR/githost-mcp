"""Path allowlist validation and credential masking."""

from __future__ import annotations

from pathlib import Path

from .config import get_config


def validate_write_path(repo_path: str) -> None:
    """Raise ValueError if repo_path is not under an allowed root."""
    config = get_config()
    if not config.allowed_repo_roots:
        raise ValueError(
            "Write operations are disabled: ALLOWED_REPO_ROOTS is not set. "
            "Set ALLOWED_REPO_ROOTS to a comma-separated list of allowed directories."
        )
    try:
        resolved = Path(repo_path).resolve()
    except Exception as e:
        raise ValueError(f"Invalid repo path: {e}") from None

    for root in config.allowed_repo_roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return
        except ValueError:
            continue

    raise ValueError(
        f"Path '{repo_path}' is not under any allowed root. "
        f"Allowed: {config.allowed_repo_roots}"
    )


def mask_credentials(text: str) -> str:
    """Replace known credential values with *** in text."""
    config = get_config()
    result = text
    for token in [
        config.github_token,
        config.gitea_token,
        config.gitlab_token,
        config.woodpecker_token,
        config.pypi_token,
        config.pypi_test_token,
        config.npm_token,
        config.audit_signing_key,
    ]:
        if token and len(token) > 4:
            result = result.replace(token, "***")
    return result
