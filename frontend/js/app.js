/**
 * Orquestração da aplicação.
 *
 * Fluxo: URL -> extracção -> escolha de vídeo/áudio/legendas -> job de
 * download -> progresso em tempo real (WebSocket) -> ficheiro final.
 */
import { ApiClient, ApiError } from './api.js';
import { ProgressSocket } from './ws.js';
import {
  $, $$, clearLog, describeAudioFormat, describeSubtitleTrack, describeVideoFormat,
  detectPlatform, formatBitrate, formatBytes, formatDate, formatDuration, formatOptionDetail,
  formatOptionTitle, formatTag, hideResult, log, renderOptions, renderPlatformBadge,
  renderSessionHistory, renderSummary, setProgress, showResult, toast,
} from './ui.js';

// --------------------------------------------------------------------------
// Estado
// --------------------------------------------------------------------------
const state = {
  client: ApiClient.fromStorage(),
  media: null,
  formats: [],
  videoFormats: [],
  audioFormats: [],
  subtitleTracks: [],
  /** Eixo de vídeo: 'auto' ou o id de um formato. */
  videoChoice: 'auto',
  /** Eixo de áudio: 'auto' ou o id de um formato. */
  audioChoice: 'auto',
  /** Eixo de legendas: 'none' | 'auto' ou o id de uma faixa. */
  subtitleChoice: 'none',
  jobId: null,
  socket: null,
  audioFormat: localStorage.getItem('mad.audioFormat') || 'mp3',
  cookiesFromBrowser: localStorage.getItem('mad.cookiesFromBrowser') || '',
  history: [],
};

/**
 * Descrição de cada modo de download.
 * O modo decide *como* os eixos são combinados; as escolhas explícitas do
 * utilizador mantêm-se quando o modo muda.
 */
const MODE_INFO = {
  auto: {
    label: 'Automático',
    desc: 'O programa escolhe a melhor qualidade disponível em cada eixo — vídeo, áudio e legendas.',
  },
  video: {
    label: 'Vídeo',
    desc: 'Um único ficheiro de vídeo. Se o stream escolhido não trouxer som, o melhor áudio é anexado automaticamente.',
  },
  both: {
    label: 'Vídeo + áudio',
    desc: 'Vídeo e áudio escolhidos à parte e fundidos num único ficheiro final.',
  },
  audio: {
    label: 'Só áudio',
    desc: 'Apenas a faixa de áudio, convertida para o formato definido nas Definições.',
  },
};

/**
 * Ordem de preferência de idioma para as legendas automáticas.
 * Espelha `DownloadService._best_subtitle` no backend.
 */
const SUBTITLE_LANGUAGE_RANK = ['pt', 'pt-pt', 'pt-br', 'en', 'en-us', 'en-gb', 'es', 'fr', 'de', 'it'];

// --------------------------------------------------------------------------
// Arranque
// --------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', init);

function init() {
  $('#inp-api-base').value = state.client.apiBase;
  $('#inp-api-key').value = state.client.apiKey;
  $('#inp-audio-format').value = state.audioFormat;
  $('#inp-cookie-browser').value = state.cookiesFromBrowser;

  $('#form-extract').addEventListener('submit', onExtract);
  $('#btn-download').addEventListener('click', onDownload);
  $('#btn-cancel').addEventListener('click', onCancel);
  $('#btn-clear-log').addEventListener('click', clearLog);
  $('#btn-settings').addEventListener('click', toggleSettings);
  $('#btn-close-settings').addEventListener('click', toggleSettings);
  $('#btn-save-settings').addEventListener('click', saveSettings);

  // Gestor de cookies (cookies.txt / Get cookies.txt LOCALLY)
  checkCookiesStatus();
  const btnBrowseCookies = $('#btn-browse-cookies');
  const inpCookieFile = $('#inp-cookie-file');
  if (btnBrowseCookies && inpCookieFile) {
    btnBrowseCookies.addEventListener('click', () => inpCookieFile.click());
    inpCookieFile.addEventListener('change', () => {
      if (inpCookieFile.files && inpCookieFile.files[0]) {
        uploadCookiesFile(inpCookieFile.files[0]);
      }
    });
  }
  const btnTogglePaste = $('#btn-toggle-paste-cookies');
  if (btnTogglePaste) {
    btnTogglePaste.addEventListener('click', () => {
      $('#cookies-paste-box').classList.toggle('hidden');
    });
  }
  const btnSaveCookieText = $('#btn-save-cookie-text');
  if (btnSaveCookieText) {
    btnSaveCookieText.addEventListener('click', saveCookiesText);
  }
  const btnCancelCookieText = $('#btn-cancel-cookie-text');
  if (btnCancelCookieText) {
    btnCancelCookieText.addEventListener('click', () => {
      $('#cookies-paste-box').classList.add('hidden');
    });
  }
  const btnDeleteCookies = $('#btn-delete-cookies');
  if (btnDeleteCookies) {
    btnDeleteCookies.addEventListener('click', deleteCookies);
  }

  // Motor de atualizações automáticas (SyncBot)
  initEngineUpdater();

  // Detecção em tempo real e botão de colar
  const urlInput = $('#inp-url');
  const badgeEl = $('#platform-badge');
  urlInput.addEventListener('input', () => {
    renderPlatformBadge(badgeEl, urlInput.value);
  });

  const btnPaste = $('#btn-paste');
  if (btnPaste) {
    btnPaste.addEventListener('click', async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          urlInput.value = text.trim();
          renderPlatformBadge(badgeEl, text.trim());
          toast('URL colado!', 'ok');
        }
      } catch {
        urlInput.focus();
        toast('Pressione Ctrl+V no campo para colar.', 'warn');
      }
    });
  }

  // Chips de plataformas
  $$('.chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const site = chip.dataset.site;
      if (site === 'all') {
        toast('Catálogo nativo completo: mais de 1.750 sites integrados!', 'ok');
        return;
      }
      urlInput.focus();
      toast(`Plataforma selecionada: ${chip.textContent}. Cole o link para descarregar.`, 'info');
    });
  });

  // Presets rápidos de qualidade
  $('#btn-preset-max')?.addEventListener('click', () => applyPreset('max'));
  $('#btn-preset-1080')?.addEventListener('click', () => applyPreset(1080));
  $('#btn-preset-720')?.addEventListener('click', () => applyPreset(720));
  $('#btn-preset-audio')?.addEventListener('click', () => applyPreset('audio'));

  // Ações de resultado e histórico
  $('#btn-copy-link')?.addEventListener('click', copyDownloadLink);
  $('#btn-new-download')?.addEventListener('click', resetForNewDownload);
  $('#btn-history-toggle')?.addEventListener('click', toggleHistory);
  $('#btn-clear-history')?.addEventListener('click', clearHistory);

  $$('input[name="mode"]').forEach((input) => {
    input.addEventListener('change', onModeChange);
  });

  log('Painel pronto. Cole um URL e clique em Analisar.', 'info');
  checkHealth();
}

async function checkHealth() {
  const pill = $('#api-status');
  try {
    const health = await state.client.health();
    const count = health.supported_sites_count || '1.750+';
    pill.textContent = `Ligado · ${count} sites suportados`;
    pill.className = 'pill pill-ok';
    log(`Servidor OK — Motor pronto (${count} plataformas, ffmpeg: ${health.ffmpeg})`, 'ok');
  } catch (err) {
    pill.textContent = 'Sem ligação';
    pill.className = 'pill pill-err';
    log(`Servidor inacessível: ${err.message}`, 'err');
  }
}

// --------------------------------------------------------------------------
// Definições
// --------------------------------------------------------------------------
function toggleSettings() {
  const panel = $('#settings-panel');
  const hidden = panel.classList.toggle('hidden');
  panel.setAttribute('aria-hidden', String(hidden));
  if (!hidden) {
    checkCookiesStatus();
    loadEngineStatus(false);
  }
}

// --------------------------------------------------------------------------
// Gestor de Cookies (cookies.txt / Get cookies.txt LOCALLY / JSON)
// --------------------------------------------------------------------------
async function checkCookiesStatus() {
  const badge = $('#cookies-status-badge');
  const label = $('#cookies-status-label');
  const delBtn = $('#btn-delete-cookies');
  const domainsList = $('#cookies-domains-list');
  if (!badge) return;

  try {
    const res = await fetch(`${state.client.apiBase}/cookies`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.present && data.count > 0) {
      badge.className = 'cookies-badge active';
      label.textContent = `${data.count} cookies ativos`;
      if (delBtn) delBtn.classList.remove('hidden');
      if (domainsList) {
        if (data.domains && data.domains.length > 0) {
          domainsList.classList.remove('hidden');
          domainsList.innerHTML =
            data.domains
              .slice(0, 8)
              .map((d) => `<span class="cookie-domain-pill">${d}</span>`)
              .join('') +
            (data.domains.length > 8 ? `<span class="cookies-filename">+${data.domains.length - 8} outros</span>` : '');
        } else {
          domainsList.classList.add('hidden');
        }
      }
    } else {
      badge.className = 'cookies-badge';
      label.textContent = 'Sem cookies carregados';
      if (delBtn) delBtn.classList.add('hidden');
      if (domainsList) domainsList.classList.add('hidden');
    }
  } catch {
    badge.className = 'cookies-badge';
    label.textContent = 'Offline';
  }
}

async function uploadCookiesFile(file) {
  if (!file) return;
  const fileNameEl = $('#cookies-file-name');
  if (fileNameEl) fileNameEl.textContent = `A carregar ${file.name}...`;

  try {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch(`${state.client.apiBase}/cookies/upload`, {
      method: 'POST',
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Falha ao processar ficheiro');
    toast(`Sucesso: ${data.message}`, 'ok');
    log(`🍪 ${data.message}`, 'ok');
    if (fileNameEl) fileNameEl.textContent = `✓ ${file.name}`;
    await checkCookiesStatus();
  } catch (err) {
    toast(`Erro nos cookies: ${err.message}`, 'err');
    log(`Falha ao carregar cookies: ${err.message}`, 'err');
    if (fileNameEl) fileNameEl.textContent = '';
  }
}

async function saveCookiesText() {
  const textarea = $('#txt-cookie-content');
  const text = textarea ? textarea.value.trim() : '';
  if (!text) {
    toast('Por favor, cola o conteúdo do ficheiro cookies.txt.', 'warn');
    return;
  }

  try {
    const res = await fetch(`${state.client.apiBase}/cookies/upload`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: text }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Falha ao processar cookies');
    toast(`Sucesso: ${data.message}`, 'ok');
    log(`🍪 ${data.message}`, 'ok');
    textarea.value = '';
    $('#cookies-paste-box').classList.add('hidden');
    await checkCookiesStatus();
  } catch (err) {
    toast(`Erro nos cookies: ${err.message}`, 'err');
    log(`Falha ao guardar cookies: ${err.message}`, 'err');
  }
}

async function deleteCookies() {
  if (!confirm('Desejas remover o ficheiro cookies.txt guardado no servidor?')) return;
  try {
    const res = await fetch(`${state.client.apiBase}/cookies`, { method: 'DELETE' });
    const data = await res.json();
    toast(data.message || 'Cookies removidos.', 'ok');
    log('🍪 Ficheiro cookies.txt removido.', 'info');
    const fileNameEl = $('#cookies-file-name');
    if (fileNameEl) fileNameEl.textContent = '';
    await checkCookiesStatus();
  } catch (err) {
    toast(`Falha ao remover cookies: ${err.message}`, 'err');
  }
}

function saveSettings() {
  state.client.apiBase = $('#inp-api-base').value.trim().replace(/\/+$/, '') || '/api';
  state.client.apiKey = $('#inp-api-key').value.trim();
  state.audioFormat = $('#inp-audio-format').value;
  state.cookiesFromBrowser = $('#inp-cookie-browser').value;
  localStorage.setItem('mad.audioFormat', state.audioFormat);
  localStorage.setItem('mad.cookiesFromBrowser', state.cookiesFromBrowser);
  state.client.save();
  toggleSettings();
  log('Definições guardadas.', 'ok');
  checkHealth();
  // O formato de áudio de saída mudou: o resumo tem de mudar com ele.
  if (!$('#formats-card').classList.contains('hidden')) renderSelection();
}

// --------------------------------------------------------------------------
// Passo 1 — extracção
// --------------------------------------------------------------------------
async function onExtract(event) {
  event.preventDefault();
  const url = $('#inp-url').value.trim();
  if (!url) return;

  const button = $('#btn-extract');
  button.disabled = true;
  button.classList.add('loading');
  $('#formats-card').classList.add('hidden');
  hideResult();
  log(`A extrair: ${url}`, 'info');

  try {
    const media = await state.client.extractInfo(url, {
      cookies_from_browser: state.cookiesFromBrowser || null,
    });
    state.media = media;
    state.formats = (media.formats || []).filter((f) => !f.unavailable);
    state.videoFormats = state.formats.filter((f) => f.media_type === 'video');
    state.audioFormats = state.formats.filter((f) => f.media_type === 'audio');
    state.subtitleTracks = flattenSubtitleTracks(media.subtitles || {});

    if (!state.formats.length) {
      throw new ApiError('Nenhum formato disponível para este URL.', { code: 'no_formats' });
    }

    // Cada extracção recomeça com os três eixos em "Automático"/"Sem legendas".
    state.videoChoice = 'auto';
    state.audioChoice = 'auto';
    state.subtitleChoice = 'none';

    renderMedia(media);
    renderSelection();
    $('#formats-card').classList.remove('hidden');
    $('#progress-card').classList.add('hidden');
    log(
      `Encontrados ${state.formats.length} formato(s) via "${media.extractor}"` +
      (media.duration ? ` · duração ${formatDuration(media.duration)}` : ''),
      'ok',
    );
  } catch (err) {
    log(`Falha na extracção: ${err.message}`, 'err');
    toast(err.message, 'err');
  } finally {
    button.disabled = false;
    button.classList.remove('loading');
  }
}

function renderMedia(media) {
  const title = media.title || 'Sem título';
  $('#media-title').textContent = title;

  const bits = [];
  if (media.duration) bits.push(formatDuration(media.duration));
  if (media.uploader) bits.push(media.uploader);
  if (media.upload_date) bits.push(formatDate(media.upload_date));
  if (media.is_live) bits.push('AO VIVO');
  bits.push(`${state.formats.length} formatos`);
  $('#media-subtitle').textContent = bits.join(' · ');
  $('#media-meta').textContent = media.extractor || '—';

  const platformBadge = $('#media-platform-badge');
  if (platformBadge) {
    const p = detectPlatform(media.webpage_url || media.original_url || media.extractor);
    platformBadge.textContent = media.extractor ? media.extractor.toUpperCase() : (p?.name || 'VÍDEO');
    if (p) {
      platformBadge.style.color = p.color;
      platformBadge.style.backgroundColor = p.bg;
      platformBadge.style.border = `1px solid ${p.color}44`;
    }
  }

  const thumb = $('#media-thumb');
  if (media.thumbnail) {
    thumb.src = media.thumbnail;
    thumb.parentElement.classList.remove('hidden');
  } else {
    thumb.removeAttribute('src');
    thumb.parentElement.classList.add('hidden');
  }
}

// --------------------------------------------------------------------------
// Passo 2 — modo, eixos de escolha e resumo
// --------------------------------------------------------------------------
function currentMode() {
  return document.querySelector('input[name="mode"]:checked')?.value ?? 'auto';
}

function onModeChange() {
  // As escolhas explícitas mantêm-se: o modo só muda a combinação.
  renderSelection();
}

/**
 * Converte o mapa de legendas do backend numa lista plana.
 * O id é exactamente `SubtitleTrack.track_id()`: `${grupo}:${idioma}:${nome}`.
 */
function flattenSubtitleTracks(subtitles) {
  const list = [];
  for (const [group, tracks] of Object.entries(subtitles || {})) {
    if (!Array.isArray(tracks)) continue;
    for (const track of tracks) {
      // O backend só descarrega faixas com URL — as restantes não podem
      // ser escolhidas pelo utilizador.
      if (!track.url) continue;
      const language = track.language || group || 'und';
      const name = track.name || 'default';
      list.push({
        id: `${group}:${language}:${name}`,
        group,
        language,
        name: track.name || language.toUpperCase(),
        url: track.url,
        automatic: Boolean(track.automatic),
      });
    }
  }
  return list;
}

// --------------------------------------------------------------------------
// Regras de selecção automática
// Espelham exactamente o backend (`app/services/pipeline.py`).
// --------------------------------------------------------------------------

/** Compara duas tuplas (ordem lexicográfica). */
function compareTuples(a, b) {
  const length = Math.max(a.length, b.length);
  for (let i = 0; i < length; i += 1) {
    const x = a[i] ?? 0;
    const y = b[i] ?? 0;
    if (x > y) return 1;
    if (x < y) return -1;
  }
  return 0;
}

/**
 * Equivalente a `max()` do Python: devolve o *primeiro* elemento com a
 * pontuação máxima (os empates mantêm a ordem da API).
 */
function pickMax(list, score) {
  let best = null;
  let bestScore = null;
  for (const item of list) {
    const current = score(item);
    if (best === null || compareTuples(current, bestScore) > 0) {
      best = item;
      bestScore = current;
    }
  }
  return best;
}

/** Um stream traz som quando tem codec de áudio. */
function hasAudio(fmt) {
  return Boolean(fmt?.acodec) && fmt.acodec !== 'none';
}

/**
 * Melhor formato de vídeo.
 * - modo "video": prefere um stream já com áudio (mais simples, sem merge);
 * - modo "auto"/"both": prefere um stream *só de vídeo*, para poder anexar
 *   a melhor faixa de áudio.
 * Desempate: (height, tbr, filesize).
 */
function pickAutoVideo(pool, mode) {
  if (!pool.length) return null;
  const quality = (f) => [f.height || 0, f.tbr || 0, f.filesize || 0];
  const videoOnly = pool.filter((f) => !hasAudio(f));
  const muxed = pool.filter((f) => hasAudio(f) && f.vcodec && f.vcodec !== 'none');

  if (mode === 'video') {
    return pickMax(muxed, quality) ?? pickMax(pool, quality);
  }
  return pickMax(videoOnly, quality) ?? pickMax(pool, quality);
}

/**
 * Melhor faixa de áudio pelo bitrate de áudio.
 * É a regra usada no modo "só áudio" (`_pick_best_audio` no backend).
 */
function pickBestAudio(pool) {
  if (!pool.length) return null;
  return pickMax(pool, (f) => [f.abr || f.tbr || 0]);
}

/**
 * Melhor faixa de áudio para *anexar* a um vídeo.
 * Em MP4 prefere-se AAC (codec "mp4a"/"aac") — o Opus dentro de MP4 não é
 * reproduzido por muitos players. Desempate: (abr, height).
 */
function pickMergedAudio(pool) {
  if (!pool.length) return null;
  const rank = (f) => [f.abr || f.tbr || 0, f.height || 0];
  const aac = pool.filter((f) => {
    const codec = (f.acodec || '').toLowerCase();
    return codec.startsWith('mp4a') || codec.startsWith('aac');
  });
  if (aac.length) return pickMax(aac, rank);
  return pickMax(pool, rank);
}

/**
 * Melhor faixa de legenda.
 * Ordem de preferência de idioma; dentro do mesmo idioma, as faixas manuais
 * ganham às automáticas.
 */
function pickAutoSubtitle(tracks) {
  if (!tracks.length) return null;
  const score = (track) => {
    const lang = (track.language || '').toLowerCase();
    const index = SUBTITLE_LANGUAGE_RANK.indexOf(lang);
    // Maior pontuação = melhor: primeiro o idioma preferido, depois as
    // faixas manuais (as automáticas pontuam menos).
    return [
      index === -1 ? 0 : SUBTITLE_LANGUAGE_RANK.length - index,
      track.automatic ? 0 : 1,
    ];
  };
  return pickMax(tracks, score);
}

/**
 * Resolve a selecção como se os três eixos estivessem em "Automático".
 *
 * É isto que a opção "Automático" de cada lista mostra: o que o programa
 * escolheria se o utilizador não mexesse em nada — independentemente da
 * escolha que está activa naquele momento.
 */
function resolveAutoScenario() {
  const mode = currentMode();
  const video = mode === 'audio' ? null : pickAutoVideo(state.videoFormats, mode);

  let audio = null;
  let audioAction = 'none';

  if (mode === 'audio') {
    audio = pickBestAudio(state.audioFormats);
    audioAction = audio ? 'extract' : 'none';
  } else if (!video) {
    audio = null;
  } else if (!hasAudio(video)) {
    // O vídeo não traz som: o melhor áudio é anexado.
    audio = pickMergedAudio(state.audioFormats);
    audioAction = audio ? 'attached' : 'none';
  } else {
    // O vídeo escolhido já traz áudio — não há faixa separada.
    audio = null;
    audioAction = 'included';
  }

  return {
    mode,
    video,
    audio,
    audioAction,
    subtitle: pickAutoSubtitle(state.subtitleTracks),
  };
}

/**
 * Resolve o que vai ser descarregado, sem tocar na API.
 * Reproduz as regras do backend para que o resumo seja verdadeiro.
 */
function resolveSelection() {
  const mode = currentMode();
  const videoPool = state.videoFormats;
  const audioPool = state.audioFormats;

  // --- eixo de vídeo ------------------------------------------------------
  const videoAuto = state.videoChoice === 'auto';
  let video = videoAuto
    ? pickAutoVideo(videoPool, mode)
    : (videoPool.find((f) => f.id === state.videoChoice) ?? null);

  // No modo "só áudio" o vídeo não é usado.
  if (mode === 'audio') video = null;

  // --- eixo de áudio ------------------------------------------------------
  const audioAuto = state.audioChoice === 'auto';
  let audio = null;
  let audioAction = 'none';

  if (mode === 'audio') {
    // Só áudio: escolhe-se a faixa e extrai-se para o formato de saída.
    audio = audioAuto
      ? pickBestAudio(audioPool)
      : (audioPool.find((f) => f.id === state.audioChoice) ?? null);
    audioAction = audio ? 'extract' : 'none';
  } else if (audioAuto) {
    if (!video) {
      // Sem vídeo não há nada a fundir — o botão de descarregar fica
      // desactivado (o backend também falharia).
      audio = null;
      audioAction = 'none';
    } else if (!hasAudio(video)) {
      // O vídeo não traz som: o melhor áudio é anexado.
      audio = pickMergedAudio(audioPool);
      audioAction = audio ? 'attached' : 'none';
    } else {
      // O vídeo escolhido já traz áudio — não há faixa separada.
      audio = null;
      audioAction = 'included';
    }
  } else {
    audio = audioPool.find((f) => f.id === state.audioChoice) ?? null;
    if (!audio || !video) {
      // Sem vídeo não há nada a fundir.
      audioAction = 'none';
    } else if (hasAudio(video)) {
      // Áudio escolhido à mão: substitui o áudio que o vídeo trazia.
      audioAction = 'replaces';
    } else {
      audioAction = 'attached';
    }
  }

  // --- eixo de legendas ----------------------------------------------------
  const subtitleAuto = state.subtitleChoice === 'auto';
  const subtitle = subtitleAuto
    ? pickAutoSubtitle(state.subtitleTracks)
    : (state.subtitleChoice === 'none'
      ? null
      : (state.subtitleTracks.find((t) => t.id === state.subtitleChoice) ?? null));

  // O backend só funde áudio quando o vídeo também é indicado de forma
  // explícita. Com o áudio escolhido à mão e o vídeo em "Automático",
  // fixamos o vídeo (resolvido com as mesmas regras do backend) para que a
  // faixa de áudio escolhida não seja descartada.
  const pinVideo = mode !== 'audio'
    && videoAuto
    && !audioAuto
    && Boolean(audio)
    && Boolean(video);

  return {
    mode,
    video,
    videoAuto,
    audio,
    audioAuto,
    audioAction,
    subtitle,
    subtitleAuto,
    pinVideo,
    autoScenario: resolveAutoScenario(),
  };
}

// --------------------------------------------------------------------------
// Desenho do passo 2
// --------------------------------------------------------------------------
function renderSelection() {
  const selection = resolveSelection();

  // Preserva scroll e foco: as listas são redesenhadas a cada escolha.
  const containers = [$('#video-format-list'), $('#audio-format-list'), $('#subtitle-list')];
  const scrolls = containers.map((el) => el.scrollTop);
  const active = document.activeElement;
  const activeName = active?.getAttribute?.('name') ?? null;
  const activeValue = active?.getAttribute?.('value') ?? null;

  renderOptions($('#video-format-list'), buildVideoOptions(selection), {
    groupName: 'video-format',
    selectedValue: state.videoChoice,
    onSelect: (opt) => {
      state.videoChoice = opt.value;
      renderSelection();
    },
  });

  renderOptions($('#audio-format-list'), buildAudioOptions(selection), {
    groupName: 'audio-format',
    selectedValue: state.audioChoice,
    onSelect: (opt) => {
      state.audioChoice = opt.value;
      renderSelection();
    },
  });

  renderOptions($('#subtitle-list'), buildSubtitleOptions(selection), {
    groupName: 'subtitle-track',
    selectedValue: state.subtitleChoice,
    onSelect: (opt) => {
      state.subtitleChoice = opt.value;
      renderSelection();
    },
  });

  containers.forEach((el, index) => { el.scrollTop = scrolls[index]; });
  if (activeName && activeValue !== null) {
    const target = document.querySelector(
      `input[name="${CSS.escape(activeName)}"][value="${CSS.escape(activeValue)}"]`,
    );
    target?.focus({ preventScroll: true });
  }

  // Descrição do modo activo + resumo "Vais obter".
  const info = MODE_INFO[selection.mode] ?? MODE_INFO.auto;
  $('#mode-desc').textContent = info.desc;
  renderSummary($('#summary-panel'), {
    badge: info.label,
    rows: buildSummaryRows(selection),
  });

  $('#btn-download').disabled = !canDownload(selection);
  updateSelectionHint(selection);
}

function buildVideoOptions(selection) {
  const options = [{
    value: 'auto',
    badge: '✦',
    title: 'Automático',
    detail: selection.mode === 'audio'
      ? 'Sem vídeo neste modo'
      : (selection.autoScenario.video
        ? describeVideoFormat(selection.autoScenario.video)
        : 'Nenhum formato de vídeo disponível.'),
    tone: 'auto',
    tag: { text: 'AUTO', className: 'tag-auto' },
  }];

  for (const fmt of state.videoFormats) {
    const [tagText, tagClass] = formatTag(fmt);
    options.push({
      value: fmt.id,
      badge: fmt.height ? `${fmt.height}p` : 'Vídeo',
      title: formatOptionTitle(fmt),
      detail: formatOptionDetail(fmt),
      tag: { text: tagText, className: tagClass },
      size: fmt.filesize ? formatBytes(fmt.filesize) : '',
    });
  }
  return options;
}

function buildAudioOptions(selection) {
  const options = [{
    value: 'auto',
    badge: '✦',
    title: 'Automático',
    detail: selection.mode === 'audio'
      ? (selection.autoScenario.audio
        ? describeAudioFormat(selection.autoScenario.audio)
        : 'Nenhuma faixa de áudio disponível.')
      : (!selection.autoScenario.video
        ? 'Sem vídeo para anexar o áudio'
        : (selection.autoScenario.audio
          ? describeAudioFormat(selection.autoScenario.audio)
          : (selection.autoScenario.audioAction === 'included'
            ? 'O vídeo escolhido já inclui áudio'
            : 'Nenhuma faixa de áudio disponível.'))),
    tone: 'auto',
    tag: { text: 'AUTO', className: 'tag-auto' },
  }];

  for (const fmt of state.audioFormats) {
    const [tagText, tagClass] = formatTag(fmt);
    options.push({
      value: fmt.id,
      badge: formatBitrate(fmt.abr ?? fmt.tbr)?.replace(' kbps', 'k') ?? 'Áudio',
      title: formatOptionTitle(fmt),
      detail: formatOptionDetail(fmt),
      tag: { text: tagText, className: tagClass },
      size: fmt.filesize ? formatBytes(fmt.filesize) : '',
    });
  }
  return options;
}

function buildSubtitleOptions(selection) {
  const options = [
    {
      value: 'none',
      badge: '—',
      title: 'Sem legendas',
      detail: 'O ficheiro final não terá legendas.',
    },
    {
      value: 'auto',
      badge: '✦',
      title: 'Automático',
      detail: selection.autoScenario.subtitle
        ? describeSubtitleTrack(selection.autoScenario.subtitle)
        : 'Nenhuma faixa de legenda disponível.',
      tone: 'auto',
      tag: { text: 'AUTO', className: 'tag-auto' },
    },
  ];

  for (const track of state.subtitleTracks) {
    options.push({
      value: track.id,
      badge: (track.language || 'und').toUpperCase().slice(0, 6),
      title: track.name || track.language || 'Legenda',
      detail: track.automatic ? 'automática' : 'manual',
      tag: { text: 'LEG', className: 'tag-video' },
    });
  }
  return options;
}

/** Uma linha por eixo, com o estado final de cada um. */
function buildSummaryRows(selection) {
  const rows = [];

  // Vídeo
  if (selection.mode === 'audio') {
    rows.push({ axis: 'Vídeo', value: 'Não incluído', note: 'modo só áudio', muted: true });
  } else if (!selection.video) {
    rows.push({ axis: 'Vídeo', value: 'Sem formato de vídeo', muted: true });
  } else {
    rows.push({
      axis: 'Vídeo',
      value: describeVideoFormat(selection.video),
      auto: selection.videoAuto,
    });
  }

  // Áudio
  if (selection.mode === 'audio') {
    rows.push({
      axis: 'Áudio',
      value: selection.audio ? describeAudioFormat(selection.audio) : 'Sem faixa de áudio',
      note: selection.audio ? `extraído para ${state.audioFormat.toUpperCase()}` : '',
      auto: selection.audioAuto,
      muted: !selection.audio,
    });
  } else if (selection.audioAction === 'included') {
    rows.push({
      axis: 'Áudio',
      value: 'Incluído no vídeo',
      note: 'o vídeo escolhido já traz som',
      auto: selection.audioAuto,
      muted: true,
    });
  } else if (selection.audioAction === 'attached') {
    rows.push({
      axis: 'Áudio',
      value: describeAudioFormat(selection.audio),
      note: 'anexado ao vídeo',
      auto: selection.audioAuto,
    });
  } else if (selection.audioAction === 'replaces') {
    rows.push({
      axis: 'Áudio',
      value: describeAudioFormat(selection.audio),
      note: 'substitui o áudio do vídeo',
      auto: selection.audioAuto,
    });
  } else {
    rows.push({
      axis: 'Áudio',
      value: selection.video ? 'Sem áudio disponível' : 'Não incluído (sem vídeo)',
      muted: true,
    });
  }

  // Legendas
  if (!selection.subtitle) {
    rows.push({ axis: 'Legendas', value: 'Sem legendas', muted: true });
  } else {
    rows.push({
      axis: 'Legendas',
      value: describeSubtitleTrack(selection.subtitle),
      auto: selection.subtitleAuto,
    });
  }

  return rows;
}

/** Há alguma coisa para descarregar? */
function canDownload(selection) {
  if (selection.mode === 'audio') return Boolean(selection.audio);
  return Boolean(selection.video);
}

function updateSelectionHint(selection) {
  const hint = $('#selection-hint');
  if (selection.mode === 'audio') {
    hint.textContent = `Ficheiro final: ${state.audioFormat.toUpperCase()}`;
  } else if (!selection.video) {
    hint.textContent = 'Este URL não tem vídeo — usa o modo "Só áudio"';
  } else if (!hasAudio(selection.video) && !selection.audio) {
    hint.textContent = 'Ficheiro final: MP4 · este URL não tem áudio disponível';
  } else {
    hint.textContent = 'Ficheiro final: MP4';
  }
}

// --------------------------------------------------------------------------
// Passo 3 — download
// --------------------------------------------------------------------------

/**
 * Corpo do pedido — espelha o contrato do backend.
 * `video_format_id`/`audio_format_id` a null significam "o programa escolhe".
 */
function buildPayload(selection) {
  const mode = selection.mode;
  const payload = {
    url: state.media.original_url || $('#inp-url').value.trim(),
    cookies_from_browser: state.cookiesFromBrowser || null,
    mode,
    video_format_id: null,
    audio_format_id: null,
    subtitle_lang: null,
    audio_format: state.audioFormat,
    extract_audio: mode === 'audio',
    merge: mode !== 'audio',
  };

  // Vídeo (não é usado no modo "só áudio").
  if (mode !== 'audio') {
    if (state.videoChoice !== 'auto') {
      payload.video_format_id = selection.video?.id ?? null;
    } else if (selection.pinVideo) {
      // Vídeo resolvido com as regras do backend, para garantir o merge
      // com o áudio escolhido à mão.
      payload.video_format_id = selection.video?.id ?? null;
    }
  }

  // Áudio.
  if (state.audioChoice !== 'auto') {
    payload.audio_format_id = selection.audio?.id ?? null;
  }

  // Legendas: null = sem legendas, "auto" = o programa escolhe, ou o id
  // completo da faixa (`grupo:idioma:nome`).
  if (state.subtitleChoice === 'auto') {
    payload.subtitle_lang = 'auto';
  } else if (state.subtitleChoice !== 'none') {
    payload.subtitle_lang = state.subtitleChoice;
  }

  return payload;
}

async function onDownload() {
  if (!state.media) return;

  const selection = resolveSelection();
  if (!canDownload(selection)) return;

  const payload = buildPayload(selection);
  const mode = selection.mode;

  if (mode === 'auto') {
    log('Modo automático: o programa escolhe o melhor vídeo, áudio e legendas.', 'ok');
  } else if (mode === 'video') {
    log('Modo Vídeo: usa o stream escolhido e liga o melhor áudio quando necessário.', 'info');
  } else if (mode === 'both') {
    log('Modo Vídeo + áudio: escolhe o melhor vídeo e a melhor faixa de áudio, fundidos num só ficheiro.', 'info');
  } else if (mode === 'audio') {
    log(`Modo Só áudio: extrai a faixa de áudio para ${state.audioFormat.toUpperCase()}.`, 'info');
  }

  const button = $('#btn-download');
  button.disabled = true;
  $('#progress-card').classList.remove('hidden');
  hideResult();
  setProgress({ percent: 0, phase: 'idle', message: 'A criar o job…' });
  log(`A iniciar download (${mode})…`, 'info');

  try {
    const job = await state.client.createDownload(payload);
    state.jobId = job.job_id;
    $('#btn-cancel').classList.remove('hidden');
    log(`Job ${job.job_id} criado.`, 'info');
    connectProgress(job.job_id);
  } catch (err) {
    log(`Não foi possível iniciar o download: ${err.message}`, 'err');
    toast(err.message, 'err');
    button.disabled = false;
  }
}

function connectProgress(jobId) {
  state.socket?.close();

  const url = ProgressSocket.buildUrl(jobId, {
    base: state.client.apiBase.startsWith('http')
      ? state.client.apiBase
      : `${location.origin}${state.client.apiBase}`,
    apiKey: state.client.apiKey,
  });

  state.socket = new ProgressSocket({
    url,
    apiKey: state.client.apiKey,
    onStatus: (status, info) => {
      if (status === 'reconnecting') {
        log(`Ligação perdida — a reconectar (tentativa ${info?.attempt ?? '?'})…`, 'warn');
      } else if (status === 'failed') {
        log('Não foi possível manter a ligação ao servidor de progresso.', 'err');
      }
    },
    onMessage: handleProgressEvent,
  });
  state.socket.connect();
}

function handleProgressEvent(event) {
  if (event.error) {
    log(`Erro do servidor: ${event.error.message}`, 'err');
    toast(event.error.message, 'err');
    return;
  }

  setProgress({
    percent: event.percent ?? 0,
    phase: event.phase ?? 'downloading',
    downloaded: event.downloaded_bytes ?? 0,
    total: event.total_bytes ?? null,
    speed: event.speed ?? 0,
    eta: event.eta ?? null,
    message: event.message ?? '',
  });

  if (event.message) log(event.message, 'info');

  if (event.status === 'completed') {
    finishJob();
  } else if (event.status === 'failed') {
    log('O download falhou.', 'err');
    toast('O download falhou. Ver o registo para mais detalhes.', 'err');
    $('#btn-cancel').classList.add('hidden');
    $('#btn-download').disabled = false;
    state.socket?.close();
  } else if (event.status === 'cancelled') {
    log('Download cancelado.', 'warn');
    $('#btn-cancel').classList.add('hidden');
    $('#btn-download').disabled = false;
    state.socket?.close();
  }
}

async function finishJob() {
  $('#btn-cancel').classList.add('hidden');
  $('#btn-download').disabled = false;
  state.socket?.close();

  try {
    const job = await state.client.getJob(state.jobId);
    const result = job.result ?? {};
    const fileUrl = state.client.fileUrl(state.jobId);
    showResult({
      filename: result.filename,
      size: result.size,
      href: fileUrl,
      warnings: result.warnings,
      media: state.media,
    });

    // Guardar no histórico da sessão
    state.history.unshift({
      title: state.media?.title || result.filename,
      filename: result.filename,
      size: result.size,
      href: fileUrl,
      thumbnail: state.media?.thumbnail,
      platform: state.media?.extractor,
      time: Date.now(),
    });
    $('#history-badge').textContent = state.history.length;
    renderSessionHistory($('#history-list'), state.history);

    log(`Concluído: ${result.filename} (${result.size ?? 0} bytes)`, 'ok');
    for (const warning of result.warnings ?? []) {
      log(`Aviso: ${warning}`, 'warn');
    }
    toast('Download concluído com sucesso!', 'ok');
  } catch (err) {
    log(`Job concluído mas não foi possível obter o resultado: ${err.message}`, 'warn');
  }
}

function applyPreset(preset) {
  if (!state.media) {
    toast('Analise um link primeiro antes de aplicar um preset.', 'warn');
    return;
  }

  if (preset === 'audio') {
    const radio = document.querySelector('input[name="mode"][value="audio"]');
    if (radio) {
      radio.checked = true;
      state.videoChoice = 'auto';
      renderSelection();
      toast('Modo só áudio selecionado.', 'info');
    }
    return;
  }

  const radio = document.querySelector('input[name="mode"][value="both"]') ||
                document.querySelector('input[name="mode"][value="auto"]');
  if (radio) radio.checked = true;

  if (preset === 'max') {
    state.videoChoice = 'auto';
    state.audioChoice = 'auto';
    renderSelection();
    toast('Preset de máxima qualidade ativado.', 'ok');
    return;
  }

  const targetHeight = Number(preset);
  // Procura o formato com altura mais próxima ou igual
  const match = state.videoFormats
    .filter((f) => f.height && f.height <= targetHeight)
    .sort((a, b) => (b.height || 0) - (a.height || 0))[0] ||
    state.videoFormats.find((f) => f.height === targetHeight);

  if (match) {
    state.videoChoice = match.id;
    state.audioChoice = 'auto';
    renderSelection();
    toast(`Preset ${targetHeight}p aplicado (${match.height}p).`, 'ok');
  } else {
    state.videoChoice = 'auto';
    renderSelection();
    toast(`Resolução ${targetHeight}p não disponível; a usar a melhor disponível.`, 'info');
  }
}

async function copyDownloadLink() {
  if (!state.jobId) return;
  const fileUrl = state.client.fileUrl(state.jobId);
  const absoluteUrl = fileUrl.startsWith('http') ? fileUrl : `${location.origin}${fileUrl}`;
  try {
    await navigator.clipboard.writeText(absoluteUrl);
    toast('Link do ficheiro copiado para a área de transferência!', 'ok');
  } catch {
    toast(`Link: ${absoluteUrl}`, 'info');
  }
}

function resetForNewDownload() {
  $('#inp-url').value = '';
  $('#platform-badge').classList.add('hidden');
  $('#formats-card').classList.add('hidden');
  $('#progress-card').classList.add('hidden');
  hideResult();
  state.media = null;
  state.formats = [];
  state.videoFormats = [];
  state.audioFormats = [];
  state.subtitleTracks = [];
  state.jobId = null;
  $('#inp-url').focus();
  toast('Pronto para um novo download!', 'info');
}

function toggleHistory() {
  const card = $('#history-card');
  if (!card) return;
  const hidden = card.classList.toggle('hidden');
  if (!hidden) {
    renderSessionHistory($('#history-list'), state.history);
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

function clearHistory() {
  state.history = [];
  $('#history-badge').textContent = '0';
  renderSessionHistory($('#history-list'), []);
  toast('Histórico da sessão limpo.', 'info');
}

async function onCancel() {
  if (!state.jobId) return;
  try {
    await state.client.cancelJob(state.jobId);
    log('Pedido de cancelamento enviado.', 'warn');
  } catch (err) {
    log(`Não foi possível cancelar: ${err.message}`, 'err');
  }
}

// --------------------------------------------------------------------------
// Motor de Atualizações Automáticas (SyncBot)
// --------------------------------------------------------------------------
let enginePollInterval = null;

function initEngineUpdater() {
  const btnCheck   = $('#btn-check-engine');
  const btnUpdate  = $('#btn-update-engine');
  const btnRollbck = $('#btn-rollback-engine');

  if (btnCheck)   btnCheck.addEventListener('click', checkEngineVersion);
  if (btnUpdate)  btnUpdate.addEventListener('click', triggerEngineUpdate);
  if (btnRollbck) btnRollbck.addEventListener('click', triggerEngineRollback);

  // Lê o estado inicial silenciosamente ao abrir as definições
  loadEngineStatus(false);
}

async function loadEngineStatus(showLog = true) {
  try {
    const data = await state.client.engineStatus();
    renderEngineStatus(data, showLog);
  } catch {
    renderEngineStatus(null, false);
  }
}

async function checkEngineVersion() {
  const btn = $('#btn-check-engine');
  if (btn) { btn.disabled = true; btn.textContent = '🔍 A verificar…'; }
  const statusEl = $('#engine-action-status');
  if (statusEl) statusEl.textContent = 'A contactar repositório da comunidade…';
  try {
    const data = await state.client.engineCheck();
    renderEngineStatus(data, true);
    if (data.update_available) {
      toast(`⚡ Nova versão ${data.latest_version} disponível!`, 'ok');
      log(`⚡ Nova versão do motor: ${data.latest_version} (atual: ${data.current_version})`, 'ok');
    } else {
      toast(`✓ Motor na versão mais recente (${data.current_version})`, 'ok');
      log(`✓ Motor atualizado: versão ${data.current_version}`, 'ok');
    }
    if (statusEl) statusEl.textContent = '';
  } catch (err) {
    toast(`Erro a verificar versão: ${err.message}`, 'err');
    if (statusEl) statusEl.textContent = `Erro: ${err.message}`;
    log(`Falha a verificar versão do motor: ${err.message}`, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔍 Verificar Novas Versões'; }
  }
}

async function triggerEngineUpdate() {
  if (!confirm('Iniciar atualização do motor de streaming em segundo plano?\nA aplicação permanecerá funcional durante o processo.')) return;
  const btnUpdate = $('#btn-update-engine');
  if (btnUpdate) { btnUpdate.disabled = true; }
  const progressBox = $('#engine-progress-box');
  if (progressBox) progressBox.classList.remove('hidden');
  log('⚡ Atualização do motor iniciada em segundo plano…', 'info');
  try {
    const data = await state.client.engineUpdate();
    if (data.success) {
      toast('Atualização iniciada! A acompanhar progresso…', 'ok');
      startEngineProgressPolling();
    } else {
      toast(`Não foi possível iniciar: ${data.message}`, 'err');
      log(`Falha ao iniciar atualização: ${data.message}`, 'err');
      if (btnUpdate) btnUpdate.disabled = false;
    }
  } catch (err) {
    toast(`Erro: ${err.message}`, 'err');
    log(`Falha na atualização do motor: ${err.message}`, 'err');
    if (btnUpdate) btnUpdate.disabled = false;
  }
}

async function triggerEngineRollback() {
  if (!confirm('Reverter o motor para a cópia de segurança anterior?\nO sistema irá restaurar automaticamente a versão mais recente disponível.')) return;
  try {
    const data = await state.client.engineRollback();
    if (data.success) {
      toast(`↺ Rollback concluído: ${data.message}`, 'ok');
      log(`↺ Motor revertido: ${data.message}`, 'info');
    } else {
      toast(`Rollback falhou: ${data.message}`, 'err');
    }
    await loadEngineStatus(true);
  } catch (err) {
    toast(`Erro no rollback: ${err.message}`, 'err');
    log(`Falha no rollback: ${err.message}`, 'err');
  }
}

function startEngineProgressPolling() {
  if (enginePollInterval) clearInterval(enginePollInterval);
  enginePollInterval = setInterval(async () => {
    try {
      const data = await state.client.engineStatus();
      renderEngineStatus(data, false);
      if (data.recent_logs && data.recent_logs.length > 0) {
        const last = data.recent_logs[data.recent_logs.length - 1];
        const stepEl = $('#engine-progress-step');
        if (stepEl) stepEl.textContent = last;
      }
      if (!data.is_updating) {
        clearInterval(enginePollInterval);
        enginePollInterval = null;
        const progressBox = $('#engine-progress-box');
        if (progressBox) progressBox.classList.add('hidden');
        const btnUpdate = $('#btn-update-engine');
        if (btnUpdate) btnUpdate.disabled = false;
        if (data.error) {
          toast(`Falha na atualização: ${data.error}`, 'err');
          log(`Falha na atualização do motor: ${data.error}`, 'err');
        } else {
          toast(`✓ Motor atualizado para ${data.current_version}!`, 'ok');
          log(`✓ Motor MyArchive Downloader atualizado para ${data.current_version}`, 'ok');
        }
      }
    } catch { /* ignora erros de polling */ }
  }, 2000);
}

function renderEngineStatus(data, showLog) {
  const badge   = $('#engine-status-badge');
  const label   = $('#engine-status-label');
  const btnUpdate  = $('#btn-update-engine');
  const btnRollbck = $('#btn-rollback-engine');
  const progressBar  = $('#engine-progress-bar');
  const progressPct  = $('#engine-progress-percent');

  if (!label) return;

  if (!data) {
    if (badge) badge.className = 'cookies-badge';
    label.textContent = 'Motor: offline';
    return;
  }

  // Versão e estado
  const ver = data.current_version || '—';
  const latestVer = data.latest_version;
  if (data.is_updating) {
    if (badge) badge.className = 'cookies-badge';
    label.textContent = `⚡ A atualizar… ${Math.round(data.progress_percent || 0)}%`;
  } else if (data.update_available && latestVer) {
    if (badge) badge.className = 'cookies-badge';
    label.textContent = `v${ver} → ${latestVer} disponível`;
  } else {
    if (badge) badge.className = 'cookies-badge active';
    label.textContent = `Motor v${ver} ✓`;
  }

  // Botões
  if (btnUpdate) {
    btnUpdate.classList.toggle('hidden', !data.update_available || data.is_updating);
  }
  if (btnRollbck) {
    btnRollbck.classList.remove('hidden');
  }

  // Barra de progresso
  if (progressBar && progressPct) {
    const pct = Math.round(data.progress_percent || 0);
    progressBar.style.width = `${pct}%`;
    progressPct.textContent = `${pct}%`;
    const box = $('#engine-progress-box');
    if (box) box.classList.toggle('hidden', !data.is_updating);
  }

  if (showLog && data.update_available && latestVer) {
    log(`Motor: nova versão ${latestVer} disponível (atual: ${ver})`, 'info');
  }
}
