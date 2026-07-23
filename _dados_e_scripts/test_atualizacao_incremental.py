import tempfile
import unittest
import importlib.util
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import baixar_diarios_caruaru as diario


def load_generator_module():
    path = Path(__file__).parent / "gerar_app_diario.py"
    spec = importlib.util.spec_from_file_location("gerar_app_diario_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class IncrementalUpdateTest(unittest.TestCase):
    def test_merge_existing_csv_rows_replaces_only_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_output = diario.OUTPUT_ROOT
            diario.OUTPUT_ROOT = Path(tmpdir)
            try:
                path = diario.OUTPUT_ROOT / "indice.csv"
                path.write_text(
                    "﻿data;ano_mes;edicao\n"
                    "07/05/2026;2026-05;Diario Oficial 2562\n"
                    "08/05/2026;2026-05;Diario Oficial 2563 antigo\n",
                    encoding="utf-8",
                )

                rows = diario.merge_existing_csv_rows(
                    "indice.csv",
                    [
                        {"data": "08/05/2026", "ano_mes": "2026-05", "edicao": "Diario Oficial 2563 novo"},
                        {"data": "09/05/2026", "ano_mes": "2026-05", "edicao": "Diario Oficial 2564"},
                    ],
                    diario.date(2026, 5, 8),
                    diario.date(2026, 5, 9),
                )

                self.assertEqual([row["edicao"] for row in rows], [
                    "Diario Oficial 2562",
                    "Diario Oficial 2563 novo",
                    "Diario Oficial 2564",
                ])
            finally:
                diario.OUTPUT_ROOT = original_output

    def test_merge_preserva_linhas_depois_da_janela(self):
        """Backfill de periodo antigo nao pode apagar os diarios recentes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            original_output = diario.OUTPUT_ROOT
            diario.OUTPUT_ROOT = Path(tmpdir)
            try:
                path = diario.OUTPUT_ROOT / "indice.csv"
                path.write_text(
                    "﻿data;ano_mes;edicao\n"
                    "05/03/2018;2018-03;Diario Oficial 900\n"
                    "10/07/2026;2026-07;Diario Oficial 2604\n",
                    encoding="utf-8",
                )

                rows = diario.merge_existing_csv_rows(
                    "indice.csv",
                    [{"data": "04/01/2019", "ano_mes": "2019-01", "edicao": "Diario Oficial 1100"}],
                    diario.date(2019, 1, 1),
                    diario.date(2019, 12, 31),
                )

                self.assertEqual([row["edicao"] for row in rows], [
                    "Diario Oficial 900",
                    "Diario Oficial 1100",
                    "Diario Oficial 2604",
                ])
            finally:
                diario.OUTPUT_ROOT = original_output

    def test_merge_existing_csv_rows_accepts_large_text_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_output = diario.OUTPUT_ROOT
            diario.OUTPUT_ROOT = Path(tmpdir)
            try:
                path = diario.OUTPUT_ROOT / "atos_pessoal.csv"
                large_text = "texto " * 40000
                path.write_text(
                    "﻿data;ano_mes;texto\n"
                    f"07/05/2026;2026-05;{large_text}\n",
                    encoding="utf-8",
                )

                rows = diario.merge_existing_csv_rows(
                    "atos_pessoal.csv", [], diario.date(2026, 5, 8), diario.date(2026, 5, 9)
                )

                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["texto"], large_text)
            finally:
                diario.OUTPUT_ROOT = original_output

    def test_generated_timestamp_is_timezone_aware(self):
        module = load_generator_module()

        value = module.current_generated_at()

        self.assertRegex(value, r"[+-]\d{2}:\d{2}$")

    def test_write_app_data_splits_large_year_into_small_shards(self):
        module = load_generator_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            original_path = module.DATA_PATH
            original_limit = getattr(module, "MAX_SHARD_BYTES", 12 * 1024 * 1024)
            module.DATA_PATH = Path(tmpdir) / "renderer" / "dados" / "diario-caruaru.json"
            module.MAX_SHARD_BYTES = 500
            try:
                acts = [
                    {
                        "id": str(index),
                        "anoMes": "2026-01",
                        "identificacao": f"Ato {index}",
                        "textoPlano": "x" * 80,
                        "orgao": "-",
                        "categoria": "-",
                        "tipo": "-",
                        "fazenda": False,
                    }
                    for index in range(8)
                ]
                module.write_app_data({
                    "generatedAt": "2026-07-23T00:00:00-03:00",
                    "source": "Diario Oficial de Caruaru",
                    "totals": {"diarios": 0, "atos": len(acts), "pessoal": 0, "fazenda": 0},
                    "months": [],
                    "agencies": [],
                    "categories": [],
                    "types": [],
                    "acts": acts,
                    "people": [],
                    "diaries": [],
                })

                manifest = module.DATA_PATH.read_text(encoding="utf-8")
                manifest_data = module.json.loads(manifest)
                shards = list(module.DATA_PATH.parent.glob("diario-caruaru-2026-*.json"))

                self.assertGreater(len(shards), 1)
                self.assertEqual(len(shards), len(manifest_data["years"]))
                self.assertTrue(all(path.stat().st_size <= module.MAX_SHARD_BYTES for path in shards))
                self.assertTrue(all(item["arquivo"].endswith(".json") for item in manifest_data["years"]))
            finally:
                module.DATA_PATH = original_path
                module.MAX_SHARD_BYTES = original_limit

    def test_workflow_runs_daily_at_brasilia_midnight_and_commits_conditionally(self):
        workflow = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "update-diarios.yml")
        text = workflow.read_text(encoding="utf-8")

        self.assertIn("0 3 * * *", text)
        self.assertIn("if git diff --quiet", text)
        self.assertIn("contents: write", text)

    def test_update_pipeline_generates_into_public_renderer_data(self):
        server = (Path(__file__).resolve().parent.parent / "servidor_app.py").read_text(encoding="utf-8")

        self.assertIn('"DIARIO_APP_ROOT"', server)
        self.assertIn("generated_dir = APP_ROOT / \"renderer\" / \"dados\"", server)


if __name__ == "__main__":
    unittest.main()
