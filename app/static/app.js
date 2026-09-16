/* Café do escritório — front-end sem dependências. */
"use strict";

const $ = (id) => document.getElementById(id);
const vistas = { entrada: $("vista-entrada"), cafe: $("vista-cafe"), historico: $("vista-historico"), escritorio: $("vista-escritorio") };
let eu = null;           // resposta de /api/eu
let escritorio = null;   // resposta de /api/escritorio
let historico = null;    // resposta de /api/historico (o mês visível)
let histDia = null;      // dia seleccionado no calendário, "YYYY-MM-DD"
let histPorDia = new Map(); // dia -> { n, horas: [{ hora, fila }] }, servidor mais fila offline

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
    if (!foto) throw erro; // no cached state for anyone: nothing to show offline
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

function desenharEu() {
  $("mes-cafes").textContent = `${plural(eu.cafes, "cápsula", "cápsulas")} · ${euros(eu.valor_cent)}`;
  $("mes-est").textContent = `${plural(eu.estimativa_cafes, "cápsula", "cápsulas")} · ${euros(eu.estimativa_cent)}`;
  $("prev").textContent = `~${String(eu.utilizador.cafes_dia).replace(".", ",")}/dia`;
  const a = eu.mes_anterior;
  $("ant-mes").textContent = nomeMes(a.mes);
  $("ant-valor").textContent = `${plural(a.cafes, "cápsula", "cápsulas")} · ${euros(a.valor_cent)}`;
  if (a.cafes === 0 && !a.pago) $("ant-estado").textContent = "Nada a pagar.";
  else if (a.pago) {
    const p = a.pagamento;
    $("ant-estado").innerHTML = p.recebedor_id === eu.utilizador.id
      ? `<span class="pago">Pago em ${dataCurta(p.em)} (és tu quem recebe)</span>`
      : `<span class="pago">Pago a ${p.recebedor} em ${dataCurta(p.em)}</span>`;
  } else $("ant-estado").innerHTML = '<span class="porpagar">Por pagar</span> — paga a quem vai comprar as cápsulas; essa pessoa marca como recebido.';
  const s = $("stock");
  s.className = "faixa" + (eu.stock.baixo ? " baixo" : "");
  s.innerHTML = textoStock(eu.stock);
}

$("btn-cafe").onclick = async () => {
  const b = $("btn-cafe");
  b.disabled = true;
  const clienteId = crypto.randomUUID();
  const em = new Date().toISOString();
  try { await idbPut("fila", { cliente_id: clienteId, tipo: "cafe", em, criado_em: em }); }
  catch (erro) { console.error("Falha ao guardar café na fila offline:", erro); }

  eu.cafes += 1; eu.valor_cent += eu.preco_cent; eu.stock.stock -= 1;   // optimista
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
    eu.cafes -= 1; eu.valor_cent -= eu.preco_cent; eu.stock.stock += 1;
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
    eu = await api("GET", "/eu");
    await guardarInstantaneo("eu", eu.utilizador.id, eu);
    desenharEu();
    await atualizarUltimoCafe();
  } catch (e) { toast(e.message, true); }
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

// ---------- escritório ----------

async function carregarEscritorio(mes) {
  try {
    escritorio = await api("GET", "/escritorio" + (mes ? `?mes=${mes}` : ""));
    if (!mes && escritorio.meses.length > 1) {
      // Sem mês escolhido: se o mês anterior ainda tem pagamentos por fazer, abre nele.
      const anterior = await api("GET", `/escritorio?mes=${escritorio.meses[1]}`);
      if (anterior.pessoas.some((p) => p.cafes > 0 && !p.pago)) escritorio = anterior;
    }
    const idAtual = eu ? eu.utilizador.id : readLocal(UTILIZADOR_ATUAL_KEY);
    if (idAtual) await guardarInstantaneo("escritorio", idAtual, escritorio);
    $("esc-fotografia-aviso").hidden = true;
  } catch (erro) {
    if (!erro || !erro.rede) throw erro;
    const idAtual = eu ? eu.utilizador.id : readLocal(UTILIZADOR_ATUAL_KEY);
    const foto = idAtual ? await idbUm("instantaneos", `escritorio:${idAtual}`).catch(() => null) : null;
    if (!foto) throw erro;
    escritorio = foto.dados;
    $("esc-fotografia-aviso").textContent = `Sem ligação. A mostrar o último estado conhecido (${horaCurta(foto.em)}).`;
    $("esc-fotografia-aviso").hidden = false;
  }
  desenharEscritorio();
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

  const fechado = e.mes < e.mes_actual;
  const tb = $("tabela").querySelector("tbody");
  tb.innerHTML = "";
  for (const p of e.pessoas) {
    const tr = document.createElement("tr");
    let estado = "";
    if (fechado) {
      if (p.pago) {
        estado = p.pagamento.recebedor_id === p.id
          ? `<span class="pago">Pago em ${dataCurta(p.pagamento.em)} (é quem recebe)</span>`
          : `<span class="pago">Pago a ${p.pagamento.recebedor} em ${dataCurta(p.pagamento.em)}</span>`;
        if (p.pagamento.recebedor_id === e.eu) estado += ` <button class="ligacao" data-anular="${p.id}">anular</button>`;
      } else if (p.cafes > 0) {
        estado = `<span class="porpagar">Por pagar</span> <button class="ligacao" data-receber="${p.id}">Recebi o pagamento</button>`;
      } else estado = '<span class="nota">—</span>';
    }
    tr.innerHTML = `<td>${p.nome}</td><td class="num">${p.cafes}</td><td class="num">${euros(p.valor_cent)}</td><td>${estado}</td>`;
    tb.appendChild(tr);
  }
  $("esc-ajuda").textContent = fechado
    ? "Mês fechado. Quem recebe o dinheiro carrega em «Recebi o pagamento» na linha de cada pessoa (também na sua)."
    : "Mês em curso. O fecho aparece quando o mês terminar.";

  const s = $("esc-stock");
  s.className = "faixa" + (e.stock.baixo ? " baixo" : "");
  s.innerHTML = textoStock(e.stock);

  const ul = $("compras");
  ul.innerHTML = "";
  for (const c of e.compras) {
    const li = document.createElement("li");
    const apagar = c.utilizador_id === e.eu ? ` <button class="ligacao" data-apagar="${c.id}">apagar</button>` : "";
    li.innerHTML = `<span>+${c.capsulas} · ${c.nome || "?"} · ${dataCurta(c.em)}${c.nota ? " · " + c.nota : ""}</span><span>${apagar}</span>`;
    ul.appendChild(li);
  }
  if (!e.compras.length) ul.innerHTML = '<li class="nota">Ainda não há entradas. Regista as cápsulas iniciais.</li>';

  $("cfg-preco").value = (e.preco_cent / 100).toFixed(2);
  $("cfg-limiar").value = e.stock.limiar;
}

$("sel-mes").onchange = () => carregarEscritorio($("sel-mes").value);

$("vista-escritorio").onclick = async (ev) => {
  const b = ev.target.closest("button[data-receber],button[data-anular],button[data-apagar]");
  if (!b) return;
  try {
    if (b.dataset.receber) {
      const p = escritorio.pessoas.find((x) => x.id === Number(b.dataset.receber));
      if (!confirm(`Confirmas que recebeste ${euros(p.valor_cent)} de ${p.nome} por ${nomeMes(escritorio.mes)}?`)) return;
      await api("POST", "/pagamentos", { mes: escritorio.mes, pagador_id: p.id });
      toast("Pagamento registado.");
    } else if (b.dataset.anular) {
      if (!confirm("Anular este pagamento?")) return;
      await api("DELETE", `/pagamentos/${escritorio.mes}/${b.dataset.anular}`);
      toast("Pagamento anulado.");
    } else if (b.dataset.apagar) {
      if (!confirm("Apagar esta entrada de cápsulas?")) return;
      await api("DELETE", `/compras/${b.dataset.apagar}`);
      toast("Entrada apagada.");
    }
    await carregarEscritorio(escritorio.mes);
  } catch (e) { toast(e.message, true); }
};

$("form-compra").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("POST", "/compras", { capsulas: Number($("compra-n").value), nota: $("compra-nota").value || null });
    $("form-compra").reset();
    toast("Entrada registada.");
    await carregarEscritorio(escritorio.mes);
  } catch (err) { toast(err.message, true); }
};

$("form-config").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("PUT", "/config", { preco_cent: Math.round(Number($("cfg-preco").value) * 100), stock_baixo: Number($("cfg-limiar").value) });
    toast("Definições guardadas.");
    await carregarEscritorio(escritorio.mes);
  } catch (err) { toast(err.message, true); }
};

// ---------- abas e arranque ----------

$("abas").onclick = async (ev) => {
  const b = ev.target.closest("button[data-vista]");
  if (!b) return;
  try {
    if (b.dataset.vista === "cafe") await entrar();
    else if (b.dataset.vista === "historico") {
      await carregarHistorico();
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
