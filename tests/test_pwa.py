"""Tests for the PWA shell: manifest icons and the service worker route."""
import json
import re

from app.main import STATIC


def _precache_list():
    text = (STATIC / "sw.js").read_text(encoding="utf-8")
    start = text.index("SHELL_URLS = [") + len("SHELL_URLS = [")
    end = text.index("]", start)
    return text[start:end]


def test_manifest_is_valid_json_with_icons_that_exist_on_disk():
    manifest = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["icons"]
    for icon in manifest["icons"]:
        assert icon["src"].startswith("/static/")
        path = STATIC / icon["src"][len("/static/"):]
        assert path.is_file(), f"missing icon file: {path}"


def test_apple_touch_icon_points_to_a_file_that_exists():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'rel="apple-touch-icon"' in html
    start = html.index('rel="apple-touch-icon"')
    href_start = html.index('href="', start) + len('href="')
    href_end = html.index('"', href_start)
    href = html[href_start:href_end]
    assert href.startswith("/static/")
    assert (STATIC / href[len("/static/"):]).is_file()


# ---------- service worker ----------

def test_service_worker_responds_200_with_js_content_type_at_root_scope(cliente):
    """The SW must control '/', not just '/static/'. Serving it at /sw.js
    (root), rather than only under /static/sw.js, means the default scope of
    a service worker (the directory of its own URL) is already '/', with no
    need for a Service-Worker-Allowed header."""
    r = cliente.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.request.url.path == "/sw.js"
    # proves the route serves the actual worker script, not just something JS shaped.
    # Compare bytes, not text: read_text() normalizes line endings while the HTTP body
    # does not, so a CRLF checkout on Windows would fail this for the wrong reason.
    assert r.content == (STATIC / "sw.js").read_bytes()
    assert 'addEventListener("fetch"' in r.text


def test_app_registers_the_root_scoped_service_worker():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'serviceWorker.register("/sw.js")' in js


def test_service_worker_never_caches_the_api():
    precache_list = _precache_list()
    assert "/api" not in precache_list
    # runtime guard: any request under /api must never go through the cache
    text = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'pathname.startsWith("/api")' in text


def test_every_precached_shell_url_actually_resolves(cliente):
    """cache.addAll() is all-or-nothing: one 404 in SHELL_URLS makes install()
    reject and the service worker never activates, silently. Guard every
    entry so a renamed or removed static file gets caught here."""
    urls = re.findall(r'"([^"]+)"', _precache_list())
    assert urls  # sanity: the list itself was found and is not empty
    for url in urls:
        r = cliente.get(url)
        assert r.status_code == 200, f"precached URL does not resolve: {url}"


def test_service_worker_does_not_serve_the_shell_cache_first():
    """The whole shell (including '/', so index.html) must never be answered
    straight from the cache without asking the network first: a stale cached
    '/' is exactly how a deployed UI change (e.g. a new settings screen)
    stops showing up for someone who already has the service worker installed."""
    text = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "cached || network" not in text


def test_service_worker_keeps_the_required_handlers_and_api_guard():
    text = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'pathname.startsWith("/api")' in text
    for handler in ("push", "notificationclick", "fetch", "install", "activate"):
        assert f'addEventListener("{handler}"' in text


def test_cache_version_was_bumped_past_v1():
    """A cache name that never changes means activate() never evicts the
    previous deploy's cache, since it only deletes caches with a different
    name than the current one."""
    text = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'CACHE_VERSION = "v1"' not in text


def test_manifest_has_description_and_scope():
    manifest = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("description")
    assert manifest.get("scope") == "/"
