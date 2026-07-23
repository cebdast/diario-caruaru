(function (global) {
  'use strict';

  const MIN_CARACTERES = 3;

  function removerAcentos(value) {
    return String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  }

  function normalizar(value) {
    return removerAcentos(value).toLowerCase();
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ---------------------------------------------------------------------
  // Extracao de texto plano do textoHtml (para busca e trechos)
  // ---------------------------------------------------------------------

  const extratorDiv = typeof document !== 'undefined' ? document.createElement('div') : null;

  function textoDoHtml(htmlStr) {
    if (!htmlStr || !extratorDiv) return '';
    // Espaco antes de cada tag evita colar palavras de blocos adjacentes.
    extratorDiv.innerHTML = String(htmlStr).replace(/</g, ' <');
    const texto = extratorDiv.textContent || '';
    extratorDiv.textContent = '';
    return texto.replace(/\s+/g, ' ').trim();
  }

  function parseDataBr(str) {
    const m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(String(str || '').trim());
    if (!m) return new Date(0);
    return new Date(Number(m[3]), Number(m[2]) - 1, Number(m[1]));
  }

  function chaveDiario(data, edicao) {
    return `${data}|${edicao}`;
  }

  // ---------------------------------------------------------------------
  // Indexacao (uma vez por carga de dados, em lotes para nao travar a UI)
  // ---------------------------------------------------------------------

  function indexar(data, onProgress) {
    return new Promise((resolve) => {
      const acts = Array.isArray(data.acts) ? data.acts : [];
      const total = acts.length;
      const LOTE = 400;
      let i = 0;

      function processarLote() {
        const fim = Math.min(i + LOTE, total);
        for (; i < fim; i++) {
          const ato = acts[i];
          if (ato.metaNorm) continue; // já indexado (carga incremental por ano)
          // textoPlano pode vir pré-computado do gerador (dados v2).
          ato.textoPlano = ato.textoPlano || textoDoHtml(ato.textoHtml);
          ato.textoNorm = normalizar(ato.textoPlano);
          ato.metaNorm = normalizar([
            ato.data,
            ato.anoMes,
            ato.edicao,
            ato.poder,
            ato.orgao,
            ato.tipo,
            ato.categoria,
            ato.identificacao,
            ato.assunto,
            ato.autoridades,
            ato.orgaosMencionados,
          ].filter(Boolean).join(' '));
        }
        if (typeof onProgress === 'function') onProgress(i, total);
        if (i < total) {
          setTimeout(processarLote, 0);
          return;
        }
        construirDiarios(data);
        resolve(data);
      }

      processarLote();
    });
  }

  function construirDiarios(data) {
    const porChave = new Map();
    (data.acts || []).forEach((ato, indice) => {
      const chave = chaveDiario(ato.data, ato.edicao);
      let diario = porChave.get(chave);
      if (!diario) {
        diario = {
          chave,
          data: ato.data,
          edicao: ato.edicao,
          dataOrd: parseDataBr(ato.data),
          atos: [],
          diary: null,
        };
        porChave.set(chave, diario);
      }
      diario.atos.push(indice);
    });
    (data.diaries || []).forEach((diary) => {
      const diario = porChave.get(chaveDiario(diary.data, diary.edicao));
      if (diario) diario.diary = diary;
    });
    porChave.forEach((diario) => {
      diario.atos.sort((a, b) => {
        const ia = data.acts[a].id || '';
        const ib = data.acts[b].id || '';
        return ia < ib ? -1 : ia > ib ? 1 : 0;
      });
    });
    data._diarios = porChave;
  }

  // ---------------------------------------------------------------------
  // Busca com agrupamento por diario
  // ---------------------------------------------------------------------

  // Conta ocorrencias com a MESMA semantica de mesclagem do destaque:
  // trechos sobrepostos (ex.: "casa" dentro de "casas") valem 1 so, para o
  // contador do cartao bater com o numero de <mark> do leitor.
  function contarOcorrenciasMescladas(textoNorm, tokens) {
    const intervalos = [];
    for (const token of tokens) {
      if (!token) continue;
      let pos = textoNorm.indexOf(token);
      while (pos !== -1) {
        intervalos.push([pos, pos + token.length]);
        pos = textoNorm.indexOf(token, pos + token.length);
      }
    }
    if (!intervalos.length) return 0;
    intervalos.sort((a, b) => (a[0] - b[0]) || (a[1] - b[1]));
    let total = 1;
    let fim = intervalos[0][1];
    for (let i = 1; i < intervalos.length; i++) {
      if (intervalos[i][0] > fim) {
        total += 1;
        fim = intervalos[i][1];
      } else if (intervalos[i][1] > fim) {
        fim = intervalos[i][1];
      }
    }
    return total;
  }

  // A consulta e tratada como UMA expressao exata (frase): "jose lucas
  // mendes" so encontra essa sequencia, nao as palavras espalhadas.
  // Acentos/maiusculas continuam ignorados e espacos repetidos viram um so.
  function fraseDaConsulta(query) {
    const frase = normalizar(String(query || '')).replace(/\s+/g, ' ').trim();
    if (!frase) return '';
    // Curta demais vira ruido; numeros com 2+ digitos valem ("lei 45" ok).
    if (frase.length >= MIN_CARACTERES || /^\d{2,}$/.test(frase)) return frase;
    return '';
  }

  function buscar(data, query) {
    const consulta = String(query || '').trim();
    const frase = fraseDaConsulta(consulta);
    if (!frase) return null;

    const tokens = [frase];
    const grupos = new Map();
    let totalAtos = 0;
    let totalOcorrencias = 0;
    const acts = data.acts || [];

    for (let i = 0; i < acts.length; i++) {
      const ato = acts[i];
      if (!ato.textoNorm.includes(frase) && !ato.metaNorm.includes(frase)) continue;

      const ocorrencias = contarOcorrenciasMescladas(ato.textoNorm, tokens);

      const chave = chaveDiario(ato.data, ato.edicao);
      let grupo = grupos.get(chave);
      if (!grupo) {
        const diario = data._diarios ? data._diarios.get(chave) : null;
        grupo = {
          chave,
          data: ato.data,
          edicao: ato.edicao,
          dataOrd: diario ? diario.dataOrd : parseDataBr(ato.data),
          diary: diario ? diario.diary : null,
          ocorrencias: 0,
          atos: [],
        };
        grupos.set(chave, grupo);
      }
      grupo.atos.push({ ato, ocorrencias });
      grupo.ocorrencias += ocorrencias;
      totalAtos += 1;
      totalOcorrencias += ocorrencias;
    }

    const diarios = [...grupos.values()].sort((a, b) =>
      (b.dataOrd - a.dataOrd) || String(b.edicao).localeCompare(String(a.edicao)));

    return {
      query: consulta,
      tokens,
      totalDiarios: diarios.length,
      totalAtos,
      totalOcorrencias,
      diarios,
    };
  }

  // ---------------------------------------------------------------------
  // Mapeamento de offsets: texto normalizado -> texto original com acentos
  // ---------------------------------------------------------------------

  function normalizarChar(ch) {
    return ch.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  }

  // Colapsa sequencias de espacos/quebras em UM espaco no texto normalizado,
  // para a frase de busca (espacos simples) casar com texto original que tem
  // "\n" ou espacos duplicados (comum em nodes de texto do leitor).
  function normalizarComMapa(texto) {
    const src = String(texto || '');
    let norm = '';
    const map = [];
    let i = 0;
    while (i < src.length) {
      if (/\s/.test(src[i])) {
        map.push(i);
        norm += ' ';
        while (i < src.length && /\s/.test(src[i])) i++;
        continue;
      }
      const n = normalizarChar(src[i]);
      for (let k = 0; k < n.length; k++) {
        norm += n[k];
        map.push(i);
      }
      i++;
    }
    return { norm, map };
  }

  function posOriginalDeNorm(texto, posNorm) {
    const src = String(texto || '');
    let contagem = 0;
    for (let i = 0; i < src.length; i++) {
      const n = normalizarChar(src[i]);
      if (contagem + n.length > posNorm) return i;
      contagem += n.length;
    }
    return src.length;
  }

  // Intervalos [inicio, fim) no texto ORIGINAL cobrindo cada ocorrencia dos
  // tokens (procurados no texto normalizado), com sobreposicoes mescladas.
  function intervalosDeTexto(texto, tokens) {
    const src = String(texto || '');
    const { norm, map } = normalizarComMapa(src);
    const intervalos = [];
    for (const token of tokens || []) {
      if (!token) continue;
      let pos = norm.indexOf(token);
      while (pos !== -1) {
        const inicio = map[pos];
        const ultimoNorm = pos + token.length - 1;
        // Fim = onde comeca o PROXIMO caractere normalizado, para engolir
        // acentos combinantes (NFD) que seguem a ultima letra do token.
        const fim = ultimoNorm + 1 < map.length ? map[ultimoNorm + 1] : src.length;
        intervalos.push([inicio, fim]);
        pos = norm.indexOf(token, pos + token.length);
      }
    }
    if (intervalos.length < 2) return intervalos;
    intervalos.sort((a, b) => (a[0] - b[0]) || (a[1] - b[1]));
    const unidos = [intervalos[0]];
    for (let i = 1; i < intervalos.length; i++) {
      const atual = intervalos[i];
      const ultimo = unidos[unidos.length - 1];
      if (atual[0] <= ultimo[1]) ultimo[1] = Math.max(ultimo[1], atual[1]);
      else unidos.push(atual);
    }
    return unidos;
  }

  function destacarTexto(original, tokens) {
    const src = String(original || '');
    const unidos = intervalosDeTexto(src, tokens);
    if (!unidos.length) return escapeHtml(src);
    let htmlStr = '';
    let cursor = 0;
    for (const [inicio, fim] of unidos) {
      htmlStr += `${escapeHtml(src.slice(cursor, inicio))}<mark>${escapeHtml(src.slice(inicio, fim))}</mark>`;
      cursor = fim;
    }
    return htmlStr + escapeHtml(src.slice(cursor));
  }

  // ---------------------------------------------------------------------
  // Trechos (snippets) com destaque, calculados so para o que e exibido
  // ---------------------------------------------------------------------

  function extrairTrechos(ato, tokens, opts) {
    const maxTrechos = (opts && opts.maxTrechos) || 2;
    const janela = (opts && opts.janela) || 100;
    const textoPlano = ato.textoPlano || '';
    const textoNorm = ato.textoNorm || '';

    const posicoes = [];
    for (const token of tokens || []) {
      if (!token) continue;
      let pos = textoNorm.indexOf(token);
      while (pos !== -1) {
        posicoes.push(pos);
        pos = textoNorm.indexOf(token, pos + token.length);
      }
    }
    posicoes.sort((a, b) => a - b);

    const escolhidas = [];
    for (const pos of posicoes) {
      if (!escolhidas.length || pos - escolhidas[escolhidas.length - 1] > janela) {
        escolhidas.push(pos);
        if (escolhidas.length >= maxTrechos) break;
      }
    }

    if (!escolhidas.length) {
      // Casou so nos metadados: mostra o comeco do texto, sem marcacao.
      const inicio = textoPlano.slice(0, janela * 2);
      const sufixo = textoPlano.length > janela * 2 ? ' …' : '';
      return inicio ? [{ html: escapeHtml(inicio) + sufixo }] : [];
    }

    // A janela para a frente precisa caber a frase inteira, senao o
    // destaque some do trecho (frase cortada nao casa no destacarTexto).
    const maiorToken = (tokens || []).reduce((m, t) => Math.max(m, t ? t.length : 0), 0);
    const janelaFim = Math.max(janela, maiorToken + 60);

    return escolhidas.map((posNorm) => {
      const centro = posOriginalDeNorm(textoPlano, posNorm);
      let inicio = Math.max(0, centro - janela);
      let fim = Math.min(textoPlano.length, centro + janelaFim);
      let guarda = 0;
      while (inicio > 0 && !/\s/.test(textoPlano[inicio - 1]) && guarda++ < 30) inicio--;
      guarda = 0;
      while (fim < textoPlano.length && !/\s/.test(textoPlano[fim]) && guarda++ < 30) fim++;
      const prefixo = inicio > 0 ? '… ' : '';
      const sufixo = fim < textoPlano.length ? ' …' : '';
      return { html: prefixo + destacarTexto(textoPlano.slice(inicio, fim).trim(), tokens) + sufixo };
    });
  }

  global.diarioBusca = {
    MIN_CARACTERES,
    normalizar,
    removerAcentos,
    fraseDaConsulta,
    escapeHtml,
    indexar,
    buscar,
    normalizarComMapa,
    posOriginalDeNorm,
    intervalosDeTexto,
    destacarTexto,
    extrairTrechos,
    chaveDiario,
    parseDataBr,
  };
})(window);
