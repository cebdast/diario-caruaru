from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from datetime import datetime


APP_ROOT = Path(__file__).resolve().parent
RENDERER_ROOT = APP_ROOT / "renderer"


def json_response(handler: SimpleHTTPRequestHandler, status: int, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_json_body(handler: SimpleHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if not length:
        return {}
    raw = handler.rfile.read(length).decode("utf-8", errors="replace")
    return json.loads(raw or "{}")


def target_to_path(payload: dict) -> str:
    target = payload.get("target") or payload.get("pdfPath") or payload.get("pdfUrl") or ""
    if isinstance(target, dict):
        target = target.get("pdfPath") or target.get("pdfUrl") or ""
    if not target:
        return ""
    if str(target).startswith("file://"):
        parsed = urlparse(str(target))
        return unquote(parsed.path).lstrip("/") if os.name == "nt" else unquote(parsed.path)
    return str(target)


def open_pdf(payload: dict) -> dict:
    target = target_to_path(payload)
    if not target:
        return {"ok": False, "message": "PDF nao informado."}
    pdf_path = safe_pdf_path(target)
    if pdf_path:
        return {"ok": True, "url": f"/api/pdf?path={quote(str(pdf_path))}", "message": "PDF pronto para abrir no navegador."}
    if target.startswith(("http://", "https://", "file://")):
        return {"ok": True, "url": target, "message": "PDF pronto para abrir no navegador."}
    return {"ok": False, "message": f"PDF nao encontrado: {target}"}


def pdf_path_candidates(path_text: str) -> list[Path]:
    candidate = Path(target_to_path({"target": path_text}))
    candidates = [candidate]
    marker = "Diarios_Oficiais_Caruaru"
    parts = list(candidate.parts)
    if marker in parts:
        rel = Path(*parts[parts.index(marker) + 1:])
        candidates.extend([
            APP_ROOT / "_dados_e_scripts" / marker / rel,
            APP_ROOT / marker / rel,
            APP_ROOT.parent / "Diario de caruaru" / marker / rel,
        ])
    return candidates


def safe_pdf_path(path_text: str) -> Path | None:
    if not path_text:
        return None
    roots = [APP_ROOT, APP_ROOT.parent, APP_ROOT.parent / "Diario de caruaru"]
    for candidate in pdf_path_candidates(path_text):
        if not candidate.exists() or candidate.suffix.lower() != ".pdf":
            continue
        resolved = candidate.resolve()
        for root in roots:
            try:
                resolved.relative_to(root.resolve())
                return resolved
            except ValueError:
                continue
    return None


def resolve_source_root() -> Path | None:
    configured = os.environ.get("DIARIO_CARUARU_SOURCE")
    candidates = [
        Path(configured) if configured else None,
        APP_ROOT / "_dados_e_scripts",
        APP_ROOT.parent / "Diario de caruaru",
        APP_ROOT,
    ]
    for candidate in candidates:
        if candidate and (candidate / "baixar_diarios_caruaru.py").exists():
            return candidate
    return None


def run_python(source_root: Path, script: str, extra_env: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    process = subprocess.run(
        [sys.executable, script],
        cwd=source_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    output = "\n".join(part for part in [process.stdout, process.stderr] if part).strip()
    if process.returncode != 0:
        raise RuntimeError(f"{script} terminou com erro {process.returncode}.\n{output}")
    return output


def parse_br_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%d/%m/%Y")
    except (TypeError, ValueError):
        return None


def latest_saved_date() -> datetime | None:
    data_path = APP_ROOT / "renderer" / "dados" / "diario-caruaru.json"
    if data_path.exists():
        try:
            data = json.loads(data_path.read_text(encoding="utf-8"))
            dates = [parse_br_date(item.get("data")) for item in data.get("diaries", [])]
            dates = [item for item in dates if item]
            if dates:
                return max(dates)
        except Exception:
            pass
    return None


def update_data() -> dict:
    source_root = resolve_source_root()
    if not source_root:
        return {"ok": False, "message": "Nao encontrei a pasta fonte para atualizar os dados."}

    start_date = latest_saved_date()
    env = {
        "DIARIO_INCREMENTAL": "1",
        "DIARIO_APP_ROOT": str(APP_ROOT),
    }
    if start_date:
        env["DIARIO_START_DATE"] = start_date.strftime("%Y-%m-%d")

    indice_path = source_root / "Diarios_Oficiais_Caruaru" / "indice.csv"
    indice_antes = indice_path.stat().st_mtime if indice_path.exists() else 0
    run_python(source_root, "baixar_diarios_caruaru.py", env)
    indice_depois = indice_path.stat().st_mtime if indice_path.exists() else 0
    if indice_antes and indice_depois == indice_antes:
        return {
            "ok": True,
            "message": "Nenhum diario novo encontrado.",
            "changedYears": [],
            "manifestChanged": False,
        }
    run_python(source_root, "gerar_app_diario.py", env)

    generated_dir = APP_ROOT / "renderer" / "dados"
    target_dir = APP_ROOT / "renderer" / "dados"
    generated_json = generated_dir / "diario-caruaru.json"
    if not generated_json.exists():
        return {"ok": False, "message": "Atualizacao terminou, mas o JSON nao foi gerado."}
    changed_years: list[int] = []
    manifest_changed = False
    if generated_dir.resolve() != target_dir.resolve():
        target_dir.mkdir(parents=True, exist_ok=True)
        # Copia apenas o manifesto e os anos que realmente mudaram.
        for shard in sorted(generated_dir.glob("diario-caruaru*.json")):
            target = target_dir / shard.name
            changed = not target.exists()
            if not changed:
                source_stat = shard.stat()
                target_stat = target.stat()
                changed = source_stat.st_size != target_stat.st_size or source_stat.st_mtime > target_stat.st_mtime + 0.001
            if not changed:
                continue
            shutil.copy2(shard, target)
            if shard.name == "diario-caruaru.json":
                manifest_changed = True
            else:
                match = re.match(r"diario-caruaru-(\d{4})(?:-\d+)?\.json$", shard.name)
                if match:
                    changed_years.append(int(match.group(1)))
    if start_date:
        return {
            "ok": True,
            "message": f"Dados atualizados desde {start_date:%d/%m/%Y}.",
            "changedYears": changed_years,
            "manifestChanged": manifest_changed,
        }
    return {
        "ok": True,
        "message": "Dados atualizados.",
        "changedYears": changed_years,
        "manifestChanged": manifest_changed,
    }


class DiarioHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(RENDERER_ROOT), **kwargs)

    def log_message(self, format: str, *args) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_GET(self) -> None:
        if self.path == "/api/capabilities":
            json_response(self, 200, {"ok": True, "openPdf": True, "update": True})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/pdf":
            query = parse_qs(parsed.query)
            pdf_path = safe_pdf_path(unquote((query.get("path") or [""])[0]))
            if not pdf_path:
                json_response(self, 404, {"ok": False, "message": "PDF nao encontrado."})
                return
            data = pdf_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'inline; filename="{pdf_path.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def do_POST(self) -> None:
        try:
            payload = read_json_body(self)
            if self.path == "/api/open-pdf":
                json_response(self, 200, open_pdf(payload))
                return
            if self.path == "/api/update":
                json_response(self, 200, update_data())
                return
            json_response(self, 404, {"ok": False, "message": "Rota nao encontrada."})
        except Exception as exc:  # pragma: no cover - protective boundary for local helper.
            json_response(self, 500, {"ok": False, "message": str(exc)})


def find_server(port: int) -> ThreadingHTTPServer:
    for candidate in range(port, port + 20):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), DiarioHandler)
        except OSError:
            continue
    raise RuntimeError("Nao encontrei uma porta livre para abrir o app.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5174)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    server = find_server(args.port)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    open_url = f"{url}?v={int(time.time())}"
    print(f"Consulta Diario Caruaru aberta em {url}")
    print("Mantenha esta janela aberta enquanto estiver usando Atualizar ou PDF.")

    if args.open:
        threading.Thread(target=lambda: (time.sleep(1), webbrowser.open(open_url)), daemon=True).start()

    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
