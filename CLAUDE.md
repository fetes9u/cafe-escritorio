# CLAUDE.md

## Estilo de resposta

When reporting information to me, be extremely concise and sacrifice grammar for sake of concision.

## Modelos e esforço para programação

Usar sempre a versão mais recente de opus, sonnet e haiku (aliases `opus`, `sonnet`, `haiku` no parâmetro `model`). Não usar fable.

| Tarefa | Modelo | Esforço inicial |
|---|---|---|
| Planear, arquitetura, refactor grande, multi-ficheiro | opus | medium; high para depurar bugs |
| Coding diário, feature com âmbito claro | sonnet | medium; high se difícil ou longa |
| Exploração, leitura, extração, tarefas mecânicas | haiku | medium; low só se simples |
| Revisão de código | modelo igual ou superior ao autor | medium |

- `xhigh` e `max` só com ganho medido em evals. `max` sobre-pensa.
- Haiku só como subagente, não como principal de agentic coding complexo.
- Escalada: falha verificável → esforço +1 no mesmo modelo → modelo acima com high.
