import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent


def renderer_root() -> Path:
    direct = APP_ROOT.parent / "renderer"
    if direct.exists():
        return direct
    direct = APP_ROOT / "renderer"
    if direct.exists():
        return direct
    return APP_ROOT / "Diario_Caruaru_App" / "renderer"


class FinalAppInterfaceTest(unittest.TestCase):
    def test_single_search_screen_without_tabs_or_filters(self):
        html = (renderer_root() / "index.html").read_text(encoding="utf-8")

        self.assertNotIn("data-tab=", html)
        self.assertNotIn("<select", html)
        self.assertNotIn("quick-filters", html)
        self.assertEqual(html.count("<input"), 1)
        self.assertIn('id="searchInput"', html)

    def test_results_and_reader_structure(self):
        html = (renderer_root() / "index.html").read_text(encoding="utf-8")
        css = (renderer_root() / "styles.css").read_text(encoding="utf-8")

        for element_id in (
            "resumoBusca",
            "resultados",
            "mostrarMais",
            "estadoVazio",
            "leitor",
            "leitorCorpo",
            "leitorPdf",
            "marcaContador",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("Abrir PDF", html)
        self.assertIn(".leitor", css)
        self.assertIn(".bloco-diario", css)
        self.assertIn("ocorrencia-atual", css)

    def test_public_interface_hides_manual_refresh_button(self):
        html = (renderer_root() / "index.html").read_text(encoding="utf-8")

        self.assertNotIn('id="refreshData"', html)
        self.assertIn('id="dataStatus"', html)

    def test_search_engine_groups_by_diary(self):
        js = (renderer_root() / "core" / "diario-busca.js").read_text(encoding="utf-8")

        for exported in ("indexar", "buscar", "extrairTrechos", "normalizarComMapa", "destacarTexto"):
            self.assertIn(exported, js)
        self.assertIn("totalDiarios", js)

    def test_pdf_links_can_open_on_act_page(self):
        platform_js = (renderer_root() / "core" / "plataforma.js").read_text(encoding="utf-8")

        self.assertIn("pdfOpenUrl", platform_js)
        self.assertIn("#page=", platform_js)
        self.assertIn("appendPage", platform_js)

    def test_public_platform_does_not_expose_browser_update_flow(self):
        platform_js = (renderer_root() / "core" / "plataforma.js").read_text(encoding="utf-8")
        app_js = (renderer_root() / "app.js").read_text(encoding="utf-8")

        self.assertNotIn("fetch('/api/update'", platform_js)
        self.assertNotIn("function refreshData()", app_js)
        self.assertNotIn("configurarBotaoAtualizar()", app_js)


if __name__ == "__main__":
    unittest.main()
