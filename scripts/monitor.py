"""
monitor.py — Agente diário de monitoramento de pesquisas presidenciais no TSE.

Filosofia:
    Guarda TUDO com cargo=Presidente. Nenhuma linha é descartada.
    Flags indicam qualidade — a decisão de usar fica no campo usa_no_agregador.
    Veja CRITERIOS.md para a documentação completa de cada flag.
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

# ─── Referências para os flags ────────────────────────────────────────────────
#
# Baseado na análise de 296 pesquisas presidenciais do TSE em 25/05/2026.
# Veja CRITERIOS.md para a documentação completa.

# flag_abrangencia_br = FALSE quando qualquer um destes padrões aparece
# nos campos DS_METODOLOGIA_PESQUISA + DS_PLANO_AMOSTRAL + DS_DADO_MUNICIPIO
PADROES_PESQUISA_NAO_NACIONAL = [
    # Pesquisas de bairro explícitas
    r"\bbairros?:",
    r"\bbairros? pesquisados",
    r"zona urbana centro",
    r"zona urbana.*zona rural",
    # Pesquisas de cidade/município único
    r"município de [a-záàâãéèêíïóôõöúüç]",
    r"cidade de [a-záàâãéèêíïóôõöúüç]",
    r"municípios do município",
    r"eleitores? do município",
    # Pesquisas estaduais disfarçadas de nacionais
    r"eleitorado (do|da|de) estado (do|da|de) [a-z]",
    r"eleitorado desta unidade da federação",
    r"eleitorado do estado",
    r"pesquisa.*estado (do|da) [a-z]",
    r"área.*estado (do|da) [a-z]",
    r"abrangência.*estado",
    r"coleta.*estado (do|da) [a-z]",
    r"universo.*estado (do|da) [a-z]",
    r"eleitores? do estado",
    r"(realizada?|realizado?) no estado",
]

# Padrões que CONFIRMAM pesquisa nacional
PADROES_NACIONAL = [
    r"eleitorado brasileiro",
    r"todo o país",
    r"todo o brasil",
    r"26 estados",
    r"cinco regiões do brasil",
    r"5.*regiões do brasil",
    r"regiões do brasil",
    r"abrangência.*(é )?nacional",
    r"coleta é nacional",
    r"universo.*brasil",
    r"estratificad.* (por |pelas? )(grandes? )?regiões",
    r"amostra.*representativa.*eleitorado.*brasil",
]

# Institutos que já divulgaram pesquisas presidenciais nacionais publicamente
INSTITUTOS_ATIVOS = {
    "QUAEST", "DATAFOLHA", "ATLASINTEL", "ATLAS INTEL",
    "PARANA PESQUISAS", "REAL TIME BIG DATA",
    "FUTURA", "FUTURA INTELIGENCIA",
    "NEXUS", "FSB", "MDA",
    "GERP", "GRUPO GERP",
    "IDEIA", "BOAS IDEIAS",
    "PODERDATA", "PODER DATA",
    "100 CIDADES",
    "JOTA", "JOTA JORNALISMO",
    "DATA POVO",
    "INDEXA",
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


# ─── 3. Calcular flags ────────────────────────────────────────────────────────

def _texto_metodologia(df: pd.DataFrame) -> pd.Series:
    """Concatena os 3 campos de texto relevantes em minúsculas."""
    return (
        df["DS_METODOLOGIA_PESQUISA"].fillna("") + " " +
        df["DS_PLANO_AMOSTRAL"].fillna("") + " " +
        df["DS_DADO_MUNICIPIO"].fillna("")
    ).str.lower()


def calcular_flags(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    texto = _texto_metodologia(df)

    # ── flag_nacional ──────────────────────────────────────────────────────────
    # Todas com DS_CARGO=Presidente têm SG_UF=BR no TSE — mantemos por segurança
    df["flag_nacional"] = df["SG_UF"] == "BR"

    # ── flag_amostra_ok ────────────────────────────────────────────────────────
    # Amostra mínima de 1.000 exclui pesquisas municipais e pilotos.
    # Veja CRITERIOS.md seção Flag 2 para justificativa.
    qt = pd.to_numeric(df["QT_ENTREVISTADO"], errors="coerce").fillna(0)
    df["flag_amostra_ok"] = qt >= 1000

    # ── flag_abrangencia_br ────────────────────────────────────────────────────
    # Verifica nos campos de texto se a pesquisa foi aplicada
    # em múltiplos estados/regiões do Brasil.
    # Abordagem em 3 camadas:
    #   1. Presença de padrão nacional → True
    #   2. Presença de padrão não-nacional → False
    #   3. Sem padrão claro → True (benefício da dúvida para pesquisas com n>=1500)

    tem_nacional    = pd.Series(False, index=df.index)
    tem_nao_nacional = pd.Series(False, index=df.index)

    for p in PADROES_NACIONAL:
        tem_nacional |= texto.str.contains(p, regex=True, na=False)

    for p in PADROES_PESQUISA_NAO_NACIONAL:
        tem_nao_nacional |= texto.str.contains(p, regex=True, na=False)

    # Nacional confirmado: tem padrão nacional E não tem padrão estadual/municipal
    confirmado_nacional = tem_nacional & ~tem_nao_nacional

    # Estadual/municipal confirmado: tem padrão não-nacional
    confirmado_nao_nacional = tem_nao_nacional

    # Sem padrão claro: benefício da dúvida para amostras grandes (>=1.500)
    sem_padrao = ~tem_nacional & ~tem_nao_nacional
    grande     = qt >= 1500
    df["flag_abrangencia_br"] = confirmado_nacional | (sem_padrao & grande)

    # Detalhamento para auditoria
    df["_abrang_confirmado_nacional"]     = confirmado_nacional
    df["_abrang_confirmado_nao_nacional"] = confirmado_nao_nacional
    df["_abrang_sem_padrao"]              = sem_padrao

    # ── flag_instituto_ativo ───────────────────────────────────────────────────
    # Informativo — não bloqueia usa_no_agregador.
    inst = df["NM_EMPRESA_FANTASIA"].fillna(df["NM_EMPRESA"]).str.upper().fillna("")
    df["flag_instituto_ativo"] = inst.apply(
        lambda x: any(k in x for k in INSTITUTOS_ATIVOS)
    )

    # ── usa_no_agregador ───────────────────────────────────────────────────────
    df["usa_no_agregador"] = (
        df["flag_nacional"] &
        df["flag_amostra_ok"] &
        df["flag_abrangencia_br"]
    )

    # ── status (campo único com hierarquia de classificação) ───────────────────
    #
    # Hierarquia (do mais para o menos restritivo):
    #
    #   1  APROVADA — nacional confirmada        → texto confirma abrangência BR
    #   2  APROVADA — nacional presumida         → sem padrão contrário + n≥1500
    #   3  EXCLUÍDA — pesquisa estadual          → texto confirma abrangência estadual
    #   4  EXCLUÍDA — pesquisa municipal/bairro  → texto menciona bairros/cidade única
    #   5  EXCLUÍDA — amostra insuficiente       → n < 1000 (mas seria nacional)
    #   6  EXCLUÍDA — abrangência inconclusiva   → sem padrão + n < 1500
    #
    # Quando há múltiplos motivos, o de maior hierarquia prevalece no label,
    # mas todos os motivos aparecem concatenados após " + ".

    def _status(row):
        uf_ok  = row["flag_nacional"]
        amo_ok = row["flag_amostra_ok"]
        abr_ok = row["flag_abrangencia_br"]
        nac    = row["_abrang_confirmado_nacional"]
        nao    = row["_abrang_confirmado_nao_nacional"]
        sem    = row["_abrang_sem_padrao"]
        n      = int(row["QT_ENTREVISTADO"]) if pd.notna(row["QT_ENTREVISTADO"]) else 0

        # ── APROVADAS ──────────────────────────────────────────────────────────
        if uf_ok and amo_ok and abr_ok:
            if nac:
                return "1_APROVADA — nacional confirmada"
            else:
                return "2_APROVADA — nacional presumida (n≥1500)"

        # ── EXCLUÍDAS — montar motivos ────────────────────────────────────────
        motivos = []

        # Motivo de abrangência (hierarquia: estadual > municipal > inconclusivo)
        if not abr_ok:
            if nao:
                # Distinguir estadual de municipal pelo texto
                txt = str(row.get("DS_DADO_MUNICIPIO", "")).lower() + \
                      str(row.get("DS_METODOLOGIA_PESQUISA", "")).lower()
                if any(p in txt for p in ["bairros:", "bairros pesquisados",
                                          "zona urbana centro", "município de "]):
                    motivos.append("pesquisa municipal/bairro")
                else:
                    motivos.append("pesquisa estadual")
            elif sem:
                motivos.append(f"abrangência inconclusiva (n={n})")

        # Motivo de amostra
        if not amo_ok:
            motivos.append(f"amostra insuficiente (n={n})")

        # Motivo de UF (raro — mantido por segurança)
        if not uf_ok:
            motivos.append("UF≠BR")

        if not motivos:
            motivos.append("motivo indeterminado")

        # Determinar prefixo numérico pelo motivo principal
        if "pesquisa estadual" in motivos[0]:
            prefixo = "3"
        elif "pesquisa municipal" in motivos[0]:
            prefixo = "4"
        elif "amostra insuficiente" in motivos[0]:
            prefixo = "5"
        else:
            prefixo = "6"

        return f"{prefixo}_EXCLUÍDA — {' + '.join(motivos)}"

    # Precisamos dos campos de texto para _status — garantir que existem
    for col in ["DS_DADO_MUNICIPIO", "DS_METODOLOGIA_PESQUISA"]:
        if col not in df.columns:
            df[col] = ""

    df["status"] = df.apply(_status, axis=1)

    # Versão legível sem o prefixo numérico (para exibição)
    df["status_label"] = df["status"].str.replace(r"^\d_", "", regex=True)

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
    df.loc[m.str.contains(r"telefon|cati|capi",                         regex=True), "metodologia"] = "telefone"
    df.loc[m.str.contains(r"online|web|internet|eletrônico|formulário", regex=True), "metodologia"] = "online"
    df.loc[m.str.contains(r"ura|robocall|automatiz",                    regex=True), "metodologia"] = "URA"

    df["pesquisa_propria"] = df["ST_PESQUISA_PROPRIA"] == "S"

    return df


# ─── 5. Colunas do histórico ──────────────────────────────────────────────────

COLUNAS = [
    "NR_PROTOCOLO_REGISTRO", "instituto", "tse_registro",
    "campo_inicio", "campo_fim", "campo_dias", "divulgacao",
    "QT_ENTREVISTADO", "custo_reais", "metodologia", "pesquisa_propria",
    # classificação principal
    "status",          # ex: "3_EXCLUÍDA — pesquisa estadual"
    "status_label",    # ex: "EXCLUÍDA — pesquisa estadual"
    "usa_no_agregador",
    # flags individuais (para auditoria)
    "flag_nacional", "flag_amostra_ok", "flag_abrangencia_br", "flag_instituto_ativo",
    "_abrang_confirmado_nacional", "_abrang_confirmado_nao_nacional", "_abrang_sem_padrao",
]


# ─── 6. Detectar novas ────────────────────────────────────────────────────────

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
            "protocolo":                  str(r["NR_PROTOCOLO_REGISTRO"]),
            "instituto":                  str(r["instituto"]),
            "tse_registro":               str(r["tse_registro"]),
            "campo_inicio":               str(r["campo_inicio"]),
            "campo_fim":                  str(r["campo_fim"]),
            "divulgacao":                 str(r["divulgacao"]),
            "amostra":                    int(r["QT_ENTREVISTADO"]) if pd.notna(r["QT_ENTREVISTADO"]) else None,
            "custo_reais":                float(r["custo_reais"]) if pd.notna(r["custo_reais"]) else None,
            "metodologia":                str(r["metodologia"]),
            "flag_nacional":              bool(r["flag_nacional"]),
            "flag_amostra_ok":            bool(r["flag_amostra_ok"]),
            "flag_abrangencia_br":        bool(r["flag_abrangencia_br"]),
            "flag_instituto_ativo":       bool(r["flag_instituto_ativo"]),
            "abrang_confirmado_nacional": bool(r["_abrang_confirmado_nacional"]),
            "abrang_nao_nacional":        bool(r["_abrang_confirmado_nao_nacional"]),
            "usa_no_agregador":           bool(r["usa_no_agregador"]),
        })

    payload = {"data": str(HOJE), "total": len(novas), "novas": registros}
    p = DATA_DIR / f"novas_{HOJE}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"  JSON: {p.name}")
    return p


# ─── 8. Relatório Markdown ────────────────────────────────────────────────────

def gerar_relatorio(df: pd.DataFrame, novas: pd.DataFrame) -> None:
    L = []
    L.append(f"# Monitor TSE — {HOJE.strftime('%d/%m/%Y')}")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| Total registradas (cargo=Presidente) | **{len(df)}** |")
    L.append(f"| Recomendadas para o agregador (`usa_no_agregador=True`) | **{int(df['usa_no_agregador'].sum())}** |")
    L.append(f"| Novas detectadas hoje | **{len(novas)}** |")
    L.append("")

    # Distribuição por status
    L.append("## 🏷️ Classificação das pesquisas")
    L.append("")
    L.append("| # | Status | Quantidade |")
    L.append("|---|--------|-----------|")
    labels = {
        "1_APROVADA — nacional confirmada":       "✅ Aprovada — nacional confirmada",
        "2_APROVADA — nacional presumida (n≥1500)":"✅ Aprovada — nacional presumida (n≥1500)",
        "3_EXCLUÍDA — pesquisa estadual":          "❌ Excluída — pesquisa estadual",
        "4_EXCLUÍDA — pesquisa municipal/bairro":  "❌ Excluída — pesquisa municipal/bairro",
        "5_EXCLUÍDA — amostra insuficiente":       "❌ Excluída — amostra insuficiente",
        "6_EXCLUÍDA — abrangência inconclusiva":   "⚠️ Excluída — abrangência inconclusiva",
    }
    contagens = df["status"].str.extract(r"^(\d)")[0].value_counts().sort_index()
    for prefixo, label in labels.items():
        chave = prefixo[0]
        count = int(contagens.get(chave, 0))
        L.append(f"| {chave} | {label} | {count} |")
    L.append("")

    # Novas
    if len(novas) > 0:
        L.append("## 🆕 Novas pesquisas detectadas")
        L.append("")
        for _, r in novas.sort_values("tse_registro", ascending=False).iterrows():
            emoji = "✅" if r["usa_no_agregador"] else "⚠️"
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
            L.append(f"| flag_nacional | {'✅' if r['flag_nacional'] else '❌'} |")
            L.append(f"| flag_amostra_ok | {'✅' if r['flag_amostra_ok'] else '❌'} (n={int(r['QT_ENTREVISTADO'])}) |")
            L.append(f"| flag_abrangencia_br | {'✅' if r['flag_abrangencia_br'] else '❌'} {'(confirmado nacional)' if r['_abrang_confirmado_nacional'] else '(sem confirmação clara)' if r['_abrang_sem_padrao'] else '(detectado como estadual/municipal)'} |")
            L.append(f"| flag_instituto_ativo | {'✅' if r['flag_instituto_ativo'] else '⚠️ novo instituto'} |")
            L.append(f"| **usa_no_agregador** | {'✅ **sim**' if r['usa_no_agregador'] else '❌ **não** — verificar manualmente'} |")
            L.append("")
    else:
        L.append("## ✅ Nenhuma pesquisa nova hoje")
        L.append("")

    # Divulgações futuras
    futuras = (df[pd.to_datetime(df["divulgacao"], errors="coerce").dt.date > HOJE]
               .drop_duplicates("NR_PROTOCOLO_REGISTRO")
               .sort_values("divulgacao"))
    if len(futuras) > 0:
        L.append("## 📅 Divulgações futuras registradas")
        L.append("")
        L.append("| Instituto | Campo | Divulgação | Amostra | Usa agregador |")
        L.append("|-----------|-------|------------|---------|--------------|")
        for _, r in futuras.iterrows():
            usa = "✅" if r["usa_no_agregador"] else "❌"
            L.append(f"| {r['instituto']} | {r['campo_inicio']} → {r['campo_fim']} | {r['divulgacao']} | {int(r['QT_ENTREVISTADO']):,} | {usa} |")
        L.append("")

    # Todas as pesquisas recomendadas
    L.append("## 📋 Pesquisas recomendadas para o agregador (`usa_no_agregador=True`)")
    L.append("")
    L.append("| Instituto | Campo fim | Amostra | Metodologia | Abrangência confirmada |")
    L.append("|-----------|-----------|---------|-------------|----------------------|")
    recomendadas = (df[df["usa_no_agregador"]]
                    .drop_duplicates("NR_PROTOCOLO_REGISTRO")
                    .sort_values("campo_fim", ascending=False))
    for _, r in recomendadas.iterrows():
        conf = "✅ confirmado" if r["_abrang_confirmado_nacional"] else "⚠️ sem padrão"
        L.append(f"| {r['instituto']} | {r['campo_fim']} | {int(r['QT_ENTREVISTADO']):,} | {r['metodologia']} | {conf} |")
    L.append("")
    L.append("---")
    L.append(f"*Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')} · Veja [CRITERIOS.md](../CRITERIOS.md) para documentação dos flags*")

    p = ROOT / f"relatorio_{HOJE}.md"
    p.write_text("\n".join(L), encoding="utf-8")
    log.info(f"  Relatório: {p.name}")


# ─── 9. Pipeline principal ────────────────────────────────────────────────────

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

    log.info("========== Concluído ==========")
    return 1 if len(novas) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
