"""No secret may be committed, and .gitignore is not what enforces it.

An ignore rule only helps when the file is named the way you expected. It does
nothing about a key pasted into `config.py`, or a debug print left in a script,
or a response cache that turned out to echo an Authorization header. So the
guard is this: scan every file git actually tracks, and fail the build on
anything shaped like a credential.

This runs in CI, which means the check happens before a push rather than after
someone notices. A secret that reaches a public remote is compromised whether or
not it is deleted afterwards -- the fix is a rotated key, not a revert -- so the
only useful place for this test is upstream of the push.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Deliberately shaped to catch real credentials and not documentation. The
# placeholders in README.md and .env.example are `sk-...` and `github_pat_...`,
# which are too short to match -- a template that tripped the test would get the
# test disabled, which is worse than not having it.
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "OpenAI key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "OpenAI project key": re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "GitHub fine-grained token": re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    "GitHub classic token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "AWS access key id": re.compile(r"AKIA[0-9A-Z]{16}"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "Slack token": re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer header": re.compile(r"[Aa]uthorization[\"']?\s*[:=]\s*[\"']?Bearer\s+\S{16,}"),
}

# Binary and generated files that are tracked but not worth scanning as text.
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".woff", ".woff2"}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO / name for name in result.stdout.split("\0") if name]


@pytest.fixture(scope="module")
def files() -> list[Path]:
    found = tracked_files()
    assert found, "git ls-files returned nothing -- is this a repository?"
    return found


def test_no_tracked_file_contains_a_credential(files):
    offences: list[str] = []
    for path in files:
        if path.suffix in SKIP_SUFFIXES or not path.exists():
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(body):
                line = body[: match.start()].count("\n") + 1
                offences.append(f"{path.relative_to(REPO)}:{line} looks like a {label}")

    assert not offences, (
        "A credential is committed. Deleting it is not the fix -- anything that "
        "reached a remote is compromised and the key must be rotated.\n  " + "\n  ".join(offences)
    )


def test_no_env_file_is_tracked(files):
    tracked = {path.name for path in files}
    leaked = {name for name in tracked if name.startswith(".env") and name != ".env.example"}
    assert not leaked, f"environment files are tracked: {sorted(leaked)}"


def test_the_ignore_rules_cover_the_usual_names():
    """The names that actually get committed by accident, not just `.env`."""
    candidates = [
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        "key.pem",
        "server.key",
        "credentials.json",
        "service-account-prod.json",
    ]
    not_ignored = [
        name
        for name in candidates
        if subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO).returncode != 0
    ]
    assert not not_ignored, f".gitignore does not cover: {not_ignored}"


def test_the_template_is_tracked_and_holds_no_real_key(files):
    """`.env.example` is committed on purpose, so it gets checked twice."""
    example = REPO / ".env.example"
    assert example in files, ".env.example should be tracked as a template"
    body = example.read_text(encoding="utf-8")
    for label, pattern in SECRET_PATTERNS.items():
        assert not pattern.search(body), f"the template contains a real {label}"


def test_the_response_cache_never_carries_a_key():
    """The cache is committed by design, so it is the one tracked file that
    could plausibly pick up a credential.

    By construction it cannot: the cache key is a hash of the model, tools and
    messages, and the stored payload is the assistant's content, tool calls and
    token counts. No header, no client config, no environment. This asserts it
    against the real file rather than trusting the construction.
    """
    from src.llm import CACHE_PATH

    if not CACHE_PATH.exists():
        pytest.skip("no cache recorded in this checkout")
    body = CACHE_PATH.read_text(encoding="utf-8")
    for label, pattern in SECRET_PATTERNS.items():
        assert not pattern.search(body), f"the response cache contains a {label}"
    assert "api_key" not in body.lower()


def test_nothing_in_src_reads_a_key_from_anywhere_but_the_environment():
    """One place reads the key, and it reads it from os.environ after loading
    .env. A second reader is how a key ends up in a config file."""
    readers = []
    for path in (REPO / "src").rglob("*.py"):
        body = path.read_text(encoding="utf-8")
        if "OPENAI_API_KEY" in body:
            readers.append(path.name)
    assert readers == ["llm.py"], f"unexpected key readers: {readers}"
