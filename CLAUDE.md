# CLAUDE.md

## Estilo de resposta

When reporting information to me, be extremely concise and sacrifice grammar for sake of concision.

## Modelos e esforço para programação (Claude 5.5)

Só usar `claude-haiku-5-5`, `claude-sonnet-5-5` e `claude-opus-5-5`. Não usar `claude-fable-5-1`.

| Tarefa | Modelo | Esforço inicial |
|---|---|---|
| Planear, arquitetura, refactor grande, multi-ficheiro | opus | medium; high para depurar bugs |
| Coding diário, feature com âmbito claro | sonnet | medium; high se difícil ou longa |
| Exploração, leitura, extração, tarefas mecânicas | haiku | medium; low só se simples |
| Revisão de código | modelo igual ou superior ao autor | medium |

- `xhigh` e `max` só com ganho medido em evals. `max` sobre-pensa.
- Haiku 5.5 só como subagente, não como principal de agentic coding complexo.
- Escalada: falha verificável → esforço +1 no mesmo modelo → modelo acima com high.
