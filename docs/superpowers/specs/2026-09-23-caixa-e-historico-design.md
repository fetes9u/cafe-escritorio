# Caixa, preço fixo e histórico de alterações: especificação

Data: 2026-09-23. Estado: aprovado (opção A); pronto para implementar.
Altera `2026-09-23-saldos-e-pagamentos-mbway-design.md`: onde as duas divergem, vale
esta.

## 1. Porquê

A versão 0.11.0 (saldos) assume que cada cápsula foi paga por alguém que tem de
recuperar o dinheiro, e calcula o preço como custo médio do armário. A equipa usa a
app de outra forma, e os dados de produção mostram-no:

- As primeiras 77 cápsulas foram uma **oferta** (Faria). A ideia era que os cafés
  dessas cápsulas se pagassem na mesma, a 0,25 €, para criar um **fundo** que
  compra as caixas seguintes.
- Quem guarda esse fundo (a **caixa**) é uma pessoa: hoje, a Ana.
- Com a oferta corrigida para 0,01 €, o valor por recuperar ficou negativo
  (−13,15 €), o preço médio caiu para 0 € e três cafés ficaram grátis.
- Duas correções de custo (19,25 → 0,01 € e 10,25 → 9,84 €) não deixaram rasto de
  quem as fez nem quando.

Pedidos do Ricardo que entram aqui: a caixa com responsável, a lista de pagamentos de
toda a gente, e editar pagamentos com registo da alteração.

## 2. Requisitos

| # | Requisito |
|---|-----------|
| C1 | Preço por café fixo e configurável (0,25 €), gravado em cada café no momento em que é marcado |
| C2 | Uma conta **Caixa**, guardada por uma pessoa escolhida nas Definições |
| C3 | Pagar à caixa (MB WAY a quem a guarda) ou a uma pessoa |
| C4 | Quem guarda a caixa pode registar saídas da caixa para uma pessoa (reembolsos) |
| C5 | Compras: paga pela caixa, paga do próprio bolso, ou oferta (custo 0) |
| C6 | Lista de pagamentos de toda a gente no Escritório |
| C7 | Editar pagamentos, compras e definições, com histórico de alterações visível (quem, quando, antes, depois) |
| C8 | A migração repara os cafés gravados a 0 € pelo preço médio |

Fora de âmbito: várias caixas, caixa sem responsável a receber pagamentos,
aprovação de alterações.

## 3. Modelo

### 3.1 Contas e movimentos

Cada pessoa tem um saldo; a Caixa tem outro. Tudo em cêntimos inteiros; nunca se
guarda um saldo.

| Movimento | Efeito |
|---|---|
| Café de X ao preço p | X −p |
| Compra paga do bolso de P, custo c | P +c |
| Compra paga pela caixa, custo c | Caixa +c |
| Oferta (compra com custo 0) | nada |
| Pagamento X → pessoa Y, valor v | X +v, Y −v |
| Pagamento X → Caixa, valor v | X +v, Caixa −v |
| Saída da Caixa → pessoa Y, valor v | Caixa +v, Y −v |

Só contam pagamentos com `anulada_em IS NULL`.

```
saldo(pessoa u) = Σ pagamentos activos com pagador = u
                − Σ pagamentos activos com recebedor = u
                + Σ custo das compras de u com paga_pela_caixa = 0
                − Σ cafes.valor_cent de u
saldo(Caixa)    = Σ pagamentos activos da caixa (de_caixa)
                − Σ pagamentos activos para a caixa (para_caixa)
                + Σ custo das compras com paga_pela_caixa = 1
```

Leituras para o ecrã:

- **Dinheiro na caixa** = `−saldo(Caixa)`: o que quem a guarda deve ter consigo.
- **Por receber** = soma dos saldos negativos das pessoas, em valor absoluto.
- **Fundo** = `Σ cafes.valor_cent − Σ compras.custo_cent`: o valor que a oferta e a
  margem criaram.
- Invariante (teste): `Σ saldos das pessoas + saldo(Caixa) = −Fundo`.

### 3.2 Preço

`config.preco_cent` volta a editar-se nas Definições (com histórico). Cada café grava
`valor_cent = config.preco_cent` no momento em que o servidor o grava. O preço médio
(`logic.preco_corrente`, `_preco_corrente`, `_por_recuperar`, `GET /api/preco/simular`)
**sai por inteiro**.

### 3.3 Responsável pela caixa

`config.caixa_responsavel_id` (id de utilizador, ou ausente). Muda-se nas Definições
(com histórico). Sem responsável:
- não há sugestão de pagamento à caixa;
- `para_caixa`, `de_caixa` e `paga_pela_caixa` respondem 409
  `{"detail": "Ainda não há ninguém responsável pela caixa. Escolhe nas Definições."}`.

A migração **não** escolhe o responsável: escolhe-se na app depois do deploy.

### 3.4 Pagamentos

- **Lado de quem paga**: `pagador_id`, ou o responsável pela caixa se `de_caixa`.
- **Lado de quem recebe**: `recebedor_id`, ou o responsável pela caixa se
  `para_caixa`.
- Registar: qualquer pessoa, como pagadora. Saída da caixa (`de_caixa`): só o
  responsável, e sempre para uma pessoa (pode ser o próprio, para se reembolsar).
- Confirmar: o lado de quem recebe, enquanto activo e não confirmado.
- Anular: qualquer dos dois lados, enquanto activo e **não confirmado**.
- **Editar** (novo): o lado de quem paga, enquanto activo (confirmado ou não). Muda
  `valor_cent` e/ou o destino. **Editar um pagamento confirmado volta a pô-lo por
  confirmar** (`confirmada_em = NULL`), e o lado de quem recebe é avisado. Cada campo
  alterado fica no histórico.
- Num pagamento entre duas pessoas (sem caixa), pagador e recebedor nunca são a mesma
  pessoa. Com caixa, o responsável pode estar nos dois lados: reembolsar-se da caixa
  (`de_caixa` para si próprio) ou pôr dinheiro seu na caixa (`para_caixa`).
- **Quando o lado de quem paga e o de quem recebe são a mesma pessoa, o pagamento
  confirma-se sozinho** ao ser registado (`confirmada_em` = agora, sem push), e o
  mesmo depois de uma edição que acabe nesse estado. Não fica por confirmar na lista
  do próprio.
- `para_caixa` e `de_caixa` nunca juntos; `de_caixa` exige recebedor; `para_caixa` não
  tem recebedor.

### 3.5 Sugestão

Com saldo < 0 e responsável definido: pagar `−saldo` à Caixa. Senão, `null`. (A
sugestão deixa de apontar para pessoas: quem adiantou dinheiro é reembolsado pela
caixa.)

### 3.6 Histórico de alterações

Tabela `historico_alteracoes (id INTEGER PRIMARY KEY, entidade TEXT NOT NULL,
entidade_id INTEGER, campo TEXT NOT NULL, antes TEXT, depois TEXT, utilizador_id
INTEGER REFERENCES utilizadores(id), em TEXT NOT NULL)`.

- `entidade`: `transferencia`, `compra`, `config`, `cafe`.
- `entidade_id`: id da linha; `NULL` para `config`.
- `campo`: nome da coluna ou chave (`valor_cent`, `recebedor_id`, `para_caixa`,
  `custo_cent`, `capsulas`, `paga_pela_caixa`, `preco_cent`, `stock_baixo`,
  `caixa_responsavel_id`, `anulada`, `confirmada`).
- `antes`/`depois`: o valor como texto (`NULL` quando não havia).
- `utilizador_id`: quem fez; `NULL` = sistema (migração).
- Regista-se: cada campo alterado num PATCH ou PUT, e também anular e confirmar
  (`campo` = `anulada` / `confirmada`, `antes` = `"0"`, `depois` = `"1"`). Registar
  um movimento novo não gera linhas.

## 4. API

### 4.1 `GET /api/eu` (muda)

`preco_cent` = `config.preco_cent`. Entra `caixa`. `sugestao` muda de forma.
`por_confirmar` inclui, para o responsável, os pagamentos à caixa.

```json
{
  "preco_cent": 25,
  "saldo_cent": -250,
  "sugestao": {"para_caixa": true, "nome": "Caixa (Ana)", "valor_cent": 250},
  "caixa": {"responsavel_id": 2, "responsavel": "Ana", "dinheiro_cent": 771},
  "por_confirmar": [
    {"id": 17, "pagador_id": 5, "pagador": "Pedro", "para_caixa": true, "valor_cent": 500, "em": "..."}
  ],
  "...": "restantes campos iguais a 0.11.0"
}
```

`caixa` é `null` sem responsável. `sugestao` é `null` com saldo >= 0 ou sem
responsável.

### 4.2 `POST /api/transferencias` (muda)

Pedido: `{"recebedor_id": 1 | null, "para_caixa": false, "de_caixa": false, "valor_cent": 500}`.
`para_caixa` e `de_caixa` são opcionais (omissão = `false`).

- 201 → `{"id", "pagador_id" (null se de_caixa), "recebedor_id" (null se para_caixa),
  "para_caixa", "de_caixa", "valor_cent", "em"}`.
- 400: combinação inválida (ver 3.4) ou pagar a si próprio. 403: `de_caixa` por quem
  não é responsável. 404: recebedor inexistente. 409: sem responsável e caixa
  envolvida. 422: valor fora de 1..100000.
- Push `pagamento` ao lado de quem recebe: `Joana registou 5,00 € pagos à caixa` /
  `Joana registou 5,00 € pagos a ti` / `Ana registou 5,00 € da caixa para ti`. Se o
  lado de quem recebe for quem registou (o responsável a reembolsar-se), não há push.

### 4.3 `PATCH /api/transferencias/{id}` (novo)

Pedido: qualquer subconjunto de `{"valor_cent": 600, "recebedor_id": 3, "para_caixa": false}`.
Mudar de "para a caixa" para uma pessoa: `{"para_caixa": false, "recebedor_id": 3}`; o
inverso: `{"para_caixa": true, "recebedor_id": null}`.

- 200 → `{"ok": true}`. 403: não é o lado de quem paga. 404. 409: anulada. 400:
  combinação inválida ou nada muda. 422: valor fora do intervalo.
- Grava uma linha de histórico por campo alterado; se estava confirmado,
  `confirmada_em = NULL` e mais uma linha (`campo` = `confirmada`, `"1"` → `"0"`).
- Push `pagamento` ao lado de quem recebe (o novo, e o antigo se mudou):
  `Joana alterou um pagamento: 5,00 € → 6,00 €`.

### 4.4 Confirmar e anular (mudam)

Mesmas rotas e respostas; os lados passam a seguir 3.4 (o responsável actua pela
caixa). Ambos gravam linha de histórico.

### 4.5 `GET /api/transferencias` (novo): pagamentos de toda a gente

Sem parâmetros. Mais recentes primeiro, até 200.

```json
{"transferencias": [
  {"id": 18, "pagador_id": 3, "pagador": "Joana", "recebedor_id": null, "recebedor": "Caixa",
   "para_caixa": true, "de_caixa": false, "valor_cent": 500, "em": "...",
   "confirmada_em": null, "anulada_em": null, "anulada_por": null,
   "editada": false, "pode_editar": true, "pode_confirmar": false, "pode_anular": true}
]}
```

`pagador` é `"Caixa"` quando `de_caixa`; `recebedor` é `"Caixa"` quando `para_caixa`.
`editada` = há linhas de histórico com `campo` em (`valor_cent`, `recebedor_id`,
`para_caixa`) para este pagamento. `pode_*` para quem pede, segundo 3.4.

### 4.6 `GET /api/historico-alteracoes?entidade=transferencia&id=18` (novo)

`entidade` obrigatória; `id` obrigatório excepto para `config`. Mais antigas primeiro.

```json
{"alteracoes": [
  {"campo": "valor_cent", "antes": "500", "depois": "600", "utilizador": "Joana", "em": "..."}
]}
```

`utilizador` é `null` para alterações do sistema.

### 4.7 `GET /api/movimentos` (muda)

Itens de `transferencias` ganham `para_caixa`, `de_caixa`, `editada`; `outro` é
`"Caixa"` quando o outro lado é a caixa, e `outro_id` fica `null`. Itens de `compras`
ganham `paga_pela_caixa` e `editada`. O resto igual.

### 4.8 `GET /api/escritorio` (muda)

Sai `pote`. Entra `caixa`. Cada compra ganha `paga_pela_caixa` e `editada`.
`preco_cent` = config.

```json
{
  "caixa": {"responsavel_id": 2, "responsavel": "Ana", "dinheiro_cent": 771,
            "por_receber_cent": 1875, "fundo_cent": 1391},
  "...": "restantes campos iguais a 0.11.0"
}
```

Sem responsável: `responsavel_id` e `responsavel` a `null`, os três valores
calculados na mesma.

### 4.9 Compras (mudam)

- `POST /api/compras`: `{"capsulas", "custo_cent" (0..1000000; 0 = oferta),
  "paga_pela_caixa": false, "nota"}`. Com `custo_cent = 0`, `paga_pela_caixa` grava
  sempre 0. Com `paga_pela_caixa = true` e `custo_cent > 0`, 403 se quem pede não for
  quem guarda a caixa (com MB WAY o dinheiro fica na conta dessa pessoa).
- `PATCH /api/compras/{id}`: qualquer subconjunto de `{"custo_cent", "capsulas",
  "paga_pela_caixa"}`, com `custo_cent` em **0..1000000, como no POST** (0 torna a
  compra numa oferta e força `paga_pela_caixa = 0`). Quem: quem registou, ou toda a
  gente se não tiver autor. 409 se `capsulas` deixar o stock negativo. Histórico por
  campo. Põe `custo_estimado = 0` quando muda o custo. Trocar `paga_pela_caixa` de
  falso para verdadeiro tem a mesma regra do POST (403 se quem pede não guarda a
  caixa); uma compra já paga pela caixa continua editável nos outros campos por quem
  a pode editar hoje, mesmo sem guardar a caixa.
- `GET /api/preco/simular`: **sai** (404).

### 4.10 `PUT /api/config` (muda)

`{"preco_cent"?, "stock_baixo"?, "caixa_responsavel_id"?}`. `caixa_responsavel_id`
tem de existir (404) ou ser `null` para tirar. Qualquer pessoa. Histórico por chave
que mude de valor (entidade `config`).

### 4.11 `GET /api/config` (novo)

`{"preco_cent": 25, "stock_baixo": 16, "caixa_responsavel_id": 2, "editada": true}`,
para o formulário das Definições e para o botão de histórico (`entidade=config`).

## 5. Ecrãs

### 5.1 Café

- Cartão de saldo igual, com a sugestão `Sugestão: paga 2,50 € à caixa (Ana)`.
- Formulário de pagamento: **A quem** tem primeiro `Caixa (Ana)`, escolhida por
  omissão, e depois as pessoas. Sem responsável, a opção da caixa não aparece.
- Para quem guarda a caixa, uma linha a mais no cartão: `Caixa contigo: 7,71 €`, e os
  pagamentos à caixa entram em "por confirmar" com `Joana → caixa · 5,00 € · hoje`.
- Tudo continua a caber em 375×812 sem scroll, com o formulário aberto.

### 5.2 Histórico → Dinheiro

Como em 0.11.0, com `Pagaste 5,00 € à caixa`, `Recebeste 5,00 € da caixa`, uma
marca `editado` (toque mostra o histórico, ver 5.4) e **Editar** nos pagamentos que
fiz e continuam activos.

### 5.3 Escritório

- Bloco **Caixa**: `Caixa com Ana: 7,71 € em dinheiro · 18,75 € por receber · fundo
  13,91 €`. Sem responsável: `Ninguém guarda a caixa. Escolhe nas Definições.`
- Tabela por pessoa igual (Saldo). Para o responsável, junto de cada saldo positivo,
  o botão **Reembolsar** abre o formulário de pagamento com `de_caixa` e essa pessoa
  como destino.
- Secção nova **Pagamentos**: a lista de 4.5, uma linha por pagamento:
  `Joana → Caixa · 5,00 € · 22/09`, estado (`por confirmar`, `confirmado`,
  `anulado`), botões conforme `pode_*` (✓, Não recebi/Anular, Editar) e a marca
  `editado`.
- Editar um pagamento: formulário na própria linha, com valor e destino (caixa ou
  pessoa), Guardar e Cancelar.
- Entradas de cápsulas: o formulário ganha **Paga com**: `Caixa` / `Do meu bolso` /
  `Oferta` (oferta põe o custo a 0 e bloqueia o campo). Sai a pré-visualização do
  preço. Cada entrada mostra `oferta`, `pela caixa` ou `do bolso de Ana`, a marca
  `editado`, e **Editar** (custo, cápsulas, paga com) onde `pode_editar`.
- Definições: **Preço por café (€)**, limiar de aviso, **Quem guarda a caixa**
  (lista de pessoas e "ninguém"), e um `ver alterações` que abre o histórico de
  `config`.

### 5.4 Histórico de uma alteração

Tocar em `editado` (ou `ver alterações`) expande, por baixo da linha, uma lista:
`22/09 14:10 · Joana · valor 5,00 € → 6,00 €`. Nomes de campos em português:
valor, destino, custo, cápsulas, paga com, preço por café, limiar, responsável pela
caixa, anulado, confirmado. Valores em cêntimos mostram-se em euros; ids de pessoa
mostram o nome (`null` → `Caixa` para destino, `ninguém` para responsável);
`utilizador` `null` mostra `sistema`.

### 5.5 Service worker

`CACHE_VERSION` → `v8`, e `?v=8` nos recursos.

## 6. Migração (em `db.init()`, idempotente)

1. `CREATE TABLE IF NOT EXISTS historico_alteracoes` (3.6).
2. `compras`: `ADD COLUMN paga_pela_caixa INTEGER NOT NULL DEFAULT 0` (com a guarda
   de `PRAGMA table_info`).
3. `transferencias`: se ainda não tiver a coluna `para_caixa`, **reconstruir** a
   tabela dentro da transacção (criar `transferencias_nova` com `pagador_id` e
   `recebedor_id` a aceitar NULL, `para_caixa` e `de_caixa INTEGER NOT NULL DEFAULT
   0`, os CHECKs de 3.4 e o `pagamento_origem_id UNIQUE`; copiar as linhas com os
   mesmos ids; apagar a antiga; renomear). O `SCHEMA` de uma base nova já cria a forma
   nova.
4. Cafés com `valor_cent = 0` passam a `config.preco_cent`, com uma linha de histórico
   por café (`entidade` `cafe`, `campo` `valor_cent`, `antes` `"0"`, `utilizador_id`
   NULL).
5. Nada mais muda de dados: as compras ficam como estão (a oferta de 0,01 € corrige-se
   na app, com histórico), e o responsável pela caixa escolhe-se na app.

**Voltar atrás:** depois de reconstruída a `transferencias` (ids a aceitar NULL), a
imagem 0.11.0 já não serve para esta base. Voltar atrás = repor o backup tirado antes
do deploy da 0.12.0. O README diz isto.

## 7. Testes

- Invariante de 3.1 depois de uma sequência mista (cafés, compra do bolso, compra pela
  caixa, oferta, pagamento à caixa, a uma pessoa, saída da caixa, edição, anulação).
- **O caso real** reconstruído: oferta de 77 a custo 0, 95 cafés a 0,25 € repartidos
  como em produção (Ana 20, Júnior 25, João 17, Luís 12, Faria 11, Diogo M. 10),
  compra da Ana de 41 a 9,84 € do bolso (o valor registado hoje; se a Ana o corrigir
  para os 11,04 € que pagou, a caixa fica com 12,71 €), responsável = Ana; todos pagam
  à caixa o que devem → dinheiro na caixa = 18,75 € e o saldo da Ana = +4,84 €; a Ana
  reembolsa-se 4,84 € da caixa (confirma-se sozinho; o `por_confirmar` dela fica
  vazio) → caixa 13,91 €, fundo 13,91 €, saldos todos a 0.
- A oferta: `PATCH` de uma compra para `custo_cent = 0` grava `paga_pela_caixa = 0` e
  uma linha de histórico.
- Permissões: editar só o lado de quem paga; `de_caixa` só o responsável; confirmar e
  anular pelos lados certos; responsável a confirmar pagamentos à caixa.
- Editar confirmado volta a por confirmar; cada campo alterado fica no histórico com
  quem e quando; `editada` fica verdadeiro.
- Sem responsável: 409 nos três casos de caixa; `sugestao` e `caixa` a `null`.
- Preço fixo: mudar o preço não mexe nos cafés já gravados; histórico de config.
- Migração: base no esquema de 0.11.0 **construída à mão** (DDL de `d6db71d`), com
  uma transferência, cafés a 0 e a 25 → reconstrução com os mesmos ids, cafés a 0
  reparados com histórico, segunda execução sem duplicar nada.
- `GET /api/preco/simular` → 404.
- Contrato (depois de juntar): campos novos de `/api/eu`, `/api/escritorio`,
  `/api/transferencias`, `/api/historico-alteracoes`, `/api/config`, e os corpos de
  POST/PATCH extraídos do `app.js`, verificados por efeito.
- Fumo: o passo 7 passa a pagar à caixa, com um responsável definido por
  `PUT /api/config`.
