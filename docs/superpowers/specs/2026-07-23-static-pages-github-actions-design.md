# Hospedagem Estática e Atualização Automática Design

## Objetivo

Publicar a versão web do app em hospedagem estática gratuita, mantendo os dados em JSON anual e abrindo os PDFs diretamente no portal oficial do Diário Oficial de Caruaru. A atualização deixa de ser acionada por botão no navegador e passa a ocorrer automaticamente todos os dias à meia-noite de Brasília via GitHub Actions.

## Escopo

- Hospedagem web estática compatível com Cloudflare Pages.
- Dados servidos como manifesto + shards anuais em `renderer/dados/`.
- PDFs abertos por URL pública oficial do portal de Caruaru.
- Atualização automática diária no GitHub Actions.
- Sem banco de dados na v1.

## Fora de Escopo

- Painel administrativo online.
- Login, permissões ou auditoria de usuários.
- Armazenamento próprio dos PDFs.
- Reescrita do pipeline de extração.

## Estado Atual

- O frontend já consome `renderer/dados/diario-caruaru.json`.
- O app já suporta o formato v2: manifesto com `years` e um JSON por ano.
- Existe atualização incremental local via `servidor_app.py`, usando `_dados_e_scripts/baixar_diarios_caruaru.py` e `_dados_e_scripts/gerar_app_diario.py`.
- O botão `Atualizar` depende de rotas locais `/api/*`, úteis para desktop/helper local, mas sem sentido na versão pública estática.

## Abordagens Consideradas

### 1. Cloudflare Pages + JSON anual + PDFs oficiais

Prós:
- Gratuito e simples.
- Reaproveita a estrutura atual.
- Evita armazenar centenas de MB de PDFs.

Contras:
- O app depende da disponibilidade e estabilidade das URLs oficiais dos PDFs.

### 2. Cloudflare Pages + banco para metadados

Prós:
- Facilita futuras consultas e painel online.

Contras:
- Aumenta a complexidade da v1 sem resolver o principal problema atual.

### 3. Hospedar tudo, incluindo PDFs

Prós:
- Menor dependência do portal oficial.

Contras:
- Custo de armazenamento e manutenção muito maior.
- Repositório e deploy ficam pesados desnecessariamente.

## Decisão

Seguir com a abordagem 1.

## Arquitetura

### Frontend público

- O site continua estático.
- O manifesto `renderer/dados/diario-caruaru.json` informa totais, meses, metadados e lista de arquivos anuais.
- Cada shard anual `renderer/dados/diario-caruaru-<ano>.json` contém os atos daquele ano.
- A UI exibe o carimbo `atualizado em ...` usando `generatedAt`.
- O botão `Atualizar` deixa de existir na interface web pública.

### PDFs

- Cada ato ou diário deve apontar para a URL oficial pública do PDF, preferindo a rota `/diario/...pdf`.
- O app deve abrir a URL oficial em nova aba.
- O helper local de desktop pode continuar existindo para contextos locais, mas a interface pública não deve depender dele.

### Atualização automática

- Um workflow do GitHub Actions roda diariamente em `03:00 UTC`, equivalente a `00:00` em Brasília/São Paulo na data de hoje, `23 de julho de 2026`.
- O workflow:
  1. Faz checkout do repositório.
  2. Configura Python.
  3. Executa atualização incremental.
  4. Regera o manifesto e os shards anuais.
  5. Roda os testes relevantes.
  6. Faz commit e push apenas se houver mudanças reais.
- Se não houver diário novo no dia, nada é gravado e nada é commitado.

### Fuso horário

- `generatedAt` deve ser gerado explicitamente no fuso `America/Sao_Paulo`, para o carimbo refletir o horário esperado do projeto.

## Erros e Resiliência

- Se o portal oficial não publicar diário novo, o workflow deve terminar com sucesso sem alterar arquivos.
- Se a extração falhar, o workflow deve falhar visivelmente no GitHub Actions.
- Se algum teste falhar, o deploy automático por commit não deve acontecer.

## Testes

- Teste do HTML para garantir que o botão `Atualizar` não apareça mais.
- Teste do JS para garantir que o frontend não dependa do fluxo de atualização web pública.
- Teste do gerador para garantir que `generatedAt` carregue timezone explícito.
- Teste do workflow para validar a presença do agendamento diário e a lógica de commit condicional.

## Notas Operacionais

- O workspace atual não está em um repositório Git inicializado, então não é possível cumprir a etapa de commit local da spec neste ambiente neste momento.
- O deploy no Cloudflare Pages será feito conectando o repositório GitHub depois que os arquivos estiverem preparados.
