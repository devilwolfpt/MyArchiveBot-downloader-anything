/**
 * Camada de apresentação: formatação e manipulação do DOM.
 * Não conhece a API — recebe dados já tratados.
 */

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

// --------------------------------------------------------------------------
// Formatação
// --------------------------------------------------------------------------
export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const decimals = value >= 100 || unit === 0 ? 0 : 1;
  return `${value.toFixed(decimals)} ${units[unit]}`;
}

export function formatSpeed(bytesPerSecond) {
  if (!bytesPerSecond || bytesPerSecond <= 0) return '—';
  return `${formatBytes(bytesPerSecond)}/s`;
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—';
  const total = Math.max(0, Math.round(Number(seconds)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function formatEta(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 1) return 'quase';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

export function formatDate(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString('pt-PT', { year: 'numeric', month: 'short', day: 'numeric' });
}

// --------------------------------------------------------------------------
// Registo
// --------------------------------------------------------------------------
const LEVEL_CLASS = { info: 'info', ok: 'ok', err: 'err', warn: 'warn' };

export function log(message, level = 'info') {
  const el = $('#log');
  if (!el) return;
  const time = new Date().toLocaleTimeString('pt-PT', { hour12: false });
  const span = document.createElement('span');
  span.className = LEVEL_CLASS[level] ?? 'info';
  span.textContent = `[${time}] ${message}\n`;
  el.appendChild(span);
  el.scrollTop = el.scrollHeight;
}

export function clearLog() {
  const el = $('#log');
  if (el) el.textContent = '';
}

// --------------------------------------------------------------------------
// Toast
// --------------------------------------------------------------------------
let toastTimer = null;

export function toast(message, kind = '') {
  const el = $('#toast');
  if (!el) return;
  el.textContent = message;
  el.className = `toast${kind ? ` ${kind}` : ''}`;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), kind === 'err' ? 6000 : 3200);
}

// --------------------------------------------------------------------------
// Formatos
// --------------------------------------------------------------------------
const VIDEO_TAGS = {
  m3u8_native: ['HLS', 'tag-hls'],
  hls: ['HLS', 'tag-hls'],
  m3u8: ['HLS', 'tag-hls'],
  dash: ['DASH', 'tag-dash'],
  mpd: ['DASH', 'tag-dash'],
};

export function formatTag(fmt) {
  if (fmt.manifest_kind === 'hls') return VIDEO_TAGS.hls;
  if (fmt.manifest_kind === 'dash') return VIDEO_TAGS.dash;
  if (fmt.media_type === 'audio') return ['ÁUDIO', 'tag-audio'];
  return ['VÍDEO', 'tag-video'];
}

export function renderFormats(container, formats, { selectedId = null, recommendedId = null, groupName = 'format', onSelect } = {}) {
  container.innerHTML = '';
  if (!formats.length) {
    container.innerHTML = '<p class="hint">Nenhum formato disponível.</p>';
    return;
  }

  for (const fmt of formats) {
    const label = document.createElement('label');
    label.className = 'format';
    if (fmt.id === selectedId) label.classList.add('selected');
    if (fmt.id === recommendedId) label.classList.add('recommended');

    const badge = fmt.height ? `${fmt.height}p` : (fmt.media_type === 'audio' ? 'Áudio' : '—');
    const [tagText, tagClass] = formatTag(fmt);

    const details = [];
    if (fmt.container && fmt.container !== 'unknown') details.push(fmt.container);
    if (fmt.vcodec && fmt.vcodec !== 'none') details.push(fmt.vcodec.split('.')[0]);
    if (fmt.acodec && fmt.acodec !== 'none') details.push(fmt.acodec.split('.')[0]);
    if (fmt.fps && fmt.fps >= 50) details.push(`${Math.round(fmt.fps)}fps`);
    if (fmt.audio_channels) details.push(`${fmt.audio_channels}ch`);
    if (fmt.filesize) details.push(formatBytes(fmt.filesize));

    const recommendedSpan = fmt.id === recommendedId
      ? '<span class="format-recommended">Recomendado</span>'
      : '';

    label.innerHTML = `
      <input type="radio" name="${escapeAttr(groupName)}" value="${escapeAttr(fmt.id)}" />
      <span class="format-badge">${escapeHtml(badge)}</span>
      <span class="format-desc">
        <strong>${escapeHtml(fmt.label || fmt.id)}</strong>
        <span>${escapeHtml(details.join(' · '))}</span>
      </span>
      <span class="format-tag ${tagClass}">${tagText}</span>
      ${recommendedSpan}
      <span class="format-size">${fmt.filesize ? formatBytes(fmt.filesize) : ''}</span>
    `;

    const input = label.querySelector('input');
    input.checked = fmt.id === selectedId;
    input.addEventListener('change', () => {
      container.querySelectorAll('.format').forEach((el) => el.classList.remove('selected'));
      label.classList.add('selected');
      onSelect?.(fmt);
    });

    container.appendChild(label);
  }
}

export function renderSubtitleTracks(container, tracks, { selectedId = null, onSelect } = {}) {
  container.innerHTML = '';
  if (!tracks.length) {
    container.innerHTML = '<p class="hint">Sem legendas disponíveis.</p>';
    return;
  }

  for (const track of tracks) {
    const label = document.createElement('label');
    label.className = 'format';
    if (track.id === selectedId) label.classList.add('selected');

    label.innerHTML = `
      <input type="radio" name="subtitle-track" value="${escapeAttr(track.id)}" />
      <span class="format-badge">${escapeHtml((track.language || 'und').toUpperCase())}</span>
      <span class="format-desc">
        <strong>${escapeHtml(track.name || track.language || 'Legenda')}</strong>
        <span>${track.automatic ? 'Automática' : 'Manual'}</span>
      </span>
      <span class="format-tag tag-video">LEG</span>
    `;

    const input = label.querySelector('input');
    input.checked = track.id === selectedId;
    input.addEventListener('change', () => {
      container.querySelectorAll('.format').forEach((el) => el.classList.remove('selected'));
      label.classList.add('selected');
      onSelect?.(track);
    });

    container.appendChild(label);
  }
}

// --------------------------------------------------------------------------
// Descrição de formatos (painel "Vais obter")
// --------------------------------------------------------------------------
const VIDEO_CODEC_NAMES = {
  avc1: 'H.264', avc3: 'H.264', h264: 'H.264', x264: 'H.264',
  hevc: 'H.265', hvc1: 'H.265', hev1: 'H.265', x265: 'H.265',
  vp9: 'VP9', vp09: 'VP9', vp8: 'VP8', vp08: 'VP8',
  av01: 'AV1', av1: 'AV1',
  mp4v: 'MPEG-4', mpeg4: 'MPEG-4', 'mpeg-4': 'MPEG-4',
  theora: 'Theora',
};

const AUDIO_CODEC_NAMES = {
  mp4a: 'AAC', aac: 'AAC', 'aac_latm': 'AAC',
  opus: 'Opus',
  'ec-3': 'E-AC3', eac3: 'E-AC3', ac3: 'AC-3',
  vorbis: 'Vorbis',
  mp3: 'MP3', 'mp3 float': 'MP3',
  flac: 'FLAC', alac: 'ALAC', dts: 'DTS', 'dts-hd': 'DTS-HD',
};

/** Nome legível de um codec ("avc1.640028" -> "H.264"). */
export function codecName(codec) {
  if (!codec || codec === 'none') return null;
  const key = String(codec).toLowerCase().split('.')[0].trim();
  if (!key) return null;
  return VIDEO_CODEC_NAMES[key] ?? AUDIO_CODEC_NAMES[key] ?? key.toUpperCase();
}

/** Bitrate legível ("4200" -> "4.2 Mbps", "160" -> "160 kbps"). */
export function formatBitrate(kbps) {
  if (kbps === null || kbps === undefined || Number.isNaN(Number(kbps))) return null;
  const value = Number(kbps);
  if (value <= 0) return null;
  if (value >= 1000) return `${(value / 1000).toFixed(1)} Mbps`;
  return `${Math.round(value)} kbps`;
}

/** Descrição curta de um formato de vídeo: "1080p · VP9 · 4.2 Mbps". */
export function describeVideoFormat(fmt) {
  if (!fmt) return '—';
  const parts = [];
  if (fmt.height) parts.push(`${fmt.height}p`);
  else if (fmt.width) parts.push(`${fmt.width}w`);
  const vcodec = codecName(fmt.vcodec);
  if (vcodec) parts.push(vcodec);
  const bitrate = formatBitrate(fmt.tbr);
  if (bitrate) parts.push(bitrate);
  return parts.join(' · ') || fmt.id;
}

/** Descrição curta de uma faixa de áudio: "Opus · 160 kbps". */
export function describeAudioFormat(fmt) {
  if (!fmt) return '—';
  const parts = [];
  const acodec = codecName(fmt.acodec);
  if (acodec) parts.push(acodec);
  const bitrate = formatBitrate(fmt.abr ?? fmt.tbr);
  if (bitrate) parts.push(bitrate);
  return parts.join(' · ') || fmt.id;
}

/** Descrição curta de uma faixa de legenda: "Português (manual)". */
export function describeSubtitleTrack(track) {
  if (!track) return '—';
  const name = track.name || track.language || 'Legenda';
  return `${name} (${track.automatic ? 'automática' : 'manual'})`;
}

/** Título de uma opção de formato: codec + container ("VP9 · mp4"). */
export function formatOptionTitle(fmt) {
  if (!fmt) return '—';
  const codec = fmt.media_type === 'audio' ? codecName(fmt.acodec) : codecName(fmt.vcodec);
  const container = fmt.container && fmt.container !== 'unknown' ? fmt.container : null;
  const parts = [];
  if (codec) parts.push(codec);
  if (container) parts.push(container);
  return parts.join(' · ') || fmt.label || fmt.id;
}

/** Metadados de uma opção de formato: bitrate, fps, codec de áudio. */
export function formatOptionDetail(fmt) {
  if (!fmt) return '';
  const parts = [];
  const bitrate = formatBitrate(fmt.media_type === 'audio' ? (fmt.abr ?? fmt.tbr) : fmt.tbr);
  if (bitrate) parts.push(bitrate);
  if (fmt.fps && fmt.fps >= 50) parts.push(`${Math.round(fmt.fps)}fps`);
  if (fmt.media_type !== 'audio' && fmt.acodec && fmt.acodec !== 'none') {
    const acodec = codecName(fmt.acodec);
    if (acodec) parts.push(acodec);
  }
  if (fmt.audio_channels) parts.push(`${fmt.audio_channels}ch`);
  return parts.join(' · ');
}

// --------------------------------------------------------------------------
// Listas de escolha (vídeo / áudio / legendas)
// --------------------------------------------------------------------------
/**
 * Desenha uma lista de opções de rádio a partir de descritores neutros.
 *
 * Cada descriptor: { value, badge, title, detail, tag, size, tone }.
 * `tone: 'auto'` marca a opção "Automático" (verde).
 */
export function renderOptions(container, options, { groupName, selectedValue = null, onSelect } = {}) {
  container.innerHTML = '';
  if (!options.length) {
    const empty = document.createElement('p');
    empty.className = 'opt-empty';
    empty.textContent = container.dataset.empty || 'Nenhuma opção disponível.';
    container.appendChild(empty);
    return;
  }

  for (const opt of options) {
    const label = document.createElement('label');
    label.className = 'opt';
    if (opt.tone) label.classList.add(`opt-${opt.tone}`);
    if (opt.value === selectedValue) label.classList.add('selected');

    const tagHtml = opt.tag
      ? `<span class="opt-tag ${escapeAttr(opt.tag.className ?? '')}">${escapeHtml(opt.tag.text)}</span>`
      : '';
    const sizeHtml = opt.size ? `<span class="opt-size">${escapeHtml(opt.size)}</span>` : '';
    const detailHtml = opt.detail ? `<span>${escapeHtml(opt.detail)}</span>` : '';

    label.innerHTML = `
      <input type="radio" name="${escapeAttr(groupName)}" value="${escapeAttr(opt.value)}" />
      <span class="opt-dot" aria-hidden="true"></span>
      <span class="opt-badge">${escapeHtml(opt.badge ?? '')}</span>
      <span class="opt-desc">
        <strong>${escapeHtml(opt.title ?? '')}</strong>
        ${detailHtml}
      </span>
      ${tagHtml}
      ${sizeHtml}
    `;

    const input = label.querySelector('input');
    input.checked = opt.value === selectedValue;
    input.addEventListener('change', () => {
      container.querySelectorAll('.opt').forEach((el) => el.classList.remove('selected'));
      label.classList.add('selected');
      onSelect?.(opt);
    });

    container.appendChild(label);
  }
}

// --------------------------------------------------------------------------
// Painel "Vais obter"
// --------------------------------------------------------------------------
/**
 * Resumo do que vai ser descarregado.
 * Cada linha: { axis, value, note, auto, muted }.
 */
export function renderSummary(container, { badge = '', rows = [] } = {}) {
  container.innerHTML = '';

  const head = document.createElement('div');
  head.className = 'summary-head';
  head.innerHTML = `
    <span class="summary-check" aria-hidden="true">✓</span>
    <span class="summary-title">Vais obter</span>
    ${badge ? `<span class="summary-badge">${escapeHtml(badge)}</span>` : ''}
  `;
  container.appendChild(head);

  const list = document.createElement('ul');
  list.className = 'summary-list';

  for (const row of rows) {
    const item = document.createElement('li');
    item.className = 'summary-row';
    if (row.muted) item.classList.add('is-muted');

    const value = row.auto
      ? `<span class="summary-auto">Automático →</span> ${escapeHtml(row.value)}`
      : escapeHtml(row.value);

    item.innerHTML = `
      <span class="summary-axis">${escapeHtml(row.axis)}</span>
      <span class="summary-value">${value}</span>
      ${row.note ? `<span class="summary-note">(${escapeHtml(row.note)})</span>` : ''}
    `;
    list.appendChild(item);
  }

  container.appendChild(list);
}

// --------------------------------------------------------------------------
// Progresso
// --------------------------------------------------------------------------
const PHASE_LABELS = {
  idle: 'A preparar',
  queued: 'Em fila',
  extracting: 'A extrair metadados',
  downloading: 'A descarregar',
  merging: 'A processar',
  finalizing: 'A finalizar',
  done: 'Concluído',
};

export function setProgress({ percent = 0, phase = 'idle', downloaded = 0, total = null, speed = 0, eta = null, message = '' } = {}) {
  const bar = $('#progress-bar');
  const barIndeterminate = $('#progress-bar-indeterminate');
  const pct = $('#progress-percent');
  const phaseEl = $('#progress-phase');
  const sizeEl = $('#progress-size');
  const speedEl = $('#progress-speed');
  const etaEl = $('#progress-eta');
  const msgEl = $('#progress-message');

  const known = total !== null && total > 0;
  if (bar && barIndeterminate) {
    if (known) {
      bar.classList.remove('hidden');
      barIndeterminate.classList.add('hidden');
      bar.style.width = `${Math.min(100, Math.max(0, percent))}%`;
    } else {
      bar.classList.add('hidden');
      barIndeterminate.classList.remove('hidden');
    }
  }

  if (pct) pct.textContent = known ? `${percent.toFixed(1)}%` : '—';
  if (phaseEl) phaseEl.textContent = PHASE_LABELS[phase] ?? phase;
  if (sizeEl) {
    sizeEl.textContent = known
      ? `${formatBytes(downloaded)} / ${formatBytes(total)}`
      : formatBytes(downloaded);
  }
  if (speedEl) speedEl.textContent = formatSpeed(speed);
  if (etaEl) etaEl.textContent = eta === null ? '—' : formatEta(eta);
  if (msgEl) msgEl.textContent = message ?? '';
}

export const PLATFORM_RULES = [
  { match: /youtube\.com|youtu\.be/i, name: 'YouTube', color: '#ff2a2a', bg: 'rgba(255, 42, 42, 0.16)' },
  { match: /tiktok\.com/i, name: 'TikTok', color: '#00f2fe', bg: 'rgba(0, 242, 254, 0.16)' },
  { match: /instagram\.com/i, name: 'Instagram', color: '#e1306c', bg: 'rgba(225, 48, 108, 0.16)' },
  { match: /twitter\.com|x\.com/i, name: 'X / Twitter', color: '#e6edf7', bg: 'rgba(255, 255, 255, 0.12)' },
  { match: /reddit\.com/i, name: 'Reddit', color: '#ff4500', bg: 'rgba(255, 69, 0, 0.16)' },
  { match: /twitch\.tv/i, name: 'Twitch', color: '#9146ff', bg: 'rgba(145, 70, 255, 0.16)' },
  { match: /soundcloud\.com/i, name: 'SoundCloud', color: '#ff5500', bg: 'rgba(255, 85, 0, 0.16)' },
  { match: /facebook\.com|fb\.watch/i, name: 'Facebook', color: '#1877f2', bg: 'rgba(24, 119, 242, 0.16)' },
  { match: /pinterest\.com|pin\.it/i, name: 'Pinterest', color: '#e60023', bg: 'rgba(230, 0, 35, 0.16)' },
  { match: /bilibili\.com/i, name: 'Bilibili', color: '#23ade5', bg: 'rgba(35, 173, 229, 0.16)' },
  { match: /vimeo\.com/i, name: 'Vimeo', color: '#1ab7ea', bg: 'rgba(26, 183, 234, 0.16)' },
  { match: /dailymotion\.com|dai\.ly/i, name: 'Dailymotion', color: '#0066dc', bg: 'rgba(0, 102, 220, 0.16)' },
  { match: /france\.tv/i, name: 'France TV', color: '#0055a5', bg: 'rgba(0, 85, 165, 0.16)' },
  { match: /bandcamp\.com/i, name: 'Bandcamp', color: '#1da0c3', bg: 'rgba(29, 160, 195, 0.16)' },
  { match: /rumble\.com/i, name: 'Rumble', color: '#85c742', bg: 'rgba(133, 199, 66, 0.16)' },
  { match: /\.m3u8($|\?)/i, name: 'Stream HLS', color: '#22d3a7', bg: 'rgba(34, 211, 167, 0.16)' },
  { match: /\.mpd($|\?)/i, name: 'Stream DASH', color: '#f59e0b', bg: 'rgba(245, 158, 11, 0.16)' },
  { match: /\.(mp4|m4v|mov|webm|mp3|m4a|wav|opus)($|\?)/i, name: 'Direct Stream', color: '#818cf8', bg: 'rgba(129, 140, 248, 0.16)' },
];

export function detectPlatform(url) {
  if (!url || typeof url !== 'string') return null;
  const clean = url.trim();
  for (const rule of PLATFORM_RULES) {
    if (rule.match.test(clean)) return rule;
  }
  return { name: 'Site Universal', color: '#22d3a7', bg: 'rgba(34, 211, 167, 0.14)' };
}

export function renderPlatformBadge(badgeEl, url) {
  if (!badgeEl) return;
  const match = url ? detectPlatform(url) : null;
  if (!match || !url.trim()) {
    badgeEl.textContent = '';
    badgeEl.classList.add('hidden');
    return;
  }
  badgeEl.textContent = match.name;
  badgeEl.style.color = match.color;
  badgeEl.style.backgroundColor = match.bg;
  badgeEl.style.border = `1px solid ${match.color}44`;
  badgeEl.classList.remove('hidden');
}

export function showResult({ filename, size, href, warnings = [], media = null }) {
  const box = $('#result-box');
  if (!box) return;
  $('#result-filename').textContent = filename ?? '';
  $('#result-size').textContent = size ? formatBytes(size) : '';
  const link = $('#result-link');
  link.href = href;
  link.setAttribute('download', filename ?? '');
  showWarnings(warnings);

  // Player embutido para reprodução imediata
  const playerContainer = $('#result-player-container');
  if (playerContainer) {
    playerContainer.innerHTML = '';
    const ext = (filename || '').split('.').pop().toLowerCase();
    const isVideo = ['mp4', 'webm', 'mkv', 'mov', 'm4v'].includes(ext);
    const isAudio = ['mp3', 'm4a', 'opus', 'wav', 'ogg', 'aac', 'flac'].includes(ext);

    if (isVideo) {
      const video = document.createElement('video');
      video.controls = true;
      video.playsInline = true;
      video.preload = 'metadata';
      video.src = href;
      if (media?.thumbnail) video.poster = media.thumbnail;
      playerContainer.appendChild(video);
      playerContainer.classList.remove('hidden');
    } else if (isAudio) {
      const audio = document.createElement('audio');
      audio.controls = true;
      audio.preload = 'metadata';
      audio.src = href;
      playerContainer.appendChild(audio);
      playerContainer.classList.remove('hidden');
    } else {
      playerContainer.classList.add('hidden');
    }
  }

  box.classList.remove('hidden');
}

export function showWarnings(warnings = []) {
  const box = $('#result-warnings');
  if (!box) return;
  const list = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
  box.innerHTML = list.map((w) => `<li>${escapeHtml(w)}</li>`).join('');
  box.classList.toggle('hidden', list.length === 0);
}

export function hideResult() {
  $('#result-box')?.classList.add('hidden');
  $('#result-warnings')?.classList.add('hidden');
  const player = $('#result-player-container');
  if (player) {
    player.innerHTML = '';
    player.classList.add('hidden');
  }
}

export function renderSessionHistory(container, history = [], onSelect = null) {
  if (!container) return;
  container.innerHTML = '';
  if (!history.length) {
    container.innerHTML = '<p class="hint">Nenhum download concluído nesta sessão ainda.</p>';
    return;
  }

  for (const item of history) {
    const el = document.createElement('div');
    el.className = 'history-item';
    el.innerHTML = `
      <div class="history-left">
        ${item.thumbnail ? `<img class="history-thumb" src="${escapeAttr(item.thumbnail)}" alt="" />` : '<div class="history-thumb" style="display:grid;place-items:center;color:#666;">▶</div>'}
        <div class="history-info">
          <div class="history-title" title="${escapeAttr(item.title || item.filename)}">${escapeHtml(item.title || item.filename)}</div>
          <div class="history-meta">${escapeHtml(item.platform || 'Site')} · ${formatBytes(item.size)} · ${new Date(item.time).toLocaleTimeString('pt-PT')}</div>
        </div>
      </div>
      <div class="history-actions">
        <a class="btn btn-sm btn-primary" href="${escapeAttr(item.href)}" download="${escapeAttr(item.filename)}">Descarregar</a>
      </div>
    `;
    container.appendChild(el);
  }
}

// --------------------------------------------------------------------------
// Utilitários
// --------------------------------------------------------------------------
export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

export function escapeAttr(value) {
  return escapeHtml(value);
}
