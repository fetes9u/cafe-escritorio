"""Tests for the push notifications client: service worker handlers, the
settings screen and the permission-request gesture. These only inspect the
static files served, the same style used in tests/test_pwa.py, and never
depend on the server-side push endpoints (a different lane is building
those): a request to a missing endpoint failing is not this file's concern.
"""
from app.main import STATIC

EVENTOS = ["cafe", "compra", "pagamento", "stock_baixo", "registo"]


# ---------- service worker ----------

def test_sw_has_a_push_handler_that_shows_a_notification_with_icon():
    text = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'addEventListener("push"' in text
    push_start = text.index('addEventListener("push"')
    push_block = text[push_start:push_start + 800]
    assert "showNotification(" in push_block
    assert "icon-192.png" in push_block


def test_sw_has_a_notificationclick_handler_that_focuses_or_opens_a_window():
    text = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'addEventListener("notificationclick"' in text
    click_start = text.index('addEventListener("notificationclick"')
    click_block = text[click_start:click_start + 800]
    assert "focus()" in click_block
    assert "openWindow(" in click_block


def test_sw_still_never_caches_the_api():
    """The push additions must be appended, never replace the existing cache
    guard: a service worker that starts caching /api would leak one user's
    data to another (see test_pwa.py for the original assertion)."""
    text = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'pathname.startsWith("/api")' in text
    fetch_start = text.index('addEventListener("fetch"')
    fetch_end = text.index("});", fetch_start)
    fetch_block = text[fetch_start:fetch_end]
    assert 'pathname.startsWith("/api")' in fetch_block


def test_sw_fetch_handler_comes_before_the_push_additions():
    """Sanity check that we appended rather than rewrote: the original
    install/activate/fetch handlers still appear before the new ones."""
    text = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert text.index('addEventListener("fetch"') < text.index('addEventListener("push"')


# ---------- permission is requested only from a click ----------

def test_app_never_requests_notification_permission_outside_a_click_handler():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "requestPermission" in js

    # The startup IIFE runs unconditionally on page load; permission must
    # never be requested from there (fails outright on iOS, burns the
    # one-shot prompt on Android).
    startup_start = js.index("(async () => {\n  try { await entrar();")
    startup_end = js.index("})();", startup_start)
    assert "requestPermission" not in js[startup_start:startup_end]

    # It must be wired to the explicit "Receber notificações" button click.
    assert '$("btn-notif-ativar").onclick' in js
    handler_start = js.index('$("btn-notif-ativar").onclick')
    handler_end = js.index("\n};", handler_start)
    assert "requestPermission" in js[handler_start:handler_end]


def test_app_hides_the_feature_when_the_server_has_no_push_configured():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "503" in js


def test_app_treats_ios_outside_standalone_as_unsupported():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    # Reuses the install-hint detection helpers already in app.js, does not
    # redefine them.
    assert js.count("function isIOS()") == 1
    assert js.count("function isStandalone()") == 1
    assert "isIOS() && !isStandalone()" in js


# ---------- settings screen ----------

def test_index_has_a_toggle_for_each_of_the_five_notification_events():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for evento in EVENTOS:
        assert f'data-evento="{evento}"' in html, f"missing toggle for event: {evento}"


def test_index_toggles_default_to_checked():
    """Everyone starts subscribed to everything; turning things off is the
    explicit action, per spec."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    start = html.index('id="form-notif-prefs"')
    end = html.index("</form>", start)
    form = html[start:end]
    for evento in EVENTOS:
        tag_start = form.index(f'data-evento="{evento}"')
        tag_end = form.index(">", tag_start)
        assert "checked" in form[tag_start:tag_end]


def test_index_has_a_visible_activate_button_and_an_unsubscribe_control():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="btn-notif-ativar"' in html
    assert 'id="btn-notif-cancelar"' in html


def test_index_has_an_ios_install_first_notice():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="notif-ios-aviso"' in html
    start = html.index('id="notif-ios-aviso"')
    end = html.index(">", start)
    assert "ecrã principal" in html[start:end + 400]


def test_settings_screen_text_is_portuguese():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    start = html.index('id="notif-secao"')
    end = html.index("</details>", start)
    section = html[start:end]
    assert "Notificações" in section
    assert "notification" not in section.lower()
