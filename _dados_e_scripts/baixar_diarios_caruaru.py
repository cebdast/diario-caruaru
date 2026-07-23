from __future__ import annotations

import csv
import html
import json
import os
import re
import ssl
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


csv.field_size_limit(10**9)

BASE_URL = "https://diariooficial.caruaru.pe.gov.br/"


def _garantir_poppler_no_path() -> None:
    """Garante que o pdftotext usado seja o do Poppler.

    No Windows e comum o PATH resolver primeiro o pdftotext antigo que vem
    com o Git (Xpdf 3.x), que nao suporta '-enc UTF-8' direito e corrompe a
    extracao. Se houver um Poppler instalado, ele passa a vir primeiro."""
    candidatos: list[Path] = []
    configurado = os.environ.get("POPPLER_PATH", "").strip()
    if configurado:
        candidatos.append(Path(configurado))
    for raiz in (Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)"), Path(r"C:\poppler")):
        if raiz.exists():
            candidatos.extend(sorted(raiz.glob("poppler-*/Library/bin"), reverse=True))
            candidatos.extend(sorted(raiz.glob("poppler-*/bin"), reverse=True))
            candidatos.append(raiz / "Library" / "bin")
    for pasta in candidatos:
        if (pasta / "pdftotext.exe").exists():
            os.environ["PATH"] = str(pasta) + os.pathsep + os.environ.get("PATH", "")
            return
    git_poppler = Path(r"C:\Program Files\Git\mingw64\bin")
    if (git_poppler / "pdftotext.exe").exists():
        os.environ["PATH"] = str(git_poppler) + os.pathsep + os.environ.get("PATH", "")


_garantir_poppler_no_path()


def configured_date(name: str, default: date) -> date:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Data invalida em {name}: {value}")


START_DATE = configured_date("DIARIO_START_DATE", date(2011, 12, 1))
END_DATE = configured_date("DIARIO_END_DATE", date.today())
INCREMENTAL_UPDATE = os.environ.get("DIARIO_INCREMENTAL", "").strip().lower() in {"1", "true", "sim", "yes"}
OUTPUT_ROOT = Path("Diarios_Oficiais_Caruaru")

MONTH_NAMES = {
    1: "Janeiro",
    2: "Fevereiro",
    3: "Marco",
    4: "Abril",
    5: "Maio",
    6: "Junho",
    7: "Julho",
    8: "Agosto",
    9: "Setembro",
    10: "Outubro",
    11: "Novembro",
    12: "Dezembro",
}

FOCUS_TERMS = [
    "secretaria da fazenda",
    "fazenda municipal",
    "sefaz",
    "secretario da fazenda",
    "secretaria municipal da fazenda",
    "tributario",
    "tributaria",
    "tributo",
    "fiscalizacao",
    "arrecadacao",
    "iss",
    "iptu",
    "itbi",
    "divida ativa",
    "credito tributario",
    "receita municipal",
    "nota fiscal",
    "simples nacional",
    "contribuinte",
]

GENERAL_TERMS = [
    "lei",
    "decreto",
    "portaria",
    "edital",
    "licitacao",
    "contrato",
    "termo aditivo",
    "homologacao",
    "nomeacao",
    "exoneracao",
    "dispensa",
    "pregao",
    "secretaria",
]

PERSONNEL_TERMS = [
    "nomear",
    "nomeacao",
    "nomeação",
    "nomeado",
    "nomeada",
    "exonerar",
    "exorenar",
    "exoneracao",
    "exoneração",
    "exonerado",
    "exonerada",
    "tornar sem efeito",
    "cargo em comissao",
    "cargo em comissão",
    "cargo em provimento efetivo",
]

APPOINTMENT_TERMS = [
    "nomear",
    "nomeacao",
    "nomeação",
    "nomeado",
    "nomeada",
    "cargo em comissao",
    "cargo em comissão",
    "cargo em provimento efetivo",
]

DISMISSAL_TERMS = [
    "exonerar",
    "exorenar",
    "exoneracao",
    "exoneração",
    "exonerado",
    "exonerada",
    "tornar sem efeito",
]


@dataclass
class Diary:
    diary_id: int
    edition: str
    published_at: date
    original_name: str
    url_path: str
    pdf_path: Path
    text_path: Path
    reading_path: Path


_ssl_context: ssl.SSLContext | None = None  # None = verificacao padrao de certificado


def _abrir_url(request: Request, timeout: int):
    """urlopen com fallback: se o certificado do site estiver expirado/invalido,
    passa a ignorar a verificacao nesta execucao (com aviso). Como o fallback so
    engata depois de uma falha real, a verificacao volta sozinha quando a
    prefeitura renovar o certificado."""
    global _ssl_context
    try:
        return urlopen(request, timeout=timeout, context=_ssl_context)
    except URLError as exc:
        if _ssl_context is None and isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
            print(
                "AVISO: certificado SSL do site do Diario Oficial invalido/expirado "
                f"({exc.reason}). Prosseguindo SEM verificacao de certificado.",
                flush=True,
            )
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            _ssl_context = context
            return urlopen(request, timeout=timeout, context=_ssl_context)
        raise


def fetch_text(url: str, retries: int = 2) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _abrir_url(request, timeout=25) as response:
                return response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(attempt * 1.5)
    raise RuntimeError(f"Nao foi possivel acessar {url}: {last_error}")


def download_file(url: str, target: Path, retries: int = 3) -> None:
    if target.exists() and target.stat().st_size > 0:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _abrir_url(request, timeout=120) as response:
                data = response.read()
            if not data.startswith(b"%PDF"):
                raise RuntimeError("resposta nao parece ser PDF")
            target.write_bytes(data)
            return
        except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            time.sleep(attempt * 2)
    raise RuntimeError(f"Falha ao baixar {url}: {last_error}")


def extract_diaries_json(page: str) -> list[dict]:
    match = re.search(r'id="diariosJSON"\s+data-items=\'([^\']+)\'', page)
    if not match:
        raise RuntimeError("Nao encontrei a lista JSON de diarios na pagina.")
    raw = html.unescape(match.group(1))
    return json.loads(raw)


def parse_date(value: str) -> date:
    if "-" in value:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    return datetime.strptime(value, "%d/%m/%Y").date()


def safe_slug(value: str) -> str:
    value = value.replace("Diário", "Diario")
    value = value.replace("Oficial", "Oficial")
    value = re.sub(r"[^\w\s.-]+", "", value, flags=re.ASCII)
    value = re.sub(r"\s+", "-", value.strip())
    return value or "Diario-Oficial"


def month_dir(day: date) -> Path:
    return OUTPUT_ROOT / f"{day.year}" / f"{day.month:02d} - {MONTH_NAMES[day.month]}"


def make_diary(item: dict) -> Diary:
    published_at = parse_date(item["dataEntrada"])
    archive = item["arquivo"]
    edition = archive.get("nome") or Path(archive["name"]).stem
    filename = f"{published_at:%Y-%m-%d}_{safe_slug(edition)}.pdf"
    folder = month_dir(published_at)
    return Diary(
        diary_id=int(item["id"]),
        edition=edition,
        published_at=published_at,
        original_name=archive["name"],
        url_path=archive["url"],
        pdf_path=folder / filename,
        text_path=folder / "textos" / filename.replace(".pdf", ".txt"),
        reading_path=folder / "leituras" / filename.replace(".pdf", ".txt"),
    )


def filter_diaries(items: Iterable[dict]) -> list[Diary]:
    diaries: list[Diary] = []
    seen: set[tuple[date, str]] = set()
    for item in items:
        day = parse_date(item["dataEntrada"])
        if not (START_DATE <= day <= END_DATE):
            continue
        diary = make_diary(item)
        key = (diary.published_at, diary.original_name)
        if key in seen:
            continue
        seen.add(key)
        diaries.append(diary)
    return sorted(diaries, key=lambda item: (item.published_at, item.edition))


def encoded_pdf_url(path: str) -> str:
    return urljoin(BASE_URL, quote(path, safe="/"))


def extract_text(pdf_path: Path, text_path: Path) -> str:
    text_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["pdftotext", "-raw", "-enc", "UTF-8", str(pdf_path), str(text_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"pdftotext excedeu 45 segundos para {pdf_path.name}") from exc
    return text_path.read_text(encoding="utf-8", errors="replace")


def normalize(text: str) -> str:
    replacements = str.maketrans(
        "áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ",
        "aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC",
    )
    return text.translate(replacements).lower()


def find_terms(text: str, terms: list[str]) -> list[str]:
    norm = normalize(text)
    found = []
    for term in terms:
        if normalize(term) in norm:
            found.append(term)
    return found


def clean_fragment(text: str) -> str:
    text = text.replace("\ufb01", "fi").replace("\ufb02", "fl")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"([([{])\s+", r"\1", text)
    return text.strip()


def readable_text(text: str) -> str:
    lines = []
    for raw_line in text.splitlines():
        line = clean_fragment(raw_line)
        if not line:
            continue
        if re.fullmatch(r"\d+", line):
            continue
        lines.append(line)

    paragraphs: list[str] = []
    current = ""
    for line in lines:
        is_heading = (
            line.isupper()
            or re.match(r"^(PORTARIA|DECRETO|LEI|EDITAL|EXTRATO|TERMO|AVISO)\b", line.upper())
        )
        if is_heading and current:
            paragraphs.append(current)
            current = ""
        if current:
            current = f"{current} {line}"
        else:
            current = line
        if re.search(r"[.;:]$", line) or len(current) > 900 or is_heading:
            paragraphs.append(current)
            current = ""
    if current:
        paragraphs.append(current)

    cleaned: list[str] = []
    seen: set[str] = set()
    for paragraph in paragraphs:
        paragraph = clean_fragment(paragraph)
        if len(paragraph) < 12:
            continue
        key = normalize(paragraph[:180])
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(paragraph)
    return "\n\n".join(cleaned) + "\n"


def write_readable_text(raw_text: str, reading_path: Path) -> str:
    text = readable_text(raw_text)
    reading_path.parent.mkdir(parents=True, exist_ok=True)
    reading_path.write_text(text, encoding="utf-8")
    return text


def snippets_for_terms(text: str, terms: list[str], limit: int = 8) -> list[str]:
    norm = normalize(text)
    snippets: list[str] = []
    seen: set[str] = set()
    for term in terms:
        term_norm = normalize(term)
        for match in re.finditer(re.escape(term_norm), norm):
            start = max(0, match.start() - 260)
            end = min(len(text), match.end() + 360)
            snippet = clean_fragment(text[start:end])
            if len(snippet) > 520:
                snippet = snippet[:517].rstrip() + "..."
            marker = normalize(snippet[:160])
            if marker not in seen:
                snippets.append(snippet)
                seen.add(marker)
            if len(snippets) >= limit:
                return snippets
    return snippets


def snippets_for_any_terms(text: str, terms: list[str], limit: int = 8) -> list[str]:
    found = find_terms(text, terms)
    return snippets_for_terms(text, found, limit=limit)


PORTARIA_RE = re.compile(
    r"(?im)^\s*(PORTARIA(?:\s+[A-ZÁÀÂÃÉÈÊÍÓÔÕÚÇ0-9./-]+)*\s+N[º°O]?\s*\.?\s*\d+[A-Z0-9./-]*)\s*$"
)


def split_portaria_blocks(text: str) -> list[tuple[str, str]]:
    matches = list(PORTARIA_RE.finditer(text))
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = clean_fragment(match.group(1))
        block = text[start:end]
        blocks.append((title, block))
    return blocks


def action_label(action_text: str) -> str:
    norm = normalize(action_text)
    if norm.startswith("exonerar a pedido"):
        return "Exonerar a pedido"
    if norm.startswith("exonerar"):
        return "Exonerar"
    if norm.startswith("nomear"):
        return "Nomear"
    if norm.startswith("tornar sem efeito"):
        return "Tornar sem efeito"
    if norm.startswith("designar"):
        return "Designar"
    if norm.startswith("cessar"):
        return "Cessar"
    if norm.startswith("declarar"):
        return "Declarar"
    if norm.startswith("retificar"):
        return "Retificar"
    first = clean_fragment(action_text).split(",", 1)[0].split(".", 1)[0]
    return first[:80]


def action_text_from_block(block: str) -> str | None:
    match = re.search(
        r"(?is)\bresolve:?\s*(.*?)(?:\n\s*Caruaru,\s+\d{1,2}\s+de\s+.+?\s+de\s+\d{4}\.?|\n\s*Rodrigo\s+Pinheiro\b|\Z)",
        block,
    )
    if not match:
        return None
    action = clean_fragment(match.group(1))
    if not find_terms(action, PERSONNEL_TERMS):
        return None
    return action


def extract_name(action_text: str, act: str) -> str:
    text = clean_fragment(action_text)
    text = re.sub(r"(?i)^(nomear|exonerar a pedido|exonerar|designar|tornar sem efeito|cessar|declarar|retificar)\b", "", text)
    text = re.sub(r"(?i)^\s*(?:o senhor|a senhora|servidor(?:a)?|,|\s)+", "", text).strip()
    if normalize(act).startswith("tornar sem efeito"):
        named = re.search(r"(?i)\b(?:nomeou|nomear|exonerou|exonerar)\s*,?\s*(.*?)(?:,\s*CPF|\s+CPF\b|,\s*para\b|,\s*do\b|$)", text)
        if named:
            return clean_fragment(named.group(1))
    match = re.search(r"(?i)(.*?)(?:,\s*CPF|\s+CPF\b|,\s*para\b|,\s*do\b|$)", text)
    return clean_fragment(match.group(1)) if match else ""


def extract_cpf(action_text: str) -> str:
    match = re.search(r"(?i)\bCPF\s*n?[º°o]?\s*([0-9Xx.*-]+)", action_text)
    return clean_fragment(match.group(1).rstrip(".,;")) if match else ""


def extract_position(action_text: str) -> str:
    patterns = [
        r"(?is)\b(?:para\s+(?:exercer\s+)?o|do)\s+(cargo\s+.*?)(?=,\s+(?:d[ao]s?|n[ao])\s+|,\s+(?:Secretaria|Autarquia|Fundação|Fundacao|Instituto|Controladoria|Procuradoria|Gabinete|Agência|Agencia|Central)\b|,\s+lotad[oa](?:\(a\))?\s+n[oa]\(a\)?\s+|,\s*com efeitos\b|\.)",
        r"(?is)\b(cargo\s+em\s+provimento\s+efetivo\s+de\s+.*?)(?=,\s+lotad[oa](?:\(a\))?\s+n[oa]\(a\)?\s+|,\s*com efeitos\b|\.)",
        r"(?is)\b(?:para\s+(?:exercer\s+)?o|do)\s+(cargo\s+.*?)(?=,\s*com efeitos\b|\.)",
    ]
    for pattern in patterns:
        match = re.search(pattern, action_text)
        if match:
            return clean_fragment(match.group(1))
    return ""


def extract_agency(action_text: str) -> str:
    pattern = (
        r"(?is)\b(?:d[ao]s?|n[ao])\s+"
        r"((?:Secretaria|Autarquia|Fundação|Fundacao|Instituto|Controladoria|Procuradoria|Gabinete|Agência|Agencia)"
        r".*?)(?=,\s*com efeitos\b|,\s*retroativos\b|,\s*com ônus\b|,\s*nos termos\b|\.)"
    )
    match = re.search(pattern, action_text)
    if match:
        return clean_fragment(match.group(1))

    comma_pattern = (
        r"(?is),\s*(?:(?:d[ao]s?|n[ao])\s+)?"
        r"((?:Secretaria|Autarquia|Fundação|Fundacao|Instituto|Controladoria|Procuradoria|Gabinete|Agência|Agencia|Central)"
        r".*?)(?=,\s*com efeitos\b|,\s*retroativos\b|,\s*com ônus\b|,\s*nos termos\b|\.)"
    )
    comma_match = re.search(comma_pattern, action_text)
    if comma_match:
        return clean_fragment(comma_match.group(1))

    fallback_pattern = r"(?is)\b(?:d[ao]s?|n[ao]|lotad[oa](?:\(a\))?\s+n[oa]\(a\)?)\s+(.+?)(?=,\s*com efeitos\b|,\s*retroativos\b|,\s*com ônus\b|,\s*nos termos\b|\.)"
    candidates = []
    for fallback in re.finditer(fallback_pattern, action_text):
        candidate = clean_fragment(fallback.group(1))
        if not normalize(candidate).startswith("cargo "):
            candidates.append(candidate)
    return candidates[-1] if candidates else ""


def extract_effects(action_text: str) -> str:
    match = re.search(r"(?is)\b(com efeitos.*?)(?=\.|$)", action_text)
    return clean_fragment(match.group(1)) if match else ""


def extract_personnel_acts_from_text(text: str) -> list[dict]:
    acts: list[dict] = []
    for portaria, block in split_portaria_blocks(text):
        action = action_text_from_block(block)
        if not action:
            continue
        act = action_label(action)
        acts.append(
            {
                "portaria": portaria,
                "ato": act,
                "nome": extract_name(action, act),
                "cpf": extract_cpf(action),
                "cargo": extract_position(action),
                "orgao": extract_agency(action),
                "efeitos": extract_effects(action),
                "texto": action,
            }
        )
    return acts


def repair_text_encoding(text: str) -> str:
    if "Ã" not in text and "Â" not in text:
        return text
    try:
        fixed = text.encode("latin1").decode("utf-8")
    except UnicodeError:
        return text
    return fixed if fixed else text


def fold_text(text: str) -> str:
    text = repair_text_encoding(text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return text.lower()


def structured_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = repair_text_encoding(clean_fragment(raw_line))
        if not line:
            continue
        norm = fold_text(line)
        if norm.startswith("diario oficial do municipio de caruaru"):
            continue
        if "estado de pernambuco" in norm and any(
            weekday in norm
            for weekday in [
                "segunda-feira",
                "terca-feira",
                "quarta-feira",
                "quinta-feira",
                "sexta-feira",
                "sabado",
                "domingo",
            ]
        ):
            continue
        lines.append(line)
    return lines


def is_power_heading(line: str) -> bool:
    norm = fold_text(line).strip(" .:-").upper()
    return norm in {"PODER EXECUTIVO", "PODER LEGISLATIVO"}


def is_context_heading(line: str) -> bool:
    if len(line) > 140 or line.endswith(".") or ":" in line or "," in line:
        return False
    norm = fold_text(line).strip(" .:-").upper()
    if norm in {"PODER EXECUTIVO", "PODER LEGISLATIVO"}:
        return False
    if uppercase_ratio(line) < 0.7:
        return False
    return bool(
        re.match(
            r"^(SECRETARIA|CONTROLADORIA|PROCURADORIA|GABINETE|FUNDACAO|AUTARQUIA|AGENCIA|INSTITUTO|CAMARA MUNICIPAL)\b",
            norm,
        )
    )


def uppercase_ratio(line: str) -> float:
    letters = [char for char in repair_text_encoding(line) if char.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for char in letters if char.isupper()) / len(letters)


def is_heading_like_act_line(line: str, kind: str) -> bool:
    repaired = repair_text_encoding(line)
    norm = fold_text(repaired)
    if len(repaired) > 180:
        return False
    if kind in {"Portaria", "Portaria GP", "Portaria Conjunta", "Decreto Executivo", "Decreto Legislativo", "Lei", "Lei Complementar"}:
        numbered_portaria_heading = kind.startswith("Portaria") and re.match(
            r"^portaria\b.{0,80}\bn\s*[oº°]?\s*\.?\s*\d", norm
        )
        if uppercase_ratio(repaired) < 0.45 and not numbered_portaria_heading:
            return False
    if kind.startswith("Portaria"):
        if norm.count("portaria") > 1:
            return False
        if re.search(r"\bminc\b", norm):
            return False
        if "regulamenta" in norm and "," in norm:
            return False
    if kind.startswith("Decreto"):
        if "decreto municipal" in norm or "decreto pnab" in norm or "decreto de fomento" in norm:
            return False
    if kind == "Edital" and re.match(r"^edital\s+elaborado\b", norm):
        return False
    if kind == "Edital":
        if norm.startswith("edital errado"):
            return False
        if not (norm.startswith("edital de ") or re.search(r"\bn\s*[oº°]?\s*\d", norm)):
            return False
    if kind == "Aviso":
        if norm.startswith("aviso e "):
            return False
        if norm != "aviso" and not norm.startswith(("aviso de ", "aviso da ", "aviso do ")):
            return False
    return True


def structured_act_start(line: str) -> tuple[str, str] | None:
    norm = fold_text(line).strip(" .")
    # Leis vem antes dos decretos; o guarda de caixa-alta em
    # is_heading_like_act_line evita capturar citacoes como
    # "Lei nº 6.998, de..." no meio do corpo de outros atos.
    if re.match(r"^lei\s+complementar\s+n\s*[oº°]?\s*", norm):
        kind = "Lei Complementar"
        return (kind, clean_fragment(line)) if is_heading_like_act_line(line, kind) else None
    if re.match(r"^lei\s+n\s*[oº°]?\s*", norm):
        kind = "Lei"
        return (kind, clean_fragment(line)) if is_heading_like_act_line(line, kind) else None
    if re.match(r"^decreto\s+legislativo\s+n\s*[oº°]?\s*", norm):
        kind = "Decreto Legislativo"
        return (kind, clean_fragment(line)) if is_heading_like_act_line(line, kind) else None
    if re.match(r"^decreto\s+n\s*[oº°]?\s*", norm):
        kind = "Decreto Executivo"
        return (kind, clean_fragment(line)) if is_heading_like_act_line(line, kind) else None
    if re.match(r"^portaria\s+conjunta\b.*\bn\s*[oº°]?\s*", norm):
        kind = "Portaria Conjunta"
        return (kind, clean_fragment(line)) if is_heading_like_act_line(line, kind) else None
    if re.match(r"^portaria\b.*\bn\s*[oº°]?\s*", norm):
        kind = "Portaria GP" if " gp " in f" {norm} " else "Portaria"
        return (kind, clean_fragment(line)) if is_heading_like_act_line(line, kind) else None
    if norm == "notificacao":
        return ("Notificação", clean_fragment(line))
    if norm == "termo de revogacao de inscricao":
        return ("Termo de Revogação de Inscrição", clean_fragment(line))
    if norm.startswith("extrato"):
        return ("Extrato", clean_fragment(line))
    if norm.startswith("aviso"):
        kind = "Aviso"
        return (kind, clean_fragment(line)) if is_heading_like_act_line(line, kind) else None
    if norm.startswith("termo de rescisao"):
        return ("Termo de Rescisão", clean_fragment(line))
    if norm.startswith("edital"):
        kind = "Edital"
        return (kind, clean_fragment(line)) if is_heading_like_act_line(line, kind) else None
    if norm.startswith("errata"):
        return ("Errata", clean_fragment(line))
    return None


def number_from_title(title: str) -> str:
    title = repair_text_encoding(title)
    match = re.search(r"\bN\s*[º°oO]*\.?\s*([0-9]+(?:\.[0-9]+)?(?:/[0-9]{4})?)", title)
    return match.group(1) if match else ""


def date_from_title(title: str) -> str:
    title = repair_text_encoding(title)
    match = re.search(r"\bDE\s+(\d{1,2}\s+DE\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ]+(?:\s+DE)?\s+\d{4})\b", title, re.I)
    return clean_fragment(match.group(1)) if match else ""


def body_start_line(line: str) -> bool:
    norm = fold_text(line).strip()
    return bool(
        re.match(
            r"^(o\(a\)|a\(o\)|o prefeito|a secretaria|o secretario|o presidente|a camara|considerando|decreta|resolve|resolvem|art\.|anexo|capitulo|secao)\b",
            norm,
        )
    )


def ementa_from_lines(lines: list[str]) -> str:
    ementa: list[str] = []
    for line in lines[1:8]:
        if body_start_line(line) or structured_act_start(line) or is_power_heading(line) or is_context_heading(line):
            break
        ementa.append(line)
        if line.endswith(".") and len(" ".join(ementa)) > 35:
            break
    return clean_fragment(" ".join(ementa))


def title_for_annex(lines: list[str]) -> str:
    title_lines: list[str] = []
    for line in lines[:8]:
        norm = fold_text(line)
        if norm.startswith("art."):
            break
        if norm.startswith("capitulo") and title_lines:
            break
        if structured_act_start(line) and title_lines:
            break
        if norm.startswith("anexo") or norm.startswith("regimento") or norm.startswith("organograma") or line.isupper():
            title_lines.append(line)
    return clean_fragment(" - ".join(title_lines)) or lines[0]


def organs_mentioned(text: str, context_orgao: str = "") -> str:
    repaired = repair_text_encoding(text)
    found: list[str] = []
    if context_orgao:
        found.append(context_orgao)
    pattern = re.compile(
        r"\b(?:Secretaria(?:\s+(?:Municipal|Executiva))?|Controladoria(?:-Geral)?|Procuradoria(?:-Geral)?|Gabinete|Fundação|Fundacao|Autarquia|Agência|Agencia|Câmara|Camara|Poder Legislativo)\b[^.;:\n]{0,110}",
        re.I,
    )
    for match in pattern.finditer(repaired):
        fragment = clean_fragment(match.group(0))
        fragment = re.split(
            r"\b(?:no uso|com efeitos|e dá outras|e da outras|que lhe|para o|para a|, CPF)\b",
            fragment,
            maxsplit=1,
            flags=re.I,
        )[0]
        fragment = clean_fragment(fragment.strip(" ,-"))
        if len(fragment) >= 10:
            found.append(fragment)
    if "sefaz" in fold_text(repaired):
        found.append("SEFAZ")
    unique: list[str] = []
    seen: set[str] = set()
    for item in found:
        key = fold_text(item)
        if key not in seen:
            unique.append(item)
            seen.add(key)
    return "; ".join(unique[:12])


def normalize_organ_key(value: str) -> str:
    value = fold_text(value)
    value = re.sub(r"\bmunicipal\b", "", value)
    value = re.sub(r"\bdo municipio\b|\bda prefeitura\b", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -")


def title_case_organ(value: str) -> str:
    value = clean_fragment(repair_text_encoding(value).strip(" -–,;"))
    if not value:
        return value
    if value.isupper():
        small_words = {"de", "da", "do", "das", "dos", "e", "em"}
        words = []
        for word in value.lower().split():
            words.append(word.upper() if word in {"sefaz", "gp", "sad", "sms", "caruaruprev"} else word if word in small_words else word.capitalize())
        return " ".join(words)
    return value


def clean_organ_candidate(value: str) -> str:
    value = repair_text_encoding(value)
    value = re.split(
        r"\b(?:no uso|com efeitos|e dá outras|e da outras|que lhe|para o|para a|por meio|conforme|representada|compete|é órgão|e orgãos|e órgãos|assim como|, CPF)\b",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    value = re.split(r"\b(?:CAPÍTULO|CAPITULO|Seção|Secao|Art\.)\b", value, maxsplit=1, flags=re.I)[0]
    value = re.split(
        r"\b(?:as|os|ao|aos|à|às|a)\s+(?:intimações|intimacoes|atividades|atribuições|atribuicoes|competências|competencias|matérias|materias|demandas|decisões|decisoes|informações|informacoes|políticas|politicas|normas|ações|acoes)\b",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    value = re.split(r"[,.;:\n]", value, maxsplit=1)[0]
    value = re.sub(r"\s+[–-]\s*(?:SEFAZ|CARUARUPREV)\b.*$", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" -–,;")
    if len(value) < 10:
        return ""
    if normalize_organ_key(value) in {"secretaria", "secretaria municipal", "secretaria executiva"}:
        return ""
    return title_case_organ(value)


def add_organ_candidate(candidates: list[str], candidate: str) -> None:
    candidate = clean_organ_candidate(candidate)
    if not candidate:
        return
    key = normalize_organ_key(candidate)
    if not key:
        return
    for index, existing in enumerate(candidates):
        existing_key = normalize_organ_key(existing)
        if key == existing_key:
            if len(candidate) > len(existing):
                candidates[index] = candidate
            return
        if key in existing_key or existing_key in key:
            if len(candidate) > len(existing):
                candidates[index] = candidate
            return
    candidates.append(candidate)


def organs_mentioned(text: str, context_orgao: str = "") -> str:
    repaired = repair_text_encoding(text)
    candidates: list[str] = []
    patterns = [
        r"\bSecretaria(?:\s+Municipal)?\s+(?:de|da|do|dos|das)\s+[^.;:\n]{2,90}",
        r"\bControladoria(?:-Geral)?\s+(?:do|da|de)\s+[^.;:\n]{2,90}",
        r"\bProcuradoria(?:-Geral)?\s+(?:do|da|de)\s+[^.;:\n]{2,90}",
        r"\bFundação\s+(?:de|da|do)\s+[^.;:\n]{2,90}",
        r"\bFundacao\s+(?:de|da|do)\s+[^.;:\n]{2,90}",
        r"\bInstituto\s+(?:de|da|do|dos|das)\s+[^.;:\n]{2,90}",
        r"\bAutarquia\s+(?:de|da|do|dos|das)\s+[^.;:\n]{2,90}",
        r"\bAgência\s+(?:de|da|do|dos|das)\s+[^.;:\n]{2,90}",
        r"\bAgencia\s+(?:de|da|do|dos|das)\s+[^.;:\n]{2,90}",
        r"\bCâmara\s+Municipal\s+de\s+[^.;:\n]{2,70}",
        r"\bCamara\s+Municipal\s+de\s+[^.;:\n]{2,70}",
    ]
    context_key = normalize_organ_key(context_orgao)
    for pattern in patterns:
        for match in re.finditer(pattern, repaired, flags=re.I):
            candidate = clean_organ_candidate(match.group(0))
            if context_key and normalize_organ_key(candidate) == context_key:
                continue
            add_organ_candidate(candidates, candidate)
    if "sefaz" in fold_text(repaired) and not any("fazenda" in fold_text(candidate) for candidate in candidates):
        add_organ_candidate(candidates, "SEFAZ")
    return "; ".join(candidates[:8])


def canonical_agency_name(value: str) -> str:
    value = repair_text_encoding(clean_fragment(value))
    value = value.replace("–", "-").replace("—", "-")
    value = re.sub(r"\bDECARUARU\b", "DE CARUARU", value, flags=re.I)
    value = re.sub(r"\s*-\s*", " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" -.,;")
    value = re.sub(r"\s+-\s+(?:SESP|SAS|SAD|SMS|SEFAZ|SEDUC|SDR|SECOM|SDS|SEPLAG|CGM|PGM)\b\.?$", "", value, flags=re.I)
    value = value.replace("Bem - Estar", "Bem-Estar").replace("Bem - estar", "Bem-estar")
    value = re.sub(r"\s+(?:de|da|do|dos|das)$", "", value, flags=re.I).strip()
    norm = fold_text(value)
    norm = re.sub(r"\s+", " ", norm).strip(" -")
    if not norm or norm in {"secretaria", "secretaria municipal", "autarquia", "autarquia de", "controladoria-geral do municipio de"}:
        return ""
    if norm.startswith("autarquia de mobilidade"):
        return "Autarquia de Mobilidade de Caruaru (AMC)"
    if norm.startswith("autarquia de urbanizacao e meio ambiente"):
        return "Autarquia de Urbanização e Meio Ambiente de Caruaru (URB)"
    if norm.startswith("controladoria") and "municipio" in norm:
        return "Controladoria-Geral do Município"
    if norm.startswith("procuradoria") and "municipio" in norm:
        return "Procuradoria-Geral do Município"
    if norm.startswith("fundacao de cultura"):
        return "Fundação de Cultura de Caruaru"
    if norm.startswith("camara municipal"):
        return "Câmara Municipal de Caruaru"
    if norm.startswith("secretaria de administracao") or norm.startswith("secretaria municipal de administracao"):
        return "Secretaria de Administração"
    if norm.startswith("secretaria de assistencia social"):
        return "Secretaria de Assistência Social e Combate à Fome"
    if norm.startswith("secretaria da fazenda") or norm.startswith("secretaria municipal da fazenda") or norm.startswith("secretaria de fazenda"):
        return "Secretaria da Fazenda"
    if norm.startswith("secretaria de educacao") or norm.startswith("secretaria municipal de educacao"):
        return "Secretaria de Educação e Esportes"
    if norm.startswith("secretaria de seguranca municipal"):
        return "Secretaria de Segurança Municipal"
    if norm.startswith("secretaria de servicos publicos -") or norm == "secretaria de servicos publicos":
        return "Secretaria de Serviços Públicos"
    if norm.startswith("secretaria de saude") or norm.startswith("secretaria municipal de saude"):
        return "Secretaria de Saúde"
    if norm.startswith("secretaria de governo"):
        return "Secretaria de Governo e Relações Institucionais"
    if norm.startswith("secretaria de infraestrutura"):
        return "Secretaria de Infraestrutura Urbana e Obras"
    if norm.startswith("instituto de previdencia"):
        return "Instituto de Previdência dos Servidores Municipais de Caruaru"
    value = re.sub(r"\s+-\s+(?:AMC|URB|PE)$", "", value, flags=re.I)
    return title_case_organ(value)


def context_heading_is_incomplete(line: str) -> bool:
    norm = fold_text(line).strip(" -")
    return bool(re.search(r"\b(de|da|do|dos|das)$", norm))


def is_document_section_heading(line: str) -> bool:
    norm = fold_text(line).strip(" .:-")
    if re.match(r"^(anexo|capitulo|secao|organograma|regimento interno)\b", norm):
        return len(line) <= 180
    exact = {
        "licitacoes e contratos",
        "municipio de caruaru",
        "prefeitura de caruaru",
        "atos diversos",
        "poder executivo",
        "poder legislativo",
        "camara municipal de caruaru",
        "publicacoes oficiais",
        "disposicoes gerais",
        "da finalidade e competencia",
        "das atribuicoes",
    }
    if norm in exact:
        return True
    if len(line) <= 90 and uppercase_ratio(line) >= 0.85 and re.search(
        r"\b(LICITACOES|CONTRATOS|SECRETARIA|MUNICIPIO|PREFEITURA|COMISSAO|FUNDO|PODER|CAMARA|ATOS)\b",
        norm.upper(),
    ):
        return True
    return False


ROLE_PATTERN = (
    r"Prefeito|Vice-Prefeito|Procurador(?:\s+|-)?Geral do Munic[ií]pio|"
    r"Secret[aá]ri[ao](?:\s+Municipal)?(?:\s+(?:de|da|do)\s+[^.;]{1,70})?|"
    r"Gestor(?:a)?(?:/Secret[aá]ri[ao])?|Controlador(?:\s+|-)?Geral do Munic[ií]pio|"
    r"Presidente|Diretor(?:a)?(?:/Presidente|\s+Presidente)?|Vereador(?:a)?|Superintendente"
)


UPPER_NAME_WORD_PATTERN = r"(?:[A-Z\u00c0-\u00de]{2,}(?:'[A-Z\u00c0-\u00de]{2,})?|D[AEOS]?|DE|DO|DA|DOS|DAS|E)"


def normalize_signature_dash(text: str) -> str:
    return clean_fragment(text).replace("–", "-").replace("—", "-")


def authority_pair_from_text(text: str) -> tuple[str, str] | None:
    text = normalize_signature_dash(text)
    pattern = re.compile(
        rf"([A-ZÁÀÂÃÉÈÊÍÓÔÕÚÇ][A-Za-zÁÀÂÃÉÈÊÍÓÔÕÚÜÇáàâãéèêíóôõúüç.'ºª-]+(?:\s+[A-ZÁÀÂÃÉÈÊÍÓÔÕÚÇ][A-Za-zÁÀÂÃÉÈÊÍÓÔÕÚÜÇáàâãéèêíóôõúüç.'ºª-]+){{1,8}})\s*-\s*({ROLE_PATTERN}[^.;]*)",
        re.I,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    name = clean_fragment(match.group(1))
    if "." in name:
        name = clean_fragment(name.rsplit(".", 1)[-1])
    return name, clean_fragment(match.group(2).rstrip("."))


def looks_like_role(line: str) -> bool:
    norm = fold_text(line).strip(" .")
    return bool(
        re.match(rf"^({ROLE_PATTERN})\.?$", normalize_signature_dash(line), re.I)
        or re.match(r"^(secretario|secretaria|prefeito|gestor|gestora|diretor|diretora|presidente|procurador|controlador|vereador|superintendente)\b", norm)
    )


def looks_like_person_name(line: str) -> bool:
    line = clean_fragment(line)
    norm = fold_text(line)
    if is_document_section_heading(line):
        return False
    if re.search(r"\b(secretaria|prefeitura|municipio|comissao|licitacao|contrato|extrato|poder|fundo)\b", norm):
        return False
    words = [word for word in re.split(r"\s+", line) if word]
    return 2 <= len(words) <= 8 and sum(any(char.isalpha() for char in word) for word in words) >= 2


def inline_authority_suffix(text: str) -> tuple[str, str, str] | None:
    text = normalize_signature_dash(text)
    pattern = re.compile(
        rf"({UPPER_NAME_WORD_PATTERN}(?:\s+{UPPER_NAME_WORD_PATTERN}){{1,10}})\s+((?i:{ROLE_PATTERN})[^.;]*)\.?$"
    )
    for match in reversed(list(pattern.finditer(text))):
        name = clean_fragment(match.group(1))
        role = clean_fragment(match.group(2).rstrip("."))
        if looks_like_person_name(name) and looks_like_role(role):
            before = clean_fragment(text[: match.start()].rstrip())
            return before, name, role
    return None


def normalize_inline_signature_in_text(text: str) -> str:
    normalized = text.replace("–", "-").replace("—", "-")
    pattern = re.compile(
        rf"({UPPER_NAME_WORD_PATTERN}(?:\s+{UPPER_NAME_WORD_PATTERN}){{1,10}})\s+((?i:{ROLE_PATTERN})[^.;]*)\.?$"
    )
    for match in reversed(list(pattern.finditer(normalized))):
        name = clean_fragment(match.group(1))
        role = clean_fragment(match.group(2).rstrip("."))
        if looks_like_person_name(name) and looks_like_role(role):
            before = normalized[: match.start()].rstrip()
            if before:
                return "\n".join([before, name, role])
    return text


def authority_pair_without_dash_from_text(text: str) -> tuple[str, str] | None:
    split = inline_authority_suffix(text)
    if not split:
        return None
    return split[1], split[2]


LOCATION_DATE_RE = re.compile(
    r"(Caruaru|Prefeitura Municipal de Caruaru|Palácio Jaime Nejaim|Palacio Jaime Nejaim|Câmara Municipal|Camara Municipal),?\s*(?:de\s+)?\d{1,2}\s+de\s+[^.]{3,80}?\s+de\s+\d{4}\.?",
    re.I,
)


DATA_DATE_RE = re.compile(r"\bData:\s*\d{1,2}/\d{1,2}/\d{4}\.?", re.I)


def signature_tail_after_date(line: str) -> str:
    line = clean_fragment(line)
    for pattern in (LOCATION_DATE_RE, DATA_DATE_RE):
        match = pattern.search(line)
        if match:
            return clean_fragment(line[match.end() :].strip(" ."))
    return ""


def line_has_signature_marker(line: str) -> bool:
    norm = fold_text(line)
    return bool(
        LOCATION_DATE_RE.search(line)
        or DATA_DATE_RE.search(line)
        or re.search(r"\s[-–—]\s*(secretario|secretaria|gestor|gestora|prefeito|diretor|diretora|presidente)\b", norm)
        or re.search(r"\b(secretario|secretaria|prefeito|gestor|gestora|diretor|diretora|presidente)\b\.?$", norm)
    )


def split_before_embedded_section_heading(line: str) -> tuple[str, str] | None:
    if not line_has_signature_marker(line):
        return None
    folded = fold_text(line)
    markers = [
        " municipio de caruaru",
        " municipio de",
        " prefeitura de caruaru",
        " fundo municipal",
        " atos diversos",
        " licitacoes e contratos",
        " poder executivo",
        " poder legislativo",
        " camara municipal de caruaru",
        " comissao permanente",
    ]
    indexes = [folded.find(marker) for marker in markers if folded.find(marker) > 0]
    if not indexes:
        return None
    start = min(indexes)
    return clean_fragment(line[:start]), clean_fragment(line[start:])


def month_label(month: str) -> str:
    year, number = month.split("-")
    names = {
        1: "janeiro",
        2: "fevereiro",
        3: "março",
        4: "abril",
        5: "maio",
        6: "junho",
        7: "julho",
        8: "agosto",
        9: "setembro",
        10: "outubro",
        11: "novembro",
        12: "dezembro",
    }
    return f"{names[int(number)]}/{year}"


def category_for_act(kind: str, text: str) -> str:
    norm = fold_text(text)
    if "regimento interno" in norm and (
        kind.startswith("Anexo")
        or kind.startswith("Decreto")
        or "homologa o regimento interno" in norm
        or "regimento interno da secretaria" in norm
    ):
        return "Regimento interno"
    if "organograma" in norm:
        return "Organograma"
    if re.search(r"processo\s+seletivo", norm) or "selecao simplificada" in norm:
        return "Processo seletivo"
    if kind == "Termo de Revogação de Inscrição" or "revogacao de inscricao" in norm:
        return "Revogação de inscrição"
    if kind == "Notificação" or "notificar" in norm:
        return "Notificação"
    if (
        "nomear" in norm
        or "exonerar" in norm
        or "designar" in norm
        or "conceder licenca" in norm
        or "licenca premio" in norm
        or re.search(r"\bconceder\b.{0,300}\blicenca\b", norm)
    ):
        return "Pessoal"
    if "medalha" in norm or "honra ao merito" in norm or "honraria" in norm:
        return "Honraria"
    if "termo aditivo" in norm or "contrato" in norm or "rescisao" in norm:
        return "Contratos"
    if "licitacao" in norm or "pregao" in norm or "sessao" in norm:
        return "Licitação"
    if "tribut" in norm or "sefaz" in norm or "fazenda" in norm or "receita municipal" in norm:
        return "Fazenda/tributos"
    return "Administrativo"


def format_authority_tail(signature_lines: list[str]) -> str:
    cleaned_lines: list[str] = []
    for raw_line in signature_lines:
        line = clean_fragment(raw_line)
        norm = fold_text(line)
        if not line:
            continue
        if "assinado de forma digital" in norm or norm.startswith("dados:"):
            continue
        if re.search(r":[0-9]{6,}", line):
            continue
        if is_document_section_heading(line):
            if cleaned_lines:
                break
            continue
        cleaned_lines.append(line)
    signature_lines = cleaned_lines

    pairs: list[str] = []
    joined_pair = authority_pair_from_text(" ".join(signature_lines))
    if joined_pair:
        pairs.append(f"{joined_pair[0]} - {joined_pair[1]}")

    for line in signature_lines:
        pair = authority_pair_from_text(line)
        if pair:
            pairs.append(f"{pair[0]} - {pair[1]}")
        inline_pair = authority_pair_without_dash_from_text(line)
        if inline_pair:
            pairs.append(f"{inline_pair[0]} - {inline_pair[1]}")

    index = 0
    while index < len(signature_lines):
        line = clean_fragment(signature_lines[index])
        next_line = clean_fragment(signature_lines[index + 1]) if index + 1 < len(signature_lines) else ""
        if looks_like_person_name(line) and looks_like_role(next_line):
            pairs.append(f"{line} - {next_line.rstrip('.')}")
            index += 2
            continue
        if uppercase_ratio(line) >= 0.75 and next_line and uppercase_ratio(next_line) < 0.6 and looks_like_role(next_line):
            pairs.append(f"{line} - {next_line.rstrip('.')}")
            index += 2
            continue
        index += 1

    if pairs:
        unique_pairs = []
        for pair in dict.fromkeys(pairs):
            if any(other != pair and other.endswith(pair) for other in pairs):
                continue
            unique_pairs.append(pair)
        return "; ".join(unique_pairs)

    text = normalize_signature_dash(" ".join(signature_lines))
    pair = authority_pair_from_text(text)
    if pair:
        return f"{pair[0]} - {pair[1]}"
    title_pattern = (
        r"Prefeito|Vice-Prefeito|Procurador(?:\s+|-)?Geral do Município|"
        r"Secretári[ao](?:\s+Municipal)?(?:\s+(?:de|da|do)\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][^;]{0,70})?|"
        r"Controlador(?:\s+|-)?Geral do Município|Presidente|Diretor(?:a)?(?:\s+Presidente)?|"
        r"Vereador(?:a)?|Superintendente"
    )
    pattern = re.compile(
        rf"([A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÇ\s]{{5,}}?)\s+({title_pattern})(?=\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ]{{3,}}\s|$)",
        re.I,
    )
    for match in pattern.finditer(text):
        name = clean_fragment(match.group(1))
        role = clean_fragment(match.group(2))
        if name and role:
            pairs.append(f"{name} - {role}")
    return "; ".join(pairs) if pairs else text


def authorities_from_lines(lines: list[str]) -> str:
    signatures: list[str] = []
    capture = False
    for line in lines:
        norm = fold_text(line)
        tail = signature_tail_after_date(line)
        if tail:
            signatures.append(tail)
            capture = True
            continue
        if is_location_line(line) or re.match(r"^(caruaru|prefeitura municipal de caruaru|palacio|camara municipal)", norm) or DATA_DATE_RE.search(line):
            capture = True
            continue
        if capture and not structured_act_start(line):
            signatures.append(line)
    if not signatures:
        for line in lines[-12:]:
            if inline_authority_suffix(line):
                signatures.append(line)
            elif looks_like_person_name(line) or looks_like_role(line):
                signatures.append(line)
    return format_authority_tail(signatures[:18])


def is_location_line(line: str) -> bool:
    line = repair_text_encoding(line)
    return bool(LOCATION_DATE_RE.match(line) or DATA_DATE_RE.match(line))


def is_command_line(line: str) -> bool:
    return bool(re.match(r"^(DECRETA|RESOLVE|RESOLVEM|R\s*e\s*s\s*o\s*l\s*v\s*e)\s*:?", repair_text_encoding(line), re.I))


def append_structured_part(
    parts: list[dict],
    ato_id: str,
    order: int,
    tipo: str,
    numero: str,
    titulo: str,
    body: list[str],
) -> int:
    text = clean_fragment(" ".join(body))
    if not titulo and not text:
        return order
    parts.append(
        {
            "ato_id": ato_id,
            "ordem": order,
            "tipo_parte": tipo,
            "numero": numero,
            "titulo": clean_fragment(titulo),
            "texto": text,
        }
    )
    return order + 1


def extract_parts_from_act_lines(ato_id: str, lines: list[str], ementa: str) -> list[dict]:
    parts: list[dict] = []
    order = 1
    if ementa:
        order = append_structured_part(parts, ato_id, order, "ementa", "", "Ementa", [ementa])

    current_type = ""
    current_number = ""
    current_title = ""
    current_body: list[str] = []
    signature_mode = False

    def flush_current() -> None:
        nonlocal order, current_type, current_number, current_title, current_body
        if current_type:
            order = append_structured_part(parts, ato_id, order, current_type, current_number, current_title, current_body)
        current_type = ""
        current_number = ""
        current_title = ""
        current_body = []

    index = 1
    while index < len(lines):
        line = lines[index]
        norm = fold_text(line).strip()

        if is_location_line(line):
            flush_current()
            current_type = "assinatura"
            current_title = "Assinaturas"
            current_body = [line]
            signature_mode = True
            index += 1
            continue
        article = re.match(r"^Art\.\s*(\d+)", line, re.I)
        inciso = re.match(r"^([IVXLCDM]+)[\.\-)]\s+(.+)", line, re.I)

        if signature_mode:
            starts_structured_after_signature = (
                norm.startswith(("anexo", "capitulo", "secao", "considerando"))
                or bool(article)
                or bool(inciso)
                or is_command_line(line)
            )
            if not starts_structured_after_signature:
                current_body.append(line)
                index += 1
                continue
            flush_current()
            signature_mode = False

        if norm.startswith("anexo"):
            flush_current()
            current_type = "anexo"
            current_title = line
            current_body = []
        elif norm.startswith("capitulo"):
            flush_current()
            current_type = "capitulo"
            current_title = line
            current_body = []
            if index + 1 < len(lines):
                next_line = lines[index + 1]
                next_norm = fold_text(next_line)
                if not next_norm.startswith(("art.", "secao", "capitulo", "anexo")) and not structured_act_start(next_line):
                    current_title = f"{current_title} - {next_line}"
                    index += 1
        elif norm.startswith("secao"):
            flush_current()
            current_type = "secao"
            current_title = line
            current_body = []
            if index + 1 < len(lines):
                next_line = lines[index + 1]
                next_norm = fold_text(next_line)
                if not next_norm.startswith(("art.", "secao", "capitulo", "anexo")) and not structured_act_start(next_line):
                    current_title = f"{current_title} - {next_line}"
                    index += 1
        elif article:
            flush_current()
            current_type = "artigo"
            current_number = article.group(1)
            current_title = f"Art. {current_number}"
            current_body = [line]
        elif inciso:
            flush_current()
            current_type = "inciso"
            current_number = inciso.group(1).upper()
            current_title = f"Inciso {current_number}"
            current_body = [line]
        elif norm.startswith("considerando"):
            flush_current()
            current_type = "considerando"
            current_title = "CONSIDERANDO"
            current_body = [line]
        elif is_command_line(line):
            flush_current()
            order = append_structured_part(parts, ato_id, order, "comando", "", line, [])
        else:
            if current_type:
                current_body.append(line)
        index += 1

    flush_current()
    return parts


def build_structured_act(raw_act: dict, sequence: int, previous_by_number: dict[str, str]) -> tuple[dict, list[dict]]:
    lines = raw_act["lines"]
    header = lines[0]
    kind = raw_act["tipo"]
    number = number_from_title(header)
    body = "\n".join(lines)
    starts_with_annex = len(lines) > 1 and fold_text(lines[1]).startswith("anexo")
    if kind == "Decreto Executivo" and starts_with_annex:
        kind = "Anexo/Regimento" if "regimento interno" in fold_text(body) else "Anexo"

    ementa = "" if kind.startswith("Anexo") else ementa_from_lines(lines)
    title = title_for_annex(lines) if kind.startswith("Anexo") else header
    ato_pai = previous_by_number.get(number, "") if kind.startswith("Anexo") else ""
    tem_anexo = "sim" if ("anexo" in fold_text(body) or kind.startswith("Anexo")) else "nao"
    ato_id = f"ato-{sequence:04d}"
    context_orgao = canonical_agency_name(raw_act.get("orgao_contexto", "")) or raw_act.get("orgao_contexto", "")
    mentioned_organs = organs_mentioned(body, context_orgao)
    if not context_orgao and kind.startswith("Anexo") and mentioned_organs:
        context_orgao = mentioned_organs.split(";", 1)[0].strip()
        mentioned_organs = organs_mentioned(body, context_orgao)
    readable_text = body if kind.startswith("Anexo") else readable_structured_body(lines)
    readable_text = normalize_inline_signature_in_text(readable_text)
    act = {
        "ato_id": ato_id,
        "poder": raw_act.get("poder", ""),
        "orgao_contexto": context_orgao,
        "tipo": kind,
        "categoria": category_for_act(kind, body),
        "identificacao": header,
        "titulo": title,
        "numero": number,
        "data_ato": date_from_title(header),
        "ementa": ementa,
        "autoridades": authorities_from_lines(lines),
        "orgaos_mencionados": mentioned_organs,
        "tem_anexo": tem_anexo,
        "ato_pai": ato_pai,
        "texto": readable_text,
    }
    parts = extract_parts_from_act_lines(ato_id, lines, ementa)
    return act, parts


def readable_structured_body(lines: list[str]) -> str:
    paragraphs: list[str] = []
    current: list[str] = []
    signature_mode = False
    expanded_lines: list[str] = []
    for line in lines:
        expanded_lines.extend(split_readable_line(line))
    expanded_lines = merge_broken_signature_lines(expanded_lines)
    for index, line in enumerate(expanded_lines):
        norm = fold_text(line)
        next_line = clean_fragment(expanded_lines[index + 1]) if index + 1 < len(expanded_lines) else ""
        starts_signature = looks_like_person_name(line) and looks_like_role(next_line)
        continues_signature = signature_mode and (looks_like_role(line) or looks_like_person_name(line))
        if signature_mode and norm.startswith(("anexo", "capitulo", "secao", "art.")):
            signature_mode = False
        elif signature_mode and is_document_section_heading(line):
            break
        starts_new = (
            structured_act_start(line)
            or body_start_line(line)
            or norm.startswith("ementa:")
            or norm.startswith(("considerando", "art.", "anexo", "capitulo", "secao"))
            or is_command_line(line)
            or is_location_line(line)
            or starts_signature
            or continues_signature
            or signature_mode
        )
        if starts_new and current:
            paragraphs.append(clean_fragment(" ".join(current)))
            current = []
        current.append(line)
        if is_location_line(line) or starts_signature:
            signature_mode = True
    if current:
        paragraphs.append(clean_fragment(" ".join(current)))
    return "\n\n".join(paragraphs)


def merge_broken_signature_lines(lines: list[str]) -> list[str]:
    merged: list[str] = []
    index = 0
    while index < len(lines):
        line = clean_fragment(lines[index])
        next_line = clean_fragment(lines[index + 1]) if index + 1 < len(lines) else ""
        if next_line and looks_like_role(next_line) and line.rstrip().endswith(("–", "-", "—")):
            merged.append(f"{line.rstrip('–-— ')} - {next_line}")
            index += 2
            continue
        if (
            next_line
            and len(line.split()) == 1
            and line[:1].isupper()
            and authority_pair_from_text(next_line)
        ):
            merged.append(f"{line} {next_line}")
            index += 2
            continue
        merged.append(line)
        index += 1
    return merged


def split_readable_line(line: str) -> list[str]:
    line = clean_fragment(line)
    if not line:
        return []

    section_split = split_before_embedded_section_heading(line)
    if section_split:
        line = section_split[0]
        if not line:
            return []

    location = LOCATION_DATE_RE.search(line)
    if location:
        if location.start() > 0:
            before = clean_fragment(line[: location.start()])
            after = clean_fragment(line[location.start() :])
            return ([before] if before else []) + split_readable_line(after)
        tail_with_punctuation = clean_fragment(line[location.end() :].strip())
        head = clean_fragment(line[: location.end()])
        if tail_with_punctuation.startswith(";"):
            inline_signature = inline_authority_suffix(tail_with_punctuation)
            if inline_signature:
                before, name, role = inline_signature
                return [clean_fragment(f"{head}{before}"), name, role]
            return [clean_fragment(f"{head}{tail_with_punctuation}")]
        tail = clean_fragment(tail_with_punctuation.strip(" ."))
        return [head] + ([tail] if tail else [])

    data = DATA_DATE_RE.search(line)
    if data:
        if data.start() > 0:
            before = clean_fragment(line[: data.start()])
            after = clean_fragment(line[data.start() :])
            return ([before] if before else []) + split_readable_line(after)
        tail = clean_fragment(line[data.end() :].strip(" ."))
        head = clean_fragment(line[: data.end()])
        return [head] + ([tail] if tail else [])

    inline_signature = inline_authority_suffix(line)
    if inline_signature:
        before, name, role = inline_signature
        before_parts = split_readable_line(before) if before else []
        return before_parts + [name, role]

    return [line]


def current_act_has_end_marker(current: dict | None) -> bool:
    if not current:
        return True
    tail = " ".join(current["lines"][-10:])
    norm_tail = fold_text(tail)
    if re.search(r"\b(caruaru|prefeitura municipal de caruaru|palacio|camara municipal),?\s+(?:de\s+)?\d{1,2}\s+de\s+", norm_tail):
        return True
    signature_titles = [
        "prefeito",
        "secretario",
        "secretaria",
        "presidente",
        "diretor",
        "diretora",
        "controlador",
        "procurador",
        "vereador",
    ]
    return any(re.fullmatch(rf".*\b{title}\b\.?", fold_text(line)) for title in signature_titles for line in current["lines"][-6:])


def next_nonempty_line(lines: list[str], index: int) -> str:
    for next_index in range(index + 1, len(lines)):
        if lines[next_index].strip():
            return lines[next_index]
    return ""


def extract_structured_acts_from_text(text: str) -> tuple[list[dict], list[dict]]:
    raw_acts: list[dict] = []
    current: dict | None = None
    current_power = ""
    current_orgao = ""

    def close_current() -> None:
        nonlocal current
        if current and len(current["lines"]) > 1:
            raw_acts.append(current)
        current = None

    lines = structured_lines(text)
    skip_indexes: set[int] = set()
    for index, line in enumerate(lines):
        if index in skip_indexes:
            continue
        if is_power_heading(line):
            close_current()
            current_power = clean_fragment(line).upper()
            current_orgao = ""
            continue

        start = structured_act_start(line)
        if start:
            if (
                current
                and start[0] == "Edital"
                and "concurso publico" in fold_text(" ".join(current["lines"][-4:]))
                and any(fold_text(item).startswith("anexo") for item in current["lines"])
            ):
                current["lines"].append(start[1])
                continue
            close_current()
            current = {
                "tipo": start[0],
                "header": start[1],
                "poder": current_power,
                "orgao_contexto": current_orgao,
                "lines": [start[1]],
            }
            continue

        if current and current_act_has_end_marker(current) and is_document_section_heading(line):
            if fold_text(line).startswith("anexo"):
                current["lines"].append(line)
                continue
            close_current()
            continue

        if current:
            embedded_section = split_before_embedded_section_heading(line)
            if embedded_section:
                before_section, _section = embedded_section
                if before_section:
                    current["lines"].append(before_section)
                close_current()
                continue

        if is_context_heading(line):
            next_line = next_nonempty_line(lines, index)
            agency_line = line
            if context_heading_is_incomplete(line) and next_line and not structured_act_start(next_line):
                agency_line = f"{line} {next_line}"
                skip_indexes.add(index + 1)
            agency = canonical_agency_name(agency_line)
            next_is_act = bool(structured_act_start(next_line))
            if current is None or next_is_act or current_act_has_end_marker(current):
                close_current()
                current_orgao = agency
            elif current:
                current["lines"].append(line)
            continue

        if current:
            current["lines"].append(line)

    close_current()

    acts: list[dict] = []
    parts: list[dict] = []
    previous_decree_by_number: dict[str, str] = {}
    for sequence, raw_act in enumerate(raw_acts, start=1):
        act, act_parts = build_structured_act(raw_act, sequence, previous_decree_by_number)
        acts.append(act)
        parts.extend(act_parts)
        if (
            (act["tipo"].startswith("Decreto") or act["tipo"] in ("Lei", "Lei Complementar"))
            and act["numero"]
            and not act["tipo"].startswith("Anexo")
        ):
            previous_decree_by_number[act["numero"]] = act["identificacao"]
    return acts, parts


def top_lines(text: str, terms: list[str], limit: int = 12) -> list[str]:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    chosen: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if len(line) < 25:
            continue
        norm = normalize(line)
        if any(normalize(term) in norm for term in terms):
            compact = line[:260]
            key = normalize(compact)
            if key not in seen:
                chosen.append(compact)
                seen.add(key)
        if len(chosen) >= limit:
            break
    return chosen


def row_date(row: dict) -> date | None:
    value = (row.get("data") or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return None


def merge_existing_csv_rows(
    csv_name: str,
    new_rows: list[dict],
    start_date: date,
    end_date: date,
    replace_keys: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Substitui apenas as linhas dentro da janela [start_date, end_date];
    o que ja existia antes E depois da janela e preservado, permitindo
    preencher periodos antigos em blocos sem perder os recentes."""
    path = OUTPUT_ROOT / csv_name
    if not path.exists():
        return new_rows
    with path.open(encoding="utf-8-sig", newline="") as handle:
        old_rows = list(csv.DictReader(handle, delimiter=";"))
    if replace_keys is not None:
        # No modo incremental, substitui apenas os diarios realmente
        # processados. Isso preserva outras edicoes do mesmo dia.
        preserved = [
            row
            for row in old_rows
            if ((row.get("data") or "").strip(), (row.get("edicao") or "").strip())
            not in replace_keys
        ]
        return preserved + new_rows
    antes: list[dict] = []
    depois: list[dict] = []
    for row in old_rows:
        parsed = row_date(row)
        if parsed is None or parsed < start_date:
            antes.append(row)
        elif parsed > end_date:
            depois.append(row)
    return antes + new_rows + depois


def split_terms(value: str) -> list[str]:
    return [clean_fragment(part) for part in value.split(",") if clean_fragment(part)]


def split_pipe(value: str) -> list[str]:
    return [clean_fragment(part) for part in value.split("|") if clean_fragment(part)]


def monthly_details_from_rows(rows: list[dict]) -> dict[str, list[dict]]:
    details: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        month = row.get("ano_mes") or ""
        if not month:
            continue
        details[month].append(
            {
                "date": row.get("data") or "",
                "edition": row.get("edicao") or "",
                "focus_terms": split_terms(row.get("termos_fazenda") or ""),
                "general_terms": split_terms(row.get("termos_gerais") or ""),
                "snippets": split_pipe(row.get("trechos_fazenda") or ""),
                "general_lines": split_pipe(row.get("trechos_nomeacao_exoneracao") or ""),
            }
        )
    return details


def write_csv(rows: list[dict]) -> None:
    OUTPUT_ROOT.mkdir(exist_ok=True)
    fieldnames = [
        "data",
        "ano_mes",
        "edicao",
        "arquivo_pdf",
        "arquivo_texto",
        "arquivo_leitura",
        "termos_fazenda",
        "termos_pessoal",
        "tem_nomeacao",
        "tem_exoneracao",
        "termos_gerais",
        "trechos_fazenda",
        "trechos_nomeacao_exoneracao",
    ]
    with (OUTPUT_ROOT / "indice.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def write_personnel_csv(acts: list[dict]) -> None:
    fieldnames = [
        "data",
        "ano_mes",
        "edicao",
        "portaria",
        "ato",
        "nome",
        "cpf",
        "cargo",
        "orgao",
        "efeitos",
        "texto",
        "arquivo_pdf",
        "arquivo_leitura",
    ]
    with (OUTPUT_ROOT / "atos_pessoal.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(acts)


def write_personnel_summary(acts: list[dict]) -> None:
    by_agency: dict[str, Counter[str]] = defaultdict(Counter)
    for act in acts:
        agency = act["orgao"] or "Orgao nao identificado"
        by_agency[agency][act["ato"]] += 1

    lines = [
        "# Atos de pessoal por secretaria/orgao",
        "",
        "Este arquivo consolida nomeacoes, exoneracoes e outros atos de pessoal extraidos das portarias dos diarios.",
        "",
    ]
    for agency in sorted(by_agency):
        total = sum(by_agency[agency].values())
        details = ", ".join(f"{kind}: {count}" for kind, count in by_agency[agency].most_common())
        lines.append(f"## {agency}")
        lines.append("")
        lines.append(f"- Total de atos: {total}")
        lines.append(f"- Distribuicao: {details}")
        lines.append("")
    (OUTPUT_ROOT / "atos_pessoal_por_secretaria.md").write_text("\n".join(lines), encoding="utf-8")


def write_structured_acts_csv(acts: list[dict]) -> None:
    fieldnames = [
        "ato_id",
        "data",
        "ano_mes",
        "edicao",
        "poder",
        "orgao_contexto",
        "tipo",
        "categoria",
        "identificacao",
        "titulo",
        "numero",
        "data_ato",
        "ementa",
        "autoridades",
        "orgaos_mencionados",
        "tem_anexo",
        "ato_pai",
        "texto",
        "arquivo_pdf",
        "arquivo_leitura",
    ]
    with (OUTPUT_ROOT / "atos_estruturados.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(acts)


def write_structured_parts_csv(parts: list[dict]) -> None:
    fieldnames = [
        "ato_id",
        "data",
        "ano_mes",
        "edicao",
        "tipo_ato",
        "identificacao",
        "ordem",
        "tipo_parte",
        "numero",
        "titulo",
        "texto",
    ]
    with (OUTPUT_ROOT / "partes_dos_atos.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(parts)


def existing_diary_keys(csv_name: str, candidates: set[tuple[str, str]]) -> set[tuple[str, str]]:
    if not candidates:
        return set()
    path = OUTPUT_ROOT / csv_name
    if not path.exists():
        return set()
    found: set[tuple[str, str]] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            key = ((row.get("data") or "").strip(), (row.get("edicao") or "").strip())
            if key in candidates:
                found.add(key)
                if found == candidates:
                    break
    return found


def append_csv_rows(csv_name: str, rows: list[dict], fieldnames: list[str]) -> None:
    if not rows:
        return
    OUTPUT_ROOT.mkdir(exist_ok=True)
    path = OUTPUT_ROOT / csv_name
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
        if needs_header:
            writer.writeheader()
        writer.writerows(rows)


def write_structured_summary(acts: list[dict]) -> None:
    by_category = Counter(act["categoria"] or "Sem categoria" for act in acts)
    by_type = Counter(act["tipo"] or "Sem tipo" for act in acts)
    by_agency = Counter((act["orgao_contexto"] or "Sem secretaria/orgao de contexto") for act in acts)
    by_month: dict[str, Counter[str]] = defaultdict(Counter)
    fazenda_acts: list[dict] = []
    for act in acts:
        by_month[act["ano_mes"]][act["categoria"]] += 1
        if "fazenda" in fold_text(" ".join([act["orgao_contexto"], act["orgaos_mencionados"], act["texto"]])) or "sefaz" in fold_text(act["texto"]):
            fazenda_acts.append(act)

    lines = [
        "# Atos estruturados por categoria",
        "",
        "Este arquivo organiza decretos, portarias, anexos, notificacoes, termos, extratos e outros atos com inicio/fim mais claros.",
        "",
        f"Total de atos estruturados: {len(acts)}",
        "",
        "## Categorias",
        "",
    ]
    for category, count in by_category.most_common():
        lines.append(f"- {category}: {count}")
    lines.extend(["", "## Tipos de ato", ""])
    for kind, count in by_type.most_common():
        lines.append(f"- {kind}: {count}")
    lines.extend(["", "## Secretarias/orgaos de contexto mais frequentes", ""])
    for agency, count in by_agency.most_common(30):
        lines.append(f"- {agency}: {count}")
    lines.extend(["", "## Por mes", ""])
    for month in sorted(by_month):
        categories = ", ".join(f"{name}: {count}" for name, count in by_month[month].most_common(8))
        lines.append(f"- {month}: {categories}")
    lines.extend(["", "## Atos com mencao a Fazenda/SEFAZ", ""])
    for act in fazenda_acts[:120]:
        subject = act["ementa"] or act["titulo"] or act["identificacao"]
        lines.append(f"- {act['data']} - {act['tipo']} - {subject}")
    (OUTPUT_ROOT / "atos_estruturados_por_categoria.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


TARIFF_VEHICLES = [
    "CAMINHÃO (TOCO)",
    "UTILITÁRIO",
    "CARRO PIPA",
    "BITRUCK",
    "CARRETA",
    "CARROÇA",
    "F-4000",
    "TRUCK",
]


def is_tariff_annex(paragraph: str) -> bool:
    norm = fold_text(paragraph)
    return norm.startswith("anexo i tarifas corrigidas pelo indice ipca") and "permissionario tipo de veiculo" in norm


def money_token(value: str) -> str:
    value = clean_fragment(value).strip()
    if not value or value == "-":
        return "-"
    return value if value.startswith("R$") else f"R$ {value}"


def vehicle_value_rows(text: str) -> list[tuple[str, str]]:
    vehicle_pattern = "|".join(re.escape(vehicle) for vehicle in TARIFF_VEHICLES)
    pattern = re.compile(rf"\b({vehicle_pattern})\s+R\$\s*([0-9.]+,[0-9]{{2}})", re.I)
    return [(clean_fragment(match.group(1).upper()), f"R$ {match.group(2)}") for match in pattern.finditer(text)]


def vehicle_fraction_rows(text: str) -> list[tuple[str, list[str]]]:
    vehicle_pattern = "|".join(re.escape(vehicle) for vehicle in TARIFF_VEHICLES)
    matches = list(re.finditer(rf"\b({vehicle_pattern})\b", text, re.I))
    rows: list[tuple[str, list[str]]] = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        vehicle = clean_fragment(match.group(1).upper())
        tail = clean_fragment(text[match.end() : next_start])
        values = re.findall(r"(?:R\$\s*)?([0-9.]+,[0-9]{2}|-)", tail)
        if values:
            rows.append((vehicle, [money_token(value) for value in values[:4]]))
    return rows


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_annex_data_table(paragraph: str) -> str:
    month_match = re.search(r"\bMês\s+Data\b", paragraph, re.I)
    if month_match:
        title = clean_fragment(paragraph[: month_match.start()])
        rest = paragraph[month_match.end() :]
        month_names = "Janeiro|Fevereiro|Março|Marco|Abril|Maio|Junho|Julho|Agosto|Setembro|Outubro|Novembro|Dezembro"
        rows = [
            [clean_fragment(match.group(1)), clean_fragment(match.group(2))]
            for match in re.finditer(rf"\b({month_names}(?:\s+[–-]\s+13º\s+Salário)?)\s+(\d{{2}}/\d{{2}}/\d{{4}})", rest, re.I)
        ]
        if rows:
            return (
                '<div class="annex-table">'
                f'<p class="doc-heading"><strong>{html.escape(title)}</strong></p>'
                + render_table(["Mês", "Data"], rows)
                + "</div>"
            )

    holiday_match = re.search(r"\bDATA\s+DENOMINAÇÃO\s+NATUREZA\b", paragraph, re.I)
    if holiday_match:
        title = clean_fragment(paragraph[: holiday_match.start()])
        rest = paragraph[holiday_match.end() :]
        month_name = r"[A-Za-zçÇãõáéíóúâêôÁÉÍÓÚÂÊÔ]+"
        listed_days = rf"\d{{1,2}}(?:º)?(?:\s*,\s*\d{{1,2}})*\s+e\s+\d{{1,2}}\s+de\s+{month_name}"
        single_day = rf"\d{{1,2}}(?:º)?\s+de\s+{month_name}"
        date_re = rf"{listed_days}|{single_day}"
        matches = list(re.finditer(date_re, rest))
        rows: list[list[str]] = []
        for index, match in enumerate(matches):
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(rest)
            date_value = clean_fragment(match.group(0))
            tail = clean_fragment(rest[match.end() : next_start])
            nature_match = re.search(
                r"\b(Feriado\s+(?:nacional|municipal)\b.*|Ponto Facultativo\b.*|Lei Estadual\b.*)",
                tail,
                re.I,
            )
            if nature_match:
                name = clean_fragment(tail[: nature_match.start()])
                nature = clean_fragment(nature_match.group(1))
                nature = re.sub(r"/\s+(\d)", r"/\1", nature)
            else:
                name = tail
                nature = ""
            if name:
                rows.append([date_value, name, nature])
        if rows:
            return (
                '<div class="annex-table">'
                f'<p class="doc-heading"><strong>{html.escape(title)}</strong></p>'
                + render_table(["Data", "Denominação", "Natureza"], rows)
                + "</div>"
            )
    return ""


def render_tariff_annex(paragraph: str) -> str:
    rest_start = re.search(r"\b1\.\s+", paragraph)
    table_text = paragraph[: rest_start.start()] if rest_start else paragraph
    rest_text = paragraph[rest_start.start() :] if rest_start else ""
    non_perm_match = re.search(r"NÃO PERMISSIONÁRIO/PRONAF\s+TIPO DE VEÍCULO\s+CHEIO\s+3/4\s+1/2\s+1/4", table_text, re.I)
    if not non_perm_match:
        return ""

    permissionario_text = table_text[: non_perm_match.start()]
    permissionario_text = re.sub(r"^.*?PERMISSIONÁRIO\s+TIPO DE VEÍCULO\s+VALOR\s+", "", permissionario_text, flags=re.I)
    non_permissionario_text = table_text[non_perm_match.end() :]

    blocks = [
        '<div class="tariff-annex">',
        '<p class="doc-heading"><strong>ANEXO I TARIFAS CORRIGIDAS PELO ÍNDICE IPCA</strong></p>',
        "<h3>Romaneio diário - Permissionário</h3>",
        render_table(["Tipo de veículo", "Valor"], [[vehicle, value] for vehicle, value in vehicle_value_rows(permissionario_text)]),
        "<h3>Não permissionário/PRONAF</h3>",
        render_table(
            ["Tipo de veículo", "Cheio", "3/4", "1/2", "1/4"],
            [[vehicle] + values + ["-"] * (4 - len(values)) for vehicle, values in vehicle_fraction_rows(non_permissionario_text)],
        ),
    ]
    if rest_text:
        rest_items = re.findall(r"([^0-9.][^R$]{2,80}?)\s+R\$\s*([0-9.]+,[0-9]{2})", rest_text)
        if rest_items:
            blocks.extend(
                [
                    "<h3>Demais tarifas</h3>",
                    render_table(["Item", "Valor"], [[clean_fragment(name), f"R$ {value}"] for name, value in rest_items]),
                ]
            )
    blocks.append("</div>")
    return "".join(blocks)


def render_contract_extract(paragraph: str) -> str:
    escaped = html.escape(paragraph)
    escaped = re.sub(
        r"^(EXTRATO DE TERMO ADITIVO)\s+",
        r'<strong>\1</strong> ',
        escaped,
        flags=re.I,
    )
    escaped = re.sub(
        r"\b(CONTRATADA|OBJETO|VIGÊNCIA):",
        r"<strong>\1:</strong>",
        escaped,
        flags=re.I,
    )
    return f'<p class="contract-extract">{escaped}</p>'


PROCUREMENT_NUMBER_LABELS = [
    "PREGÃO ELETRÔNICO",
    "PREGÃO PRESENCIAL",
    "CONCORRÊNCIA PÚBLICA",
    "CONCORRÊNCIA",
    "DISPENSA DE LICITAÇÃO",
    "INEXIGIBILIDADE DE LICITAÇÃO",
    "CREDENCIAMENTO",
    "CHAMAMENTO PÚBLICO",
    "PROCESSO",
    "CONTRATO",
    "TERMO ADITIVO",
]

PROCUREMENT_COLON_LABELS = [
    "CONTRATADA",
    "CONTRATANTE",
    "OBJETO",
    "VALOR",
    "VIGÊNCIA",
    "DATA DA SESSÃO",
    "DATA DE ABERTURA",
    "ABERTURA",
    "FORNECEDOR REGISTRADO",
    "EMPRESA",
    "SECRETARIA",
]


def procurement_extract_class(paragraph: str) -> str:
    norm = fold_text(paragraph)
    if len(paragraph) < 80:
        return ""
    procurement_cues = [
        "processo",
        "pregao",
        "concorrencia",
        "dispensa",
        "inexigibilidade",
        "contrato",
        "contratada",
        "objeto",
        "vigencia",
        "valor",
        "licitacao",
        "termo aditivo",
    ]
    if sum(1 for cue in procurement_cues if cue in norm) < 3:
        return ""
    if norm.startswith("aviso"):
        return "notice-extract"
    if norm.startswith(("extrato", "termo de rescisao", "termo de revogacao")):
        return "contract-extract"
    return ""


def render_procurement_extract(paragraph: str) -> str:
    kind = procurement_extract_class(paragraph)
    text = clean_fragment(paragraph)
    field_start_labels = [label for label in PROCUREMENT_NUMBER_LABELS + PROCUREMENT_COLON_LABELS if label not in {"CONTRATO", "TERMO ADITIVO"}]
    field_re = re.compile(
        r"\b(" + "|".join(re.escape(label) for label in field_start_labels) + r")\b",
        re.I,
    )
    first_field = field_re.search(text)
    title = ""
    body = text
    if first_field and first_field.start() > 0:
        title = clean_fragment(text[: first_field.start()])
        body = clean_fragment(text[first_field.start() :])
    elif norm := re.match(r"^(AVISO [A-ZÁÀÂÃÉÊÍÓÔÕÚÇ/\s-]+|EXTRATO [A-ZÁÀÂÃÉÊÍÓÔÕÚÇ/\s-]+)\s+(.+)$", text, re.I):
        title = clean_fragment(norm.group(1))
        body = clean_fragment(norm.group(2))

    escaped = html.escape(body)
    for label in PROCUREMENT_NUMBER_LABELS:
        escaped = re.sub(
            rf"\b({re.escape(label)})\s+(?:N[º°o]\.?)",
            lambda match: f"<strong>{match.group(1).upper()} Nº</strong>",
            escaped,
            flags=re.I,
        )
    for label in PROCUREMENT_COLON_LABELS:
        escaped = re.sub(
            rf"\b({re.escape(label)}):",
            lambda match: f"<strong>{match.group(1).upper()}:</strong>",
            escaped,
            flags=re.I,
        )
    escaped = re.sub(r"\b(UASG)\s+(\d+)", r"<strong>UASG</strong> \2", escaped, flags=re.I)
    escaped = re.sub(
        r"\s+(?=<strong>(?:PROCESSO|CONTRATO|CONTRATADA|CONTRATANTE|OBJETO|VALOR|VIGÊNCIA|DATA|ABERTURA|FORNECEDOR|EMPRESA|SECRETARIA|UASG))",
        "<br>",
        escaped,
        flags=re.I,
    )
    escaped = re.sub(
        r"\s+(?=(?:RATIFICO|RECONHECE E AUTORIZA|Publique\.|Caruaru/PE|Caruaru,)\b)",
        "<br>",
        escaped,
        flags=re.I,
    )
    escaped = re.sub(
        r"\s+(?=(?:CONSIDERANDO|ONDE\s+SE\s+L[ÊE]|LEIA-SE|PASSA\s+A\s+VIGORAR|CL[ÁA]USULA|-\s+NO\s+SUBITEM)\b)",
        "<br>",
        escaped,
        flags=re.I,
    )
    escaped = re.sub(r"\s+(?=\d{1,2}(?:\.\d{1,3})+\.?\s+)", "<br>", escaped)
    escaped = re.sub(r"\s+(?=\d{1,2}\.\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ])", "<br>", escaped)
    escaped = re.sub(r"\s+(?=[IVXLCDM]{1,8}\s*[-–]\s+)", "<br>", escaped)
    title_html = render_procurement_title(title)
    segments = [segment.strip() for segment in escaped.split("<br>") if segment.strip()]
    pieces: list[str] = []
    if title_html.strip():
        pieces.append(f'<p class="extract-title">{title_html.strip()}</p>')
    field_items: list[str] = []
    narrative: list[str] = []

    label_re = re.compile(r"^<strong>([^<]+?)\s*[:.]?\s*</strong>\s*(.*)$", re.S)
    number_label_re = re.compile(r"^<strong>([^<]+?)\s+N[º°o]\s*</strong>\s*(.*)$", re.S | re.I)

    def normalize_label(raw: str) -> str:
        s = raw.strip().rstrip(":").rstrip(".")
        return s.title() if s.isupper() else s

    for segment in segments:
        seg = segment.strip()
        if not seg:
            continue
        m = number_label_re.match(seg)
        if m:
            label = normalize_label(m.group(1)) + " Nº"
            value = m.group(2).strip()
            field_items.append(f"<div><dt>{label}</dt><dd>{value or '-'}</dd></div>")
            continue
        m = label_re.match(seg)
        if m:
            label = normalize_label(m.group(1))
            value = m.group(2).strip()
            field_items.append(f"<div><dt>{label}</dt><dd>{value or '-'}</dd></div>")
            continue
        # narrative segment (no leading label)
        narrative.append(seg)

    if field_items:
        pieces.append('<dl class="extract-fields">' + "".join(field_items) + "</dl>")
    for seg in narrative:
        for chunk in split_long_text(seg, 1300):
            pieces.append(f"<p>{chunk}</p>")
    return f'<div class="procurement-extract {kind}">{"".join(pieces)}</div>'


def render_procurement_title(title: str) -> str:
    title = clean_fragment(title)
    if not title:
        return ""
    match = re.match(
        r"^(EXTRATO\s+(?:DE\s+)?(?:TERMO ADITIVO|TERMO DE APOSTILAMENTO|TERMO DE RATIFICAÇÃO|CONTRATO|ATA DE REGISTRO DE PREÇO|TERMO DE AUTORIZAÇÃO)|AVISO\s+(?:DE\s+)?[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ\s/-]+?)(\s+.+)?$",
        title,
        re.I,
    )
    if not match:
        return f"<strong>{html.escape(title)}</strong> "
    head = html.escape(clean_fragment(match.group(1)))
    tail = html.escape(clean_fragment(match.group(2) or ""))
    return f"<strong>{head}</strong>{(' ' + tail) if tail else ''} "


def render_disclosure_box(paragraphs: list[str]) -> str:
    text = " ".join(clean_fragment(item) for item in paragraphs if clean_fragment(item))
    return f'<div class="disclosure-box">{html.escape(text)}</div>'


def is_public_contest_annex_text(text: str) -> bool:
    norm = fold_text(text)
    return (
        "anexo unico" in norm
        and "concurso publico" in norm
        and ("ibam" in norm or "listagem final" in norm or "resultado final" in norm)
        and (
            ("nome do" in norm and "candidato" in norm)
            or "n de inscricao" in norm
            or "inscricao" in norm and "resultado" in norm
            or "ag. de transito e transporte" in norm
            or "guarda municipal" in norm
        )
    )


def is_contest_page_header(line: str) -> bool:
    norm = fold_text(line)
    return (
        "diario oficial do municipio" in norm
        or "lei no 6.155/2018" in norm
        or norm.startswith("edicao ")
    )


def parse_contest_candidate_rows(lines: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    index = 0
    while index < len(lines):
        line = clean_fragment(lines[index])
        if not re.fullmatch(r"\d{1,3}", line):
            index += 1
            continue
        if index + 4 >= len(lines):
            break

        row_start = index
        classification = line
        index += 1
        name_lines: list[str] = []
        while index < len(lines) and not re.fullmatch(r"\d{7,9}", clean_fragment(lines[index])):
            token = clean_fragment(lines[index])
            if token and not is_contest_page_header(token):
                name_lines.append(token)
            index += 1
        if index >= len(lines):
            index = row_start + 1
            continue

        registration = clean_fragment(lines[index])
        index += 1
        if index < len(lines) and re.fullmatch(r"\d-\d", clean_fragment(lines[index])):
            registration = f"{registration} {clean_fragment(lines[index])}"
            index += 1

        children = clean_fragment(lines[index]) if index < len(lines) else ""
        if re.fullmatch(r"\d+", children):
            index += 1
        else:
            children = ""

        birth = clean_fragment(lines[index]) if index < len(lines) else ""
        if index + 1 < len(lines) and re.fullmatch(r"\d{2}/\d{2}/\d{3}", birth) and re.fullmatch(r"\d", clean_fragment(lines[index + 1])):
            birth += clean_fragment(lines[index + 1])
            index += 2
        elif birth:
            index += 1

        objective = ""
        discursive = ""
        aptitude = ""
        if index < len(lines):
            score_match = re.search(r"(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+([A-ZÃÁÀÂÉÊÍÓÔÚÇ]+)", clean_fragment(lines[index]), re.I)
            if score_match:
                objective, discursive, aptitude = score_match.groups()
                index += 1

        evaluation = ""
        if index < len(lines):
            evaluation = clean_fragment(lines[index])
            index += 1
            if index < len(lines) and len(clean_fragment(lines[index])) <= 2 and evaluation.upper().endswith("D"):
                evaluation += clean_fragment(lines[index])
                index += 1

        assessment = clean_fragment(lines[index]) if index < len(lines) else ""
        if assessment:
            index += 1
        t_value = clean_fragment(lines[index]) if index < len(lines) else ""
        if t_value:
            index += 1
        total = clean_fragment(lines[index]) if index < len(lines) else ""
        if total:
            index += 1
        if index < len(lines) and re.fullmatch(r"\d", clean_fragment(lines[index])) and total and re.fullmatch(r"\d+(?:\.\d+)?", total):
            total += clean_fragment(lines[index])
            index += 1

        if not name_lines or not registration:
            index = row_start + 1
            continue

        rows.append(
            {
                "classificacao": classification,
                "nome": " ".join(name_lines),
                "inscricao": registration,
                "filhos": children,
                "nascimento": birth,
                "objetiva": objective,
                "discursiva": discursive,
                "aptidao": aptitude,
                "avaliacao": evaluation,
                "nota": assessment,
                "t": t_value,
                "total": total,
            }
        )
    return rows


def parse_layout_contest_rows(lines: list[str]) -> list[dict[str, str]]:
    cleaned = [clean_fragment(line) for line in lines if clean_fragment(line)]
    rows: list[dict[str, str]] = []
    score_re = re.compile(
        r"^(\d{1,3})(?:\s+(.*?))?\s+(\d+)\s+(\d{2}/\d{2}/\d{4})\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)$"
    )
    for index, line in enumerate(cleaned):
        match = score_re.match(line)
        if not match:
            continue

        classification, middle_name, children, birth, objective, discursive, t_value, total = match.groups()
        previous: list[str] = []
        cursor = index - 1
        while cursor >= 0 and len(previous) < 5:
            item = cleaned[cursor]
            if score_re.match(item) or item.lower().startswith(("class.", "ibam", "medio completo", "no. de vagas")):
                break
            previous.append(item)
            cursor -= 1
        previous.reverse()

        name_parts: list[str] = []
        registration = ""
        reg_index = -1
        reg_match: re.Match[str] | None = None
        for prev_index, item in enumerate(previous):
            found = re.search(r"(\d{6,9}-?)$", item)
            if found:
                reg_index = prev_index
                reg_match = found
        if reg_match:
            item = previous[reg_index]
            before = clean_fragment(item[: reg_match.start()])
            registration = reg_match.group(1)
            if before and not is_contest_page_header(before):
                name_parts.append(before)
            elif reg_index > 0:
                before_line = previous[reg_index - 1]
                before_norm = fold_text(before_line)
                if (
                    before_line
                    and not is_contest_page_header(before_line)
                    and "nome do" not in before_norm
                    and "candidato" not in before_norm
                    and not re.fullmatch(r"\d-\d|\d", before_line)
                ):
                    name_parts.append(before_line)

        if middle_name:
            name_parts.append(middle_name)

        next_index = index + 1
        if next_index < len(cleaned):
            suffix = cleaned[next_index]
            if registration.endswith("-") and re.fullmatch(r"\d-\d|\d", suffix):
                registration += suffix
                next_index += 1
        if next_index < len(cleaned):
            tail = cleaned[next_index]
            if tail and not score_re.match(tail) and not re.search(r"\d{6,9}-?$", tail):
                tail_suffix = re.match(r"(.+?)\s+(\d-\d|\d)$", tail)
                if tail_suffix and registration.endswith("-"):
                    name_parts.append(clean_fragment(tail_suffix.group(1)))
                    registration += tail_suffix.group(2)
                elif not re.fullmatch(r"\d-\d|\d", tail):
                    name_parts.append(tail)

        if not registration or not name_parts:
            continue
        rows.append(
            {
                "classificacao": classification,
                "nome": clean_fragment(" ".join(name_parts)),
                "inscricao": registration,
                "filhos": children,
                "nascimento": birth,
                "objetiva": objective,
                "discursiva": discursive,
                "aptidao": "",
                "avaliacao": "",
                "nota": "",
                "t": t_value,
                "total": total,
            }
        )
    return rows


def parse_registration_result_rows(lines: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    pattern = re.compile(r"(\d{6,9}-\d)\s+(.+?)\s+(APROVADO|REPROVADO|APTA|APTO|INAPTA|INAPTO)$", re.I)
    for line in lines:
        clean = clean_fragment(line)
        match = pattern.search(clean)
        if not match:
            continue
        rows.append(
            {
                "classificacao": str(len(rows) + 1),
                "nome": clean_fragment(match.group(2)),
                "inscricao": clean_fragment(match.group(1)),
                "filhos": "",
                "nascimento": "",
                "objetiva": "",
                "discursiva": "",
                "aptidao": "",
                "avaliacao": clean_fragment(match.group(3)),
                "nota": "",
                "t": "",
                "total": "",
            }
        )
    return rows


def render_contest_rows_table(rows: list[dict[str, str]]) -> str:
    headers = [
        ("classificacao", "Class."),
        ("nome", "Nome do candidato"),
        ("inscricao", "No. Insc."),
        ("filhos", "No. Filhos"),
        ("nascimento", "Data nascimento"),
        ("objetiva", "Objetiva"),
        ("discursiva", "Discursiva"),
        ("aptidao", "Aptidão"),
        ("avaliacao", "Avaliação"),
        ("nota", "Nota"),
        ("t", "T"),
        ("total", "Total"),
    ]
    head = "".join(f"<th>{html.escape(label)}</th>" for _key, label in headers)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{html.escape(row.get(key, ''))}</td>" for key, _label in headers) + "</tr>")
    return (
        '<div class="contest-table structured-table">'
        "<table><thead><tr>"
        + head
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


_CONTEST_INTRO_NOISE_RE_PAGE_FOOTER = re.compile(r"^\d{1,4}\s+\d{1,2}\s+de\s+[a-zçãéíóú]+\s+de\s+\d{4}", re.I)
_CONTEST_INTRO_NOISE_RE_PAGE_FOOTER_PIPE = re.compile(r"\|\s*\d+\s*$")
_CONTEST_INTRO_NOISE_RE_TITLE = re.compile(r"^(decreto|portaria|edital|aviso|errata|lei)\s+(n|conjunta|legislativa|legislativo|de|do|da)\b", re.I)
_CONTEST_INTRO_NOISE_RE_SIGN_NAME = re.compile(r"^[A-ZÁÉÍÓÚÇÂÊÔÃÕ][A-ZÁÉÍÓÚÇÂÊÔÃÕ\s]{4,}$")
_CONTEST_INTRO_NOISE_RE_SIGN_ROLE = re.compile(r"^(diretor|diretora|presidente|prefeito|secret[aá]ri[oa]|gestor|gestora|procurador|procuradora|controlador|superintendente)\b", re.I)


def is_contest_intro_noise(line: str) -> bool:
    """Page footer, signature line, or duplicated act title — not part of the annex content."""
    txt = line.strip()
    if not txt:
        return True
    if _CONTEST_INTRO_NOISE_RE_PAGE_FOOTER.match(txt):
        return True
    if _CONTEST_INTRO_NOISE_RE_PAGE_FOOTER_PIPE.search(txt):
        return True
    if _CONTEST_INTRO_NOISE_RE_TITLE.match(txt):
        return True
    words = txt.split()
    if len(words) <= 6 and _CONTEST_INTRO_NOISE_RE_SIGN_NAME.match(txt):
        return True
    if _CONTEST_INTRO_NOISE_RE_SIGN_ROLE.match(txt):
        return True
    return False


def render_public_contest_annex(text: str) -> str:
    lines = [clean_fragment(line) for line in text.splitlines()]
    if len(lines) <= 2:
        lines = re.split(
            r"\s+(?=(?:IBAM Caruaru|Listagem Final|DATA DA PUBLICA|M.dio completo|401 - GUARDA MUNICIPAL|No\. de vagas|Class\.?|Nome do Candidato|No\. Insc\.|No\. Filho|Data Nasciment|Objetiva|Discursiv|Aptid|Avalia..o|T Total|\d{1,3}\s+[A-Z]))",
            " ".join(lines),
        )
    lines = [line for line in lines if line and not is_contest_page_header(line)]
    intro: list[str] = []
    table_lines: list[str] = []
    in_table = False
    for line in lines:
        norm = fold_text(line)
        if (
            "ibam caruaru" in norm
            or "listagem final" in norm
            or "n de inscricao" in norm
            or "n de inscri" in norm
            or norm.startswith(("concurso publico", "medio completo", "401 - guarda municipal", "301 - ag.", "class", "cl "))
        ):
            in_table = True
        if in_table:
            table_lines.append(line)
        else:
            intro.append(line)
    # Drop leading lines that are page footer / signature of previous act / duplicated act title
    while intro and is_contest_intro_noise(intro[0]):
        intro.pop(0)
    blocks: list[str] = ['<div class="contest-annex">']
    for line in intro[:4]:
        blocks.append(f'<p class="doc-heading"><strong>{html.escape(line)}</strong></p>')
    if table_lines:
        rows = parse_contest_candidate_rows(table_lines)
        if not rows:
            rows = parse_layout_contest_rows(table_lines)
        if not rows:
            rows = parse_registration_result_rows(table_lines)
        if rows:
            summary_lines = []
            for line in table_lines[:8]:
                if not re.fullmatch(r"class\.?|nome do|candidato|no\. inscr?\.?|no\.|filho|s|data|nasciment|o|objetiva|s|discursiv|a|aptid.|avalia..o.*", fold_text(line)):
                    summary_lines.append(line)
            if summary_lines:
                blocks.append('<p class="contest-summary">' + html.escape(" ".join(summary_lines)) + "</p>")
            blocks.append(render_contest_rows_table(rows))
        elif is_garbled_contest_table(table_lines):
            # Extração do PDF embaralhou as colunas — orientar usuário a abrir o PDF
            blocks.append(
                '<div class="missing-annex-note">'
                '<strong>Tabela do concurso disponível somente no PDF</strong>'
                '<span>A extração automática produziu colunas embaralhadas. '
                'Use o botão <em>Abrir PDF</em> acima para consultar a listagem oficial.</span>'
                '</div>'
            )
        else:
            blocks.append(f'<pre class="contest-table">{html.escape("\n".join(table_lines))}</pre>')
    blocks.append("</div>")
    return "".join(blocks)


def is_garbled_contest_table(table_lines: list[str]) -> bool:
    """Tabela de concurso bagunçada: muitas linhas curtas (colunas quebradas pelo pdftotext)."""
    cleaned = [clean_fragment(line) for line in table_lines if clean_fragment(line)]
    if len(cleaned) < 8:
        return False
    short_lines = sum(1 for line in cleaned if len(line) <= 14)
    very_short = sum(1 for line in cleaned if len(line) <= 6)
    fragment_ratio = short_lines / len(cleaned)
    # Mais de 20% das linhas com <=14 chars OU mais de 3 com <=6 chars
    # (dados de candidato bem extraídos têm 30+ chars/linha; nomes quebrados em 2 linhas
    # produzem fragmentos curtos como "SILVA", "FILHO", "OLIVEIRA", etc.)
    return fragment_ratio > 0.20 or very_short >= 3


def is_unextracted_map_annex(paragraph: str) -> bool:
    norm = fold_text(paragraph)
    return norm.startswith("anexo unico mapa contendo") and "vias centrais" in norm and "vias arteriais" in norm


def render_unextracted_map_annex(paragraph: str) -> str:
    return (
        '<div class="missing-annex-note">'
        f'<strong>{html.escape(paragraph)}</strong>'
        '<span>Conteúdo do anexo disponível no PDF como mapa/imagem.</span>'
        "</div>"
    )


def is_short_unextracted_annex(paragraph: str) -> bool:
    norm = fold_text(paragraph)
    return norm.startswith("anexo") and len(clean_fragment(paragraph)) <= 220


def is_titled_short_annex(paragraph: str) -> bool:
    """Anexo sem conteudo textual cujo titulo traz o ato antes de "ANEXO"
    (ex.: "DECRETO No 042, DE ... ANEXO UNICO" de um mapa/imagem)."""
    norm = fold_text(paragraph)
    return (
        len(clean_fragment(paragraph)) <= 220
        and bool(re.match(r"^(decreto|lei|portaria)\b", norm))
        and bool(re.search(r"\banexo\b", norm))
    )


def render_short_unextracted_annex(paragraph: str) -> str:
    return (
        '<div class="missing-annex-note">'
        f'<strong>{html.escape(paragraph)}</strong>'
        '<span>Conteúdo do anexo disponível no PDF como imagem ou diagrama.</span>'
        "</div>"
    )


MONEY_VALUE_RE = re.compile(r"R\$\s*\d{1,3}(?:\.\d{3})*,\d{2}", re.I)


def render_simple_table(headers: list[str], rows: list[list[str]], class_name: str, title: str = "", note: str = "") -> str:
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    pieces = [f'<div class="annex-table {class_name}">']
    if title:
        pieces.append(f'<p class="doc-heading"><strong>{html.escape(title)}</strong></p>')
    pieces.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
    if note:
        pieces.append(f'<p class="table-note">{html.escape(note)}</p>')
    pieces.append("</div>")
    return "".join(pieces)


CPF_MASK_RE = re.compile(r"\d{3}\.XXX\.XXX-(?:\d{2})?", re.I)
CANDIDATE_DATE_TIME_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})\b")


def candidate_table_marker(paragraph: str) -> re.Match[str] | None:
    return re.search(
        r"\bNOME\s+CPF\s+(?:(FUN[ÇC][ÃA]O)\s+CLASS\.?|AP)\s+DATA\s+HOR[ÁA]RIO\b",
        paragraph,
        re.I,
    )


def is_candidate_schedule_table(paragraph: str) -> bool:
    marker = candidate_table_marker(paragraph)
    if not marker:
        return False
    return len(CPF_MASK_RE.findall(paragraph)) >= 1 and len(CANDIDATE_DATE_TIME_RE.findall(paragraph)) >= 1


def split_candidate_position(value: str) -> tuple[str, str]:
    value = clean_fragment(value)
    match = re.match(r"(.+?)\s+(\d+[ªº]?(?:\s*-\s*PCD)?|PCD)$", value, re.I)
    if match:
        return clean_fragment(match.group(1)), clean_fragment(match.group(2))
    return value, ""


def iter_candidate_schedule_rows(rest: str, has_function: bool) -> list[list[str]]:
    rows: list[list[str]] = []
    cursor = 0
    for cpf_match in CPF_MASK_RE.finditer(rest):
        if cpf_match.start() < cursor:
            continue
        name = clean_fragment(rest[cursor : cpf_match.start()].strip(" -;:"))
        if not name:
            continue
        tail = rest[cpf_match.end() :]
        date_match = CANDIDATE_DATE_TIME_RE.search(tail)
        if not date_match:
            continue
        middle = clean_fragment(tail[: date_match.start()].strip(" -;:"))
        row_end = cpf_match.end() + date_match.end()
        cursor = row_end
        if has_function:
            function, position = split_candidate_position(middle)
            rows.append([name, clean_fragment(cpf_match.group(0)), function, position, date_match.group(1), date_match.group(2)])
        else:
            rows.append([name, clean_fragment(cpf_match.group(0)), middle, date_match.group(1), date_match.group(2)])
    return rows


def render_candidate_schedule_table(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    marker = candidate_table_marker(text)
    if not marker:
        return ""
    title = clean_fragment(text[: marker.start()].strip(" -;:"))
    rest = clean_fragment(text[marker.end() :])
    has_function = bool(marker.group(1))
    rows = iter_candidate_schedule_rows(rest, has_function)
    if len(rows) < 1:
        return ""
    if has_function:
        return render_simple_table(["Nome", "CPF", "Função", "Class.", "Data", "Horário"], rows, "candidate-schedule-table", title)
    return render_simple_table(["Nome", "CPF", "AP", "Data", "Horário"], rows, "candidate-schedule-table", title)


def is_schedule_annex_table(paragraph: str) -> bool:
    norm = fold_text(paragraph)
    return norm.startswith("anexo") and "cronograma" in norm and ("data etapa" in norm or "atividade data" in norm)


def render_schedule_annex_table(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    activity_marker = re.search(r"\bATIVIDADE\s+DATA\b", text, re.I)
    if activity_marker:
        title = clean_fragment(text[: activity_marker.start()])
        rest = clean_fragment(text[activity_marker.end() :])
        date_re = re.compile(r"\b\d{2}/\d{2}/\d{4}(?:\s+a\s+\d{2}/\d{2}/\d{4})?\b")
        rows: list[list[str]] = []
        cursor = 0
        for match in date_re.finditer(rest):
            activity = clean_fragment(rest[cursor : match.start()])
            if activity:
                rows.append([activity, clean_fragment(match.group(0))])
            cursor = match.end()
        if len(rows) >= 2:
            return render_simple_table(["Atividade", "Data"], rows, "schedule-annex-table", title)
    marker = re.search(r"\bDATA\s+ETAPA\b", text, re.I)
    if not marker:
        return ""
    title = clean_fragment(text[: marker.start()])
    rest = clean_fragment(text[marker.end() :])
    date_re = re.compile(r"\b\d{2}/\d{2}/\d{4}(?:\s+a\s+\d{2}/\d{2}/\d{4})?\b")
    matches = list(date_re.finditer(rest))
    rows: list[list[str]] = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(rest)
        date_value = clean_fragment(match.group(0))
        step = clean_fragment(rest[match.end() : next_start])
        if step:
            rows.append([date_value, step])
    if len(rows) < 2:
        return ""
    return render_simple_table(["Data", "Etapa"], rows, "schedule-annex-table", title)


def role_name_registration_marker(paragraph: str) -> re.Match[str] | None:
    return re.search(r"\bCargo\s+Nome\s+Matr[íi]cula\b", paragraph, re.I)


def is_role_name_registration_table(paragraph: str) -> bool:
    marker = role_name_registration_marker(paragraph)
    if not marker:
        return False
    rest = paragraph[marker.end() :]
    return len(re.findall(r"\b(?:Aposentado|Pensionista)\b", rest, re.I)) >= 3


def render_role_name_registration_table(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    marker = role_name_registration_marker(text)
    if not marker:
        return ""
    title = clean_fragment(text[: marker.start()].strip(" -;:"))
    rest = clean_fragment(text[marker.end() :])
    role_matches = list(re.finditer(r"\b(Aposentado|Pensionista)\b", rest, re.I))
    rows: list[list[str]] = []
    for index, role_match in enumerate(role_matches):
        next_start = role_matches[index + 1].start() if index + 1 < len(role_matches) else len(rest)
        role = clean_fragment(role_match.group(1))
        segment = clean_fragment(rest[role_match.end() : next_start])
        reg_match = re.match(r"(.+?)\s+([0-9]{1,6}(?:\s+E\s+[0-9]{1,6})?)$", segment, re.I)
        if reg_match:
            rows.append([role, clean_fragment(reg_match.group(1)), clean_fragment(reg_match.group(2))])
    if len(rows) < 3:
        return ""
    return render_simple_table(["Cargo", "Nome", "Matrícula"], rows, "role-name-registration-table", title)


_MUSIC_EDITAL_PROTOCOL_RE = re.compile(r"(?<!\w)([A-Z0-9]{10})(?!\w)")
_MUSIC_EDITAL_STATUS_RE = re.compile(
    r"(N[ÃA]O\s+HABILITAD[OA]?\s*O?|HABILITAD[OA]|INABILITAD[OA]|INDEFERIDO|DEFERIDO)",
    re.I,
)


def music_edital_table_marker(paragraph: str) -> "re.Match[str] | None":
    return re.search(
        r"\b(?:PROTOCOLO|PROTOLOCO)\s+PROPONENTE\b.{0,60}\b(?:ARTISTA|ARTIS)\b",
        paragraph,
        re.I | re.S,
    )


def is_music_edital_table(paragraph: str) -> bool:
    if not music_edital_table_marker(paragraph):
        return False
    codes = [m.group(1) for m in _MUSIC_EDITAL_PROTOCOL_RE.finditer(paragraph) if re.search(r"\d", m.group(1))]
    return len(codes) >= 3


def render_music_edital_table(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    marker = music_edital_table_marker(text)
    if not marker:
        return ""
    title = clean_fragment(text[: marker.start()].strip(" -;:"))
    header_region = text[marker.start() : marker.start() + 200]
    rest = text[marker.end() :]
    has_obs = bool(re.search(r"\bOBSERVA", header_region, re.I))

    splits = [(m.start(), m.group(1)) for m in _MUSIC_EDITAL_PROTOCOL_RE.finditer(rest) if re.search(r"\d", m.group(1))]
    if len(splits) < 3:
        return ""

    rows = []
    for i, (pos, code) in enumerate(splits):
        end = splits[i + 1][0] if i + 1 < len(splits) else len(rest)
        after = clean_fragment(rest[pos + len(code) : end])
        status_match = _MUSIC_EDITAL_STATUS_RE.search(after)
        if status_match:
            names = clean_fragment(after[: status_match.start()])
            status = re.sub(r"\s+", " ", status_match.group(0)).strip()
            status = re.sub(r"(?i)HABILITAD\s+O\b", "HABILITADO", status)
            obs = clean_fragment(after[status_match.end() :])
        else:
            names = after
            status = "-"
            obs = ""
        row = [code, names, status]
        if has_obs:
            row.append(obs)
        rows.append(row)

    if len(rows) < 3:
        return ""
    headers = ["Protocolo", "Proponente / Artista", "Situação"]
    if has_obs:
        headers.append("Observação")
    return render_simple_table(headers, rows, "music-edital-table", title)


def is_role_matricula_name_table(paragraph: str) -> bool:
    if not re.search(r"\bCARGO(?:/\s*N[ÍI]VEL)?\b.{0,40}\bMATR[ÍI]CULA\b.{0,40}\bNOME\b", paragraph, re.I):
        return False
    rows = re.findall(r"\b\d{4,8}\b\s+[A-ZÁÉÍÓÚÇÂÊÔÃÕ]", paragraph)
    return len(rows) >= 3


def render_role_matricula_name_table(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    marker = re.search(r"\bCARGO(?:/\s*N[ÍI]VEL)?\b.{0,40}\bMATR[ÍI]CULA\b.{0,40}\bNOME\b", text, re.I)
    if not marker:
        return ""
    title = clean_fragment(text[: marker.start()].strip(" -;:"))
    rest = clean_fragment(text[marker.end():])
    # Detect cargo keyword set (uppercase words appearing before matricula numbers)
    pattern = re.compile(r"([A-ZÁÉÍÓÚÇÂÊÔÃÕ][A-ZÁÉÍÓÚÇÂÊÔÃÕ \-/]{2,40}?)\s+(\d{4,8})\s+([A-ZÁÉÍÓÚÇÂÊÔÃÕ][^\d]{3,200}?)(?=\s+[A-ZÁÉÍÓÚÇÂÊÔÃÕ][A-ZÁÉÍÓÚÇÂÊÔÃÕ \-/]{2,40}?\s+\d{4,8}\s+|$)")
    rows = []
    for m in pattern.finditer(rest):
        cargo = clean_fragment(m.group(1))
        matricula = clean_fragment(m.group(2))
        nome = clean_fragment(m.group(3))
        rows.append([cargo, matricula, nome])
    if len(rows) < 3:
        return ""
    return render_simple_table(["Cargo", "Matrícula", "Nome"], rows, "role-matricula-name-table", title)


def is_committee_members_table(paragraph: str) -> bool:
    cpfs = re.findall(r"CPF[:.]?\s*\d{3}\.\*{3}\.\*{3}-\d{2}", paragraph, re.I)
    return len(cpfs) >= 3


def render_committee_members_table(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    # Each row pattern: <Cargo:> <Nome (Title Case or UPPERCASE)> - CPF: XXX.***.***-XX
    # Cargo can include roman numerals like "II Vice-Presidente:"
    pattern = re.compile(
        r"((?:[IVXLCDM]+\s*\.?\s*)?[A-ZÁÉÍÓÚÇÂÊÔÃÕ][A-Za-záéíóúçãõâêô\-/ ]{2,80}?):\s*"
        r"([A-ZÁÉÍÓÚÇÂÊÔÃÕ][A-Za-záéíóúçãõâêô '\-]{4,120}?)\s*-\s*"
        r"CPF[:.]?\s*(\d{3}\.\*{3}\.\*{3}-\d{2})",
        re.I,
    )
    rows = []
    for m in pattern.finditer(text):
        cargo = clean_fragment(m.group(1)).rstrip(":")
        nome = clean_fragment(m.group(2))
        cpf = clean_fragment(m.group(3))
        rows.append([cargo, nome, cpf])
    if len(rows) < 3:
        return ""
    # Title is text up to first CPF row
    first_match = pattern.search(text)
    title = clean_fragment(text[: first_match.start()].strip(" -;:")) if first_match else ""
    return render_simple_table(["Cargo", "Nome", "CPF"], rows, "committee-members-table", title)


_CARGO_GUARDA_RE = re.compile(
    r"^(GUARDA\s+MUNICIPAL(?:\s+I{1,3})?|"
    r"SUBINSPETOR(?:\s+I{1,3})?|"
    r"INSPETOR(?:\s+I{1,3})?|"
    r"AGENTE(?:\s+(?:DE|MUNICIPAL|FISCAL))?(?:\s+[A-ZÁÉÍÓÚÇ]{3,30})?|"
    r"FISCAL(?:\s+(?:DE|MUNICIPAL))?(?:\s+[A-ZÁÉÍÓÚÇ]{3,30})?|"
    r"COORDENADOR(?:\s+(?:DE|GERAL|MUNICIPAL))?(?:\s+[A-ZÁÉÍÓÚÇ]{3,30})?|"
    r"DIRETOR(?:\s+(?:DE|GERAL|MUNICIPAL))?(?:\s+[A-ZÁÉÍÓÚÇ]{3,30})?)\b",
    re.I,
)


def is_servidor_list_table(paragraph: str) -> bool:
    if "servidores abaixo listados" not in paragraph.lower():
        return False
    matriculas = re.findall(r"\b\d{3,5}-\d\b", paragraph)
    return len(matriculas) >= 3


def render_servidor_list(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    m_intro = re.search(r"^(.+?servidores\s+abaixo\s+listados[:.]?\s*)", text, re.I | re.S)
    if not m_intro:
        return ""
    intro = m_intro.group(1).strip()
    section = text[m_intro.end():]
    after = ""
    m_end = re.search(r"\s+(Caruaru\s*[,/-].+)$", section, re.I | re.S)
    if m_end:
        after = m_end.group(1).strip()
        section = section[:m_end.start()]

    # Split por matrícula NN-N preservando o valor
    parts = re.split(r"\s*[—–-]?\s*(\d{3,5}-\d)\s*[—–-]?\s*", section)
    if len(parts) < 5:
        return ""

    rows: list[list[str]] = []
    nome_atual = parts[0].strip(" -–—,.")
    for i in range(1, len(parts), 2):
        matric = parts[i]
        if i + 1 >= len(parts):
            rows.append([nome_atual, matric, ""])
            break
        chunk = parts[i + 1].strip(" -–—")
        m_cargo = _CARGO_GUARDA_RE.match(chunk)
        if m_cargo:
            cargo = clean_fragment(m_cargo.group(1))
            proximo = chunk[m_cargo.end():].strip(" -–—,.")
        else:
            # Fallback: split na primeira sequência de 2+ palavras CAIXA-ALTA que parece nome
            split_match = re.search(r"\s+(?=[A-ZÁÉÍÓÚÇ][A-ZÁÉÍÓÚÇa-záéíóúç]{2,}\s+[A-ZÁÉÍÓÚÇ][A-ZÁÉÍÓÚÇa-záéíóúç])", chunk)
            if split_match:
                cargo = chunk[:split_match.start()].strip()
                proximo = chunk[split_match.end():].strip(" -–—,.")
            else:
                cargo = chunk.strip()
                proximo = ""
        rows.append([nome_atual, matric, cargo])
        nome_atual = proximo

    if len(rows) < 3:
        return ""

    intro_html = f'<p>{html.escape(intro[:400])}</p>' if intro else ""
    table_html = render_simple_table(["Nome", "Matrícula", "Cargo"], rows, "servidor-list-table")
    after_html = f'<p>{html.escape(after[:300])}</p>' if after else ""
    return intro_html + table_html + after_html


def is_errata_text(paragraph: str) -> bool:
    if not re.match(r"^\s*ERRATA\b", paragraph, re.I):
        return False
    # Required: must have either Onde consta/Leia-se OR (Edição + Referente)
    if re.search(r"\bOnde\s+(?:consta|se\s+l[êe])[:.]?", paragraph, re.I):
        return True
    if re.search(r"\bLeia[-\s]se[:.]?", paragraph, re.I):
        return True
    has_edicao = bool(re.search(r"\bEdi[çc][ãa]o\s*(?:n[º°]\.?\s*)?\d+", paragraph, re.I))
    has_referente = bool(re.search(r"[Rr]eferente\s+(?:a|ao|à)\b", paragraph))
    return has_edicao and has_referente and len(paragraph) > 250


def render_errata(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    items: list[tuple[str, str]] = []

    m_titulo = re.match(r"^(ERRATA(?:\s+(?:DO|DA|AO|N[º°]\s*\d+)[^.]{0,80})?)\b", text, re.I)
    titulo = clean_fragment(m_titulo.group(1)) if m_titulo else "ERRATA"
    rest = text[m_titulo.end():].lstrip(" ,.-:") if m_titulo else text

    m_edicao = re.search(
        r"(?:Na\s+(?:edi[çc][ãa]o|publica[çc][ãa]o)\b[^,.]*?)(Edi[çc][ãa]o\s*(?:n[º°]\.?\s*)?\d+[^,.]*?(?:,\s*(?:do dia|p[áa]g)[^,.]*?)?)(?=,\s*[A-Z]|\.\s|$)",
        rest, re.I,
    )
    if not m_edicao:
        m_edicao = re.search(r"\b(Edi[çc][ãa]o\s*(?:n[º°]\.?\s*)?\d+[^,.]{0,80})", rest, re.I)

    m_ref = re.search(r"[Rr]eferente\s+(?:a|ao|à)\s+([^.]{5,300})", rest)
    m_onde = re.search(r"\bOnde\s+(?:consta|se\s+l[êe])[:.]?\s*(.+?)(?=\s*Leia[-\s]se[:.]|$)", rest, re.I | re.S)
    m_leia = re.search(r"\bLeia[-\s]se[:.]?\s*(.+?)(?=\s*Os demais|\s*Caruaru\s*[,/-]|\s*[A-Z][a-z]+,\s+\d{1,2}\s+de\s+|$)", rest, re.I | re.S)

    def trim(s: str, limit: int = 600) -> str:
        s = clean_fragment(s).rstrip(",.;: ")
        if len(s) > limit:
            s = s[:limit].rsplit(" ", 1)[0] + "…"
        return s

    if m_edicao:
        items.append(("Edição original", trim(m_edicao.group(1), 200)))
    if m_ref:
        items.append(("Referente a", trim(m_ref.group(1), 300)))
    if m_onde:
        items.append(("Onde constava", trim(m_onde.group(1), 600)))
    if m_leia:
        items.append(("Leia-se", trim(m_leia.group(1), 600)))

    # Fallback: errata sem "Onde consta/Leia-se" — captura o texto da retificação
    if not m_onde and not m_leia:
        # Pega tudo após "Referente ao XXXX." até fim ou assinatura
        if m_ref:
            after_ref = rest[m_ref.end():].strip()
            after_ref = re.split(r"\s*Caruaru\s*[,/-]|\s*[A-Z][a-z]+,\s+\d{1,2}\s+de\s+", after_ref, maxsplit=1)[0].strip()
            if after_ref and len(after_ref) > 30:
                items.append(("Retificação", trim(after_ref, 800)))

    if len(items) < 2:
        return ""

    field_html = "".join(f"<div><dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd></div>" for k, v in items)
    return f'<div class="errata-extract"><p class="doc-heading"><strong>{html.escape(titulo)}</strong></p><dl class="errata-fields">{field_html}</dl></div>'


def is_notification_text(paragraph: str) -> bool:
    if len(paragraph) < 200:
        return False
    return bool(
        re.search(r"\bNOTIFICA(?:R|R\s+a\s+empresa)?\b", paragraph, re.I)
        and re.search(r"\bempresa\b|\bCNPJ\b", paragraph, re.I)
        and re.search(r"\bComiss[ãa]o\b", paragraph, re.I)
    )


def render_notification(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    items: list[tuple[str, str]] = []

    m_designacao = re.search(r"designad[oa]\s+pela\s+([^,.]+?)(?:,|\.|no uso)", text, re.I)
    designacao = clean_fragment(m_designacao.group(1)) if m_designacao else ""

    m_comissao = re.search(r"\b(?:A|O)\s+(Presidente\s+da\s+Comiss[ãa]o[^,.]+?|Comiss[ãa]o[^,.]+?),", text, re.I)
    comissao = clean_fragment(m_comissao.group(1)) if m_comissao else ""

    m_empresa = re.search(r"\bempresa\s+([A-ZÁÉÍÓÚÇÂÊÔÃÕ][^,.]{3,140}?)(?:,|\sinscrita|\.)", text, re.I)
    empresa = clean_fragment(m_empresa.group(1)) if m_empresa else ""

    m_cnpj = re.search(r"CNPJ[/\.\s]*(?:n[º°o]\.?)?\s*([\d./-]{14,20})", text, re.I)
    cnpj = clean_fragment(m_cnpj.group(1)) if m_cnpj else ""

    m_assunto = re.search(r"sobre\s+(.+?)(?:\.\s*$|\.\s+[A-Z])", text, re.I | re.S)
    if not m_assunto:
        m_assunto = re.search(r"NOTIFICA[^,]*,\s*pelo\s+presente,\s+a\s+empresa\s+[^,]+,?\s*(?:inscrita\s+no\s+CNPJ[^,]+,)?\s*(.+?)$", text, re.I | re.S)
    assunto = clean_fragment(m_assunto.group(1)) if m_assunto else ""

    if comissao:
        items.append(("Comissão", comissao))
    if designacao:
        items.append(("Designação", designacao))
    if empresa:
        items.append(("Empresa notificada", empresa))
    if cnpj:
        items.append(("CNPJ", cnpj))
    if assunto:
        # truncate assunto if very long
        if len(assunto) > 600:
            assunto = assunto[:600].rsplit(" ", 1)[0] + "…"
        items.append(("Assunto", assunto))

    if len(items) < 2:
        return ""

    field_html = "".join(f"<div><dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd></div>" for k, v in items)
    return f'<div class="notification-extract"><p class="doc-heading"><strong>NOTIFICAÇÃO</strong></p><dl class="notification-fields">{field_html}</dl></div>'


def code_position_table_marker(paragraph: str) -> re.Match[str] | None:
    return re.search(
        r"\bC[óo]digo\s+Cargo\s+AC\s+PcD\s+CN\s+Requisitos\s+para\s+provimento.*?Vencimento\b",
        paragraph,
        re.I,
    )


def is_code_position_table(paragraph: str) -> bool:
    return bool(code_position_table_marker(paragraph))


def render_code_position_table(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    marker = code_position_table_marker(text)
    if not marker:
        return ""
    title = clean_fragment(text[: marker.start()].strip(" -;:"))
    rest = clean_fragment(text[marker.end() :])
    note = ""
    note_match = re.search(r"\s+TOTAL\s+DE\s+VAGAS:", rest, re.I)
    row_text = rest
    if note_match:
        row_text = clean_fragment(rest[: note_match.start()])
        note = clean_fragment(rest[note_match.start() :])
    pattern = re.compile(
        r"(\d{3})\s+(.+?)\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)\s+(.+?)\s+(\d+\s*h)\s+(R\$\s*[\d.,]+)",
        re.I,
    )
    rows = [
        [
            clean_fragment(match.group(1)),
            clean_fragment(match.group(2)),
            clean_fragment(match.group(3)),
            clean_fragment(match.group(4)),
            clean_fragment(match.group(5)),
            clean_fragment(match.group(6)),
            clean_fragment(match.group(7)),
            clean_fragment(match.group(8)),
        ]
        for match in pattern.finditer(row_text)
    ]
    if not rows:
        return ""
    return render_simple_table(
        ["Código", "Cargo", "AC", "PcD", "CN", "Requisitos", "Jornada", "Vencimento"],
        rows,
        "code-position-table",
        title,
        note,
    )


INLINE_HEADER_TABLE_PATTERNS = [
    r"\bETAPA\s+DATA\s+PREVISTA\b",
    r"\bEVENTO\s+DATA\s+LOCAL\b",
    r"\bETAPA\s+DATA/HOR[ÁA]RIO\s+LOCAL\b",
    r"\bRequisitos/\s*A[çc][õo]es\s+Per[íi]odo\s+Hor[áa]rio\s+Local\b",
    r"\bREQUISITOS\s+PONTU[ÁA]VEIS\s+PONTUA[ÇC][ÃA]O\b",
    r"\bITEM\s+LOCAL\s+TEMA\s+VALOR\s+TOTAL\b",
    r"\bDATA\s+VALOR\s+DA\s+TARIFA\b",
    r"\bSERVI[ÇC]O\s+UTILIZADO\s+COM\s+O\s+RECURSO\b",
    r"\bN[º°O]\s+Atividade\s+Data\(s\)\s+Hor[áa]rio\(s\)\s+Local\b",
]


def is_inline_header_table(paragraph: str) -> bool:
    return any(re.search(pattern, paragraph, re.I) for pattern in INLINE_HEADER_TABLE_PATTERNS)


def render_inline_header_table(paragraph: str) -> str:
    return render_preserved_tabular_annex(paragraph)


def split_value_annex_title(description: str) -> tuple[str, str]:
    description = clean_fragment(description)
    marker = re.match(r"(.+?TIPO DE COBRAN[ÇC]A VALOR)\s+(.+)$", description, re.I)
    if marker:
        return clean_fragment(marker.group(1)), clean_fragment(marker.group(2))
    sao_joao = re.match(r"(.+?S[ÃA]O JO[ÃA]O\s+\d{4})\s+(.+)$", description, re.I)
    if sao_joao:
        return clean_fragment(sao_joao.group(1)), clean_fragment(sao_joao.group(2))
    return "", description


def is_value_annex_table(paragraph: str) -> bool:
    norm = fold_text(paragraph)
    return norm.startswith("anexo") and len(MONEY_VALUE_RE.findall(paragraph)) >= 2 and (
        "preco publico" in norm
        or "precos publicos" in norm
        or "tipo de cobranca valor" in norm
        or "unidade fiscal do municipio" in norm
        or "taxa de expediente" in norm
    )


def render_value_annex_table(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    matches = list(MONEY_VALUE_RE.finditer(text))
    title = ""
    rows: list[list[str]] = []
    cursor = 0
    for match in matches:
        description = clean_fragment(text[cursor : match.start()].strip(" -;:"))
        if not description:
            cursor = match.end()
            continue
        if not rows:
            found_title, description = split_value_annex_title(description)
            title = found_title or title
        amount = clean_fragment(match.group(0))
        cursor = match.end()
        rest = text[cursor:].lstrip()
        if rest.startswith("("):
            close_index = rest.find(")")
            if close_index >= 0:
                amount = f"{amount} {clean_fragment(rest[: close_index + 1])}"
                cursor = len(text) - len(rest[close_index + 1 :])
        rows.append([description, amount])
    rendered = render_simple_table(["Descrição", "Valor"], rows, "value-annex-table", title)
    tail = clean_fragment(text[cursor:])
    if tail:
        rendered += f"<p>{html.escape(tail)}</p>"
    return rendered


def is_financial_annex_table(paragraph: str) -> bool:
    norm = fold_text(paragraph)
    return norm.startswith("anexo") and "demonstrativo de superavit financeiro" in norm


def render_financial_annex_table(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    marker = re.search(r"DEMONSTRATIVO DE SUPER[ÁA]VIT FINANCEIRO", text, re.I)
    if not marker:
        return f"<p>{html.escape(text)}</p>"
    intro = clean_fragment(text[: marker.start()])
    table_text = clean_fragment(text[marker.end() :])
    source = ""
    first_row = re.search(r"\([A-Z]\)", table_text)
    source_matches = list(re.finditer(r"\s+Fonte:\s+", table_text, re.I))
    source_match = source_matches[-1] if source_matches and first_row and source_matches[-1].start() > first_row.start() else None
    if source_match:
        source = f"Fonte: {clean_fragment(table_text[source_match.end():])}"
        table_text = clean_fragment(table_text[: source_match.start()])
    rows: list[list[str]] = []
    pattern = re.compile(
        r"(\([A-Z]\))\s+(.+?)\s+(-?\d[\d.]*,\d{2})(?=\s+\([A-Z]\)|$)",
        re.I,
    )
    for match in pattern.finditer(table_text):
        rows.append([match.group(1), clean_fragment(match.group(2)), match.group(3)])
    if not rows:
        return f"<p>{html.escape(text)}</p>"
    pieces = []
    if intro:
        pieces.append(f"<p>{html.escape(intro)}</p>")
    pieces.append(
        render_simple_table(
            ["Item", "Descrição", "Valor"],
            rows,
            "financial-annex-table",
            "DEMONSTRATIVO DE SUPERÁVIT FINANCEIRO",
            source,
        )
    )
    return "".join(pieces)


def is_process_annex_table(paragraph: str) -> bool:
    norm = fold_text(paragraph)
    return norm.startswith("anexo") and "protocolo" in norm and (
        "processo proprietario obra" in norm or "anuencias revogadas" in norm
    )


def render_process_annex_table(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    marker = re.search(r"Processo\s+Propriet[áa]rio\s+Obra\s+Em vigor at[ée]", text, re.I)
    title = clean_fragment(text[: marker.start()]) if marker else ""
    row_text = text[marker.end() :] if marker else text
    rows: list[list[str]] = []
    pattern = re.compile(
        r"Protocolo\s+(\d+/\d{4})\s+(.+?)\s+(\d{2}/\d{2}/\d{4})(?=\s+Protocolo\s+\d+/\d{4}|$)",
        re.I,
    )
    for match in pattern.finditer(row_text):
        rows.append([match.group(1), clean_fragment(match.group(2)), match.group(3)])
    if not rows:
        return render_preserved_tabular_annex(paragraph)
    return render_simple_table(["Protocolo", "Proprietário/Obra", "Em vigor até"], rows, "process-annex-table", title)


def is_vacancies_annex_table(paragraph: str) -> bool:
    norm = fold_text(paragraph)
    return norm.startswith("anexo") and "quadro de vagas" in norm and (
        "carga horaria" in norm or "c.h" in norm or "remuneracao" in norm or "vencimento" in norm
    )


VACANCY_REQUIREMENT_START_RE = re.compile(
    r"\b("
    r"Diploma|Declara[çc][ãa]o|Certificado|Ensino|N[ií]vel|Curso|"
    r"Gradua[çc][ãa]o|Licenciatura|Experi[eê]ncia|Registro|"
    r"Regularmente|Estar|Possuir|Ser|Laudo|Carteira"
    r")\b",
    re.I,
)


def split_vacancy_role_requirements(value: str) -> tuple[str, str]:
    value = clean_fragment(value)
    match = VACANCY_REQUIREMENT_START_RE.search(value)
    if match and match.start() > 0:
        return clean_fragment(value[: match.start()]), clean_fragment(value[match.start() :])
    return value, ""


def trim_annex_continuation(text: str) -> str:
    return clean_fragment(
        re.sub(
            r"\s+(?:LICITA[ÇC][ÕO]ES\s+E\s+CONTRATOS|PODER\s+(?:EXECUTIVO|LEGISLATIVO)|PREFEITURA\s+MUNICIPAL\s+DE\s+CARUARU)\b.*$",
            "",
            text,
            flags=re.I,
        )
    )


def render_layout_vacancy_annex(text: str) -> str:
    norm = fold_text(text)
    if "quadro de vagas" not in norm or "r$" not in norm or "\n" not in text:
        return ""

    raw_lines = [line.rstrip() for line in text.splitlines()]
    start = 0
    for index, line in enumerate(raw_lines):
        if "anexo" in fold_text(line) and "quadro de vagas" in fold_text(line):
            start = index
            break
    lines = raw_lines[start:]
    if not lines:
        return ""

    note_start = None
    for index, line in enumerate(lines):
        if re.search(r"VAGAS\s+AC\s*=", line, re.I):
            note_start = index
            break
    table_lines = lines[:note_start] if note_start is not None else lines
    note_lines = lines[note_start:] if note_start is not None else []
    note = clean_fragment(re.sub(r"[\uf076•▪]\s*", "", " ".join(note_lines)))
    note = trim_annex_continuation(note)

    money_re = re.compile(
        r"(R\$\s*\d[\d.]*,\d{2}(?:\s*h/a)?(?:\s*a\s*R\$\s*\d[\d.]*,\d{2})?)\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)",
        re.I,
    )
    anchors = [index for index, line in enumerate(table_lines) if money_re.search(line)]
    if not anchors:
        return ""

    header_end = 0
    for index in range(0, anchors[0]):
        line_norm = fold_text(table_lines[index])
        if any(cue in line_norm for cue in ["funcao", "requisitos", "horaria", "remuneracao", "vagas"]):
            header_end = index
    block_start = header_end + 1
    title = clean_fragment(lines[0])
    rows: list[list[str]] = []

    for anchor_number, anchor in enumerate(anchors):
        next_anchor = anchors[anchor_number + 1] if anchor_number + 1 < len(anchors) else len(table_lines)
        block_end = next_anchor - 1
        for index in range(anchor, next_anchor):
            clean = clean_fragment(table_lines[index])
            if clean.endswith("."):
                block_end = index
                break
        block = table_lines[block_start : block_end + 1]
        block_start = block_end + 1
        anchor_line = table_lines[anchor]
        money_match = money_re.search(anchor_line)
        if not money_match:
            continue

        role_parts: list[str] = []
        requirement_parts: list[str] = []
        workload_parts: list[str] = []
        for raw_line in block:
            role = clean_fragment(raw_line[:18])
            requirement = clean_fragment(raw_line[18:60])
            workload = clean_fragment(raw_line[60:72])
            if "(" in role and requirement[:1].islower():
                paren_index = role.find("(")
                requirement = clean_fragment(role[paren_index:] + requirement)
                role = clean_fragment(role[:paren_index])
            if role:
                role_parts.append(role)
            if requirement and "R$" not in requirement:
                requirement_parts.append(requirement)
            workload_match = re.search(r"(?:\d+\s+a\s+)?\d+\s*h(?:/a|/s|\s*/\s*s)?", workload, re.I)
            if workload_match:
                workload_parts.append(clean_fragment(workload_match.group(0)))
            elif re.search(r"\d+\s+a$", workload, re.I):
                workload_parts.append(workload)

        workload_before_money = clean_fragment(anchor_line[: money_match.start()])
        workload_match = re.search(r"((?:\d+\s+a\s+)?\d+\s*h(?:/a|/s|\s*/\s*s)?)\s*$", workload_before_money, re.I)
        if workload_match and workload_match.group(1) not in workload_parts:
            workload_parts.append(clean_fragment(workload_match.group(1)))

        role = clean_fragment(" ".join(role_parts))
        requirements = clean_fragment(" ".join(requirement_parts))
        role = role.replace("Assistente So Social", "Assistente Social")
        requirements = requirements.replace("Serviços cial,", "Serviço Social,")
        workload = clean_fragment(" ".join(workload_parts))
        if not role:
            role, requirements = split_vacancy_role_requirements(requirements)
        rows.append(
            [
                role,
                requirements,
                workload,
                clean_fragment(money_match.group(1)),
                money_match.group(2),
                money_match.group(3),
                money_match.group(4),
                money_match.group(5),
            ]
        )

    total_match = None
    for line in table_lines[anchors[-1] + 1 :]:
        total_match = re.search(r"\bTOTAL\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)", line, re.I)
        if total_match:
            break
    if total_match:
        rows.append(["TOTAL", "", "", "", total_match.group(1), total_match.group(2), total_match.group(3), total_match.group(4)])

    if not rows:
        return ""
    return render_simple_table(
        ["Função", "Requisitos obrigatórios", "Carga horária", "Remuneração", "Total de vagas", "Vagas AC*", "Vagas PcD*", "Vagas PN*"],
        rows,
        "vacancies-annex-table",
        title,
        note,
    )


def render_vacancies_annex_table(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    pay_marker = re.search(
        r"FUN[ÇC][ÃA]O\s+REQUISITOS(?:\s+OBRIGAT[ÓO]RI\s*OS)?\s+(?:CARGA\s+HOR[ÁA]RIA|C\.?\s*H\.?)\s+(REMUNERA[ÇC]\s*[ÃA]O|VENCIMENTO(?:\s+BASE)?)\s+TOTA\s*L\s+DE\s+VAGA\s*S\s+VAGA\s*S\s+AC\*?\s+VAGA\s*S\s+PCD\*?\s+VAGA\s*S\s+(PN|CN)\*?",
        text,
        re.I,
    )
    if pay_marker:
        title = clean_fragment(text[: pay_marker.start()])
        row_text = trim_annex_continuation(clean_fragment(text[pay_marker.end() :]))
        note = ""
        note_match = re.search(r"\s+(?:[\uf076•▪*]\s*)?VAGAS\s+AC\s*=", row_text, re.I)
        if note_match:
            note = clean_fragment(row_text[note_match.start() :])
            note = clean_fragment(re.sub(r"[\uf076•▪]\s*", "", note))
            row_text = clean_fragment(row_text[: note_match.start()])

        total_row: list[str] | None = None
        total_match = re.search(r"\bTOTAL\s+(?:-\s+){0,2}(\d+|-)\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)", row_text, re.I)
        rows_text = row_text
        if total_match:
            total_row = ["TOTAL", "", "", "", total_match.group(1), total_match.group(2), total_match.group(3), total_match.group(4)]
            rows_text = clean_fragment(row_text[: total_match.start()])

        rows_with_pay: list[list[str]] = []
        workload = r"(?:At[ée]\s*\d+\s*h(?:/\s*m[êe]s|/a)?|(?:\d+\s+ou\s+)?\d+\s*h(?:\s*/\s*(?:sem|s|a)|/sem|/a)?|\d+\s*h(?:oras)?(?:\s+por\s+semana|\s+semanais)?)"
        money = r"R\$\s*\d[\d.]*,\d{2}(?:\s*(?:h/a|hora/aula|mensais?))?(?:\s*a\s*R\$\s*\d[\d.]*,\d{2}(?:\s*(?:h/a|hora/aula|mensais?))?)?(?:\s*\+\s*complemento\s*\([^)]+\))?"
        pay_pattern = re.compile(
            rf"\b({workload})\s+({money})\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)(?=\s|$)",
            re.I,
        )
        previous_end = 0
        for match in pay_pattern.finditer(rows_text):
            prefix = clean_fragment(rows_text[previous_end : match.start()])
            previous_end = match.end()
            if not prefix:
                continue
            role, requirements = split_vacancy_role_requirements(prefix)
            rows_with_pay.append(
                [
                    role,
                    requirements,
                    clean_fragment(match.group(1)),
                    clean_fragment(match.group(2)),
                    clean_fragment(match.group(3)),
                    match.group(4),
                    match.group(5),
                    match.group(6),
                ]
            )
        if total_row:
            rows_with_pay.append(total_row)
        if rows_with_pay:
            pay_label = "Vencimento base" if "vencimento" in fold_text(pay_marker.group(1)) else "Remuneração"
            last_quota = pay_marker.group(2).upper()
            return render_simple_table(
                ["Função", "Requisitos obrigatórios", "Carga horária", pay_label, "Total de vagas", "Vagas AC*", "Vagas PcD*", f"Vagas {last_quota}*"],
                rows_with_pay,
                "vacancies-annex-table",
                title,
                note,
            )
    marker = re.search(
        r"FUN[ÇC][ÃA]O\s+REQUISITOS(?:\s+OBRIGAT[ÓO]RIOS)?\s+CARGA\s+HOR[ÁA]RIA\s+TOTAL\s+DE\s+VAGAS\s+VAGAS\s+AC\*?\s+VAGAS\s+PCD\*?\s+VAGAS\s+PN\*?",
        text,
        re.I,
    )
    if marker:
        title = clean_fragment(text[: marker.start()])
        row_text = trim_annex_continuation(clean_fragment(text[marker.end() :]))
        note = ""
        note_match = re.search(r"\s+(?:[\uf076•▪*]\s*)?VAGAS\s+AC\s*=", row_text, re.I)
        if note_match:
            note = clean_fragment(row_text[note_match.start() :])
            note = clean_fragment(re.sub(r"[\uf076•▪]\s*", "", note))
            row_text = clean_fragment(row_text[: note_match.start()])

        total_row: list[str] | None = None
        total_match = re.search(r"\bTOTAL\s+-?\s+-?\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)", row_text, re.I)
        rows_text = row_text
        if total_match:
            total_row = ["TOTAL", "", "", total_match.group(1), total_match.group(2), total_match.group(3), total_match.group(4)]
            rows_text = clean_fragment(row_text[: total_match.start()])

        rows: list[list[str]] = []
        workload_pattern = re.compile(
            r"\b(At[ée]\s*\d+\s*h(?:/\s*m[êe]s|/a)?|\d+\s*h(?:oras)?(?:\s+por\s+semana|\s+semanais)?|\d+\s*h/a)\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)(?=\s|$)",
            re.I,
        )
        previous_end = 0
        for match in workload_pattern.finditer(rows_text):
            prefix = clean_fragment(rows_text[previous_end : match.start()])
            previous_end = match.end()
            if not prefix:
                continue
            role, requirements = split_vacancy_role_requirements(prefix)
            rows.append(
                [
                    role,
                    requirements,
                    clean_fragment(match.group(1)),
                    match.group(2),
                    match.group(3),
                    match.group(4),
                    match.group(5),
                ]
            )
        if total_row:
            rows.append(total_row)
        if rows:
            return render_simple_table(
                ["Função", "Requisitos obrigatórios", "Carga horária", "Total de vagas", "Vagas AC*", "Vagas PcD*", "Vagas PN*"],
                rows,
                "vacancies-annex-table",
                title,
                note,
            )

    marker = re.search(
        r"FUN[ÇC][ÃA]O\s+REQUISITOS\s+CARGA\s+HOR[ÁA]RIA\s+VAGAS\s+AC\*?\s+VAGAS\s+PcD\*?\s+VAGAS\s+PN\*?\s+TOTAL\s+DE\s+VAGAS",
        text,
        re.I,
    )
    if not marker:
        return render_preserved_tabular_annex(paragraph)
    title = clean_fragment(text[: marker.start()])
    row_text = clean_fragment(text[marker.end() :])
    note = ""
    note_match = re.search(r"\s+\*\s*VAGAS\s+AC\s*=", row_text, re.I)
    if note_match:
        note = clean_fragment(row_text[note_match.start() :])
        row_text = clean_fragment(row_text[: note_match.start()])
    rows: list[list[str]] = []
    pattern = re.compile(
        r"(.+?)\s+(\d+h(?:\s+por\s+semana)?|\d+\s*h(?:oras)?(?:\s+semanais)?)\s+(\d+|-)\s+(\d+|-)\s+(\d+|-)\s+(\d+)(?=\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][A-Za-zÁÀÂÃÉÊÍÓÔÕÚÇáàâãéêíóôõúç/ºª.\- ]{2,90}\s+|$)",
        re.I,
    )
    for match in pattern.finditer(row_text):
        rows.append([clean_fragment(match.group(1)), match.group(2), match.group(3), match.group(4), match.group(5), match.group(6)])
    if not rows:
        return render_preserved_tabular_annex(paragraph)
    return render_simple_table(
        ["Função/Requisitos", "Carga horária", "Vagas AC", "Vagas PcD", "Vagas PN", "Total"],
        rows,
        "vacancies-annex-table",
        title,
        note,
    )


def is_preserved_tabular_annex(paragraph: str) -> bool:
    norm = fold_text(paragraph)
    generic_table_cues = [
        "quadro",
        "cronograma",
        "classificacao",
        "pontuacao",
        "inscricao",
        "cargo",
        "vagas",
        "protocolo",
        "valor",
    ]
    return norm.startswith("anexo") and (
        "processo proprietario obra" in norm
        or "anuencias revogadas" in norm
        or "quadro de vagas" in norm
        or "cronograma eleitoral" in norm
        or "periodo horario local" in norm
        or "atividade data" in norm
        or "relacao das entidades" in norm
        or "relacao das organizacoes" in norm
        or "relacao dos" in norm
        or "relacao de" in norm and len(paragraph) > 260
        or "criterios de avaliacao" in norm
        or "criterios de selecao" in norm
        or "pontuacao maxima" in norm
        or "areas de avaliacao" in norm
        or ("protocolo" in norm and norm.count("protocolo") >= 3)
        or sum(1 for cue in generic_table_cues if cue in norm) >= 3
    )


def render_preserved_tabular_annex(paragraph: str) -> str:
    text = clean_fragment(paragraph)
    text = re.sub(r"\s+(Protocolo\s+\d+/\d{4})", r"\n\1", text, flags=re.I)
    text = re.sub(r"\s+(Processo\s+Proprietário\s+Obra\s+Em vigor até)", r"\n\1\n", text, flags=re.I)
    text = re.sub(r"\s+(Função\s+Requisitos\s+Carga)", r"\n\1", text, flags=re.I)
    text = re.sub(r"\s+(?=(?:ETAPA\s+DATA\s+PREVISTA|EVENTO\s+DATA\s+LOCAL|ETAPA\s+DATA/HOR[ÁA]RIO\s+LOCAL|Requisitos/\s*A[çc][õo]es\s+Per[íi]odo\s+Hor[áa]rio\s+Local|REQUISITOS\s+PONTU[ÁA]VEIS|ITEM\s+LOCAL\s+TEMA\s+VALOR|DATA\s+VALOR\s+DA\s+TARIFA|SERVI[ÇC]O\s+UTILIZADO\s+COM\s+O\s+RECURSO|N[º°O]\s+Atividade\s+Data\(s\)\s+Hor[áa]rio\(s\)\s+Local)\b)", "\n", text, flags=re.I)
    text = re.sub(r"\s+(?=(?:CAP[ÍI]TULO|T[ÍI]TULO|Art\.\s*\d+[º°]?|§\s*\d+[º°]?|\d{1,2}\.\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ])\b)", "\n", text, flags=re.I)
    text = re.sub(r"\s+(?=[A-Z]\)\s+\d{2}\s+PROJETOS\b)", "\n", text, flags=re.I)
    text = re.sub(r"\s+(?=(?:Eu,|Declaro|Por ser verdade|Local e Data)\b)", "\n", text, flags=re.I)
    if any(cue in fold_text(text) for cue in ["cronograma", "data prevista", "data/horario"]):
        text = re.sub(
            r"\s+(?=(?:Publica[çc][ãa]o|Per[íi]odo|Data\s+limite|Prazo|Divulga[çc][ãa]o|Recurso|Recursos|Resultado|Inscri[çc][ãa]o|Avalia[çc][ãa]o|Entrevista|Homologa[çc][ãa]o)\b)",
            "\n",
            text,
            flags=re.I,
        )
    if "relacao" in fold_text(text):
        text = re.sub(
            r"\s+(?=(?:Associa[çc][ãa]o|Centro|Instituto|Lar|Casa|Unidade|Rede|Sindicato|Federa[çc][ãa]o|Obra|Pastoral)\b)",
            "\n",
            text,
            flags=re.I,
        )
    if "areas de avaliacao" in fold_text(text) or "criterios de avaliacao" in fold_text(text):
        text = re.sub(r"\s+(?=(?:Empreendedor|Empresa|Produto|Mercado|Gest[ãa]o|Crit[ée]rios?)\s*:)", "\n", text, flags=re.I)
    lines = [clean_fragment(line) for line in text.splitlines() if clean_fragment(line)]
    if not lines:
        return ""
    title = lines[0] if len(lines) > 1 and fold_text(lines[0]).startswith("anexo") else ""
    body_lines = lines[1:] if title else lines
    rows = [[line] for line in body_lines] or [[title]]
    return render_simple_table(["Conteudo tabular extraido"], rows, "preserved-annex-table", title)


def render_known_tabular_annex(paragraph: str) -> str:
    if is_candidate_schedule_table(paragraph):
        return render_candidate_schedule_table(paragraph)
    if is_schedule_annex_table(paragraph):
        return render_schedule_annex_table(paragraph)
    if is_role_name_registration_table(paragraph):
        return render_role_name_registration_table(paragraph)
    if is_code_position_table(paragraph):
        return render_code_position_table(paragraph)
    if is_inline_header_table(paragraph):
        return render_inline_header_table(paragraph)
    if is_process_annex_table(paragraph):
        return render_process_annex_table(paragraph)
    if is_vacancies_annex_table(paragraph):
        return render_vacancies_annex_table(paragraph)
    if is_value_annex_table(paragraph):
        return render_value_annex_table(paragraph)
    if is_financial_annex_table(paragraph):
        return render_financial_annex_table(paragraph)
    if is_preserved_tabular_annex(paragraph):
        return render_preserved_tabular_annex(paragraph)
    return ""


def render_whole_tabular_annex(text: str) -> str:
    compact = clean_fragment(text)
    if not compact:
        return ""
    annex_text = compact
    prefix_html = ""
    annex_match = re.search(r"\bANEXO\b", compact, re.I)
    if not annex_match and not fold_text(compact).startswith("anexo"):
        return ""
    if annex_match and annex_match.start() > 0:
        prefix = clean_fragment(compact[: annex_match.start()])
        if annex_match.start() <= 180 and structured_act_start(prefix):
            prefix_html = f'<p class="doc-heading"><strong>{html.escape(prefix)}</strong></p>'
            annex_text = clean_fragment(compact[annex_match.start() :])
        else:
            return ""
    rendered = render_known_tabular_annex(annex_text)
    return prefix_html + rendered if rendered else ""


SECTION_START_RE = re.compile(
    r"(?<!Art\.)(?<!art\.)\s+(?=(?:\d{1,2}(?:\.\d{1,3})*\.?)\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ])"
)


def split_embedded_sections(item: str) -> list[str]:
    paragraph = clean_fragment(item)
    if not paragraph:
        return []
    if len(paragraph) < 260 and not re.search(r"\b\d+(?:\.\d+)+\.?\s+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ]", paragraph):
        return [paragraph]
    if procurement_extract_class(paragraph):
        return [paragraph]

    pieces = SECTION_START_RE.split(paragraph)
    expanded: list[str] = []
    for piece in pieces:
        piece = clean_fragment(piece)
        if not piece:
            continue
        piece = re.sub(r"\s+(?=ANEXO\s+(?:[IVX]+|ÚNICO|UNICO)\b)", "\n", piece, flags=re.I)
        piece = re.sub(r"\s+(?=NOME\s+CPF\s+(?:FUN[ÇC][ÃA]O\s+CLASS\.?|AP)\s+DATA\s+HOR[ÁA]RIO\b)", "\n", piece, flags=re.I)
        piece = re.sub(r"\s+(?=Cargo\s+Nome\s+Matr[íi]cula\b)", "\n", piece, flags=re.I)
        piece = re.sub(r"\s+(?=C[óo]digo\s+Cargo\s+AC\s+PcD\s+CN\b)", "\n", piece, flags=re.I)
        piece = re.sub(r"\s+(?=(?:ETAPA\s+DATA\s+PREVISTA|EVENTO\s+DATA\s+LOCAL|ETAPA\s+DATA/HOR[ÁA]RIO\s+LOCAL|Requisitos/\s*A[çc][õo]es\s+Per[íi]odo\s+Hor[áa]rio\s+Local|REQUISITOS\s+PONTU[ÁA]VEIS|ITEM\s+LOCAL\s+TEMA\s+VALOR|DATA\s+VALOR\s+DA\s+TARIFA|SERVI[ÇC]O\s+UTILIZADO\s+COM\s+O\s+RECURSO|N[º°O]\s+Atividade\s+Data\(s\)\s+Hor[áa]rio\(s\)\s+Local)\b)", "\n", piece, flags=re.I)
        piece = re.sub(r"\s+(?=(?:CAP[ÍI]TULO|T[ÍI]TULO)\s+[IVXLC]+\b)", "\n", piece, flags=re.I)
        piece = re.sub(r"\s+(?=(?:DISPOSI[ÇC][ÕO]ES\s+GERAIS|DA\s+FINALIDADE|DAS\s+ATRIBUI[ÇC][ÕO]ES)\b)", "\n", piece, flags=re.I)
        piece = re.sub(r"\s+(?=Art\.\s*\d+[º°]?)", "\n", piece, flags=re.I)
        piece = re.sub(r"\s+(?=§\s*\d+[º°]?)", "\n", piece)
        piece = re.sub(r"\s+(?=Par[áa]grafo\s+(?:[ÚU]nico|Primeiro|Segundo)\b)", "\n", piece, flags=re.I)
        piece = re.sub(r"\s+(?=(?:CONSIDERANDO|DECRETA:|RESOLVE:)\b)", "\n", piece, flags=re.I)
        if len(piece) > 1000 and piece.count(" - ") >= 4 and not candidate_table_marker(piece):
            piece = re.sub(r"\s+-\s+", "\n- ", piece)
        expanded.extend(clean_fragment(part) for part in piece.split("\n") if clean_fragment(part))
    return expanded or [paragraph]


def numbered_section_match(paragraph: str) -> re.Match[str] | None:
    return re.match(r"^(\d{1,2}(?:\.\d{1,3})*\.?)\s+(.+)$", paragraph)


def render_numbered_section(paragraph: str) -> str:
    match = numbered_section_match(paragraph)
    if not match:
        return f"<p>{html.escape(paragraph)}</p>"
    number = html.escape(match.group(1))
    rest = clean_fragment(match.group(2))
    if rest.endswith(":") and uppercase_ratio(rest) >= 0.55 and len(rest) <= 160:
        return f'<p class="numbered-section section-heading"><strong>{html.escape(paragraph)}</strong></p>'
    chunks = split_long_text(rest, 1300)
    if not chunks:
        return f'<p class="numbered-section"><strong>{number}</strong></p>'
    blocks = [f'<p class="numbered-section"><strong>{number}</strong> {html.escape(chunks[0])}</p>']
    blocks.extend(f'<p class="numbered-section numbered-continuation">{html.escape(chunk)}</p>' for chunk in chunks[1:])
    return "".join(blocks)


def split_long_text(text: str, limit: int = 1300) -> list[str]:
    text = clean_fragment(text)
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        candidates = [
            remaining.rfind("; ", 0, limit),
            remaining.rfind(". ", 0, limit),
            remaining.rfind(", ", 0, limit),
        ]
        cut = max(candidates)
        if cut < int(limit * 0.45):
            cut = remaining.rfind(" ", 0, limit)
        if cut < int(limit * 0.45):
            cut = limit
        chunks.append(clean_fragment(remaining[: cut + 1]))
        remaining = clean_fragment(remaining[cut + 1 :])
    if remaining:
        chunks.append(remaining)
    return chunks


ROMAN_ITEM_RE = re.compile(r"\s+(?=[IVXLCDM]{1,8}(?:\s*[-–]|\.)\s+)")


def render_article_block(prefix: str, rest: str) -> str:
    rest = clean_fragment(rest)
    escaped_prefix = html.escape(prefix)
    if not rest:
        return f'<p class="article"><strong>{escaped_prefix}</strong></p>'
    roman_count = len(ROMAN_ITEM_RE.findall(rest))
    if roman_count < 2 and not (roman_count == 1 and len(rest) > 1200):
        chunks = split_long_text(rest, 1300)
        if not chunks:
            return f'<p class="article"><strong>{escaped_prefix}</strong></p>'
        blocks = [f'<p class="article"><strong>{escaped_prefix}</strong> {html.escape(chunks[0])}</p>']
        blocks.extend(f'<p class="article article-continuation">{html.escape(chunk)}</p>' for chunk in chunks[1:])
        return "".join(blocks)
    pieces = [clean_fragment(piece) for piece in ROMAN_ITEM_RE.split(rest) if clean_fragment(piece)]
    blocks: list[str] = []
    intro = pieces[0] if pieces and not re.match(r"^[IVXLCDM]{1,8}\s*[-–]\s+", pieces[0]) else ""
    if intro:
        blocks.append(f'<p class="article"><strong>{escaped_prefix}</strong> {html.escape(intro)}</p>')
        pieces = pieces[1:]
    else:
        blocks.append(f'<p class="article"><strong>{escaped_prefix}</strong></p>')
    for piece in pieces:
        match = re.match(r"^([IVXLCDM]{1,8})(?:\s*[-–]|\.)\s*(.+)$", piece)
        if match:
            item = html.escape(match.group(1).upper())
            chunks = split_long_text(match.group(2), 1200)
            if chunks:
                blocks.append(f'<p class="article-subitem"><strong>{item}</strong> {html.escape(chunks[0])}</p>')
                for chunk in chunks[1:]:
                    blocks.append(f'<p class="article-subitem article-continuation">{html.escape(chunk)}</p>')
        else:
            blocks.append(f'<p class="article-subitem">{html.escape(piece)}</p>')
    return "".join(blocks)


def html_paragraphs(text: str) -> str:
    if is_public_contest_annex_text(text):
        return render_public_contest_annex(text)
    if is_music_edital_table(text):
        result = render_music_edital_table(text)
        if result:
            return result
    layout_vacancy_html = render_layout_vacancy_annex(text)
    if layout_vacancy_html:
        return layout_vacancy_html
    whole_annex_html = render_whole_tabular_annex(text)
    if whole_annex_html:
        return whole_annex_html
    compact_text = clean_fragment(text)
    if procurement_extract_class(compact_text):
        return render_procurement_extract(compact_text)
    blocks: list[str] = []
    items: list[str] = []
    for item in text.split("\n\n"):
        items.extend(split_embedded_sections(item))
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        paragraph = clean_fragment(item)
        if not paragraph:
            continue
        tariff_html = render_tariff_annex(paragraph) if is_tariff_annex(paragraph) else ""
        if tariff_html:
            blocks.append(tariff_html)
            continue
        annex_table_html = render_annex_data_table(paragraph)
        if annex_table_html:
            blocks.append(annex_table_html)
            continue
        if is_unextracted_map_annex(paragraph):
            blocks.append(render_unextracted_map_annex(paragraph))
            continue
        if is_short_unextracted_annex(paragraph) or (
            paragraph == compact_text and is_titled_short_annex(paragraph)
        ):
            blocks.append(render_short_unextracted_annex(paragraph))
            continue
        if is_process_annex_table(paragraph):
            blocks.append(render_process_annex_table(paragraph))
            continue
        if is_vacancies_annex_table(paragraph):
            blocks.append(render_vacancies_annex_table(paragraph))
            continue
        if is_value_annex_table(paragraph):
            blocks.append(render_value_annex_table(paragraph))
            continue
        if is_financial_annex_table(paragraph):
            blocks.append(render_financial_annex_table(paragraph))
            continue
        if is_candidate_schedule_table(paragraph):
            blocks.append(render_candidate_schedule_table(paragraph))
            continue
        if is_schedule_annex_table(paragraph):
            blocks.append(render_schedule_annex_table(paragraph))
            continue
        if is_role_name_registration_table(paragraph):
            blocks.append(render_role_name_registration_table(paragraph))
            continue
        if is_music_edital_table(paragraph):
            blocks.append(render_music_edital_table(paragraph))
            continue
        if is_committee_members_table(paragraph):
            html_block = render_committee_members_table(paragraph)
            if html_block:
                blocks.append(html_block)
                continue
        if is_role_matricula_name_table(paragraph):
            html_block = render_role_matricula_name_table(paragraph)
            if html_block:
                blocks.append(html_block)
                continue
        if is_notification_text(paragraph):
            html_block = render_notification(paragraph)
            if html_block:
                blocks.append(html_block)
                continue
        if is_errata_text(paragraph):
            html_block = render_errata(paragraph)
            if html_block:
                blocks.append(html_block)
                continue
        if is_servidor_list_table(paragraph):
            html_block = render_servidor_list(paragraph)
            if html_block:
                blocks.append(html_block)
                continue
        if is_code_position_table(paragraph):
            blocks.append(render_code_position_table(paragraph))
            continue
        if is_inline_header_table(paragraph):
            blocks.append(render_inline_header_table(paragraph))
            continue
        if is_preserved_tabular_annex(paragraph):
            blocks.append(render_preserved_tabular_annex(paragraph))
            continue
        if paragraph.startswith(";") and blocks and 'class="place-date"' in blocks[-1]:
            continuation = html.escape(paragraph)
            blocks[-1] = blocks[-1].replace("</p>", f"{continuation}</p>")
            continue
        norm = fold_text(paragraph)
        if blocks and 'class="place-date"' in blocks[-1] and len(paragraph) <= 80 and "republica" in norm and not looks_like_person_name(paragraph):
            continuation = html.escape(paragraph)
            blocks[-1] = blocks[-1].replace("</p>", f" {continuation}</p>")
            continue
        if norm.startswith("divulgacao:"):
            disclosure = [paragraph]
            while index < len(items):
                next_paragraph = clean_fragment(items[index])
                if not next_paragraph:
                    index += 1
                    continue
                next_norm = fold_text(next_paragraph)
                if structured_act_start(next_paragraph):
                    break
                disclosure.append(next_paragraph)
                index += 1
                if next_norm.startswith("online:") or "www.caruaru.pe.gov.br" in next_norm:
                    break
            blocks.append(render_disclosure_box(disclosure))
            continue
        if fold_text(paragraph).startswith("ementa:"):
            split_match = re.search(
                r"\s+(O\(A\)|A\(O\)|O PREFEITO|A SECRET[ÁA]RI[AO]|O SECRET[ÁA]RIO|A PRESIDENTE|O PRESIDENTE)\b",
                paragraph,
                re.I,
            )
            if split_match:
                ementa_text = paragraph[: split_match.start()].strip()
                rest_text = paragraph[split_match.start() :].strip()
                rest = html.escape(ementa_text.split(":", 1)[1].strip())
                blocks.append(f'<p class="ementa"><strong>Ementa:</strong> {rest}</p>')
                blocks.append(f"<p>{html.escape(rest_text)}</p>")
                continue
        norm = fold_text(paragraph)
        inline_signature = inline_authority_suffix(paragraph)
        if inline_signature:
            before, name, role = inline_signature
            if before:
                blocks.append(html_paragraphs(before))
            blocks.append(f'<p class="signature-name"><strong>{html.escape(name)}</strong></p>')
            blocks.append(f'<p class="signature-role">{html.escape(role)}</p>')
            continue
        escaped = html.escape(paragraph)
        article = re.match(r"^(Art\.\s*\d+[º°]?\s*\.?)\s*(.*)", paragraph, re.I)
        authority_pair = authority_pair_from_text(paragraph)
        procurement_kind = procurement_extract_class(paragraph)
        if procurement_kind:
            blocks.append(render_procurement_extract(paragraph))
        elif numbered_section_match(paragraph):
            blocks.append(render_numbered_section(paragraph))
        elif norm.startswith("extrato de termo aditivo") and len(paragraph) > 120:
            blocks.append(render_contract_extract(paragraph))
        elif structured_act_start(paragraph):
            blocks.append(f'<p class="doc-heading"><strong>{escaped}</strong></p>')
        elif norm.startswith("ementa:"):
            rest = html.escape(paragraph.split(":", 1)[1].strip())
            blocks.append(f'<p class="ementa"><strong>Ementa:</strong> {rest}</p>')
        elif is_command_line(paragraph):
            command_chunks = split_long_text(paragraph, 1300)
            if command_chunks:
                blocks.append(f'<p class="command"><strong>{html.escape(command_chunks[0])}</strong></p>')
                for chunk in command_chunks[1:]:
                    blocks.append(f'<p class="command command-continuation">{html.escape(chunk)}</p>')
        elif is_document_section_heading(paragraph):
            blocks.append(f'<p class="doc-heading"><strong>{escaped}</strong></p>')
        elif article:
            blocks.append(render_article_block(article.group(1), article.group(2)))
        elif is_location_line(paragraph):
            blocks.append(f'<p class="place-date">{escaped}</p>')
        elif "assinado de forma digital" in norm or norm.startswith("dados:") or re.search(r":[0-9]{6,}", paragraph):
            for chunk in split_long_text(paragraph, 1300):
                blocks.append(f'<p class="digital-signature">{html.escape(chunk)}</p>')
        elif authority_pair and len(paragraph) <= 220:
            name, role = authority_pair
            blocks.append(f'<p class="signature-name"><strong>{html.escape(name)}</strong></p>')
            blocks.append(f'<p class="signature-role">{html.escape(role)}</p>')
        elif (uppercase_ratio(paragraph) >= 0.78 or looks_like_person_name(paragraph)) and len(paragraph) <= 90:
            blocks.append(f'<p class="signature-name"><strong>{escaped}</strong></p>')
        elif re.match(r"^(Prefeito|Procurador|Secretári|Controlador|Presidente|Diretor|Vereador|Superintendente)", paragraph, re.I):
            blocks.append(f'<p class="signature-role">{escaped}</p>')
        else:
            for chunk in split_long_text(paragraph, 1300):
                blocks.append(f"<p>{html.escape(chunk)}</p>")
    return "".join(blocks) or "<p>-</p>"


def write_general_html_index(rows: list[dict], acts: list[dict], parts: list[dict]) -> None:
    months = sorted({act["ano_mes"] for act in acts})
    types = sorted({act["tipo"] for act in acts if act["tipo"]})
    categories = sorted({act["categoria"] for act in acts if act["categoria"]})
    agencies = sorted({act["orgao_contexto"] for act in acts if act["orgao_contexto"]})
    part_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for part in parts:
        part_counts[part["ato_id"]][part["tipo_parte"]] += 1

    cards = []
    templates = []
    for act_index, act in enumerate(acts):
        pdf = html.escape(relative_to_output(act["arquivo_pdf"]))
        subject = act["ementa"] or act["titulo"] or act["identificacao"]
        subject_html = f"<p class=\"subject\">{html.escape(subject)}</p>" if subject and subject != act["identificacao"] else ""
        template_id = f"act-body-{act_index}"
        templates.append(f'<template id="{template_id}">{html_paragraphs(act["texto"])}</template>')
        modal_meta = " - ".join(part for part in [act["data"], act["edicao"], act["tipo"]] if part)
        parts_summary = ", ".join(f"{kind}: {count}" for kind, count in part_counts[act["ato_id"]].most_common())
        searchable = fold_text(
            " ".join(
                [
                    act["data"],
                    act["ano_mes"],
                    act["edicao"],
                    act["poder"],
                    act["orgao_contexto"],
                    act["tipo"],
                    act["categoria"],
                    act["identificacao"],
                    act["titulo"],
                    act["numero"],
                    act["ementa"],
                    act["autoridades"],
                    act["orgaos_mencionados"],
                ]
            )
        )
        parent_html = f"<div><dt>Ato pai</dt><dd>{html.escape(act['ato_pai'])}</dd></div>" if act["ato_pai"] else ""
        cards.append(
            f"""
            <article class="act-card" data-month="{html.escape(act['ano_mes'])}" data-type="{html.escape(act['tipo'])}" data-category="{html.escape(act['categoria'])}" data-agency="{html.escape(act['orgao_contexto'])}" data-search="{html.escape(searchable)}" data-index="{act_index}" data-template="{template_id}" data-title="{html.escape(act['identificacao'])}" data-subject="{html.escape(subject or '')}" data-meta="{html.escape(modal_meta)}">
              <div class="card-head">
                <div>
                  <span class="date">{html.escape(act['data'])} - {html.escape(act['edicao'])}</span>
                  <h2>{html.escape(act['identificacao'])}</h2>
                  {subject_html}
                </div>
                <span class="kind">{html.escape(act['tipo'])}</span>
              </div>
              <dl>
                <div><dt>Categoria</dt><dd>{html.escape(act['categoria'] or '-')}</dd></div>
                <div><dt>Secretaria/órgão</dt><dd>{html.escape(act['orgao_contexto'] or '-')}</dd></div>
                <div><dt>Assunto/ementa</dt><dd>{html.escape(subject or '-')}</dd></div>
                <div><dt>Poder</dt><dd>{html.escape(act['poder'] or '-')}</dd></div>
                <div><dt>Responsáveis/assinaturas</dt><dd>{html.escape(act['autoridades'] or '-')}</dd></div>
                <div><dt>Órgãos mencionados</dt><dd>{html.escape(act['orgaos_mencionados'] or '-')}</dd></div>
                <div><dt>Partes encontradas</dt><dd>{html.escape(parts_summary or '-')}</dd></div>
                {parent_html}
              </dl>
              <div class="card-actions">
                <button type="button" class="open-act" data-index="{act_index}" data-template="{template_id}">Ler texto completo</button>
              </div>
              <div class="links"><a href="{pdf}" target="_blank" rel="noopener">PDF</a></div>
            </article>"""
        )

    diary_rows = []
    for row in rows:
        pdf = html.escape(relative_to_output(row["arquivo_pdf"]))
        diary_rows.append(
            f"<tr><td>{html.escape(row['data'])}</td><td>{html.escape(row['edicao'])}</td><td><a href=\"{pdf}\" target=\"_blank\" rel=\"noopener\">PDF</a></td></tr>"
        )

    month_options = "\n".join(f'<option value="{month}">{month_label(month)}</option>' for month in months)
    type_options = "\n".join(f'<option value="{html.escape(kind)}">{html.escape(kind)}</option>' for kind in types)
    category_options = "\n".join(f'<option value="{html.escape(category)}">{html.escape(category)}</option>' for category in categories)
    agency_options = "\n".join(f'<option value="{html.escape(agency)}">{html.escape(agency)}</option>' for agency in agencies)
    html_page = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Consulta geral - Diarios de Caruaru</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; color: #1f2933; background: #f5f7f8; }}
    body.modal-open {{ overflow: hidden; }}
    header {{ background: #103f36; color: white; padding: 22px 28px; }}
    main {{ padding: 20px 28px 36px; max-width: 1420px; margin: 0 auto; }}
    h1 {{ margin: 0 0 6px; font-size: 26px; }}
    .quick a {{ color: white; margin-right: 14px; }}
    .toolbar {{ display: grid; grid-template-columns: minmax(280px, 1fr) repeat(4, minmax(150px, 220px)); gap: 10px; margin: 18px 0; }}
    input, select {{ height: 40px; border: 1px solid #b8c2cc; border-radius: 6px; padding: 0 10px; font-size: 14px; background: white; }}
    .summary {{ margin: 10px 0 18px; font-weight: 700; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(440px, 1fr)); gap: 14px; }}
    .act-card {{ background: white; border: 1px solid #d8e0e5; border-radius: 8px; padding: 14px; box-shadow: 0 1px 2px rgba(16, 24, 40, .06); }}
    .card-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
    .date {{ display: inline-block; color: #586875; font-weight: 700; margin-bottom: 4px; }}
    h2 {{ font-size: 17px; line-height: 1.3; margin: 0; }}
    .subject {{ margin: 8px 0 0; line-height: 1.45; color: #334155; }}
    .kind {{ background: #e7f0ed; color: #103f36; border-radius: 999px; padding: 6px 10px; font-size: 12px; font-weight: 700; white-space: nowrap; }}
    dl {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; margin: 12px 0; }}
    dt {{ color: #586875; font-size: 12px; font-weight: 700; }}
    dd {{ margin: 2px 0 0; line-height: 1.35; }}
    details {{ border-top: 1px solid #e3e8ec; padding-top: 10px; margin-top: 10px; }}
    summary {{ cursor: pointer; font-weight: 700; color: #103f36; }}
    .card-actions {{ border-top: 1px solid #e3e8ec; padding-top: 12px; margin-top: 12px; }}
    .open-act {{ border: 1px solid #9fb4aa; background: white; color: #103f36; border-radius: 6px; padding: 8px 12px; font-weight: 700; cursor: pointer; }}
    .open-act:hover {{ background: #eef7f3; }}
    .body-text p {{ line-height: 1.55; margin: 10px 0 0; }}
    .body-text .doc-heading {{ font-weight: 700; color: #0f172a; }}
    .body-text .ementa {{ display: inline-block; background: #eaf4ff; border-left: 4px solid #4f8fcf; padding: 6px 8px; }}
    .body-text .command {{ margin-top: 16px; color: #0f172a; }}
    .body-text .article strong {{ color: #0f172a; }}
    .body-text .place-date {{ text-align: center; margin-top: 16px; }}
    .body-text .signature-name {{ text-align: center; text-decoration: underline; margin-top: 12px; }}
    .body-text .signature-role {{ text-align: center; margin-top: 2px; }}
    .body-text .digital-signature {{ text-align: center; font-size: 12px; color: #334155; margin-top: 2px; }}
    .body-text .tariff-annex {{ margin-top: 12px; }}
    .body-text .tariff-annex h3 {{ margin: 18px 0 8px; font-size: 14px; color: #111827; }}
    .body-text .tariff-annex table {{ width: 100%; border-collapse: collapse; margin: 8px 0 14px; font-size: 13px; }}
    .body-text .tariff-annex th, .body-text .tariff-annex td {{ border: 1px solid #d8e0e5; padding: 7px 8px; text-align: left; vertical-align: top; }}
    .body-text .tariff-annex th {{ background: #f1f5f9; font-weight: 800; }}
    .body-text .annex-table {{ margin-top: 12px; }}
    .body-text .annex-table table {{ width: 100%; border-collapse: collapse; margin: 8px 0 14px; font-size: 13px; }}
    .body-text .annex-table th, .body-text .annex-table td {{ border: 1px solid #d8e0e5; padding: 7px 8px; text-align: left; vertical-align: top; }}
    .body-text .annex-table th {{ background: #f1f5f9; font-weight: 800; }}
    .modal-search {{ display: grid; grid-template-columns: minmax(180px, 1fr) auto; gap: 8px; align-items: center; margin: 0 0 14px; }}
    .modal-search input {{ width: 100%; }}
    .modal-search-count {{ color: #586875; font-size: 12px; font-weight: 700; white-space: nowrap; }}
    mark.modal-highlight {{ background: #fde68a; color: inherit; padding: 0 1px; border-radius: 2px; }}
    .links {{ margin-top: 12px; }}
    .links a, table a {{ color: #0f5d45; font-weight: 700; margin-right: 10px; }}
    .modal-backdrop {{ position: fixed; inset: 0; z-index: 1000; display: flex; align-items: center; justify-content: center; padding: 24px; background: rgba(15, 23, 42, .58); }}
    .modal-backdrop[hidden] {{ display: none; }}
    .modal-shell {{ position: relative; width: min(1040px, 100%); max-height: 92vh; display: flex; align-items: center; }}
    .modal-panel {{ width: 100%; max-height: 92vh; overflow: auto; background: white; border-radius: 8px; padding: 24px 72px; box-shadow: 0 20px 60px rgba(15, 23, 42, .35); }}
    .modal-panel h2 {{ font-size: 20px; line-height: 1.3; margin: 4px 0 8px; }}
    .modal-meta {{ color: #586875; font-size: 13px; font-weight: 700; }}
    .modal-subject {{ margin: 0 0 16px; color: #334155; line-height: 1.45; }}
    .modal-body {{ font-size: 15px; color: #111827; }}
    .modal-close {{ position: absolute; top: 12px; right: 12px; z-index: 2; width: 36px; height: 36px; border: 1px solid #cbd5df; border-radius: 50%; background: white; color: #103f36; font-size: 22px; line-height: 1; cursor: pointer; }}
    .modal-nav {{ position: absolute; top: 50%; z-index: 2; width: 44px; height: 70px; transform: translateY(-50%); border: 1px solid #cbd5df; border-radius: 999px; background: white; color: #103f36; font-size: 28px; font-weight: 700; cursor: pointer; box-shadow: 0 8px 24px rgba(15, 23, 42, .18); }}
    .modal-prev {{ left: 12px; }}
    .modal-next {{ right: 12px; }}
    .modal-nav:hover, .modal-close:hover {{ background: #eef7f3; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 12px; background: white; }}
    td, th {{ border-bottom: 1px solid #e3e8ec; padding: 8px; text-align: left; }}
    @media (max-width: 960px) {{
      main {{ padding: 14px; }}
      .toolbar {{ grid-template-columns: 1fr; }}
      .cards {{ grid-template-columns: 1fr; }}
      .card-head {{ display: block; }}
      .kind {{ display: inline-block; margin-top: 10px; }}
      dl {{ grid-template-columns: 1fr; }}
      .modal-backdrop {{ padding: 0; }}
      .modal-shell {{ width: 100%; height: 100vh; max-height: 100vh; }}
      .modal-panel {{ height: 100vh; max-height: 100vh; border-radius: 0; padding: 56px 46px 24px; }}
      .modal-prev {{ left: 4px; }}
      .modal-next {{ right: 4px; }}
      .modal-nav {{ width: 34px; height: 54px; font-size: 22px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Consulta geral dos Diarios Oficiais de Caruaru</h1>
    <div>Periodo: {START_DATE:%d/%m/%Y} a {END_DATE:%d/%m/%Y} - {len(acts)} atos estruturados em {len(rows)} diarios</div>
    <p class="quick"><a href="consulta.html">Nomeacoes e exoneracoes</a><a href="atos_estruturados.csv">Atos CSV</a><a href="partes_dos_atos.csv">Partes CSV</a><a href="atos_estruturados_por_categoria.md">Resumo por categoria</a></p>
  </header>
  <main>
    <div class="toolbar">
      <input id="search" placeholder="Pesquisar decreto, portaria, secretaria, pessoa, assunto, artigo...">
      <select id="month"><option value="">Todos os meses</option>{month_options}</select>
      <select id="type"><option value="">Todos os tipos</option>{type_options}</select>
      <select id="category"><option value="">Todas as categorias</option>{category_options}</select>
      <select id="agency"><option value="">Todas as secretarias/órgãos</option>{agency_options}</select>
    </div>
    <div class="summary" id="count"></div>
    <section class="cards">
      {''.join(cards)}
    </section>
    <details>
      <summary>Diarios usados na consulta</summary>
      <table>
        <thead><tr><th>Data</th><th>Edicao</th><th>Arquivos</th></tr></thead>
        <tbody>{''.join(diary_rows)}</tbody>
      </table>
    </details>
  </main>
  <div class="act-templates" hidden>
    {''.join(templates)}
  </div>
  <div class="modal-backdrop" id="actModal" hidden>
    <div class="modal-shell" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
      <button type="button" class="modal-close" aria-label="Fechar">&times;</button>
      <button type="button" class="modal-nav modal-prev" aria-label="Ato anterior">&lt;</button>
      <article class="modal-panel">
        <div class="modal-meta" id="modalMeta"></div>
        <h2 id="modalTitle"></h2>
        <p class="modal-subject" id="modalSubject"></p>
        <div class="modal-search">
          <input id="modalSearch" type="search" autocomplete="off" placeholder="Pesquisar neste ato...">
          <span id="modalSearchCount" class="modal-search-count"></span>
        </div>
        <div class="body-text modal-body" id="modalBody"></div>
      </article>
      <button type="button" class="modal-nav modal-next" aria-label="Proximo ato">&gt;</button>
    </div>
  </div>
  <script>
    const search = document.querySelector('#search');
    const month = document.querySelector('#month');
    const type = document.querySelector('#type');
    const category = document.querySelector('#category');
    const agency = document.querySelector('#agency');
    const cards = [...document.querySelectorAll('.act-card')];
    const count = document.querySelector('#count');
    const modal = document.querySelector('#actModal');
    const modalTitle = document.querySelector('#modalTitle');
    const modalMeta = document.querySelector('#modalMeta');
    const modalSubject = document.querySelector('#modalSubject');
    const modalBody = document.querySelector('#modalBody');
    const modalSearch = document.querySelector('#modalSearch');
    const modalSearchCount = document.querySelector('#modalSearchCount');
    const modalClose = document.querySelector('.modal-close');
    const modalPrev = document.querySelector('.modal-prev');
    const modalNext = document.querySelector('.modal-next');
    let currentModalIndex = -1;
    function normalizeText(value) {{
      return value.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
    }}
    function renderModalBodyFromTemplate() {{
      const card = cards.find(item => Number(item.dataset.index) === currentModalIndex);
      const template = card ? document.getElementById(card.dataset.template) : null;
      modalBody.replaceChildren();
      if (template) {{
        modalBody.appendChild(template.content.cloneNode(true));
      }} else {{
        modalBody.textContent = 'Texto nao disponivel.';
      }}
    }}
    function clearModalSearch() {{
      modalSearch.value = '';
      modalSearchCount.textContent = '';
    }}
    function highlightModalSearch() {{
      renderModalBodyFromTemplate();
      const term = modalSearch.value.trim();
      if (!term) {{
        modalSearchCount.textContent = '';
        return;
      }}
      const needle = normalizeText(term);
      const walker = document.createTreeWalker(modalBody, NodeFilter.SHOW_TEXT);
      const textNodes = [];
      while (walker.nextNode()) {{
        textNodes.push(walker.currentNode);
      }}
      let count = 0;
      let firstMark = null;
      for (const node of textNodes) {{
        const source = node.nodeValue || '';
        const normalized = normalizeText(source);
        let matchIndex = normalized.indexOf(needle);
        if (matchIndex < 0) {{
          continue;
        }}
        const fragment = document.createDocumentFragment();
        let cursor = 0;
        while (matchIndex >= 0) {{
          fragment.append(document.createTextNode(source.slice(cursor, matchIndex)));
          const mark = document.createElement('mark');
          mark.className = 'modal-highlight';
          mark.textContent = source.slice(matchIndex, matchIndex + term.length);
          fragment.append(mark);
          firstMark = firstMark || mark;
          count += 1;
          cursor = matchIndex + term.length;
          matchIndex = normalized.indexOf(needle, cursor);
        }}
        fragment.append(document.createTextNode(source.slice(cursor)));
        node.replaceWith(fragment);
      }}
      modalSearchCount.textContent = count ? count + ' ocorrência(s)' : 'Sem ocorrência';
      if (firstMark) {{
        firstMark.scrollIntoView({{ block: 'center' }});
      }}
    }}
    cards.forEach(card => {{
      const template = document.getElementById(card.dataset.template);
      const templateText = template ? template.content.textContent : '';
      card._search = normalizeText(card.textContent + ' ' + templateText + ' ' + (card.dataset.search || ''));
    }});
    function visibleCards() {{
      return cards.filter(card => card.style.display !== 'none');
    }}
    function openModalByCard(card) {{
      if (!card) {{
        return;
      }}
      currentModalIndex = Number(card.dataset.index);
      modalTitle.textContent = card.dataset.title || '';
      modalMeta.textContent = card.dataset.meta || '';
      modalSubject.textContent = card.dataset.subject || '';
      modalSubject.hidden = !card.dataset.subject || card.dataset.subject === card.dataset.title;
      clearModalSearch();
      renderModalBodyFromTemplate();
      modal.hidden = false;
      document.body.classList.add('modal-open');
    }}
    function closeModal() {{
      modal.hidden = true;
      document.body.classList.remove('modal-open');
      currentModalIndex = -1;
    }}
    function moveModal(delta) {{
      const visible = visibleCards();
      if (!visible.length) {{
        return;
      }}
      const currentCard = cards.find(card => Number(card.dataset.index) === currentModalIndex);
      let position = visible.indexOf(currentCard);
      if (position < 0) {{
        position = delta > 0 ? -1 : 0;
      }}
      const nextCard = visible[(position + delta + visible.length) % visible.length];
      openModalByCard(nextCard);
    }}
    function applyFilters() {{
      const query = normalizeText(search.value.trim());
      let visible = 0;
      for (const card of cards) {{
        const show = (!month.value || card.dataset.month === month.value)
          && (!type.value || card.dataset.type === type.value)
          && (!category.value || card.dataset.category === category.value)
          && (!agency.value || card.dataset.agency === agency.value)
          && (!query || card._search.includes(query));
        card.style.display = show ? '' : 'none';
        if (show) visible += 1;
      }}
      count.textContent = visible + ' ato(s) encontrado(s)';
    }}
    [search, month, type, category, agency].forEach(item => item.addEventListener('input', applyFilters));
    [month, type, category, agency].forEach(item => item.addEventListener('change', applyFilters));
    document.querySelectorAll('.open-act').forEach(button => {{
      button.addEventListener('click', () => openModalByCard(button.closest('.act-card')));
    }});
    modalClose.addEventListener('click', closeModal);
    modalPrev.addEventListener('click', () => moveModal(-1));
    modalNext.addEventListener('click', () => moveModal(1));
    modalSearch.addEventListener('input', highlightModalSearch);
    modal.addEventListener('click', event => {{
      if (event.target === modal) {{
        closeModal();
      }}
    }});
    document.addEventListener('keydown', event => {{
      if (modal.hidden) {{
        return;
      }}
      if (event.key === 'Escape') {{
        closeModal();
      }}
      if (event.key === 'ArrowLeft') {{
        moveModal(-1);
      }}
      if (event.key === 'ArrowRight') {{
        moveModal(1);
      }}
    }});
    applyFilters();
  </script>
</body>
</html>
"""
    (OUTPUT_ROOT / "consulta_geral.html").write_text(html_page, encoding="utf-8")


def write_readme(diaries: list[Diary], rows: list[dict], acts: list[dict], structured_acts: list[dict] | None = None) -> None:
    by_month = Counter(row["ano_mes"] for row in rows if row.get("ano_mes"))
    focus_count = sum(1 for row in rows if row["termos_fazenda"])
    appointment_count = sum(1 for act in acts if normalize(act["ato"]).startswith("nomear"))
    dismissal_count = sum(1 for act in acts if normalize(act["ato"]).startswith("exonerar"))
    structured_count = len(structured_acts or [])
    lines = [
        "# Diarios Oficiais de Caruaru",
        "",
        f"Periodo coletado: {START_DATE:%d/%m/%Y} a {END_DATE:%d/%m/%Y}.",
        f"Total de PDFs baixados: {len(rows)}.",
        f"Diarios com mencoes ligadas a Fazenda/tributos/receita: {focus_count}.",
        f"Atos de nomeacao extraidos: {appointment_count}.",
        f"Atos de exoneracao extraidos: {dismissal_count}.",
        f"Total de atos de pessoal extraidos: {len(acts)}.",
        f"Total de atos gerais estruturados: {structured_count}.",
        "",
        "## Como consultar",
        "",
        "- Abra `consulta_geral.html` no navegador para pesquisar decretos, portarias, anexos, artigos, secoes, notificacoes, termos e secretarias.",
        "- Abra `atos_pessoal.csv` no Excel para filtrar pessoa por pessoa, com portaria, ato, cargo, orgao e efeitos.",
        "- Abra `atos_estruturados.csv` no Excel para ver um ato por linha, com tipo, categoria, secretaria, ementa, autoridades e texto limpo.",
        "- Abra `partes_dos_atos.csv` para consultar artigo por artigo, considerando, anexo, capitulo, secao, inciso e assinatura.",
        "- Abra `indice.csv` no Excel para filtrar por data, mes, edicao ou termos encontrados.",
        "- Abra `consulta.html` no navegador para pesquisar por pessoa, cargo, secretaria/orgao, nomeacoes e exoneracoes.",
        "- Veja `atos_pessoal_por_secretaria.md` para uma contagem agrupada por secretaria/orgao.",
        "- Veja `atos_estruturados_por_categoria.md` para uma contagem por tipo, categoria, mes e Fazenda/SEFAZ.",
        "- As pastas estao separadas por `ano/mes`, e cada arquivo comeca por `AAAA-MM-DD`.",
        "- Os textos extraidos ficam na subpasta `textos`; a versao mais amigavel para leitura fica em `leituras`.",
        "- Veja `resumos_mensais.md` para uma leitura consolidada mes a mes.",
        "",
        "## Quantidade por mes",
        "",
    ]
    for month, count in sorted(by_month.items()):
        year, month_number = month.split("-")
        lines.append(f"- {month}: {count} diarios")
    (OUTPUT_ROOT / "LEIA-ME.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def relative_to_output(path_text: str) -> str:
    path = Path(path_text)
    try:
        return path.relative_to(OUTPUT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_legacy_html_index(rows: list[dict]) -> None:
    months = sorted({row["ano_mes"] for row in rows})
    cards = []
    for row in rows:
        pdf = html.escape(relative_to_output(row["arquivo_pdf"]))
        text = html.escape(relative_to_output(row["arquivo_texto"]))
        searchable = normalize(
            " ".join(
                [
                    row["data"],
                    row["ano_mes"],
                    row["edicao"],
                    row["termos_fazenda"],
                    row["termos_pessoal"],
                    row["termos_gerais"],
                    row["trechos_fazenda"],
                    row["trechos_nomeacao_exoneracao"],
                ]
            )
        )
        focus = html.escape(row["termos_fazenda"] or "Sem destaque de Fazenda")
        personnel = html.escape(row["termos_pessoal"] or "Sem nomeacao/exoneracao identificada")
        focus_items = [
            html.escape(clean_fragment(item))
            for item in row["trechos_fazenda"].split(" | ")
            if item.strip()
        ][:3]
        personnel_items = [
            html.escape(clean_fragment(item))
            for item in row["trechos_nomeacao_exoneracao"].split(" | ")
            if item.strip()
        ][:4]
        focus_html = "".join(f"<p>{item}</p>" for item in focus_items) or "<p>Sem trecho de Fazenda destacado.</p>"
        personnel_html = "".join(f"<p>{item}</p>" for item in personnel_items) or "<p>Sem trecho de nomeacao/exoneracao destacado.</p>"
        flags = []
        if row["tem_nomeacao"] == "sim":
            flags.append("nomeacao")
        if row["tem_exoneracao"] == "sim":
            flags.append("exoneracao")
        if row["termos_fazenda"]:
            flags.append("fazenda")
        flag_text = " ".join(flags) or "outros"
        cards.append(
            f"""
            <article class="diary-card" data-month="{html.escape(row['ano_mes'])}" data-kind="{html.escape(flag_text)}" data-search="{html.escape(searchable)}">
              <div class="card-head">
                <div>
                  <span class="date">{html.escape(row['data'])}</span>
                  <h2>{html.escape(row['edicao'])}</h2>
                </div>
              <div class="links"><a href="{pdf}" target="_blank" rel="noopener">PDF</a><a href="{text}" target="_blank" rel="noopener">Texto bruto</a></div>
              </div>
              <div class="tags">
                <span>{focus}</span>
                <span>{personnel}</span>
              </div>
              <details open>
                <summary>Nomeacoes e exoneracoes</summary>
                <div class="snippet-list">{personnel_html}</div>
              </details>
              <details>
                <summary>Fazenda / tributos</summary>
                <div class="snippet-list">{focus_html}</div>
              </details>
            </article>"""
        )

    options = "\n".join(f'<option value="{month}">{month_label(month)}</option>' for month in months)
    html_page = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Consulta - Diarios Oficiais de Caruaru</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; color: #1f2933; background: #f5f7f8; }}
    header {{ background: #0f5d45; color: white; padding: 22px 28px; }}
    main {{ padding: 20px 28px 36px; max-width: 1280px; margin: 0 auto; }}
    h1 {{ margin: 0 0 6px; font-size: 26px; }}
    .toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 18px 0; }}
    input, select {{ height: 38px; border: 1px solid #b8c2cc; border-radius: 6px; padding: 0 10px; font-size: 14px; }}
    input {{ min-width: 280px; flex: 1; }}
    .summary {{ margin: 10px 0 18px; font-weight: 700; }}
    .filters {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 16px; }}
    .filters button {{ border: 1px solid #9fb4aa; background: white; color: #173f32; border-radius: 6px; padding: 8px 12px; cursor: pointer; }}
    .filters button.active {{ background: #173f32; color: white; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 14px; }}
    .diary-card {{ background: white; border: 1px solid #d8e0e5; border-radius: 8px; padding: 14px; box-shadow: 0 1px 2px rgba(16, 24, 40, .06); }}
    .card-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
    .date {{ display: inline-block; color: #586875; font-weight: 700; margin-bottom: 4px; }}
    h2 {{ font-size: 17px; margin: 0; }}
    .links {{ white-space: nowrap; }}
    .links a {{ display: inline-block; margin-right: 8px; color: #0f5d45; font-weight: 700; }}
    .quick a {{ color: white; margin-right: 14px; }}
    .tags {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }}
    .tags span {{ background: #eef4f1; border: 1px solid #d5e4de; border-radius: 999px; padding: 5px 9px; font-size: 12px; }}
    details {{ border-top: 1px solid #e3e8ec; padding-top: 10px; margin-top: 10px; }}
    summary {{ cursor: pointer; font-weight: 700; color: #173f32; }}
    .snippet-list p {{ line-height: 1.5; margin: 10px 0 0; }}
    @media (max-width: 760px) {{
      main {{ padding: 14px; }}
      .cards {{ grid-template-columns: 1fr; }}
      .card-head {{ display: block; }}
      .links {{ margin-top: 10px; white-space: normal; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Diarios Oficiais de Caruaru</h1>
    <div>Periodo: {START_DATE:%d/%m/%Y} a {END_DATE:%d/%m/%Y} - {len(rows)} diarios</div>
    <p class="quick"><a href="resumos_mensais.md">Resumo mensal</a><a href="indice.csv">Indice CSV</a></p>
  </header>
  <main>
    <div class="toolbar">
      <input id="search" placeholder="Pesquisar por nome, cargo, secretaria, nomear, exonerar, IPTU...">
      <select id="month"><option value="">Todos os meses</option>{options}</select>
    </div>
    <div class="filters">
      <button class="active" data-kind="">Todos</button>
      <button data-kind="nomeacao">Nomeacoes</button>
      <button data-kind="exoneracao">Exoneracoes</button>
      <button data-kind="fazenda">Fazenda</button>
    </div>
    <div class="summary" id="count"></div>
    <section class="cards">
      {''.join(cards)}
    </section>
  </main>
  <script>
    const search = document.querySelector('#search');
    const month = document.querySelector('#month');
    const cards = [...document.querySelectorAll('.diary-card')];
    const count = document.querySelector('#count');
    const buttons = [...document.querySelectorAll('.filters button')];
    let activeKind = '';
    function normalizeText(value) {{
      return value.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
    }}
    function applyFilters() {{
      const query = normalizeText(search.value.trim());
      const selectedMonth = month.value;
      let visible = 0;
      for (const row of cards) {{
        const okMonth = !selectedMonth || row.dataset.month === selectedMonth;
        const okKind = !activeKind || row.dataset.kind.includes(activeKind);
        const okSearch = !query || row.dataset.search.includes(query);
        const show = okMonth && okKind && okSearch;
        row.style.display = show ? '' : 'none';
        if (show) visible += 1;
      }}
      count.textContent = visible + ' diario(s) encontrado(s)';
    }}
    buttons.forEach(button => button.addEventListener('click', () => {{
      activeKind = button.dataset.kind;
      buttons.forEach(item => item.classList.toggle('active', item === button));
      applyFilters();
    }}));
    search.addEventListener('input', applyFilters);
    month.addEventListener('change', applyFilters);
    applyFilters();
  </script>
</body>
</html>
"""
    (OUTPUT_ROOT / "consulta.html").write_text(html_page, encoding="utf-8")


def act_kind(ato: str) -> str:
    norm = normalize(ato)
    if norm.startswith("nomear"):
        return "nomeacao"
    if norm.startswith("exonerar"):
        return "exoneracao"
    return "outros"


def write_html_index(rows: list[dict], acts: list[dict]) -> None:
    months = sorted({row["ano_mes"] for row in rows})
    agencies = sorted({act["orgao"] for act in acts if act["orgao"]})
    act_cards = []
    for act in acts:
        pdf = html.escape(relative_to_output(act["arquivo_pdf"]))
        kind = act_kind(act["ato"])
        searchable = normalize(
            " ".join(
                [
                    act["data"],
                    act["ano_mes"],
                    act["edicao"],
                    act["portaria"],
                    act["ato"],
                    act["nome"],
                    act["cpf"],
                    act["cargo"],
                    act["orgao"],
                    act["efeitos"],
                    act["texto"],
                ]
            )
        )
        act_cards.append(
            f"""
            <article class="act-card" data-month="{html.escape(act['ano_mes'])}" data-kind="{kind}" data-agency="{html.escape(act['orgao'])}" data-search="{html.escape(searchable)}">
              <div class="card-head">
                <div>
                  <span class="date">{html.escape(act['data'])} - {html.escape(act['edicao'])}</span>
                  <h2>{html.escape(act['nome'] or 'Nome nao identificado')}</h2>
                </div>
                <span class="kind {kind}">{html.escape(act['ato'])}</span>
              </div>
              <dl>
                <div><dt>Portaria</dt><dd>{html.escape(act['portaria'])}</dd></div>
                <div><dt>CPF</dt><dd>{html.escape(act['cpf'] or '-')}</dd></div>
                <div><dt>Cargo</dt><dd>{html.escape(act['cargo'] or '-')}</dd></div>
                <div><dt>Secretaria/orgao</dt><dd>{html.escape(act['orgao'] or '-')}</dd></div>
                <div><dt>Efeitos</dt><dd>{html.escape(act['efeitos'] or '-')}</dd></div>
              </dl>
              <p class="full-text">{html.escape(act['texto'])}</p>
              <div class="links"><a href="{pdf}" target="_blank" rel="noopener">PDF</a></div>
            </article>"""
        )

    diary_rows = []
    for row in rows:
        pdf = html.escape(relative_to_output(row["arquivo_pdf"]))
        diary_rows.append(
            f"<tr><td>{html.escape(row['data'])}</td><td>{html.escape(row['edicao'])}</td><td><a href=\"{pdf}\" target=\"_blank\" rel=\"noopener\">PDF</a></td></tr>"
        )

    month_options = "\n".join(f'<option value="{month}">{month_label(month)}</option>' for month in months)
    agency_options = "\n".join(f'<option value="{html.escape(agency)}">{html.escape(agency)}</option>' for agency in agencies)
    html_page = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Consulta - Atos de pessoal dos Diarios de Caruaru</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; color: #1f2933; background: #f5f7f8; }}
    header {{ background: #0f5d45; color: white; padding: 22px 28px; }}
    main {{ padding: 20px 28px 36px; max-width: 1380px; margin: 0 auto; }}
    h1 {{ margin: 0 0 6px; font-size: 26px; }}
    .quick a {{ color: white; margin-right: 14px; }}
    .toolbar {{ display: grid; grid-template-columns: minmax(280px, 1fr) 170px minmax(260px, 340px); gap: 10px; margin: 18px 0; }}
    input, select {{ height: 40px; border: 1px solid #b8c2cc; border-radius: 6px; padding: 0 10px; font-size: 14px; background: white; }}
    .filters {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 16px; }}
    .filters button {{ border: 1px solid #9fb4aa; background: white; color: #173f32; border-radius: 6px; padding: 8px 12px; cursor: pointer; }}
    .filters button.active {{ background: #173f32; color: white; }}
    .summary {{ margin: 10px 0 18px; font-weight: 700; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 14px; }}
    .act-card {{ background: white; border: 1px solid #d8e0e5; border-radius: 8px; padding: 14px; box-shadow: 0 1px 2px rgba(16, 24, 40, .06); }}
    .card-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
    .date {{ display: inline-block; color: #586875; font-weight: 700; margin-bottom: 4px; }}
    h2 {{ font-size: 18px; margin: 0; }}
    .kind {{ border-radius: 999px; padding: 6px 10px; font-size: 12px; font-weight: 700; white-space: nowrap; }}
    .nomeacao {{ background: #e6f4ee; color: #0f5d45; }}
    .exoneracao {{ background: #fdeceb; color: #9f2a22; }}
    .outros {{ background: #eef2f7; color: #364152; }}
    dl {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; margin: 12px 0; }}
    dt {{ color: #586875; font-size: 12px; font-weight: 700; }}
    dd {{ margin: 2px 0 0; line-height: 1.35; }}
    .full-text {{ border-top: 1px solid #e3e8ec; margin: 12px 0 0; padding-top: 12px; line-height: 1.5; }}
    .links {{ margin-top: 12px; }}
    .links a, table a {{ color: #0f5d45; font-weight: 700; margin-right: 10px; }}
    details {{ margin-top: 24px; background: white; border: 1px solid #d8e0e5; border-radius: 8px; padding: 12px; }}
    summary {{ cursor: pointer; font-weight: 700; color: #173f32; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
    td, th {{ border-bottom: 1px solid #e3e8ec; padding: 8px; text-align: left; }}
    @media (max-width: 820px) {{
      main {{ padding: 14px; }}
      .toolbar {{ grid-template-columns: 1fr; }}
      .cards {{ grid-template-columns: 1fr; }}
      .card-head {{ display: block; }}
      .kind {{ display: inline-block; margin-top: 10px; }}
      dl {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Atos de pessoal dos Diarios Oficiais de Caruaru</h1>
    <div>Periodo: {START_DATE:%d/%m/%Y} a {END_DATE:%d/%m/%Y} - {len(acts)} atos extraidos de {len(rows)} diarios</div>
    <p class="quick"><a href="atos_pessoal.csv">Atos CSV</a><a href="atos_pessoal_por_secretaria.md">Por secretaria</a><a href="indice.csv">Indice dos diarios</a><a href="resumos_mensais.md">Resumo mensal</a></p>
  </header>
  <main>
    <div class="toolbar">
      <input id="search" placeholder="Pesquisar pessoa, CPF, cargo, secretaria, portaria...">
      <select id="month"><option value="">Todos os meses</option>{month_options}</select>
      <select id="agency"><option value="">Todas as secretarias/orgaos</option>{agency_options}</select>
    </div>
    <div class="filters">
      <button class="active" data-kind="">Todos os atos</button>
      <button data-kind="nomeacao">Nomeacoes</button>
      <button data-kind="exoneracao">Exoneracoes</button>
      <button data-kind="outros">Outros atos</button>
    </div>
    <div class="summary" id="count"></div>
    <section class="cards">
      {''.join(act_cards)}
    </section>
    <details>
      <summary>Indice dos diarios usados na extracao</summary>
      <table>
        <thead><tr><th>Data</th><th>Edicao</th><th>Arquivos</th></tr></thead>
        <tbody>{''.join(diary_rows)}</tbody>
      </table>
    </details>
  </main>
  <script>
    const search = document.querySelector('#search');
    const month = document.querySelector('#month');
    const agency = document.querySelector('#agency');
    const cards = [...document.querySelectorAll('.act-card')];
    const count = document.querySelector('#count');
    const buttons = [...document.querySelectorAll('.filters button')];
    let activeKind = '';
    function normalizeText(value) {{
      return value.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
    }}
    function applyFilters() {{
      const query = normalizeText(search.value.trim());
      const selectedMonth = month.value;
      const selectedAgency = agency.value;
      let visible = 0;
      for (const card of cards) {{
        const okMonth = !selectedMonth || card.dataset.month === selectedMonth;
        const okKind = !activeKind || card.dataset.kind === activeKind;
        const okAgency = !selectedAgency || card.dataset.agency === selectedAgency;
        const okSearch = !query || card.dataset.search.includes(query);
        const show = okMonth && okKind && okAgency && okSearch;
        card.style.display = show ? '' : 'none';
        if (show) visible += 1;
      }}
      count.textContent = visible + ' ato(s) encontrado(s)';
    }}
    buttons.forEach(button => button.addEventListener('click', () => {{
      activeKind = button.dataset.kind;
      buttons.forEach(item => item.classList.toggle('active', item === button));
      applyFilters();
    }}));
    search.addEventListener('input', applyFilters);
    month.addEventListener('change', applyFilters);
    agency.addEventListener('change', applyFilters);
    applyFilters();
  </script>
</body>
</html>
"""
    (OUTPUT_ROOT / "consulta.html").write_text(html_page, encoding="utf-8")


def write_monthly_summary(rows: list[dict], monthly_details: dict[str, list[dict]]) -> None:
    lines = [
        "# Resumos mensais dos Diarios Oficiais de Caruaru",
        "",
        "Resumo gerado por extracao de texto dos PDFs. O foco principal e identificar mencoes relacionadas a Secretaria da Fazenda, tributos, receita municipal, fiscalizacao e temas proximos.",
        "",
    ]
    for month in sorted(monthly_details):
        entries = monthly_details[month]
        year, month_number = month.split("-")
        title = f"{MONTH_NAMES[int(month_number)]} de {year}"
        term_counter: Counter[str] = Counter()
        general_counter: Counter[str] = Counter()
        focus_entries = [entry for entry in entries if entry["focus_terms"]]
        for entry in entries:
            term_counter.update(entry["focus_terms"])
            general_counter.update(entry["general_terms"])

        lines.extend([f"## {title}", ""])
        lines.append(f"- Diarios publicados no mes: {len(entries)}")
        if term_counter:
            top_terms = ", ".join(f"{term} ({count})" for term, count in term_counter.most_common(8))
            lines.append(f"- Destaques ligados a Fazenda/receita/tributos: {top_terms}.")
        else:
            lines.append("- Nao encontrei mencoes diretas aos termos de Fazenda/tributos configurados.")
        if general_counter:
            top_general = ", ".join(f"{term} ({count})" for term, count in general_counter.most_common(8))
            lines.append(f"- Temas administrativos recorrentes: {top_general}.")

        if focus_entries:
            lines.append("")
            lines.append("### Ocorrencias ligadas a Fazenda")
            for entry in focus_entries[:12]:
                terms = ", ".join(entry["focus_terms"][:6])
                lines.append(f"- {entry['date']} - {entry['edition']}: {terms}")
                for snippet in entry["snippets"][:2]:
                    lines.append(f"  - Trecho: {snippet}")

        lines.append("")
        lines.append("### Principais registros do mes")
        representative = []
        for entry in entries:
            if entry["general_lines"]:
                representative.append(entry)
        for entry in representative[:10]:
            lines.append(f"- {entry['date']} - {entry['edition']}")
            for line in entry["general_lines"][:2]:
                lines.append(f"  - {line}")
        lines.append("")

    (OUTPUT_ROOT / "resumos_mensais.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    print("Lendo lista oficial de diarios...")
    page = fetch_text(BASE_URL)
    items = extract_diaries_json(page)
    diaries = filter_diaries(items)
    print(f"Encontrados {len(diaries)} diarios entre {START_DATE} e {END_DATE}.")

    rows: list[dict] = []
    all_personnel_acts: list[dict] = []
    all_structured_acts: list[dict] = []
    all_structured_parts: list[dict] = []
    monthly_details: dict[str, list[dict]] = defaultdict(list)
    failures: list[str] = []
    processed_diary_keys: set[tuple[str, str]] = set()

    for index, diary in enumerate(diaries, start=1):
        url = encoded_pdf_url(diary.url_path)
        print(f"[{index}/{len(diaries)}] {diary.published_at:%d/%m/%Y} - {diary.edition}")
        diary_key = (diary.published_at.strftime("%d/%m/%Y"), diary.edition)
        if (
            INCREMENTAL_UPDATE
            and diary.pdf_path.exists()
            and diary.text_path.exists()
            and diary.reading_path.exists()
        ):
            print("  ja processado; pulando download e extracao.")
            continue
        try:
            download_file(url, diary.pdf_path)
            text = extract_text(diary.pdf_path, diary.text_path)
            friendly_text = write_readable_text(text, diary.reading_path)
            focus_terms = find_terms(friendly_text, FOCUS_TERMS)
            personnel_acts = extract_personnel_acts_from_text(text)
            structured_acts, structured_parts = extract_structured_acts_from_text(text)
            personnel_terms = sorted({act["ato"] for act in personnel_acts})
            appointment_terms = [act for act in personnel_acts if act_kind(act["ato"]) == "nomeacao"]
            dismissal_terms = [act for act in personnel_acts if act_kind(act["ato"]) == "exoneracao"]
            general_terms = find_terms(friendly_text, GENERAL_TERMS)
            snippets = snippets_for_terms(friendly_text, focus_terms)
            personnel_snippets = [act["texto"] for act in personnel_acts[:10]]
            general_lines = top_lines(friendly_text, GENERAL_TERMS)
            month = diary.published_at.strftime("%Y-%m")
            for act in personnel_acts:
                all_personnel_acts.append(
                    {
                        "data": diary.published_at.strftime("%d/%m/%Y"),
                        "ano_mes": month,
                        "edicao": diary.edition,
                        "portaria": act["portaria"],
                        "ato": act["ato"],
                        "nome": act["nome"],
                        "cpf": act["cpf"],
                        "cargo": act["cargo"],
                        "orgao": act["orgao"],
                        "efeitos": act["efeitos"],
                        "texto": act["texto"],
                        "arquivo_pdf": str(diary.pdf_path),
                        "arquivo_leitura": str(diary.reading_path),
                    }
                )

            id_map: dict[str, str] = {}
            structured_meta: dict[str, dict] = {}
            act_prefix = f"{diary.published_at:%Y%m%d}-{diary.diary_id}"
            for act_index, act in enumerate(structured_acts, start=1):
                old_id = act["ato_id"]
                new_id = f"{act_prefix}-{act_index:04d}"
                id_map[old_id] = new_id
                structured_meta[old_id] = {
                    "tipo_ato": act["tipo"],
                    "identificacao": act["identificacao"],
                }
                enriched_act = dict(act)
                enriched_act.update(
                    {
                        "ato_id": new_id,
                        "data": diary.published_at.strftime("%d/%m/%Y"),
                        "ano_mes": month,
                        "edicao": diary.edition,
                        "arquivo_pdf": str(diary.pdf_path),
                        "arquivo_leitura": str(diary.reading_path),
                    }
                )
                if enriched_act["ato_pai"]:
                    enriched_act["ato_pai"] = enriched_act["ato_pai"]
                all_structured_acts.append(enriched_act)
            for part in structured_parts:
                old_id = part["ato_id"]
                enriched_part = dict(part)
                enriched_part.update(
                    {
                        "ato_id": id_map.get(old_id, old_id),
                        "data": diary.published_at.strftime("%d/%m/%Y"),
                        "ano_mes": month,
                        "edicao": diary.edition,
                        "tipo_ato": structured_meta.get(old_id, {}).get("tipo_ato", ""),
                        "identificacao": structured_meta.get(old_id, {}).get("identificacao", ""),
                    }
                )
                all_structured_parts.append(enriched_part)

            row = {
                "data": diary.published_at.strftime("%d/%m/%Y"),
                "ano_mes": month,
                "edicao": diary.edition,
                "arquivo_pdf": str(diary.pdf_path),
                "arquivo_texto": str(diary.text_path),
                "arquivo_leitura": str(diary.reading_path),
                "termos_fazenda": ", ".join(focus_terms),
                "termos_pessoal": ", ".join(personnel_terms),
                "tem_nomeacao": "sim" if appointment_terms else "nao",
                "tem_exoneracao": "sim" if dismissal_terms else "nao",
                "termos_gerais": ", ".join(general_terms),
                "trechos_fazenda": " | ".join(snippets[:3]),
                "trechos_nomeacao_exoneracao": " | ".join(personnel_snippets[:4]),
            }
            rows.append(row)
            monthly_details[month].append(
                {
                    "date": diary.published_at.strftime("%d/%m/%Y"),
                    "edition": diary.edition,
                    "focus_terms": focus_terms,
                    "general_terms": general_terms,
                    "snippets": snippets,
                    "general_lines": general_lines,
                }
            )
            processed_diary_keys.add(diary_key)
        except Exception as exc:
            failures.append(f"{diary.published_at:%d/%m/%Y} {diary.edition}: {exc}")

    if INCREMENTAL_UPDATE:
        print(
            "Atualizacao incremental: preservando dados fora do periodo "
            f"{START_DATE:%d/%m/%Y} a {END_DATE:%d/%m/%Y}."
        )
        if not failures and not processed_diary_keys:
            print("Nenhum diario novo para processar.")
            return 0
        if not failures and not existing_diary_keys("indice.csv", processed_diary_keys):
            append_csv_rows("indice.csv", rows, [
                "data", "ano_mes", "edicao", "arquivo_pdf", "arquivo_texto",
                "arquivo_leitura", "termos_fazenda", "termos_pessoal", "tem_nomeacao",
                "tem_exoneracao", "termos_gerais", "trechos_fazenda",
                "trechos_nomeacao_exoneracao",
            ])
            append_csv_rows("atos_pessoal.csv", all_personnel_acts, [
                "data", "ano_mes", "edicao", "portaria", "ato", "nome", "cpf",
                "cargo", "orgao", "efeitos", "texto", "arquivo_pdf", "arquivo_leitura",
            ])
            append_csv_rows("atos_estruturados.csv", all_structured_acts, [
                "ato_id", "data", "ano_mes", "edicao", "poder", "orgao_contexto", "tipo",
                "categoria", "identificacao", "titulo", "numero", "data_ato", "ementa",
                "autoridades", "orgaos_mencionados", "tem_anexo", "ato_pai", "texto",
                "arquivo_pdf", "arquivo_leitura",
            ])
            append_csv_rows("partes_dos_atos.csv", all_structured_parts, [
                "ato_id", "data", "ano_mes", "edicao", "tipo_ato", "identificacao", "ordem",
                "tipo_parte", "numero", "titulo", "texto",
            ])
            print(f"Atualizacao incremental: anexados {len(processed_diary_keys)} diarios novos.")
            print("Concluido sem falhas.")
            return 0
        rows = merge_existing_csv_rows("indice.csv", rows, START_DATE, END_DATE, processed_diary_keys)
        all_personnel_acts = merge_existing_csv_rows("atos_pessoal.csv", all_personnel_acts, START_DATE, END_DATE, processed_diary_keys)
        all_structured_acts = merge_existing_csv_rows("atos_estruturados.csv", all_structured_acts, START_DATE, END_DATE, processed_diary_keys)
        all_structured_parts = merge_existing_csv_rows("partes_dos_atos.csv", all_structured_parts, START_DATE, END_DATE, processed_diary_keys)
        monthly_details = monthly_details_from_rows(rows)

    write_csv(rows)
    write_personnel_csv(all_personnel_acts)
    write_personnel_summary(all_personnel_acts)
    write_structured_acts_csv(all_structured_acts)
    write_structured_parts_csv(all_structured_parts)
    write_structured_summary(all_structured_acts)
    write_monthly_summary(rows, monthly_details)
    write_readme(diaries, rows, all_personnel_acts, all_structured_acts)
    write_html_index(rows, all_personnel_acts)
    write_general_html_index(rows, all_structured_acts, all_structured_parts)

    if failures:
        (OUTPUT_ROOT / "falhas.txt").write_text("\n".join(failures) + "\n", encoding="utf-8")
        print(f"Concluido com {len(failures)} falhas. Veja falhas.txt.")
        return 1

    print("Concluido sem falhas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
