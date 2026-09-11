# Café do escritório — plano de implementação

Spec: [`../specs/2026-09-11-cafe-escritorio-design.md`](../specs/2026-09-11-cafe-escritorio-design.md).
Cada tarefa indica as secções da spec e os requisitos (R#) que cobre. Ordem: de
dentro para fora — dados e cálculos, API, front-end, container, cluster, documentação.

Estado em 2026-09-11 10:05: as tarefas 1–3 têm um primeiro rascunho escrito antes
deste plano existir; serão revistas contra a spec auditada (§13), não assumidas
como certas. Cada tarefa abaixo lista o que a auditoria acrescentou.

## Tarefa 1 — Esqueleto e esquema da BD  (spec §3, §10)

- `requirements.txt` (inclui `tzdata`), `requirements-dev.txt`, `pyproject.toml`
  (pytest), `.gitignore`.
- `app/db.py`: `init()`, `conn()` (WAL, foreign keys), esquema completo da §3 com
  `INSERT OR IGNORE` dos dois valores de `config`, `get_config`/`set_config`.
- Verificação: `python -c "from app import db; db.init('x.db')"` cria as 6 tabelas.

## Tarefa 2 — Lógica pura de calendário e estimativas  (spec §7; R6, R10, R11)

- `app/logic.py`: `agora()`, `local()`, `mes_de()`, `mes_anterior()`, `limites_mes()`,
  `dias_uteis()`, `ritmo_diario()`, `estimativa_mes()`, `data_fim_stock()`,
  `resumo_stock()`.
- Auditoria: `decorridos` recebe a data de registo da pessoa; arredondamento 0,5 sobe.
- `tests/test_logic.py` cobre cada função, incluindo: mês em hora local à meia-noite
  de dia 1, dia 1 sem histórico (usa o declarado), pessoa registada a meio do mês,
  último dia do mês, ritmo 0, stock 0, aviso de stock baixo e "não chega ao fim do
  mês".

## Tarefa 3 — API  (spec §4, §8; R1, R2, R5, R7, R8, R9)

- `app/main.py` com todas as rotas da §8, na ordem: autenticação → "eu" → escritório.
- Regras a garantir explicitamente:
  - PIN: 4 dígitos, PBKDF2 com salt, bloqueio 5×/60 s (§4).
  - `/cafe`: grava `mes` pela hora local (§3).
  - `/cafe/ultimo`: 409 se o mês tiver pagamentos (§3).
  - `/pagamentos`: recebedor = sessão; snapshot de cápsulas e valor; 400 se
    `mes >= mes actual`; 409 se duplicado; anular só pelo recebedor (§6).
  - `/eu` e `/escritorio`: valor do mês vem do snapshot quando há pagamento (§3).
  - Auditoria: `DELETE /compras/{id}` 403 se não foi quem registou, 409 se stock < 0;
    cookie 180 dias + `secure` atrás de HTTPS; middleware com os três cabeçalhos;
    `/escritorio` devolve `criado_em` por pessoa para o ritmo individual.
- `tests/test_api.py` com todos os casos da §11 mais: apagar entrada de outro (403),
  apagar entrada que deixa stock negativo (409), cabeçalhos presentes, café novo
  nunca cai em mês pago. BD temporária por teste e relógio injectável
  (`logic.agora` substituível) para testar meses fechados sem depender da data real.

## Tarefa 4 — Front-end  (spec §5, §6, §9)

- `app/static/index.html`, `style.css`, `app.js`.
- Entrada: grelha de nomes (2 colunas, filtro acima de 12) → PIN com teclado nativo
  que submete ao 4.º dígito; "Sou novo" com nome, PIN×2, cafés/dia.
- Café (ordem da §5): abas, botão grande com contador optimista e bloqueio de toque
  duplo, faixa de stock (vermelha se baixo), este mês + estimativa, previsão
  editável, mês anterior (pago/por pagar/és tu quem recebe), fundo com "Apagar o
  último café" (confirmação), "Mudar PIN", "Sair".
- Escritório: selector de mês, tabela com coluna Estado, totais, stock + ritmo,
  entradas (registar, lista, apagar só as próprias), fecho de mês com "Recebi o
  pagamento"/"Anular", preço e limiar.
- `manifest.json`, `icon.svg`, meta iOS.
- Formatação: € com vírgula (`1,25 €`), datas `DD/MM`.
- Verificação: correr `uvicorn` localmente e percorrer os três ecrãs no browser em
  viewport de telemóvel; conferir os erros da API aparecem legíveis.

## Tarefa 5 — Container  (spec §10; R12)

- `Dockerfile`: `python:3.12-slim`, `pip install -r requirements.txt`, copia `app/`,
  utilizador não-root, `VOLUME /data`, `ENV CAFE_DB=/data/cafe.db CAFE_TZ=Europe/Lisbon`,
  `CMD uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- `.dockerignore`.
- Verificação: `docker build` e `docker run -p 8000:8000` com um volume; marcar um
  café; reiniciar o container; o café continua lá.

## Tarefa 6 — Manifestos Kubernetes  (spec §10; R12)

- `k8s/namespace.yaml`, `pvc.yaml`, `deployment.yaml` (replicas 1, Recreate,
  `runAsUser`/`fsGroup` 1000, requests 50m/128Mi, limits 500m/256Mi, liveness e
  readiness em `/api/utilizadores`, env `CAFE_DB`, `CAFE_TZ`, `imagePullSecrets`
  comentado), `service.yaml`, `ingress.yaml` (host placeholder, TLS comentado).
- `k8s/kustomization.yaml` para `kubectl apply -k k8s/`.
- Verificação: `kubectl apply -k k8s/ --dry-run=client`.

## Tarefa 7 — README e entrega  (spec §12)

- README com: o que é; regras (preço, fecho de mês, stock baixo, quem pode apagar o
  quê); correr localmente; construir e publicar a imagem; instalar no k8s (o que
  mudar: host, imagem, storage class, pull secret, TLS; aviso sobre `local-path`);
  registar as 50 cápsulas iniciais; repor um PIN (comando Python, não em claro);
  backup/restore em WAL com `Connection.backup` + `kubectl cp`.
- Commits por tarefa; repositório privado `cafe-escritorio` no GitHub; push.

## Fora do plano

Ver §2 da spec (fora de âmbito). Se alguma tarefa revelar complexidade escondida,
parar e rever a spec antes de continuar.
