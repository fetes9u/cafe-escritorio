"""Tests for the PWA shell: manifest icons and the service worker route."""
import json

from app.main import STATIC


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


def test_app_registers_the_root_scoped_service_worker():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'serviceWorker.register("/sw.js")' in js


def test_service_worker_never_caches_the_api():
    text = (STATIC / "sw.js").read_text(encoding="utf-8")
    start = text.index("SHELL_URLS = [") + len("SHELL_URLS = [")
    end = text.index("]", start)
    precache_list = text[start:end]
    assert "/api" not in precache_list
    # runtime guard: any request under /api must never go through the cache
    assert 'pathname.startsWith("/api")' in text
