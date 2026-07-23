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
