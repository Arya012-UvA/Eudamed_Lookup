"""Syntax-check the JavaScript embedded in the HTML pages.

Both pages are authored as one large Python string, so a bad edit can close a
template literal early and produce a page that lints clean, imports fine and
serves a 200 while being completely broken in the browser. This has happened,
so it is guarded.

Skipped when node is unavailable; the browser is the real check, this is the
cheap one that runs everywhere node exists.
"""

import re
import shutil
import subprocess

import pytest

from eudamed.report import TEMPLATE
from eudamed.webui import PAGE

NODE = shutil.which("node")

PAGES = {"webui.PAGE": PAGE, "report.TEMPLATE": TEMPLATE}


def extract_script(html):
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "no inline <script> block found"
    return "\n".join(blocks)


@pytest.mark.parametrize("name", sorted(PAGES))
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_embedded_js_parses(name, tmp_path):
    path = tmp_path / "page.js"
    path.write_text(extract_script(PAGES[name]), encoding="utf-8")
    done = subprocess.run([NODE, "--check", str(path)],
                          capture_output=True, text=True)
    assert done.returncode == 0, f"{name} has a JS syntax error:\n{done.stderr}"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_backticks_are_balanced(name):
    """A cheap structural check that runs even without node."""
    js = extract_script(PAGES[name])
    assert js.count("`") % 2 == 0, f"{name} has an odd number of backticks"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_is_well_formed_html(name):
    html = PAGES[name]
    assert html.lstrip().startswith("<!doctype html>")
    assert html.count("<script>") == html.count("</script>")
    assert html.count("<style>") == html.count("</style>")
    assert "</body>" in html and "</html>" in html
