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


# ---------- notification settings live in the office tab ----------

def _view_slice(html, view_id):
    """Returns the markup of a top-level <section id="view_id">...</section>,
    the same style tests/test_pwa.py already uses for handler blocks."""
    start = html.index(f'id="{view_id}"')
    end = html.index("</section>", start)
    return html[start:end]


def test_notif_settings_live_in_the_office_view_not_the_cafe_view():
    """People look for notification settings next to the other app settings
    (capsule price, stock threshold), which are on the office tab."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    escritorio = _view_slice(html, "vista-escritorio")
    cafe = _view_slice(html, "vista-cafe")
    assert 'id="notif-secao"' in escritorio
    assert 'id="notif-secao"' not in cafe


def test_notif_settings_are_closed_by_default():
    """Design review (Escritório tab): the accordion opens on demand like
    Definições and Pagamentos, instead of pushing those down every time."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    start = html.index('id="notif-secao"')
    end = html.index(">", start)
    tag = html[start:end]
    assert "open" not in tag


# ---------- errors on the notifications path are surfaced, not swallowed ----------

def test_app_never_swallows_an_error_on_the_notifications_path():
    """A bare `catch {` (or `catch{`) discards the exception: nothing reaches
    the console, nothing reaches the user. Regression for a real report where
    a user clicked "Receber notificacoes", the browser refused, and there was
    no way to tell why."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    marker = "// ---------- notificações push ----------"
    assert marker in js
    notif_js = js[js.index(marker):]
    assert "catch {" not in notif_js
    assert "catch{" not in notif_js


def test_app_logs_notification_errors_to_the_console():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    marker = "// ---------- notificações push ----------"
    notif_js = js[js.index(marker):]
    assert "console.error" in notif_js


def test_activate_notifications_handler_reports_the_real_error_to_the_user():
    """The toast shown to the user must include the caught error's name and
    message, e.g. "NotAllowedError, registration failed", not a generic
    apology with no diagnostic value."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    handler_start = js.index('$("btn-notif-ativar").onclick')
    handler_end = js.index("\n};", handler_start)
    handler = js[handler_start:handler_end]
    assert "catch (erro)" in handler
    assert "console.error" in handler
    assert "erro.name" in handler or "explicarErroNotif" in handler
    assert "erro.message" in handler or "explicarErroNotif" in handler
