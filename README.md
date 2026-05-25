# Monitor TSE — Pesquisas Presidenciais 2026

Agente que roda automaticamente todo dia às **09h (Brasília)** e detecta novas pesquisas presidenciais registradas no TSE.

## O que ele faz

1. Baixa o CSV oficial do TSE (atualizado diariamente)
2. Filtra apenas pesquisas presidenciais nacionais com amostra ≥ 1.000
3. Compara com o histórico do dia anterior
4. Se há pesquisas novas:
   - Abre uma **Issue** no GitHub com todos os detalhes
   - Envia um **e-mail** de notificação
5. Salva snapshot diário, histórico acumulado e relatório `.md`

## Arquivos gerados

```
data/
  historico.csv           → todas as pesquisas já vistas (acumulado)
  snapshot_YYYY-MM-DD.csv → pesquisas do dia
  novas_YYYY-MM-DD.json   → novas detectadas (lido pelo workflow)
relatorio_YYYY-MM-DD.md   → resumo legível do dia
```

## Filtros aplicados

| Critério | Valor |
|----------|-------|
| Cargo | Presidente da República |
| Abrangência | Nacional (UF = BR) |
| Amostra mínima | 1.000 entrevistados |
| Excluídas | Pesquisas de bairro/cidade única |

## Setup (veja SETUP.md para o passo a passo completo)

1. Criar o repositório no GitHub
2. Criar os arquivos conforme estrutura acima
3. Configurar os Secrets (e-mail)
4. Ativar o GitHub Actions

## Fonte dos dados

[Portal de Dados Abertos do TSE](https://dadosabertos.tse.jus.br/dataset/pesquisas-eleitorais-2026)
