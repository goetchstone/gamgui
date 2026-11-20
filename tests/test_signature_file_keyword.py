"""A signature that is one of GAM's file keywords (failure log 2026-11-20).

GAM reads a `signature` value of `file`, `htmlfile`, `textfile`, `gdoc`, `ghtml`, `gcsdoc` or `gcshtml` (stripped,
lowercased, `_` removed: getStringOrFile, SORF_FILE_ARGUMENTS, read from the vendored build) as a request to load the
next argument, `html`, as a file: a missing one fails the write, and a file of that name in GAM's working folder would
become the signature. The app refuses such a body before GAM, on every path that sets one.
"""

from __future__ import annotations

from html import unescape

import pytest

from gamgui.core.gam.commands import GAMCommands

from .helpers import gam_writes
from .test_users_web import _sig_preview, client  # noqa: F401

ALICE = "alice@example.com"


@pytest.mark.parametrize("body", ["file", "HTML_FILE", "  gdoc  ", "GcsHtml", "textfile", "ghtml", "gcsdoc"])
def test_the_builder_refuses_a_body_gam_would_read_as_a_file(body):
    with pytest.raises(ValueError, match="reads it as a file"):
        GAMCommands.set_signature(ALICE, body)


@pytest.mark.parametrize("body", ["File me later", "profile", "", "<b>file</b>"])
def test_a_body_that_only_contains_one_is_a_signature(body):
    assert GAMCommands.set_signature(ALICE, body)[3] == body


def test_a_template_that_renders_to_one_previews_nothing_and_names_who(client, gam_calls):  # noqa: F811
    page, token = _sig_preview(client, template="gdoc", scope_type="company", scope_value="")
    assert not token and "GAM reads it as a file" in unescape(page) and ALICE in page
    assert gam_writes(gam_calls()) == []


def test_the_user_page_refuses_one_in_words(client, gam_calls):  # noqa: F811
    r = client.post("/users/signature", data={"email": ALICE, "signature": "file", "html": "on"})
    assert "GAM reads it as a file" in unescape(r.text) and gam_writes(gam_calls()) == []


