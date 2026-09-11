# Café do escritório — especificação

Data: 2026-09-11. Estado: auditado (ver §13); pronto para implementar.

## 1. Objectivo

Contabilizar os cafés (cápsulas) que cada pessoa do escritório bebe, para ao fim do
mês cada um pagar o seu consumo a quem vai comprar as cápsulas seguintes, e para se
saber com antecedência quando o stock acaba. Tem de ser **super simples**: marcar um
café demora um toque no telemóvel.

## 2. Requisitos (o que foi pedido)

| # | Requisito | Origem |
|---|-----------|--------|
| R1 | Login por PIN de 4 dígitos + algo que distinga contas | pedido inicial |
| R2 | Identificação: escolher o nome numa lista, depois o PIN; qualquer pessoa cria a sua conta | resposta à Q2 |
| R3 | Stock inicial de 50 cápsulas; cobra-se desde o primeiro café (não há cafés grátis) | resposta à Q3 |
| R4 | Preço por cápsula 0,25 € (configurável) | pedido inicial |
| R5 | Cada café bebido é marcado na conta de quem o bebeu | pedido inicial |
| R6 | Estimativa até ao fim do mês, em cápsulas e em euros, com base no histórico | pedido inicial |
| R7 | Pagamento ao fim do mês, de todos para quem vai comprar, com base no consumo do mês anterior | mensagem 09:44 |
| R8 | Fica registado que X pagou, marcado por quem recebeu | mensagem 09:44 |
| R9 | Pode ser preciso reabastecer a meio do mês; avisar quando o stock ≤ 16 (1 caixa) | mensagem 09:46 |
| R10 | Tendência: o stock chega ao fim do mês ou é preciso comprar entretanto; data prevista em que acaba | mensagem 09:48 |
| R11 | Ao registar, perguntar quantos cafés por dia a pessoa prevê beber, para haver dados desde o dia 1 | mensagem 09:51 |
| R12 | Web app em container para o k3s da empresa (não o do autor); código no GitHub | resposta à Q1 |

Sugestões aceites (não pedidas, mas baratas): desfazer o último café; mudar o PIN;
histórico mensal consultável; registo de compras com quem registou.

Fora de âmbito, de propósito: notificações push/e-mail, perfis de admin, integrações
de pagamento (MB Way etc.), gráficos, multi-escritório, i18n.

## 3. Modelo de dados (SQLite, um ficheiro)

```
utilizadores  id, nome (único, sem distinção de maiúsculas), pin_hash, cafes_dia (R11),
              tentativas, bloqueado_ate, criado_em
sessoes       token, utilizador_id, criado_em
cafes         id, utilizador_id, em (instante UTC), mes ('YYYY-MM' em hora local)
compras       id, utilizador_id (quem registou), capsulas, em, nota
pagamentos    id, mes, pagador_id, recebedor_id, capsulas, valor_cent, em
              UNIQUE(mes, pagador_id)
config        chave, valor   — preco_cent=25, stock_baixo=16
```

Regras:
- **Stock = Σ compras.capsulas − COUNT(cafes)**. É a única aritmética de stock. As 50
  cápsulas iniciais (R3) entram como a primeira compra registada na app.
- **Dinheiro em cêntimos inteiros**; nunca floats.
- **`cafes.mes` deriva-se da hora local (Europe/Lisbon, env `CAFE_TZ`)** no momento de
  gravar; um café às 00:30 do dia 1 pertence ao mês novo.
- **`pagamentos` guarda a fotografia** (cápsulas e valor no momento em que se marcou
  o pagamento, ao preço em vigor nesse momento). Depois de um mês ter qualquer
  pagamento, os cafés desse mês ficam congelados: "apagar o último café" recusa.
  Um café novo nunca cai num mês pago, porque só se fecham meses já terminados e
  os cafés gravam-se sempre com a hora actual.
- **Preço**: o que conta é o preço em vigor quando se marca o pagamento (não há
  histórico de preços). Mudar o preço a meio do mês afecta o mês inteiro ainda não
  pago; documentado no ecrã.
- **Apagar uma compra recusa (409) se deixasse o stock negativo.** Só quem registou
  a compra a pode apagar.

## 4. Autenticação (R1, R2)

- Ecrã inicial: grelha de 2 colunas com os nomes; com mais de 12 nomes aparece um
  campo "filtrar" por cima. Toca-se no nome → campo de PIN (`type=password`,
  `inputmode=numeric`, teclado nativo) que submete sozinho ao 4.º dígito.
- "Sou novo": nome, PIN (4 dígitos, confirmado duas vezes), "quantos cafés bebes por
  dia?" (R11; valor de 0 a 20, por defeito 1). O nome tem de ser único; o erro diz
  "junta o apelido".
- Sessão: cookie `httponly`, `samesite=lax`, `secure` quando o pedido chega por
  HTTPS (directo ou via `X-Forwarded-Proto`), **180 dias**. Na prática só se mete o
  PIN uma vez por dispositivo. Botão "Sair" sempre visível para telemóveis partilhados.
- Cabeçalhos: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Content-Security-Policy: default-src 'self'`.
- PIN guardado com PBKDF2-SHA256 + salt.
- Protecção mínima contra adivinhar (a lista de nomes é pública e há só 10 000 PINs):
  5 tentativas erradas seguidas → 60 s de bloqueio para esse utilizador.
- Mudar o PIN: pede o actual e o novo.
- Sem recuperação de PIN: quem se esquece, pede a quem tem acesso à BD (documentado
  no README, um comando `sqlite3`).

## 5. Ecrã "O meu café" (R5, R6, R9, R10, R11)

Ordem de cima para baixo (o que importa a quem está de pé com a chávena na mão):
1. Abas "Café" | "Escritório" no topo.
2. Botão grande **"Bebi um café ☕"**. O contador sobe logo (optimista) e o botão
   fica desactivado e a meio-tom até a resposta chegar; se falhar, o contador volta
   atrás e aparece o erro. Um segundo toque não duplica.
3. Stock, em faixa vermelha quando baixo: "Faltam 37 cápsulas · acabam ~24/09 ·
   chega ao fim do mês ✅" ou "⚠️ Stock baixo (12) · acabam ~17/09 · comprar antes
   do fim do mês".
4. Este mês: N cápsulas · X,XX €. **Estimativa até ao fim do mês**: N cápsulas ·
   X,XX € (§7).
5. A minha previsão: "~2/dia" com botão para alterar (R11).
6. Mês anterior: N cápsulas · X,XX € — **pago a Y em DD/MM** ou **por pagar**. Se
   Y é o próprio (a pessoa que recebe também bebe e marca-se a si própria), mostra
   "pago (és tu quem recebe)".
7. No fundo, discretos: **"Apagar o último café"** (pede confirmação; só os
   próprios; recusa se o mês já tem pagamentos), "Mudar PIN", "Sair".

## 6. Ecrã "Escritório" (R7, R8, R9, R10)

- Selector de mês (por defeito o actual; histórico para trás).
- Tabela: nome · cápsulas · € · estado de pagamento (só para meses fechados).
- Total do mês em cápsulas e €.
- Stock com o mesmo resumo do §5, mais o ritmo do escritório (cafés/dia útil).
- **Entradas de cápsulas**: "Registar entrada: N cápsulas" (+ nota opcional). Lista
  das últimas entradas com quem registou e quando; "apagar" só na linha de quem
  registou, e recusado se deixasse o stock negativo.
- **Fecho de mês** (qualquer mês já terminado): coluna "Estado" por pessoa —
  "por pagar" com botão **"Recebi o pagamento"**, ou "pago a <recebedor> em DD/MM".
  Quem carrega é o recebedor (a sessão diz quem é), incluindo na sua própria linha.
  "Anular" só aparece ao recebedor.
- Preço por cápsula e limiar de stock baixo editáveis aqui (qualquer conta; não há
  admin — confiança de escritório).

## 7. Cálculos (R6, R10, R11)

Tudo em **dias úteis** (segunda a sexta); o escritório não trabalha ao fim-de-semana.

- `decorridos` = dias úteis desde o dia 1 do mês **ou desde o dia em que a pessoa
  se registou, o que for mais tarde**, até hoje, inclusive. (Quem entra a dia 20 não
  é diluído por dias em que ainda não existia.)
- `restantes` = dias úteis de amanhã até ao último dia do mês.
- **Ritmo de uma pessoa** = `cafes_mes / decorridos` se `decorridos ≥ 5`; senão a
  previsão declarada `cafes_dia` (R11). Assim no dia 1 há sempre uma estimativa.
- **Estimativa pessoal** = `cafes_mes + ritmo × restantes`, arredondada ao inteiro
  mais próximo (0,5 sobe); em € é o inteiro × `preco_cent`.
- **Ritmo do escritório** = soma dos ritmos de todas as pessoas registadas
  (cada um com o seu `decorridos`).
- **Data em que o stock acaba** = a partir de amanhã, subtrai o ritmo do escritório
  por cada dia útil até chegar a zero. `None` se o ritmo é 0. Hoje, se stock ≤ 0.
- **Chega ao fim do mês** = data em que acaba > último dia do mês (ou `None`).
- **Stock baixo** = stock ≤ `stock_baixo` (16).

## 8. API (JSON, prefixo `/api`)

| Método | Rota | Autenticado | Faz |
|--------|------|-------------|-----|
| GET | /utilizadores | não | lista id+nome para o ecrã de entrada |
| POST | /registar | não | cria conta, abre sessão |
| POST | /login | não | valida PIN, abre sessão; 429 se bloqueado |
| POST | /logout | sim | apaga sessão |
| GET | /eu | sim | tudo o que o ecrã "O meu café" mostra |
| POST | /cafe | sim | marca um café |
| DELETE | /cafe/ultimo | sim | desfaz o último café próprio (409 se mês fechado) |
| PUT | /eu/previsao | sim | altera `cafes_dia` |
| PUT | /eu/pin | sim | muda o PIN |
| GET | /escritorio?mes= | sim | tabela do mês, stock, compras, meses disponíveis |
| POST | /compras | sim | regista entrada de cápsulas |
| DELETE | /compras/{id} | sim | apaga uma entrada; 403 se não foi quem registou; 409 se stock ficaria < 0 |
| POST | /pagamentos | sim | recebedor = sessão; 400 se mês ainda não terminou; 409 se já pago |
| DELETE | /pagamentos/{mes}/{pagador} | sim | só o recebedor |
| PUT | /config | sim | preco_cent, stock_baixo |

Erros: JSON `{"detail": "mensagem em português"}` com o código HTTP adequado; o
front-end mostra o `detail` tal e qual.

## 9. Front-end

HTML + CSS + JS puros, um ficheiro de cada, servidos pela própria app em `/`. Sem
build, sem framework. Mobile-first (o uso é ao pé da máquina, de telemóvel), mas
legível num monitor. Três vistas no mesmo `index.html` alternadas por JS: Entrada,
Café, Escritório. Tema claro simples; sem dependências externas (funciona numa rede
sem saída para a Internet). `manifest.json` (`display: standalone`), ícone SVG e
`apple-mobile-web-app-capable` para se poder pôr no ecrã inicial do telemóvel.

## 10. Operação (R12)

- **Stack**: Python 3.12, FastAPI, uvicorn, SQLite (stdlib). Sem ORM.
- **Container**: `python:3.12-slim`, utilizador não-root (uid 1000), `uvicorn` na
  porta 8000, BD em `/data/cafe.db` (env `CAFE_DB`), `CAFE_TZ=Europe/Lisbon` por
  defeito. A imagem slim não traz zoneinfo do sistema: o pacote pip `tzdata` vai
  em `requirements.txt` para o `ZoneInfo` funcionar.
- **Kubernetes** (`k8s/`, `kustomization.yaml`): `Namespace`, `PersistentVolumeClaim`
  (1 Gi, RWO, `storageClassName` omitido para usar a default do cluster),
  `Deployment` com **`replicas: 1` e `strategy: Recreate`** (SQLite num PVC RWO não
  suporta dois pods), `securityContext` com `runAsUser: 1000` e `fsGroup: 1000`
  (senão o PVC fica de root e o SQLite não escreve), requests 50m/128Mi, limits
  500m/256Mi, liveness e readiness em `GET /api/utilizadores`, `Service`, `Ingress`
  com host de exemplo `cafe.example.com`, classe omitida e bloco TLS/cert-manager
  comentado. `imagePullSecrets` comentado para o caso de a imagem ser privada. Sem
  assunções sobre túnel Cloudflare ou registry: a imagem é
  `ghcr.io/<conta>/cafe-escritorio:<tag>` como placeholder e o README explica como
  construir e publicar.
- **Backup**: em WAL há três ficheiros (`.db`, `.db-wal`, `.db-shm`); copiar só o
  `.db` perde dados. O README documenta `kubectl exec … python -c "sqlite3 backup"`
  (a API `Connection.backup` da stdlib, sem precisar do binário `sqlite3`) seguido
  de `kubectl cp`, e o restore. Avisa que a storage default do k3s (`local-path`)
  prende o volume ao nó. Nada automático.
- **Sem CI** nesta fase.

## 11. Testes

`pytest`:
- `tests/test_logic.py`: dias úteis, mês em hora local, ritmo declarado vs real,
  estimativa no dia 1 e no último dia, data de fim do stock, aviso de stock baixo.
- `tests/test_api.py` (TestClient, BD temporária): registo e nome duplicado; login
  certo, errado, bloqueio após 5; marcar/desfazer; desfazer recusado em mês pago;
  stock = compras − cafés; escritório por mês; pagamento por quem recebe, recusa em
  mês corrente, duplicado, anular só pelo recebedor; snapshot do pagamento não muda
  com cafés posteriores; config.

## 12. Entrega

Repositório GitHub **privado** `cafe-escritorio` na conta do autor, com README
(o que é, como correr localmente, como construir a imagem, como instalar no k8s,
como registar as 50 cápsulas iniciais, como repor um PIN, como fazer backup).

## 13. Auditoria do plano (2026-09-11)

Cinco revisões independentes (cobertura, modelo de dados, segurança, operação, UX).
Aceite e já reflectido nas secções acima:

- Stock nunca fica negativo por apagar uma entrada; só quem registou apaga (§3, §6).
- `decorridos` conta a partir do registo da pessoa, não do dia 1 (§7).
- Preço: o que vale é o do momento do fecho; a fotografia do pagamento congela-o (§3).
- Arredondamento explícito (§7).
- Cookie a 180 dias, `secure` atrás de HTTPS, cabeçalhos básicos (§4).
- Ecrã "Café" reordenado: botão → stock → números; "Apagar o último café" com
  confirmação no fundo; abas; PIN com teclado nativo; filtro de nomes acima de 12 (§4, §5).
- Textos: "Registar entrada", "Recebi o pagamento", "Apagar o último café" (§6).
- Recebedor também se marca a si próprio; texto claro nesse caso (§5, §6).
- `tzdata` via pip; `fsGroup`; requests/limits; readiness; backup em WAL com a API
  da stdlib; avisos sobre `local-path`, pull secret e TLS (§10).
- `manifest.json` + ícone SVG + meta iOS para "adicionar ao ecrã inicial" (§9).

Rejeitado, com razão:

- Admin/lista de nomes autorizados para mudar o preço — contraria "sem admin";
  confiança de escritório (R-fora de âmbito).
- Rate limit global por IP na app — é trabalho do Ingress; o bloqueio por
  utilizador já cobre o PIN.
- Histórico de preços por café — complexidade sem pedido; a regra do fecho chega.
- Ambiguidade da hora repetida na mudança para hora de Inverno — guardamos UTC, não
  há ambiguidade.
- Cookie a 30 dias — obrigaria a PIN todos os meses; 180 dias com "Sair" visível.
