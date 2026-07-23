# Static Pages and GitHub Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare the Diário Caruaru web app for free static hosting and replace the local browser update button with a daily automated data refresh at midnight Brasília time.

**Architecture:** Keep the web app static, continue serving a manifest plus yearly JSON shards, open official Caruaru PDF URLs directly, and run the existing incremental Python pipeline from GitHub Actions. The public UI shows only the last update timestamp while desktop-only helper code remains available for local use.

**Tech Stack:** Static HTML/CSS/JavaScript, Python data pipeline, unittest, GitHub Actions, Cloudflare Pages

## Global Constraints

- No database in v1.
- PDFs must open from the official Caruaru public URLs.
- The public web UI must not expose the local `Atualizar` button.
- The daily automation must run at `03:00 UTC`, matching `00:00` in Brasília/São Paulo on `2026-07-23`.
- If no new diary exists for the day, the automation must not write or commit anything.
- `generatedAt` must carry explicit `America/Sao_Paulo` time semantics.

---

### Task 1: Lock the public UI contract

**Files:**
- Modify: `_dados_e_scripts/test_app_final_interface.py`

**Interfaces:**
- Consumes: `renderer/index.html`, `renderer/styles.css`
- Produces: Failing tests that define the public static UI contract

- [ ] **Step 1: Write the failing test**

```python
    def test_public_interface_hides_manual_refresh_button(self):
        html = (renderer_root() / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="refreshData"', html)
        self.assertIn('id="dataStatus"', html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest _dados_e_scripts.test_app_final_interface.FinalAppInterfaceTest.test_public_interface_hides_manual_refresh_button -v`
Expected: FAIL because `refreshData` still exists in `renderer/index.html`

- [ ] **Step 3: Write minimal implementation**

Remove the refresh button markup from `renderer/index.html` and adjust the existing interface test that currently expects `refreshData` to exist.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest _dados_e_scripts.test_app_final_interface -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add _dados_e_scripts/test_app_final_interface.py renderer/index.html
git commit -m "feat: remove public manual refresh control"
```

### Task 2: Simplify public runtime behavior

**Files:**
- Modify: `renderer/app.js`
- Modify: `renderer/core/plataforma.js`

**Interfaces:**
- Consumes: UI contract from Task 1
- Produces: Public runtime that shows update status but does not expose browser-triggered updates

- [ ] **Step 1: Write the failing test**

Add assertions to `_dados_e_scripts/test_app_final_interface.py`:

```python
    def test_public_platform_does_not_expose_browser_update_flow(self):
        platform_js = (renderer_root() / "core" / "plataforma.js").read_text(encoding="utf-8")
        app_js = (renderer_root() / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("fetch('/api/update'", platform_js)
        self.assertNotIn("refreshData()", app_js)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest _dados_e_scripts.test_app_final_interface.FinalAppInterfaceTest.test_public_platform_does_not_expose_browser_update_flow -v`
Expected: FAIL because the current code still references the browser update flow

- [ ] **Step 3: Write minimal implementation**

Remove the browser-only update path and related event wiring from the public runtime while keeping PDF opening logic intact.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest _dados_e_scripts.test_app_final_interface -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add _dados_e_scripts/test_app_final_interface.py renderer/app.js renderer/core/plataforma.js
git commit -m "feat: remove public browser update flow"
```

### Task 3: Make generated timestamps explicit for São Paulo

**Files:**
- Modify: `_dados_e_scripts/gerar_app_diario.py`
- Create or Modify: `_dados_e_scripts/test_atualizacao_incremental.py`

**Interfaces:**
- Consumes: Existing generator output format
- Produces: `generatedAt` values with explicit timezone offset

- [ ] **Step 1: Write the failing test**

Add a unit test asserting the generated timestamp contains an offset:

```python
    def test_generated_timestamp_is_timezone_aware(self):
        from gerar_app_diario import current_generated_at
        value = current_generated_at()
        self.assertRegex(value, r"[+-]\d{2}:\d{2}$")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest _dados_e_scripts.test_atualizacao_incremental -v`
Expected: FAIL because `current_generated_at` does not exist yet and timestamps are naive

- [ ] **Step 3: Write minimal implementation**

Add a helper that generates ISO timestamps in `America/Sao_Paulo` and reuse it where `generatedAt` is written.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest _dados_e_scripts.test_atualizacao_incremental -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add _dados_e_scripts/gerar_app_diario.py _dados_e_scripts/test_atualizacao_incremental.py
git commit -m "feat: emit sao paulo timestamps in generated data"
```

### Task 4: Add scheduled GitHub Actions refresh

**Files:**
- Create: `.github/workflows/update-diarios.yml`
- Create: `docs/cloudflare-pages-deploy.md`

**Interfaces:**
- Consumes: Existing incremental scripts in `_dados_e_scripts/`
- Produces: Daily scheduled refresh with conditional commit behavior and deployment notes

- [ ] **Step 1: Write the failing test**

Create a lightweight workflow contract test, for example in `_dados_e_scripts/test_atualizacao_incremental.py`:

```python
    def test_workflow_runs_daily_at_brasilia_midnight_and_commits_conditionally(self):
        workflow = Path(".github/workflows/update-diarios.yml").read_text(encoding="utf-8")
        self.assertIn("0 3 * * *", workflow)
        self.assertIn("if git diff --quiet", workflow)
        self.assertIn("contents: write", workflow)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest _dados_e_scripts.test_atualizacao_incremental -v`
Expected: FAIL because the workflow file does not exist yet

- [ ] **Step 3: Write minimal implementation**

Add a workflow that checks out the repo, installs Python, runs the incremental pipeline, runs targeted tests, and commits only when tracked files changed.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest _dados_e_scripts.test_atualizacao_incremental -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/update-diarios.yml docs/cloudflare-pages-deploy.md _dados_e_scripts/test_atualizacao_incremental.py
git commit -m "feat: automate diario updates with github actions"
```

### Task 5: Verify the whole static-hosting slice

**Files:**
- Modify: none required unless verification exposes gaps
- Test: `_dados_e_scripts/test_app_final_interface.py`
- Test: `_dados_e_scripts/test_atualizacao_incremental.py`

**Interfaces:**
- Consumes: Tasks 1-4 outputs
- Produces: Verified static-hosting-ready app changes

- [ ] **Step 1: Write the failing test**

No new test file. Use the existing red-green coverage from Tasks 1-4.

- [ ] **Step 2: Run test to verify it fails**

Before implementation, at least one test from Tasks 1-4 must fail in each red phase.

- [ ] **Step 3: Write minimal implementation**

Apply only the code required to satisfy the tests and keep the data pipeline behavior unchanged outside the static-hosting scope.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest _dados_e_scripts.test_app_final_interface _dados_e_scripts.test_atualizacao_incremental -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "feat: prepare diario app for static hosting"
```
