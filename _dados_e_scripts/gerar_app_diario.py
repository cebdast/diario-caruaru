from __future__ import annotations

import csv
import html as html_module
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from baixar_diarios_caruaru import (
    END_DATE,
    FOCUS_TERMS,
    authority_pair_from_text,
    authority_pair_without_dash_from_text,
    canonical_agency_name,
    clean_fragment,
    fold_text,
    html_paragraphs,
    month_label,
    repair_text_encoding,
    INCREMENTAL_UPDATE,
    START_DATE,
)


csv.field_size_limit(10**9)

SOURCE_ROOT = Path("Diarios_Oficiais_Caruaru")
APP_ROOT = Path(os.environ.get("DIARIO_APP_ROOT", "Diario_Caruaru_App"))
DATA_PATH = APP_ROOT / "renderer" / "dados" / "diario-caruaru.json"
MAX_SHARD_BYTES = 12 * 1024 * 1024
PAGE_TEXT_CACHE: dict[str, list[str]] = {}
RAW_PAGE_TEXT_CACHE: dict[str, list[str]] = {}
CROP_TEXT_CACHE: dict[tuple[str, int, int, int, int, int], str] = {}
PDF_SIZE_CACHE: dict[str, tuple[int, int]] = {}
SAO_PAULO_TZ = timezone(timedelta(hours=-3), name="America/Sao_Paulo")


def current_generated_at() -> str:
    return datetime.now(SAO_PAULO_TZ).isoformat(timespec="seconds")


KNOWN_AGENCIES = [
    "Secretaria da Fazenda",
    "Secretaria de Administração",
    "Secretaria de Educação e Esportes",
    "Secretaria de Saúde",
    "Secretaria de Assistência Social e Combate à Fome",
    "Secretaria de Segurança Municipal",
    "Secretaria de Serviços Públicos",
    "Secretaria de Governo e Relações Institucionais",
    "Secretaria de Infraestrutura Urbana e Obras",
    "Secretaria de Desenvolvimento Rural",
    "Secretaria de Comunicação",
    "Secretaria da Mulher",
    "Fundação de Cultura de Caruaru",
    "Autarquia de Mobilidade de Caruaru (AMC)",
    "Autarquia de Urbanização e Meio Ambiente de Caruaru (URB)",
    "Instituto de Previdência dos Servidores Municipais de Caruaru",
    "Câmara Municipal de Caruaru",
    "Controladoria Geral do Município",
    "Procuradoria Geral do Município",
    "Gabinete do Prefeito",
]


def read_csv(name: str) -> list[dict[str, str]]:
    path = SOURCE_ROOT / name
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def file_url(path_value: str) -> str:
    if not path_value:
        return ""
    filename = Path(path_value).name
    edition = re.search(r"Diario[-_ ]Oficial[-_ ](\d+)", filename, re.IGNORECASE)
    if edition:
        filename = f"Diario Oficial {edition.group(1)}.pdf"
    return f"https://diariooficial.caruaru.pe.gov.br/diario/{quote(filename)}"


def append_pdf_page(url: str, page: int | None) -> str:
    if not url or not page:
        return url
    base = url.split("#", 1)[0]
    return f"{base}#page={page}"


def absolute_pdf_path(pdf_path: str) -> Path | None:
    if not pdf_path:
        return None
    path = Path(pdf_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def raw_pdf_page_texts(pdf_path: str) -> list[str]:
    path = absolute_pdf_path(pdf_path)
    if not path:
        return []
    cache_key = str(path.resolve())
    if cache_key in RAW_PAGE_TEXT_CACHE:
        return RAW_PAGE_TEXT_CACHE[cache_key]
    if not path.exists():
        RAW_PAGE_TEXT_CACHE[cache_key] = []
        return []
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        RAW_PAGE_TEXT_CACHE[cache_key] = []
        return []
    if result.returncode != 0 or not result.stdout:
        RAW_PAGE_TEXT_CACHE[cache_key] = []
        return []
    pages = result.stdout.split("\f")
    RAW_PAGE_TEXT_CACHE[cache_key] = pages
    return pages


def pdf_page_texts(pdf_path: str) -> list[str]:
    path = absolute_pdf_path(pdf_path)
    if not path:
        return []
    cache_key = str(path.resolve())
    if cache_key in PAGE_TEXT_CACHE:
        return PAGE_TEXT_CACHE[cache_key]
    pages = [fold_text(page) for page in raw_pdf_page_texts(pdf_path)]
    PAGE_TEXT_CACHE[cache_key] = pages
    return pages


def pdf_page_size(pdf_path: str) -> tuple[int, int]:
    path = absolute_pdf_path(pdf_path)
    if not path:
        return (613, 860)
    cache_key = str(path.resolve())
    if cache_key in PDF_SIZE_CACHE:
        return PDF_SIZE_CACHE[cache_key]
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        PDF_SIZE_CACHE[cache_key] = (613, 860)
        return PDF_SIZE_CACHE[cache_key]
    match = re.search(r"Page size:\s+([\d.]+)\s+x\s+([\d.]+)", result.stdout or "")
    if not match:
        PDF_SIZE_CACHE[cache_key] = (613, 860)
        return PDF_SIZE_CACHE[cache_key]
    PDF_SIZE_CACHE[cache_key] = (int(float(match.group(1))), int(float(match.group(2))))
    return PDF_SIZE_CACHE[cache_key]


def crop_pdf_page_text(pdf_path: str, page: int, x: int, y: int, width: int, height: int) -> str:
    path = absolute_pdf_path(pdf_path)
    if not path or not path.exists() or page < 1:
        return ""
    cache_key = (str(path.resolve()), page, x, y, width, height)
    if cache_key in CROP_TEXT_CACHE:
        return CROP_TEXT_CACHE[cache_key]
    try:
        result = subprocess.run(
            [
                "pdftotext",
                "-layout",
                "-enc",
                "UTF-8",
                "-f",
                str(page),
                "-l",
                str(page),
                "-x",
                str(x),
                "-y",
                str(y),
                "-W",
                str(width),
                "-H",
                str(height),
                str(path),
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        CROP_TEXT_CACHE[cache_key] = ""
        return ""
    text = result.stdout if result.returncode == 0 else ""
    CROP_TEXT_CACHE[cache_key] = text
    return text


def cut_before_next_act(text: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []
    kept_chars = 0
    for line in lines:
        clean = clean_fragment(line)
        norm = fold_text(clean)
        is_act_heading = bool(re.match(r"^(portaria|decreto|extrato|aviso|errata|notificacao)\s+(n\b|n[oº°]|conjunta|legislativa|legislativo|de\b|do\b|da\b)", norm))
        if kept_chars > 200 and is_act_heading:
            break
        kept.append(line)
        kept_chars += len(clean)
    return "\n".join(kept)


def text_from_last_annex_marker(text: str, row: dict[str, str]) -> str:
    candidates: list[int] = []
    ident = clean_fragment(row.get("identificacao") or "")
    if ident:
        folded_text = fold_text(text)
        folded_ident = fold_text(ident)
        pos = folded_text.rfind(folded_ident)
        if pos >= 0:
            candidates.append(pos)
    for match in re.finditer(r"\bANEXO\s+(?:ÚNICO|UNICO|[IVX]+)\b", text, re.I):
        candidates.append(match.start())
    if not candidates:
        return ""
    return text[max(candidates) :]


def recover_short_annex_text(row: dict[str, str], text: str, pdf_path: str, pdf_page: int | None) -> str:
    if not str(row.get("tipo") or "").startswith("Anexo"):
        return text
    if len(clean_fragment(text)) > 350 or not pdf_path or not pdf_page:
        return text
    cue_text = fold_text(" ".join([text, row.get("ementa") or "", row.get("titulo") or "", row.get("ato_pai") or ""]))
    page_width, page_height = pdf_page_size(pdf_path)
    left_width = max(1, page_width // 2)
    right_width = max(1, page_width - left_width)

    if "quadro de vagas" in cue_text:
        left = crop_pdf_page_text(pdf_path, pdf_page, 0, 0, left_width, page_height)
        right = crop_pdf_page_text(pdf_path, pdf_page, left_width, 0, right_width, page_height)
        candidates: list[str] = []
        for piece in [left, right]:
            recovered_piece = cut_before_next_act(text_from_last_annex_marker(piece, row))
            recovered_norm = fold_text(recovered_piece)
            if (
                "quadro de vagas" in recovered_norm
                and "r$" in recovered_norm
                and ("remuneracao" in recovered_norm or "vencimento" in recovered_norm)
            ):
                candidates.append(recovered_piece)
        if candidates:
            recovered = max(candidates, key=lambda value: len(clean_fragment(value)))
            if len(clean_fragment(recovered)) > len(clean_fragment(text)) + 600:
                return recovered
        return text

    if "concurso publico" not in cue_text:
        return text

    left = crop_pdf_page_text(pdf_path, pdf_page, 0, 0, left_width, page_height)
    right = crop_pdf_page_text(pdf_path, pdf_page, left_width, 0, right_width, page_height)
    next_left = crop_pdf_page_text(pdf_path, pdf_page + 1, 0, 0, left_width, page_height)

    _CONTEST_TERMS = ["ibam", "class", "candidato", "000", "resultado final", "listagem final", "gabarito"]
    left_text = cut_before_next_act(text_from_last_annex_marker(left, row))
    right_cut = cut_before_next_act(right)
    next_left_cut = cut_before_next_act(next_left)

    pieces = []
    for piece in [left_text, right_cut, next_left_cut]:
        if clean_fragment(piece) and any(term in fold_text(piece) for term in _CONTEST_TERMS):
            pieces.append(piece)

    recovered = "\n\n".join(piece.strip() for piece in pieces if clean_fragment(piece))
    recovered_norm = fold_text(recovered)
    has_contest_table = (
        "ibam" in recovered_norm
        or "resultado final" in recovered_norm
        or "listagem final" in recovered_norm
        or ("candidato" in recovered_norm and "000" in recovered_norm)
    )
    if len(clean_fragment(recovered)) > len(clean_fragment(text)) + 600 and has_contest_table:
        return recovered
    return text


def duplicate_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", fold_text(value)).strip()


def modal_subject(assunto: str, texto: str) -> str:
    subject = clean_fragment(assunto)
    if not subject:
        return ""
    body = duplicate_key(texto[:2500])
    key = duplicate_key(subject)
    if not key:
        return ""
    if len(key) >= 36 and key[: min(len(key), 140)] in body[: max(600, len(key) + 180)]:
        return ""
    return subject


def act_page_terms(row: dict[str, str], texto: str) -> list[tuple[str, int]]:
    terms: list[tuple[str, int]] = []
    for value, weight in [
        (row.get("identificacao") or "", 6),
        (row.get("titulo") or "", 7),
        (row.get("ementa") or "", 4),
    ]:
        clean = clean_fragment(value)
        if len(clean) >= 12:
            terms.append((fold_text(clean), weight))
    compact = clean_fragment(texto)
    compact = re.sub(r"\s+", " ", compact)
    for chunk in re.split(r"(?<=\.)\s+|\n+", compact):
        chunk = clean_fragment(chunk)
        if 35 <= len(chunk) <= 220:
            terms.append((fold_text(chunk), 5))
        if len(terms) >= 8:
            break
    seen: set[str] = set()
    unique: list[tuple[str, int]] = []
    for term, weight in terms:
        term = term.strip()
        if len(term) < 12 or term in seen:
            continue
        seen.add(term)
        unique.append((term, weight))
    return unique


def infer_pdf_page(pdf_path: str, row: dict[str, str], texto: str) -> int | None:
    pages = pdf_page_texts(pdf_path)
    terms = act_page_terms(row, texto)
    if not pages or not terms:
        return None
    best_page = 0
    best_score = 0
    for index, page_text in enumerate(pages, start=1):
        score = 0
        for term, weight in terms:
            if term and term in page_text:
                score += weight + min(len(term) // 80, 3)
        if score > best_score:
            best_score = score
            best_page = index
    return best_page or None


_LOTACAO_RE = re.compile(
    r"(?:do\(a\)|d[ao]s?|n[ao]s?|junto\s+(?:a|à|ao)|para\s+(?:a|o|os|as))\s+"
    r"((?:Secretaria|Central|Autarquia|Instituto|Fundo|Gabinete|Procuradoria|"
    r"Departamento|Coordenadoria|Superintendência|Câmara|Controladoria|Ouvidoria|"
    r"Empresa|Companhia|Fundação|Comissão|Diretoria|Ger[êe]ncia)"
    r"(?:\s+(?:Municipal|Geral|Permanente))?"
    r"(?:\s+(?:de|da|do|dos|das))?\s+"
    r"[A-ZÁÉÍÓÚÇÂÊÔÃÕ][^,.;\n]{2,90})",
    re.I,
)


def infer_agency_from_assignment(row: dict[str, str]) -> str:
    texto = (row.get("texto") or "")[:2500]
    m = _LOTACAO_RE.search(texto)
    if not m:
        return ""
    cand = clean_fragment(m.group(1)).rstrip(",.;:- ")
    folded = fold_text(cand)
    for known in KNOWN_AGENCIES:
        if fold_text(known) in folded:
            return canonical_agency_name(known)
    return canonical_agency_name(cand)


def infer_agency(row: dict[str, str]) -> str:
    context = clean_fragment(row.get("orgao_contexto") or "")
    if context and context != "-":
        return canonical_agency_name(context)

    haystack = fold_text(
        " ".join(
            [
                row.get("identificacao", ""),
                row.get("titulo", ""),
                row.get("ementa", ""),
                row.get("autoridades", ""),
                row.get("orgaos_mencionados", ""),
                row.get("texto", "")[:2500],
            ]
        )
    )
    matches: list[tuple[int, str]] = []
    for agency in KNOWN_AGENCIES:
        index = haystack.find(fold_text(agency))
        if index >= 0:
            matches.append((index, agency))
    if matches:
        return sorted(matches)[0][1]
    fallback = infer_agency_from_assignment(row)
    return fallback or "-"


def search_text(values: list[str]) -> str:
    return fold_text(" ".join(value for value in values if value))


def build_outline(parts: list[dict[str, str]]) -> list[dict[str, str]]:
    outline: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for part in parts:
        kind = part.get("tipo_parte") or ""
        if kind not in {"anexo", "capitulo", "secao"}:
            continue
        title = clean_fragment(part.get("titulo") or part.get("texto") or "")
        if not title:
            continue
        key = (kind, fold_text(title))
        if key in seen:
            continue
        seen.add(key)
        outline.append({"type": kind, "title": title[:180]})
    return outline


def is_long_document(text: str, parts_summary: dict[str, int], category: str, kind: str) -> bool:
    return (
        category == "Regimento interno"
        or kind.startswith("Anexo")
        or len(text) > 7000
        or parts_summary.get("capitulo", 0) >= 2
        or parts_summary.get("secao", 0) >= 4
        or parts_summary.get("artigo", 0) >= 15
    )


def infer_authorities(text: str) -> str:
    chunks = [clean_fragment(chunk) for chunk in text.replace("\n\n", "\n").split("\n")]
    for chunk in reversed(chunks[-20:]):
        if not chunk:
            continue
        pair = authority_pair_from_text(chunk) or authority_pair_without_dash_from_text(chunk)
        if pair:
            return f"{pair[0]} - {pair[1]}"
    return ""


def is_valid_authority_string(text: str) -> bool:
    """Returns True only if text looks like a real Name - Role authority string."""
    if not text or len(text) > 300:
        return False
    for segment in text.split(";"):
        s = segment.strip()
        if s and (authority_pair_from_text(s) or authority_pair_without_dash_from_text(s)):
            return True
    return False


_WEEKDAYS = ("segunda-feira", "terca-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sabado", "domingo")


def texto_plano_de_html(texto_html: str) -> str:
    """Texto puro do textoHtml, pre-computado para o app nao precisar
    remover tags no navegador (a etapa mais lenta da indexacao)."""
    plano = re.sub(r"<[^>]+>", " ", texto_html or "")
    plano = html_module.unescape(plano)
    return " ".join(plano.split())


_DATA_EXTENSO_RE = re.compile(r"\d{1,2} de [a-zç]+ de \d{4}")
_MASTHEAD_ANO_RE = re.compile(r"^ano\s+[ivxlcdm]+\s+n")
_NOME_JORNAL = "diariooficialdecaruaru"
# Sufixos que exigem hifen em portugues (cliticos, compostos frequentes).
_SUFIXOS_HIFEN = {"se", "lo", "la", "los", "las", "lhe", "lhes", "o", "a", "os", "as", "feira", "mail"}


_RUN_ALFABETICO_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]{15,}")


def _maior_run_alfabetico(linha: str) -> int:
    return max((len(m.group(0)) for m in _RUN_ALFABETICO_RE.finditer(linha)), default=0)


def _linha_de_capa(linha: str, norm: str, perto_do_topo: bool, na_capa: bool = False) -> bool:
    """Mobilia da capa/cabecalho do diario: masthead ("Ano XV nº ... Lei nº
    6.155, DE 21 DE DEZEMBRO DE 2018..."), nome do jornal colado pelo
    pdftotext e manchetes de capa em caixa-alta sem espacos
    ("PREFEITURADECARUARUREALIZA...")."""
    compacto = norm.replace(" ", "")
    # So quando a linha e essencialmente o nome do jornal; citacoes como
    # "publicado no Diario Oficial de Caruaru, edicao..." sao conteudo.
    if _NOME_JORNAL in compacto and len(compacto) <= len(_NOME_JORNAL) + 10:
        return True
    if _MASTHEAD_ANO_RE.match(norm):
        return True
    if norm in ("estado de pernambuco", "prefeitura municipal de caruaru", "prefeitura de caruaru"):
        return True
    # Data solta so e mobilia no topo da pagina (capa/cabecalho); em tabelas
    # de cronograma no meio da pagina e conteudo.
    if perto_do_topo and _DATA_EXTENSO_RE.fullmatch(norm):
        return True
    if norm.startswith("21 de dezembro de 2018") and "estado de pernambuco" in norm:
        return True
    # Manchete colada: sequencia alfabetica gigante em linha de caixa-alta.
    # No topo da pagina 1 (capa) o limiar e agressivo (15+: "SÁBADO(13)O25º
    # ARRAIALDAPESSOAIDOSA"); no resto do diario e conservador (24+, acima da
    # maior palavra real do portugues) para nunca descartar rubricas
    # orcamentarias, CNPJs ou "CONTRATO......Nº" (conteudo legitimo).
    limiar = 15 if na_capa else 24
    if _maior_run_alfabetico(linha) >= limiar:
        maiusculas = sum(1 for c in linha if c.isupper())
        minusculas = sum(1 for c in linha if c.islower())
        if maiusculas >= minusculas:
            return True
    return False


_PALAVRA_HIFEN_RE = re.compile(r"[a-zà-öø-ÿ]+-[a-zà-öø-ÿ]+")
_EMENDA_RE = re.compile(r"([\wà-öø-ÿ@./_-]+)-\n(\S+)", re.IGNORECASE)


def _emendar_hifens(texto_bloco: str, hifenizadas: set[str]) -> str:
    """Emenda palavras quebradas por hifen no fim da linha ("reforcan-\ndo"),
    MAS preserva o hifen quando ele e real: compostos vistos inteiros em outro
    ponto do diario ("segunda-feira"), cliticos ("revogando-se") e
    enderecos/URLs ("cpl-p@hotmail.com", ".../diario-oficial/")."""
    def emenda(m: re.Match) -> str:
        antes, depois = m.group(1), m.group(2)
        nucleo = depois.lower().strip(".,;:)]}?!")
        composto = f"{antes.lower().rsplit('-', 1)[-1]}-{nucleo}"
        if (
            composto in hifenizadas
            or nucleo in _SUFIXOS_HIFEN
            or re.search(r"[@/.]", depois)
            or re.search(r"[@/]|www|http", antes.lower())
        ):
            return f"{antes}-{depois}"
        return f"{antes}{depois}"

    return _EMENDA_RE.sub(emenda, texto_bloco)


def _leftover_diary_text(raw_text: str, covered_texts: list[str]) -> str:
    """Linhas do texto bruto do diario que nao entraram em NENHUM ato.

    Garante que a busca do app cubra 100% do que foi publicado: o que o
    extrator estruturado nao reconhecer (tabelas de anexos, listas de
    classificacao etc.) vira um ato sintetico "Demais publicacoes".
    O texto tambem e limpo do ruido tipografico do PDF: mobilia de capa,
    rodapes de secao repetidos pagina a pagina e hifenizacao de quebra de
    linha."""
    covered_lines: set[str] = set()
    for texto in covered_texts:
        for linha in texto.splitlines():
            norm = fold_text(" ".join(linha.split()))
            if norm:
                covered_lines.add(norm)
    haystack = fold_text(" ".join(" ".join(t.split()) for t in covered_texts))

    # Compostos hifenizados vistos INTEIROS (nao no fim da linha): sao a
    # evidencia de que o hifen e ortografico, nao quebra tipografica.
    hifenizadas: set[str] = set()
    for linha_raw in raw_text.splitlines():
        hifenizadas.update(m.group(0) for m in _PALAVRA_HIFEN_RE.finditer(linha_raw.lower()))

    # 1a passada: filtra mobilia obvia e junta candidatos com posicao de
    # pagina. Atencao: str.splitlines() quebra no \f, entao as paginas
    # precisam ser separadas explicitamente com split("\f").
    candidatos: list[tuple[int, str, str, int]] = []
    indice = 0
    for pagina, texto_pagina in enumerate(raw_text.split("\f")):
        linhas_desde_topo = 0
        for raw_line in texto_pagina.splitlines():
            indice += 1
            linhas_desde_topo += 1
            line = repair_text_encoding(clean_fragment(raw_line))
            if len(line) < 4:
                continue
            norm = fold_text(" ".join(line.split()))
            if norm.startswith("diario oficial do municipio de caruaru"):
                continue
            if "estado de pernambuco" in norm and any(wd in norm for wd in _WEEKDAYS):
                continue
            if _linha_de_capa(
                line,
                norm,
                perto_do_topo=linhas_desde_topo <= 5,
                na_capa=pagina == 0 and linhas_desde_topo <= 15,
            ):
                continue
            if norm in covered_lines or norm in haystack:
                continue
            candidatos.append((indice, line, norm, pagina))

    if not candidatos:
        return ""

    # 2a passada: rodape/cabecalho de secao = linha que repete UMA vez por
    # pagina em 3+ paginas. Linhas repetidas dentro da mesma pagina (tabelas,
    # listas de resultado) sao conteudo e ficam todas.
    ocorrencias_pagina: Counter = Counter()
    for _, _, norm, pag in candidatos:
        ocorrencias_pagina[(norm, pag)] += 1
    paginas_por_norm: dict[str, set[int]] = defaultdict(set)
    for (norm, pag), _n in ocorrencias_pagina.items():
        paginas_por_norm[norm].add(pag)

    def eh_mobilia_de_pagina(norm: str) -> bool:
        pags = paginas_por_norm[norm]
        return (
            len(norm) >= 12
            and len(pags) >= 3
            and all(ocorrencias_pagina[(norm, pag)] == 1 for pag in pags)
        )

    sobras: list[tuple[int, str]] = []
    mobilia_emitida: set[str] = set()
    for indice, line, norm, _pag in candidatos:
        if eh_mobilia_de_pagina(norm):
            if norm in mobilia_emitida:
                continue
            mobilia_emitida.add(norm)
        sobras.append((indice, line))

    # Agrupa linhas contiguas do original em blocos (paragrafos separados) e
    # emenda palavras hifenizadas na quebra de linha.
    blocos: list[list[str]] = []
    anterior: int | None = None
    for indice, linha in sobras:
        if anterior is None or indice - anterior > 1:
            blocos.append([])
        blocos[-1].append(linha)
        anterior = indice
    partes = [_emendar_hifens("\n".join(bloco), hifenizadas) for bloco in blocos]

    texto = "\n\n".join(partes)
    return texto if len(texto) >= 300 else ""


def _leftover_act_id(row: dict[str, str]) -> str:
    dia = "".join(reversed(row.get("data", "").split("/"))) or "00000000"
    numero = re.sub(r"\D", "", row.get("edicao", "")) or "0000"
    # Prefixo "z" garante que o ato sintetico ordene DEPOIS dos atos reais do
    # diario no leitor (os ids reais tem miolo numerico vindo do site).
    return f"{dia}-z{numero}-9999"


def row_date(row: dict[str, str]) -> date | None:
    try:
        return datetime.strptime(row.get("data") or "", "%d/%m/%Y").date()
    except ValueError:
        return None


def rows_in_window(
    rows: list[dict[str, str]], start_date: date | None, end_date: date | None
) -> list[dict[str, str]]:
    if start_date is None or end_date is None:
        return rows
    return [
        row
        for row in rows
        if (parsed := row_date(row)) is not None and start_date <= parsed <= end_date
    ]


def build_data(start_date: date | None = None, end_date: date | None = None) -> dict:
    acts_rows = rows_in_window(read_csv("atos_estruturados.csv"), start_date, end_date)
    people_rows = rows_in_window(read_csv("atos_pessoal.csv"), start_date, end_date)
    diary_rows = rows_in_window(read_csv("indice.csv"), start_date, end_date)
    part_rows = rows_in_window(read_csv("partes_dos_atos.csv"), start_date, end_date)

    part_counts: dict[str, Counter[str]] = defaultdict(Counter)
    parts_by_act: dict[str, list[dict[str, str]]] = defaultdict(list)
    for part in part_rows:
        part_counts[part["ato_id"]][part["tipo_parte"]] += 1
        parts_by_act[part["ato_id"]].append(part)

    acts = []
    covered_by_diary: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in acts_rows:
        orgao = infer_agency(row)
        assunto = row.get("ementa") or row.get("titulo") or row.get("identificacao") or ""
        texto = row.get("texto") or ""
        pdf_path = row.get("arquivo_pdf", "")
        pdf_page = infer_pdf_page(pdf_path, row, texto)
        texto = recover_short_annex_text(row, texto, pdf_path, pdf_page)
        covered_by_diary[(row["data"], row["edicao"])].append(texto)
        raw_auth = row.get("autoridades") or ""
        autoridades = raw_auth if is_valid_authority_string(raw_auth) else ""
        autoridades = autoridades or infer_authorities(texto)
        fazenda = any(fold_text(term) in fold_text(" ".join([orgao, assunto, texto])) for term in FOCUS_TERMS)
        pdf_url = file_url(pdf_path)
        parts_summary = dict(part_counts[row["ato_id"]])
        outline = build_outline(parts_by_act[row["ato_id"]])
        texto_html = html_paragraphs(texto)
        if (row.get("tipo") or "").startswith("Anexo"):
            # Rede de segurança p/ QUALQUER era do diário: anexo que renderizou
            # curto e sem estrutura visível ganha a nota apontando para o PDF.
            visivel = " ".join(re.sub(r"<[^>]+>", " ", texto_html).split())
            tem_estrutura = any(t in texto_html for t in ("<table", "<pre", "missing-annex-note"))
            if len(visivel) < 450 and not tem_estrutura:
                texto_html += (
                    '<div class="missing-annex-note">'
                    "<strong>Conteúdo completo no PDF</strong>"
                    "<span>Este anexo não tem texto extraível (mapa, imagem ou tabela "
                    "não estruturada). Use o botão <em>Abrir PDF</em> para consultá-lo.</span>"
                    "</div>"
                )
        acts.append(
            {
                "id": row["ato_id"],
                "data": row["data"],
                "anoMes": row["ano_mes"],
                "edicao": row["edicao"],
                "poder": (row.get("poder") or "-").rstrip(".") or "-",
                "orgao": orgao,
                "tipo": row.get("tipo") or "-",
                "categoria": row.get("categoria") or "-",
                "identificacao": row.get("identificacao") or "-",
                "assunto": assunto,
                "modalAssunto": modal_subject(assunto, texto),
                "autoridades": autoridades,
                "orgaosMencionados": row.get("orgaos_mencionados") or "",
                "temAnexo": row.get("tem_anexo") == "sim",
                "atoPai": row.get("ato_pai") or "",
                "textoHtml": texto_html,
                # Do texto extraído (não do HTML): renderizadores de anexo podem
                # compactar tabelas, mas a busca precisa ver o conteúdo todo.
                "textoPlano": " ".join(texto.split()),
                "pdfPath": str((Path.cwd() / pdf_path).resolve()) if pdf_path else "",
                "pdfUrl": pdf_url,
                "pdfPage": pdf_page,
                "pdfOpenUrl": append_pdf_page(pdf_url, pdf_page),
                "partes": parts_summary,
                "outline": outline,
                "isLongDocument": is_long_document(texto, parts_summary, row.get("categoria") or "", row.get("tipo") or ""),
                "fazenda": fazenda or row.get("categoria") == "Fazenda/tributos",
            }
        )

    # Atos sinteticos "Demais publicacoes": sobras do texto bruto de cada
    # diario que nenhum ato estruturado cobriu (cobertura de busca 100%).
    for row in diary_rows:
        text_path = Path(row.get("arquivo_texto") or "")
        if not text_path.is_absolute():
            text_path = Path.cwd() / text_path
        if not text_path.exists():
            continue
        raw_text = text_path.read_text(encoding="utf-8", errors="replace")
        chave = (row["data"], row["edicao"])
        sobra = _leftover_diary_text(raw_text, covered_by_diary.get(chave, []))
        if not sobra:
            continue
        pdf_path = row.get("arquivo_pdf", "")
        pdf_url = file_url(pdf_path)
        sobra_html = html_paragraphs(sobra)
        acts.append(
            {
                "id": _leftover_act_id(row),
                "data": row["data"],
                "anoMes": row["ano_mes"],
                "edicao": row["edicao"],
                "poder": "-",
                "orgao": "-",
                "tipo": "Demais publicações",
                "categoria": "Conteúdo não estruturado",
                "identificacao": f"Demais publicações — {row['edicao']}",
                "assunto": "Trechos deste diário que não foram reconhecidos como atos (tabelas, listas e anexos).",
                "modalAssunto": "",
                "autoridades": "",
                "orgaosMencionados": "",
                "temAnexo": False,
                "atoPai": "",
                "textoHtml": sobra_html,
                "textoPlano": " ".join(sobra.split()),
                "pdfPath": str((Path.cwd() / pdf_path).resolve()) if pdf_path else "",
                "pdfUrl": pdf_url,
                "pdfPage": None,
                "pdfOpenUrl": pdf_url,
                "partes": {},
                "outline": [],
                "isLongDocument": len(sobra) > 7000,
                "fazenda": False,
            }
        )

    parents_by_ident: dict[str, dict] = {}
    for act in acts:
        if not str(act.get("tipo") or "").startswith("Anexo"):
            ident = clean_fragment(act.get("identificacao") or "")
            if ident:
                parents_by_ident.setdefault(ident, act)
    for act in acts:
        pai_ident = clean_fragment(act.get("atoPai") or "")
        if not pai_ident:
            continue
        pai = parents_by_ident.get(pai_ident)
        if not pai:
            continue
        if (act.get("orgao") in ("", "-")) and pai.get("orgao") not in ("", "-"):
            act["orgao"] = pai["orgao"]
        if not act.get("autoridades") and pai.get("autoridades"):
            act["autoridades"] = pai["autoridades"]
        if not act.get("orgaosMencionados") and pai.get("orgaosMencionados"):
            act["orgaosMencionados"] = pai["orgaosMencionados"]

    people = []
    for row in people_rows:
        orgao = canonical_agency_name(row.get("orgao") or "") if row.get("orgao") else "-"
        pdf_path = row.get("arquivo_pdf", "")
        people.append(
            {
                "data": row["data"],
                "anoMes": row["ano_mes"],
                "edicao": row["edicao"],
                "portaria": row.get("portaria") or "-",
                "ato": row.get("ato") or "-",
                "nome": row.get("nome") or "Nome não identificado",
                "cpf": row.get("cpf") or "",
                "cargo": row.get("cargo") or "",
                "orgao": orgao,
                "efeitos": row.get("efeitos") or "",
                "texto": row.get("texto") or "",
                "pdfPath": str((Path.cwd() / pdf_path).resolve()) if pdf_path else "",
                "pdfUrl": file_url(pdf_path),
                "search": search_text(list(row.values()) + [orgao]),
            }
        )

    diaries = []
    for row in diary_rows:
        pdf_path = row.get("arquivo_pdf", "")
        diaries.append(
            {
                "data": row["data"],
                "anoMes": row["ano_mes"],
                "edicao": row["edicao"],
                "pdfPath": str((Path.cwd() / pdf_path).resolve()) if pdf_path else "",
                "pdfUrl": file_url(pdf_path),
                "temNomeacao": row.get("tem_nomeacao") == "sim",
                "temExoneracao": row.get("tem_exoneracao") == "sim",
                "fazenda": bool(row.get("termos_fazenda")),
                "termosFazenda": row.get("termos_fazenda") or "",
                "search": search_text(list(row.values())),
            }
        )

    month_counts = Counter(act["anoMes"] for act in acts)
    agency_counts = Counter(act["orgao"] for act in acts if act["orgao"] and act["orgao"] != "-")
    category_counts = Counter(act["categoria"] for act in acts if act["categoria"] and act["categoria"] != "-")
    type_counts = Counter(act["tipo"] for act in acts if act["tipo"] and act["tipo"] != "-")

    return {
        "generatedAt": current_generated_at(),
        "source": "Diário Oficial de Caruaru",
        "totals": {
            "diarios": len(diaries),
            "atos": len(acts),
            "pessoal": len(people),
            "fazenda": sum(1 for act in acts if act["fazenda"]),
        },
        "months": [
            {"value": month, "label": month_label(month), "count": count}
            for month, count in sorted(month_counts.items(), reverse=True)
        ],
        "agencies": [{"name": name, "count": count} for name, count in agency_counts.most_common()],
        "categories": [{"name": name, "count": count} for name, count in category_counts.most_common()],
        "types": [{"name": name, "count": count} for name, count in type_counts.most_common()],
        "acts": acts,
        "people": people,
        "diaries": diaries,
    }


def _shard_payload(year: str, acts: list[dict], people: list[dict]) -> str:
    return json.dumps(
        {"ano": int(year) if year.isdigit() else 0, "acts": acts, "people": people},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _split_year_records(year: str, acts: list[dict], people: list[dict]) -> list[dict]:
    """Divide um ano em arquivos pequenos o bastante para hospedagem estÃ¡tica."""
    chunks: list[dict] = []
    current = {"acts": [], "people": []}
    base_size = len(_shard_payload(year, [], []).encode("utf-8"))
    current_size = base_size

    for key, records in (("acts", acts), ("people", people)):
        for record in records:
            record_size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            separator_size = 1 if current[key] else 0
            if (current["acts"] or current["people"]) and current_size + separator_size + record_size > MAX_SHARD_BYTES:
                chunks.append(current)
                current = {"acts": [], "people": []}
                current_size = base_size
                separator_size = 0
            current[key].append(record)
            current_size += separator_size + record_size

    if current["acts"] or current["people"]:
        chunks.append(current)
    return chunks


def _year_shard_paths(year: str) -> list[Path]:
    paths = sorted(DATA_PATH.parent.glob(f"diario-caruaru-{year}-*.json"))
    legacy = DATA_PATH.parent / f"diario-caruaru-{year}.json"
    if legacy.exists():
        paths.insert(0, legacy)
    return paths


def _load_year_records(year: str) -> dict[str, list[dict]]:
    records = {"acts": [], "people": []}
    for shard_path in _year_shard_paths(year):
        shard = json.loads(shard_path.read_text(encoding="utf-8"))
        records["acts"].extend(shard.get("acts", []))
        records["people"].extend(shard.get("people", []))
    return records


def write_app_data(data: dict) -> None:
    """Formato v2: manifesto (diario-caruaru.json) + um arquivo por ano
    (diario-caruaru-<ano>.json) com os atos/pessoal daquele ano. Com todo o
    historico (2011+) um arquivo unico ficaria grande demais para o app."""
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    for antigo in DATA_PATH.parent.glob("diario-caruaru-*.json"):
        antigo.unlink()

    acts_por_ano: dict[str, list] = defaultdict(list)
    for act in data["acts"]:
        acts_por_ano[str(act.get("anoMes") or "0000")[:4]].append(act)
    people_por_ano: dict[str, list] = defaultdict(list)
    for person in data["people"]:
        people_por_ano[str(person.get("anoMes") or "0000")[:4]].append(person)

    years_meta = []
    total_bytes = 0
    for ano in sorted(set(acts_por_ano) | set(people_por_ano), reverse=True):
        chunks = _split_year_records(ano, acts_por_ano.get(ano, []), people_por_ano.get(ano, []))
        for index, chunk in enumerate(chunks, start=1):
            arquivo = f"diario-caruaru-{ano}-{index:02d}.json"
            payload = _shard_payload(ano, chunk["acts"], chunk["people"])
            (DATA_PATH.parent / arquivo).write_text(payload, encoding="utf-8")
            tamanho = len(payload.encode("utf-8"))
            total_bytes += tamanho
            years_meta.append(
                {
                    "ano": int(ano) if ano.isdigit() else 0,
                    "arquivo": arquivo,
                    "atos": len(chunk["acts"]),
                    "pessoal": len(chunk["people"]),
                    "bytes": tamanho,
                }
            )

    manifest = {chave: valor for chave, valor in data.items() if chave not in ("acts", "people")}
    manifest["schema"] = 2
    manifest["years"] = years_meta
    DATA_PATH.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(
        f"Dados do app gerados em {DATA_PATH.parent} "
        f"(manifesto + {len(years_meta)} anos, {total_bytes:,} bytes nos anos)"
    )


def _stats_for(acts: list[dict], people: list[dict], diaries: list[dict]) -> dict[str, Counter]:
    return {
        "months": Counter(act.get("anoMes") for act in acts if act.get("anoMes")),
        "agencies": Counter(
            act.get("orgao")
            for act in acts
            if act.get("orgao") and act.get("orgao") != "-"
        ),
        "categories": Counter(
            act.get("categoria")
            for act in acts
            if act.get("categoria") and act.get("categoria") != "-"
        ),
        "types": Counter(act.get("tipo") for act in acts if act.get("tipo") and act.get("tipo") != "-"),
        "diarios": Counter({"total": len(diaries)}),
        "atos": Counter({"total": len(acts)}),
        "pessoal": Counter({"total": len(people)}),
        "fazenda": Counter({"total": sum(1 for act in acts if act.get("fazenda"))}),
    }


def _merge_counter_list(
    current: list[dict], old_stats: Counter, new_stats: Counter, key: str
) -> list[dict]:
    counts = Counter({item[key]: item.get("count", 0) for item in current})
    counts.subtract(old_stats)
    counts.update(new_stats)
    counts = Counter({name: count for name, count in counts.items() if count > 0})
    if key == "value":
        return [
            {"value": value, "label": month_label(value), "count": counts[value]}
            for value in sorted(counts, reverse=True)
        ]
    return [{key: name, "count": count} for name, count in counts.most_common()]


def write_app_data_incremental(data: dict, start_date: date, end_date: date) -> None:
    """Atualiza somente a janela coletada e os shards dos anos afetados."""
    if not DATA_PATH.exists():
        write_app_data(data)
        return

    original_manifest = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(json.dumps(original_manifest))
    affected_years = {str(year) for year in range(start_date.year, end_date.year + 1)}
    old_stats = _stats_for([], [], [])
    old_manifest_diaries = [
        diary
        for diary in original_manifest.get("diaries", [])
        if start_date <= (row_date(diary) or date.min) <= end_date
    ]
    manifest["diaries"] = sorted(
        [
            diary
            for diary in original_manifest.get("diaries", [])
            if not (start_date <= (row_date(diary) or date.min) <= end_date)
        ] + data["diaries"],
        key=lambda diary: (row_date(diary) or date.min, str(diary.get("edicao") or "")),
    )
    new_stats = _stats_for(data["acts"], data["people"], data["diaries"])
    changed_shards = False

    for year in sorted(affected_years):
        old_shard = _load_year_records(year)

        old_acts = [
            act for act in old_shard.get("acts", [])
            if start_date <= (row_date(act) or date.min) <= end_date
        ]
        old_people = [
            person for person in old_shard.get("people", [])
            if start_date <= (row_date(person) or date.min) <= end_date
        ]
        old_window_stats = _stats_for(old_acts, old_people, [])
        for name in ("months", "agencies", "categories", "types", "atos", "pessoal", "fazenda"):
            old_stats[name].update(old_window_stats[name])

    old_stats["diarios"] = Counter({"total": len(old_manifest_diaries)})

    for year in sorted(affected_years):
        old_shard = _load_year_records(year)

        acts = [
            act for act in old_shard.get("acts", [])
            if not (start_date <= (row_date(act) or date.min) <= end_date)
        ]
        acts.extend(act for act in data["acts"] if str(act.get("anoMes") or "")[:4] == year)
        people = [
            person for person in old_shard.get("people", [])
            if not (start_date <= (row_date(person) or date.min) <= end_date)
        ]
        people.extend(person for person in data["people"] if str(person.get("anoMes") or "")[:4] == year)

        old_paths = _year_shard_paths(year)
        chunks = _split_year_records(year, acts, people)
        new_payloads = {
            DATA_PATH.parent / f"diario-caruaru-{year}-{index:02d}.json": _shard_payload(
                year, chunk["acts"], chunk["people"]
            )
            for index, chunk in enumerate(chunks, start=1)
        }
        old_payloads = {
            path: path.read_text(encoding="utf-8")
            for path in old_paths
        }
        if old_payloads != new_payloads:
            for path in old_paths:
                path.unlink()
            for path, payload in new_payloads.items():
                path.write_text(payload, encoding="utf-8")
            changed_shards = True
        if not acts and not people:
            continue

    manifest["totals"]["diarios"] = len(manifest["diaries"])
    manifest["totals"]["atos"] = manifest["totals"].get("atos", 0) - old_stats["atos"]["total"] + new_stats["atos"]["total"]
    manifest["totals"]["pessoal"] = manifest["totals"].get("pessoal", 0) - old_stats["pessoal"]["total"] + new_stats["pessoal"]["total"]
    manifest["totals"]["fazenda"] = manifest["totals"].get("fazenda", 0) - old_stats["fazenda"]["total"] + new_stats["fazenda"]["total"]
    manifest["months"] = _merge_counter_list(manifest.get("months", []), old_stats["months"], new_stats["months"], "value")
    manifest["agencies"] = _merge_counter_list(manifest.get("agencies", []), old_stats["agencies"], new_stats["agencies"], "name")
    manifest["categories"] = _merge_counter_list(manifest.get("categories", []), old_stats["categories"], new_stats["categories"], "name")
    manifest["types"] = _merge_counter_list(manifest.get("types", []), old_stats["types"], new_stats["types"], "name")

    years_meta = []
    for item in manifest.get("years", []):
        if str(item.get("ano")) not in affected_years:
            years_meta.append(item)
    for year in sorted(affected_years, reverse=True):
        for shard_path in _year_shard_paths(year):
            shard = json.loads(shard_path.read_text(encoding="utf-8"))
            years_meta.append({
                "ano": int(year),
                "arquivo": shard_path.name,
                "atos": len(shard.get("acts", [])),
                "pessoal": len(shard.get("people", [])),
                "bytes": shard_path.stat().st_size,
            })
    manifest["years"] = sorted(years_meta, key=lambda item: int(item.get("ano", 0)), reverse=True)
    manifest["schema"] = 2

    old_static = dict(original_manifest)
    old_static.pop("generatedAt", None)
    new_static = dict(manifest)
    new_static.pop("generatedAt", None)
    if not changed_shards and old_static == new_static:
        return

    manifest["generatedAt"] = current_generated_at()
    DATA_PATH.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> int:
    if INCREMENTAL_UPDATE:
        data = build_data(START_DATE, END_DATE)
        write_app_data_incremental(data, START_DATE, END_DATE)
    else:
        data = build_data()
        write_app_data(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
