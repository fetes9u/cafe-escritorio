# Saldos e pagamentos por MB WAY: especificação

Data: 2026-09-23. Estado: aprovado; pronto para implementar.

## 1. Objectivo

Hoje cada pessoa paga, no mês seguinte, exactamente o que bebeu a quem compra as
cápsulas, e essa pessoa marca "Recebi o pagamento". A equipa paga por MB WAY e não
quer andar com moedas: quer mandar 5 € quando bebeu 4 €, e às vezes a pessoas
diferentes. O fecho de mês não comporta nenhuma das duas coisas.

Passa a haver **um saldo corrido por pessoa**, contra o "pote" do escritório,
calculado a partir de três movimentos: cafés, compras de cápsulas e pagamentos
MB WAY. O fecho de mês desaparece.

## 2. Requisitos

| # | Requisito | Origem |
|---|-----------|--------|
| S1 | Pagar qualquer valor (5, 10, 20 € ou outro); o que sobra fica a favor | pedido inicial |
| S2 | Pagar a qualquer colega; a app sabe a quem foi | pedido inicial |
| S3 | Saldo corrido, sem fecho de mês | opção C escolhida |
| S4 | Quem pagou regista; o saldo muda logo; quem recebeu pode dizer "Não recebi" | resposta à Q2 |
| S5 | Converter os dados de produção sem perder nada | resposta à Q3 |
| S6 | A app não guarda números de telefone | resposta à Q4 |
| S7 | Preço por café = custo médio do que está no armário | revisão adversarial, aceite |
| S8 | Quem recebe tem uma lista "por confirmar" na app, não só a notificação | revisão adversarial, aceite |

Fora de âmbito: resumo mensal por push (não há agendador na app), desactivar contas
de quem sai da equipa, pagamentos pela própria app (a app só regista).

## 3. Modelo

### 3.1 Movimentos

| Movimento | Onde | Efeito no saldo |
|---|---|---|
| Café | `cafes.valor_cent` (coluna nova) | −valor para quem bebeu |
| Compra | `compras.custo_cent` (coluna nova) | +custo para quem registou |
| Pagamento | tabela nova `transferencias`, só as não anuladas | +valor para quem pagou, −valor para quem recebeu |

O saldo **nunca se guarda**; calcula-se sempre:

```
saldo(u) = Σ transferencias activas com pagador = u
         − Σ transferencias activas com recebedor = u
         + Σ compras.custo_cent com utilizador_id = u
         − Σ cafes.valor_cent com utilizador_id = u
```

"Activa" = `anulada_em IS NULL`. Tudo em cêntimos inteiros.

### 3.2 Preço por café: custo médio do armário

```
por_recuperar = Σ compras.custo_cent − Σ cafes.valor_cent
stock         = Σ compras.capsulas − COUNT(cafes)          (o _stock que já existe)
```

- `stock > 0`: `preco = max(0, arredonda(por_recuperar / stock))`, com o `arredonda`
  de `logic.py` (0,5 sobe).
- `stock <= 0`: preço do café gravado mais recentemente (`valor_cent` do último por
  `id`, não por `em`: um café offline sincronizado tarde tem um `em` antigo mas foi
  precificado agora); se não houver nenhum, `config.preco_cent`.

O preço calcula-se **no momento em que o café é gravado no servidor** e fica no
`valor_cent` desse café para sempre. Um café offline sincronizado mais tarde leva o
preço do momento da sincronização: é um desvio de cêntimos que o próprio mecanismo
absorve, porque o preço seguinte volta a dividir o que falta recuperar pelo que
resta.

Porque é assim: com "custo da última compra", uma caixa nova mais cara re-precifica
as cápsulas antigas que ainda estão no armário e cobra-se a mais (exemplo da
revisão: 17,50 € de excesso numa troca de 0,25 € para 0,60 €). Com o custo médio,
quando a última cápsula sai, o dinheiro está recuperado ao cêntimo, e os
arredondamentos corrigem-se sozinhos café a café.

O `preco_cent` da `config` deixa de se editar na app; fica só como valor de recurso
e como preço da conversão (secção 6).

### 3.3 O pote

`por_recuperar` é, por construção, a soma dos saldos de toda a gente (as
transferências anulam-se entre pagador e recebedor). O Escritório mostra-o como
"Pote: 12,00 € em cápsulas no armário (42 cápsulas)".

Excepção conhecida: uma compra com `utilizador_id NULL` conta para `por_recuperar`
mas não credita ninguém. O código actual grava sempre o autor, portanto não deve
haver nenhuma; o ensaio da migração sobre a cópia de produção confirma-o.

### 3.4 Pagamentos: ciclo de vida

```
registado (confirmada_em NULL, anulada_em NULL)
   ├── recebedor carrega ✓         → confirmado (confirmada_em = agora). Imutável.
   ├── recebedor carrega Não recebi → anulado (anulada_por = recebedor)
   └── pagador carrega Anular       → anulado (anulada_por = pagador)
```

- Conta para o saldo desde que é registado até ser anulado.
- Depois de confirmado já não se anula: um engano corrige-se com um pagamento no
  sentido contrário.
- Anulado não volta atrás.
- Valor entre 1 cêntimo e 1000,00 €. Pagador e recebedor diferentes.

### 3.5 Sugestão de a quem pagar

Só quando `saldo < 0`. Credor = a outra pessoa com o maior saldo positivo (em
empate, o nome por ordem alfabética). Valor sugerido = `min(−saldo, saldo do
credor)`, para cinco pessoas não mandarem cada uma 10 € à mesma pessoa. Sem credor
positivo, não há sugestão.

## 4. API

Contrato ao nível do campo. Nomes em português, como o resto da API.

### 4.1 `GET /api/eu` (muda)

Sai `mes_anterior`. Entram `saldo_cent`, `sugestao`, `por_confirmar`.
`preco_cent` passa a ser o preço corrente da secção 3.2. `valor_cent` passa a ser a
soma dos `valor_cent` dos cafés do mês. `estimativa_cent = valor_cent +
(estimativa_cafes − cafes) × preco_cent`.

```json
{
  "utilizador": {"id": 3, "nome": "Joana", "cafes_dia": 2},
  "preco_cent": 31,
  "mes": "2026-09",
  "cafes": 12,
  "valor_cent": 330,
  "estimativa_cafes": 40,
  "estimativa_cent": 1198,
  "ultimo_cafe": "2026-09-23T08:12:00+00:00",
  "saldo_cent": -250,
  "sugestao": {"utilizador_id": 1, "nome": "Ricardo", "valor_cent": 250},
  "por_confirmar": [
    {"id": 17, "pagador_id": 5, "pagador": "Pedro", "valor_cent": 500, "em": "2026-09-22T10:00:00+00:00"}
  ],
  "stock": { "...": "igual ao actual" }
}
```

`sugestao` é `null` quando não há. `por_confirmar` são as transferências activas e
não confirmadas em que eu sou o recebedor, mais recentes primeiro.

### 4.2 `POST /api/transferencias` (novo)

Pedido: `{"recebedor_id": 1, "valor_cent": 500}`. O pagador é quem está
autenticado.

- 201 → `{"id": 18, "pagador_id": 3, "recebedor_id": 1, "valor_cent": 500, "em": "..."}`
- 400 se `recebedor_id` for o próprio; 404 se não existir; 422 se o valor sair de
  1..100000.
- Push `pagamento` **só ao recebedor**: título `Pagamento`, corpo
  `Joana registou 5,00 € pagos a ti`.

### 4.3 `POST /api/transferencias/{id}/confirmar` (novo)

Só o recebedor. 200 → `{"ok": true}`. 403 se não for o recebedor; 404 se não
existir; 409 se já estiver anulada ou confirmada.

### 4.4 `POST /api/transferencias/{id}/anular` (novo)

Pagador ou recebedor. 200 → `{"ok": true}`. 403 a mais ninguém; 404 se não
existir; 409 se já confirmada ou anulada. Push `pagamento` **só à outra parte**:
`Joana anulou o pagamento de 5,00 €` ou `Ricardo disse que não recebeu os 5,00 €`.

### 4.5 `GET /api/movimentos` (novo)

Os meus movimentos em dinheiro, para o separador Histórico → Dinheiro.

```json
{
  "saldo_cent": -250,
  "transferencias": [
    {"id": 18, "sentido": "paguei", "outro_id": 1, "outro": "Ricardo", "valor_cent": 500,
     "em": "...", "confirmada_em": null, "anulada_em": null, "anulada_por": null}
  ],
  "compras": [
    {"id": 4, "capsulas": 50, "custo_cent": 1500, "custo_estimado": false, "em": "..."}
  ],
  "meses": [
    {"mes": "2026-09", "cafes": 42, "valor_cent": 1050}
  ]
}
```

`sentido` é `"paguei"` ou `"recebi"`. `anulada_por` é o id de quem anulou. Tudo do
mais recente para o mais antigo; `transferencias` inclui as anuladas.

### 4.6 `GET /api/escritorio` (muda)

Por pessoa saem `pago` e `pagamento`; entra `saldo_cent`. `valor_cent` passa a ser a
soma dos `valor_cent` do mês escolhido. Entra `pote`. Cada compra ganha
`custo_cent`, `custo_estimado` e `pode_editar`.

```json
{
  "eu": 3, "mes": "2026-09", "mes_actual": "2026-09", "meses": ["2026-09"],
  "preco_cent": 31,
  "pessoas": [{"id": 1, "nome": "Ricardo", "cafes": 30, "valor_cent": 750, "saldo_cent": 1200}],
  "total_cafes": 30, "total_cent": 750,
  "pote": {"valor_cent": 1200, "capsulas": 42},
  "stock": { "...": "igual" },
  "compras": [{"id": 4, "capsulas": 50, "custo_cent": 1500, "custo_estimado": false,
               "em": "...", "nota": null, "utilizador_id": 1, "nome": "Ricardo", "pode_editar": true}]
}
```

`pode_editar` é verdadeiro para quem registou a compra, ou para toda a gente se a
compra não tiver autor.

### 4.7 Compras (mudam)

- `POST /api/compras`: `{"capsulas": 50, "custo_cent": 1500, "nota": null}`;
  `custo_cent` obrigatório, 1..1000000. A notificação passa a
  `Ricardo repôs 50 cápsulas (15,00 €)`.
- `PATCH /api/compras/{id}` (novo): `{"custo_cent": 1450}`. Só se `pode_editar`
  (403 caso contrário). Põe `custo_estimado = 0`. 200 → `{"ok": true}`.
- `GET /api/preco/simular?capsulas=50&custo_cent=1500` (novo):
  `{"preco_cent": 31}`, o preço que ficaria em vigor se esta compra fosse gravada
  agora. Usado para o formulário mostrar "o café passa a custar 0,31 €" antes de
  guardar.

### 4.8 O que sai

- `POST /api/pagamentos` e `DELETE /api/pagamentos/{mes}/{pagador_id}` respondem
  **410** `{"detail": "Os pagamentos mensais acabaram. Actualiza a app."}`, para um
  cliente antigo em cache mostrar uma mensagem com sentido em vez de um 404.
- `PUT /api/config` deixa de aceitar `preco_cent` (o campo sai do modelo).
- `DELETE /api/cafe/ultimo` deixa de recusar por "mês com pagamentos".
- `_aceita_em` deixa de trocar a hora de um café por "agora" quando o mês estava
  pago.
- `_pagamento()` e todas as leituras da tabela `pagamentos` fora da migração.

### 4.9 Notificações

`_avisar` difunde a todos menos ao autor, e assim continua para `cafe`, `compra`,
`stock_baixo` e `registo`. Para `pagamento` entra um envio dirigido a **uma**
pessoa, que respeita na mesma quem desligou o evento. O payload não muda:
`{"titulo", "corpo", "url", "evento"}`.

## 5. Ecrãs

### 5.1 Café

O bloco "Mês anterior" (`ant-mes`, `ant-valor`, `ant-estado`) é substituído por um
cartão de saldo com o mesmo tamanho. A página continua sem scroll em 375×812.

- Frase: `Deves 2,50 €` (saldo < 0), `Tens 1,00 € a teu favor` (> 0),
  `Contas certas` (= 0). Qualquer saldo negativo diz "Deves", incluindo o de quem
  recebeu dinheiro dos outros: esse dinheiro é devido ao pote.
- Com sugestão: `Sugestão: paga 2,50 € ao Ricardo` e o botão **Paguei por MB WAY**.
  Sem dívida, o botão fica na mesma, mais discreto (pode-se pagar adiantado).
- O botão troca o conteúdo do cartão por um formulário: atalhos `5 €`, `10 €`,
  `20 €` e `2,50 €` (o valor sugerido, se houver), um campo livre, e **A quem**
  (lista de `/api/utilizadores` sem o próprio, com o credor sugerido já
  escolhido). Guardar → `POST /api/transferencias` → toast
  `Pagamento registado; o Ricardo foi avisado.` → recarrega `/api/eu`.
- Com `por_confirmar` não vazio: `2 pagamentos por confirmar`, cada um com
  `Pedro · 5,00 € · ontem` e os botões **✓** e **Não recebi**.
- Pagar precisa de rede: sem rede, o botão mostra `Precisas de rede para registar
  um pagamento.` Não entra na fila offline.
- O desconto optimista de um café (`eu.valor_cent += eu.preco_cent`) passa a
  descontar também `eu.saldo_cent`.

### 5.2 Histórico

Um seletor `Cafés | Dinheiro` por cima. "Cafés" é o calendário que já existe.
"Dinheiro" usa `/api/movimentos`: o saldo no topo e depois a lista.

- `Pagaste 5,00 € ao Ricardo · 22/09`, com **Anular** enquanto não estiver
  confirmado nem anulado.
- `Recebeste 5,00 € do Pedro · 22/09`, com **✓** e **Não recebi** nas mesmas
  condições.
- Anuladas riscadas, com `anulado por ti` ou `anulado pelo Ricardo`.
- `Compraste 50 cápsulas · 15,00 €`.
- Uma linha por mês: `Setembro: 42 cafés, −10,50 €`.

### 5.3 Escritório

- Tabela: `Nome | Cafés | € do mês | Saldo`. A coluna "Pago" e o botão "Recebi o
  pagamento" desaparecem; o seletor de meses fica para os relatórios.
- Por cima: `Pote: 12,00 € em cápsulas no armário (42 cápsulas)`.
- Formulário de entrada de cápsulas: campo **Custo (€)** obrigatório; ao escrever
  cápsulas e custo, mostra `O café passa a custar 0,31 €` via
  `/api/preco/simular`.
- Lista de entradas: cada uma com o custo; as de `custo_estimado` marcadas
  `custo estimado`; onde `pode_editar`, um botão **Corrigir custo**.
- Definições: sai o preço; fica o limiar de aviso.

### 5.4 Service worker e fotografias offline

Sobe `CACHE_VERSION` em `sw.js` para os telemóveis largarem o `app.js` antigo.

O `app.js` guarda fotografias de `/api/eu` e `/api/escritorio` na loja IndexedDB
`instantaneos` para mostrar sem rede. Depois do deploy, o `app.js` novo pode ler
uma fotografia no formato antigo (com `mes_anterior`, sem `saldo_cent`). Uma
fotografia de `/api/eu` sem `saldo_cent`, ou de `/api/escritorio` sem `pote`, trata-se
como inexistente: nunca se desenha "Deves NaN €".

## 6. Conversão dos dados de produção

Corre em `db.init()`, numa **única transacção explícita**, e é idempotente.

1. `ALTER TABLE cafes ADD COLUMN valor_cent INTEGER` (aceita NULL no esquema).
2. `ALTER TABLE compras ADD COLUMN custo_cent INTEGER` e
   `ADD COLUMN custo_estimado INTEGER NOT NULL DEFAULT 0`.
3. `CREATE TABLE IF NOT EXISTS transferencias (id INTEGER PRIMARY KEY, pagador_id
   INTEGER NOT NULL REFERENCES utilizadores(id), recebedor_id INTEGER NOT NULL
   REFERENCES utilizadores(id), valor_cent INTEGER NOT NULL CHECK (valor_cent > 0),
   em TEXT NOT NULL, confirmada_em TEXT, anulada_em TEXT, anulada_por INTEGER
   REFERENCES utilizadores(id), pagamento_origem_id INTEGER UNIQUE, CHECK
   (pagador_id != recebedor_id))`.
4. Cafés com `valor_cent IS NULL`: se houver `pagamentos` para (`mes`,
   `utilizador_id`) com `capsulas > 0`, `valor_cent = pagamentos.valor_cent /
   pagamentos.capsulas` (divisão exacta, porque o valor foi gravado como `n ×
   preço`); senão `config.preco_cent`.
5. Compras com `custo_cent IS NULL`: `custo_cent = capsulas × config.preco_cent`,
   `custo_estimado = 1`.
6. `INSERT OR IGNORE INTO transferencias` a partir de `pagamentos`, com
   `pagamento_origem_id = pagamentos.id`, `em = confirmada_em = pagamentos.em`,
   **só onde `pagador_id != recebedor_id`**: quem recebia marcava também a sua
   própria linha, e isso não moveu dinheiro nenhum.

Os passos 4 a 6 só tocam em linhas por converter (`IS NULL`, `UNIQUE`), portanto
um segundo arranque, ou um arranque depois de um crash a meio, não duplica nada.

A tabela `pagamentos` fica na base de dados de produção, sem ser lida nem escrita
pelo código novo; sai numa versão seguinte. O `CREATE TABLE pagamentos` sai do
`SCHEMA`: uma base nova não a tem, e por isso os passos 4 e 6 só correm se a tabela
existir (`SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pagamentos'`).

A idempotência vem das guardas de cada passo (`IS NULL`, `UNIQUE`, `PRAGMA
table_info` antes de cada `ALTER`), não da transacção. O `executescript(SCHEMA)`
faz commit implícito; para os passos 4 a 6 correrem mesmo juntos, a ligação passa a
`isolation_level = None` com `BEGIN IMMEDIATE` e `COMMIT` explícitos à volta deles.

**Voltar atrás:** depois de a versão nova criar uma transferência, voltar à imagem
anterior **não é suportado**. A imagem antiga não vê as transferências, e ao
regressar re-migraria qualquer "Recebi o pagamento" feito entretanto. A partir daí
só se corrige com uma versão nova. Fica escrito no README.

**Ensaio:** antes do deploy, tirar cópia da base de produção pela API de backup,
correr `db.init()` sobre a cópia e confirmar: o número de cafés, compras e
pagamentos antes e depois; Σ `transferencias.valor_cent` = Σ `pagamentos.valor_cent`
das linhas com pagador ≠ recebedor; e a soma dos saldos igual a `por_recuperar`.

## 7. Testes

Lógica pura (`tests/test_logic.py`):
- preço médio: 18,50 € e 60 cápsulas dá 31; stock 0 usa o último preço; valor por
  recuperar negativo dá 0; o 0,5 arredonda para cima.
- exemplo da revisão: caixa de 100 a 25 €, 50 bebidas, caixa de 10 a 6 €; ao
  esvaziar o armário, `por_recuperar` fica **exactamente 0**. Com `por_recuperar >= 0`,
  arredondamento com 0,5 a subir e sem apagar nem corrigir compras, o último café
  leva o que falta ao cêntimo e o valor nunca fica negativo pelo caminho.

API (`tests/test_api.py` e um ficheiro novo `tests/test_saldos.py`):
- o exemplo do pedido: bebo 16 cafés a 0,25 €, pago 5 € → `saldo_cent = 100`.
- pagamento: +pagador, −recebedor; anular devolve os dois; confirmar e depois
  anular dá 409; a um terceiro, 403; a si próprio, 400.
- sugestão: limitada ao que o credor tem a receber; `null` sem credor.
- soma dos saldos = `pote.valor_cent`, depois de uma sequência mista de cafés,
  compras, pagamentos, anulações e correcção de custo.
- push de pagamento só ao recebedor (e só à outra parte ao anular).
- compras: `custo_cent` obrigatório; `PATCH` só por quem pode; `simular` bate com o
  preço do café seguinte.
- rotas antigas de pagamentos respondem 410; desfazer café num mês que tinha
  pagamento já não dá 409.

Migração (`tests/test_db_migracao.py`):
- a base antiga constrói-se **à mão**, com a DDL literal do esquema de `55e7ec6`,
  antes de chamar `init()`. Um teste que chama `init()` sobre um ficheiro vazio e
  depois insere linhas passa quer a migração funcione quer não.
- base no esquema antigo com cafés, compras, um pagamento normal e um "próprio"
  (pagador = recebedor) → depois de `init()`: preços dos cafés pagos vêm da
  fotografia, os outros do `preco_cent`; compras com custo estimado; uma
  transferência, não duas.
- `init()` duas vezes → nada duplicado.

Fumo (`scripts/smoke_deploy.py`): um passo novo pelo percurso do utilizador: duas
contas, uma bebe, paga à outra por `POST /api/transferencias`, e o `saldo_cent` das
duas em `/api/eu` mexe no sentido certo (ler de volta, não confiar no 201).

Contrato (`tests/test_contrato_api.py`, escrito depois de juntar as duas metades,
porque só passa com o cliente e o servidor novos na mesma árvore): os campos que o `app.js` lê de `/api/eu`
(`saldo_cent`, `sugestao`, `por_confirmar`), de `/api/movimentos` e de
`/api/escritorio` (`saldo_cent`, `pote`, `pode_editar`) extraídos do cliente por
regex e confirmados contra a resposta real, e o corpo que envia a
`POST /api/transferencias`.

## 8. Documentação

O README troca a regra do fecho de mês pelas regras desta especificação (saldo,
preço médio, pagamentos, voltar atrás) e perde a linha "o preço que conta é o que
estiver em vigor quando se marca o pagamento".
