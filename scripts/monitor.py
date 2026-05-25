"""
monitor.py — Agente diário de monitoramento de pesquisas presidenciais no TSE.
Executa via GitHub Actions todo dia às 09h Brasília.
"""

import hashlib
import io
import json
import logging
import os
import re
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

# ─── Configuração ──────────────────────────────────────────────────────────────

URL_TSE = (
    "https://cdn.tse.jus.br/estatistica/sead/odsele/"
    "pesquisa_eleitoral/pesquisa_eleitoral_2026.zip"
)
ARQUIVO_BRASIL = "pesquisa_eleitoral_2026_BRASIL.csv"

ROOT     = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

HISTORICO_CSV = DATA_DIR / "historico.csv"
HOJE          = date.today()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ─── Filtros ───────────────────────────────────────────────────────────────────
#
# Com base na análise do CSV do TSE (25/05/2026):
#
#   DS_CARGO == 'Presidente'     → apenas cargo presidencial
#   SG_UF    == 'BR'             → registro nacional, não estadual
#   QT_ENTREVISTADO >= 1000      → amostra mínima aceitável
#   DS_DADO_MUNICIPIO            → excluir pesquisas de bairro/cidade única

PALAVRAS_EXCLUIR_MUNICIPIO = [
    r"\bbairr",
    r"zona urbana",
    r"município de ",
    r"cidade de ",
    r"\bzona rural\b",
]

INSTITUTOS_CONHECIDOS = [
    "QUAEST", "DATAFOLHA", "ATLASINTEL", "ATLAS INTEL",
    "PARANA PESQUISAS", "REAL TIME", "FUTURA", "NEXUS", "FSB",
    "MDA", "GERP", "IDEIA", "VERITA", "IPEC", "IPESPE",
    "PODERDATA", "RANKING BRASIL", "100 CIDADES", "BOAS IDEIAS",
    "JOTA", "ANOVA", "NEOKEMP", "DOXA", "ECONOMETRICA",
]

# ─── 1. Download ───────────────────────────────────────────────────────────────

def baixar() -> pd.DataFrame:
    log.info(f"Baixando ZIP do TSE...")
    r = requests.get(URL_TSE, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        with zf.open(ARQUIVO_BRASIL) as f:
            df = pd.read_csv(f, sep=";", encoding="latin1", low_memory=False)
    log.info(f"  {len(df)} registros totais")
    return df

# ─── 2. Filtrar pesquisas presidenciais nacionais ──────────────────────────────

def filtrar(df: pd.DataFrame) -> pd.DataFrame:
    # Cargo = Presidente
    mask = df["DS_CARGO"] == "Presidente"
    # UF nacional
    mask &= df["SG_UF"] == "BR"
    # Amostra mínima
    qt = pd.to_numeric(df["QT_ENTREVISTADO"], errors="coerce").fillna(0)
    mask &= qt >= 1000
    # Sem pesquisas de bairro/cidade
    mun = df["DS_DADO_MUNICIPIO"].fillna("").str.lower()
    for p in PALAVRAS_EXCLUIR_MUNICIPIO:
        mask &= ~mun.str.contains(p, regex=True, na=False)

    resultado = df[mask].copy()
    log.info(f"  {len(resultado)} pesquisas presidenciais nacionais após filtros")
    return resultado

# ─── 3. Enriquecer ─────────────────────────────────────────────────────────────

def enriquecer(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Custo numérico
    df["custo"] = pd.to_numeric(
        df["VR_PESQUISA"].astype(str).str.replace(",", "."), errors="coerce"
    )

    # Datas
    for col_orig, col_novo in [
        ("DT_INICIO_PESQUISA", "campo_inicio"),
        ("DT_FIM_PESQUISA",    "campo_fim"),
        ("DT_DIVULGACAO",      "divulgacao"),
        ("DT_REGISTRO",        "registro"),
    ]:
        df[col_novo] = pd.to_datetime(df[col_orig], errors="coerce").dt.date

    # Duração do campo
    df["duracao_dias"] = (
        pd.to_datetime(df["DT_FIM_PESQUISA"]) -
        pd.to_datetime(df["DT_INICIO_PESQUISA"])
    ).dt.days

    # Instituto (nome fantasia ou razão social)
    df["instituto"] = (
        df["NM_EMPRESA_FANTASIA"].fillna(df["NM_EMPRESA"]).str.strip()
    )

    # Metodologia resumida
    m = df["DS_METODOLOGIA_PESQUISA"].fillna("").str.lower()
    df["metodologia"] = "presencial"
    df.loc[m.str.contains(r"telefon|cati|capi",              regex=True), "metodologia"] = "telefone"
    df.loc[m.str.contains(r"online|web|internet|eletrônico|formulário", regex=True), "metodologia"] = "online"
    df.loc[m.str.contains(r"ura|robocall|automatiz",         regex=True), "metodologia"] = "URA"

    # Instituto conhecido?
    up = df["instituto"].str.upper().fillna("")
    df["conhecido"] = up.apply(
        lambda x: any(k in x for k in INSTITUTOS_CONHECIDOS)
    )

    # Pesquisa própria?
    df["propria"] = df["ST_PESQUISA_PROPRIA"] == "S"

    return df

# ─── 4. Colunas do histórico ────────────────────────────────────────────────────

COLUNAS = [
    "NR_PROTOCOLO_REGISTRO",
    "instituto",
    "registro",
    "campo_inicio",
    "campo_fim",
    "divulgacao",
    "QT_ENTREVISTADO",
    "custo",
    "metodologia",
    "duracao_dias",
    "propria",
    "conhecido",
]

# ─── 5. Detectar novas ─────────────────────────────────────────────────────────

def protocolos_vistos() -> set:
    if not HISTORICO_CSV.exists():
        return set()
    df = pd.read_csv(HISTORICO_CSV, usecols=["NR_PROTOCOLO_REGISTRO"])
    return set(df["NR_PROTOCOLO_REGISTRO"].astype(str))

def detectar_novas(df: pd.DataFrame, vistos: set) -> pd.DataFrame:
    mask = ~df["NR_PROTOCOLO_REGISTRO"].astype(str).isin(vistos)
    novas = df[mask].copy()
    log.info(f"  {len(novas)} pesquisas NOVAS detectadas")
    return novas

def atualizar_historico(df: pd.DataFrame) -> None:
    if HISTORICO_CSV.exists():
        existente = pd.read_csv(HISTORICO_CSV)
        combinado = pd.concat([existente, df[COLUNAS]], ignore_index=True)
        combinado = combinado.drop_duplicates(subset=["NR_PROTOCOLO_REGISTRO"])
    else:
        combinado = df[COLUNAS].copy()
    combinado.to_csv(HISTORICO_CSV, index=False, encoding="utf-8")
    log.info(f"  Histórico: {len(combinado)} pesquisas totais")

# ─── 6. Snapshot diário ────────────────────────────────────────────────────────

def salvar_snapshot(df: pd.DataFrame) -> None:
    caminho = DATA_DIR / f"snapshot_{HOJE}.csv"
    df[COLUNAS].to_csv(caminho, index=False, encoding="utf-8")
    log.info(f"  Snapshot salvo: {caminho.name}")

# ─── 7. JSON de novas (lido pelo workflow para criar a Issue) ──────────────────

def salvar_json(novas: pd.DataFrame) -> Path:
    registros = []
    for _, r in novas.sort_values("registro", ascending=False).iterrows():
        registros.append({
            "protocolo":   str(r["NR_PROTOCOLO_REGISTRO"]),
            "instituto":   str(r["instituto"]),
            "registro":    str(r["registro"]),
            "campo_inicio": str(r["campo_inicio"]),
            "campo_fim":   str(r["campo_fim"]),
            "divulgacao":  str(r["divulgacao"]),
            "amostra":     int(r["QT_ENTREVISTADO"]) if pd.notna(r["QT_ENTREVISTADO"]) else None,
            "custo":       float(r["custo"]) if pd.notna(r["custo"]) else None,
            "metodologia": str(r["metodologia"]),
            "conhecido":   bool(r["conhecido"]),
        })

    payload = {
        "data":   str(HOJE),
        "total":  len(novas),
        "novas":  registros,
    }

    caminho = DATA_DIR / f"novas_{HOJE}.json"
    caminho.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"  JSON salvo: {caminho.name}")
    return caminho

# ─── 8. Relatório Markdown ─────────────────────────────────────────────────────

def gerar_relatorio(df: pd.DataFrame, novas: pd.DataFrame) -> None:
    linhas = []
    linhas.append(f"# Monitoramento TSE — {HOJE.strftime('%d/%m/%Y')}")
    linhas.append(f"")
    linhas.append(f"| | |")
    linhas.append(f"|---|---|")
    linhas.append(f"| Total de pesquisas presidenciais nacionais | **{len(df)}** |")
    linhas.append(f"| Pesquisas novas hoje | **{len(novas)}** |")
    linhas.append(f"")

    # Novas pesquisas
    if len(novas) > 0:
        linhas.append("## 🆕 Novas pesquisas")
        linhas.append("")
        for _, r in novas.sort_values("registro", ascending=False).iterrows():
            linhas.append(f"### {r['instituto']}")
            linhas.append(f"- **Protocolo:** `{r['NR_PROTOCOLO_REGISTRO']}`")
            linhas.append(f"- **Registrado no TSE:** {r['registro']}")
            linhas.append(f"- **Campo:** {r['campo_inicio']} → {r['campo_fim']} ({r['duracao_dias']} dias)")
            linhas.append(f"- **Divulgação prevista:** {r['divulgacao']}")
            linhas.append(f"- **Amostra:** {int(r['QT_ENTREVISTADO']):,} entrevistados")
            custo = f"R$ {r['custo']:,.0f}" if pd.notna(r["custo"]) else "não informado"
            linhas.append(f"- **Custo:** {custo}")
            linhas.append(f"- **Metodologia:** {r['metodologia']}")
            linhas.append(f"- **Instituto conhecido:** {'✅ sim' if r['conhecido'] else '⚠️ não reconhecido'}")
            linhas.append("")
    else:
        linhas.append("## ✅ Nenhuma pesquisa nova hoje")
        linhas.append("")

    # Próximas divulgações
    futuras = df[
        pd.to_datetime(df["divulgacao"], errors="coerce").dt.date > HOJE
    ].sort_values("divulgacao")

    if len(futuras) > 0:
        linhas.append("## 📅 Divulgações futuras registradas")
        linhas.append("")
        linhas.append("| Instituto | Campo | Divulgação | Amostra |")
        linhas.append("|-----------|-------|------------|---------|")
        vistos = set()
        for _, r in futuras.iterrows():
            chave = str(r["NR_PROTOCOLO_REGISTRO"])
            if chave in vistos:
                continue
            vistos.add(chave)
            linhas.append(
                f"| {r['instituto']} "
                f"| {r['campo_inicio']} → {r['campo_fim']} "
                f"| {r['divulgacao']} "
                f"| {int(r['QT_ENTREVISTADO']):,} |"
            )
        linhas.append("")

    # Resumo por instituto
    linhas.append("## 📊 Pesquisas por instituto (acumulado 2026)")
    linhas.append("")
    linhas.append("| Instituto | Pesquisas | Última divulgação | Amostra média |")
    linhas.append("|-----------|-----------|------------------|--------------|")
    por_inst = (
        df.groupby("instituto")
        .agg(total=("NR_PROTOCOLO_REGISTRO", "count"),
             ultima=("divulgacao", "max"),
             media=("QT_ENTREVISTADO", "mean"))
        .sort_values("total", ascending=False)
        .head(20)
    )
    for inst, row in por_inst.iterrows():
        linhas.append(
            f"| {inst} | {int(row['total'])} | {row['ultima']} | {row['media']:,.0f} |"
        )
    linhas.append("")

    # Alertas
    alertas = []
    institutos_novos = novas[~novas["conhecido"]]["instituto"].tolist()
    if institutos_novos:
        alertas.append(f"⚠️ Instituto(s) nunca visto(s) antes: **{', '.join(institutos_novos)}**")
    grandes = novas[novas["QT_ENTREVISTADO"] >= 4000]
    for _, r in grandes.iterrows():
        alertas.append(f"🔬 Pesquisa com amostra grande: **{r['instituto']}** (n={int(r['QT_ENTREVISTADO']):,})")

    if alertas:
        linhas.append("## ⚡ Alertas")
        linhas.append("")
        for a in alertas:
            linhas.append(a)
        linhas.append("")

    linhas.append("---")
    linhas.append(f"*Gerado automaticamente em {datetime.now().strftime('%d/%m/%Y %H:%M')} (Brasília)*")

    caminho = ROOT / f"relatorio_{HOJE}.md"
    caminho.write_text("\n".join(linhas), encoding="utf-8")
    log.info(f"  Relatório salvo: {caminho.name}")

# ─── 9. Pipeline principal ─────────────────────────────────────────────────────

def main() -> int:
    log.info(f"========== Monitor TSE — {HOJE} ==========")

    df_bruto = baixar()
    df       = filtrar(df_bruto)
    df       = enriquecer(df)

    salvar_snapshot(df)

    vistos = protocolos_vistos()
    novas  = detectar_novas(df, vistos)

    atualizar_historico(df)
    salvar_json(novas)
    gerar_relatorio(df, novas)

    log.info(f"========== Concluído ==========")

    # exit 0 = sem novidades | exit 1 = há novas (o workflow usa isso)
    return 1 if len(novas) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())

