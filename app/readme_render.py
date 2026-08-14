"""Renders README.md to an HTML fragment for the live /readme route
(app/routers/dashboard.py), always built fresh from the same Markdown
source on every request.
"""

from typing import NamedTuple

import markdown

MARKDOWN_EXTENSIONS = ["fenced_code", "tables", "sane_lists", "toc"]

# toc_depth "2-4" skips the single <h1> title (nothing to link to) and keeps
# #### subsections like "Restoring" -- "2-3" would drop those, and that's
# exactly the kind of section someone jumps to in an emergency.
MARKDOWN_EXTENSION_CONFIGS = {"toc": {"toc_depth": "2-4"}}


class RenderedReadme(NamedTuple):
    html: str
    toc: str


def render_readme_html(md_text: str) -> RenderedReadme:
    # SECURITY INVARIANT: the returned .html/.toc are rendered with `|safe` in
    # templates/readme.html (markdown output is HTML by design). This is only
    # safe because md_text is a TRUSTED source -- the repo's own README.md,
    # never user- or device-supplied input. If you ever point this helper at
    # untrusted content (asset notes, a chat message), sanitise the output
    # first (e.g. bleach.clean with an allowlist) -- otherwise it's stored XSS.
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
    return RenderedReadme(html=html, toc=md.toc)
