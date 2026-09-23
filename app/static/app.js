/* Café do escritório, front-end sem dependências. */
"use strict";

const $ = (id) => document.getElementById(id);
const vistas = { entrada: $("vista-entrada"), cafe: $("vista-cafe"), historico: $("vista-historico"), escritorio: $("vista-escritorio") };
let eu = null;           // resposta de /api/eu
let escritorio = null;   // resposta de /api/escritorio
let historico = null;    // resposta de /api/historico (o mês visível)
let histDia = null;      // dia seleccionado no calendário, "YYYY-MM-DD"
let histPorDia = new Map(); // dia -> { n, horas: [{ hora, fila }] }, servidor mais fila offline
let dinheiro = null;     // resposta de /api/movimentos (separador Histórico → Dinheiro)

// ---------- utilidades ----------

function euros(cent) {
  return (cent / 100).toFixed(2).replace(".", ",") + " €";
}
function dataCurta(iso) {
  const d = new Date(iso);
  return String(d.getDate()).padStart(2, "0") + "/" + String(d.getMonth() + 1).padStart(2, "0");
}
function nomeMes(ym) {
  const [a, m] = ym.split("-").map(Number);
  const meses = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"];
  return `${meses[m - 1]} ${a}`;
}
function plural(n, s, p) { return `${n} ${n === 1 ? s : p}`; }

// ---------- caixa: nomes de utilizadores, destinos de pagamento e histórico de alterações ----------
//
// Shared by every screen that touches a pagamento, uma compra ou as
// definições: Café (o formulário de pagar), Histórico → Dinheiro (Editar) e
// Escritório (Reembolsar, Editar num pagamento ou numa compra, Definições).

let utilizadoresCache = null;

// Fetched fresh (never trusts a stale cache): the people list can change
// between screens, same reasoning abrirFormPagar already used before this
// helper existed.
async function listaUtilizadores() {
  utilizadoresCache = await api("GET", "/utilizadores");
  return utilizadoresCache;
}

function nomeUtilizador(id) {
  if (id === null || id === undefined) return null;
  const u = (utilizadoresCache || []).find((x) => x.id === Number(id));
  return u ? u.nome : `#${id}`;
}

// Builds the "A quem" / "Destino" options for a payment: "normal" is Caixa
// (when a keeper exists) plus everyone except me; "de_caixa" is everyone
// including me, since the keeper can reimburse themselves (spec 3.4).
async function opcoesDestino(cfg) {
  const tipo = (cfg && cfg.tipo) || "normal";
  const lista = await listaUtilizadores();
  const opcoes = [];
  if (tipo === "normal" && eu && eu.caixa) opcoes.push({ value: "caixa", label: `Caixa (${eu.caixa.responsavel})` });
  for (const u of lista) {
    if (tipo === "normal" && eu && u.id === eu.utilizador.id) continue;
    opcoes.push({ value: String(u.id), label: u.nome });
  }
  return opcoes;
}

// Parses a euro-amount input into cents, or null if out of the 0,01..1000 €
// range the server accepts (spec 4.2/4.3).
function lerValorCent(input) {
  const cent = Math.round(Number(input.value.replace(",", ".")) * 100);
  return cent >= 1 && cent <= 100000 ? cent : null;
}

// Payments, edits and settings always need the network (spec item 6): never
// queued, and the error shown must say so instead of a generic "Sem
// ligação." from a failed fetch().
function msgRede(erro, acao) {
  return erro && erro.rede ? `Precisas de rede para ${acao}.` : erro.message;
}

// Portuguese labels for tests/historico_alteracoes.campo (spec 5.4).
const CAMPO_HISTORICO_LABEL = {
  valor_cent: "valor",
  recebedor_id: "destino",
  para_caixa: "destino",
  custo_cent: "custo",
  capsulas: "cápsulas",
  paga_pela_caixa: "paga com",
  preco_cent: "preço por café",
  stock_baixo: "limiar",
  caixa_responsavel_id: "responsável pela caixa",
  anulada: "anulado",
  confirmada: "confirmado",
};

// Renders one antes/depois value of historico_alteracoes in the shape the
// field asks for: cents to euros, person ids to names, the two null cases
// named per spec 5.4 ("Caixa" for a null destination, "ninguém" for a null
// keeper), the rest as the raw text the server sent.
function textoValorHistorico(campo, valor) {
  if (valor === null || valor === undefined) {
    if (campo === "recebedor_id") return "Caixa";
    if (campo === "caixa_responsavel_id") return "ninguém";
    return "-";
  }
  switch (campo) {
    case "valor_cent": case "custo_cent": case "preco_cent":
      return euros(Number(valor));
    case "recebedor_id": case "caixa_responsavel_id":
      return nomeUtilizador(valor);
    case "para_caixa":
      return valor === "1" ? "Caixa" : "pessoa";
    case "paga_pela_caixa":
      return valor === "1" ? "pela caixa" : "do bolso";
    case "anulada": case "confirmada":
      return valor === "1" ? "sim" : "não";
    default:
      return valor;
  }
}

function formatarAlteracao(a) {
  const rotulo = CAMPO_HISTORICO_LABEL[a.campo] || a.campo;
  const antes = textoValorHistorico(a.campo, a.antes);
  const depois = textoValorHistorico(a.campo, a.depois);
  const quem = a.utilizador || "sistema";
  return `${dataCurta(a.em)} ${horaCurta(a.em)} · ${quem} · ${rotulo} ${antes} → ${depois}`;
}

// A pagamento's destination moving between "Caixa" and a person writes TWO
// historico_alteracoes rows (campo recebedor_id and campo para_caixa) from
// the same PATCH: shown apart they read as two edits, not one. The server
// computes the request's instant once and passes it to every
// regista_alteracao() call of that request, so the two rows share the exact
// same `em`; they pair on em + utilizador equality, order-independent.
// Drops the para_caixa half of a matched pair; the paired recebedor_id row
// alone already renders "Caixa" for a null destination
// (textoValorHistorico), so it reads as the single "destino Caixa → Pedro" /
// "Pedro → Caixa" line the spec wants. A lone recebedor_id change (person to
// person, no matching para_caixa row) is left untouched, and a lone
// para_caixa row (no matching recebedor_id row) is kept too, in case one is
// ever written on its own.
function juntarAlteracoesDestino(alteracoes) {
  const chave = (a) => `${a.em}|${a.utilizador}`;
  const destinos = new Set(alteracoes.filter((a) => a.campo === "recebedor_id").map(chave));
  return alteracoes.filter((a) => a.campo !== "para_caixa" || !destinos.has(chave(a)));
}

// Toggles the change history under a row (spec 5.4). `ul` is the
// `.historico-lista` element to fill and show/hide; `id` is omitted (left
// undefined) for entidade=config, the one entity that has none. Fetched
// once per element and cached in the DOM (`dataset.carregado`), so toggling
// open/closed again never re-fetches.
async function expandirHistorico(ul, entidade, id) {
  if (!ul.hidden) { ul.hidden = true; return; }
  if (!ul.dataset.carregado) {
    if (!utilizadoresCache) await listaUtilizadores().catch(() => { utilizadoresCache = []; });
    try {
      const qs = id != null ? `?entidade=${entidade}&id=${id}` : `?entidade=${entidade}`;
      const r = await api("GET", `/historico-alteracoes${qs}`);
      const alteracoes = juntarAlteracoesDestino(r.alteracoes || []);
      ul.innerHTML = alteracoes.map((a) => `<li>${formatarAlteracao(a)}</li>`).join("") || "<li>Sem alterações.</li>";
      ul.dataset.carregado = "1";
    } catch (erro) { toast(erro.message, true); return; }
  }
  ul.hidden = false;
}

// Builds the inline "valor + destino" mini-form used by Reembolsar and by
// Editar on a pagamento (Histórico → Dinheiro and Escritório → Pagamentos).
// `opcoesDestinoLista` null means a fixed destination (Reembolsar: the
// person is already chosen, so no select is shown at all).
function montarFormPagamentoInline(container, opts) {
  const temDestino = !!opts.opcoesDestino;
  container.innerHTML = `<form class="linha">
    <label>Valor (€) <input type="number" step="0.01" min="0.01" max="1000" class="fp-valor" required></label>
    ${temDestino ? '<label>Destino <select class="fp-destino" required></select></label>' : ""}
    <button type="submit">${opts.textoGuardar || "Guardar"}</button>
    <button type="button" class="ligacao fp-cancelar">Cancelar</button>
  </form>`;
  const form = container.querySelector("form");
  const valorInput = form.querySelector(".fp-valor");
  valorInput.value = (opts.valorInicial / 100).toFixed(2);
  let sel = null;
  if (temDestino) {
    sel = form.querySelector(".fp-destino");
    for (const o of opts.opcoesDestino) {
      const op = document.createElement("option");
      op.value = o.value; op.textContent = o.label;
      sel.appendChild(op);
    }
    if (opts.destinoInicial !== undefined) {
      // The current destination can be missing from opcoesDestino (eg. "caixa"
      // after the keeper was unset): without this, sel.value silently falls
      // back to the first option and Guardar would save a change no one made.
      if (![...sel.options].some((o) => o.value === opts.destinoInicial)) {
        const op = document.createElement("option");
        op.value = opts.destinoInicial;
        op.textContent = opts.destinoInicialLabel || opts.destinoInicial;
        sel.insertBefore(op, sel.firstChild);
      }
      sel.value = opts.destinoInicial;
    }
  }
  form.querySelector(".fp-cancelar").onclick = () => {
    container.hidden = true;
    container.innerHTML = "";
    if (opts.onCancelar) opts.onCancelar();
  };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const valorCent = lerValorCent(valorInput);
    if (valorCent === null) return toast("Mete um valor entre 0,01 € e 1000 €.", true);
    await opts.onGuardar(valorCent, sel ? sel.value : undefined);
  };
  container.hidden = false;
}

// Builds the PATCH /api/transferencias/{id} body from what actually
// changed (spec 4.3: "qualquer subconjunto"; 400 if nothing changes).
// `comCaixa` is false for a de_caixa row, where the destination is always a
// person and there is no "para a caixa" choice to send.
async function guardarEdicaoPagamento(id, valorCent, destinoValue, original, comCaixa) {
  const corpo = {};
  if (valorCent !== original.valorCent) corpo.valor_cent = valorCent;
  if (destinoValue !== original.destino) {
    if (comCaixa) {
      corpo.para_caixa = destinoValue === "caixa";
      corpo.recebedor_id = destinoValue === "caixa" ? null : Number(destinoValue);
    } else {
      corpo.recebedor_id = Number(destinoValue);
    }
  }
  if (!Object.keys(corpo).length) { toast("Nada para guardar."); return false; }
  await api("PATCH", `/transferencias/${id}`, corpo);
  return true;
}

// "Deves 2,50 €" / "Tens 1,00 € a teu favor" / "Contas certas": any negative
// balance says "Deves", including someone else's money still owed to the pot.
function fraseSaldo(cent) {
  if (cent < 0) return `Deves ${euros(-cent)}`;
  if (cent > 0) return `Tens ${euros(cent)} a teu favor`;
  return "Contas certas";
}

// "hoje" / "ontem" / a short date, for the "por confirmar" and "Dinheiro"
// lists. Deliberately separate from textoUltimoCafe(): that one also knows
// about the offline queue, which these lists never touch.
function diaRelativo(iso) {
  const d = new Date(iso);
  const agora = new Date();
  const ontem = new Date(agora.getFullYear(), agora.getMonth(), agora.getDate() - 1);
  const dia = diaLocal(d);
  if (dia === diaLocal(agora)) return "hoje";
  if (dia === diaLocal(ontem)) return "ontem";
  return dataCurta(iso);
}

const DIAS_SEMANA = ["dom", "seg", "ter", "qua", "qui", "sex", "sáb"];

// Local calendar keys built from the local date fields, never from
// toISOString(): in Lisbon a local midnight is still the previous day in UTC.
function diaLocal(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function mesLocal(d) {
  return diaLocal(d).slice(0, 7);
}
function mesVizinho(ym, delta) {
  const [a, m] = ym.split("-").map(Number);
  return mesLocal(new Date(a, m - 1 + delta, 1));
}

let toastTimer;
function toast(msg, erro = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = erro ? "erro" : "";
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), erro ? 4000 : 1800);
}

async function api(metodo, rota, corpo) {
  let r;
  try {
    r = await fetch("/api" + rota, {
      method: metodo,
      headers: corpo ? { "Content-Type": "application/json" } : {},
      body: corpo ? JSON.stringify(corpo) : undefined,
    });
  } catch {
    // fetch() itself threw: no network, not an HTTP error. Callers that care
    // about the offline case check `erro.rede` instead of parsing messages.
    const erro = new Error("Sem ligação.");
    erro.rede = true;
    throw erro;
  }
  if (r.status === 401 && rota !== "/login") {
    // Session expired: not a decision to discard work, so the queue (fila) is
    // left alone. But someone else's /api/escritorio snapshot has no reason
    // to keep sitting on a shared device's disk after the session that
    // fetched it is gone, so the instantaneos store is wiped here too.
    idbLimpar("instantaneos").catch((erro) => console.error("Falha ao limpar fotografias após expirar a sessão:", erro));
    carregarNomes().catch(() => {});
    mostrar("entrada");
    throw new Error("Sessão expirada. Entra outra vez.");
  }
  const dados = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(dados.detail || `Erro ${r.status}`);
  return dados;
}

// ---------- offline (IndexedDB) ----------

const DB_NOME = "cafe-offline";
const DB_VERSAO = 1;
let dbPromise = null;

function abrirDB() {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const pedido = indexedDB.open(DB_NOME, DB_VERSAO);
    pedido.onupgradeneeded = () => {
      const db = pedido.result;
      if (!db.objectStoreNames.contains("fila")) db.createObjectStore("fila", { keyPath: "cliente_id" });
      if (!db.objectStoreNames.contains("instantaneos")) db.createObjectStore("instantaneos", { keyPath: "chave" });
    };
    pedido.onsuccess = () => resolve(pedido.result);
    pedido.onerror = () => reject(pedido.error);
  });
  return dbPromise;
}

// Wraps an IDBRequest in a promise; every idb* helper below builds on this.
function idbPedido(pedido) {
  return new Promise((resolve, reject) => {
    pedido.onsuccess = () => resolve(pedido.result);
    pedido.onerror = () => reject(pedido.error);
  });
}

async function idbPut(loja, valor) {
  const db = await abrirDB();
  return idbPedido(db.transaction(loja, "readwrite").objectStore(loja).put(valor));
}
async function idbApagar(loja, chave) {
  const db = await abrirDB();
  return idbPedido(db.transaction(loja, "readwrite").objectStore(loja).delete(chave));
}
async function idbTodos(loja) {
  const db = await abrirDB();
  return idbPedido(db.transaction(loja, "readonly").objectStore(loja).getAll());
}
async function idbUm(loja, chave) {
  const db = await abrirDB();
  return idbPedido(db.transaction(loja, "readonly").objectStore(loja).get(chave));
}
async function idbLimpar(loja) {
  const db = await abrirDB();
  return idbPedido(db.transaction(loja, "readwrite").objectStore(loja).clear());
}

// Wipes the whole offline database. Called on logout: /api/escritorio holds
// everyone's data, so per-user keying only stays safe on a shared device if
// nothing survives past the session that fetched it.
async function apagarDB() {
  dbPromise = null;
  await new Promise((resolve) => {
    const pedido = indexedDB.deleteDatabase(DB_NOME);
    pedido.onsuccess = () => resolve();
    pedido.onerror = () => resolve();
    pedido.onblocked = () => resolve();
  });
}

// Stores the JSON response of an authenticated GET exactly as it arrived,
// keyed by user so a shared device never mixes two people's snapshots. The
// optional suffix keys one snapshot per month (historico:<id>:<mes>); the
// callers without it keep the same eu:<id> and escritorio:<id> keys.
async function guardarInstantaneo(tipo, utilizadorId, dados, sufixo) {
  try {
    await idbPut("instantaneos", {
      chave: `${tipo}:${utilizadorId}` + (sufixo ? `:${sufixo}` : ""),
      utilizador_id: utilizadorId,
      dados,
      em: new Date().toISOString(),
    });
  } catch (erro) {
    console.error("Falha ao guardar fotografia offline:", erro);
  }
}

function horaCurta(iso) {
  return new Date(iso).toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit" });
}

// Reflects how many queued coffees are waiting to sync, shown right under
// the coffee button so offline state is never a blank screen.
async function atualizarAvisoFila() {
  const el = $("fila-aviso");
  if (!el) return;
  let n = 0;
  try { n = (await idbTodos("fila")).length; } catch { n = 0; }
  if (n > 0) {
    el.textContent = `${plural(n, "café", "cafés")} por sincronizar`;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

// Text for the "Último café" line: the most recent of the server's
// ultimo_cafe and the queued entries (which carry `em`). "hoje" and "ontem"
// come from the browser clock, like the snapshot warnings already do.
function textoUltimoCafe(ultimoIso, fila) {
  let em = ultimoIso || null;
  let porSincronizar = false;
  for (const entrada of fila || []) {
    if (!em || Date.parse(entrada.em) > Date.parse(em)) { em = entrada.em; porSincronizar = true; }
  }
  if (!em) return "Ainda nenhum café.";
  const d = new Date(em);
  const agora = new Date();
  const ontem = new Date(agora.getFullYear(), agora.getMonth(), agora.getDate() - 1);
  const dia = diaLocal(d);
  let quando;
  if (dia === diaLocal(agora)) quando = "hoje";
  else if (dia === diaLocal(ontem)) quando = "ontem";
  else quando = `${DIAS_SEMANA[d.getDay()]} ${dataCurta(em)}`;
  return `Último café: ${quando} às ${horaCurta(em)}${porSincronizar ? " (por sincronizar)" : ""}`;
}

// Redrawn at the same points as atualizarAvisoFila(): both read the queue.
async function atualizarUltimoCafe() {
  const el = $("ultimo-cafe");
  if (!el || !eu) return;
  let fila;
  try { fila = await idbTodos("fila"); } catch { fila = []; }
  el.textContent = textoUltimoCafe(eu.ultimo_cafe, fila);
}

// The last-coffee undo is only safe while the coffee is still in the local
// queue (see spec section 7): with an empty queue and no network there is
// nothing local to remove, and replaying DELETE /api/cafe/ultimo later could
// remove a different coffee than the one the person saw.
async function atualizarEstadoDesfazer() {
  const btn = $("btn-desfazer");
  const razao = $("desfazer-razao");
  if (!btn) return;
  let n = 0;
  try { n = (await idbTodos("fila")).length; } catch { n = 0; }
  if (n === 0 && !navigator.onLine) {
    btn.disabled = true;
    if (razao) { razao.textContent = "Sem café por sincronizar e sem ligação: não é possível desfazer agora."; razao.hidden = false; }
  } else {
    btn.disabled = false;
    if (razao) razao.hidden = true;
  }
}

const UTILIZADOR_ATUAL_KEY = "cafe-utilizador-atual";

let sincronizando = false;

// Flushes the local queue to the server, one entry at a time. A queue entry
// is only deleted after a 2xx response: anything else (a real network
// failure especially) leaves it queued so a half-failed flush never drops a
// coffee. The server snapshot is only re-fetched once, after the whole
// queue has drained, so the optimistic count never shows +1 twice.
async function sincronizarFila() {
  if (sincronizando || !eu) return;
  sincronizando = true;
  try {
    let entradas;
    try { entradas = await idbTodos("fila"); }
    catch (erro) { console.error("Falha ao ler a fila offline:", erro); return; }

    for (const entrada of entradas) {
      try {
        await api("POST", "/cafe", { cliente_id: entrada.cliente_id, em: entrada.em });
        await idbApagar("fila", entrada.cliente_id);
      } catch (erro) {
        if (erro && erro.rede) break; // sem rede: as restantes falham na mesma, tenta-se noutro evento
        console.error("Falha ao sincronizar café da fila:", erro);
      }
    }

    const restantes = await idbTodos("fila").catch(() => entradas);
    await atualizarAvisoFila();
    await atualizarUltimoCafe();
    await atualizarEstadoDesfazer();

    if (!restantes.length) {
      try {
        eu = await api("GET", "/eu");
        await guardarInstantaneo("eu", eu.utilizador.id, eu);
        desenharEu();
        await atualizarUltimoCafe();
      } catch (erro) {
        console.error("Falha ao actualizar depois de sincronizar a fila:", erro);
      }
    }
  } finally {
    sincronizando = false;
  }
}

function mostrar(vista) {
  for (const [k, el] of Object.entries(vistas)) el.hidden = k !== vista;
  $("abas").hidden = vista === "entrada";
  for (const b of $("abas").querySelectorAll("button")) b.classList.toggle("activa", b.dataset.vista === vista);
}

// ---------- entrada ----------

let utilizadores = [];

async function carregarNomes() {
  utilizadores = await api("GET", "/utilizadores");
  $("filtro").hidden = utilizadores.length <= 12;
  desenharNomes();
}

function desenharNomes() {
  const f = $("filtro").value.trim().toLowerCase();
  const grelha = $("nomes");
  grelha.innerHTML = "";
  for (const u of utilizadores.filter((u) => u.nome.toLowerCase().includes(f))) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = u.nome;
    b.onclick = () => pedirPin(u);
    grelha.appendChild(b);
  }
  if (!utilizadores.length) grelha.innerHTML = '<p class="nota">Ainda não há ninguém. Cria a primeira conta.</p>';
}

let pinDe = null;
function pedirPin(u) {
  pinDe = u;
  $("pin-nome").textContent = u.nome;
  $("pin").value = "";
  $("passo-nomes").hidden = true;
  $("form-pin").hidden = false;
  $("pin").focus();
}

$("filtro").oninput = desenharNomes;
$("pin-voltar").onclick = () => { $("form-pin").hidden = true; $("passo-nomes").hidden = false; };
$("pin").oninput = async () => {
  const pin = $("pin").value.replace(/\D/g, "");
  $("pin").value = pin;
  if (pin.length !== 4) return;
  try {
    await api("POST", "/login", { utilizador_id: pinDe.id, pin });
    $("form-pin").hidden = true; $("passo-nomes").hidden = false;
    await entrar();
  } catch (e) {
    toast(e.message, true);
    $("pin").value = "";
    $("pin").focus();
  }
};
$("form-pin").onsubmit = (e) => e.preventDefault();

$("btn-novo").onclick = () => { $("passo-nomes").hidden = true; $("form-registo").hidden = false; $("reg-nome").focus(); };
$("reg-cancelar").onclick = () => { $("form-registo").hidden = true; $("passo-nomes").hidden = false; };
$("form-registo").onsubmit = async (e) => {
  e.preventDefault();
  if ($("reg-pin").value !== $("reg-pin2").value) return toast("Os dois PINs não são iguais.", true);
  try {
    await api("POST", "/registar", { nome: $("reg-nome").value, pin: $("reg-pin").value, cafes_dia: Number($("reg-cafes").value) });
    $("form-registo").reset();
    $("form-registo").hidden = true; $("passo-nomes").hidden = false;
    await entrar();
  } catch (err) { toast(err.message, true); }
};

// ---------- café ----------

async function entrar() {
  try {
    eu = await api("GET", "/eu");
    writeLocal(UTILIZADOR_ATUAL_KEY, String(eu.utilizador.id));
    await guardarInstantaneo("eu", eu.utilizador.id, eu);
    $("fotografia-aviso").hidden = true;
  } catch (erro) {
    if (!erro || !erro.rede) throw erro; // not an offline case: let the caller send us to the login screen
    const idGuardado = readLocal(UTILIZADOR_ATUAL_KEY);
    const foto = idGuardado ? await idbUm("instantaneos", `eu:${idGuardado}`).catch(() => null) : null;
    // No cached state, or a pre-MB WAY/pre-caixa snapshot without saldo_cent
    // or caixa: treat both as absent rather than risk drawing "Deves NaN €"
    // or a broken keeper line from an old shape.
    if (!foto || foto.dados.saldo_cent === undefined || foto.dados.caixa === undefined) throw erro;
    eu = foto.dados;
    $("fotografia-aviso").textContent = `Sem ligação. A mostrar o último estado conhecido (${horaCurta(foto.em)}).`;
    $("fotografia-aviso").hidden = false;
  }
  desenharEu();
  mostrar("cafe");
  await atualizarAvisoFila();
  await atualizarUltimoCafe();
  await atualizarEstadoDesfazer();
  atualizarEstadoNotif().catch((erro) => console.error("Falha ao atualizar estado das notificações:", erro));
  sincronizarFila().catch((erro) => console.error("Falha ao sincronizar a fila offline:", erro));
}

function textoStock(s) {
  const acaba = s.acaba_em ? `acabam ~${dataCurta(s.acaba_em)}` : "sem consumo previsto";
  const fim = s.chega_ao_fim_do_mes ? "chega ao fim do mês ✅" : "comprar antes do fim do mês ⚠️";
  const cab = s.baixo ? `⚠️ Stock baixo: ${plural(s.stock, "cápsula", "cápsulas")}` : `Faltam ${plural(s.stock, "cápsula", "cápsulas")}`;
  return `${cab}<small>${acaba} · ${fim} · ritmo ${s.ritmo_dia}/dia útil</small>`;
}

// Only fills #saldo-vista: #form-pagar is a sibling element, never touched
// here, so a redraw triggered by sync/online events never wipes a form the
// person has open (eg. mid-typing an amount).
function desenharEu() {
  $("mes-cafes").textContent = `${plural(eu.cafes, "cápsula", "cápsulas")} · ${euros(eu.valor_cent)}`;
  $("mes-est").textContent = `${plural(eu.estimativa_cafes, "cápsula", "cápsulas")} · ${euros(eu.estimativa_cent)}`;
  $("prev").textContent = `~${String(eu.utilizador.cafes_dia).replace(".", ",")}/dia`;

  $("saldo-frase").textContent = fraseSaldo(eu.saldo_cent);
  if (eu.caixa && eu.caixa.responsavel_id === eu.utilizador.id) {
    $("caixa-contigo").textContent = `Caixa contigo: ${euros(eu.caixa.dinheiro_cent)}`;
    $("caixa-contigo").hidden = false;
  } else {
    $("caixa-contigo").hidden = true;
  }
  if (eu.sugestao) {
    // sugestao só aponta para a caixa agora (secção 3.5); eu.caixa existe
    // sempre que sugestao existe, porque a sugestão exige responsável.
    $("saldo-sugestao").textContent = `Sugestão: paga ${euros(eu.sugestao.valor_cent)} à caixa (${eu.caixa.responsavel})`;
    $("saldo-sugestao").hidden = false;
    $("btn-pagar").classList.remove("discreto");
  } else {
    $("saldo-sugestao").hidden = true;
    $("btn-pagar").classList.add("discreto"); // no debt: paying ahead is still possible, just less urgent
  }

  const pc = eu.por_confirmar || [];
  if (pc.length) {
    $("por-confirmar-titulo").textContent = plural(pc.length, "pagamento por confirmar", "pagamentos por confirmar");
    const ul = $("por-confirmar-lista");
    ul.innerHTML = "";
    for (const t of pc) {
      const destino = t.para_caixa ? " → caixa" : "";
      const li = document.createElement("li");
      li.innerHTML = `<span>${t.pagador}${destino} · ${euros(t.valor_cent)} · ${diaRelativo(t.em)}</span>`
        + `<span><button type="button" class="ligacao" data-confirmar="${t.id}">✓</button> `
        + `<button type="button" class="ligacao" data-recusar="${t.id}">Não recebi</button></span>`;
      ul.appendChild(li);
    }
    $("por-confirmar").hidden = false;
  } else {
    $("por-confirmar").hidden = true;
  }

  const s = $("stock");
  s.className = "faixa" + (eu.stock.baixo ? " baixo" : "");
  s.innerHTML = textoStock(eu.stock);
}

// Refreshes /api/eu, re-snapshots it and redraws the card: shared by the
// payment form, and by confirming or refusing a "por confirmar" payment.
async function recarregarEu() {
  eu = await api("GET", "/eu");
  await guardarInstantaneo("eu", eu.utilizador.id, eu);
  desenharEu();
}

$("btn-cafe").onclick = async () => {
  const b = $("btn-cafe");
  b.disabled = true;
  const clienteId = crypto.randomUUID();
  const em = new Date().toISOString();
  try { await idbPut("fila", { cliente_id: clienteId, tipo: "cafe", em, criado_em: em }); }
  catch (erro) { console.error("Falha ao guardar café na fila offline:", erro); }

  eu.cafes += 1; eu.valor_cent += eu.preco_cent; eu.stock.stock -= 1; eu.saldo_cent -= eu.preco_cent; // optimista
  desenharEu();
  $("ultimo-cafe").textContent = textoUltimoCafe(eu.ultimo_cafe, [{ em }]); // a linha muda já, sem esperar pela fila
  await atualizarAvisoFila();
  await atualizarEstadoDesfazer();

  try {
    await api("POST", "/cafe", { cliente_id: clienteId, em });
    await idbApagar("fila", clienteId);
    eu.ultimo_cafe = em; // the queue entry is gone, so the line must not fall back to the previous coffee
    toast("Café marcado ☕");
    await sincronizarFila(); // flushes anything still queued, then refreshes /api/eu once
  } catch (erro) {
    if (erro && erro.rede) toast("Sem ligação. O café fica em fila e sincroniza sozinho.", true);
    else toast(erro.message, true);
  } finally {
    await atualizarAvisoFila();
    await atualizarUltimoCafe();
    await atualizarEstadoDesfazer();
    desenharEu();
    b.disabled = false;
  }
};

$("btn-desfazer").onclick = async () => {
  let fila;
  try { fila = await idbTodos("fila"); } catch { fila = []; }

  if (fila.length) {
    // Offline undo: drop the queued coffee locally. It never reached the
    // server, so there is nothing there to delete.
    const ultimo = fila.reduce((a, b) => (a.criado_em > b.criado_em ? a : b));
    try { await idbApagar("fila", ultimo.cliente_id); }
    catch (erro) { toast("Não foi possível desfazer: " + erro.message, true); return; }
    eu.cafes -= 1; eu.valor_cent -= eu.preco_cent; eu.stock.stock += 1; eu.saldo_cent += eu.preco_cent;
    desenharEu();
    await atualizarAvisoFila();
    await atualizarUltimoCafe();
    await atualizarEstadoDesfazer();
    toast("Café retirado da fila.");
    return;
  }

  if (!navigator.onLine) {
    await atualizarEstadoDesfazer();
    toast("Sem café por sincronizar e sem ligação: não é possível desfazer.", true);
    return;
  }

  if (!confirm("Apagar o teu último café?")) return;
  try {
    await api("DELETE", "/cafe/ultimo");
    toast("Café apagado.");
    await recarregarEu();
    await atualizarUltimoCafe();
  } catch (e) { toast(e.message, true); }
};

// ---------- pagamentos por MB WAY ----------

function escolherChip(valorCent) {
  for (const b of $("pagar-chips").querySelectorAll("button")) {
    b.classList.toggle("selecionada", Number(b.dataset.valor) === valorCent);
  }
  $("pagar-livre").value = (valorCent / 100).toFixed(2);
}

// Fetched fresh every time the form opens (never the login screen's global
// `utilizadores`, which entrar() never populates): the people list can
// change between sessions and this form always needs the current one.
async function abrirFormPagar() {
  if (!navigator.onLine) return toast("Precisas de rede para registar um pagamento.", true);
  let opcoes;
  try { opcoes = await opcoesDestino({ tipo: "normal" }); }
  catch (erro) { return toast(erro && erro.rede ? "Precisas de rede para registar um pagamento." : erro.message, true); }

  const sel = $("pagar-recebedor");
  sel.innerHTML = "";
  for (const o of opcoes) {
    const op = document.createElement("option");
    op.value = o.value; op.textContent = o.label;
    sel.appendChild(op);
  }
  // Caixa (Ana) vem primeiro na lista e é a escolha por omissão (spec 5.1).
  if (eu.caixa) sel.value = "caixa";

  const valores = [500, 1000, 2000];
  if (eu.sugestao && !valores.includes(eu.sugestao.valor_cent)) valores.push(eu.sugestao.valor_cent);
  valores.sort((a, b) => a - b);
  const chips = $("pagar-chips");
  chips.innerHTML = "";
  for (const v of valores) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.textContent = euros(v);
    b.dataset.valor = v;
    b.onclick = () => escolherChip(v);
    chips.appendChild(b);
  }
  $("pagar-livre").value = "";
  if (eu.sugestao) escolherChip(eu.sugestao.valor_cent);

  $("saldo-vista").hidden = true;
  $("form-pagar").hidden = false;
}

$("btn-pagar").onclick = () => { abrirFormPagar().catch((erro) => toast(erro.message, true)); };

$("pagar-livre").oninput = () => {
  for (const b of $("pagar-chips").querySelectorAll("button")) b.classList.remove("selecionada");
};

$("pagar-cancelar").onclick = () => {
  $("form-pagar").hidden = true;
  $("saldo-vista").hidden = false;
};

$("form-pagar").onsubmit = async (e) => {
  e.preventDefault();
  const valorCent = lerValorCent($("pagar-livre"));
  if (valorCent === null) return toast("Mete um valor entre 0,01 € e 1000 €.", true);
  const destino = $("pagar-recebedor").value;
  if (!destino) return toast("Escolhe a quem pagar.", true);
  const paraCaixa = destino === "caixa";
  // Quando eu sou a caixa (guardo-a) e pago para a caixa, ou pago a mim
  // próprio via caixa, o pagamento confirma-se sozinho e não há push
  // (spec 3.4/4.2): o toast não promete uma notificação que não é enviada.
  const souORecebedor = paraCaixa
    ? eu.caixa && eu.caixa.responsavel_id === eu.utilizador.id
    : Number(destino) === eu.utilizador.id;
  try {
    if (paraCaixa) await api("POST", "/transferencias", { recebedor_id: null, para_caixa: true, valor_cent: valorCent });
    else await api("POST", "/transferencias", { recebedor_id: Number(destino), para_caixa: false, valor_cent: valorCent });
    const nome = paraCaixa ? eu.caixa.responsavel : $("pagar-recebedor").selectedOptions[0].textContent;
    $("form-pagar").hidden = true;
    $("saldo-vista").hidden = false;
    toast(souORecebedor ? "Pagamento registado." : `Pagamento registado. Notificação enviada a ${nome}.`);
    await recarregarEu();
  } catch (erro) {
    if (erro && erro.rede) toast("Precisas de rede para registar um pagamento.", true);
    else toast(erro.message, true);
  }
};

// "por confirmar" ✓ / Não recebi, shown right on the Café card.
$("por-confirmar-lista").onclick = async (ev) => {
  const b = ev.target.closest("button[data-confirmar],button[data-recusar]");
  if (!b) return;
  const id = b.dataset.confirmar || b.dataset.recusar;
  try {
    await api("POST", `/transferencias/${id}/${b.dataset.confirmar ? "confirmar" : "anular"}`);
    toast("Pagamento actualizado.");
    await recarregarEu();
  } catch (erro) { toast(erro.message, true); }
};

$("btn-prev").onclick = async () => {
  const v = prompt("Quantos cafés bebes por dia, mais ou menos? (ex.: 1, 2, 0,5)", String(eu.utilizador.cafes_dia));
  if (v === null) return;
  const n = Number(v.replace(",", "."));
  if (!(n >= 0 && n <= 20)) return toast("Mete um número entre 0 e 20.", true);
  try { await api("PUT", "/eu/previsao", { cafes_dia: n }); eu = await api("GET", "/eu"); desenharEu(); toast("Previsão actualizada."); }
  catch (e) { toast(e.message, true); }
};

$("btn-pin").onclick = async () => {
  const actual = prompt("PIN actual:"); if (actual === null) return;
  const novo = prompt("PIN novo (4 dígitos):"); if (novo === null) return;
  const novo2 = prompt("Repete o PIN novo:"); if (novo2 === null) return;
  if (novo !== novo2) return toast("Os dois PINs não são iguais.", true);
  try { await api("PUT", "/eu/pin", { pin_actual: actual, pin_novo: novo }); toast("PIN alterado."); }
  catch (e) { toast(e.message, true); }
};

$("btn-sair").onclick = async () => {
  // Try to flush first: fila holds only this person's own queued coffees
  // (not third-party data like instantaneos), so wiping it unread would
  // throw away work the app exists to protect.
  await sincronizarFila().catch((erro) => console.error("Falha ao sincronizar antes de sair:", erro));
  let fila;
  try { fila = await idbTodos("fila"); } catch { fila = []; }
  if (fila.length) {
    // Could not drain (no network): the queue cannot simply survive the
    // logout either, since its record shape carries no user id and the
    // next person to log in on this device would flush it under their own
    // session. Name what is about to be lost and make it a real choice.
    const mensagem = `Tens ${plural(fila.length, "café", "cafés")} por sincronizar. Se saíres agora, ${fila.length === 1 ? "perde-se." : "perdem-se."}`;
    if (!confirm(mensagem)) return; // stays logged in, queue untouched
  }

  await api("POST", "/logout").catch(() => {});
  eu = null;
  escritorio = null;
  dinheiro = null;
  removeLocal(UTILIZADOR_ATUAL_KEY);
  await apagarDB(); // /api/escritorio holds everyone's data: nothing offline survives a shared device's logout
  await carregarNomes();
  mostrar("entrada");
};

// ---------- histórico ----------

// Fetches one month of the person's own coffees. The raw response is stored
// as the snapshot before the offline queue is merged in, so a queued coffee
// is never baked into a snapshot the server will count again after sync.
async function carregarHistorico(mes) {
  const idAtual = eu ? eu.utilizador.id : readLocal(UTILIZADOR_ATUAL_KEY);
  let alvo = mes || mesLocal(new Date());
  try {
    const dados = await api("GET", "/historico" + (mes ? `?mes=${mes}` : ""));
    alvo = dados.mes;
    if (idAtual) await guardarInstantaneo("historico", idAtual, dados, dados.mes);
    historico = dados;
    $("hist-aviso").hidden = true;
  } catch (erro) {
    if (!erro || !erro.rede) throw erro;
    const foto = idAtual ? await idbUm("instantaneos", `historico:${idAtual}:${alvo}`).catch(() => null) : null;
    if (foto) {
      historico = foto.dados;
      $("hist-aviso").textContent = `Sem ligação. A mostrar o último estado conhecido (${horaCurta(foto.em)}).`;
    } else {
      historico = { mes: alvo, hoje: diaLocal(new Date()), dias: [] };
      $("hist-aviso").textContent = "Sem ligação e sem histórico guardado para este mês.";
    }
    $("hist-aviso").hidden = false;
  }

  histPorDia = new Map();
  for (const d of historico.dias) histPorDia.set(d.dia, { n: d.n, horas: d.horas.map((hora) => ({ hora, fila: false })) });

  // Queued coffees count as marked (H7): each one lands on its local day,
  // converted here in the browser because the queue only exists here.
  let fila;
  try { fila = await idbTodos("fila"); } catch { fila = []; }
  for (const entrada of fila) {
    const d = new Date(entrada.em);
    if (mesLocal(d) !== historico.mes) continue;
    const dia = diaLocal(d);
    if (!histPorDia.has(dia)) histPorDia.set(dia, { n: 0, horas: [] });
    const info = histPorDia.get(dia);
    info.n += 1;
    info.horas.push({ hora: horaCurta(entrada.em), fila: true });
    info.horas.sort((a, b) => a.hora.localeCompare(b.hora));
  }

  // Current month opens on today (the "já marquei?" answer with zero taps);
  // any other month opens on its most recent day with coffees, if any.
  if (historico.mes === historico.hoje.slice(0, 7)) histDia = historico.hoje;
  else histDia = [...histPorDia.keys()].sort().pop() || null;
  desenharHistorico();
}

function desenharHistorico() {
  const h = historico;
  const [ano, mes] = h.mes.split("-").map(Number);
  $("hist-mes").textContent = nomeMes(h.mes);
  $("hist-seguinte").disabled = h.mes >= h.hoje.slice(0, 7);

  const grelha = $("hist-grelha");
  grelha.innerHTML = "";
  for (const s of ["S", "T", "Q", "Q", "S", "S", "D"]) {
    const c = document.createElement("span");
    c.className = "semana";
    c.textContent = s;
    grelha.appendChild(c);
  }
  const vazios = (new Date(ano, mes - 1, 1).getDay() + 6) % 7; // semana a começar à segunda
  for (let i = 0; i < vazios; i++) grelha.appendChild(document.createElement("span"));
  const nDias = new Date(ano, mes, 0).getDate();
  for (let n = 1; n <= nDias; n++) {
    const dia = `${h.mes}-${String(n).padStart(2, "0")}`;
    const info = histPorDia.get(dia);
    const semana = new Date(ano, mes - 1, n).getDay();
    const b = document.createElement("button");
    b.type = "button";
    b.className = "dia";
    b.dataset.dia = dia;
    if (semana === 0 || semana === 6) b.classList.add("fds");
    if (dia === h.hoje) b.classList.add("hoje");
    if (dia > h.hoje) { b.classList.add("futuro"); b.disabled = true; }
    if (dia === histDia) b.classList.add("escolhido");
    if (info && info.horas.some((x) => x.fila)) b.classList.add("fila");
    b.innerHTML = `<small>${n}</small><span class="n">${info ? info.n : ""}</span>`;
    grelha.appendChild(b);
  }

  const detalhe = $("hist-detalhe");
  if (!histDia) { detalhe.textContent = "Toca num dia"; return; }
  const [a, m, d] = histDia.split("-").map(Number);
  const nome = DIAS_SEMANA[new Date(a, m - 1, d).getDay()];
  const cabecalho = `${nome.charAt(0).toUpperCase()}${nome.slice(1)} ${d}`;
  const info = histPorDia.get(histDia);
  if (!info || !info.n) detalhe.textContent = `${cabecalho} · nenhum café`;
  else {
    const horas = info.horas.map((x) => x.hora + (x.fila ? " (por sincronizar)" : ""));
    detalhe.textContent = `${cabecalho} · ${plural(info.n, "café", "cafés")} · ${horas.join(" · ")}`;
  }
}

function escolherDia(dia) {
  histDia = dia;
  desenharHistorico();
}

$("hist-grelha").onclick = (ev) => {
  const b = ev.target.closest("button.dia");
  if (!b || b.disabled) return;
  escolherDia(b.dataset.dia);
};
$("hist-anterior").onclick = () => {
  carregarHistorico(mesVizinho(historico.mes, -1)).catch((e) => toast(e.message, true));
};
$("hist-seguinte").onclick = () => {
  if (historico.mes >= historico.hoje.slice(0, 7)) return;
  carregarHistorico(mesVizinho(historico.mes, 1)).catch((e) => toast(e.message, true));
};

// ---------- histórico: separador Dinheiro ----------

function selecionarHistVista(v) {
  $("hist-cafes").hidden = v !== "cafes";
  $("hist-dinheiro").hidden = v !== "dinheiro";
  for (const b of $("hist-selector").querySelectorAll("button")) b.classList.toggle("activa", b.dataset.histVista === v);
  if (v === "dinheiro" && !dinheiro) carregarDinheiro().catch((e) => toast(e.message, true));
}

$("hist-selector").onclick = (ev) => {
  const b = ev.target.closest("button[data-hist-vista]");
  if (!b) return;
  selecionarHistVista(b.dataset.histVista);
};

async function carregarDinheiro() {
  const idAtual = eu ? eu.utilizador.id : readLocal(UTILIZADOR_ATUAL_KEY);
  try {
    dinheiro = await api("GET", "/movimentos");
    if (idAtual) await guardarInstantaneo("movimentos", idAtual, dinheiro);
    $("dinheiro-aviso").hidden = true;
  } catch (erro) {
    if (!erro || !erro.rede) throw erro;
    const foto = idAtual ? await idbUm("instantaneos", `movimentos:${idAtual}`).catch(() => null) : null;
    if (!foto) throw erro;
    dinheiro = foto.dados;
    $("dinheiro-aviso").textContent = `Sem ligação. A mostrar o último estado conhecido (${horaCurta(foto.em)}).`;
    $("dinheiro-aviso").hidden = false;
  }
  desenharDinheiro();
}

function desenharDinheiro() {
  const d = dinheiro;
  $("dinheiro-saldo").textContent = fraseSaldo(d.saldo_cent);

  // Transfers and purchases interleaved by date, most recent first (section
  // 4.5 hands over three separate arrays; merging them into one feed here is
  // this client's own choice, not something the spec spells out).
  const itens = [];
  for (const t of d.transferencias) itens.push({ tipo: "transferencia", em: t.em, dado: t });
  for (const c of d.compras) itens.push({ tipo: "compra", em: c.em, dado: c });
  itens.sort((a, b) => (a.em < b.em ? 1 : -1));

  const ul = $("dinheiro-lista");
  ul.innerHTML = "";
  for (const it of itens) {
    const li = document.createElement("li");
    if (it.tipo === "compra") {
      li.innerHTML = `<span>Compraste ${plural(it.dado.capsulas, "cápsula", "cápsulas")} · ${euros(it.dado.custo_cent)} · ${dataCurta(it.dado.em)}</span>`;
    } else {
      const t = it.dado;
      const paraCaixa = t.outro === "Caixa";
      const texto = t.sentido === "paguei"
        ? `Pagaste ${euros(t.valor_cent)} ${paraCaixa ? "à caixa" : `a ${t.outro}`}`
        : `Recebeste ${euros(t.valor_cent)} ${paraCaixa ? "da caixa" : `de ${t.outro}`}`;
      const botoes = [];
      if (t.anulada_em) {
        li.className = "anulada";
        botoes.push(`<span class="nota">${t.anulada_por === eu.utilizador.id ? "anulado por ti" : `anulado por ${t.outro}`}</span>`);
      } else {
        if (!t.confirmada_em) {
          if (t.sentido === "paguei") botoes.push(`<button type="button" class="ligacao" data-mov-anular="${t.id}">Anular</button>`);
          else {
            botoes.push(`<button type="button" class="ligacao" data-mov-confirmar="${t.id}">✓</button>`);
            botoes.push(`<button type="button" class="ligacao" data-mov-anular="${t.id}">Não recebi</button>`);
          }
        }
        // Editar: só o lado de quem paga, enquanto activo (confirmado ou não).
        if (t.sentido === "paguei") botoes.push(`<button type="button" class="ligacao" data-mov-editar="${t.id}">Editar</button>`);
      }
      const editado = t.editada ? ` <button type="button" class="ligacao" data-mov-historico="${t.id}">editado</button>` : "";
      li.innerHTML = `<span>${texto} · ${dataCurta(t.em)}${editado}</span><span>${botoes.join(" ")}</span>`
        + `<div class="fp-inline" hidden></div><ul class="historico-lista" hidden></ul>`;
    }
    ul.appendChild(li);
  }

  for (const m of d.meses) {
    const li = document.createElement("li");
    li.className = "nota";
    li.textContent = `${nomeMes(m.mes)}: ${plural(m.cafes, "café", "cafés")}, ${euros(-m.valor_cent)}`;
    ul.appendChild(li);
  }
}

$("dinheiro-lista").onclick = async (ev) => {
  const li = ev.target.closest("li");
  if (!li) return;
  const bAcao = ev.target.closest("button[data-mov-confirmar],button[data-mov-anular]");
  if (bAcao) {
    const id = bAcao.dataset.movConfirmar || bAcao.dataset.movAnular;
    try {
      await api("POST", `/transferencias/${id}/${bAcao.dataset.movConfirmar ? "confirmar" : "anular"}`);
      toast("Pagamento actualizado.");
      await carregarDinheiro();
      if (eu) await recarregarEu(); // the balance shown on the Café card moves too
    } catch (erro) { toast(erro.message, true); }
    return;
  }
  const bHistorico = ev.target.closest("button[data-mov-historico]");
  if (bHistorico) {
    await expandirHistorico(li.querySelector(".historico-lista"), "transferencia", bHistorico.dataset.movHistorico);
    return;
  }
  const bEditar = ev.target.closest("button[data-mov-editar]");
  if (bEditar) {
    const t = dinheiro.transferencias.find((x) => String(x.id) === bEditar.dataset.movEditar);
    if (!t) return;
    const comCaixa = !t.de_caixa;
    let opcoes;
    try { opcoes = await opcoesDestino({ tipo: comCaixa ? "normal" : "de_caixa" }); }
    catch (erro) { toast(msgRede(erro, "editar um pagamento"), true); return; }
    const destinoInicial = t.para_caixa ? "caixa" : String(t.outro_id);
    const container = li.querySelector(".fp-inline");
    montarFormPagamentoInline(container, {
      valorInicial: t.valor_cent,
      opcoesDestino: opcoes,
      destinoInicial,
      destinoInicialLabel: t.para_caixa ? "Caixa" : t.outro,
      onGuardar: async (valorCent, destinoValue) => {
        try {
          const mudou = await guardarEdicaoPagamento(t.id, valorCent, destinoValue, { valorCent: t.valor_cent, destino: destinoInicial }, comCaixa);
          container.hidden = true; container.innerHTML = "";
          if (mudou) toast("Pagamento actualizado.");
          await carregarDinheiro();
          if (eu) await recarregarEu();
        } catch (erro) { toast(msgRede(erro, "editar um pagamento"), true); }
      },
    });
  }
};

// ---------- escritório ----------

async function carregarEscritorio(mes) {
  try {
    escritorio = await api("GET", "/escritorio" + (mes ? `?mes=${mes}` : ""));
    const idAtual = eu ? eu.utilizador.id : readLocal(UTILIZADOR_ATUAL_KEY);
    if (idAtual) await guardarInstantaneo("escritorio", idAtual, escritorio);
    $("esc-fotografia-aviso").hidden = true;
  } catch (erro) {
    if (!erro || !erro.rede) throw erro;
    const idAtual = eu ? eu.utilizador.id : readLocal(UTILIZADOR_ATUAL_KEY);
    const foto = idAtual ? await idbUm("instantaneos", `escritorio:${idAtual}`).catch(() => null) : null;
    // A snapshot without `caixa` is from before this screen existed (pote,
    // not caixa): treat it as absent rather than draw a broken caixa line.
    if (!foto || foto.dados.caixa === undefined) throw erro;
    escritorio = foto.dados;
    $("esc-fotografia-aviso").textContent = `Sem ligação. A mostrar o último estado conhecido (${horaCurta(foto.em)}).`;
    $("esc-fotografia-aviso").hidden = false;
  }
  desenharEscritorio();
  // Pagamentos e Definições precisam sempre de rede (nunca vêm da
  // fotografia); cada um mostra o seu próprio aviso se falhar, sem
  // impedir o resto do ecrã de aparecer.
  await carregarPagamentos();
  await carregarConfig();
}

function saldoCurto(cent) {
  if (cent === 0) return euros(0);
  return (cent < 0 ? "-" : "+") + euros(Math.abs(cent));
}

function desenharEscritorio() {
  const e = escritorio;
  const sel = $("sel-mes");
  sel.innerHTML = "";
  for (const m of e.meses) {
    const o = document.createElement("option");
    o.value = m; o.textContent = nomeMes(m) + (m === e.mes_actual ? " (actual)" : "");
    sel.appendChild(o);
  }
  sel.value = e.mes;
  $("esc-total").textContent = `${plural(e.total_cafes, "cápsula", "cápsulas")} · ${euros(e.total_cent)}`;
  // "Caixa com Ana: 7,71 € em dinheiro · 18,75 € por receber · fundo 13,91 €",
  // ou o aviso sem responsável (spec 5.3).
  $("esc-caixa").textContent = (!e.caixa || e.caixa.responsavel_id == null)
    ? "Ninguém guarda a caixa. Escolhe nas Definições."
    : `Caixa com ${e.caixa.responsavel}: ${euros(e.caixa.dinheiro_cent)} em dinheiro · ${euros(e.caixa.por_receber_cent)} por receber · fundo ${euros(e.caixa.fundo_cent)}`;

  const souGuarda = e.caixa && e.caixa.responsavel_id === e.eu;
  const tb = $("tabela").querySelector("tbody");
  tb.innerHTML = "";
  for (const p of e.pessoas) {
    const tr = document.createElement("tr");
    tr.dataset.pessoaId = p.id;
    const classeSaldo = p.saldo_cent < 0 ? "saldo-neg" : p.saldo_cent > 0 ? "saldo-pos" : "";
    const reembolsar = souGuarda && p.saldo_cent > 0
      ? ` <button type="button" class="ligacao btn-reembolsar" data-reembolsar="${p.id}">Reembolsar</button>` : "";
    tr.innerHTML = `<td>${p.nome}</td><td class="num">${p.cafes}</td><td class="num">${euros(p.valor_cent)}</td>`
      + `<td class="num ${classeSaldo}">${saldoCurto(p.saldo_cent)}${reembolsar}</td>`;
    tb.appendChild(tr);
  }

  const s = $("esc-stock");
  s.className = "faixa" + (e.stock.baixo ? " baixo" : "");
  s.innerHTML = textoStock(e.stock);

  $("compra-paga-com").querySelector('option[value="caixa"]').disabled = !e.caixa || e.caixa.responsavel_id == null;
  if ($("compra-paga-com").value === "caixa" && $("compra-paga-com").querySelector('option[value="caixa"]').disabled) {
    $("compra-paga-com").value = "bolso";
  }

  const ul = $("compras");
  ul.innerHTML = "";
  for (const c of e.compras) {
    const acoes = [];
    if (c.pode_editar) acoes.push(`<button type="button" class="ligacao" data-editar-compra="${c.id}">Editar</button>`);
    if (c.utilizador_id === e.eu) acoes.push(`<button type="button" class="ligacao" data-apagar="${c.id}">apagar</button>`);
    const li = document.createElement("li");
    li.dataset.id = c.id;
    const origem = c.custo_cent === 0 ? "oferta" : c.paga_pela_caixa ? "pela caixa" : `do bolso de ${c.nome || "?"}`;
    const estimado = c.custo_estimado ? ' <span class="nota">custo estimado</span>' : "";
    const editado = c.editada ? ` <button type="button" class="ligacao" data-historico-compra="${c.id}">editado</button>` : "";
    li.innerHTML = `<span>+${c.capsulas} · ${euros(c.custo_cent)} · ${origem} · ${dataCurta(c.em)}`
      + `${c.nota ? " · " + c.nota : ""}${estimado}${editado}</span><span>${acoes.join(" ")}</span>`
      + `<div class="fp-inline" hidden></div><ul class="historico-lista" hidden></ul>`;
    ul.appendChild(li);
  }
  if (!e.compras.length) ul.innerHTML = '<li class="nota">Ainda não há entradas. Regista as cápsulas iniciais.</li>';
}

$("sel-mes").onchange = () => carregarEscritorio($("sel-mes").value);

// ---------- escritório: pagamentos (secção nova, GET /api/transferencias) ----------

let pagamentos = null; // resposta de /api/transferencias (todos os pagamentos, para o Escritório)

function linhaPagamento(t) {
  const estado = t.anulada_em ? "anulado" : t.confirmada_em ? "confirmado" : "por confirmar";
  const souRecebedor = (t.para_caixa && eu && eu.caixa && eu.caixa.responsavel_id === eu.utilizador.id)
    || (eu && t.recebedor_id === eu.utilizador.id);
  const botoes = [];
  if (t.pode_confirmar) botoes.push(`<button type="button" class="ligacao" data-pg-confirmar="${t.id}">✓</button>`);
  if (t.pode_anular) botoes.push(`<button type="button" class="ligacao" data-pg-anular="${t.id}">${souRecebedor ? "Não recebi" : "Anular"}</button>`);
  if (t.pode_editar) botoes.push(`<button type="button" class="ligacao" data-pg-editar="${t.id}">Editar</button>`);
  const editado = t.editada ? ` <button type="button" class="ligacao" data-pg-historico="${t.id}">editado</button>` : "";
  return `<li data-id="${t.id}"><span>${t.pagador} → ${t.recebedor} · ${euros(t.valor_cent)} · ${dataCurta(t.em)} · ${estado}${editado}</span>`
    + `<span>${botoes.join(" ")}</span><div class="fp-inline" hidden></div><ul class="historico-lista" hidden></ul></li>`;
}

function desenharPagamentos() {
  $("pagamentos-lista").innerHTML = (pagamentos || []).map(linhaPagamento).join("") || '<li class="nota">Ainda não há pagamentos.</li>';
}

// Pagamentos precisa sempre de rede: falha em silêncio no ecrã (nunca lança),
// para nunca impedir o resto do Escritório de aparecer offline.
async function carregarPagamentos() {
  const aviso = $("pagamentos-aviso");
  try {
    const r = await api("GET", "/transferencias");
    pagamentos = r.transferencias;
    aviso.hidden = true;
    desenharPagamentos();
  } catch (erro) {
    pagamentos = null;
    aviso.textContent = erro && erro.rede ? "Precisas de rede para ver os pagamentos." : erro.message;
    aviso.hidden = false;
    $("pagamentos-lista").innerHTML = "";
  }
}

// ---------- escritório: definições (preço, limiar, responsável pela caixa) ----------

let config = null; // resposta de /api/config

async function carregarConfig() {
  const aviso = $("cfg-aviso");
  try {
    config = await api("GET", "/config");
    aviso.hidden = true;
    const lista = await listaUtilizadores().catch(() => []);
    const sel = $("cfg-caixa-responsavel");
    sel.innerHTML = '<option value="">ninguém</option>';
    for (const u of lista) {
      const op = document.createElement("option");
      op.value = u.id; op.textContent = u.nome;
      sel.appendChild(op);
    }
    sel.value = config.caixa_responsavel_id != null ? String(config.caixa_responsavel_id) : "";
    $("cfg-preco").value = (config.preco_cent / 100).toFixed(2);
    $("cfg-limiar").value = config.stock_baixo;
  } catch (erro) {
    config = null;
    aviso.textContent = erro && erro.rede ? "Precisas de rede para ver as definições." : erro.message;
    aviso.hidden = false;
  }
}

$("cfg-ver-alteracoes").onclick = () => {
  expandirHistorico($("cfg-historico-lista"), "config").catch((erro) => toast(erro.message, true));
};

// ---------- escritório: um único despachante de cliques para a vista inteira ----------
//
// Apagar/Editar numa compra, Editar/Confirmar/Anular/histórico num
// pagamento, e Reembolsar numa pessoa: uma delegação só, em vez de vários
// listeners a competir pelos mesmos elementos.
$("vista-escritorio").onclick = async (ev) => {
  const bApagar = ev.target.closest("button[data-apagar]");
  if (bApagar) {
    try {
      if (!confirm("Apagar esta entrada de cápsulas?")) return;
      await api("DELETE", `/compras/${bApagar.dataset.apagar}`);
      toast("Entrada apagada.");
      await carregarEscritorio(escritorio.mes);
    } catch (erro) { toast(erro.message, true); }
    return;
  }

  const bHistoricoCompra = ev.target.closest("button[data-historico-compra]");
  if (bHistoricoCompra) {
    const li = bHistoricoCompra.closest("li");
    await expandirHistorico(li.querySelector(".historico-lista"), "compra", bHistoricoCompra.dataset.historicoCompra);
    return;
  }

  const bEditarCompra = ev.target.closest("button[data-editar-compra]");
  if (bEditarCompra) {
    const c = escritorio.compras.find((x) => String(x.id) === bEditarCompra.dataset.editarCompra);
    if (c) abrirEdicaoCompra(bEditarCompra.closest("li"), c);
    return;
  }

  const bReembolsar = ev.target.closest("button[data-reembolsar]");
  if (bReembolsar) {
    abrirReembolso(bReembolsar);
    return;
  }

  const bPgConfirmar = ev.target.closest("button[data-pg-confirmar]");
  const bPgAnular = ev.target.closest("button[data-pg-anular]");
  if (bPgConfirmar || bPgAnular) {
    const id = bPgConfirmar ? bPgConfirmar.dataset.pgConfirmar : bPgAnular.dataset.pgAnular;
    try {
      await api("POST", `/transferencias/${id}/${bPgConfirmar ? "confirmar" : "anular"}`);
      toast("Pagamento actualizado.");
      await carregarPagamentos();
      if (eu) await recarregarEu();
    } catch (erro) { toast(erro.message, true); }
    return;
  }

  const bPgHistorico = ev.target.closest("button[data-pg-historico]");
  if (bPgHistorico) {
    const li = bPgHistorico.closest("li");
    await expandirHistorico(li.querySelector(".historico-lista"), "transferencia", bPgHistorico.dataset.pgHistorico);
    return;
  }

  const bPgEditar = ev.target.closest("button[data-pg-editar]");
  if (bPgEditar) {
    const t = (pagamentos || []).find((x) => String(x.id) === bPgEditar.dataset.pgEditar);
    if (t) await abrirEdicaoPagamento(bPgEditar.closest("li"), t);
  }
};

async function abrirEdicaoPagamento(li, t) {
  const comCaixa = !t.de_caixa;
  let opcoes;
  try { opcoes = await opcoesDestino({ tipo: comCaixa ? "normal" : "de_caixa" }); }
  catch (erro) { toast(msgRede(erro, "editar um pagamento"), true); return; }
  const destinoInicial = t.para_caixa ? "caixa" : String(t.recebedor_id);
  const container = li.querySelector(".fp-inline");
  montarFormPagamentoInline(container, {
    valorInicial: t.valor_cent,
    opcoesDestino: opcoes,
    destinoInicial,
    destinoInicialLabel: t.para_caixa ? "Caixa" : t.recebedor,
    onGuardar: async (valorCent, destinoValue) => {
      try {
        const mudou = await guardarEdicaoPagamento(t.id, valorCent, destinoValue, { valorCent: t.valor_cent, destino: destinoInicial }, comCaixa);
        container.hidden = true; container.innerHTML = "";
        if (mudou) toast("Pagamento actualizado.");
        await carregarPagamentos();
        if (eu) await recarregarEu();
      } catch (erro) { toast(msgRede(erro, "editar um pagamento"), true); }
    },
  });
}

// Reembolsar: uma linha nova por baixo da pessoa, com o valor sugerido igual
// ao saldo dela e sem selector de destino (o destino já é essa pessoa).
function abrirReembolso(botao) {
  const pessoaId = Number(botao.dataset.reembolsar);
  const pessoa = escritorio.pessoas.find((p) => p.id === pessoaId);
  if (!pessoa) return;
  const trPessoa = botao.closest("tr");
  const proxima = trPessoa.nextElementSibling;
  if (proxima && proxima.classList.contains("linha-reembolso")) { proxima.remove(); return; }
  const tr = document.createElement("tr");
  tr.className = "linha-reembolso";
  const td = document.createElement("td");
  td.colSpan = 4;
  tr.appendChild(td);
  trPessoa.after(tr);
  montarFormPagamentoInline(td, {
    valorInicial: pessoa.saldo_cent,
    opcoesDestino: null,
    onGuardar: async (valorCent) => {
      try {
        await api("POST", "/transferencias", { recebedor_id: pessoaId, de_caixa: true, valor_cent: valorCent });
        toast("Reembolso registado.");
        tr.remove();
        await carregarEscritorio(escritorio.mes);
        if (eu) await recarregarEu();
      } catch (erro) { toast(msgRede(erro, "reembolsar"), true); }
    },
    onCancelar: () => tr.remove(),
  });
}

function abrirEdicaoCompra(li, c) {
  const container = li.querySelector(".fp-inline");
  const pagaComInicial = c.custo_cent === 0 ? "oferta" : c.paga_pela_caixa ? "caixa" : "bolso";
  const pagaComOpcoes = [];
  // "Caixa" fica na lista se há responsável hoje, ou se esta compra já foi
  // paga pela caixa dantes (responsável entretanto removido): sem isto, o
  // select cairia na primeira opção e Guardar mudaria "paga com" sem ninguém
  // ter pedido.
  if ((eu && eu.caixa) || pagaComInicial === "caixa") pagaComOpcoes.push({ value: "caixa", label: "Caixa" });
  pagaComOpcoes.push({ value: "bolso", label: "Do meu bolso" }, { value: "oferta", label: "Oferta" });
  container.innerHTML = `<form class="linha">
    <label>Cápsulas <input type="number" min="1" max="10000" class="ec-capsulas" required></label>
    <label>Custo (€) <input type="number" step="0.01" min="0" max="10000" class="ec-custo" required></label>
    <label>Paga com <select class="ec-paga-com"></select></label>
    <button type="submit">Guardar</button>
    <button type="button" class="ligacao ec-cancelar">Cancelar</button>
  </form>`;
  const form = container.querySelector("form");
  const capsulasInput = form.querySelector(".ec-capsulas");
  const custoInput = form.querySelector(".ec-custo");
  const sel = form.querySelector(".ec-paga-com");
  capsulasInput.value = c.capsulas;
  custoInput.value = (c.custo_cent / 100).toFixed(2);
  for (const o of pagaComOpcoes) {
    const op = document.createElement("option");
    op.value = o.value; op.textContent = o.label;
    sel.appendChild(op);
  }
  sel.value = pagaComInicial;
  custoInput.disabled = pagaComInicial === "oferta";
  sel.onchange = () => {
    if (sel.value === "oferta") { custoInput.value = "0.00"; custoInput.disabled = true; }
    else { custoInput.disabled = false; if (custoInput.value === "0.00") custoInput.value = ""; }
  };
  form.querySelector(".ec-cancelar").onclick = () => { container.hidden = true; container.innerHTML = ""; };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const capsulas = Number(capsulasInput.value);
    const pagaCom = sel.value;
    const custoCent = pagaCom === "oferta" ? 0 : Math.round(Number(custoInput.value.replace(",", ".")) * 100);
    if (pagaCom !== "oferta" && !(custoCent >= 1 && custoCent <= 1000000)) return toast("Mete um custo entre 0,01 € e 10000 €.", true);
    const corpo = {};
    if (capsulas !== c.capsulas) corpo.capsulas = capsulas;
    if (custoCent !== c.custo_cent) corpo.custo_cent = custoCent;
    if (pagaCom !== "oferta" && (pagaCom === "caixa") !== !!c.paga_pela_caixa) corpo.paga_pela_caixa = pagaCom === "caixa";
    if (!Object.keys(corpo).length) { toast("Nada para guardar."); return; }
    try {
      await api("PATCH", `/compras/${c.id}`, corpo);
      toast("Entrada actualizada.");
      container.hidden = true; container.innerHTML = "";
      await carregarEscritorio(escritorio.mes);
    } catch (erro) { toast(msgRede(erro, "editar uma entrada"), true); }
  };
  container.hidden = false;
}

$("compra-paga-com").onchange = () => {
  const custo = $("compra-custo");
  if ($("compra-paga-com").value === "oferta") { custo.value = "0.00"; custo.disabled = true; }
  else { custo.disabled = false; if (custo.value === "0.00") custo.value = ""; }
};

$("form-compra").onsubmit = async (e) => {
  e.preventDefault();
  const pagaCom = $("compra-paga-com").value;
  const custoCent = pagaCom === "oferta" ? 0 : Math.round(Number($("compra-custo").value.replace(",", ".")) * 100);
  if (pagaCom !== "oferta" && !(custoCent >= 1 && custoCent <= 1000000)) return toast("Mete um custo entre 0,01 € e 10000 €.", true);
  try {
    await api("POST", "/compras", {
      capsulas: Number($("compra-n").value),
      custo_cent: custoCent,
      paga_pela_caixa: pagaCom === "caixa",
      nota: $("compra-nota").value || null,
    });
    $("form-compra").reset();
    $("compra-paga-com").value = "bolso";
    $("compra-custo").disabled = false;
    toast("Entrada registada.");
    await carregarEscritorio(escritorio.mes);
  } catch (err) { toast(err.message, true); }
};

$("form-config").onsubmit = async (e) => {
  e.preventDefault();
  const precoCent = Math.round(Number($("cfg-preco").value.replace(",", ".")) * 100);
  if (!(precoCent >= 1 && precoCent <= 10000)) return toast("Mete um preço entre 0,01 € e 100 €.", true);
  const respId = $("cfg-caixa-responsavel").value;
  try {
    await api("PUT", "/config", {
      preco_cent: precoCent,
      stock_baixo: Number($("cfg-limiar").value),
      caixa_responsavel_id: respId === "" ? null : Number(respId),
    });
    toast("Definições guardadas.");
    // O histórico de config já aberto fica desactualizado depois de gravar:
    // esquece a versão em cache para o próximo "ver alterações" ir buscá-la.
    const histCfg = $("cfg-historico-lista");
    delete histCfg.dataset.carregado;
    histCfg.hidden = true;
    await carregarEscritorio(escritorio.mes);
    if (eu) await recarregarEu(); // a linha da caixa e as opções de pagar mudam com o responsável
  } catch (err) { toast(msgRede(err, "guardar as definições"), true); }
};

// ---------- abas e arranque ----------

$("abas").onclick = async (ev) => {
  const b = ev.target.closest("button[data-vista]");
  if (!b) return;
  try {
    if (b.dataset.vista === "cafe") await entrar();
    else if (b.dataset.vista === "historico") {
      await carregarHistorico();
      dinheiro = null; // force a fresh /api/movimentos next time Dinheiro is opened
      selecionarHistVista("cafes");
      mostrar("historico");
    } else {
      await carregarEscritorio();
      mostrar("escritorio");
      atualizarEstadoNotif().catch((erro) => console.error("Falha ao atualizar estado das notificações:", erro));
    }
  } catch (e) { toast(e.message, true); }
};

(async () => {
  try { await entrar(); }
  catch { await carregarNomes(); mostrar("entrada"); }
})();

// The fila flushes on the 'online' event and whenever the app becomes
// visible again (eg. switching back to a backgrounded tab), on top of the
// flush already triggered right after login inside entrar().
window.addEventListener("online", () => {
  sincronizarFila().catch((erro) => console.error("Falha ao sincronizar a fila offline:", erro));
  atualizarEstadoDesfazer();
});
window.addEventListener("offline", () => {
  atualizarEstadoDesfazer();
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  sincronizarFila().catch((erro) => console.error("Falha ao sincronizar a fila offline:", erro));
  atualizarEstadoDesfazer();
});

// ---------- service worker (app shell offline) ----------

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
  // A new service worker taking control means a fresh deploy just activated;
  // reload once so an already-open tab picks up the new shell right away
  // instead of running on stale app.js until the next manual refresh.
  let recarregado = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (recarregado) return;
    recarregado = true;
    location.reload();
  });
}

// ---------- install prompt (PWA) ----------

const IOS_HINT_DISMISSED_KEY = "cafe-ios-install-hint-dismissed";

function readLocal(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function writeLocal(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode or blocked storage: no memory, no harm */ }
}
function removeLocal(key) {
  try { localStorage.removeItem(key); } catch { /* private mode or blocked storage: no memory, no harm */ }
}

function isIOS() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}
function isSafari() {
  const ua = navigator.userAgent;
  return /safari/i.test(ua) && !/crios|fxios|edgios|opios|android/i.test(ua);
}
function isStandalone() {
  return window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
}

let installEvent = null;

function showInstallBanner(text, withButton) {
  $("instalar-texto").textContent = text;
  $("instalar-btn").hidden = !withButton;
  $("instalar").hidden = false;
  document.body.classList.add("com-faixa-instalar");
}
function hideInstallBanner() {
  $("instalar").hidden = true;
  document.body.classList.remove("com-faixa-instalar");
}

$("instalar-fechar").onclick = () => {
  hideInstallBanner();
  if (isIOS()) writeLocal(IOS_HINT_DISMISSED_KEY, "1");
};

$("instalar-btn").onclick = async () => {
  if (!installEvent) return;
  hideInstallBanner();
  installEvent.prompt();
  try { await installEvent.userChoice; } catch { /* user dismissed the native prompt */ }
  installEvent = null;
};

// Android/Chrome: the browser offers the install event, so we show a button.
window.addEventListener("beforeinstallprompt", (ev) => {
  ev.preventDefault();
  installEvent = ev;
  showInstallBanner("Instala a app para teres acesso rápido, sem abrir o browser.", true);
});

window.addEventListener("appinstalled", () => {
  hideInstallBanner();
  installEvent = null;
});

// iOS/Safari: there is no beforeinstallprompt, so the hint is manual and shown only once.
if (!isStandalone() && isIOS() && isSafari() && !readLocal(IOS_HINT_DISMISSED_KEY)) {
  showInstallBanner("Para instalar: toca em Partilhar e depois em «Adicionar ao ecrã principal».", false);
}

// ---------- notificações push ----------

let chaveVapid = null;
let subscricaoAtual = null;

// Base64 URL-safe -> Uint8Array, formato exigido por pushManager.subscribe().
function urlBase64ParaUint8Array(base64) {
  const preenchimento = "=".repeat((4 - (base64.length % 4)) % 4);
  const normal = (base64 + preenchimento).replace(/-/g, "+").replace(/_/g, "/");
  const bruto = atob(normal);
  const bytes = new Uint8Array(bruto.length);
  for (let i = 0; i < bruto.length; i++) bytes[i] = bruto.charCodeAt(i);
  return bytes;
}

function notifSuportado() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

function esconderTudoNotif() {
  $("btn-notif-ativar").hidden = true;
  $("notif-ios-aviso").hidden = true;
  $("form-notif-prefs").hidden = true;
  $("btn-notif-cancelar").hidden = true;
}

async function carregarPreferenciasNotif() {
  try {
    const prefs = await api("GET", "/notificacoes/preferencias");
    for (const chk of $("form-notif-prefs").querySelectorAll("input[data-evento]")) {
      chk.checked = prefs[chk.dataset.evento] !== false;
    }
  } catch (erro) {
    console.error("Falha ao ler preferências de notificações:", erro);
    // mantém os interruptores no estado por omissão (tudo ligado)
  }
}

// Chamado depois de entrar, e outra vez depois de qualquer ação de notificações,
// para o ecrã refletir sempre o estado real do dispositivo.
async function atualizarEstadoNotif() {
  const estado = $("notif-estado");

  if (!notifSuportado()) {
    estado.textContent = "Este dispositivo não suporta notificações.";
    esconderTudoNotif();
    return;
  }

  if (isIOS() && !isStandalone()) {
    estado.textContent = "";
    esconderTudoNotif();
    $("notif-ios-aviso").hidden = false;
    return;
  }

  let resp;
  try {
    resp = await fetch("/api/push/chave");
  } catch (erro) {
    console.error("Falha ao verificar o estado das notificações:", erro);
    estado.textContent = "Não foi possível verificar as notificações.";
    esconderTudoNotif();
    return;
  }
  if (resp.status === 503) {
    estado.textContent = "As notificações não estão configuradas neste servidor.";
    esconderTudoNotif();
    return;
  }
  if (!resp.ok) {
    estado.textContent = "Não foi possível verificar as notificações.";
    esconderTudoNotif();
    return;
  }
  const dados = await resp.json().catch(() => ({}));
  chaveVapid = dados.chave_publica;

  const registration = await navigator.serviceWorker.ready;
  subscricaoAtual = await registration.pushManager.getSubscription();

  if (Notification.permission === "denied") {
    estado.textContent = "As notificações foram bloqueadas nas definições do browser.";
    esconderTudoNotif();
    if (subscricaoAtual) {
      $("btn-notif-cancelar").hidden = false;
      $("form-notif-prefs").hidden = false;
      await carregarPreferenciasNotif();
    }
    return;
  }

  if (subscricaoAtual) {
    estado.textContent = "Notificações ligadas neste dispositivo.";
    esconderTudoNotif();
    $("btn-notif-cancelar").hidden = false;
    $("form-notif-prefs").hidden = false;
    await carregarPreferenciasNotif();
  } else {
    estado.textContent = "Notificações desligadas neste dispositivo.";
    esconderTudoNotif();
    $("btn-notif-ativar").hidden = false;
  }
}

// Erros de pushManager.subscribe()/getSubscription() distinguem-se pelo "name"
// (NotAllowedError, InvalidStateError, AbortError, NotSupportedError); sem ele
// não há como diagnosticar à distância porque não temos acesso ao dispositivo.
function explicarErroNotif(erro) {
  const nome = erro && erro.name ? erro.name : "Erro";
  const mensagem = erro && erro.message ? erro.message : String(erro);
  let dica = "";
  if (nome === "AbortError") dica = " (costuma ser falha a contactar o serviço de push)";
  else if (nome === "InvalidStateError") dica = " (costuma ser uma subscrição anterior com outra chave; cancela a subscrição e volta a ligar)";
  return `${nome}, ${mensagem}${dica}`;
}

// Só pedimos permissão a partir daqui, num clique explícito: nunca ao carregar
// a página (falha sempre em iOS e, em Android, gasta o pedido sem contexto).
$("btn-notif-ativar").onclick = async () => {
  try {
    const permissao = await Notification.requestPermission();
    if (permissao !== "granted") {
      toast("Permissão de notificações recusada.", true);
      return;
    }
    if (!chaveVapid) throw new Error("sem chave");
    const registration = await navigator.serviceWorker.ready;
    const subscricao = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ParaUint8Array(chaveVapid),
    });
    const chaves = subscricao.toJSON().keys;
    await api("POST", "/push/subscricoes", {
      endpoint: subscricao.endpoint,
      p256dh: chaves.p256dh,
      auth: chaves.auth,
      dispositivo: navigator.userAgent.slice(0, 120),
    });
    toast("Notificações ligadas.");
  } catch (erro) {
    console.error("Falha ao ligar notificações:", erro);
    toast(`Não foi possível ligar as notificações: ${explicarErroNotif(erro)}`, true);
  } finally {
    await atualizarEstadoNotif();
  }
};

$("btn-notif-cancelar").onclick = async () => {
  try {
    if (subscricaoAtual) {
      await api("DELETE", "/push/subscricoes", { endpoint: subscricaoAtual.endpoint })
        .catch((erro) => console.error("Falha ao cancelar subscrição no servidor:", erro));
      await subscricaoAtual.unsubscribe();
    }
    toast("Subscrição cancelada neste dispositivo.");
  } catch (erro) {
    console.error("Falha ao cancelar subscrição de notificações:", erro);
    toast(`Não foi possível cancelar a subscrição: ${explicarErroNotif(erro)}`, true);
  } finally {
    await atualizarEstadoNotif();
  }
};

$("form-notif-prefs").addEventListener("change", async (ev) => {
  if (!ev.target.closest("input[data-evento]")) return;
  const prefs = {};
  for (const chk of $("form-notif-prefs").querySelectorAll("input[data-evento]")) prefs[chk.dataset.evento] = chk.checked;
  try { await api("PUT", "/notificacoes/preferencias", prefs); }
  catch (e) { toast(e.message, true); }
});
