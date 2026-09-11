/* Café do escritório — front-end sem dependências. */
"use strict";

const $ = (id) => document.getElementById(id);
const vistas = { entrada: $("vista-entrada"), cafe: $("vista-cafe"), escritorio: $("vista-escritorio") };
let eu = null;           // resposta de /api/eu
let escritorio = null;   // resposta de /api/escritorio

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
  const r = await fetch("/api" + rota, {
    method: metodo,
    headers: corpo ? { "Content-Type": "application/json" } : {},
    body: corpo ? JSON.stringify(corpo) : undefined,
  });
  if (r.status === 401 && rota !== "/login") { carregarNomes().catch(() => {}); mostrar("entrada"); throw new Error("Sessão expirada. Entra outra vez."); }
  const dados = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(dados.detail || `Erro ${r.status}`);
  return dados;
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
  eu = await api("GET", "/eu");
  desenharEu();
  mostrar("cafe");
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
  eu.cafes += 1; eu.valor_cent += eu.preco_cent; eu.stock.stock -= 1;   // optimista
  desenharEu();
  try {
    await api("POST", "/cafe");
    toast("Café marcado ☕");
    eu = await api("GET", "/eu");
  } catch (e) {
    toast(e.message, true);
    eu = await api("GET", "/eu").catch(() => eu);
  } finally {
    desenharEu();
    b.disabled = false;
  }
};

$("btn-desfazer").onclick = async () => {
  if (!confirm("Apagar o teu último café?")) return;
  try { await api("DELETE", "/cafe/ultimo"); toast("Café apagado."); eu = await api("GET", "/eu"); desenharEu(); }
  catch (e) { toast(e.message, true); }
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
  await api("POST", "/logout").catch(() => {});
  eu = null;
  await carregarNomes();
  mostrar("entrada");
};

// ---------- escritório ----------

async function carregarEscritorio(mes) {
  escritorio = await api("GET", "/escritorio" + (mes ? `?mes=${mes}` : ""));
  if (!mes && escritorio.meses.length > 1) {
    // Sem mês escolhido: se o mês anterior ainda tem pagamentos por fazer, abre nele.
    const anterior = await api("GET", `/escritorio?mes=${escritorio.meses[1]}`);
    if (anterior.pessoas.some((p) => p.cafes > 0 && !p.pago)) escritorio = anterior;
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
    else { await carregarEscritorio(); mostrar("escritorio"); }
  } catch (e) { toast(e.message, true); }
};

(async () => {
  try { await entrar(); }
  catch { await carregarNomes(); mostrar("entrada"); }
})();
