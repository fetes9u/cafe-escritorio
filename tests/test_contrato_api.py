"""Contract tests between the client (app/static/app.js) and the server
(app/main.py).

Two real bugs slipped past the existing suite because the backend lane
tested only what the server claims to return and the frontend lane tested
only the static files served, never the join between them: a field name
that matched on one side and not the other, and a payload shape the server
never actually parsed. Every test here extracts what the client actually
does from app.js/index.html with a regex instead of retyping a field name,
event name or route by hand, so changing either side without changing the
other is what makes the test fail, not a hand-maintained expectation.
"""
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

from app import main as app_main
from app.main import EVENTOS, STATIC, app
from tests.conftest import regista


# ---------- GET /api/push/chave: the field the client reads must exist ----------

def _campo_lido_pela_chave_vapid() -> str:
    """The name of the field app.js reads off the JSON response of
    GET /api/push/chave (`chaveVapid = dados.<campo>`), extracted from the
    source instead of hardcoded, so renaming the field on either side (client
    or server) is what breaks this test, not the test itself going stale."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    m = re.search(r"chaveVapid\s*=\s*dados\.(\w+)", js)
    assert m, "could not find `chaveVapid = dados.<campo>` in app.js; did the client change?"
    return m.group(1)


def test_o_campo_que_o_cliente_le_da_chave_existe_na_resposta_real(cliente, monkeypatch):
    monkeypatch.setenv("CAFE_VAPID_PUBLIC", "chave-publica-de-teste")
    campo = _campo_lido_pela_chave_vapid()

    resposta = cliente.get("/api/push/chave").json()

    assert campo in resposta, (
        f"app.js reads `dados.{campo}` from GET /api/push/chave, but the "
        f"server's response has no such field: {sorted(resposta)}"
    )
    assert resposta[campo] == "chave-publica-de-teste"


# ---------- PUT /api/notificacoes/preferencias: the exact shape the client sends ----------

def _eventos_do_formulario() -> list[str]:
    """The event names the notification-settings form in index.html carries
    (`data-evento="..."`), the same set app.js's change handler iterates over
    to build the {evento: booleano} map it PUTs."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    eventos = re.findall(r'data-evento="(\w+)"', html)
    assert eventos, "could not find any data-evento attribute in index.html"
    return eventos


def _verifica_payload_do_cliente_para_notificacoes() -> str:
    """Verify that app.js sends a map of eventos (keyed by chk.dataset.evento, valued by
    chk.checked) to PUT /notificacoes/preferencias, NOT a {"desligados": [...]} list.

    Extracts the payload structure directly from the form change handler in app.js.
    Returns the field name used for the event key (expected to be "evento"), or raises
    AssertionError if the structure doesn't match, with a message explaining the
    client/server shape mismatch."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    # Must find: prefs[chk.dataset.<evento>] = chk.checked
    # This proves client builds {evento: boolean}, not {desligados: [...]}.
    m = re.search(r'prefs\[chk\.dataset\.(\w+)\]\s*=\s*chk\.(checked)', js)
    assert m, (
        "Client payload structure mismatch: app.js form change handler does not build "
        "`prefs[chk.dataset.<field>] = chk.checked`. Is the client sending "
        "`{desligados: [...]}` or some other structure instead of an event map?"
    )

    event_field = m.group(1)
    value_field = m.group(2)
    assert value_field == "checked", (
        f"Expected checkbox value from `.checked`, but client uses `.{value_field}`"
    )

    return event_field


def test_o_mapa_que_o_cliente_monta_fica_mesmo_gravado(cliente):
    """Sends exactly what app.js's form change handler assembles: a map of
    every event in the form to its checked state, never a {"desligados": [...]}
    list. A 200 alone does not prove anything was written, so this reads the
    preference back through GET afterwards."""

    # Verify app.js builds the payload as a map, not a list
    event_field = _verifica_payload_do_cliente_para_notificacoes()
    assert event_field == "evento", (
        f"Client and server disagree on payload SHAPE: app.js uses field `{event_field}` "
        f"but server expects `evento`. The payload is a map keyed by the event name; "
        f"this mismatch means the client's change will be silently dropped."
    )

    a = regista(cliente, "Ana")
    eventos = _eventos_do_formulario()

    desligados = set(eventos[::2])  # turn off every other toggle, to cover both states
    mapa = {evento: evento not in desligados for evento in eventos}

    r = a.put("/api/notificacoes/preferencias", json=mapa)
    assert r.status_code == 200

    persistido = a.get("/api/notificacoes/preferencias").json()
    for evento in eventos:
        assert persistido[evento] == mapa[evento], (
            f"evento={evento}: PUT enviou {mapa[evento]}, GET leu de volta {persistido[evento]}"
        )


# ---------- every /api/... route the client calls must exist in the app ----------

def _rotas_chamadas_no_app_js() -> set[tuple[str, str]]:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    rotas: set[tuple[str, str]] = set()
    for metodo, caminho in re.findall(r'api\("([A-Z]+)",\s*"([^"]*)"', js):
        rotas.add((metodo, "/api" + caminho))
    for caminho in re.findall(r'fetch\("(/api/[^"]+)"', js):
        rotas.add(("GET", caminho))
    assert rotas, "could not find any api()/fetch() call in app.js"
    return rotas


def _rotas_da_app() -> set[tuple[str, str]]:
    rotas: set[tuple[str, str]] = set()
    for route in app.routes:
        metodos = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not metodos or not path:
            continue
        for metodo in metodos:
            if metodo == "HEAD":
                continue
            rotas.add((metodo, path))
    return rotas


def test_todas_as_rotas_chamadas_pelo_app_js_existem_na_app():
    chamadas = _rotas_chamadas_no_app_js()
    existentes = _rotas_da_app()
    em_falta = chamadas - existentes
    assert not em_falta, f"app.js calls routes that do not exist in the app: {sorted(em_falta)}"


# ---------- push payload: every field sw.js reads must be present, and non-empty ----------

def _campos_lidos_pelo_push_handler() -> set[str]:
    """The fields the `push` listener in sw.js reads off the pushed JSON
    (`dados.<campo>`), extracted from the source instead of hardcoded, so a
    field renamed or removed on either side is what breaks this test."""
    js = (STATIC / "sw.js").read_text(encoding="utf-8")
    m = re.search(r'self\.addEventListener\("push".*?\n\}\);', js, re.S)
    assert m, "could not find the `push` event listener in sw.js; did it change?"
    campos = set(re.findall(r"dados\.(\w+)", m.group(0)))
    assert campos, "could not find any `dados.<campo>` read in the push handler in sw.js"
    return campos


def test_a_notificacao_enviada_tem_todos_os_campos_que_o_sw_le_para_todos_os_eventos(cliente, monkeypatch):
    """For every evento the server can send, the payload actually put on the
    wire must carry every field sw.js's push handler reads, and titulo/corpo
    must not just be present but non-empty: a payload with the right keys and
    an empty value is the same bug (empty notification) wearing a different
    hat."""
    monkeypatch.setenv("CAFE_VAPID_PRIVATE", "chave-privada-de-teste")
    monkeypatch.setenv("CAFE_VAPID_PUBLIC", "chave-publica-de-teste")
    monkeypatch.setenv("CAFE_VAPID_CONTACTO", "mailto:teste@exemplo.pt")
    monkeypatch.setattr(app_main, "_vapid_instance", lambda: object())
    mock = MagicMock()
    monkeypatch.setattr(app_main, "webpush", mock)

    destinatario = regista(cliente, "Ana")
    destinatario.post(
        "/api/push/subscricoes",
        json={"endpoint": "https://push.example/ana", "p256dh": "p", "auth": "a"},
    )
    autor_id = 0  # never matches the recipient's id, so Ana always gets notified

    campos_lidos = _campos_lidos_pelo_push_handler()

    for evento in EVENTOS:
        mock.reset_mock()
        app_main._enviar_notificacao(evento, f"mensagem de teste para {evento}", autor_id)

        mock.assert_called_once()
        payload = json.loads(mock.call_args.kwargs["data"])

        em_falta = campos_lidos - set(payload)
        assert not em_falta, (
            f"evento={evento}: sw.js reads {sorted(campos_lidos)} from the pushed JSON, "
            f"but the server's payload is missing {sorted(em_falta)}: {payload}"
        )
        assert isinstance(payload.get("titulo"), str) and payload["titulo"], (
            f"evento={evento}: `titulo` must be a non-empty string, got {payload.get('titulo')!r}"
        )
        assert isinstance(payload.get("corpo"), str) and payload["corpo"], (
            f"evento={evento}: `corpo` must be a non-empty string, got {payload.get('corpo')!r}"
        )


# ---------- POST /api/cafe: the offline queue seam ----------
#
# The offline feature (see docs/superpowers/specs/2026-09-11-offline-e-sincronizacao-design.md)
# introduced a new client/server seam: the coffee button and the queue flush
# both build a request body by hand and the server reads it back through the
# NovoCafe model. Everything below is extracted from app.js/main.py with a
# regex, never retyped, so a rename on either side is what breaks these
# tests, not a hand-maintained expectation.

def _texto_main() -> str:
    """Full source of app/main.py, read directly so the server-side field
    names checked below come from the file the server actually runs,
    never retyped by hand."""
    return Path(app_main.__file__).read_text(encoding="utf-8")


def _campos_de_objeto_js(literal: str) -> set[str]:
    """Given the text between `{` and `}` of a JS object literal, return the
    set of field names it assigns, whether written as `campo: valor` or as
    shorthand `campo`."""
    campos = set()
    for parte in literal.split(","):
        parte = parte.strip()
        if not parte:
            continue
        campos.add(parte.split(":")[0].strip())
    return campos


def _corpos_enviados_para_post_cafe() -> list[set[str]]:
    """The field names of the object each of app.js's two POST /api/cafe call
    sites sends: the coffee button (`btn-cafe`'s onclick) and the queue flush
    (`sincronizarFila`). Both are extracted from the source, never retyped,
    so a divergence between the two sites, itself the bug the spec calls out
    in section 8, or a rename on either side is what breaks this, not a
    hand-maintained list."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    chamadas = re.findall(r'api\("POST",\s*"/cafe",\s*\{([^}]*)\}\)', js)
    assert len(chamadas) == 2, (
        f"expected exactly 2 call sites for POST /api/cafe in app.js (button click and "
        f"queue flush), found {len(chamadas)}; did a call site get added or removed? "
        f"update this test if that change was intentional"
    )
    return [_campos_de_objeto_js(corpo) for corpo in chamadas]


def _campos_aceites_por_novo_cafe() -> set[str]:
    """The field names the server's NovoCafe pydantic model declares, i.e.
    what POST /api/cafe actually accepts, extracted from app/main.py's
    source instead of retyped."""
    main_src = _texto_main()
    # Capture only the class body: consecutive indented (or blank) lines
    # right after the class header, stopping at the first line back at
    # column 0. A lazy match up to "\nclass " is not enough, since it also
    # swallows every module-level function between NovoCafe and the next
    # class, including unrelated `try:` blocks indented 4 spaces inside them.
    m = re.search(r'class NovoCafe\(BaseModel\):\n((?:[ \t]+.*\n|\n)+)', main_src)
    assert m, "could not find `class NovoCafe(BaseModel):` in app/main.py; did the request model move or get renamed?"
    campos = set(re.findall(r'^ {4}(\w+):', m.group(1), re.M))
    assert campos, "found NovoCafe but no field declarations inside it"
    return campos


def test_os_dois_pontos_de_chamada_do_post_cafe_concordam_entre_si():
    """The button click and the queue flush must send the exact same field
    names, since a divergence between the two client sites is itself the bug
    this file exists to catch (spec section 8)."""
    sites = _corpos_enviados_para_post_cafe()
    assert sites[0] == sites[1], (
        f"app.js's two POST /api/cafe call sites disagree on which fields they send: "
        f"button click sends {sorted(sites[0])}, queue flush sends {sorted(sites[1])}"
    )


def test_o_servidor_aceita_o_corpo_que_o_cliente_envia_no_post_cafe_e_grava_o_cafe(cliente):
    """Sends exactly what app.js's own call sites build (extracted, not
    retyped) to the real POST /api/cafe endpoint via TestClient, and checks
    the coffee was actually recorded, not just that the request was accepted."""
    campos_cliente = _corpos_enviados_para_post_cafe()[0]
    campos_servidor = _campos_aceites_por_novo_cafe()

    em_falta = campos_cliente - campos_servidor
    assert not em_falta, (
        f"app.js sends {sorted(campos_cliente)} on POST /api/cafe, but the server's "
        f"NovoCafe model does not accept {sorted(em_falta)} (it accepts {sorted(campos_servidor)})"
    )

    a = regista(cliente, "Rita")
    antes = a.get("/api/eu").json()

    valores = {"cliente_id": "11111111-1111-1111-1111-111111111111", "em": "2026-09-11T09:00:00+00:00"}
    corpo = {campo: valores[campo] for campo in campos_cliente}

    r = a.post("/api/cafe", json=corpo)
    assert r.status_code in (200, 201), r.text

    depois = a.get("/api/eu").json()
    assert depois["cafes"] == antes["cafes"] + 1, (
        f"POST /api/cafe with the client's own body {corpo} was accepted but did not "
        f"record a coffee: before={antes['cafes']}, after={depois['cafes']}"
    )


def test_o_cliente_nao_le_nenhum_campo_da_resposta_do_post_cafe():
    """Both POST /api/cafe call sites in app.js (the button click and the
    queue flush) await the call without ever assigning its result to a
    variable: the coffee count is updated optimistically before the call is
    even made, not from the response body. There is nothing to assert a
    field of here, so this asserts the emptiness explicitly instead of
    silently checking nothing: if either call site starts capturing the
    response (`const r = await api(...)`, a destructuring assignment, or
    `.then(...)`), this regex catches it and this test must be extended to
    assert on the field(s) then read (per spec section 5.1: `ok`,
    `cliente_id`, `em`, `duplicado`)."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    chamadas_com_atribuicao = re.findall(r'=\s*await\s+api\("POST",\s*"/cafe"', js)
    assert not chamadas_com_atribuicao, (
        f"app.js now assigns the response of POST /api/cafe to something "
        f"({chamadas_com_atribuicao!r}); this test file must be updated to assert on "
        f"the field(s) the client reads from it, per spec section 5.1"
    )


# ---------- IndexedDB `fila`: the offline queue record shape ----------

def _keypath_da_fila() -> str:
    """The keyPath app.js declares for the `fila` object store, extracted
    from the `createObjectStore` call in `abrirDB`."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    m = re.search(r'createObjectStore\("fila",\s*\{\s*keyPath:\s*"(\w+)"', js)
    assert m, "could not find the `fila` object store's keyPath declaration in app.js; did abrirDB change?"
    return m.group(1)


def _campos_do_registo_da_fila() -> set[str]:
    """The field names of the record app.js actually writes to the `fila`
    store (`idbPut("fila", {...})` in the coffee button handler)."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    m = re.search(r'idbPut\("fila",\s*\{([^}]*)\}\)', js)
    assert m, 'could not find the idbPut("fila", {...}) call in app.js that builds a queue record'
    return _campos_de_objeto_js(m.group(1))


def test_o_registo_da_fila_bate_certo_com_a_spec_e_com_o_campo_que_o_servidor_le():
    """Spec section 5.2 fixes the `fila` record shape as
    {cliente_id, tipo, em, criado_em} keyed by cliente_id. This checks
    app.js's actual keyPath and record fields against those names (the spec
    names are hardcoded here because they are normative, not extracted; the
    client's own source is what is extracted), and separately that the
    keyPath the client stores under is the same field name the server reads
    off the request body, since idempotency depends on both sides meaning
    the same thing by "cliente_id"."""
    keypath = _keypath_da_fila()
    campos = _campos_do_registo_da_fila()

    esperado = {"cliente_id", "tipo", "em", "criado_em"}  # spec secção 5.2, normativo
    assert campos == esperado, (
        f"app.js's `fila` queue record has fields {sorted(campos)}, but spec section 5.2 "
        f"fixes it as {sorted(esperado)}"
    )
    assert keypath == "cliente_id", (
        f"app.js's `fila` object store has keyPath {keypath!r}, but spec section 5.2 fixes "
        f"it as 'cliente_id'"
    )

    campos_servidor = _campos_aceites_por_novo_cafe()
    assert keypath in campos_servidor, (
        f"the `fila` store's keyPath is {keypath!r}, but the server's NovoCafe model reads "
        f"no field of that name (it accepts {sorted(campos_servidor)}); a queued record can "
        f"never be matched for idempotency if the two sides disagree on this name"
    )


# ---------- idempotency across the real seam ----------

def test_o_mesmo_cliente_id_duas_vezes_da_um_cafe_so_e_a_segunda_diz_duplicado(cliente):
    """End to end proof that the client's retry is safe: builds the body
    from the field names app.js's own call site sends (not hardcoded),
    POSTs it twice, and checks exactly one coffee exists and the second
    response reports `duplicado`."""
    campos_cliente = _corpos_enviados_para_post_cafe()[0]
    a = regista(cliente, "Duplicado")

    valores = {"cliente_id": "22222222-2222-2222-2222-222222222222", "em": "2026-09-11T09:00:00+00:00"}
    corpo = {campo: valores[campo] for campo in campos_cliente}

    r1 = a.post("/api/cafe", json=corpo)
    assert r1.status_code == 201, r1.text
    assert r1.json()["duplicado"] is False, r1.json()

    r2 = a.post("/api/cafe", json=corpo)
    assert r2.status_code == 200, r2.text
    assert r2.json()["duplicado"] is True, (
        f"the same cliente_id sent twice must report duplicado=true on the second "
        f"response, got {r2.json()}"
    )

    eu = a.get("/api/eu").json()
    assert eu["cafes"] == 1, (
        f"the same cliente_id sent twice recorded {eu['cafes']} coffees instead of exactly one"
    )


# ---------- caixa, preço fixo e histórico de alterações: the join between the two halves ----------
#
# See docs/superpowers/specs/2026-09-23-caixa-e-historico-design.md section 4
# for the field-level API contract (it supersedes
# 2026-09-23-saldos-e-pagamentos-mbway-design.md where the two diverge, per
# its own section 1). The server lane and the client lane implemented their
# halves in parallel worktrees against that contract; nothing below retypes a
# field name, a route or a literal by hand, so changing either side without
# changing the other is what breaks these tests, exactly like every other
# test in this file.

def _texto_app_js() -> str:
    """Full source of app/static/app.js, read directly so every field name
    checked below comes from the file the browser actually runs, never
    retyped by hand."""
    return (STATIC / "app.js").read_text(encoding="utf-8")


# ---- GET /api/eu: saldo_cent, caixa, sugestao and por_confirmar (spec 4.1) ----

def _campo_saldo_de_eu() -> str:
    m = re.search(r"fraseSaldo\(eu\.(\w+)\)", _texto_app_js())
    assert m, "could not find `fraseSaldo(eu.<campo>)` in app.js; did desenharEu change?"
    return m.group(1)


def _campo_sugestao_de_eu() -> str:
    m = re.search(r"if \(eu\.(\w+)\) \{", _texto_app_js())
    assert m, "could not find the `if (eu.<campo>)` guard around #saldo-sugestao in app.js"
    return m.group(1)


def _campo_por_confirmar_de_eu() -> str:
    m = re.search(r"const pc = eu\.(\w+) \|\| \[\];", _texto_app_js())
    assert m, "could not find `const pc = eu.<campo> || [];` in app.js; did desenharEu change?"
    return m.group(1)


def _subcampos_da_sugestao() -> set[str]:
    campos = set(re.findall(r"eu\.sugestao\.(\w+)", _texto_app_js()))
    assert campos, "could not find any `eu.sugestao.<campo>` read in app.js"
    return campos


def _campos_de_caixa_em_eu() -> set[str]:
    """The field names app.js reads off `eu.caixa` (the keeper line on the
    Café card and the payment form's default destination), extracted instead
    of retyped, per spec 4.1."""
    campos = set(re.findall(r"eu\.caixa\.(\w+)", _texto_app_js()))
    assert campos, "could not find any `eu.caixa.<campo>` read in app.js"
    return campos


def _subcampos_do_item_por_confirmar() -> set[str]:
    m = re.search(r"for \(const t of pc\) \{(.*?)\n    \}", _texto_app_js(), re.S)
    assert m, "could not find the `for (const t of pc)` loop in desenharEu in app.js"
    campos = set(re.findall(r"(?<!\w)t\.(\w+)", m.group(1)))
    assert campos, "could not find any `t.<campo>` read inside the por_confirmar loop"
    return campos


def test_os_campos_de_saldo_caixa_sugestao_e_por_confirmar_de_eu_batem_com_a_resposta_real(cliente):
    """Builds a real state with a keeper (Ana, set through PUT /api/config so
    caixa and the sugestao path are both live), a debtor with no offsetting
    payment (Bruno: sugestao must not be null) and an unconfirmed payment to
    the caixa (Carla pays the caixa: Ana's, the keeper's, por_confirmar must
    not be empty), so none of the checks below is vacuous."""
    ana = regista(cliente, "Ana")
    bruno = regista(cliente, "Bruno")
    carla = regista(cliente, "Carla")
    ana_id = ana.get("/api/eu").json()["utilizador"]["id"]

    r = ana.put("/api/config", json={"caixa_responsavel_id": ana_id})
    assert r.status_code == 200, r.text

    r = bruno.post("/api/cafe", json={})
    assert r.status_code == 201, r.text
    r = carla.post("/api/transferencias", json={"recebedor_id": None, "para_caixa": True, "valor_cent": 400})
    assert r.status_code == 201, r.text

    resp_bruno = bruno.get("/api/eu").json()
    resp_ana = ana.get("/api/eu").json()

    campo_saldo = _campo_saldo_de_eu()
    assert campo_saldo in resp_bruno, (
        f"app.js reads `eu.{campo_saldo}`, but GET /api/eu has no such field: {sorted(resp_bruno)}"
    )
    assert resp_bruno[campo_saldo] == -25, (
        f"Bruno drank a 25 cent coffee and made no payment, so his saldo_cent should be "
        f"-25, got {resp_bruno[campo_saldo]}"
    )

    for campo in _campos_de_caixa_em_eu():
        assert campo in resp_bruno["caixa"], (
            f"app.js reads `eu.caixa.{campo}`, but the response's caixa has no such field: "
            f"{sorted(resp_bruno['caixa'])}"
        )
    assert resp_bruno["caixa"]["responsavel_id"] == ana_id
    assert resp_bruno["caixa"]["responsavel"] == "Ana"
    assert resp_bruno["caixa"]["dinheiro_cent"] == 400, (
        "Carla's 400 cent payment to the caixa counts even unconfirmed (spec 3.1: only "
        f"anulada_em matters), so caixa.dinheiro_cent should be 400, got "
        f"{resp_bruno['caixa']['dinheiro_cent']}"
    )

    campo_sugestao = _campo_sugestao_de_eu()
    assert campo_sugestao in resp_bruno, (
        f"app.js reads `eu.{campo_sugestao}`, but GET /api/eu has no such field: {sorted(resp_bruno)}"
    )
    sugestao = resp_bruno[campo_sugestao]
    assert sugestao is not None, (
        "Bruno owes money and a keeper is set, so sugestao must not be null here; "
        "otherwise the check below is vacuous"
    )
    for subcampo in _subcampos_da_sugestao():
        assert subcampo in sugestao, (
            f"app.js reads `eu.sugestao.{subcampo}`, but the response's sugestao has no "
            f"such field: {sorted(sugestao)}"
        )
    assert sugestao["valor_cent"] == 25

    campo_pc = _campo_por_confirmar_de_eu()
    assert campo_pc in resp_ana, (
        f"app.js reads `eu.{campo_pc}`, but GET /api/eu has no such field: {sorted(resp_ana)}"
    )
    por_confirmar = resp_ana[campo_pc]
    assert por_confirmar, (
        "Carla just paid the caixa and Ana guards it, so her por_confirmar must not be "
        "empty here; otherwise the check below is vacuous"
    )
    item = por_confirmar[0]
    for subcampo in _subcampos_do_item_por_confirmar():
        assert subcampo in item, (
            f"app.js reads `t.{subcampo}` off a por_confirmar item, but the response's "
            f"item has no such field: {sorted(item)}"
        )
    assert item["pagador"] == "Carla"
    assert item["valor_cent"] == 400
    assert item["para_caixa"] is True



# ---- GET /api/movimentos: transferencias, compras and meses items (spec 4.5) ----

def _corpo_desenhar_dinheiro() -> str:
    m = re.search(r"function desenharDinheiro\(\) \{(.*?)\n\}\n", _texto_app_js(), re.S)
    assert m, "could not find `function desenharDinheiro() {...}` in app.js; did the Dinheiro tab change?"
    return m.group(1)


def _campo_saldo_de_movimentos() -> str:
    m = re.search(r"fraseSaldo\(d\.(\w+)\)", _corpo_desenhar_dinheiro())
    assert m, "could not find `fraseSaldo(d.<campo>)` in desenharDinheiro"
    return m.group(1)


def _campos_de_transferencia_em_movimentos() -> set[str]:
    campos = set(re.findall(r"(?<!\w)t\.(\w+)", _corpo_desenhar_dinheiro()))
    assert campos, "could not find any `t.<campo>` read off a transferencia in desenharDinheiro"
    return campos


def _campos_de_compra_em_movimentos() -> set[str]:
    campos = set(re.findall(r"it\.dado\.(\w+)", _corpo_desenhar_dinheiro()))
    assert campos, "could not find any `it.dado.<campo>` read off a compra in desenharDinheiro"
    return campos


def _campos_de_mes_em_movimentos() -> set[str]:
    campos = set(re.findall(r"(?<!\w)m\.(\w+)", _corpo_desenhar_dinheiro()))
    assert campos, "could not find any `m.<campo>` read off a mes in desenharDinheiro"
    return campos


def _literais_de_sentido_comparados_pelo_cliente() -> set[str]:
    campos = set(re.findall(r't\.sentido === "(\w+)"', _texto_app_js()))
    assert campos, 'could not find any `t.sentido === "..."` comparison in app.js'
    return campos


def test_os_campos_de_movimentos_batem_com_a_resposta_real(cliente):
    """Non-empty transferencias, compras and meses arrays for three different
    people in one scenario: Ana bought capsules (her compras), Bruno drank a
    coffee this month (his meses) and Carla paid Bruno (a transferencia seen
    by both, one on each side of `sentido`)."""
    ana = regista(cliente, "Ana")
    bruno = regista(cliente, "Bruno")
    carla = regista(cliente, "Carla")
    bruno_id = bruno.get("/api/eu").json()["utilizador"]["id"]

    r = ana.post("/api/compras", json={"capsulas": 100, "custo_cent": 2500, "nota": None})
    assert r.status_code == 201, r.text
    r = bruno.post("/api/cafe", json={})
    assert r.status_code == 201, r.text
    r = carla.post("/api/transferencias", json={"recebedor_id": bruno_id, "valor_cent": 500})
    assert r.status_code == 201, r.text

    dados_carla = carla.get("/api/movimentos").json()
    dados_bruno = bruno.get("/api/movimentos").json()
    dados_ana = ana.get("/api/movimentos").json()

    campo_saldo = _campo_saldo_de_movimentos()
    assert campo_saldo in dados_carla, (
        f"app.js reads `d.{campo_saldo}`, but GET /api/movimentos has no such field: {sorted(dados_carla)}"
    )
    assert dados_carla[campo_saldo] == 500

    assert dados_carla["transferencias"], "Carla just paid Bruno, transferencias must not be empty"
    assert dados_ana["compras"], "Ana just bought capsules, compras must not be empty"
    assert dados_bruno["meses"], "Bruno drank a coffee this month, meses must not be empty"

    item_carla = dados_carla["transferencias"][0]
    item_bruno = dados_bruno["transferencias"][0]
    for campo in _campos_de_transferencia_em_movimentos():
        assert campo in item_carla, (
            f"app.js reads `t.{campo}` off a transferencia in Dinheiro, but the response "
            f"has no such field: {sorted(item_carla)}"
        )

    literais = _literais_de_sentido_comparados_pelo_cliente()
    assert "paguei" in literais, f'app.js should literal-compare t.sentido against "paguei", found {literais}'
    assert item_carla["sentido"] in literais, (
        f"Carla paid, so her transferencia's sentido must be a value app.js actually "
        f"compares against ({literais}), got {item_carla['sentido']!r}"
    )
    # "recebi" is spec-normative (section 4.5: sentido é "paguei" ou "recebi"), never
    # literal-compared by app.js (it is the implicit else branch), so it is hardcoded here
    # instead of extracted, same as the fila record shape earlier in this file.
    assert item_bruno["sentido"] == "recebi", (
        f"Bruno received, so his transferencia's sentido must be 'recebi' per spec 4.5, "
        f"got {item_bruno['sentido']!r}"
    )
    assert item_carla["outro"] == "Bruno"
    assert item_bruno["outro"] == "Carla"
    assert item_carla["confirmada_em"] is None
    assert item_carla["anulada_em"] is None
    assert item_carla["anulada_por"] is None

    item_compra = dados_ana["compras"][0]
    for campo in _campos_de_compra_em_movimentos():
        assert campo in item_compra, (
            f"app.js reads `it.dado.{campo}` off a compra in Dinheiro, but the response "
            f"has no such field: {sorted(item_compra)}"
        )
    assert item_compra["capsulas"] == 100
    assert item_compra["custo_cent"] == 2500

    item_mes = dados_bruno["meses"][0]
    for campo in _campos_de_mes_em_movimentos():
        assert campo in item_mes, (
            f"app.js reads `m.{campo}` off a mes in Dinheiro, but the response has no "
            f"such field: {sorted(item_mes)}"
        )
    assert item_mes["cafes"] == 1
    assert item_mes["valor_cent"] == 25


# ---- GET /api/escritorio: caixa, pessoas and compras (spec 4.8) ----

def _corpo_desenhar_escritorio() -> str:
    m = re.search(r"function desenharEscritorio\(\) \{(.*?)\n\}\n", _texto_app_js(), re.S)
    assert m, "could not find `function desenharEscritorio() {...}` in app.js; did the Escritório tab change?"
    return m.group(1)


def _campos_de_caixa_no_escritorio() -> set[str]:
    campos = set(re.findall(r"e\.caixa\.(\w+)", _corpo_desenhar_escritorio()))
    assert campos, "could not find any `e.caixa.<campo>` read in desenharEscritorio"
    return campos


def _campos_de_pessoa_no_escritorio() -> set[str]:
    campos = set(re.findall(r"(?<!\w)p\.(\w+)", _corpo_desenhar_escritorio()))
    assert campos, "could not find any `p.<campo>` read off a pessoa in desenharEscritorio"
    return campos


def _campos_de_compra_no_escritorio() -> set[str]:
    campos = set(re.findall(r"(?<!\w)c\.(\w+)", _corpo_desenhar_escritorio()))
    assert campos, "could not find any `c.<campo>` read off a compra in desenharEscritorio"
    return campos


def test_os_campos_de_escritorio_batem_com_a_resposta_real(cliente):
    """Spec 4.8: pote is gone, replaced by caixa. Ana guards the caixa and
    buys capsules paid by it (so caixa.dinheiro_cent moves), Bruno drinks a
    coffee unpaid (so caixa.por_receber_cent is non-zero)."""
    ana = regista(cliente, "Ana")
    bruno = regista(cliente, "Bruno")
    ana_id = ana.get("/api/eu").json()["utilizador"]["id"]

    r = ana.put("/api/config", json={"caixa_responsavel_id": ana_id})
    assert r.status_code == 200, r.text
    r = ana.post("/api/compras", json={"capsulas": 100, "custo_cent": 2500, "paga_pela_caixa": True, "nota": None})
    assert r.status_code == 201, r.text
    r = bruno.post("/api/cafe", json={})
    assert r.status_code == 201, r.text

    esc = ana.get("/api/escritorio").json()
    assert "pote" not in esc, "spec 4.8: pote sai, entra caixa"

    for campo in _campos_de_caixa_no_escritorio():
        assert campo in esc["caixa"], (
            f"app.js reads `e.caixa.{campo}`, but GET /api/escritorio's caixa has no such "
            f"field: {sorted(esc['caixa'])}"
        )
    assert esc["caixa"]["responsavel_id"] == ana_id
    assert esc["caixa"]["responsavel"] == "Ana"
    assert esc["caixa"]["dinheiro_cent"] == -2500, (
        "the caixa paid 2500 cents for the capsules and nothing has been recovered yet, so "
        f"caixa.dinheiro_cent should be -2500, got {esc['caixa']['dinheiro_cent']}"
    )
    assert esc["caixa"]["por_receber_cent"] == 25, (
        f"Bruno's 25 cent coffee is unpaid, so caixa.por_receber_cent should be 25, got "
        f"{esc['caixa']['por_receber_cent']}"
    )
    assert esc["caixa"]["fundo_cent"] == 25 - 2500

    pessoa_ana = next(p for p in esc["pessoas"] if p["nome"] == "Ana")
    for campo in _campos_de_pessoa_no_escritorio():
        assert campo in pessoa_ana, (
            f"app.js reads `p.{campo}` off a pessoa in Escritório, but the response's "
            f"pessoa has no such field: {sorted(pessoa_ana)}"
        )
    assert pessoa_ana["saldo_cent"] == 0  # she bought capsules through the caixa, not her own pocket

    compra = esc["compras"][0]
    for campo in _campos_de_compra_no_escritorio():
        assert campo in compra, (
            f"app.js reads `c.{campo}` off a compra in Escritório, but the response's "
            f"compra has no such field: {sorted(compra)}"
        )
    assert compra["custo_cent"] == 2500
    assert compra["paga_pela_caixa"] is True
    assert compra["custo_estimado"] is False
    assert compra["pode_editar"] is True
    assert compra["editada"] is False


# ---- exact request shapes: transferencias, compras and config (spec 4.2, 4.3, 4.9, 4.10) ----

def _corpos_enviados_para_post_transferencias() -> dict[str, set[str]]:
    """The three literal body shapes app.js's own POST /api/transferencias
    call sites build: paying the caixa and paying a person (both in the
    payment form's submit handler) and a de_caixa reimbursement (the
    Reembolsar inline form). Classified by each call site's own literal
    para_caixa/de_caixa value, never retyped, so a renamed field or a
    reordered/merged call site on either side is what breaks this, not a
    hand-maintained expectation."""
    js = _texto_app_js()
    corpos = re.findall(r'api\("POST",\s*"/transferencias",\s*\{([^}]*)\}\)', js)
    assert len(corpos) == 3, (
        f"expected exactly 3 call sites for POST /api/transferencias in app.js (pay the "
        f"caixa, pay a person, de_caixa reimbursement), found {len(corpos)}; did a call "
        f"site get added, removed or merged? update this test if that change was intentional"
    )
    por_tipo: dict[str, set[str]] = {}
    for corpo in corpos:
        if "de_caixa: true" in corpo:
            chave = "de_caixa"
        elif "para_caixa: true" in corpo:
            chave = "para_caixa"
        elif "para_caixa: false" in corpo:
            chave = "pessoa"
        else:
            raise AssertionError(f"could not classify a POST /api/transferencias call site: {corpo!r}")
        assert chave not in por_tipo, f"two call sites both classify as {chave!r}: {corpo!r}"
        por_tipo[chave] = _campos_de_objeto_js(corpo)
    assert set(por_tipo) == {"de_caixa", "para_caixa", "pessoa"}, (
        f"expected the three POST /api/transferencias call sites to cover pagar à caixa, "
        f"pagar a uma pessoa and o reembolso de_caixa, got {sorted(por_tipo)}"
    )
    return por_tipo


def _corpo_enviado_para_post_compras() -> set[str]:
    m = re.search(r'api\("POST",\s*"/compras",\s*\{([^}]*)\}\)', _texto_app_js())
    assert m, 'could not find the `api("POST", "/compras", {...})` call in app.js'
    return _campos_de_objeto_js(m.group(1))


def _corpo_enviado_para_put_config() -> set[str]:
    m = re.search(r'api\("PUT",\s*"/config",\s*\{([^}]*)\}\)', _texto_app_js())
    assert m, 'could not find the `api("PUT", "/config", {...})` call in app.js'
    return _campos_de_objeto_js(m.group(1))


def _campos_enviados_no_patch_transferencias() -> set[str]:
    """PATCH /api/transferencias/{id}'s body is never a single literal object
    in app.js: `guardarEdicaoPagamento` builds it conditionally, field by
    field, depending on what actually changed (spec 4.3: "qualquer
    subconjunto"). This extracts the field names from its own
    `corpo.<campo> = ...` assignments instead of retyping them."""
    js = _texto_app_js()
    m = re.search(r"async function guardarEdicaoPagamento\(.*?\n\}\n", js, re.S)
    assert m, "could not find `async function guardarEdicaoPagamento(...) {...}` in app.js"
    campos = set(re.findall(r"corpo\.(\w+)\s*=", m.group(0)))
    assert campos, "could not find any `corpo.<campo> = ` assignment in guardarEdicaoPagamento"
    return campos


def _campos_enviados_no_patch_compras() -> set[str]:
    """Same reasoning as _campos_enviados_no_patch_transferencias, for
    PATCH /api/compras/{id}'s body, built field by field in
    `abrirEdicaoCompra`'s submit handler."""
    js = _texto_app_js()
    m = re.search(r"function abrirEdicaoCompra\(li, c\) \{(.*?)\n\}\n", js, re.S)
    assert m, "could not find `function abrirEdicaoCompra(li, c) {...}` in app.js"
    campos = set(re.findall(r"corpo\.(\w+)\s*=", m.group(1)))
    assert campos, "could not find any `corpo.<campo> = ` assignment in abrirEdicaoCompra"
    return campos


def test_os_tres_corpos_que_o_cliente_envia_ao_pagar_movem_mesmo_o_dinheiro_certo(cliente):
    """Sends exactly the three body shapes app.js's own call sites build
    (extracted, not retyped) to the real POST /api/transferencias, then
    proves each one's effect by reading balances/caixa back, never trusting
    the 201 alone."""
    ana = regista(cliente, "Ana")  # guarda a caixa
    bruno = regista(cliente, "Bruno")
    carla = regista(cliente, "Carla")
    ana_id = ana.get("/api/eu").json()["utilizador"]["id"]
    bruno_id = bruno.get("/api/eu").json()["utilizador"]["id"]

    r = ana.put("/api/config", json={"caixa_responsavel_id": ana_id})
    assert r.status_code == 200, r.text

    corpos = _corpos_enviados_para_post_transferencias()

    # 1) para a caixa
    campos = corpos["para_caixa"]
    valores = {"recebedor_id": None, "para_caixa": True, "de_caixa": False, "valor_cent": 500}
    corpo = {campo: valores[campo] for campo in campos}
    r = bruno.post("/api/transferencias", json=corpo)
    assert r.status_code == 201, r.text
    caixa_depois = ana.get("/api/eu").json()["caixa"]
    assert caixa_depois["dinheiro_cent"] == 500, (
        f"the client's own body {corpo} paid 500 cents to the caixa, dinheiro_cent should "
        f"be +500, got {caixa_depois['dinheiro_cent']}"
    )

    # 2) a uma pessoa
    campos = corpos["pessoa"]
    valores = {"recebedor_id": bruno_id, "para_caixa": False, "de_caixa": False, "valor_cent": 300}
    corpo = {campo: valores[campo] for campo in campos}
    r = carla.post("/api/transferencias", json=corpo)
    assert r.status_code == 201, r.text
    carla_depois = carla.get("/api/eu").json()
    assert carla_depois["saldo_cent"] == 300, (
        f"the client's own body {corpo} should have moved Carla's saldo_cent to +300, got "
        f"{carla_depois['saldo_cent']}"
    )

    # 3) reembolso de_caixa, pelo responsável
    campos = corpos["de_caixa"]
    valores = {"recebedor_id": bruno_id, "de_caixa": True, "para_caixa": False, "valor_cent": 100}
    corpo = {campo: valores[campo] for campo in campos}
    r = ana.post("/api/transferencias", json=corpo)
    assert r.status_code == 201, r.text
    caixa_final = ana.get("/api/eu").json()["caixa"]
    assert caixa_final["dinheiro_cent"] == 400, (
        f"the client's own body {corpo} paid 100 cents out of the caixa, dinheiro_cent "
        f"should drop from 500 to 400, got {caixa_final['dinheiro_cent']}"
    )


def test_o_corpo_que_o_cliente_envia_ao_comprar_capsulas_grava_mesmo_a_compra(cliente):
    """Sends exactly what app.js's purchase form builds to the real
    POST /api/compras, then proves the effect through /api/escritorio: the
    new entry's own fields, never the 201 alone."""
    ana = regista(cliente, "Ana")

    campos = _corpo_enviado_para_post_compras()
    assert campos == {"capsulas", "custo_cent", "paga_pela_caixa", "nota"}, (
        f"app.js's purchase form sends {sorted(campos)}, expected exactly capsulas, "
        f"custo_cent, paga_pela_caixa and nota (spec 4.9)"
    )
    valores = {"capsulas": 50, "custo_cent": 1500, "paga_pela_caixa": False, "nota": "compra de teste"}
    corpo = {campo: valores[campo] for campo in campos}

    r = ana.post("/api/compras", json=corpo)
    assert r.status_code == 201, r.text

    compra = ana.get("/api/escritorio").json()["compras"][0]
    assert compra["capsulas"] == 50
    assert compra["custo_cent"] == 1500
    assert compra["paga_pela_caixa"] is False


def test_o_corpo_que_o_cliente_envia_numa_oferta_grava_custo_zero_e_paga_pela_caixa_falso(cliente):
    """A gift (Paga com: Oferta) sends the same field set as any other
    purchase, but with custo_cent = 0; spec 4.9 says the server must force
    paga_pela_caixa to 0 in that case regardless of what was sent, so this
    sends true for it on purpose."""
    ana = regista(cliente, "Ana")

    campos = _corpo_enviado_para_post_compras()
    valores = {"capsulas": 77, "custo_cent": 0, "paga_pela_caixa": True, "nota": None}
    corpo = {campo: valores[campo] for campo in campos}

    r = ana.post("/api/compras", json=corpo)
    assert r.status_code == 201, r.text

    compra = ana.get("/api/escritorio").json()["compras"][0]
    assert compra["capsulas"] == 77
    assert compra["custo_cent"] == 0
    assert compra["paga_pela_caixa"] is False, (
        "a 0-cost purchase (a gift) must never be recorded as paid by the caixa, even if "
        "the client's own body says otherwise (spec 4.9)"
    )


def test_o_cliente_pode_editar_valor_e_destino_de_um_pagamento_e_o_servidor_grava_os_dois(cliente):
    """Sends exactly what app.js's guardarEdicaoPagamento builds for the two
    shapes it actually assembles (a value-only change, and a destination
    change from a person to the caixa, always sent as para_caixa+recebedor_id
    together): proves the server applies each field by reading the
    transferencia back and by a historico_alteracoes row for it."""
    ana = regista(cliente, "Ana")
    carla = regista(cliente, "Carla")
    bruno = regista(cliente, "Bruno")
    ana_id = ana.get("/api/eu").json()["utilizador"]["id"]
    bruno_id = bruno.get("/api/eu").json()["utilizador"]["id"]

    r = ana.put("/api/config", json={"caixa_responsavel_id": ana_id})
    assert r.status_code == 200, r.text

    campos = _campos_enviados_no_patch_transferencias()
    assert campos == {"valor_cent", "para_caixa", "recebedor_id"}, (
        f"guardarEdicaoPagamento sends {sorted(campos)}, expected exactly valor_cent, "
        f"para_caixa and recebedor_id (spec 4.3)"
    )

    r = carla.post("/api/transferencias", json={"recebedor_id": bruno_id, "para_caixa": False, "valor_cent": 500})
    assert r.status_code == 201, r.text
    t_id = r.json()["id"]

    # valor_cent sozinho
    r = carla.patch(f"/api/transferencias/{t_id}", json={"valor_cent": 700})
    assert r.status_code == 200, r.text
    item = next(t for t in carla.get("/api/transferencias").json()["transferencias"] if t["id"] == t_id)
    assert item["valor_cent"] == 700
    hist = carla.get(f"/api/historico-alteracoes?entidade=transferencia&id={t_id}").json()["alteracoes"]
    assert any(a["campo"] == "valor_cent" and a["antes"] == "500" and a["depois"] == "700" for a in hist), (
        f"PATCH {{'valor_cent': 700}} left no matching historico_alteracoes row: {hist}"
    )

    # para_caixa + recebedor_id juntos, como o cliente sempre os envia ao mudar o destino
    r = carla.patch(f"/api/transferencias/{t_id}", json={"para_caixa": True, "recebedor_id": None})
    assert r.status_code == 200, r.text
    item = next(t for t in carla.get("/api/transferencias").json()["transferencias"] if t["id"] == t_id)
    assert item["para_caixa"] is True
    assert item["recebedor_id"] is None
    assert item["recebedor"] == "Caixa"
    hist = carla.get(f"/api/historico-alteracoes?entidade=transferencia&id={t_id}").json()["alteracoes"]
    assert any(a["campo"] == "para_caixa" for a in hist), f"no historico row for para_caixa: {hist}"
    assert any(a["campo"] == "recebedor_id" for a in hist), f"no historico row for recebedor_id: {hist}"


def test_o_cliente_pode_corrigir_custo_capsulas_e_quem_pagou_de_uma_entrada(cliente):
    """Sends exactly what app.js's abrirEdicaoCompra builds for each of the
    three fields it can send on its own (spec 4.9: "qualquer subconjunto de
    custo_cent, capsulas, paga_pela_caixa"), one at a time, proving the
    server applies each by reading the entry back and by a
    historico_alteracoes row for it."""
    ana = regista(cliente, "Ana")
    ana_id = ana.get("/api/eu").json()["utilizador"]["id"]

    r = ana.put("/api/config", json={"caixa_responsavel_id": ana_id})
    assert r.status_code == 200, r.text

    campos = _campos_enviados_no_patch_compras()
    assert campos == {"capsulas", "custo_cent", "paga_pela_caixa"}, (
        f"abrirEdicaoCompra sends {sorted(campos)}, expected exactly capsulas, custo_cent "
        f"and paga_pela_caixa (spec 4.9)"
    )

    r = ana.post("/api/compras", json={"capsulas": 50, "custo_cent": 1500, "paga_pela_caixa": False, "nota": None})
    assert r.status_code == 201, r.text
    compra_id = ana.get("/api/escritorio").json()["compras"][0]["id"]

    r = ana.patch(f"/api/compras/{compra_id}", json={"custo_cent": 1450})
    assert r.status_code == 200, r.text
    compra = ana.get("/api/escritorio").json()["compras"][0]
    assert compra["custo_cent"] == 1450
    assert compra["custo_estimado"] is False
    hist = ana.get(f"/api/historico-alteracoes?entidade=compra&id={compra_id}").json()["alteracoes"]
    assert any(a["campo"] == "custo_cent" and a["antes"] == "1500" and a["depois"] == "1450" for a in hist)

    r = ana.patch(f"/api/compras/{compra_id}", json={"capsulas": 60})
    assert r.status_code == 200, r.text
    compra = ana.get("/api/escritorio").json()["compras"][0]
    assert compra["capsulas"] == 60
    hist = ana.get(f"/api/historico-alteracoes?entidade=compra&id={compra_id}").json()["alteracoes"]
    assert any(a["campo"] == "capsulas" and a["antes"] == "50" and a["depois"] == "60" for a in hist)

    r = ana.patch(f"/api/compras/{compra_id}", json={"paga_pela_caixa": True})
    assert r.status_code == 200, r.text
    compra = ana.get("/api/escritorio").json()["compras"][0]
    assert compra["paga_pela_caixa"] is True
    assert compra["editada"] is True
    hist = ana.get(f"/api/historico-alteracoes?entidade=compra&id={compra_id}").json()["alteracoes"]
    assert any(a["campo"] == "paga_pela_caixa" and a["antes"] == "0" and a["depois"] == "1" for a in hist)


def test_o_corpo_que_o_cliente_envia_nas_definicoes_grava_mesmo_o_preco_o_limiar_e_o_responsavel(cliente):
    """Sends exactly what app.js's Definições form builds to the real
    PUT /api/config, then proves the effect through GET /api/config and a
    historico_alteracoes row per changed key (spec 4.10)."""
    ana = regista(cliente, "Ana")
    bruno = regista(cliente, "Bruno")
    bruno_id = bruno.get("/api/eu").json()["utilizador"]["id"]

    campos = _corpo_enviado_para_put_config()
    assert campos == {"preco_cent", "stock_baixo", "caixa_responsavel_id"}, (
        f"app.js's Definições form sends {sorted(campos)}, expected exactly preco_cent, "
        f"stock_baixo and caixa_responsavel_id (spec 4.10)"
    )
    valores = {"preco_cent": 3000, "stock_baixo": 5, "caixa_responsavel_id": bruno_id}
    corpo = {campo: valores[campo] for campo in campos}

    r = ana.put("/api/config", json=corpo)
    assert r.status_code == 200, r.text

    cfg = ana.get("/api/config").json()
    assert cfg["preco_cent"] == 3000
    assert cfg["stock_baixo"] == 5
    assert cfg["caixa_responsavel_id"] == bruno_id

    hist = ana.get("/api/historico-alteracoes?entidade=config").json()["alteracoes"]
    assert any(a["campo"] == "preco_cent" and a["depois"] == "3000" for a in hist)
    assert any(a["campo"] == "stock_baixo" and a["depois"] == "5" for a in hist)
    assert any(a["campo"] == "caixa_responsavel_id" and a["depois"] == str(bruno_id) for a in hist)


# ---- GET /api/config (spec 4.11) ----

def _campos_de_config() -> set[str]:
    js = _texto_app_js()
    m = re.search(r"async function carregarConfig\(\) \{(.*?)\n\}\n", js, re.S)
    assert m, "could not find `async function carregarConfig() {...}` in app.js"
    campos = set(re.findall(r"config\.(\w+)", m.group(1)))
    assert campos, "could not find any `config.<campo>` read in carregarConfig"
    return campos


def test_o_get_config_tem_os_campos_que_o_cliente_le(cliente):
    ana = regista(cliente, "Ana")
    ana_id = ana.get("/api/eu").json()["utilizador"]["id"]
    r = ana.put("/api/config", json={"preco_cent": 30, "stock_baixo": 20, "caixa_responsavel_id": ana_id})
    assert r.status_code == 200, r.text

    campos = _campos_de_config()
    cfg = ana.get("/api/config").json()
    for campo in campos:
        assert campo in cfg, (
            f"app.js reads `config.{campo}`, but GET /api/config has no such field: {sorted(cfg)}"
        )
    assert cfg["preco_cent"] == 30
    assert cfg["stock_baixo"] == 20
    assert cfg["caixa_responsavel_id"] == ana_id


# ---- GET /api/transferencias: pagamentos de toda a gente (spec 4.5) ----

def _campos_de_pagamento_na_lista() -> set[str]:
    """Field names app.js reads off an item of GET /api/transferencias's
    `transferencias` array, both in the Escritório → Pagamentos row builder
    (linhaPagamento) and in opening its inline edit form
    (abrirEdicaoPagamento, the one place that also reads de_caixa),
    extracted instead of retyped."""
    js = _texto_app_js()
    m1 = re.search(r"function linhaPagamento\(t\) \{(.*?)\n\}\n", js, re.S)
    assert m1, "could not find `function linhaPagamento(t) {...}` in app.js"
    m2 = re.search(r"async function abrirEdicaoPagamento\(li, t\) \{(.*?)\n\}\n", js, re.S)
    assert m2, "could not find `async function abrirEdicaoPagamento(li, t) {...}` in app.js"
    campos = set(re.findall(r"(?<!\w)t\.(\w+)", m1.group(1))) | set(re.findall(r"(?<!\w)t\.(\w+)", m2.group(1)))
    assert campos, "could not find any `t.<campo>` read off a pagamento in app.js"
    return campos


def test_os_campos_de_um_pagamento_na_lista_de_transferencias_batem_com_a_resposta_real(cliente):
    """Three payments that exercise every shape spec 4.5 describes: person to
    person, person to caixa, and a de_caixa reimbursement, so pagador,
    recebedor, para_caixa and de_caixa are all meaningfully non-trivial in
    the same response."""
    ana = regista(cliente, "Ana")  # guarda a caixa
    bruno = regista(cliente, "Bruno")
    carla = regista(cliente, "Carla")
    ana_id = ana.get("/api/eu").json()["utilizador"]["id"]
    bruno_id = bruno.get("/api/eu").json()["utilizador"]["id"]
    carla_id = carla.get("/api/eu").json()["utilizador"]["id"]

    r = ana.put("/api/config", json={"caixa_responsavel_id": ana_id})
    assert r.status_code == 200, r.text

    r = bruno.post("/api/transferencias", json={"recebedor_id": carla_id, "para_caixa": False, "valor_cent": 300})
    assert r.status_code == 201, r.text
    r = carla.post("/api/transferencias", json={"recebedor_id": None, "para_caixa": True, "valor_cent": 400})
    assert r.status_code == 201, r.text
    r = ana.post("/api/transferencias", json={"recebedor_id": bruno_id, "de_caixa": True, "valor_cent": 100})
    assert r.status_code == 201, r.text

    campos = _campos_de_pagamento_na_lista()
    lista = ana.get("/api/transferencias").json()["transferencias"]
    assert len(lista) == 3, f"expected the 3 payments just registered, got {len(lista)}: {lista}"
    for item in lista:
        for campo in campos:
            assert campo in item, (
                f"app.js reads `t.{campo}` off a pagamento, but the response's item has no "
                f"such field: {sorted(item)}"
            )

    de_pessoa = next(t for t in lista if not t["para_caixa"] and not t["de_caixa"])
    assert de_pessoa["pagador"] == "Bruno"
    assert de_pessoa["recebedor"] == "Carla"
    assert de_pessoa["recebedor_id"] == carla_id
    assert de_pessoa["valor_cent"] == 300
    assert de_pessoa["confirmada_em"] is None
    assert de_pessoa["anulada_em"] is None
    assert de_pessoa["editada"] is False

    para_caixa_item = next(t for t in lista if t["para_caixa"])
    assert para_caixa_item["pagador"] == "Carla"
    assert para_caixa_item["recebedor"] == "Caixa"
    assert para_caixa_item["recebedor_id"] is None
    assert para_caixa_item["valor_cent"] == 400
    assert para_caixa_item["pode_confirmar"] is True  # Ana guards the caixa and is asking
    assert para_caixa_item["pode_anular"] is True
    assert para_caixa_item["pode_editar"] is False  # only Carla, who paid, can edit it

    de_caixa_item = next(t for t in lista if t["de_caixa"])
    assert de_caixa_item["pagador"] == "Caixa"
    assert de_caixa_item["recebedor"] == "Bruno"
    assert de_caixa_item["recebedor_id"] == bruno_id
    assert de_caixa_item["valor_cent"] == 100
    assert de_caixa_item["pode_editar"] is True  # Ana, asking, is the paying side (de_caixa)
    assert de_caixa_item["pode_anular"] is True
    assert de_caixa_item["pode_confirmar"] is False


# ---- GET /api/historico-alteracoes (spec 4.6) ----

def _campos_de_alteracao_no_historico() -> set[str]:
    """Field names `formatarAlteracao` reads off one historico_alteracoes
    item, extracted from its own function body."""
    js = _texto_app_js()
    m = re.search(r"function formatarAlteracao\(a\) \{(.*?)\n\}\n", js, re.S)
    assert m, "could not find `function formatarAlteracao(a) {...}` in app.js"
    campos = set(re.findall(r"(?<!\w)a\.(\w+)", m.group(1)))
    assert campos, "could not find any `a.<campo>` read off a historico_alteracoes item in app.js"
    return campos


def test_o_historico_de_uma_alteracao_real_tem_os_campos_que_o_cliente_le(cliente):
    """Edits a real transferencia (changes its valor_cent) and reads the
    change back through GET /api/historico-alteracoes, checking every field
    formatarAlteracao actually reads off an item, by value, not just
    presence."""
    ana = regista(cliente, "Ana")
    bruno = regista(cliente, "Bruno")
    bruno_id = bruno.get("/api/eu").json()["utilizador"]["id"]

    r = ana.post("/api/transferencias", json={"recebedor_id": bruno_id, "para_caixa": False, "valor_cent": 500})
    assert r.status_code == 201, r.text
    t_id = r.json()["id"]

    r = ana.patch(f"/api/transferencias/{t_id}", json={"valor_cent": 600})
    assert r.status_code == 200, r.text

    campos = _campos_de_alteracao_no_historico()
    hist = ana.get(f"/api/historico-alteracoes?entidade=transferencia&id={t_id}").json()["alteracoes"]
    assert hist, "the edit above must have left a historico_alteracoes row"
    linha = next(a for a in hist if a["campo"] == "valor_cent")
    for campo in campos:
        assert campo in linha, (
            f"app.js reads `a.{campo}` off a historico_alteracoes item, but the response's "
            f"item has no such field: {sorted(linha)}"
        )
    assert linha["antes"] == "500"
    assert linha["depois"] == "600"
    assert linha["utilizador"] == "Ana"
    assert linha["em"]


# ---- GET /api/preco/simular sai (spec 4.9) ----

def test_preco_simular_saiu_do_cliente_e_devolve_404_no_servidor(cliente):
    """Spec 4.9: GET /api/preco/simular sai (404). Checks both halves of the
    contract: app.js no longer references the route at all, and the server
    really returns 404 for it, not just that the client stopped calling it."""
    assert "preco/simular" not in _texto_app_js(), (
        "app.js still references /preco/simular, but spec 4.9 removes this route"
    )
    ana = regista(cliente, "Ana")
    r = ana.get("/api/preco/simular?capsulas=100&custo_cent=3000")
    assert r.status_code == 404, r.text
