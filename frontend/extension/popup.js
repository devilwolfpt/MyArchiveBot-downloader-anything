/**
 * Popup da extensão MV3.
 *
 * Reutiliza os mesmos módulos do painel web (`../js/api.js`, `../js/ws.js`,
 * `../js/ui.js`) — a única diferença é a origem da URL (a do separador activo)
 * e o delivery do ficheiro (via `chrome.downloads`).
 */
import { ApiClient } from '../js/api.js';
import { ProgressSocket } from '../js/ws.js';
import {
  $, $$, clearLog, formatBytes, formatDuration, hideResult, log, renderFormats,
  setProgress, showResult, toast,
} from '../js/ui.js';

const state = {
  client: null,
  media: null,
  formats: [],
  selectedFormatId: null,
  jobId: null,
  socket: null,
  audioFormat: 'mp3',
};

document.addEventListener('DOMContentLoaded', init);

function init() {
  const stored = chrome.storage.local.get(['apiBase', 'apiKey', 'audioFormat']);
  state.audioFormat = stored.audioFormat || 'mp3';
  state.client = new ApiClient({
    apiBase: stored.apiBase || 'http://localhost:8000/api',
    apiKey: stored.apiKey || '',
  });

  $('#inp-api-base').value = state.client.apiBase;
  $('#inp-api-key').value = state.client.apiKey;
  $('#inp-audio-format')?.remove();

  $('#btn-extract').addEventListener('click', onExtract);
  $('#btn-detect').addEventListener('click', onDetect);
  $('#btn-download').addEventListener('click', onDownload);
  $('#btn-cancel').addEventListener('click', onCancel);
  $('#btn-clear-log').addEventListener('click', clearLog);
  $('#btn-settings').addEventListener('click', toggleSettings);
  $('#btn-close-settings').addEventListener('click', toggleSettings);
  $('#btn-save-settings').addEventListener('click', saveSettings);

  $$('input[name="mode"]').forEach((input) => input.addEventListener('change', applyModeFilter));

  log('Extensão pronta.', 'info');
  checkHealth();
  fillCurrentTabUrl();
}

// --------------------------------------------------------------------------
async function checkHealth() {
  const pill = $('#api-status');
  try {
    const health = await state.client.health();
    pill.textContent = 'Ligado';
    pill.className = 'pill pill-ok';
  } catch {
    pill.textContent = 'Sem ligação';
    pill.className = 'pill pill-err';
  }
}

function toggleSettings() {
  $('#settings-panel').classList.toggle('hidden');
}

function saveSettings() {
  state.client.apiBase = $('#inp-api-base').value.trim().replace(/\/+$/, '') || '/api';
  state.client.apiKey = $('#inp-api-key').value.trim();
  chrome.storage.local.set({ apiBase: state.client.apiBase, apiKey: state.client.apiKey });
  toggleSettings();
  checkHealth();
}

// --------------------------------------------------------------------------
async function fillCurrentTabUrl() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.url) {
      $('#inp-url').value = tab.url;
      log(`URL do separador: ${tab.url}`, 'info');
    }
  } catch (err) {
    log(`Não foi possível ler o separador: ${err.message}`, 'warn');
  }
}

async function onDetect() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        const media = [...document.querySelectorAll('video, audio')].map((el) => el.currentSrc || el.src).filter(Boolean);
        const sources = [...document.querySelectorAll('source[src]')].map((el) => el.src);
        const og = document.querySelector('meta[property="og:video"], meta[property="og:video:url"]')?.content;
        return [...new Set([...media, ...sources, og].filter(Boolean))];
      },
    });
    const found = results[0]?.result ?? [];
    if (found.length) {
      $('#inp-url').value = found[0];
      log(`Media detectada na página: ${found[0]}`, 'ok');
    } else {
      log('Nenhum media encontrado por detecção directa.', 'warn');
    }
  } catch (err) {
    log(`Detecção falhou: ${err.message}`, 'err');
  }
}

// --------------------------------------------------------------------------
async function onExtract() {
  const url = $('#inp-url').value.trim();
  if (!url) return;

  $('#btn-extract').disabled = true;
  log(`A extrair: ${url}`, 'info');

  try {
    const media = await state.client.extractInfo(url);
    state.media = media;
    state.formats = (media.formats || []).filter((f) => !f.unavailable);
    if (!state.formats.length) throw new Error('Nenhum formato disponível');

    $('#media-meta').textContent = `${media.extractor} · ${state.formats.length}`;
    applyModeFilter();
    $('#formats-card').classList.remove('hidden');
    log(`Formatos encontrados (${media.title ?? ''})`, 'ok');
  } catch (err) {
    log(`Erro: ${err.message}`, 'err');
    toast(err.message, 'err');
  } finally {
    $('#btn-extract').disabled = false;
  }
}

function currentMode() {
  return document.querySelector('input[name="mode"]:checked')?.value ?? 'video';
}

function applyModeFilter() {
  const mode = currentMode();
  let visible = state.formats;

  if (mode === 'audio') {
    visible = state.formats.filter((f) => f.media_type === 'audio');
  } else if (mode === 'both') {
    const videoOnly = state.formats.filter((f) => f.media_type === 'video' && (!f.acodec || f.acodec === 'none'));
    visible = videoOnly.length ? videoOnly : state.formats.filter((f) => f.media_type === 'video');
  } else {
    visible = state.formats.filter((f) => f.media_type === 'video');
  }
  if (!visible.length) visible = state.formats;

  const best = [...visible].sort(
    (a, b) => (b.height || 0) * 1000 + (b.tbr || b.abr || 0) - ((a.height || 0) * 1000 + (a.tbr || a.abr || 0)),
  )[0];
  state.selectedFormatId = best?.id ?? null;

  renderFormats($('#format-list'), visible, {
    selectedId: state.selectedFormatId,
    onSelect: (fmt) => { state.selectedFormatId = fmt.id; },
  });
  $('#btn-download').disabled = !state.selectedFormatId;
}

// --------------------------------------------------------------------------
async function onDownload() {
  if (!state.selectedFormatId || !state.media) return;

  const mode = currentMode();
  $('#btn-download').disabled = true;
  $('#progress-card').classList.remove('hidden');
  hideResult();

  try {
    const job = await state.client.createDownload({
      url: state.media.original_url || $('#inp-url').value.trim(),
      format_id: state.selectedFormatId,
      mode,
      audio_format: state.audioFormat,
      extract_audio: mode === 'audio',
      merge: mode !== 'audio',
    });
    state.jobId = job.job_id;
    $('#btn-cancel').classList.remove('hidden');
    log(`Job ${job.job_id} criado.`, 'info');
    connectProgress(job.job_id);
  } catch (err) {
    log(`Erro: ${err.message}`, 'err');
    toast(err.message, 'err');
    $('#btn-download').disabled = false;
  }
}

function connectProgress(jobId) {
  state.socket?.close();
  const base = state.client.apiBase.startsWith('http')
    ? state.client.apiBase
    : `http://localhost:8000/api`;

  state.socket = new ProgressSocket({
    url: ProgressSocket.buildUrl(jobId, { base, apiKey: state.client.apiKey }),
    onStatus: (status, info) => {
      if (status === 'reconnecting') log(`A reconectar (${info?.attempt ?? '?'})…`, 'warn');
    },
    onMessage: (event) => {
      if (event.error) {
        log(event.error.message, 'err');
        return;
      }
      setProgress({
        percent: event.percent,
        phase: event.phase,
        downloaded: event.downloaded_bytes,
        total: event.total_bytes,
        speed: event.speed,
        eta: event.eta,
        message: event.message,
      });
      if (event.status === 'completed') finishJob();
      if (event.status === 'failed' || event.status === 'cancelled') {
        $('#btn-cancel').classList.add('hidden');
        $('#btn-download').disabled = false;
        state.socket?.close();
      }
    },
  });
  state.socket.connect();
}

async function finishJob() {
  $('#btn-cancel').classList.add('hidden');
  $('#btn-download').disabled = false;
  state.socket?.close();

  try {
    const job = await state.client.getJob(state.jobId);
    const result = job.result ?? {};
    showResult({
      filename: result.filename,
      size: result.size,
      href: state.client.fileUrl(state.jobId),
      warnings: result.warnings,
    });

    // Entrega através do gestor de downloads do browser (com tray + pausa).
    const url = state.client.fileUrl(state.jobId);
    chrome.runtime.sendMessage({ type: 'download', url, filename: result.filename });

    log(`Concluído: ${result.filename} (${formatBytes(result.size ?? 0)})`, 'ok');
    for (const warning of result.warnings ?? []) {
      log(`Aviso: ${warning}`, 'warn');
    }
    toast('Download concluído!', 'ok');
  } catch (err) {
    log(`Erro ao obter resultado: ${err.message}`, 'err');
  }
}

async function onCancel() {
  if (!state.jobId) return;
  try {
    await state.client.cancelJob(state.jobId);
    log('Cancelamento pedido.', 'warn');
  } catch (err) {
    log(err.message, 'err');
  }
}

// expõe utilitários usados pelo background
export { formatDuration };
