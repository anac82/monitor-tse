"""
monitor.py — Agente diário de monitoramento de pesquisas presidenciais no TSE.

Filosofia:
    Guarda TUDO com cargo=Presidente. Nenhuma linha é descartada.
    Dois flags decidem usa_no_agregador. Veja CRITERIOS.md para detalhes.

Flags (aplicados em sequência):

    FLAG 1 — amostra_ok        n > 1.000 entrevistados
    FLAG 2 — nacional_explicito  qualquer campo menciona abrangência BR
    FLAG 3 — estadual_explicito  qualquer campo menciona abrangência estadual

    Regra final:
        usa_no_agregador = FLAG1 AND FLAG2 AND NOT FLAG3
        (nacional prevalece sobre estadual quando ambos aparecem)

    Status:
        1_APROVADA            F1=True  F2=True   (F3 ignorado — nacional prevalece)
        2_EXCLUIDA_ESTADUAL   F1=True  F2=False  F3=True
        3_INCONCLUSIVA        F1=True  F2=False  F3=False  (sem padrão nos campos)
        4_EXCLUIDA_AMOSTRA    F1=False
"""

import io
import json
import logging
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

# ─── Configuração ──────────────────────────────────────────────────────────────

URL_TSE        = ("https://cdn.tse.jus.br/estatistica/sead/odsele/"
                  "pesquisa_eleitoral/pesquisa_eleitoral_2026.zip")
ARQUIVO_BRASIL = "pesquisa_eleitoral_2026_BRASIL.csv"

ROOT          = Path(__file__).parent.parent
DATA_DIR      = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
HISTORICO_CSV = DATA_DIR / "historico.csv"
HOJE          = date.today()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)


# ─── Padrões de texto ─────────────────────────────────────────────────────────
#
# Analisados nos campos:
#   DS_METODOLOGIA_PESQUISA + DS_PLANO_AMOSTRAL + DS_DADO_MUNICIPIO
#
# Baseado na leitura de 296 pesquisas presidenciais do TSE em 25/05/2026.
# Veja CRITERIOS.md para exemplos reais de cada padrão.

# FLAG 2 — menção EXPLÍCITA de abrangência nacional
# (qualquer um destes → nacional confirmado)
PADROES_NACIONAL = [
    r"eleitorado brasileiro",
    r"todo o (país|brasil)",
    r"26 estados",
    r"(cinco|5).*regiões do brasil",
    r"regiões do brasil",
    r"abrangência.*(é )?nacional",
    r"coleta.*(é|de abrangência) nacional",
    r"universo.*brasil",
    r"eleitorado.*brasil",
    r"amostra.*representativa.*brasil",
    r"estratificad.*(por|pelas?) (grandes? )?regiões",
    r"área de abrangência.*nacional",
    r"nível nacional",
    r"eleitores?.*(de )?todo.*brasil",
]

# FLAG 3 — menção EXPLÍCITA de abrangência estadual
# (qualquer um destes → estadual confirmado, SALVO se FLAG 2 também for True)
PADROES_ESTADUAL = [
    r"eleitorado do estado",
    r"eleitorado desta unidade da federação",
    r"eleitores? do estado",
    r"pesquisa realizada no estado",
    r"realizada? no estado",
    r"abrangência.*estado",
    r"coleta.*estado (do|da) [a-z]",
    r"universo.*estado (do|da) [a-z]",
    r"representativa.*estado (do|da) [a-z]",
    r"eleitorado de [a-záàâãéèêíïóôõöúüç]+(,| |$)",
]

# Institutos que já publicaram pesquisas nacionais (informativo)
INSTITUTOS_CONHECIDOS = {
    "QUAEST", "DATAFOLHA", "ATLASINTEL", "ATLAS INTEL",
    "PARANA PESQUISAS", "REAL TIME BIG DATA",
    "FUTURA", "FUTURA INTELIGENCIA",
    "NEXUS", "FSB", "MDA", "GERP", "GRUPO GERP",
    "IDEIA", "BOAS IDEIAS", "PODERDATA", "PODER DATA",
    "100 CIDADES", "JOTA", "JOTA JORNALISMO",
    "DATA POVO", "INDEXA",
}


# ─── 1. Download ───────────────────────────────────────────────────────────────

def baixar() -> pd.DataFrame:
    log.info("Baixando ZIP do TSE...")
    r = requests.get(URL_TSE, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        with zf.open(ARQUIVO_BRASIL) as f:
            df = pd.read_csv(f, sep=";", encoding="latin1", low_memory=False)
    log.info(f"  {len(df)} registros totais")
    return df


# ─── 2. Filtrar só cargo Presidente ───────────────────────────────────────────

def filtrar_cargo(df: pd.DataFrame) -> pd.DataFrame:
    pres = df[df["DS_CARGO"] == "Presidente"].copy()
    log.info(f"  {len(pres)} pesquisas com cargo=Presidente")
    return pres


# ─── 3. Calcular flags e status ───────────────────────────────────────────────

def calcular_flags(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Texto unificado para busca (todos os campos relevantes em minúsculas)
    texto = (
        df["DS_METODOLOGIA_PESQUISA"].fillna("") + " " +
        df["DS_PLANO_AMOSTRAL"].fillna("") + " " +
        df["DS_DADO_MUNICIPIO"].fillna("")
    ).str.lower()

    qt = pd.to_numeric(df["QT_ENTREVISTADO"], errors="coerce").fillna(0)

    # ── FLAG 1: amostra_ok ────────────────────────────────────────────────────
    df["flag_amostra_ok"] = qt > 1000

    # ── FLAG 2: nacional_explicito ────────────────────────────────────────────
    f2 = pd.Series(False, index=df.index)
    for p in PADROES_NACIONAL:
        f2 |= texto.str.contains(p, regex=True, na=False)
    df["flag_nacional_explicito"] = f2

    # ── FLAG 3: estadual_explicito ────────────────────────────────────────────
    f3 = pd.Series(False, index=df.index)
    for p in PADROES_ESTADUAL:
        f3 |= texto.str.contains(p, regex=True, na=False)
    df["flag_estadual_explicito"] = f3

    # ── Instituto conhecido (informativo) ─────────────────────────────────────
    inst_up = df["NM_EMPRESA_FANTASIA"].fillna(df["NM_EMPRESA"]).str.upper().fillna("")
    df["flag_instituto_conhecido"] = inst_up.apply(
        lambda x: any(k in x for k in INSTITUTOS_CONHECIDOS)
    )

    # ── usa_no_agregador ──────────────────────────────────────────────────────
    # Regra: F1=True AND F2=True
    # Nacional prevalece sobre estadual (F2 anula F3)
    df["usa_no_agregador"] = df["flag_amostra_ok"] & df["flag_nacional_explicito"]

    # ── status (campo único com hierarquia) ───────────────────────────────────
    #
    #   1_APROVADA           n>1000 + nacional explícito
    #   2_EXCLUIDA_ESTADUAL  n>1000 + estadual explícito (sem nacional)
    #   3_INCONCLUSIVA       n>1000 + sem padrão algum
    #   4_EXCLUIDA_AMOSTRA   n<=1000

    def _status(row):
        f1 = row["flag_amostra_ok"]
        f2 = row["flag_nacional_explicito"]
        f3 = row["flag_estadual_explicito"]
        n  = int(row["QT_ENTREVISTADO"]) if pd.notna(row["QT_ENTREVISTADO"]) else 0

        if not f1:
            return f"4_EXCLUIDA_AMOSTRA (n={n})"
        if f2:
            return "1_APROVADA"
        if f3:
            return "2_EXCLUIDA_ESTADUAL"
        return "3_INCONCLUSIVA"

    df["status"] = df.apply(_status, axis=1)

    return df


# ─── 4. Enriquecer ────────────────────────────────────────────────────────────

def enriquecer(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["custo_reais"] = pd.to_numeric(
        df["VR_PESQUISA"].astype(str).str.replace(",", "."), errors="coerce"
    )
    for orig, novo in [
        ("DT_INICIO_PESQUISA", "campo_inicio"),
        ("DT_FIM_PESQUISA",    "campo_fim"),
        ("DT_DIVULGACAO",      "divulgacao"),
        ("DT_REGISTRO",        "tse_registro"),
    ]:
        df[novo] = pd.to_datetime(df[orig], errors="coerce").dt.date

    df["campo_dias"] = (
        pd.to_datetime(df["DT_FIM_PESQUISA"]) -
        pd.to_datetime(df["DT_INICIO_PESQUISA"])
    ).dt.days

    df["instituto"] = df["NM_EMPRESA_FANTASIA"].fillna(df["NM_EMPRESA"]).str.strip()

    m = df["DS_METODOLOGIA_PESQUISA"].fillna("").str.lower()
    df["metodologia"] = "presencial"
    df.loc[m.str.contains(r"telefon|cati|capi",                          regex=True), "metodologia"] = "telefone"
    df.loc[m.str.contains(r"online|web|internet|eletrônico|formulário",  regex=True), "metodologia"] = "online"
    df.loc[m.str.contains(r"ura|robocall|automatiz",                     regex=True), "metodologia"] = "URA"

    df["pesquisa_propria"] = df["ST_PESQUISA_PROPRIA"] == "S"

    return df


# ─── 5. Colunas do histórico ──────────────────────────────────────────────────

COLUNAS = [
    # Identificação
    "NR_PROTOCOLO_REGISTRO",
    "instituto",
    "tse_registro",
    # Datas e campo
    "campo_inicio",
    "campo_fim",
    "campo_dias",
    "divulgacao",
    # Números
    "QT_ENTREVISTADO",
    "custo_reais",
    # Metodologia
    "metodologia",
    "pesquisa_propria",
    # Classificação
    "status",           # 1_APROVADA / 2_EXCLUIDA_ESTADUAL / 3_INCONCLUSIVA / 4_EXCLUIDA_AMOSTRA
    "usa_no_agregador", # True / False
    # Flags individuais
    "flag_amostra_ok",
    "flag_nacional_explicito",
    "flag_estadual_explicito",
    "flag_instituto_conhecido",
]


# ─── 6. Histórico ────────────────────────────────────────────────────────────

def protocolos_vistos() -> set:
    if not HISTORICO_CSV.exists():
        return set()
    return set(pd.read_csv(HISTORICO_CSV,
                           usecols=["NR_PROTOCOLO_REGISTRO"])
               ["NR_PROTOCOLO_REGISTRO"].astype(str))


def detectar_novas(df: pd.DataFrame, vistos: set) -> pd.DataFrame:
    novas = df[~df["NR_PROTOCOLO_REGISTRO"].astype(str).isin(vistos)].copy()
    log.info(f"  {len(novas)} pesquisas NOVAS (já vistas: {len(vistos)})")
    return novas


def atualizar_historico(df: pd.DataFrame) -> None:
    novo = df[COLUNAS].copy()
    if HISTORICO_CSV.exists():
        existente = pd.read_csv(HISTORICO_CSV)
        # Recriar se as colunas mudaram
        if set(COLUNAS) - set(existente.columns):
            log.info("  Histórico desatualizado — recriando do zero")
            combinado = novo
        else:
            combinado = pd.concat([existente, novo], ignore_index=True)
            # keep="first" preserva edições manuais de usa_no_agregador
            combinado = combinado.drop_duplicates("NR_PROTOCOLO_REGISTRO", keep="first")
    else:
        combinado = novo
    combinado.to_csv(HISTORICO_CSV, index=False, encoding="utf-8")
    log.info(f"  Histórico: {len(combinado)} pesquisas")


# ─── 7. Snapshot e JSON ───────────────────────────────────────────────────────

def salvar_snapshot(df: pd.DataFrame) -> None:
    p = DATA_DIR / f"snapshot_{HOJE}.csv"
    df[COLUNAS].to_csv(p, index=False, encoding="utf-8")
    log.info(f"  Snapshot: {p.name}")


def salvar_json(novas: pd.DataFrame) -> Path:
    registros = []
    for _, r in novas.sort_values("tse_registro", ascending=False).iterrows():
        registros.append({
            "protocolo":               str(r["NR_PROTOCOLO_REGISTRO"]),
            "instituto":               str(r["instituto"]),
            "tse_registro":            str(r["tse_registro"]),
            "campo_inicio":            str(r["campo_inicio"]),
            "campo_fim":               str(r["campo_fim"]),
            "divulgacao":              str(r["divulgacao"]),
            "amostra":                 int(r["QT_ENTREVISTADO"]) if pd.notna(r["QT_ENTREVISTADO"]) else None,
            "custo_reais":             float(r["custo_reais"]) if pd.notna(r["custo_reais"]) else None,
            "metodologia":             str(r["metodologia"]),
            "status":                  str(r["status"]),
            "usa_no_agregador":        bool(r["usa_no_agregador"]),
            "flag_amostra_ok":         bool(r["flag_amostra_ok"]),
            "flag_nacional_explicito": bool(r["flag_nacional_explicito"]),
            "flag_estadual_explicito": bool(r["flag_estadual_explicito"]),
            "flag_instituto_conhecido":bool(r["flag_instituto_conhecido"]),
        })
    payload = {"data": str(HOJE), "total": len(novas), "novas": registros}
    p = DATA_DIR / f"novas_{HOJE}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"  JSON: {p.name}")
    return p


# ─── 8. Relatório Markdown ────────────────────────────────────────────────────

STATUS_LABEL = {
    "1": "✅ Aprovada",
    "2": "❌ Excluída — estadual",
    "3": "⚠️ Inconclusiva — sem padrão",
    "4": "❌ Excluída — amostra insuficiente",
}

def gerar_relatorio(df: pd.DataFrame, novas: pd.DataFrame) -> None:
    L = []
    L.append(f"# Monitor TSE — {HOJE.strftime('%d/%m/%Y')}")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| Total registradas (cargo=Presidente) | **{len(df)}** |")
    L.append(f"| Aprovadas para o agregador | **{int(df['usa_no_agregador'].sum())}** |")
    L.append(f"| Novas detectadas hoje | **{len(novas)}** |")
    L.append("")

    # Distribuição por status
    L.append("## 📊 Classificação")
    L.append("")
    L.append("| Status | Quantidade | Critério |")
    L.append("|--------|-----------|---------|")
    L.append(f"| ✅ Aprovada | {(df['status']=='1_APROVADA').sum()} | n>1.000 + nacional explícito |")
    L.append(f"| ❌ Excluída — estadual | {df['status'].str.startswith('2_').sum()} | nacional ausente + estadual explícito |")
    L.append(f"| ⚠️ Inconclusiva | {(df['status']=='3_INCONCLUSIVA').sum()} | n>1.000 + sem padrão nos campos |")
    L.append(f"| ❌ Excluída — amostra | {df['status'].str.startswith('4_').sum()} | n ≤ 1.000 |")
    L.append("")

    # Novas pesquisas
    if len(novas) > 0:
        L.append("## 🆕 Novas pesquisas detectadas")
        L.append("")
        for _, r in novas.sort_values("tse_registro", ascending=False).iterrows():
            s = r["status"]
            emoji = "✅" if r["usa_no_agregador"] else ("⚠️" if s == "3_INCONCLUSIVA" else "❌")
            L.append(f"### {emoji} {r['instituto']}")
            L.append("| Campo | Valor |")
            L.append("|---|---|")
            L.append(f"| Protocolo | `{r['NR_PROTOCOLO_REGISTRO']}` |")
            L.append(f"| Registro TSE | {r['tse_registro']} |")
            L.append(f"| Campo | {r['campo_inicio']} → {r['campo_fim']} ({r['campo_dias']} dias) |")
            L.append(f"| Divulgação | {r['divulgacao']} |")
            L.append(f"| Amostra | {int(r['QT_ENTREVISTADO']):,} entrevistados |")
            custo = f"R$ {r['custo_reais']:,.0f}" if pd.notna(r["custo_reais"]) else "não informado"
            L.append(f"| Custo | {custo} |")
            L.append(f"| Metodologia | {r['metodologia']} |")
            L.append(f"| flag_amostra_ok | {'✅' if r['flag_amostra_ok'] else '❌'} (n={int(r['QT_ENTREVISTADO'])}) |")
            L.append(f"| flag_nacional_explicito | {'✅' if r['flag_nacional_explicito'] else '❌'} |")
            L.append(f"| flag_estadual_explicito | {'✅ sim (mas nacional prevalece)' if r['flag_estadual_explicito'] and r['flag_nacional_explicito'] else '✅ sim' if r['flag_estadual_explicito'] else '❌'} |")
            L.append(f"| flag_instituto_conhecido | {'✅' if r['flag_instituto_conhecido'] else '⚠️ novo'} |")
            L.append(f"| **status** | `{r['status']}` |")
            L.append(f"| **usa_no_agregador** | {'✅ **sim**' if r['usa_no_agregador'] else '❌ **não**'} |")
            L.append("")
    else:
        L.append("## ✅ Nenhuma pesquisa nova hoje")
        L.append("")

    # Divulgações futuras
    futuras = (df[pd.to_datetime(df["divulgacao"], errors="coerce").dt.date > HOJE]
               .drop_duplicates("NR_PROTOCOLO_REGISTRO")
               .sort_values("divulgacao"))
    if len(futuras) > 0:
        L.append("## 📅 Divulgações futuras")
        L.append("")
        L.append("| Instituto | Campo | Divulgação | Amostra | Status |")
        L.append("|-----------|-------|------------|---------|--------|")
        for _, r in futuras.iterrows():
            L.append(f"| {r['instituto']} | {r['campo_inicio']} → {r['campo_fim']} | {r['divulgacao']} | {int(r['QT_ENTREVISTADO']):,} | `{r['status']}` |")
        L.append("")

    # Aprovadas
    L.append("## ✅ Pesquisas aprovadas para o agregador")
    L.append("")
    L.append("| Instituto | Campo fim | Amostra | Metodologia |")
    L.append("|-----------|-----------|---------|-------------|")
    aprovadas = (df[df["usa_no_agregador"]]
                 .drop_duplicates("NR_PROTOCOLO_REGISTRO")
                 .sort_values("campo_fim", ascending=False))
    for _, r in aprovadas.iterrows():
        L.append(f"| {r['instituto']} | {r['campo_fim']} | {int(r['QT_ENTREVISTADO']):,} | {r['metodologia']} |")
    L.append("")
    L.append("---")
    L.append(f"*Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}*")

    p = ROOT / f"relatorio_{HOJE}.md"
    p.write_text("\n".join(L), encoding="utf-8")
    log.info(f"  Relatório: {p.name}")


# ─── 9. Alerta TXT ───────────────────────────────────────────────────────────

def gerar_alerta_txt(novas: pd.DataFrame) -> None:
    p = ROOT / "alerta.txt"
    if len(novas) == 0:
        p.write_text(f"[{HOJE}] Nenhuma pesquisa nova hoje.\n", encoding="utf-8")
        return

    linhas = [
        "=" * 60,
        f"MONITOR TSE — {HOJE}",
        f"{len(novas)} NOVA(S) PESQUISA(S) PRESIDENCIAL(IS)",
        "=" * 60,
        "",
    ]
    for _, r in novas.sort_values("tse_registro", ascending=False).iterrows():
        n = int(r["QT_ENTREVISTADO"]) if pd.notna(r["QT_ENTREVISTADO"]) else 0
        linhas += [
            f"INSTITUTO:   {r['instituto']}",
            f"PROTOCOLO:   {r['NR_PROTOCOLO_REGISTRO']}",
            f"CAMPO:       {r['campo_inicio']} até {r['campo_fim']}",
            f"DIVULGAÇÃO:  {r['divulgacao']}",
            f"AMOSTRA:     {n:,} entrevistados".replace(",", "."),
            f"METODOLOGIA: {r['metodologia']}",
            f"STATUS:      {r['status']}",
            f"AGREGADOR:   {'SIM' if r['usa_no_agregador'] else 'NÃO — verificar manualmente'}",
            "-" * 60,
            "",
        ]
    linhas.append("Veja o relatório completo no repositório.")
    p.write_text("\n".join(linhas), encoding="utf-8")
    log.info(f"  Alerta: {p.name}")


# ─── 10. Pipeline principal ───────────────────────────────────────────────────

def main() -> int:
    log.info(f"========== Monitor TSE — {HOJE} ==========")

    df = baixar()
    df = filtrar_cargo(df)
    df = calcular_flags(df)
    df = enriquecer(df)

    salvar_snapshot(df)

    vistos = protocolos_vistos()
    novas  = detectar_novas(df, vistos)

    atualizar_historico(df)
    salvar_json(novas)
    gerar_relatorio(df, novas)
    gerar_alerta_txt(novas)

    log.info("========== Concluído ==========")
    return 1 if len(novas) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
