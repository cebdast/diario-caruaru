# Consulta Diário Caruaru

Aplicativo de consulta dos Diários Oficiais de Caruaru, baseado na estrutura do app de ISSQN.

## O que ele consulta

- Atos estruturados: decretos, portarias, extratos, editais, avisos e anexos.
- Atos de pessoal: nomeações, exonerações, designações, concessões e atos relacionados.
- Secretarias e órgãos, com nomes normalizados.
- Recorte da Secretaria da Fazenda, tributos, ISS, IPTU, ITBI, receita, fiscalização e regimentos.
- Diários por mês/ano, com abertura direta do PDF.

## Como abrir

Para testar no navegador:

```bash
python -m http.server 5174 -d renderer
```

Depois abra:

```text
http://localhost:5174
```

Para usar como aplicativo desktop, instale as dependências e rode:

```bash
npm install
npm start
```

Para Android/Capacitor:

```bash
npm install
npm run mobile:sync
npm run mobile:open
```

O arquivo principal de dados é `renderer/dados/diario-caruaru.json`.

## Atualizacao manual no site

O site tambem possui o botao `Atualizar agora`. Ele dispara o mesmo workflow
automatico do GitHub Actions; a rotina noturna continua ativa.

No Cloudflare Pages, cadastre estes segredos no ambiente **Production**:

- `GITHUB_ACTIONS_TOKEN`: token fine-grained do GitHub com acesso somente ao
  repositorio `cebdast/diario-caruaru` e permissao **Actions: Read and write**.
- `MANUAL_UPDATE_KEY`: uma chave criada por voce para proteger o botao publico.

Depois de cadastrar os segredos, publique um novo deploy do Pages. A chave do
botao nunca e enviada ao GitHub; somente a Function do Cloudflare conhece o
token de Actions.
