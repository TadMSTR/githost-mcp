"""Roots are matched as written, not resolved (vikunja#996).

The same case table runs in host-forge/scripts' workspace_policy.py tests
(scripts/tests/test_workspace_policy_root_matching.py), so githost and the resolver agree:

| case | layout | verdict |
|---|---|---|
| normal | <root>/repo is a directory | allowed |
| swapped | <root> replaced by a symlink to <outside> after load; ask <root>/repo | refused |
| repo-link | <root>/link -> <outside>/repo | refused |
| root-link | <root> is a symlink when the config loads | refused, the warning names it |
"""

import os

import pytest

from githost_mcp.config import get_config, reset_config
from githost_mcp.errors import PathNotAllowed
from githost_mcp.security import validate_read_path, validate_write_path


@pytest.fixture()
def layout(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "repo").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "repo").mkdir(parents=True)
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(root))
    reset_config()
    yield {"root": root, "outside": outside, "tmp": tmp_path}
    reset_config()


@pytest.mark.parametrize("validate", [validate_read_path, validate_write_path])
def test_normal_repo_under_a_root_is_allowed(layout, validate):
    assert validate(str(layout["root"] / "repo")) == str(layout["root"] / "repo")


@pytest.mark.parametrize("validate", [validate_read_path, validate_write_path])
def test_root_swapped_for_a_symlink_after_load_is_refused(layout, validate):
    root, outside = layout["root"], layout["outside"]
    get_config()  # loaded while the root is a real directory
    os.rename(root, layout["tmp"] / "root.moved")
    root.symlink_to(outside)
    with pytest.raises(PathNotAllowed, match="symlink"):
        validate(str(root / "repo"))


@pytest.mark.parametrize("validate", [validate_read_path, validate_write_path])
def test_symlinked_repo_pointing_outside_every_root_is_refused(layout, validate):
    (layout["root"] / "link").symlink_to(layout["outside"] / "repo")
    with pytest.raises(PathNotAllowed):
        validate(str(layout["root"] / "link"))


def test_a_root_that_is_a_symlink_at_load_is_refused_by_name(tmp_path, monkeypatch):
    real = tmp_path / "real"
    (real / "repo").mkdir(parents=True)
    link = tmp_path / "linkroot"
    link.symlink_to(real)
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(link))
    events = []

    class _Log:
        def warning(self, event, **kw):
            events.append((event, kw))

        info = warning

    monkeypatch.setattr("githost_mcp.config.log", _Log())
    reset_config()
    config = get_config()
    assert config.allowed_read_roots == [] and config.allowed_write_roots == []
    assert ("allowlist_root_refused_symlink", {"root": str(link)}) in events
    with pytest.raises(PathNotAllowed):
        validate_read_path(str(link / "repo"))
    reset_config()


def test_roots_are_kept_as_written_not_resolved(tmp_path, monkeypatch):
    (tmp_path / "a" / "b").mkdir(parents=True)
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", f"{tmp_path}/a/./b/../b")
    reset_config()
    assert get_config().allowed_read_roots == [str(tmp_path / "a" / "b")]
    reset_config()
