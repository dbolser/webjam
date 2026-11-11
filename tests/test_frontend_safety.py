"""Static safety checks for the vanilla-JS frontend (Phase 0, issue #1).

These tests grep static/app.js and static/index.html for the XSS patterns that
the security review found: data interpolated into JavaScript inside inline
on*="..." handlers with only HTML escaping (esc()), which the browser decodes
back before the JS runs. Every interpolation in a handler must now go through
jsArg(...) (JSON.stringify + HTML-attribute escaping) or Number(...).

They are deliberately strict so that new inline handlers cannot reintroduce the
bug. Phase 1 replaces inline handlers with data-* attributes and delegated
listeners, at which point most of this file can go.
"""

import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "static" / "app.js"
INDEX_HTML = ROOT / "static" / "index.html"

SAFE_HANDLER_WRAPPERS = ("jsArg", "Number")
HANDLER_ATTR = re.compile(r"\bon[a-z]+=([\"'])")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _line_of(src: str, idx: int) -> int:
    return src.count("\n", 0, idx) + 1


def _end_of_interpolation(src: str, start: int) -> int:
    """Given src[start:start+2] == '${', return the index just past its '}'."""
    depth = 1
    i = start + 2
    while i < len(src) and depth:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return i


def _attribute_body_end(src: str, start: int, quote: str) -> int:
    """Scan from start to the closing quote of an attribute value, skipping
    over ${...} interpolations (which may themselves contain quotes or nested
    template literals)."""
    i = start
    while i < len(src):
        if src.startswith("${", i):
            i = _end_of_interpolation(src, i)
            continue
        if src[i] == quote:
            return i
        i += 1
    return i


def _interpolations(body: str):
    """Yield (offset, expression) for every top-level ${...} in body."""
    i = 0
    while True:
        i = body.find("${", i)
        if i < 0:
            return
        end = _end_of_interpolation(body, i)
        yield i, body[i + 2 : end - 1]
        i = end


def _fully_wrapped(expr: str, wrappers) -> bool:
    """True if expr is exactly `wrapper( ... )` with the wrapper's own closing
    paren as the final character (so `Number(a) + b` does not count)."""
    expr = expr.strip()
    for name in wrappers:
        if not expr.startswith(name + "("):
            continue
        depth = 0
        in_str = None
        for pos, ch in enumerate(expr[len(name) :], start=len(name)):
            if in_str:
                if ch == "\\":
                    continue
                if ch == in_str:
                    in_str = None
                continue
            if ch in "'\"`":
                in_str = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return pos == len(expr) - 1
    return False


def _handler_attributes(src: str):
    """Yield (line, quote, body) for each inline on*= attribute in src."""
    for m in HANDLER_ATTR.finditer(src):
        quote = m.group(1)
        start = m.end()
        end = _attribute_body_end(src, start, quote)
        yield _line_of(src, m.start()), quote, src[start:end]


def _attributes_named(src: str, name: str):
    pattern = re.compile(r"\b" + re.escape(name) + r'="')
    for m in pattern.finditer(src):
        start = m.end()
        end = _attribute_body_end(src, start, '"')
        yield _line_of(src, m.start()), src[start:end]


@pytest.fixture(scope="module")
def app_js() -> str:
    return _read(APP_JS)


@pytest.fixture(scope="module")
def index_html() -> str:
    return _read(INDEX_HTML)


# --- Inline handler (on*=) checks -------------------------------------------


def test_scanner_actually_sees_the_handlers(app_js):
    """Guard against a vacuous pass if the scanner regex ever stops matching."""
    with_interp = [b for _, _, b in _handler_attributes(app_js) if "${" in b]
    assert len(with_interp) >= 80, len(with_interp)


@pytest.mark.parametrize("path", [APP_JS, INDEX_HTML], ids=["app.js", "index.html"])
def test_no_quoted_interpolation_inside_handlers(path):
    """The original bug: on*="f('${esc(x)}')". Any '${ or "${ inside a
    handler means data is being placed inside a JS string literal by hand."""
    src = _read(path)
    offenders = []
    for line, _quote, body in _handler_attributes(src):
        for off, expr in _interpolations(body):
            before = body[off - 1] if off > 0 else ""
            if before in ("'", '"', "`") and not _fully_wrapped(expr, SAFE_HANDLER_WRAPPERS):
                offenders.append(f"{path.name}:{line}: {before}${{{expr}}}")
            elif before in ("'", '"', "`"):
                # Even jsArg() output must not be wrapped in extra quotes: it
                # already produces a complete JS literal.
                offenders.append(f"{path.name}:{line}: quoted {before}${{{expr}}}")
    assert not offenders, "Quoted interpolation inside inline handler:\n" + "\n".join(offenders)


@pytest.mark.parametrize("path", [APP_JS, INDEX_HTML], ids=["app.js", "index.html"])
def test_every_handler_interpolation_is_jsarg_or_number(path):
    src = _read(path)
    offenders = []
    for line, _quote, body in _handler_attributes(src):
        for _off, expr in _interpolations(body):
            if not _fully_wrapped(expr, SAFE_HANDLER_WRAPPERS):
                offenders.append(f"{path.name}:{line}: ${{{expr.strip()[:100]}}}")
    assert not offenders, (
        "Interpolations in on*= handlers must be ${jsArg(...)} or ${Number(...)}:\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("path", [APP_JS, INDEX_HTML], ids=["app.js", "index.html"])
def test_no_esc_inside_handlers(path):
    """esc() alone is never correct in a JS context: &#39; decodes to '."""
    src = _read(path)
    offenders = [
        f"{path.name}:{line}"
        for line, _q, body in _handler_attributes(src)
        if re.search(r"\$\{\s*esc\(", body)
    ]
    assert not offenders, offenders


def test_no_single_quoted_handlers_with_interpolation(app_js, index_html):
    for name, src in (("app.js", app_js), ("index.html", index_html)):
        bad = [line for line, q, body in _handler_attributes(src) if q == "'" and "${" in body]
        assert not bad, f"{name}: single-quoted on*='...' with interpolation at lines {bad}"


def test_no_handler_body_built_from_a_variable(app_js):
    """onclick="${someCodeString}" bypasses the wrapper rule; build the call
    inline with jsArg/Number arguments instead."""
    bad = [
        line
        for line, _q, body in _handler_attributes(app_js)
        if re.fullmatch(r"\s*\$\{[^}]*\}\s*", body)
    ]
    assert not bad, bad


def test_index_html_handlers_are_static(index_html):
    bad = [line for line, _q, body in _handler_attributes(index_html) if "${" in body]
    assert not bad, bad


# --- href / style attribute checks ------------------------------------------


def test_href_interpolations_are_encoded(app_js):
    offenders = []
    for line, body in _attributes_named(app_js, "href"):
        for _off, expr in _interpolations(body):
            if not _fully_wrapped(expr, ("Number", "encodeURIComponent", "esc")):
                offenders.append(f"app.js:{line}: href ${{{expr}}}")
    assert not offenders, "\n".join(offenders)


def test_style_colour_interpolations_are_validated_and_escaped(app_js):
    """avatar_color comes from the users table; validate it as a hex colour
    (safeColor) and HTML-escape it before it reaches a style attribute."""
    offenders = []
    for line, body in _attributes_named(app_js, "style"):
        for _off, expr in _interpolations(body):
            expr = expr.strip()
            if re.search(r"colou?r", expr, re.I) and not (
                expr.startswith("esc(safeColor(") and _fully_wrapped(expr, ("esc",))
            ):
                offenders.append(f"app.js:{line}: style ${{{expr}}}")
    assert not offenders, "\n".join(offenders)


def test_every_avatar_colour_goes_through_safe_color(app_js):
    for m in re.finditer(r"[\w.]*avatar_color", app_js):
        prefix = app_js[max(0, m.start() - len("safeColor(")) : m.start()]
        assert prefix == "safeColor(", f"app.js:{_line_of(app_js, m.start())}: raw {m.group(0)}"


def test_other_style_interpolations_are_numeric_or_escaped(app_js):
    offenders = []
    for line, body in _attributes_named(app_js, "style"):
        for _off, expr in _interpolations(body):
            if not _fully_wrapped(expr, ("Number", "esc")):
                offenders.append(f"app.js:{line}: style ${{{expr}}}")
    assert not offenders, "\n".join(offenders)


# --- innerHTML sinks fed by error messages / toasts --------------------------


def test_error_messages_are_escaped_before_html(app_js):
    # showToast() renders its argument as HTML.
    raw_toast = re.findall(r"showToast\(\s*(?:err|error|e)\.message", app_js)
    assert not raw_toast, raw_toast
    raw_interp = re.findall(r"\$\{\s*(?:err|error|e)\.message\s*\}", app_js)
    assert not raw_interp, raw_interp


def test_toast_template_interpolations_escape_text(app_js):
    """Every showToast(`...`) template interpolation of a title/name/message
    must be esc()'d (numbers and pre-built constant suffixes are fine)."""
    offenders = []
    for m in re.finditer(r"showToast\(\s*`", app_js):
        start = m.end()
        end = _attribute_body_end(app_js, start, "`")
        body = app_js[start:end]
        for _off, expr in _interpolations(body):
            if re.search(r"(title|name|message|username|label)\b", expr) and not expr.strip().startswith("esc("):
                offenders.append(f"app.js:{_line_of(app_js, m.start())}: ${{{expr}}}")
    assert not offenders, "\n".join(offenders)


# --- api() CSRF header -------------------------------------------------------


def test_api_sends_x_webjam_header(app_js):
    m = re.search(r"async function api\(path, options = \{\}\) \{(.*?)\n\}", app_js, re.S)
    assert m, "api() helper not found"
    body = m.group(1)
    assert re.search(r"'X-Webjam':\s*'1'", body), "api() must send X-Webjam: 1"
    # headers must come after ...options so a caller's options cannot drop it.
    assert body.index("...options") < body.index("headers:"), "spread options before headers"


def test_all_fetches_go_through_api(app_js):
    """Only api() may call fetch(), so every request carries X-Webjam."""
    assert len(re.findall(r"\bfetch\(", app_js)) == 1


# --- Behavioural check of the helpers (needs node) ---------------------------


def _extract_function(src: str, name: str) -> str:
    m = re.search(r"^function " + re.escape(name) + r"\(.*?^\}", src, re.S | re.M)
    assert m, f"{name}() not found in app.js"
    return m.group(0)


PAYLOADS = [
    "plain",
    "it's",
    'say "hi"',
    "');alert(document.cookie);//",
    '");alert(1);//',
    "\\'); alert(1); //",
    "</script><img src=x onerror=alert(1)>",
    "&#39;);alert(1);//",
    "&quot;,alert(1),&quot;",
    "line\nbreak sep",
    "${alert(1)}",
    "`backtick`",
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_jsarg_round_trips_through_html_attribute_decoding(app_js):
    """Simulate the browser: jsArg() output is HTML-decoded (as the attribute
    parser does) and then evaluated as JS. The result must be exactly the
    original value, i.e. the payload never escapes its literal."""
    helpers = _extract_function(app_js, "esc") + "\n" + _extract_function(app_js, "jsArg")
    script = helpers + "\nconst vals = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n" \
        "process.stdout.write(JSON.stringify(vals.map((v) => jsArg(v))));"
    values = PAYLOADS + [42, None, True]
    out = subprocess.run(
        ["node", "-e", script], input=json.dumps(values), capture_output=True, text=True, check=True
    ).stdout
    encoded = json.loads(out)
    for original, attr_text in zip(values, encoded):
        assert '"' not in attr_text and "'" not in attr_text and "<" not in attr_text
        decoded = html.unescape(attr_text)  # what the JS engine receives
        # Evaluate the decoded text as a bare JS expression, as the handler would.
        evaluated = subprocess.run(
            ["node", "-e", "process.stdout.write(JSON.stringify([" + decoded + "]))"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert json.loads(evaluated) == [original], (original, decoded)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_safe_color_rejects_css_injection(app_js):
    script = _extract_function(app_js, "safeColor") + """
const cases = ['#abc', '#A1B2C3', 'red', '#12345', '#abc; background:url(//evil)',
  'url(javascript:alert(1))', '', null, undefined, ' #ffffff '];
process.stdout.write(JSON.stringify(cases.map((c) => safeColor(c))));"""
    out = json.loads(
        subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    )
    fallback = out[2]
    assert out[0] == "#abc" and out[1] == "#A1B2C3" and out[9] == "#ffffff"
    assert re.fullmatch(r"#[0-9a-fA-F]{3,6}", fallback)
    assert out[3:9] == [fallback] * 6
