"""git_commit concluding a conflicted merge keeps both parents (vikunja#1300).

Each test resolves a real conflict in a scratch repo through git_add + git_commit, then
checks the commit's parents, that the merge state is gone, and that git accepts a second
integration of the same branch (it would conflict again if the first merge had lost its
second parent).
"""

import json
import os
import stat

import git
import pytest

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config
from githost_mcp.tools import git_local


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(tmp_path))
    monkeypatch.setenv("AGENT_ID", "test")
    reset_config()
    init_logging()


@pytest.fixture()
def fns():
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    git_local.register(MockMCP())
    return registered


def _conflicted_repo(path):
    """main and feature both change file.txt; returns the repo on main, mid-merge."""
    repo = git.Repo.init(path, initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "t@test.com")
    (path / "file.txt").write_text("base\n")
    repo.index.add(["file.txt"])
    repo.index.commit("base")
    repo.git.checkout("-b", "feature")
    (path / "file.txt").write_text("feature\n")
    repo.git.commit("-am", "feature side")
    repo.git.checkout("main")
    (path / "file.txt").write_text("main\n")
    repo.git.commit("-am", "main side")
    return repo


def _start_conflict(repo):
    with pytest.raises(git.GitCommandError):
        repo.git.merge("feature")
    assert os.path.exists(os.path.join(repo.git_dir, "MERGE_HEAD"))


def _resolve_and_commit(fns, repo, workdir):
    (workdir / "file.txt").write_text("resolved\n")
    assert "error" not in fns["git_add"](str(workdir), ["file.txt"])
    result = fns["git_commit"](str(workdir), "merge feature")
    assert "error" not in result, result
    return result


def _assert_merged(repo, main_before, feature_sha):
    head = repo.head.commit
    assert [p.hexsha for p in head.parents] == [main_before, feature_sha]
    for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE"):
        assert not os.path.exists(os.path.join(repo.git_dir, name)), name
    # A second integration: a new feature commit merges cleanly, with no conflict on the
    # already-resolved file, only because the first merge recorded feature as a parent.
    repo.git.checkout("feature")
    with open(os.path.join(repo.working_tree_dir, "other.txt"), "w") as fh:
        fh.write("x\n")
    repo.git.add("other.txt")
    repo.git.commit("-m", "feature again")
    repo.git.checkout("main")
    repo.git.merge("feature", "--no-edit")
    with open(os.path.join(repo.working_tree_dir, "file.txt")) as fh:
        assert fh.read() == "resolved\n"


def _effect(tmp_path):
    entries = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    return [e for e in entries if e["tool"] == "git_commit"][-1]["effect"]


def test_merge_commit_in_a_plain_clone_keeps_both_parents(fns, tmp_path):
    repo = _conflicted_repo(tmp_path / "repo")
    main_before = repo.head.commit.hexsha
    feature_sha = repo.commit("feature").hexsha
    _start_conflict(repo)

    _resolve_and_commit(fns, repo, tmp_path / "repo")

    effect = _effect(tmp_path)
    assert effect["parent_sha"] == main_before
    assert effect["merge_parents"] == [main_before, feature_sha]
    _assert_merged(repo, main_before, feature_sha)


def test_merge_commit_in_a_linked_worktree_keeps_both_parents(fns, tmp_path):
    main_repo = _conflicted_repo(tmp_path / "repo")
    main_repo.git.checkout("--detach")
    wt = tmp_path / "wt"
    main_repo.git.worktree("add", str(wt), "main")
    repo = git.Repo(wt)
    main_before = repo.head.commit.hexsha
    feature_sha = repo.commit("feature").hexsha
    _start_conflict(repo)
    # The worktree's merge state lives in its own git dir, not the common one.
    assert repo.git_dir != main_repo.git_dir

    _resolve_and_commit(fns, repo, wt)

    assert _effect(tmp_path)["merge_parents"] == [main_before, feature_sha]
    _assert_merged(repo, main_before, feature_sha)


def test_merge_commit_on_the_signing_path_keeps_both_parents(fns, tmp_path, monkeypatch):
    """The git binary path (git_signing_key set) concludes the merge itself."""
    fake_gpg = tmp_path / "fake-gpg"
    fake_gpg.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "echo '[GNUPG:] SIG_CREATED D 1 8 00 0 0' >&2\n"
        "printf -- '-----BEGIN PGP SIGNATURE-----\\nfake\\n-----END PGP SIGNATURE-----\\n'\n"
    )
    fake_gpg.chmod(fake_gpg.stat().st_mode | stat.S_IXUSR)
    repo = _conflicted_repo(tmp_path / "repo")
    with repo.config_writer() as cw:
        cw.set_value("gpg", "program", str(fake_gpg))
    monkeypatch.setenv("GIT_SIGNING_KEY", "TESTKEY")
    reset_config()
    main_before = repo.head.commit.hexsha
    feature_sha = repo.commit("feature").hexsha
    _start_conflict(repo)

    result = _resolve_and_commit(fns, repo, tmp_path / "repo")

    assert _effect(tmp_path)["merge_parents"] == [main_before, feature_sha]
    # The agent identity reaches the signed commit (it used to be passed as `commit -c`,
    # which git reads as --reedit-message, so every signed commit failed).
    assert repo.head.commit.author.name == "test-agent"
    assert repo.head.commit.committer.name == "test-agent"
    assert result["author"].startswith("test-agent <")
    assert repo.head.commit.gpgsig
    _assert_merged(repo, main_before, feature_sha)


def test_ordinary_commit_records_no_merge_parents(fns, tmp_path):
    repo = _conflicted_repo(tmp_path / "repo")
    (tmp_path / "repo" / "new.txt").write_text("n\n")
    fns["git_add"](str(tmp_path / "repo"), ["new.txt"])
    before = repo.head.commit.hexsha
    assert "error" not in fns["git_commit"](str(tmp_path / "repo"), "plain")
    effect = _effect(tmp_path)
    assert effect["parent_sha"] == before
    assert "merge_parents" not in effect
    assert len(repo.head.commit.parents) == 1


def test_malformed_merge_head_refuses_the_commit(fns, tmp_path):
    repo = _conflicted_repo(tmp_path / "repo")
    (tmp_path / "repo" / "new.txt").write_text("n\n")
    fns["git_add"](str(tmp_path / "repo"), ["new.txt"])
    with open(os.path.join(repo.git_dir, "MERGE_HEAD"), "w") as fh:
        fh.write("not-a-sha\n")
    before = repo.head.commit.hexsha
    result = fns["git_commit"](str(tmp_path / "repo"), "plain")
    assert "MERGE_HEAD is malformed" in result["error"]
    assert repo.head.commit.hexsha == before


def test_merge_head_shas_reads_every_line(tmp_path):
    repo = git.Repo.init(tmp_path / "r")
    a, b = "a" * 40, "b" * 40
    (tmp_path / "r" / ".git" / "MERGE_HEAD").write_text(f"{a}\n{b}\n")
    assert git_local.merge_head_shas(repo) == [a, b]


def test_merge_head_shas_empty_without_a_merge(tmp_path):
    assert git_local.merge_head_shas(git.Repo.init(tmp_path / "r")) == []


def test_clear_merge_state_removes_every_merge_file(tmp_path):
    repo = git.Repo.init(tmp_path / "r")
    for name in git_local._MERGE_STATE_FILES:
        (tmp_path / "r" / ".git" / name).write_text("x")
    git_local.clear_merge_state(repo)
    for name in git_local._MERGE_STATE_FILES:
        assert not (tmp_path / "r" / ".git" / name).exists()
