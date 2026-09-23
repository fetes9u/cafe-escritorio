# ☕ Café do escritório

App web mínima para contar os cafés (cápsulas) que cada pessoa bebe no escritório,
saber quando o stock acaba e quanto cada um deve ao pote. Um toque para marcar um
café, de telemóvel, ao lado da máquina.

- **Entrar**: escolhe-se o nome numa lista e mete-se um PIN de 4 dígitos. Qualquer
  pessoa cria a sua conta; ao criar, diz quantos cafés bebe por dia para as
  estimativas fazerem sentido desde o primeiro dia.
- **Café**: botão "Bebi um café"; total do mês em cápsulas e €; estimativa até ao fim
  do mês; stock, data prevista em que acaba, e aviso quando há ≤ 16 cápsulas (uma
  caixa) ou quando não chega ao fim do mês. Por baixo do botão, "Último café: hoje
  às 09:12", para acabar com o "já marquei o meu?". Um cartão com o saldo ("Deves
  2,50 €", "Tens 1,00 € a teu favor"), a sugestão de a quem pagar, o botão "Paguei
  por MB WAY" e os pagamentos que outros te fizeram, por confirmar.
- **Histórico**: calendário do mês com os cafés de cada dia (só os próprios); tocar
  num dia mostra as horas; setas para recuar nos meses. Cafés marcados sem rede
  aparecem logo, com "(por sincronizar)". Em "Dinheiro", o saldo e a lista de
  pagamentos, compras e totais de cada mês.
- **Escritório**: tabela do mês por pessoa com o saldo de cada um (com histórico de
  meses), o pote, entradas de cápsulas com o custo, e o limiar de aviso.

## Regras

- Cobra-se desde o primeiro café. **Stock = cápsulas registadas − cafés marcados.**
- **Saldo corrido, sem fecho de mês.** Cada pessoa tem um saldo contra o pote do
  escritório, calculado (nunca guardado) a partir de três movimentos: cada café
  desconta o seu preço, cada entrada de cápsulas credita o custo a quem a registou,
  e cada pagamento credita quem pagou e desconta quem recebeu. Saldo negativo é
  "Deves", mesmo para quem recebeu dinheiro dos outros: esse dinheiro é do pote.
- **Preço médio do armário.** Cada entrada de cápsulas regista o custo real. O
  preço de um café é o que falta recuperar (custos das compras menos o que os cafés
  já cobraram) a dividir pelas cápsulas que restam, arredondado ao cêntimo (0,5
  sobe). Calcula-se quando o servidor grava o café e fica nesse café para sempre;
  quando a última cápsula sai, o pote fica a zero ao cêntimo. Com o armário vazio,
  vale o preço do último café gravado; sem nenhum, 0,25 €. O preço não se edita.
- O **pote** (Escritório) é o que falta recuperar em cápsulas no armário, e é a
  soma dos saldos de toda a gente.
- **Pagamentos por MB WAY**: a app não paga nada, só regista. Quem pagou regista o
  valor (entre 0,01 € e 1000,00 €) e a quem; qualquer valor, a qualquer colega, e o
  que sobra fica a favor. O saldo muda logo e quem recebeu é avisado. Quem recebeu
  confirma (a partir daí é imutável) ou diz "Não recebi"; quem pagou pode anular
  enquanto não estiver confirmado. Anulado não volta atrás e deixa de contar; um
  engano num pagamento confirmado corrige-se com um pagamento no sentido contrário.
- **Sugestão de a quem pagar**, só para quem deve: a pessoa com o maior saldo a
  favor, e nunca mais do que ela tem a receber.
- Quem registou uma entrada de cápsulas pode corrigir-lhe o custo. As entradas
  anteriores aos saldos têm o custo estimado ao preço por cápsula que estava
  configurado, marcadas "custo estimado".
- "Apagar o último café" só apaga os próprios; uma entrada de cápsulas só a apaga
  quem a registou, e nunca se o stock ficasse negativo.
- Estimativas em **dias úteis**: nos primeiros 5 dias úteis de histórico usa-se a
  previsão que a pessoa declarou; depois, a média real do mês.
- 5 PINs errados seguidos bloqueiam essa conta por 60 s. A sessão dura 180 dias no
  dispositivo; há botão "Sair" para telemóveis partilhados.
- Não há administrador: qualquer conta regista entradas e muda o limiar de aviso. É
  uma app de confiança de escritório.

## Correr localmente

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
CAFE_DB=data/dev.db .venv/bin/uvicorn app.main:app --reload --reload-dir app
```

Abre <http://localhost:8000>. Variáveis: `CAFE_DB` (caminho do SQLite, por defeito
`data/cafe.db`), `CAFE_TZ` (por defeito `Europe/Lisbon`).

## Construir e publicar a imagem

```bash
docker build -t ghcr.io/CONTA/cafe-escritorio:0.1.0 .
docker push ghcr.io/CONTA/cafe-escritorio:0.1.0
```

Serve qualquer registry. Se for privado, cria um pull secret no namespace e
descomenta `imagePullSecrets` em `k8s/deployment.yaml`.

## Instalar no Kubernetes

O que mudar antes de aplicar:

1. `k8s/deployment.yaml` → `image:` (o registry e a tag que publicaste).
2. `k8s/ingress.yaml` → `host:`; se quiseres TLS, descomenta o bloco `tls` e a
   anotação do cert-manager (ou aponta para um secret com o certificado).
3. `k8s/pvc.yaml` → `storageClassName`, se não quiseres a default do cluster.

```bash
kubectl apply -k k8s/
kubectl -n cafe rollout status deploy/cafe
```

Notas para quem opera:

- **Uma réplica, `strategy: Recreate`, sempre.** A base de dados é um SQLite num
  PVC `ReadWriteOnce`; duas réplicas corrompem-na.
- O contentor corre como uid 1000 e o Deployment define `fsGroup: 1000` para o PVC
  ficar com as permissões certas.
- Em k3s a StorageClass default costuma ser `local-path`: o volume vive no disco do
  nó onde o pod arrancou. Se o nó morrer, os dados vão com ele: faz backups.
- Upgrade: publica uma tag nova, muda `image:` e `kubectl apply -k k8s/`. O esquema
  cria-se sozinho e o arranque converte uma base antiga no lugar (colunas novas e,
  na primeira vez, os dados do antigo fecho de mês: preços dos cafés, custos
  estimados das compras e pagamentos passados a transferências), sem duplicar nada
  num segundo arranque. Antes do upgrade para os saldos, tira um backup.
- **Voltar atrás não é suportado depois de haver uma transferência.** A imagem
  anterior aos saldos não vê a tabela `transferencias`, e ao regressar voltaria a
  converter qualquer "Recebi o pagamento" feito entretanto. A partir daí só se
  corrige com uma versão nova; a alternativa é repor o backup tirado antes do
  upgrade, perdendo o que se registou depois.

### Registar as cápsulas iniciais

Depois de criar a primeira conta, vai a **Escritório → Entradas de cápsulas** e
regista as 50 (ou as que houver). Não há stock "mágico": tudo o que entra é uma
entrada, tudo o que sai é um café.

### Backup e restore

O SQLite está em modo WAL (três ficheiros em `/data`). Não copies só o `cafe.db`;
usa a API de backup, que produz um ficheiro consistente:

```bash
kubectl -n cafe exec deploy/cafe -- python -c "import sqlite3; s=sqlite3.connect('/data/cafe.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()"
kubectl -n cafe cp $(kubectl -n cafe get pod -l app=cafe -o name | cut -d/ -f2):/data/backup.db ./cafe-$(date +%F).db
kubectl -n cafe exec deploy/cafe -- rm /data/backup.db
```

Restore: escala o deployment para 0, copia o ficheiro para `/data/cafe.db` (com
`kubectl cp` para um pod temporário que monte o PVC, ou directamente no nó em
`/var/lib/rancher/k3s/storage/pvc-…/`), apaga `cafe.db-wal` e `cafe.db-shm` se
existirem, e volta a escalar para 1.

### Repor o PIN de alguém

Não há recuperação pela app. Quem tiver acesso ao cluster:

```bash
kubectl -n cafe exec deploy/cafe -- python -c "
from app.main import _hash_pin; from app import db
db.init()
with db.conn() as c:
    c.execute('UPDATE utilizadores SET pin_hash=?, tentativas=0, bloqueado_ate=NULL WHERE nome=?', (_hash_pin('0000'), 'NOME'))
print('ok')"
```

A pessoa entra com `0000` e muda o PIN em "Mudar PIN".

## Estrutura

```
app/main.py      API (FastAPI) e ficheiros estáticos
app/db.py        esquema SQLite
app/logic.py     dias úteis, estimativas, data em que o stock acaba, preço médio
app/static/      index.html, app.js, style.css (sem build, sem dependências)
tests/           pytest (lógica pura + API com TestClient)
k8s/             manifestos (kubectl apply -k k8s/)
docs/superpowers/specs/   especificação e auditoria
```
