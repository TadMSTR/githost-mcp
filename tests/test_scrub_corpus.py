"""scrub() against a corpus of real failure strings, both directions (vikunja#913).

tests/fixtures/scrub_corpus.json was captured from real failures of git/GitPython,
PyGithub, httpx, twine and npm against a local stub server. Every planted credential must
be redacted; every ordinary message must come back byte-identical, because scrub() is on
every tool return path and an over-broad shape would mangle error text silently.
"""

import json
import pathlib
import time

import pytest

from githost_mcp.config import reset_config
from githost_mcp.security import scrub

CORPUS = json.loads((pathlib.Path(__file__).parent / "fixtures" / "scrub_corpus.json").read_text())

# Shape-valid fakes, built by concatenation so no literal token sits in the repo.
FAKES = {
    "<<GHP>>": "ghp_" + "Zq8" * 12,
    "<<BEARER>>": "opaque" + "Q7x" * 10,
    "<<NPM>>": "npm_" + "Ab3" * 12,
    "<<PYPI>>": "pypi-AgE" + "Ik" * 30,
    "<<GLPAT>>": "glpat-" + "Xy9" * 7,
}
MACHINE = {"<<PORT>>": "40123", "<<TMP>>": "/tmp/scratch-abc", "<<VENV>>": "/opt/venvs/x"}


@pytest.fixture(autouse=True)
def no_known_tokens(monkeypatch):
    """Nothing in config: only the shapes can redact."""
    for var in ("GITHUB_TOKEN", "GITEA_TOKEN", "GITLAB_TOKEN", "NPM_TOKEN", "PYPI_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    reset_config()


def _fill(text: str) -> str:
    for table in (FAKES, MACHINE):
        for k, v in table.items():
            text = text.replace(k, v)
    return text


def test_corpus_has_both_kinds():
    kinds = {c["credential"] for c in CORPUS["cases"]}
    assert kinds == {True, False}
    assert {c["source"] for c in CORPUS["cases"]} >= {
        "gitpython",
        "pygithub",
        "httpx",
        "twine",
        "npm",
    }


@pytest.mark.parametrize(
    "case", [c for c in CORPUS["cases"] if not c["credential"]], ids=lambda c: c["source"]
)
def test_ordinary_failure_text_is_byte_identical(case):
    text = _fill(case["text"])
    assert scrub(text) == text


@pytest.mark.parametrize(
    "case", [c for c in CORPUS["cases"] if c["credential"]], ids=lambda c: c["source"]
)
def test_every_planted_credential_is_redacted(case):
    text = _fill(case["text"])
    planted = [v for k, v in FAKES.items() if k in case["text"]]
    assert planted, "precondition: the case plants a credential"
    out = scrub(text)
    for value in planted:
        assert value not in out
        assert value[8:24] not in out  # no recognisable middle either


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abc.def-ghi", "Authorization: Bearer ***"),
        ("authorization=token deadbeefcafe", "authorization=token ***"),
        ("AUTHORIZATION: Basic dXNlcjpwYXNz trailing", "AUTHORIZATION: Basic *** trailing"),
        ("'Authorization: bearer xyz'", "'Authorization: bearer ***'"),
        ("x " + "ghs_" + "a1" * 18 + " y", "x ghs_*** y"),
        ("github_pat_" + "A1_" * 20, "github_pat_***"),
        ("tok=" + "xoxb-" + "1234-abcd-5678", "tok=xoxb-***"),
        ("NPM_TOKEN=" + "npm_" + "z9" * 18, "NPM_TOKEN=npm_***"),
        # Serialised and env forms (audit F-02).
        ("{'Authorization': 'Bearer abc.def'}", "{'Authorization': 'Bearer ***'}"),
        ('{"Authorization": "token deadbeef"}', '{"Authorization": "token ***"}'),
        ("HTTP_AUTHORIZATION=Bearer abc.def x", "HTTP_AUTHORIZATION=Bearer *** x"),
        ("X-Authorization: Negotiate YIIabc", "X-Authorization: Negotiate ***"),
        ("Authorization: Digest username", "Authorization: Digest ***"),
        ("Authorization: " + "f0" * 20, "Authorization: ***"),
    ],
)
def test_shapes_redact(text, expected):
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "npm_config_cache is unset",
        "uses pypi-publish action",
        "the Authorization header is missing",
        "Authorization: Bearer",
        "branch ghp_short",
        "xghp_" + "a1" * 18,
        "set npm_config_registry=https://registry.npmjs.org/",
        "run github_pat_check before release",
        "authorization: required",
        "{'Authorization': 'Bearer'}",
        "unauthorization: abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_lookalikes_are_untouched(text):
    assert scrub(text) == text


def test_shapes_are_linear():
    """64 KB of near-misses for each shape, well under a second."""
    blobs = [
        "Authorization: " * 4000,
        "ghp_" * 16000,
        "_" * 64000,
        "a" * 64000,
        "authorization:bearer " * 3000,
        "'Authorization': '" * 3500,
        "HTTP_AUTHORIZATION=" * 3400,
    ]
    start = time.perf_counter()
    for blob in blobs:
        scrub(blob)
    assert time.perf_counter() - start < 0.5
