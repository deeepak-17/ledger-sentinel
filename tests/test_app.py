"""The demo, driven headlessly.

`make demo` is the one command a judge runs, so it gets a test. Streamlit's
AppTest executes the real script in-process, which catches the failure modes that
matter here -- an import error, a bad column reference, a Streamlit API that
changed under us -- without needing a browser.

What this does not test is how it looks. That is what the video is for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "src" / "app.py")
TIMEOUT = 120


@pytest.fixture(scope="module")
def app():
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    return at


def test_the_demo_runs_without_raising(app):
    assert not app.exception


def test_the_headline_is_the_false_match_rate(app):
    labels = [metric.label for metric in app.metric]
    assert labels[0] == "False-match rate"
    assert labels[1] == "Match rate"


def test_it_shows_the_held_out_numbers(app):
    values = {metric.label: metric.value for metric in app.metric}
    assert values["False-match rate"] == "0.0%"
    assert values["Match rate"] == "70.0%"
    assert values["Exceptions"] == "9"


def test_every_tab_is_present(app):
    assert len(app.tabs) == 5


def test_switching_to_the_tuning_seed_still_works():
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    at.sidebar.radio[1].set_value("A").run()
    assert not at.exception
    values = {metric.label: metric.value for metric in at.metric}
    assert values["False-match rate"] == "0.0%"


def test_the_ai_toggle_is_disabled_without_a_recorded_cache(app):
    from src.llm import CACHE_PATH

    toggle = app.toggle[0]
    if CACHE_PATH.exists():
        pytest.skip("a cache is recorded in this checkout")
    assert toggle.disabled
    assert toggle.value is False
