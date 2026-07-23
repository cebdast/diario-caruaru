const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const ATOS_POR_BLOCO = 6;
const DIARIOS_POR_PAGINA = 20;

const state = {
  data: null,
  pronto: false,
  resultado: null,
  diariosVisiveis: DIARIOS_POR_PAGINA,
  expandidos: new Set(),
  carregandoAnos: false,
  leitor: {
    aberto: false,
    modo: 'busca',
    chave: '',
    lista: [],
    indice: -1,
    marcas: [],
    marcaAtual: -1,
  },
};

const escapeHtml = (value) => window.diarioBusca.escapeHtml(value);

function debounce(fn, ms = 250) {
  let timer = null;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
  wrapped.flush = (...args) => {
    clearTimeout(timer);
    fn(...args);
  };
  return wrapped;
}

function numeroBr(n) {
  return Number(n || 0).toLocaleString('pt-BR');
}

function plural(n, singular, pluralForm) {
  return `${numeroBr(n)} ${n === 1 ? singular : pluralForm}`;
}

const formatadorDataLonga = new Intl.DateTimeFormat('pt-BR', {
  weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
});

function dataExtensa(dataBr) {
  const data = window.diarioBusca.parseDataBr(dataBr);
  if (!data.getTime()) return dataBr || '';
  const texto = formatadorDataLonga.format(data);
  return texto.charAt(0).toUpperCase() + texto.slice(1);
}

function rotuloEdicao(edicao) {
  const m = /(\d+)\s*$/.exec(String(edicao || ''));
  return m ? `Edição nº ${m[1]}` : String(edicao || '');
}

function pdfHref(item) {
  return window.plataforma.pdfHref ? window.plataforma.pdfHref(item) : '#';
}

function openPdf(item) {
  window.plataforma.abrirPdf(item).catch((error) => {
    const status = $('#updateStatus');
    if (status) status.textContent = error.message;
  });
}

// ---------------------------------------------------------------------
// Limpeza do corpo do ato (portado do app anterior)
// ---------------------------------------------------------------------

function cleanupLeadingNoise(body, ato) {
  const titleNorm = String(ato.identificacao || '').replace(/\s+/g, ' ').trim().replace(/\.$/, '').toLowerCase();
  const containers = body.querySelectorAll('.contest-annex, .annex-table');
  const targets = containers.length ? [...containers] : [body];
  for (const root of targets) {
    const headings = [...root.querySelectorAll(':scope > .doc-heading')];
    for (const h of headings) {
      const txt = (h.textContent || '').replace(/\s+/g, ' ').trim();
      const norm = txt.toLowerCase().replace(/\.$/, '');
      const words = txt.split(/\s+/);
      const isPageFooter = /^\d{1,4}\s+\d{1,2}\s+de\s+[a-zçãéíóú]+\s+de\s+\d{4}/i.test(txt) || /\|\s*\d+\s*$/.test(txt);
      const isDuplicateTitle = norm === titleNorm;
      const isSignatureName = /^[A-ZÁÉÍÓÚÇÂÊÔÃÕ][A-ZÁÉÍÓÚÇÂÊÔÃÕ\s]{4,}$/.test(txt) && words.length <= 6;
      const isSignatureRole = /^(diretor|diretora|presidente|prefeito|secret[aá]ri[oa]|gestor|gestora|procurador|procuradora|controlador|superintendente)\b/i.test(txt);
      if (isPageFooter || isDuplicateTitle || isSignatureName || isSignatureRole) {
        h.remove();
      } else {
        break;
      }
    }
  }
}

function assuntoDoAto(ato) {
  const rawSubject = Object.prototype.hasOwnProperty.call(ato, 'modalAssunto') ? ato.modalAssunto : ato.assunto;
  const subject = (rawSubject || '').replace(/\s+/g, ' ').trim();
  if (!subject || subject === ato.identificacao) return '';

  const body = String(ato.textoPlano || '');
  if (!body) return subject;

  const normalizedSubject = window.diarioBusca.normalizar(subject).replace(/[^a-z0-9]+/g, ' ').trim();
  const normalizedBodyStart = window.diarioBusca.normalizar(body.slice(0, Math.max(700, subject.length + 220))).replace(/[^a-z0-9]+/g, ' ').trim();
  const subjectPrefix = normalizedSubject.slice(0, Math.min(160, normalizedSubject.length));

  if (subjectPrefix.length >= 48 && normalizedBodyStart.includes(subjectPrefix)) return '';
  if (ato.tipo === 'Extrato' && normalizedBodyStart.includes(normalizedSubject.slice(0, 80))) return '';

  return subject;
}

// ---------------------------------------------------------------------
// Busca e resultados agrupados por diário
// ---------------------------------------------------------------------

function tokensAtivos() {
  return state.resultado ? state.resultado.tokens : [];
}

function grupoPorChave(chave) {
  return state.resultado ? state.resultado.diarios.find((d) => d.chave === chave) : null;
}

function onBusca() {
  if (!state.pronto) return;
  const query = $('#searchInput').value;
  $('#limparBusca').hidden = !query;
  state.resultado = window.diarioBusca.buscar(state.data, query);
  state.diariosVisiveis = DIARIOS_POR_PAGINA;
  state.expandidos = new Set();
  renderResultados();
}

function renderResultados() {
  const resumo = $('#resumoBusca');
  const container = $('#resultados');
  const mostrarMais = $('#mostrarMais');
  const vazio = $('#estadoVazio');
  const hint = $('#searchHint');
  const query = $('#searchInput').value.trim();

  if (!state.resultado) {
    resumo.hidden = true;
    container.innerHTML = '';
    mostrarMais.hidden = true;
    vazio.hidden = false;
    if (query) {
      hint.textContent = `Digite ao menos ${window.diarioBusca.MIN_CARACTERES} letras para buscar.`;
    } else if (!state.carregandoAnos) {
      hint.textContent = 'Busca a expressão exata, ignorando acentos e maiúsculas.';
    }
    return;
  }

  const r = state.resultado;
  vazio.hidden = true;
  if (!state.carregandoAnos) hint.textContent = '';
  const notaCarregando = state.carregandoAnos
    ? ' <em class="nota-carregando">(ainda carregando anos anteriores…)</em>'
    : '';

  if (!r.totalDiarios) {
    resumo.hidden = false;
    resumo.innerHTML = `Nenhum diário contém a expressão exata <strong>“${escapeHtml(r.query)}”</strong>. Confira a grafia ou tente uma parte do termo (ex.: só o sobrenome).${notaCarregando}`;
    container.innerHTML = '';
    mostrarMais.hidden = true;
    return;
  }

  resumo.hidden = false;
  resumo.innerHTML = `<strong>“${escapeHtml(r.query)}”</strong> aparece em <strong>${plural(r.totalDiarios, 'diário', 'diários')}</strong>` +
    ` — ${plural(r.totalAtos, 'ato', 'atos')}, ${plural(r.totalOcorrencias, 'ocorrência', 'ocorrências')}.${notaCarregando}`;

  const visiveis = r.diarios.slice(0, state.diariosVisiveis);
  container.innerHTML = visiveis.map(renderBlocoDiario).join('');
  mostrarMais.hidden = state.diariosVisiveis >= r.totalDiarios;
  if (!mostrarMais.hidden) {
    mostrarMais.textContent = `Mostrar mais diários (${numeroBr(r.totalDiarios - state.diariosVisiveis)} restantes)`;
  }
}

function renderBlocoDiario(grupo) {
  const expandido = state.expandidos.has(grupo.chave);
  const atosVisiveis = expandido ? grupo.atos : grupo.atos.slice(0, ATOS_POR_BLOCO);
  const restantes = grupo.atos.length - atosVisiveis.length;
  const alvoPdf = grupo.diary || (grupo.atos[0] && grupo.atos[0].ato);
  const tokens = tokensAtivos();

  const linhas = atosVisiveis.map(({ ato, ocorrencias }) => {
    const trechos = window.diarioBusca.extrairTrechos(ato, tokens, { maxTrechos: 2, janela: 90 })
      .map((t) => `<p class="trecho">${t.html}</p>`).join('');
    const contagem = ocorrencias
      ? `<span class="ato-ocorrencias">${plural(ocorrencias, 'ocorrência', 'ocorrências')}</span>`
      : '';
    return `
      <button type="button" class="ato-linha" data-abrir-ato="${escapeHtml(ato.id)}" data-chave="${escapeHtml(grupo.chave)}">
        <span class="ato-cabeca">
          <span class="ato-tipo">${escapeHtml(ato.tipo || 'Ato')}</span>
          <strong class="ato-titulo">${escapeHtml(ato.identificacao || '(sem identificação)')}</strong>
          ${contagem}
        </span>
        ${trechos}
      </button>
    `;
  }).join('');

  const expansor = restantes > 0
    ? `<button type="button" class="bloco-expandir" data-expandir="${escapeHtml(grupo.chave)}">Mostrar mais ${plural(restantes, 'ato deste diário', 'atos deste diário')}</button>`
    : '';

  return `
    <article class="bloco-diario">
      <header class="bloco-cabecalho">
        <div>
          <h3 class="bloco-data">${escapeHtml(dataExtensa(grupo.data))}</h3>
          <p class="bloco-edicao">${escapeHtml(rotuloEdicao(grupo.edicao))} · ${plural(grupo.atos.length, 'ato encontrado', 'atos encontrados')} · ${plural(grupo.ocorrencias, 'ocorrência', 'ocorrências')}</p>
        </div>
        <a class="pdf-link" href="${escapeHtml(pdfHref(alvoPdf))}" data-pdf-chave="${escapeHtml(grupo.chave)}" target="_blank" rel="noopener">PDF do diário</a>
      </header>
      <div class="bloco-atos">${linhas}${expansor}</div>
    </article>
  `;
}

// ---------------------------------------------------------------------
// Estado vazio (antes de buscar)
// ---------------------------------------------------------------------

function renderEstadoVazio() {
  const data = state.data;
  const meses = [...(data.months || [])].sort((a, b) => String(a.value).localeCompare(String(b.value)));
  const primeiro = meses.length ? meses[0].label : '';
  const ultimo = meses.length ? meses[meses.length - 1].label : '';

  $('#vazioTotais').innerHTML = `
    <div class="total-item"><strong>${numeroBr(data.totals.diarios)}</strong><span>diários</span></div>
    <div class="total-item"><strong>${numeroBr(data.totals.atos)}</strong><span>atos publicados</span></div>
    <div class="total-item"><strong>${escapeHtml(primeiro)}</strong><span>até ${escapeHtml(ultimo)}</span></div>
  `;

  const recentes = [...(data.diaries || [])]
    .sort((a, b) => window.diarioBusca.parseDataBr(b.data) - window.diarioBusca.parseDataBr(a.data))
    .slice(0, 8);

  $('#ultimosDiarios').innerHTML = recentes.map((diary) => {
    const chave = window.diarioBusca.chaveDiario(diary.data, diary.edicao);
    const temAtos = state.data._diarios && state.data._diarios.has(chave);
    return `
      <li>
        <span class="ultimo-info"><strong>${escapeHtml(diary.data)}</strong> · ${escapeHtml(rotuloEdicao(diary.edicao))}</span>
        <span class="ultimo-acoes">
          ${temAtos ? `<button type="button" data-ver-diario="${escapeHtml(chave)}">Ver atos</button>` : ''}
          <a class="pdf-link" href="${escapeHtml(pdfHref(diary))}" data-pdf-chave="${escapeHtml(chave)}" target="_blank" rel="noopener">PDF</a>
        </span>
      </li>
    `;
  }).join('');
}

// ---------------------------------------------------------------------
// Leitor em tela cheia
// ---------------------------------------------------------------------

function atosDoDiario(chave) {
  // _diarios só existe depois que a indexação termina (state.pronto).
  const diario = state.data && state.data._diarios ? state.data._diarios.get(chave) : null;
  if (!diario) return [];
  return diario.atos.map((indice) => state.data.acts[indice]);
}

function abrirLeitor(chave, atoId, modo) {
  const grupo = grupoPorChave(chave);
  let lista;
  if (modo === 'busca' && grupo) {
    lista = grupo.atos.map((item) => item.ato);
  } else {
    modo = 'diario';
    lista = atosDoDiario(chave);
  }
  if (!lista.length) return;

  let indice = atoId ? lista.findIndex((ato) => ato.id === atoId) : 0;
  if (indice < 0) {
    // O ato pedido não está na lista filtrada: cai para a lista completa.
    modo = 'diario';
    lista = atosDoDiario(chave);
    indice = Math.max(0, lista.findIndex((ato) => ato.id === atoId));
  }

  const jaAberto = state.leitor.aberto;
  state.leitor = { aberto: true, modo, chave, lista, indice, marcas: [], marcaAtual: -1 };
  $('#leitor').hidden = false;
  document.body.classList.add('leitor-aberto');
  if (!jaAberto) {
    try { history.pushState({ leitor: true }, ''); } catch (_) { /* file:// antigo */ }
    $('#leitorVoltar').focus();
  }
  renderLeitor();
}

function fecharLeitor(viaHistorico) {
  if (!state.leitor.aberto) return;
  state.leitor.aberto = false;
  state.leitor.marcas = [];
  $('#leitor').hidden = true;
  $('#leitorDrawer').hidden = true;
  document.body.classList.remove('leitor-aberto');
  if (!viaHistorico && history.state && history.state.leitor) {
    try { history.back(); } catch (_) { /* sem histórico */ }
  }
  const input = $('#searchInput');
  if (input && !('ontouchstart' in window)) input.focus();
}

function renderLeitor() {
  const leitor = state.leitor;
  const ato = leitor.lista[leitor.indice];
  if (!ato) return;

  $('#leitorMeta').textContent = `${ato.data} · ${rotuloEdicao(ato.edicao)} · ${ato.tipo || 'Ato'}`;
  $('#leitorEdicao').textContent = `${dataExtensa(ato.data)} — ${rotuloEdicao(ato.edicao)}`;
  $('#leitorTitulo').textContent = ato.identificacao || '(sem identificação)';

  const assunto = assuntoDoAto(ato);
  $('#leitorAssunto').textContent = assunto;
  $('#leitorAssunto').hidden = !assunto;

  const pdf = $('#leitorPdf');
  pdf.href = pdfHref(ato);
  pdf.textContent = ato.pdfPage ? `Abrir PDF (pág. ${ato.pdfPage})` : 'Abrir PDF';
  pdf.hidden = !ato.pdfPath && !ato.pdfUrl;

  $('#atoPosicao').textContent = `Ato ${leitor.indice + 1} de ${leitor.lista.length}${leitor.modo === 'busca' ? ' encontrados' : ''}`;
  $('#atoAnterior').disabled = leitor.indice <= 0;
  $('#atoProximo').disabled = leitor.indice >= leitor.lista.length - 1;

  const corpo = $('#leitorCorpo');
  corpo.innerHTML = ato.textoHtml || '<p>-</p>';
  cleanupLeadingNoise(corpo, ato);

  $('#leitorRolagem').scrollTop = 0;
  renderDrawer();

  const tokens = tokensAtivos();
  leitor.marcas = [];
  leitor.marcaAtual = -1;
  if (tokens.length) {
    // setTimeout em vez de requestAnimationFrame: rAF nao dispara em abas
    // sem pintura (segundo plano/webview), deixando o texto sem destaque.
    setTimeout(() => {
      if (!leitor.aberto || leitor.lista[leitor.indice] !== ato) return;
      destacarNoDom(corpo, tokens);
      leitor.marcas = $$('mark.ocorrencia', corpo);
      atualizarContadorMarcas();
      if (leitor.marcas.length) navegarMarca(0, true);
    }, 0);
  } else {
    atualizarContadorMarcas();
  }
}

function destacarNoDom(root, tokens) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);

  for (const node of nodes) {
    const texto = node.nodeValue || '';
    if (!texto.trim()) continue;
    const intervalos = window.diarioBusca.intervalosDeTexto(texto, tokens);
    if (!intervalos.length) continue;

    const fragment = document.createDocumentFragment();
    let cursor = 0;
    for (const [inicio, fim] of intervalos) {
      if (inicio > cursor) fragment.append(document.createTextNode(texto.slice(cursor, inicio)));
      const mark = document.createElement('mark');
      mark.className = 'ocorrencia';
      mark.textContent = texto.slice(inicio, fim);
      fragment.append(mark);
      cursor = fim;
    }
    if (cursor < texto.length) fragment.append(document.createTextNode(texto.slice(cursor)));
    node.replaceWith(fragment);
  }
}

function atualizarContadorMarcas() {
  const leitor = state.leitor;
  const caixa = $('#leitorOcorrencias');
  const temBusca = tokensAtivos().length > 0;
  caixa.hidden = !temBusca;
  if (!temBusca) return;
  $('#marcaContador').textContent = leitor.marcas.length
    ? `${leitor.marcaAtual + 1} / ${leitor.marcas.length}`
    : '0 / 0';
  $('#marcaAnterior').disabled = !leitor.marcas.length;
  $('#marcaProxima').disabled = !leitor.marcas.length;
}

function navegarMarca(delta, absoluto) {
  const leitor = state.leitor;
  if (!leitor.marcas.length) return;
  const atual = leitor.marcas[leitor.marcaAtual];
  if (atual) atual.classList.remove('ocorrencia-atual');
  leitor.marcaAtual = absoluto
    ? delta
    : (leitor.marcaAtual + delta + leitor.marcas.length) % leitor.marcas.length;
  const marca = leitor.marcas[leitor.marcaAtual];
  marca.classList.add('ocorrencia-atual');
  marca.scrollIntoView({ block: 'center' });
  atualizarContadorMarcas();
}

function navegarAto(delta) {
  const leitor = state.leitor;
  const novo = leitor.indice + delta;
  if (novo < 0 || novo >= leitor.lista.length) return;
  leitor.indice = novo;
  renderLeitor();
}

function renderDrawer() {
  const leitor = state.leitor;
  const todos = atosDoDiario(leitor.chave);
  const grupo = grupoPorChave(leitor.chave);
  const encontrados = new Set(grupo ? grupo.atos.map((item) => item.ato.id) : []);
  const atual = leitor.lista[leitor.indice];

  $('#drawerLista').innerHTML = todos.map((ato) => `
    <li>
      <button type="button" data-drawer-ato="${escapeHtml(ato.id)}"
              class="${ato.id === atual.id ? 'atual' : ''}${encontrados.has(ato.id) ? ' encontrado' : ''}">
        <span class="drawer-tipo">${escapeHtml(ato.tipo || 'Ato')}</span>
        <span class="drawer-titulo">${escapeHtml(ato.identificacao || '(sem identificação)')}</span>
        ${encontrados.has(ato.id) ? '<span class="drawer-badge">contém o termo</span>' : ''}
      </button>
    </li>
  `).join('');
}

// ---------------------------------------------------------------------
// Atualização dos dados
// ---------------------------------------------------------------------

function latestDiaryDateText() {
  const dates = (state.data && state.data.diaries ? state.data.diaries : [])
    .map((diary) => diary.data)
    .filter(Boolean)
    .sort((a, b) => window.diarioBusca.parseDataBr(a) - window.diarioBusca.parseDataBr(b));
  return dates[dates.length - 1] || '';
}

async function refreshDataLocal() {
  const button = $('#refreshData');
  const status = $('#updateStatus');
  if (!window.plataforma.podeAtualizar) {
    status.textContent = 'Atualização disponível apenas no app de PC.';
    return;
  }

  button.disabled = true;
  button.classList.add('atualizando');
  $('span', button).textContent = 'Atualizando…';
  const latest = latestDiaryDateText();
  status.textContent = latest
    ? `Buscando diários publicados após ${latest}…`
    : 'Buscando novos diários…';
  try {
    const result = await window.plataforma.atualizarDados();
    if (!result || !result.ok) {
      throw new Error((result && result.message) || 'Não foi possível atualizar.');
    }
    const changedYears = Array.isArray(result.changedYears) ? result.changedYears : null;
    if (changedYears && changedYears.length && state.data && Array.isArray(state.data.acts)) {
      const data = await window.plataforma.carregarDados({ cacheBust: true });
      await aplicarAnosAtualizados(data, changedYears);
    } else if (changedYears && !changedYears.length && result.manifestChanged) {
      const data = await window.plataforma.carregarDados({ cacheBust: true });
      Object.assign(state.data, data);
      atualizarStatusDados(state.data);
    } else if (!changedYears) {
      const data = await window.plataforma.carregarDados({ cacheBust: true });
      await aplicarDados(data, { cacheBust: true });
    }
    status.textContent = result.message || 'Dados atualizados.';
    onBusca();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
    button.classList.remove('atualizando');
    $('span', button).textContent = 'Atualizar';
  }
}

// ---------------------------------------------------------------------
// Inicialização
// ---------------------------------------------------------------------

function refazerBuscaAtiva() {
  if ($('#searchInput').value.trim()) onBusca();
}

async function aplicarDados(data, opts) {
  const cacheBust = !!(opts && opts.cacheBust);
  state.pronto = false;
  state.carregandoAnos = false;
  state.resultado = null;

  // Limpa a tela antiga: os itens renderizados apontam para dados que ainda
  // não foram indexados (_diarios só existe ao fim da indexação).
  $('#resultados').innerHTML = '';
  $('#resumoBusca').hidden = true;
  $('#mostrarMais').hidden = true;
  $('#estadoVazio').hidden = true;

  const input = $('#searchInput');
  input.disabled = true;
  const hint = $('#searchHint');

  // Dados v2: manifesto { years: [{ano, arquivo, …}] } + um arquivo por ano.
  // Dados v1 (formato antigo): objeto único com acts embutido.
  const ehV2 = Array.isArray(data.years) && !Array.isArray(data.acts);
  state.data = ehV2 ? Object.assign({}, data, { acts: [], people: [] }) : data;

  $('#dataStatus').textContent =
    `${plural(state.data.totals.diarios, 'diário', 'diários')} · ${plural(state.data.totals.atos, 'ato', 'atos')}` +
    ` · atualizado em ${new Date(state.data.generatedAt).toLocaleString('pt-BR')}`;

  if (!ehV2) {
    await window.diarioBusca.indexar(state.data, (feitos, total) => {
      hint.textContent = `Preparando a busca… ${Math.round((feitos / total) * 100)}%`;
    });
    state.pronto = true;
    input.disabled = false;
    renderEstadoVazio();
    renderResultados();
    return;
  }

  // Carrega do ano mais recente para o mais antigo; a busca é liberada
  // assim que o primeiro ano fica pronto e vai crescendo em segundo plano.
  const anos = data.years.slice().sort((a, b) => Number(b.ano) - Number(a.ano));
  state.carregandoAnos = true;
  const falhas = [];
  for (let i = 0; i < anos.length; i++) {
    const ano = anos[i];
    hint.textContent = `Preparando ${ano.ano}… (${i + 1} de ${anos.length} anos)`;
    try {
      const url = `dados/${ano.arquivo}${cacheBust ? `?t=${Date.now()}` : ''}`;
      const resposta = await fetch(url, { cache: 'no-store' });
      if (!resposta.ok) throw new Error(`HTTP ${resposta.status}`);
      const shard = await resposta.json();
      state.data.acts.push(...(shard.acts || []));
    } catch (_) {
      falhas.push(ano.ano);
      continue;
    }
    await window.diarioBusca.indexar(state.data, null);
    if (!state.pronto) {
      state.pronto = true;
      input.disabled = false;
      renderEstadoVazio();
      renderResultados();
      if (!('ontouchstart' in window)) input.focus();
    }
    refazerBuscaAtiva();
  }
  state.carregandoAnos = false;
  state.pronto = true;
  input.disabled = false;
  renderEstadoVazio();
  if ($('#searchInput').value.trim()) onBusca();
  else renderResultados();
  if (falhas.length) {
    $('#updateStatus').textContent = `Aviso: não carregou ${falhas.join(', ')}.`;
  }
}

function atualizarStatusDados(data) {
  $('#dataStatus').textContent =
    `${plural(data.totals.diarios, 'di\u00e1rio', 'di\u00e1rios')} \u00b7 ${plural(data.totals.atos, 'ato', 'atos')}` +
    ` \u00b7 atualizado em ${new Date(data.generatedAt).toLocaleString('pt-BR')}`;
}

async function aplicarAnosAtualizados(manifest, changedYears) {
  const anosAlterados = new Set(changedYears.map((ano) => String(ano)));
  const antigos = state.data.acts.filter((act) =>
    !anosAlterados.has(String(act.anoMes || '').slice(0, 4))
  );
  const novos = [];
  const anos = (manifest.years || []).filter((ano) =>
    anosAlterados.has(String(ano.ano))
  );

  state.data = Object.assign(state.data, manifest, { acts: antigos });
  state.pronto = false;
  $('#searchInput').disabled = true;
  for (const ano of anos) {
    const url = `dados/${ano.arquivo}?t=${Date.now()}`;
    const resposta = await fetch(url, { cache: 'no-store' });
    if (!resposta.ok) throw new Error(`HTTP ${resposta.status}`);
    const shard = await resposta.json();
    novos.push(...(shard.acts || []));
  }
  state.data.acts.push(...novos);
  await window.diarioBusca.indexar(state.data, null);
  state.pronto = true;
  state.carregandoAnos = false;
  $('#searchInput').disabled = false;
  atualizarStatusDados(state.data);
  state.resultado = null;
  renderEstadoVazio();
  renderResultados();
}

function wireEvents() {
  const input = $('#searchInput');
  const buscaDebounced = debounce(onBusca, 250);
  input.addEventListener('input', buscaDebounced);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') buscaDebounced.flush();
  });

  $('#limparBusca').addEventListener('click', () => {
    input.value = '';
    input.focus();
    onBusca();
  });

  $('#mostrarMais').addEventListener('click', () => {
    state.diariosVisiveis += DIARIOS_POR_PAGINA;
    renderResultados();
  });

  document.addEventListener('click', (event) => {
    // Durante a (re)indexação nada é clicável com segurança; deixa os links
    // <a> seguirem o href normal e ignora os botões.
    if (!state.pronto) return;

    const pdfLink = event.target.closest('a.pdf-link, #leitorPdf');
    if (pdfLink) {
      let alvo = null;
      if (pdfLink.id === 'leitorPdf') {
        alvo = state.leitor.lista[state.leitor.indice];
      } else if (pdfLink.dataset.pdfChave) {
        const diario = state.data._diarios.get(pdfLink.dataset.pdfChave);
        alvo = (diario && (diario.diary || state.data.acts[diario.atos[0]])) || null;
      }
      if (!alvo) return; // sem alvo resolvível: deixa o <a> seguir o href
      event.preventDefault();
      openPdf(alvo);
      return;
    }

    const linhaAto = event.target.closest('[data-abrir-ato]');
    if (linhaAto) {
      abrirLeitor(linhaAto.dataset.chave, linhaAto.dataset.abrirAto, 'busca');
      return;
    }

    const expandir = event.target.closest('[data-expandir]');
    if (expandir) {
      state.expandidos.add(expandir.dataset.expandir);
      renderResultados();
      return;
    }

    const verDiario = event.target.closest('[data-ver-diario]');
    if (verDiario) {
      abrirLeitor(verDiario.dataset.verDiario, null, 'diario');
      return;
    }

    const drawerAto = event.target.closest('[data-drawer-ato]');
    if (drawerAto) {
      $('#leitorDrawer').hidden = true;
      abrirLeitor(state.leitor.chave, drawerAto.dataset.drawerAto, 'diario');
    }
  });

  $('#leitorVoltar').addEventListener('click', () => fecharLeitor(false));
  $('#atoAnterior').addEventListener('click', () => navegarAto(-1));
  $('#atoProximo').addEventListener('click', () => navegarAto(1));
  $('#marcaAnterior').addEventListener('click', () => navegarMarca(-1));
  $('#marcaProxima').addEventListener('click', () => navegarMarca(1));
  $('#leitorLista').addEventListener('click', () => {
    const drawer = $('#leitorDrawer');
    drawer.hidden = !drawer.hidden;
  });
  $('#drawerFechar').addEventListener('click', () => {
    $('#leitorDrawer').hidden = true;
  });

  document.addEventListener('keydown', (event) => {
    if (!state.leitor.aberto) return;
    if (event.target instanceof Element && event.target.matches('input, textarea')) return;
    if (event.key === 'Escape') {
      const drawer = $('#leitorDrawer');
      if (!drawer.hidden) drawer.hidden = true;
      else fecharLeitor(false);
    }
    if (event.key === 'ArrowLeft') navegarAto(-1);
    if (event.key === 'ArrowRight') navegarAto(1);
  });

  window.addEventListener('popstate', (event) => {
    if (state.leitor.aberto) {
      fecharLeitor(true);
      return;
    }
    // Avançar do navegador reentra no estado {leitor:true} com o leitor já
    // fechado: devolve para trás para não deixar um "voltar" morto.
    if (event.state && event.state.leitor) {
      try { history.back(); } catch (_) { /* sem histórico */ }
    }
  });

}

async function configurarAtualizacaoLocal() {
  const button = $('#refreshData');
  if (!window.plataforma.podeAtualizar) {
    button.hidden = true;
    return;
  }
  if (window.plataforma.ambiente === 'browser') {
    // Servidor estático simples (ex.: python -m http.server) não sabe atualizar.
    try {
      const response = await fetch('/api/capabilities');
      const caps = await response.json();
      if (!caps || !caps.update) button.hidden = true;
    } catch (_) {
      button.hidden = true;
    }
  }
}

async function init() {
  try {
    wireEvents();
    const data = await window.plataforma.carregarDados();
    await aplicarDados(data);
    if (!('ontouchstart' in window)) $('#searchInput').focus();
  } catch (error) {
    $('#dataStatus').textContent = error.message;
    $('#searchHint').textContent = '';
    $('#resultados').innerHTML =
      `<div class="erro-carga"><strong>Não foi possível carregar os dados.</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}

init();
