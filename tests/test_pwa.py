"""Tests for the PWA shell: manifest icons and the service worker route."""
import json

from app.main import STATIC


def test_manifest_e_json_valido_com_icones_reais():
    manifest = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["icons"]
    for icon in manifest["icons"]:
        assert icon["src"].startswith("/static/")
        caminho = STATIC / icon["src"][len("/static/"):]
        assert caminho.is_file(), f"ícone em falta no disco: {caminho}"


def test_apple_touch_icon_aponta_para_um_ficheiro_existente():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'rel="apple-touch-icon"' in html
    inicio = html.index('rel="apple-touch-icon"')
    href_inicio = html.index('href="', inicio) + len('href="')
    href_fim = html.index('"', href_inicio)
    href = html[href_inicio:href_fim]
    assert href.startswith("/static/")
    assert (STATIC / href[len("/static/"):]).is_file()
