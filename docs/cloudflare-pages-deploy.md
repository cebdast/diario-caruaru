# Deploy no Cloudflare Pages

## Objetivo

Publicar a interface web estática do app no Cloudflare Pages, enquanto os dados são atualizados automaticamente pelo GitHub Actions.

## Como conectar

1. Suba este projeto para um repositório no GitHub.
2. No Cloudflare Pages, crie um novo projeto conectando esse repositório.
3. Configure:
   - Framework preset: `None`
   - Build command: deixe vazio
   - Build output directory: `renderer`
4. Salve e faça o primeiro deploy.

## Como a atualização funciona

- O site público serve diretamente os arquivos dentro de `renderer/`.
- Os JSONs públicos ficam em `renderer/dados/`.
- O workflow `.github/workflows/update-diarios.yml` roda todos os dias à `00:00` de Brasília, equivalente a `03:00 UTC` em `23 de julho de 2026`.
- Quando houver diário novo, o workflow atualiza os JSONs, roda os testes e faz commit no GitHub.
- O Cloudflare Pages detecta esse commit e publica automaticamente.
- Se não houver diário novo, nada é commitado.

## Observações

- Os PDFs continuam sendo abertos no portal oficial de Caruaru.
- Os dados de cada ano sao divididos em varios JSON menores para respeitar o limite gratuito de 25 MiB por arquivo do Cloudflare Pages.
- Não é necessário banco de dados para essa v1.
- Se a estrutura do portal oficial mudar, o pipeline de extração precisará ser ajustado.
