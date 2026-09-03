"""Renders README.md to an HTML fragment for the live /readme route
(app/routers/dashboard.py), always built fresh from the same Markdown
source on every request.
"""

from typing import NamedTuple

import markdown
import nh3

MARKDOWN_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "toc"]

# toc_depth "2-4" skips the single <h1> title (nothing to link to) and keeps
# #### subsections like "Restoring" -- "2-3" would drop those, and that's
# exactly the kind of section someone jumps to in an emergency.
MARKDOWN_EXTENSION_CONFIGS = {"toc": {"toc_depth": "2-4"}}


# nh3's default allowlist covers everything Markdown emits (headings, lists,
# tables, pre/code, links, images) but no attributes beyond a few per-tag
# ones. The toc extension needs `id` on headings for its anchors to resolve
# and `class` on its wrapper div; fenced code gets class="language-x".
# Neither can carry script. Everything else -- <script>, on* handlers,
# javascript: URLs, <iframe>, <style> -- is dropped.
_ALLOWED_ATTRIBUTES = {tag: set(attrs) for tag, attrs in nh3.ALLOWED_ATTRIBUTES.items()}
_ALLOWED_ATTRIBUTES.setdefault("*", set()).update({"id", "class"})


def sanitise(html: str) -> str:
    # link_rel=None: nh3 would otherwise stamp rel="noopener noreferrer" on
    # every <a>, including the toc's in-page #anchors. That rel only matters
    # with target="_blank", which Markdown never emits, and the app's
    # Referrer-Policy: same-origin (app/main.py) already withholds the
    # referrer from external links.
    return nh3.clean(html, attributes=_ALLOWED_ATTRIBUTES, link_rel=None)


class RenderedReadme(NamedTuple):
    html: str
    toc: str


def render_readme_html(md_text: str) -> RenderedReadme:
    # SECURITY INVARIANT: the returned .html/.toc are rendered with `|safe` in
    # templates/readme.html (markdown output is HTML by design). The input is
    # the repo's own README.md, a trusted source -- but Python-Markdown passes
    # raw HTML in its input straight through, so the output is sanitised with
    # nh3 anyway: the README is edited by hand and by pull request, and a
    # <script> that slipped into it would otherwise run in the operator's
    # authenticated session (the CSP has no 'unsafe-inline' for script-src,
    # so that would need an external file too -- but two layers is the point).
    # Do NOT remove the sanitiser to "fix" some HTML the README wants; extend
    # _ALLOWED_ATTRIBUTES instead.
    # The module-level markdown.markdown() convenience function builds and
    # discards a Markdown instance internally, so there's no way to reach
    # the toc extension's generated table of contents through it -- use the
    # class form instead and read .toc off the instance after converting.
    # A fresh instance per call (matching the previous per-request
    # behaviour) sidesteps needing to .reset() a reused one.
    md = markdown.Markdown(
        extensions=MARKDOWN_EXTENSIONS,
        extension_configs=MARKDOWN_EXTENSION_CONFIGS,
    )
    html = md.convert(md_text)
    return RenderedReadme(html=sanitise(html), toc=sanitise(md.toc))
