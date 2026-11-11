"""Static safety checks for the new ES-module frontend (docs/phase1b-shell.md §7.2).

They guard static/index.html and every static/js/**/*.js file:
templates are lit-html (auto-escaping), there are no HTML sinks, no inline handlers or styles,
fetch goes through api.js (which adds the X-Webjam CSRF header), imports are relative and
resolve, the vendored lit-html is pinned, and every href binding goes through a URL helper.
The legacy UI keeps its own checks in tests/test_legacy_frontend_safety.py until cutover.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
JS_DIR = STATIC / "js"
INDEX_HTML = STATIC / "index.html"
VENDOR = STATIC / "vendor" / "lit-html"

JS_FILES = sorted(JS_DIR.rglob("*.js"))
URL_HELPERS = ("trackUrl(", "playlistUrl(", "userUrl(", "localUrl(", "iconHref(")


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _line_of(src: str, idx: int) -> int:
    return src.count("\n", 0, idx) + 1


def _strip_comments(src: str) -> str:
    """Blank out // and /* */ comments (keeping offsets), outside strings and templates."""
    out = list(src)
    i, n = 0, len(src)
    mode = None  # None | "'" | '"' | "`"
    depth = []  # template ${ nesting
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if mode is None:
            if ch == "/" and nxt == "/":
                j = src.find("\n", i)
                j = n if j < 0 else j
                for k in range(i, j):
                    out[k] = " "
                i = j
                continue
            if ch == "/" and nxt == "*":
                j = src.find("*/", i + 2)
                j = n if j < 0 else j + 2
                for k in range(i, j):
                    if out[k] != "\n":
                        out[k] = " "
                i = j
                continue
            if ch in "'\"`":
                mode = ch
            elif ch == "}" and depth:
                depth.pop()
                mode = "`"
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if mode == "`" and ch == "$" and nxt == "{":
            depth.append(True)
            mode = None
            i += 2
            continue
        if ch == mode:
            mode = None
        i += 1
    return "".join(out)


def _template_end(src: str, start: int) -> int:
    """src[start] is the opening backtick; return the index of its closing backtick."""
    i = start + 1
    while i < len(src):
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            return i
        if src.startswith("${", i):
            i = _expr_end(src, i + 2)
            continue
        i += 1
    return len(src)


def _expr_end(src: str, i: int) -> int:
    """Scan a ${ ... } expression starting at i; return the index just past its '}'."""
    depth = 1
    while i < len(src) and depth:
        ch = src[i]
        if ch in "'\"":
            j = i + 1
            while j < len(src) and src[j] != ch:
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "`":
            i = _template_end(src, i) + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return i


def _templates(src: str):
    """Yield (offset, body) for every html`...` / svg`...` tagged template (nested ones too)."""
    for m in re.finditer(r"\b(?:html|svg)`", src):
        start = m.end() - 1
        end = _template_end(src, start)
        yield start, src[start + 1 : end]


def _static_parts(body: str) -> str:
    """The template body with every ${...} replaced by a placeholder."""
    out, i = [], 0
    while True:
        j = body.find("${", i)
        if j < 0:
            out.append(body[i:])
            return "".join(out)
        out.append(body[i:j])
        out.append("\x00")
        i = _expr_end(body, j + 2)


@pytest.fixture(scope="module")
def sources():
    assert JS_FILES, "no files under static/js"
    return {p: _read(p) for p in JS_FILES}


def test_js_tree_exists():
    for name in ("main.js", "lit.js", "api.js", "store.js", "router.js", "actions.js", "slots.js", "keys.js",
                 "paste-router.js", "format.js", "components/track-row.js", "player/player.js",
                 "player/youtube.js", "panels/dock.js", "panels/crate.js", "views/home.js", "views/playlist.js"):
        assert (JS_DIR / name).is_file(), name


# 1. No HTML sinks or string evaluation.
BANNED = [
    (re.compile(r"\binnerHTML\b"), "innerHTML"),
    (re.compile(r"\bouterHTML\b"), "outerHTML"),
    (re.compile(r"\binsertAdjacentHTML\b"), "insertAdjacentHTML"),
    (re.compile(r"\bdocument\.write"), "document.write"),
    (re.compile(r"\bDOMParser\b"), "DOMParser"),
    (re.compile(r"\bcreateContextualFragment\b"), "createContextualFragment"),
    (re.compile(r"\bsrcdoc\b"), "srcdoc"),
    (re.compile(r"\beval\s*\("), "eval("),
    (re.compile(r"\bnew\s+Function\b"), "new Function"),
    (re.compile(r"\bset(?:Timeout|Interval)\s*\(\s*['\"`]"), "setTimeout/setInterval with a string"),
]


@pytest.mark.parametrize("path", JS_FILES + [INDEX_HTML], ids=lambda p: _rel(p))
def test_no_html_sinks(path):
    src = _read(path)
    code = _strip_comments(src) if path.suffix == ".js" else src
    for rx, name in BANNED:
        m = rx.search(code)
        assert not m, f"{_rel(path)}:{_line_of(src, m.start())}: {name}"


# 2. No unsafe lit directives; only lit.js imports the vendor tree.
def test_no_unsafe_directives_and_vendor_only_via_lit(sources):
    for path, src in sources.items():
        code = _strip_comments(src)
        for word in ("unsafeHTML", "unsafeSVG", "unsafeStatic", "templateContent"):
            assert word not in code, f"{_rel(path)}: {word}"
        if path.name == "lit.js" and path.parent == JS_DIR:
            continue
        assert "vendor/" not in code, f"{_rel(path)} imports from vendor/; use js/lit.js"
    assert not list(VENDOR.rglob("unsafe-*.js")), "unsafe directives must not be vendored"


# 3. No inline event handlers.
ON_ATTR = re.compile(r"\son[a-z]+\s*=", re.I)


def test_no_inline_handlers_in_templates(sources):
    offenders = []
    for path, src in sources.items():
        code = _strip_comments(src)
        for off, body in _templates(code):
            m = ON_ATTR.search(_static_parts(body))
            if m:
                offenders.append(f"{_rel(path)}:{_line_of(src, off)}: {m.group(0).strip()}")
    assert not offenders, offenders


def test_no_on_property_assignments(sources):
    rx = re.compile(r"\.on[a-z]+\s*=(?!=)")
    offenders = []
    for path, src in sources.items():
        code = _strip_comments(src)
        for m in rx.finditer(code):
            line_start = code.rfind("\n", 0, m.start()) + 1
            line = code[line_start : code.find("\n", m.start())]
            if path.name == "youtube.js" and "window.onYouTubeIframeAPIReady" in line:
                continue
            offenders.append(f"{_rel(path)}:{_line_of(src, m.start())}: {line.strip()}")
    assert not offenders, offenders


def test_index_html_is_static():
    html = _read(INDEX_HTML)
    assert not ON_ATTR.search(html), "inline handler in index.html"
    assert not re.search(r"\sstyle\s*=", html, re.I), "style attribute in index.html"
    assert not re.search(r"<style\b", html, re.I), "inline <style> in index.html"
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.I | re.S)
    assert len(scripts) == 1, f"exactly one script tag expected, got {len(scripts)}"
    attrs, body = scripts[0]
    assert body.strip() == "", "inline script in index.html"
    assert re.search(r'type="module"', attrs)
    src = re.search(r'src="([^"]+)"', attrs).group(1)
    assert src.split("?")[0] == "/static/js/main.js", src
    for m in re.finditer(r'<(?:link|script|img)\b[^>]*(?:href|src)="(https?:)?//', html, re.I):
        pytest.fail(f"third-party resource in index.html: {m.group(0)}")


# 4. No style= in templates, no javascript: anywhere.
def test_no_style_attributes_in_templates(sources):
    offenders = []
    for path, src in sources.items():
        code = _strip_comments(src)
        for off, body in _templates(code):
            if re.search(r"\sstyle\s*=", _static_parts(body), re.I):
                offenders.append(f"{_rel(path)}:{_line_of(src, off)}")
    assert not offenders, offenders


@pytest.mark.parametrize("path", JS_FILES + [INDEX_HTML], ids=lambda p: _rel(p))
def test_no_javascript_urls(path):
    assert "javascript:" not in _read(path).lower(), _rel(path)


# 5. fetch only in api.js, which sends the CSRF header on writes.
def test_fetch_only_in_api_js(sources):
    for path, src in sources.items():
        if path == JS_DIR / "api.js":
            continue
        assert not re.search(r"\bfetch\s*\(", _strip_comments(src)), f"{_rel(path)} calls fetch()"
    api = _read(JS_DIR / "api.js")
    assert re.search(r"""\[\s*['"]X-Webjam['"]\s*\]\s*=\s*['"]1['"]""", api) or "'X-Webjam': '1'" in api
    assert re.search(r"if \(method !== 'GET'\) \{\s*headers\['X-Webjam'\] = '1';", api), \
        "api.js must add X-Webjam: 1 to every non-GET request"
    assert "credentials: 'same-origin'" in api


def test_api_never_sends_user_id_in_google_mode():
    api = _read(JS_DIR / "api.js")
    assert "if (state.auth.mode === 'google') return null;" in api


# 6. Relative imports that resolve.
IMPORT_RX = re.compile(
    r"""(?:\bimport\s+(?:[\w*{}\s,]+\s+from\s+)?|\bexport\s+(?:\*|\{[^}]*\})\s+from\s+|\bimport\s*\(\s*)(['"])([^'"]+)\1"""
)


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: _rel(p))
def test_imports_are_relative_and_resolve(path):
    code = _strip_comments(_read(path))
    for m in IMPORT_RX.finditer(code):
        spec = m.group(2)
        assert spec.startswith(("./", "../")), f"{_rel(path)}: bare or absolute import {spec!r}"
        target = (path.parent / spec).resolve()
        assert target.is_file(), f"{_rel(path)}: import {spec!r} does not resolve"
        assert STATIC in target.parents, f"{_rel(path)}: import {spec!r} leaves static/"


# 7. Pinned lit-html with resolvable internal imports.
def test_vendored_lit_html_is_pinned():
    assert _read(VENDOR / "VERSION").strip() == "3.2.1"
    assert "BSD-3-Clause" in _read(VENDOR / "LICENSE") or "Redistribution and use" in _read(VENDOR / "LICENSE")
    files = ["lit-html.js", "directive.js", "directive-helpers.js", "async-directive.js",
             "directives/repeat.js", "directives/class-map.js", "directives/live.js", "directives/ref.js"]
    for f in files:
        assert (VENDOR / f).is_file(), f
    for f in VENDOR.rglob("*.js"):
        for m in re.finditer(r"""from\s*["']([^"']+)["']""", _read(f)):
            spec = m.group(1)
            assert spec.startswith(("./", "../")), f"{f}: {spec}"
            assert (f.parent / spec).resolve().is_file(), f"{f}: {spec} does not resolve"


def test_fonts_are_vendored_with_licences():
    fonts = STATIC / "vendor" / "fonts"
    for name in ("instrument-sans.woff2", "instrument-serif.woff2", "jetbrains-mono.woff2"):
        assert (fonts / name).stat().st_size > 10_000, name
    assert len(list(fonts.glob("OFL*.txt"))) == 3


# 8. href bindings go through a URL helper (or a literal same-origin path).
def test_href_bindings_use_url_helpers(sources):
    rx = re.compile(r"""\b(?:href|src)\s*=\s*["']?\$\{""")
    offenders = []
    for path, src in sources.items():
        code = _strip_comments(src)
        for off, body in _templates(code):
            for m in rx.finditer(body):
                attr = m.group(0).split("=")[0].strip()
                expr = body[m.end() : _expr_end(body, m.end()) - 1].strip()
                if attr == "src":
                    ok = expr.startswith("thumbSrc(")
                else:
                    ok = expr.startswith(URL_HELPERS) or re.match(r"""^['"`]/(?!/)""", expr) is not None
                if not ok:
                    offenders.append(f"{_rel(path)}:{_line_of(src, off)}: {attr}=${{{expr}}}")
    assert not offenders, offenders


# 9. Syntax.
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: _rel(p))
def test_node_check(path):
    res = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


# Lane wiring: main.js imports F's registrations before the (commented) lane entry modules.
def test_main_imports_core_before_lanes():
    main = _read(JS_DIR / "main.js")
    core = main.index("import './core.js';")
    routes = main.index("import './routes.js';")
    for lane in ("editing/index.js", "discovery/index.js", "share/index.js"):
        assert lane in main, lane
        assert main.index(lane) > max(core, routes), lane


def test_stylesheets_linked_for_every_lane():
    html = _read(INDEX_HTML)
    hrefs = re.findall(r'<link rel="stylesheet" href="([^"]+)"', html)
    expected = ["tokens", "base", "layout", "components", "editing", "discovery", "share-import", "mobile"]
    assert [h.rsplit("/", 1)[-1].removesuffix(".css") for h in hrefs] == expected
    for h in hrefs:
        assert (ROOT / h.lstrip("/")).is_file(), h
