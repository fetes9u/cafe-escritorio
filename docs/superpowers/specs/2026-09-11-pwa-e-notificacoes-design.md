# PWA e notificações push, especificação

Data: 2026-09-11. Altera a spec `2026-09-11-cafe-escritorio-design.md`.

## 1. Objetivo

Duas coisas pedidas: a app passa a ser instalável no telemóvel (PWA) e a equipa
recebe notificações quando alguém bebe um café, repõe cápsulas, paga, ou se regista.

## 2. O que isto reverte da spec original

A spec original excluía isto de propósito, e a revogação é deliberada:

- **"Fora de âmbito, de propósito: notificações push/e-mail"**: deixa de ser
  verdade, passa a ser o centro desta mudança.
- **"perfis de admin"** e **"Rejeitado, com razão: admin/lista de nomes"**:
  continuam válidos. O pedido inicial pedia aprovação de registos, foi retirado
  a favor de uma notificação de "entrou um colega novo". **A app continua sem
  administrador.** Ninguém aprova ninguém e qualquer conta continua a poder mudar
  o preço e registar cápsulas.

O ponto "confiança de escritório" mantém-se, portanto, intacto.

## 3. PWA

### 3.1 Instalabilidade

- `manifest.json` ganha ícones PNG reais: 192x192, 512x512, um 512 `maskable`,
  e 180x180 para `apple-touch-icon`. iOS ignora SVG no `apple-touch-icon`.
- Service worker com scope `/`. Servido em `/sw.js` na raiz, porque um ficheiro
  em `/static/sw.js` teria scope `/static/` e não controlaria a página da app.
- Cache apenas do app shell (`/`, CSS, JS, ícones), com nome de cache versionado e
  limpeza das versões antigas no `activate`.
- **Nunca cachear nada sob `/api`.** São dados por utilizador atrás de um cookie de
  sessão: uma resposta em cache mostraria a uma pessoa os dados de outra.

### 3.2 Instalação guiada

- Android: apanhar `beforeinstallprompt` e mostrar botão "Instalar".
- iOS: esse evento não existe. Detetar iOS fora de standalone e mostrar a dica
  "Partilhar, depois Adicionar ao ecrã principal", dispensável e persistida em
  `localStorage`.

Em iOS isto não é conveniência: **sem a PWA instalada não há notificações de todo.**

## 4. Notificações

### 4.1 Eventos

Cinco tipos, todos **ligados por omissão**, cada pessoa desliga o que não quiser:

| Chave | Dispara quando | Texto |
|---|---|---|
| `cafe` | alguém marca um café | "A Ana bebeu um café" |
| `compra` | alguém regista cápsulas | "O Rui repôs 50 cápsulas" |
| `pagamento` | alguém marca pagamento recebido | "A Ana pagou 3,75 EUR ao Rui" |
| `stock_baixo` | o stock cruza o limiar, ou chega a zero | "Restam 12 cápsulas" |
| `registo` | uma conta nova é criada | "A Marta juntou-se ao café" |

Regra fixa, sem opção: **ninguém recebe notificação dos seus próprios atos.**

`stock_baixo` dispara na **transição** para abaixo do limiar, não a cada café
abaixo dele, senão repete-se a cada café até alguém repor.

O ecrã de definições mostra os cinco interruptores em lugar visível. O padrão é
receber tudo, portanto desligar tem de ser fácil de encontrar.

### 4.2 Modelo de dados

```sql
CREATE TABLE IF NOT EXISTS subscricoes (
    endpoint TEXT PRIMARY KEY,
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id) ON DELETE CASCADE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    dispositivo TEXT,
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subscricoes_utilizador ON subscricoes(utilizador_id);

-- Só guarda o que a pessoa DESLIGOU. Ausência de linha significa ligado, que é
-- o padrão. Evita semear cinco linhas por cada conta nova.
CREATE TABLE IF NOT EXISTS notificacoes_desligadas (
    utilizador_id INTEGER NOT NULL REFERENCES utilizadores(id) ON DELETE CASCADE,
    evento TEXT NOT NULL,
    PRIMARY KEY (utilizador_id, evento)
);
```

O `endpoint` é a chave primária: é único global e pertence a um só dispositivo.
Uma pessoa com telemóvel e portátil tem duas linhas, e recebe nos dois.

### 4.3 API

| Método | Rota | Faz |
|---|---|---|
| GET | `/api/push/chave` | devolve a chave pública VAPID, ou 503 se o push não estiver configurado |
| POST | `/api/push/subscricoes` | grava endpoint, p256dh, auth para a sessão atual |
| DELETE | `/api/push/subscricoes` | remove a subscrição deste dispositivo |
| GET | `/api/notificacoes/preferencias` | os cinco eventos e se estão ligados |
| PUT | `/api/notificacoes/preferencias` | grava os que ficam desligados |

### 4.4 Envio

- `pywebpush` 2.5.0. Traz `aiohttp`, `cryptography`, `http-ece`, `requests` e
  `py-vapid`. É um salto real face ao `requirements.txt` atual (FastAPI e uvicorn),
  e foi aceite conscientemente.
- Envio em `BackgroundTasks` do FastAPI, nunca no caminho do pedido: um push
  service lento não pode atrasar o toque em "Bebi um café".
- Resposta **404 ou 410** do push service: apagar a subscrição, sempre.
- O texto e os dados vão cifrados ponta a ponta com as chaves do dispositivo. A
  Apple e a Google encaminham sem ler o conteúdo.

### 4.5 Chaves VAPID

- Par gerado uma vez, com `py-vapid`. Privada em `CAFE_VAPID_PRIVATE`, pública em
  `CAFE_VAPID_PUBLIC`, mais `CAFE_VAPID_CONTACTO` (um mailto).
- **Nunca commitadas.** Em produção, um Secret do Kubernetes montado como env.
- **Nunca rodadas sem intenção:** mudar a chave pública invalida todas as
  subscrições existentes e obriga toda a gente a subscrever de novo.
- Sem as variáveis definidas, a app **arranca à mesma** e funciona sem push. É o
  que permite correr localmente e nos testes sem segredos.

## 5. Operação

- O cluster precisa de saída HTTPS para `web.push.apple.com` e
  `fcm.googleapis.com`. Não é preciso nada a entrar: o fluxo é todo de saída.
- A app é só de LAN. A notificação chega ao telemóvel em qualquer sítio, porque
  vem pela internet, mas tocar nela só abre a app dentro da rede do escritório.
  É uma limitação aceite, não um defeito a corrigir.

## 6. Testes

- Rota `/sw.js` responde 200, com content-type de JavaScript e scope `/`.
- `manifest.json` é JSON válido e cada `src` de ícone existe em disco.
- Subscrever grava a linha; subscrever duas vezes com o mesmo endpoint não duplica.
- Um 410 simulado do push service apaga a subscrição.
- Quem desligou um evento não entra na lista de destinatários desse evento.
- O autor de um ato nunca entra na lista de destinatários.
- `stock_baixo` dispara na transição e não se repete a cada café abaixo do limiar.
- A app arranca e serve pedidos sem as variáveis VAPID definidas.

## 7. Fora de âmbito

- Notificações por email.
- Resumos diários ou agregação de cafés: decidido que cada café notifica, e quem
  não quiser desliga.
- Admin, aprovação de contas, suspensão de contas.
- Acesso à app de fora da rede da empresa.
