# ☕ Café do escritório

App web mínima para contar os cafés (cápsulas) que cada pessoa bebe no escritório,
saber quando o stock acaba e acertar contas ao fim do mês. Um toque para marcar um
café, de telemóvel, ao lado da máquina.

- **Entrar**: escolhe-se o nome numa lista e mete-se um PIN de 4 dígitos. Qualquer
  pessoa cria a sua conta; ao criar, diz quantos cafés bebe por dia para as
  estimativas fazerem sentido desde o primeiro dia.
- **Café**: botão "Bebi um café"; total do mês em cápsulas e €; estimativa até ao fim
  do mês; stock, data prevista em que acaba, e aviso quando há ≤ 16 cápsulas (uma
  caixa) ou quando não chega ao fim do mês. Por baixo do botão, "Último café: hoje
  às 09:12", para acabar com o "já marquei o meu?".
- **Histórico**: calendário do mês com os cafés de cada dia (só os próprios); tocar
  num dia mostra as horas; setas para recuar nos meses. Cafés marcados sem rede
  aparecem logo, com "(por sincronizar)".
- **Escritório**: tabela do mês por pessoa (com histórico de meses), entradas de
  cápsulas, fecho de mês, preço por cápsula e limiar de aviso.

## Regras

- Cada cápsula custa **0,25 €** (configurável em Escritório → Definições).
- Cobra-se desde o primeiro café. **Stock = cápsulas registadas − cafés marcados.**
- **Fecho de mês**: no mês seguinte, cada pessoa paga o que bebeu a quem vai comprar
  as cápsulas. Essa pessoa carrega em "Recebi o pagamento" na linha de cada um
  (também na sua). Fica registado quem pagou, a quem e quando; só quem recebeu
  pode anular. Depois do primeiro pagamento de um mês, os cafés desse mês ficam
  congelados.
- O preço que conta é o que estiver em vigor quando se marca o pagamento.
- "Apagar o último café" só apaga os próprios; uma entrada de cápsulas só a apaga
  quem a registou, e nunca se o stock ficasse negativo.
- Estimativas em **dias úteis**: nos primeiros 5 dias úteis de histórico usa-se a
  previsão que a pessoa declarou; depois, a média real do mês.
- 5 PINs errados seguidos bloqueiam essa conta por 60 s. A sessão dura 180 dias no
  dispositivo; há botão "Sair" para telemóveis partilhados.
- Não há administrador: qualquer conta regista entradas e muda o preço. É uma app
  de confiança de escritório.

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
  nó onde o pod arrancou. Se o nó morrer, os dados vão com ele — faz backups.
- Upgrade: publica uma tag nova, muda `image:` e `kubectl apply -k k8s/`. O esquema
  da base de dados cria-se sozinho e é compatível para a frente (só `CREATE IF NOT
  EXISTS`).

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
app/logic.py     dias úteis, estimativas, data em que o stock acaba
app/static/      index.html, app.js, style.css (sem build, sem dependências)
tests/           pytest (lógica pura + API com TestClient)
k8s/             manifestos (kubectl apply -k k8s/)
docs/superpowers/specs/   especificação e auditoria
```
