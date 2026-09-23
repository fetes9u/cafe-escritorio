"""Tests for the per-person history client: the Histórico tab, its month
calendar and the "Último café" line on the Café page. These only inspect
the static files served, the same style as tests/test_push_frontend.py;
the one exception binds the fields the client dereferences to the real
responses, as tests/test_contrato_api.py does (tests/test_historico.py
covers the endpoint itself).
"""
from app.main import STATIC
from tests.conftest import regista


def _app_js():
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _funcao(js, nome):
    """Extracts `async function <nome>(...) { ... }` up to its closing brace.
    A declaration ends with "\\n}" (no semicolon), unlike the "\\n};" that
    closes the arrow handlers sliced in tests/test_push_frontend.py."""
    marcador = f"async function {nome}("
    assert marcador in js, f"could not find {marcador} in app.js"
    inicio = js.index(marcador)
    fim = js.index("\n}", inicio)
    return js[inicio:fim]


# ---------- index.html: the tab, its section and the last-coffee line ----------

def test_index_has_the_historico_tab_between_cafe_and_escritorio():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-vista="historico"' in html
    assert html.index('data-vista="cafe"') < html.index('data-vista="historico"') < html.index('data-vista="escritorio"')


def test_index_has_the_historico_section_with_its_ids():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for id_ in ("vista-historico", "hist-aviso", "hist-anterior", "hist-mes", "hist-seguinte", "hist-grelha", "hist-detalhe"):
        assert f'id="{id_}"' in html, f"missing #{id_}"


def test_index_has_the_last_coffee_line_under_the_coffee_button():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="ultimo-cafe"' in html
    assert html.index('id="btn-cafe"') < html.index('id="ultimo-cafe"')


# ---------- app.js: fetch, snapshot and the offline queue on the calendar ----------

def test_app_fetches_the_historico_endpoint():
    js = _app_js()
    corpo = _funcao(js, "carregarHistorico")
    assert '"/historico"' in corpo


def test_app_snapshots_the_month_under_the_historico_prefix():
    """The snapshot key is historico:<utilizador_id>:<mes>: written through
    guardarInstantaneo with the month as suffix and read back literally on
    the offline path, so a cached September never answers for August."""
    js = _app_js()
    corpo = _funcao(js, "carregarHistorico")
    assert 'guardarInstantaneo("historico"' in corpo
    assert "`historico:${idAtual}:${alvo}`" in corpo
    # the existing callers keep their two-part keys
    assert 'guardarInstantaneo("eu", eu.utilizador.id, eu)' in js
    assert 'guardarInstantaneo("escritorio", idAtual, escritorio)' in js


def test_app_merges_the_offline_queue_into_the_calendar():
    js = _app_js()
    corpo = _funcao(js, "carregarHistorico")
    assert 'idbTodos("fila")' in corpo
    # the snapshot is stored from the raw response, before the queue is read
    assert corpo.index('guardarInstantaneo("historico"') < corpo.index('idbTodos("fila")')


def test_app_shows_the_offline_warnings_on_the_historico_tab():
    js = _app_js()
    corpo = _funcao(js, "carregarHistorico")
    assert "erro.rede" in corpo
    assert "Sem ligação. A mostrar o último estado conhecido" in corpo
    assert "Sem ligação e sem histórico guardado para este mês" in corpo


def test_app_wires_the_tab_into_the_nav_dispatcher():
    js = _app_js()
    inicio = js.index('$("abas").onclick')
    bloco = js[inicio:js.index("\n};", inicio)]
    assert 'b.dataset.vista === "historico"' in bloco
    assert "carregarHistorico()" in bloco
    assert 'mostrar("historico")' in bloco


# ---------- app.js: the "Último café" line ----------

def test_last_coffee_text_covers_every_case_of_the_spec():
    js = _app_js()
    assert "function textoUltimoCafe(" in js
    inicio = js.index("function textoUltimoCafe(")
    corpo = js[inicio:js.index("\n}", inicio)]
    assert "Ainda nenhum café." in corpo
    assert '"hoje"' in corpo
    assert '"ontem"' in corpo
    assert "por sincronizar" in corpo


def test_last_coffee_line_is_refreshed_wherever_the_queue_warning_is():
    """The line reads the same queue as atualizarAvisoFila(), so it must be
    redrawn at the same moments: login, after marking, after undo, after
    sync. Count both, allowing the optimistic direct write in btn-cafe."""
    js = _app_js()
    corpo = _funcao(js, "atualizarUltimoCafe")
    assert 'idbTodos("fila")' in corpo
    assert "eu.ultimo_cafe" in corpo
    for funcao in ("entrar", "sincronizarFila"):
        assert "atualizarUltimoCafe()" in _funcao(js, funcao), f"{funcao} does not refresh the last-coffee line"
    for handler in ("btn-cafe", "btn-desfazer"):
        inicio = js.index(f'$("{handler}").onclick')
        bloco = js[inicio:js.index("\n};", inicio)]
        assert "atualizarUltimoCafe()" in bloco, f"{handler} does not refresh the last-coffee line"


# ---------- contract: the fields the client dereferences exist in the live JSON ----------

def test_the_fields_the_client_reads_exist_in_the_real_responses(cliente):
    """textoUltimoCafe() reads eu.ultimo_cafe and desenharHistorico() reads
    historico.hoje and historico.mes; a text assertion on app.js alone would
    stay green if the server dropped either, so bind them to the responses."""
    js = _app_js()
    assert "eu.ultimo_cafe" in js
    assert "h.hoje" in js and "h.mes" in js
    c = regista(cliente, "Ana")
    eu = c.get("/api/eu").json()
    assert "ultimo_cafe" in eu
    hist = c.get("/api/historico").json()
    assert set(hist) >= {"mes", "hoje", "dias"}
    assert len(hist["hoje"]) == 10 and hist["hoje"][:7] == hist["mes"]  # "YYYY-MM-DD", compared as-is by the client


# ---------- histórico: separador Cafés | Dinheiro ----------

def test_index_has_the_hist_selector_with_both_options():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="hist-selector"' in html
    assert 'data-hist-vista="cafes"' in html
    assert 'data-hist-vista="dinheiro"' in html
    assert 'id="hist-dinheiro"' in html


def test_cafes_is_the_default_hist_subview():
    """The switch must default to Cafés: it is a class on the button in the
    markup, and the nav dispatcher resets to it every time the tab is opened."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    start = html.index('data-hist-vista="cafes"')
    end = html.index(">", start)
    assert "activa" in html[start:end]
    assert html.index('id="hist-dinheiro"') > 0 and 'hidden' in html[html.index('id="hist-dinheiro"'):html.index('id="hist-dinheiro"') + 40]

    js = _app_js()
    inicio = js.index('$("abas").onclick')
    bloco = js[inicio:js.index("\n};", inicio)]
    assert 'selecionarHistVista("cafes")' in bloco


def test_app_fetches_movimentos_for_the_dinheiro_subview():
    js = _app_js()
    corpo = _funcao(js, "carregarDinheiro")
    assert '"/movimentos"' in corpo
    assert 'guardarInstantaneo("movimentos"' in corpo


def test_dinheiro_reads_the_fields_the_contract_promises():
    """Binds this client to every field section 4.5 promises for
    GET /api/movimentos, so a dropped field breaks here, not silently."""
    js = _app_js()
    assert "function desenharDinheiro()" in js
    inicio = js.index("function desenharDinheiro()")
    corpo = js[inicio:js.index("\n}", inicio)]
    for campo in ("d.saldo_cent", "d.transferencias", "d.compras", "d.meses"):
        assert campo in corpo, f"desenharDinheiro must read {campo}"
    for campo in ("t.sentido", "t.outro", "t.valor_cent", "t.em", "t.confirmada_em", "t.anulada_em", "t.anulada_por"):
        assert campo in corpo, f"desenharDinheiro must read {campo} off a transferencia"
    for campo in ("it.dado.capsulas", "it.dado.custo_cent"):
        assert campo in corpo, f"desenharDinheiro must read {campo} off a compra"
    for campo in ("m.mes", "m.cafes", "m.valor_cent"):
        assert campo in corpo, f"desenharDinheiro must read {campo} off a mes"


def test_dinheiro_lista_sends_the_right_verb_for_each_action():
    js = _app_js()
    marcador = '$("dinheiro-lista").onclick'
    assert marcador in js
    inicio = js.index(marcador)
    bloco = js[inicio:js.index("\n};", inicio)]
    assert "/transferencias/" in bloco
    assert '"confirmar" : "anular"' in bloco


# ---------- service worker ----------

def test_cache_version_is_v8():
    text = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'CACHE_VERSION = "v8"' in text


# ---------- caixa: preco/simular is gone, the new sections exist ----------

def test_no_preco_simular_call_is_left_anywhere_in_the_client():
    """The average-price preview (and its endpoint) is removed by the caixa
    spec (section 4.9: `GET /api/preco/simular` sai, 404); nothing in the
    client may still reference it."""
    js = _app_js()
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "preco/simular" not in js
    assert "compra-preview" not in js
    assert "compra-preview" not in html


def test_destination_change_pairs_para_caixa_with_recebedor_id_into_one_line():
    """A pagamento's destination moving between Caixa and a person writes two
    historico_alteracoes rows (campo para_caixa and campo recebedor_id) from
    the same PATCH. The server computes the request's instant once and
    passes it to every regista_alteracao() call of that request, so both
    rows share the exact same em; the client pairs them on em + utilizador
    equality, order-independent (no reliance on adjacency or write order).
    The expander must pair them into a single line before rendering."""
    js = _app_js()
    assert "function juntarAlteracoesDestino(" in js
    inicio = js.index("function juntarAlteracoesDestino(")
    corpo = js[inicio:js.index("\n}", inicio)]
    assert '"para_caixa"' in corpo
    assert '"recebedor_id"' in corpo
    assert "a.em" in corpo and "a.utilizador" in corpo
    assert "alteracoes[i - 1]" not in corpo
    assert "slice(0, 16)" not in corpo
    inicio = js.index("async function expandirHistorico(")
    corpo = js[inicio:js.index("\n}", inicio)]
    assert "juntarAlteracoesDestino(" in corpo


def test_index_has_the_new_caixa_sections_with_their_ids():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for id_ in (
        "caixa-contigo", "esc-caixa", "pagamentos-secao", "pagamentos-lista",
        "compra-paga-com", "cfg-preco", "cfg-caixa-responsavel", "cfg-ver-alteracoes",
        "cfg-historico-lista",
    ):
        assert f'id="{id_}"' in html, f"missing #{id_}"
    assert 'id="esc-pote"' not in html, "esc-pote was replaced by esc-caixa (spec 4.8: sai pote, entra caixa)"
