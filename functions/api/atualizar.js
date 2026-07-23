const GITHUB_DISPATCH_URL =
  'https://api.github.com/repos/cebdast/diario-caruaru/actions/workflows/update-diarios.yml/dispatches';

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
    },
  });
}

export async function onRequestPost({ request, env }) {
  if (!env.MANUAL_UPDATE_KEY || !env.GITHUB_ACTIONS_TOKEN) {
    return json({ ok: false, message: 'Atualizacao manual ainda nao foi configurada.' }, 503);
  }

  const updateKey = request.headers.get('X-Update-Key') || '';
  if (updateKey !== env.MANUAL_UPDATE_KEY) {
    return json({ ok: false, message: 'Chave de atualizacao invalida.' }, 401);
  }

  const githubToken = String(env.GITHUB_ACTIONS_TOKEN).trim();
  const response = await fetch(GITHUB_DISPATCH_URL, {
    method: 'POST',
    headers: {
      accept: 'application/vnd.github+json',
      authorization: `Bearer ${githubToken}`,
      'content-type': 'application/json',
      'x-github-api-version': '2022-11-28',
    },
    body: JSON.stringify({ ref: 'main' }),
  });

  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      return json({
        ok: false,
        message: 'GitHub recusou o token. Confirme Actions: Read and write e o repositorio diario-caruaru.',
      }, 502);
    }
    if (response.status === 404) {
      return json({
        ok: false,
        message: 'GitHub nao encontrou o repositorio ou o workflow update-diarios.yml.',
      }, 502);
    }
    return json({ ok: false, message: 'O GitHub recusou a solicitacao de atualizacao.' }, 502);
  }

  return json({
    ok: true,
    message: 'Atualizacao solicitada. Os dados serao publicados apos o processamento.',
  }, 202);
}
