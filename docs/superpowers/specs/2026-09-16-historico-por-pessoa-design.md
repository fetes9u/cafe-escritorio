# Histórico por pessoa: especificação

Data: 2026-09-16. Estado: aprovado; pronto para implementar.

## 1. Objectivo

Acabar com a dúvida "já marquei o meu café hoje?" ao pé da máquina, e dar a cada
pessoa uma vista do seu próprio consumo ao longo do mês. Duas peças:

1. Uma linha "Último café" na página do Café, sem tocar em mais nada nessa página
   (cabe no ecrã sem scroll e assim fica).
2. Uma aba nova, **Histórico**, com um calendário do mês e o detalhe das horas do
   dia tocado.

## 2. Requisitos

| # | Requisito | Origem |
|---|-----------|--------|
| H1 | Na página do Café, ver de relance quando foi o último café | pedido inicial |
| H2 | Histórico só do próprio; não se vê o dos colegas | assumido e aceite |
| H3 | Histórico numa página própria, para a do Café continuar a caber no ecrã | resposta à Q1 |
| H4 | Entrada por uma terceira aba `Café · Histórico · Escritório` | resposta à Q2 |
| H5 | Calendário do mês: um quadrado por dia com o número de cafés; setas para mudar de mês | resposta à Q3 |
| H6 | Tocar num dia mostra o detalhe das horas | resposta à Q3 |
| H7 | Um café marcado sem rede (na fila offline) conta como marcado, na linha e no calendário | decorrente do offline |

Fora de âmbito: apagar cafés a partir do histórico (a regra "só o último" mantém-se),
ver o histórico dos colegas, exportar.

## 3. Página do Café: linha "Último café"

Uma linha por baixo do botão "Bebi um café", na classe `nota`, com id `ultimo-cafe`:

| Situação | Texto |
|----------|-------|
| Café de hoje | `Último café: hoje às 09:12` |
| Café de ontem | `Último café: ontem às 16:40` |
| Mais antigo | `Último café: ter 09/09 às 10:05` |
| Nunca | `Ainda nenhum café.` |
| Há café na fila offline | usa o mais recente da fila e acrescenta ` (por sincronizar)` |

Fonte: `ultimo_cafe` de `/api/eu` (já existe, nunca foi mostrado) e as entradas de
`idbTodos("fila")`, que trazem `em`. "Hoje" e "ontem" calculam-se no browser a
partir do relógio local, tal como já se faz para `fotografia-aviso`. A linha é
desenhada em `desenharEu()` e actualizada depois de cada marcação, desfazer e
sincronização, nos mesmos pontos onde já se chama `atualizarAvisoFila()`.

## 4. Aba Histórico

### 4.1 Layout

Tudo dimensionado para caber num ecrã de telemóvel (375×812) sem scroll, com a
faixa de instalação da PWA visível. De cima para baixo:

1. **Cabeçalho do mês**: `‹  Set 2026  ›`. A seta `›` fica desactivada no mês
   corrente. Não há limite para trás; um mês sem cafés mostra a grelha vazia.
2. **Grelha 7 colunas**, cabeçalho `S T Q Q S S D` (semana a começar à segunda).
   Cada dia é um `button` com o número do dia em pequeno e o número de cafés em
   grande; vazio quando 0. Sábado e domingo esbatidos. Hoje com contorno na cor do
   café. Dias futuros a cinzento e não clicáveis. Dias antes do início do mês e
   depois do fim são células vazias sem botão.
3. **Faixa de detalhe**, altura fixa (não salta quando muda), por baixo da grelha:
   `Ter 16 · 3 cafés · 08:45 · 11:10 · 15:30`. Sem café: `Ter 16 · nenhum café`.
   Ao abrir a aba no mês corrente está seleccionado **hoje**, portanto a pergunta
   mais frequente tem zero toques. Noutro mês, o último dia com café; se não houver,
   a faixa diz `Toca num dia`.

Não é um tooltip flutuante: num ecrã táctil não há hover, e uma bolha ancorada a
uma célula da última linha sai do ecrã.

### 4.2 Cafés na fila offline

As entradas de `idbTodos("fila")` cujo `em` cai no mês visível somam-se ao dia
certo na grelha e aparecem no detalhe com a marca `(por sincronizar)` a seguir à
hora. A conversão UTC→dia local dessas entradas faz-se no browser (é o único sítio
onde existem), no mesmo fuso do telemóvel que as criou.

### 4.3 Offline

Ao falhar por rede (`erro.rede`), a aba mostra o último instantâneo guardado, chave
`historico:<utilizador_id>:<mes>`, com o aviso já usado nas outras vistas: `Sem
ligação. A mostrar o último estado conhecido (HH:MM)`. Sem instantâneo para esse
mês, mostra a grelha vazia com o aviso `Sem ligação e sem histórico guardado para
este mês`. O instantâneo grava-se em cada resposta bem sucedida. O service worker
continua a não cachear `/api/` (há teste que o garante).

## 5. API

`GET /api/historico?mes=YYYY-MM` (mês opcional; por omissão o corrente em hora local).

- Utilizador: o da sessão (`Depends(utilizador_actual)`), nunca de parâmetro.
- `mes` validado com `^\d{4}-\d{2}$` como em `Pagamento.mes`; inválido dá 422 pela
  validação do FastAPI, sem string nova.
- Consulta `SELECT em FROM cafes WHERE utilizador_id = ? AND mes = ? ORDER BY em`
  (usa o índice `cafes_mes`).
- Agrupa **no servidor em hora local** (`logic.local`, fuso `CAFE_TZ`): `cafes.em`
  é UTC e `cafes.mes` já é derivado em local; agrupar no browser punha um café das
  00:30 UTC no dia errado e desalinhava com os totais do mês.

Resposta:

```json
{
  "mes": "2026-09",
  "hoje": "2026-09-16",
  "dias": [
    {"dia": "2026-09-16", "n": 3, "horas": ["08:45", "11:10", "15:30"]},
    {"dia": "2026-09-15", "n": 1, "horas": ["10:02"]}
  ]
}
```

`dias` só traz dias com cafés, do mais recente para o mais antigo; `horas` em
`HH:MM` local, por ordem crescente. `hoje` vem do servidor para o browser marcar o
dia certo sem depender do fuso do telemóvel.

## 6. Front-end

- `index.html`: botão `data-vista="historico"` em `#abas`; `section#vista-historico`
  com `#hist-aviso` (nota, hidden), cabeçalho `#hist-anterior` / `#hist-mes` /
  `#hist-seguinte`, `#hist-grelha`, `#hist-detalhe`.
- `app.js`: `vistas.historico`; `carregarHistorico(mes)` (fetch, instantâneo,
  fusão com a fila); `desenharHistorico()` (grelha e detalhe); `escolherDia(dia)`;
  `textoUltimoCafe()` usada por `desenharEu()`. A aba usa o mesmo despacho de
  `$("abas").onclick` que as outras.
- `style.css`: `.calendario` (grid 7 colunas), `.dia`, `.dia.fds`, `.dia.hoje`,
  `.dia.futuro`, `.dia.escolhido`, `.dia .n`, `#hist-detalhe` com `min-height`.
- Service worker: `index.html`, `app.js` e `style.css` já estão na lista de
  pré-cache; incrementar a versão da cache como manda `test_cache_version_was_bumped_past_v1`.

## 7. Testes

API (`tests/test_historico.py`, com `cliente`, `relogio` e `regista` do conftest):

- Três cafés em dois dias devolvem dois `dias` com `n` e `horas` certos, mais
  recente primeiro.
- Fronteira da meia-noite local: café às 23:30 UTC em Setembro (00:30 em Lisboa no
  horário de Verão) cai no dia seguinte local.
- Mês sem cafés devolve `dias: []`.
- `mes` inválido dá 422; sem sessão dá 401.
- O histórico de A não contém os cafés de B.
- `mes` omitido é o mês corrente segundo o `relogio`.

Front-end (`tests/test_historico_frontend.py`, no estilo de `test_push_frontend.py`):

- `index.html` tem a aba `data-vista="historico"` e os ids da secção.
- `app.js` chama `/historico`, grava instantâneo com prefixo `historico:` e junta a
  fila offline ao calendário.
- `index.html` tem `#ultimo-cafe` e `app.js` escreve `por sincronizar` nessa linha.

Verificação final: suite completa (`pytest`), e a aba aberta no browser embutido em
tamanho `mobile` para confirmar que grelha e detalhe cabem sem scroll.
