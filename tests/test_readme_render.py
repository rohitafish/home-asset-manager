"""Tests for app/readme_render.py -- the /readme page's Markdown rendering.

Small, but it carries the app's one deliberate `|safe` template output, and
the module's SECURITY INVARIANT comment is the reason that's acceptable:
md_text is the repo's own README.md, never user- or device-supplied. These
pin the two things that comment depends on staying true -- that the helper
takes its Markdown as an argument (so a caller can't accidentally be handed
untrusted content without it being visible at the call site) and that it
does emit raw HTML, which is exactly why the invariant matters.

The rest is about the table of contents, which exists for a specific reason:
someone reaching for the README on the dashboard is usually looking for a
procedure -- "Restoring", "Power outages" -- and jumping to it.
"""

from app.readme_render import MARKDOWN_EXTENSIONS, render_readme_html


def test_headings_and_paragraphs_become_html():
    rendered = render_readme_html("# Title\n\nSome text.\n")

    assert "<h1" in rendered.html
    assert "<p>Some text.</p>" in rendered.html


def test_fenced_code_blocks_are_rendered():
    """The README is full of shell snippets -- launchctl invocations,
    redeploy commands -- and they have to survive as code, not prose."""
    rendered = render_readme_html("```\nlaunchctl kickstart -k gui/501/com.assetmgt.app\n```\n")

    assert "<code>" in rendered.html
    assert "launchctl kickstart" in rendered.html


def test_tables_are_rendered():
    rendered = render_readme_html("| a | b |\n| - | - |\n| 1 | 2 |\n")

    assert "<table>" in rendered.html
    assert "<td>1</td>" in rendered.html


def test_toc_is_returned_separately_from_the_body():
    """The convenience function markdown.markdown() builds and discards its
    Markdown instance, so there is no way to reach the toc extension's output
    through it -- the class form plus reading .toc off the instance is what
    makes this work at all, and a refactor back to the convenience function
    would silently return an empty toc."""
    rendered = render_readme_html("# Title\n\n## Setup\n\ntext\n\n## Restoring\n\ntext\n")

    assert "Setup" in rendered.toc
    assert "Restoring" in rendered.toc
    assert '<a href="#setup">' in rendered.toc


def test_toc_skips_the_h1_title_and_keeps_h4_subsections():
    """toc_depth is "2-4" on purpose: the single <h1> is the page title with
    nothing to link to, while #### subsections like "Restoring" are exactly
    what someone jumps to in an emergency. "2-3" would drop them."""
    rendered = render_readme_html(
        "# Page Title\n\n## Backups\n\n### Nightly\n\n#### Restoring\n\ntext\n"
    )

    assert "Page Title" not in rendered.toc
    assert "Backups" in rendered.toc
    assert "Restoring" in rendered.toc


def test_headings_get_anchors_so_the_toc_links_resolve():
    rendered = render_readme_html("## Power outages\n\ntext\n")

    assert 'id="power-outages"' in rendered.html


def test_each_call_gets_a_fresh_instance():
    """The page rebuilds from source on every request. A reused Markdown
    instance would need .reset() between calls; a fresh one per call
    sidesteps that, and this catches state leaking between renders."""
    first = render_readme_html("## Alpha\n")
    second = render_readme_html("## Beta\n")

    assert "Alpha" in first.toc
    assert "Alpha" not in second.toc
    assert "Beta" in second.toc


def test_benign_html_in_the_source_is_kept():
    """Markdown output is HTML by design and rendered with `|safe`; harmless
    raw HTML in the README (a <div> with a class) survives sanitisation."""
    rendered = render_readme_html("<div class='raw'>kept</div>\n")

    assert '<div class="raw">kept</div>' in rendered.html


def test_script_and_event_handlers_in_the_source_are_removed():
    """The README is trusted but hand-edited and PR-edited; the rendered
    fragment is sanitised regardless so a stray <script> or onclick never
    reaches the operator's authenticated session."""
    rendered = render_readme_html(
        "# T\n\n<script>alert(1)</script>\n\n"
        "<a href=\"javascript:alert(2)\" onclick=\"alert(3)\">x</a>\n\n"
        "<img src=x onerror=alert(4)>\n"
    )

    assert "<script" not in rendered.html
    assert "onclick" not in rendered.html
    assert "onerror" not in rendered.html
    assert "javascript:" not in rendered.html
    assert "alert(1)" not in rendered.html


def test_toc_anchors_survive_sanitisation():
    """The sanitiser must keep the heading ids the toc links point at."""
    rendered = render_readme_html("# T\n\n## Restoring\n\ntext\n")

    assert 'id="restoring"' in rendered.html
    assert 'href="#restoring"' in rendered.toc


def test_the_real_readme_renders():
    """The actual input this module has in production -- 88KB of Markdown
    that only this page ever parses."""
    from pathlib import Path

    rendered = render_readme_html(Path("README.md").read_text())

    assert rendered.html.strip()
    assert rendered.toc.strip()


def test_the_extension_list_is_what_the_rendering_relies_on():
    assert set(MARKDOWN_EXTENSIONS) == {"fenced_code", "tables", "sane_lists", "toc"}
