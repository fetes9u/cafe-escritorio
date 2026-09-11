# Offline e sincronização, especificação

Data: 2026-09-11. Continua `2026-09-11-pwa-e-notificacoes-design.md`.

## 1. Objetivo

A app passa a aceitar o registo de um café sem rede e a sincronizar quando a
ligação voltar, e o ecrã passa a mostrar estado real offline, não um vazio.

O caso que importa é concreto: a pessoa está de pé à máquina, o WiFi da cozinha
é fraco, carrega no botão e vai-se embora. O café tem de ficar registado, com a
hora a que foi bebido, e tem de aparecer uma vez só.

## 2. Decisões tomadas antes de escrever código

| Decisão | Escolha | Porquê |
|---|---|---|
| Âmbito de escrita offline | Só o café e o desfazer | Repor stock e pagar são acções feitas sentado, com rede. O café é o único que se faz de pé à máquina. |
| Estado offline | Fotografia do último estado, por utilizador | Um botão que aceita o toque num ecrã vazio é meia funcionalidade. |
| Background Sync API | **Rejeitada** | Não existe no iOS. O caminho pelo evento `online` e pela reabertura tem de existir na mesma, e a Background Sync seria um segundo caminho exercitado só em metade dos telemóveis. |
| Notificação de café sincronizado tarde | Não notifica se chegou com mais de 15 minutos de atraso | É notícia velha. O `stock_baixo` notifica sempre, porque descreve o estado actual e não um acontecimento passado. |

## 3. Idempotência, e a migração que ela obriga

O cliente gera um `crypto.randomUUID()` por café e envia-o. Sem isto, uma fila
que reenvia duplica cafés: a rede fraca da cozinha é exactamente onde a resposta
se perde **depois** de o servidor ter gravado. Corrige também um defeito que já
existe hoje, sem offline nenhum: dois toques numa ligação má contam dois cafés.

**A coluna nova não chega sozinha à produção.** O `db.py` é uma string de
`CREATE TABLE IF NOT EXISTS` e não tem mecanismo de migração. A base de dados de
produção tem cafés reais num PVC `local-path` com `reclaimPolicy: Delete`, logo
não há "apagar e recriar". A coluna entra dentro do `init()`, assim:

```python
# migração: a DDL acima só corre em base de dados nova.
cols = {r["name"] for r in c.execute("PRAGMA table_info(cafes)")}
if "cliente_id" not in cols:
    c.execute("ALTER TABLE cafes ADD COLUMN cliente_id TEXT")
c.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS cafes_cliente_id "
    "ON cafes(cliente_id) WHERE cliente_id IS NOT NULL"
)
```

O índice é **parcial** de propósito: as linhas que já existem ficam com
`cliente_id NULL` e não podem colidir umas com as outras.

## 4. A hora do café, e o mês já pago

O cliente envia o instante real. Sem isso, cinco cafés da manhã aparecem todos
às 14h, quando a ligação voltar.

O servidor não aceita às cegas, porque a data decide o mês e o mês decide quem
paga o quê:

1. Aceita `em` se estiver entre `agora - 7 dias` e `agora + 5 min`. Fora disso,
   usa `agora`.
2. **Se o mês resultante já tiver pagamento dessa pessoa, usa `agora`.**

O ponto 2 não é zelo: o `valor_cent` de um mês pago é uma fotografia gravada no
momento do pagamento, mas a contagem de cafés é recalculada a cada leitura. Um
café a entrar num mês já pago fica invisivelmente por cobrar e nada na app mostra
a divergência. O `DELETE /api/cafe/ultimo` já recusa mexer num mês com
pagamentos; esta regra é a mesma ideia do lado da escrita.

## 5. Contrato, ao nível do campo

Esta secção é normativa. Os nomes abaixo são os nomes reais, nos dois lados.
Três defeitos em produção nesta app vieram de duas metades a escolherem nomes
diferentes para o mesmo campo.

### 5.1 `POST /api/cafe`

Corpo, **todos os campos opcionais** (um cliente antigo em cache continua a
funcionar sem corpo nenhum):

```json
{
  "cliente_id": "9f1c0b3e-5a7d-4c2e-9b11-2a3f4d5e6c70",
  "em": "2026-09-11T09:03:12.000Z"
}
```

- `cliente_id`: string UUID. Se já existir um café com este `cliente_id`, **não
  insere, não notifica** e responde `200`.
- `em`: instante UTC ISO-8601. Regras da secção 4.
- Café novo responde `201`. As duas respostas têm o mesmo corpo:

```json
{"ok": true, "cliente_id": "<o mesmo>", "em": "<o instante aceite pelo servidor>", "duplicado": false}
```

O cliente trata `200` e `201` como sucesso e usa o `em` devolvido, que pode
diferir do enviado, para actualizar a fotografia local.

### 5.2 IndexedDB no cliente

Base `cafe-offline`, versão 1, dois object stores:

| Store | keyPath | Registo |
|---|---|---|
| `fila` | `cliente_id` | `{cliente_id, tipo: "cafe", em, criado_em}` |
| `instantaneos` | `chave` | `{chave, utilizador_id, dados, em}` |

`chave` é `"eu:<utilizador_id>"` ou `"escritorio:<utilizador_id>"`. `dados` é a
resposta JSON tal como veio.

## 6. Fotografia do estado, e a regra que não se mexe

A app guarda a última resposta de `/api/eu` e `/api/escritorio` em IndexedDB,
depois de uma resposta autenticada bem sucedida.

**A regra do `sw.js` de nunca cachear `/api` mantém-se intacta.** A distinção é
o que impede que numa máquina partilhada alguém veja os dados de quem entrou
antes: a cache HTTP do service worker não conhece sessões, a fotografia é
escrita pela app, guardada por `id` de utilizador e **apagada no logout**.

O `/api/escritorio` tem os dados de toda a gente, logo a chave por utilizador e
a limpeza no logout não são detalhe, são a condição para isto ser aceitável.

## 7. Comportamento visível

- Sem rede, o botão de café aceita o toque, o número sobe na hora e aparece
  "1 café por sincronizar".
- O desfazer offline **remove o café da fila**, não enfileira um pedido de
  apagar. Um café que nunca chegou ao servidor não precisa de ser apagado lá.
- O resto do ecrã mostra a fotografia, com uma marca clara de que é o último
  estado conhecido e não o estado de agora.
- A fila esvazia-se no evento `online`, quando a app volta a ficar visível
  (`visibilitychange`), e ao arrancar depois do login.

## 8. Testes

Além dos testes de cada lado:

- **Contrato:** extrair do `app.js` o nome de cada campo que o cliente envia no
  `POST /api/cafe` e confirmar contra o que o servidor aceita, pelo mesmo padrão
  já usado em `tests/test_contrato_api.py`.
- **Idempotência:** o mesmo `cliente_id` enviado duas vezes dá um café, a segunda
  resposta é `200` e **não** dispara notificação.
- **Migração:** abrir uma base de dados criada com o esquema antigo, correr
  `init()`, e confirmar que a coluna e o índice aparecem e que as linhas
  existentes sobrevivem. Este é o teste que protege os cafés reais.
- **Mês pago:** um café com `em` dentro de um mês já pago é gravado com `agora`.
- **Notificação tardia:** um café com `em` de há duas horas não gera push de
  `cafe`; um que cruze o limiar de stock gera `stock_baixo` na mesma.
