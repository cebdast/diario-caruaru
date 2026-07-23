(function (global) {
  'use strict';

  const DADOS_URL = 'dados/diario-caruaru.json';
  const isElectron = !!(global.api && typeof global.api.abrirPdf === 'function');
  const isLocalHelper = /^https?:$/.test(global.location.protocol)
    && ['127.0.0.1', 'localhost'].includes(global.location.hostname);

  async function carregarDados(options = {}) {
    const url = options.cacheBust ? `${DADOS_URL}?t=${Date.now()}` : DADOS_URL;
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) {
      throw new Error('Nao foi possivel carregar os dados do Diario.');
    }
    return response.json();
  }

  function escolherPdf(target) {
    if (!target) return '';
    if (typeof target === 'object') {
      if (target.pdfOpenUrl) return target.pdfOpenUrl;
      if (target.pdfPage && target.pdfUrl) return appendPage(target.pdfUrl, target.pdfPage);
      return isElectron ? (target.pdfPath || target.pdfUrl || '') : (target.pdfUrl || target.pdfPath || '');
    }
    return target;
  }

  function appendPage(url, page) {
    if (!url || !page) return url || '';
    return `${String(url).split('#')[0]}#page=${Number(page)}`;
  }

  function pdfHref(itemOuCaminho) {
    const target = escolherPdf(itemOuCaminho);
    if (!target) return '#';
    if (isLocalHelper && typeof itemOuCaminho === 'object' && itemOuCaminho.pdfPath) {
      return appendPage(`/api/pdf?path=${encodeURIComponent(itemOuCaminho.pdfPath)}`, itemOuCaminho.pdfPage);
    }
    if (isLocalHelper && /^[A-Za-z]:[\\/]/.test(target)) {
      return `/api/pdf?path=${encodeURIComponent(target)}`;
    }
    return target;
  }

  async function abrirPdf(itemOuCaminho) {
    const target = escolherPdf(itemOuCaminho);
    if (!target) return;
    if (!isLocalHelper && global.api && typeof global.api.abrirPdf === 'function') {
      await global.api.abrirPdf(target);
      return;
    }
    const href = pdfHref(itemOuCaminho);
    if (href && href !== '#') {
      global.open(href, '_blank', 'noopener');
      return { ok: true, url: href };
    }
    if (global.api && typeof global.api.abrirPdf === 'function') {
      await global.api.abrirPdf(target);
      return;
    }
    if (isLocalHelper) {
      const response = await fetch('/api/open-pdf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(typeof itemOuCaminho === 'object' ? itemOuCaminho : { target }),
      });
      const result = await response.json();
      if (result && result.ok && result.url) {
        global.open(result.url, '_blank', 'noopener');
        return;
      }
      if (result && result.ok) return;
      throw new Error((result && result.message) || 'Nao foi possivel abrir o PDF.');
    }
    const link = document.createElement('a');
    link.href = target;
    link.target = '_blank';
    link.rel = 'noopener';
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  async function atualizarDados() {
    if (global.api && typeof global.api.atualizarDados === 'function') {
      return global.api.atualizarDados();
    }
    if (isLocalHelper) {
      const response = await fetch("/api/update", { method: 'POST' });
      return response.json();
    }
    return { ok: false, message: 'A atualizacao local esta disponivel apenas no app de PC.' };
  }

  async function solicitarAtualizacao(chave) {
    const response = await fetch('/api/atualizar', {
      method: 'POST',
      headers: { 'X-Update-Key': chave },
      cache: 'no-store',
    });
    let result = {};
    try { result = await response.json(); } catch (_) { /* resposta sem JSON */ }
    if (!response.ok) {
      throw new Error(result.message || 'Nao foi possivel solicitar a atualizacao.');
    }
    return result;
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.body.classList.add(isElectron ? 'electron' : 'mobile');
  });

  global.plataforma = {
    carregarDados,
    abrirPdf,
    atualizarDados,
    solicitarAtualizacao,
    pdfHref,
    podeAtualizar: (isElectron && !!(global.api && typeof global.api.atualizarDados === 'function')) || isLocalHelper,
    ambiente: isElectron ? 'electron' : 'browser',
  };
})(window);
