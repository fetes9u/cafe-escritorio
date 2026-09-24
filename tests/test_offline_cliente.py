"""Tests for the offline queue and snapshot logic in app/static/app.js and
the cache guard in app/static/sw.js.

These only inspect the served static files, the same style as
tests/test_pwa.py: no browser, no IndexedDB runtime. Every assertion
extracts the real name or the real code shape from the source with a regex
instead of retyping it, so changing the client is what breaks a test here,
not the test going stale on its own.
"""
import re

from app.main import STATIC


def _app_js():
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _sw_js():
    return (STATIC / "sw.js").read_text(encoding="utf-8")


def _bloco_balanceado(texto, pos_abertura):
    """Returns (block, end_index) for the brace-balanced block starting at
    the '{' found at pos_abertura, so we can slice out exactly one function
    or one onclick handler body regardless of how it is formatted."""
    assert texto[pos_abertura] == "{"
    profundidade = 0
    for j in range(pos_abertura, len(texto)):
        if texto[j] == "{":
            profundidade += 1
        elif texto[j] == "}":
            profundidade -= 1
            if profundidade == 0:
                return texto[pos_abertura:j + 1], j
    raise AssertionError("unbalanced braces starting at " + str(pos_abertura))


def _funcao(js, nome):
    """Extracts the full source of `async function <nome>(...) { ... }`."""
    marcador = f"async function {nome}("
    assert marcador in js, f"could not find {marcador} in app.js"
    inicio = js.index(marcador)
    chaveta = js.index("{", inicio)
    bloco, fim = _bloco_balanceado(js, chaveta)
    return js[inicio:fim + 1]


def _localizar_try(bloco, contendo):
    """Finds the try/catch pair whose try-body contains `contendo` (a function
    body can have more than one try/catch, eg. the coffee button also queues
    to IndexedDB in a separate try before ever calling the API)."""
    for m in re.finditer(r"\btry\b", bloco):
        pos_chaveta_try = bloco.index("{", m.start())
        bloco_try, fim_try = _bloco_balanceado(bloco, pos_chaveta_try)
        if contendo in bloco_try:
            pos_catch = bloco.index("catch", fim_try)
            pos_chaveta_catch = bloco.index("{", pos_catch)
            bloco_catch, _ = _bloco_balanceado(bloco, pos_chaveta_catch)
            return bloco_try, bloco_catch
    raise AssertionError(f"could not find a try block containing {contendo!r}")


def _handler(js, seletor):
    """Extracts the body of `$("<seletor>").onclick = async () => { ... };`."""
    marcador = f'$("{seletor}").onclick'
    assert marcador in js, f"could not find {marcador} in app.js"
    inicio = js.index(marcador)
    chaveta = js.index("{", inicio)
    bloco, _ = _bloco_balanceado(js, chaveta)
    return bloco


# ---------- IndexedDB shape: cafe-offline v1, fila + instantaneos ----------

def test_abre_a_base_cafe_offline_versao_1_com_as_duas_lojas_certas():
    js = _app_js()

    m = re.search(r"indexedDB\.open\((\w+),\s*(\w+)\)", js)
    assert m, "could not find the indexedDB.open(...) call in app.js"
    nome_var, versao_var = m.group(1), m.group(2)

    nome_m = re.search(rf'{re.escape(nome_var)}\s*=\s*"([^"]+)"', js)
    versao_m = re.search(rf"{re.escape(versao_var)}\s*=\s*(\d+)", js)
    assert nome_m, f"could not resolve the constant {nome_var} used as the database name"
    assert versao_m, f"could not resolve the constant {versao_var} used as the database version"
    assert nome_m.group(1) == "cafe-offline"
    assert versao_m.group(1) == "1"

    fila_m = re.search(r'createObjectStore\(\s*"fila"\s*,\s*\{\s*keyPath:\s*"([^"]+)"', js)
    instantaneos_m = re.search(r'createObjectStore\(\s*"instantaneos"\s*,\s*\{\s*keyPath:\s*"([^"]+)"', js)
    assert fila_m, "the 'fila' object store is not created with an explicit keyPath"
    assert instantaneos_m, "the 'instantaneos' object store is not created with an explicit keyPath"
    assert fila_m.group(1) == "cliente_id"
    assert instantaneos_m.group(1) == "chave"


# ---------- the queued coffee is only deleted after a 2xx response ----------

def test_sincronizar_fila_so_apaga_a_entrada_depois_do_pedido_ter_sucesso():
    js = _app_js()
    corpo = _funcao(js, "sincronizarFila")

    # narrow down to the per-entry loop first: the function also has an outer
    # try/finally around the whole body, which is not what this checks
    pos_for = corpo.index("for (const entrada of entradas)")
    pos_chaveta_for = corpo.index("{", pos_for)
    bloco_for, _ = _bloco_balanceado(corpo, pos_chaveta_for)

    bloco_try, bloco_catch = _localizar_try(bloco_for, '"/cafe"')

    assert re.search(r'await api\([^)]*"/cafe"', bloco_try), (
        "sincronizarFila must POST to /cafe for each queued entry"
    )
    assert "idbApagar" in bloco_try, "the success path must remove the entry from the queue"
    assert bloco_try.index("api(") < bloco_try.index("idbApagar"), (
        "the queue entry must be deleted only after the request comes back, not before"
    )
    assert "idbApagar" not in bloco_catch, (
        "the failure path (network error or otherwise) must never delete the queue entry, "
        "or a half-failed flush would double count"
    )


def test_botao_de_cafe_so_apaga_a_fila_depois_do_pedido_ter_sucesso():
    js = _app_js()
    bloco = _handler(js, "btn-cafe")

    bloco_try, bloco_catch = _localizar_try(bloco, '"/cafe"')
    assert re.search(r'await api\([^)]*"/cafe"', bloco_try)
    assert "idbApagar" in bloco_try
    assert bloco_try.index("api(") < bloco_try.index("idbApagar")
    assert "idbApagar" not in bloco_catch


# ---------- the client sends exactly the contract field names ----------

def test_cliente_envia_cliente_id_e_em_no_pedido_de_sincronizacao():
    js = _app_js()
    corpo = _funcao(js, "sincronizarFila")
    m = re.search(r'await api\("POST",\s*"/cafe",\s*\{([^}]*)\}\)', corpo)
    assert m, "sincronizarFila must POST a body to /cafe"
    campos = m.group(1)
    assert "cliente_id" in campos
    assert re.search(r"\bem\b", campos)


# ---------- the snapshot is only refreshed once the queue is fully drained ----------

def test_fotografia_so_e_atualizada_depois_da_fila_ficar_vazia():
    js = _app_js()
    corpo = _funcao(js, "sincronizarFila")
    # the GET /api/eu refresh must be guarded by a check on the post-flush queue, not run unconditionally
    pos_get_eu = corpo.index('api("GET", "/eu")')
    antes = corpo[:pos_get_eu]
    assert "restantes" in antes and (".length" in antes), (
        "the refresh of /api/eu after a flush must be gated on the queue actually being empty"
    )


# ---------- undo ----------

def test_desfazer_offline_nao_enfileira_um_delete():
    js = _app_js()
    bloco = _handler(js, "btn-desfazer")
    assert "/cafe/ultimo" in bloco, "the online fallback must still exist"
    # the offline branch (queue non-empty) must return before ever reaching the network DELETE
    pos_fila_if = bloco.index("if (fila.length)")
    pos_delete = bloco.index("/cafe/ultimo")
    assert pos_fila_if < pos_delete, "the queue branch must be checked before the network delete path"
    ramo_fila, _ = _bloco_balanceado(bloco, bloco.index("{", pos_fila_if))
    assert "/cafe/ultimo" not in ramo_fila, (
        "undo while the coffee is still queued must never call DELETE /api/cafe/ultimo"
    )
    assert "idbApagar" in ramo_fila


def test_desfazer_fica_desativado_sem_fila_e_sem_rede():
    js = _app_js()
    corpo = _funcao(js, "atualizarEstadoDesfazer")
    assert "navigator.onLine" in corpo
    assert "btn.disabled = true" in corpo
    assert "razao" in corpo, "the reason for a disabled undo must be shown on screen, not just implied"


# ---------- saldo: the optimistic coffee update also moves saldo_cent ----------

def test_marcar_cafe_desconta_o_saldo_de_forma_otimista():
    js = _app_js()
    bloco = _handler(js, "btn-cafe")
    assert "eu.saldo_cent -= eu.preco_cent" in bloco, (
        "marking a coffee optimistically must also debit saldo_cent, per spec 5.1"
    )


def test_desfazer_offline_repoe_o_saldo():
    js = _app_js()
    bloco = _handler(js, "btn-desfazer")
    pos_fila_if = bloco.index("if (fila.length)")
    ramo_fila, _ = _bloco_balanceado(bloco, bloco.index("{", pos_fila_if))
    assert "eu.saldo_cent += eu.preco_cent" in ramo_fila, (
        "undoing a queued coffee must restore the saldo_cent the optimistic update debited"
    )


# ---------- pagamentos por MB WAY: never queued, always need the network ----------

def test_pagar_por_mbway_nunca_toca_na_fila_offline():
    """Paying needs network per spec 5.1: an offline payment must show the
    spec's message and never be queued like a coffee is."""
    js = _app_js()
    inicio = js.index("async function abrirFormPagar(")
    fim = js.index("\n}", inicio)
    abrir = js[inicio:fim]
    assert "navigator.onLine" in abrir
    assert "Precisas de rede para registar um pagamento." in abrir
    assert 'idbPut("fila"' not in abrir

    # _handler() only matches .onclick; the pay form uses .onsubmit, so extract it directly.
    marcador = '$("form-pagar").onsubmit'
    assert marcador in js
    pos = js.index(marcador)
    chaveta = js.index("{", pos)
    submit_bloco, _ = _bloco_balanceado(js, chaveta)
    assert 'idbPut("fila"' not in submit_bloco
    assert "erro.rede" in submit_bloco
    assert "Precisas de rede para registar um pagamento." in submit_bloco


def test_apenas_o_cafe_e_posto_na_fila_offline():
    """Spec item 6: payments, edits and settings all need the network and
    must never be queued like a coffee is. `idbPut("fila", ...)` must appear
    exactly once in the whole client, at the coffee button."""
    js = _app_js()
    assert js.count('idbPut("fila"') == 1


def test_pagamento_registado_recarrega_eu():
    js = _app_js()
    marcador = '$("form-pagar").onsubmit'
    pos = js.index(marcador)
    chaveta = js.index("{", pos)
    submit_bloco, _ = _bloco_balanceado(js, chaveta)
    assert '"/transferencias"' in submit_bloco
    assert "recarregarEu()" in submit_bloco


# ---------- snapshot guard: an old-shape snapshot is treated as absent ----------

def test_entrar_trata_fotografia_sem_saldo_cent_como_inexistente():
    js = _app_js()
    corpo = _funcao(js, "entrar")
    assert "foto.dados.saldo_cent === undefined" in corpo
    assert "!foto || foto.dados.saldo_cent === undefined" in corpo


def test_entrar_trata_fotografia_sem_caixa_como_inexistente():
    """A pre-caixa snapshot of /api/eu has no `caixa` key: it must be treated
    as absent too, or the keeper line and the payment form could draw from
    the old shape."""
    js = _app_js()
    corpo = _funcao(js, "entrar")
    assert "foto.dados.caixa === undefined" in corpo


def test_escritorio_trata_fotografia_sem_caixa_como_inexistente():
    """`pote` was removed from /api/escritorio in favour of `caixa` (spec
    4.8): a snapshot from before that change has no `caixa` key and must be
    treated as absent, not checked against the field that no longer exists."""
    js = _app_js()
    corpo = _funcao(js, "carregarEscritorio")
    assert "!foto || foto.dados.caixa === undefined" in corpo


# ---------- service worker: cache guard intact, cache version bumped ----------

def test_service_worker_continua_a_nunca_cachear_api():
    texto = _sw_js()
    assert 'pathname.startsWith("/api")' in texto


def test_versao_da_cache_avancou_do_v3():
    texto = _sw_js()
    m = re.search(r'CACHE_VERSION\s*=\s*"([^"]+)"', texto)
    assert m, "could not find CACHE_VERSION in sw.js"
    assert m.group(1) not in ("v1", "v2", "v3")


# ---------- logout wipes the offline database ----------

def test_logout_apaga_a_base_de_dados_offline():
    js = _app_js()
    bloco = _handler(js, "btn-sair")
    assert "apagarDB()" in bloco, "logout must wipe the offline database (/api/escritorio holds everyone's data)"

    corpo_apagar = _funcao(js, "apagarDB")
    m = re.search(r"indexedDB\.deleteDatabase\((\w+)\)", corpo_apagar)
    assert m, "apagarDB must actually call indexedDB.deleteDatabase"
    nome_var = m.group(1)
    nome_m = re.search(rf'{re.escape(nome_var)}\s*=\s*"([^"]+)"', js)
    assert nome_m and nome_m.group(1) == "cafe-offline", (
        "apagarDB must delete the same 'cafe-offline' database the app opens"
    )


def test_logout_nao_apaga_sem_esvaziar_a_fila_ou_confirmar():
    """The wipe on logout is the one thing that can throw away a coffee no
    one ever synced. It must only be reachable after the queue actually
    drained, or after the person was told what they are about to lose and
    said yes anyway."""
    js = _app_js()
    bloco = _handler(js, "btn-sair")

    pos_sync = bloco.index("sincronizarFila")
    pos_fila_check = bloco.index('idbTodos("fila")')
    assert pos_sync < pos_fila_check, "logout must try to flush the queue before looking at what is left in it"

    m = re.search(r"if\s*\(fila\.length\)\s*\{", bloco)
    assert m, "logout must branch on whether the queue actually drained"
    ramo_fila, fim_ramo = _bloco_balanceado(bloco, bloco.index("{", m.start()))

    assert "por sincronizar" in ramo_fila, "the message shown must name what is queued, in Portuguese"
    assert re.search(r"confirm\([^)]*\)", ramo_fila), "a queue that did not drain must be confirmed with the person"
    assert re.search(r"if\s*\(!confirm\([^)]*\)\)\s*return", ramo_fila), (
        "cancelling the confirmation must leave the session as is: no wipe, no logout"
    )

    pos_apagar = bloco.index("apagarDB()")
    assert pos_apagar >= fim_ramo, (
        "apagarDB() must sit after the whole confirm branch, never before the cancel check inside it, "
        "or the confirmation could be bypassed"
    )


def _funcao_sincrona(js, nome):
    """Same as _funcao, but for a plain `function <nome>(...) { ... }` that
    is never declared `async` (abrirDB is one)."""
    marcador = f"function {nome}("
    assert marcador in js, f"could not find {marcador} in app.js"
    inicio = js.index(marcador)
    chaveta = js.index("{", inicio)
    bloco, fim = _bloco_balanceado(js, chaveta)
    return js[inicio:fim + 1]


# ---------- the iPhone shared-device hang: a held connection blocks the wipe ----------

def test_apagar_db_fecha_a_ligacao_existente_antes_de_apagar():
    """The bug this pins: apagarDB() used to call indexedDB.deleteDatabase()
    while the IDBDatabase connection abrirDB() had already opened was still
    open. The delete then stayed pending, and the next login's
    guardarInstantaneo() call (which opens the database again) queued behind
    it and never resolved, so entrar() hung on the "Quem és?" screen with no
    JS error. Closing the held connection before deleting is what unblocks
    it."""
    js = _app_js()
    corpo = _funcao(js, "apagarDB")

    pos_close = corpo.index(".close()")
    pos_delete = corpo.index("indexedDB.deleteDatabase(")
    assert pos_close < pos_delete, (
        "apagarDB must close the connection it already holds before asking "
        "indexedDB.deleteDatabase() to run, or the delete stays blocked behind it"
    )
    m = re.search(r"await\s+\w+\(\s*dbPromise", corpo)
    assert m, "apagarDB must actually await the existing dbPromise to get the connection it holds"
    assert m.start() < pos_close, (
        "apagarDB must await the existing dbPromise before closing it, not just null the variable "
        "out and hope the connection went away on its own"
    )


def test_abrir_db_liberta_a_ligacao_em_onversionchange():
    """A held connection must also let go on its own the moment something
    else needs the database closed, whether that is another tab of the app
    or our own apagarDB() call above. Without this, a fresh connection
    opened right after a wipe attempt could go on to block the next delete
    the same way."""
    js = _app_js()
    corpo = _funcao_sincrona(js, "abrirDB")

    m = re.search(r"onversionchange\s*=\s*\(\)\s*=>\s*\{", corpo)
    assert m, "abrirDB must install db.onversionchange so other holders let go of the connection"
    bloco_handler, _ = _bloco_balanceado(corpo, corpo.index("{", m.start()))
    assert ".close()" in bloco_handler, "onversionchange must close the connection"
    assert "dbPromise = null" in bloco_handler, (
        "onversionchange must also clear dbPromise so the next abrirDB() call reopens instead of "
        "handing back a closed connection"
    )


def test_sessao_expirada_limpa_fotografias_mas_nao_a_fila():
    """An expired session is not a decision to discard work (item 1 governs
    that), but /api/escritorio's snapshot is someone else's data and has no
    reason to keep sitting on a shared device after their session ended."""
    js = _app_js()
    m = re.search(r'if\s*\(r\.status === 401[^)]*\)\s*\{', js)
    assert m, "could not find the 401 branch in api()"
    bloco_401, _ = _bloco_balanceado(js, js.index("{", m.start()))

    assert re.search(r'idbLimpar\(\s*"instantaneos"\s*\)', bloco_401), (
        "an expired session must clear the instantaneos snapshots"
    )
    assert '"fila"' not in bloco_401 and "'fila'" not in bloco_401, (
        "an expired session must never touch the coffee queue: that is not a discard decision"
    )
