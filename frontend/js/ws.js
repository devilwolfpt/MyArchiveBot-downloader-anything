/**
 * Cliente WebSocket com reconexão automática.
 *
 * O backend publica eventos de progresso do download; se a ligação cair
 * (sleep do portátil, mudança de rede, proxy), o socket volta a ligar-se
 * com backoff exponencial. Como o servidor mantém um buffer dos últimos
 * eventos, a reconexão retoma exactamente onde ficou.
 */
export class ProgressSocket {
  constructor({ url, apiKey = '', onMessage, onStatus, maxRetries = 8, baseDelay = 700 }) {
    this.url = url;
    this.apiKey = apiKey;
    this.onMessage = onMessage ?? (() => {});
    this.onStatus = onStatus ?? (() => {});
    this.maxRetries = maxRetries;
    this.baseDelay = baseDelay;

    this.ws = null;
    this.closed = false;
    this.attempts = 0;
    this.retryTimer = null;
  }

  /** Constrói o URL do WebSocket a partir do endpoint HTTP do job. */
  static buildUrl(jobId, { base = '', apiKey = '' } = {}) {
    const httpBase = base || `${globalThis.location?.origin ?? 'http://localhost'}/api`;
    const wsBase = httpBase.replace(/^http/, 'ws').replace(/\/+$/, '');
    const url = new URL(`${wsBase}/ws/jobs/${encodeURIComponent(jobId)}`);
    if (apiKey) url.searchParams.set('api_key', apiKey);
    return url.toString();
  }

  connect() {
    this.closed = false;
    this._open();
  }

  _open() {
    if (this.closed) return;
    this.onStatus(this.attempts === 0 ? 'connecting' : 'reconnecting');

    let socket;
    try {
      socket = new WebSocket(this.url);
    } catch (err) {
      this._scheduleRetry(String(err));
      return;
    }
    this.ws = socket;

    socket.onopen = () => {
      this.attempts = 0;
      this.onStatus('open');
    };

    socket.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      this.onMessage(payload);
    };

    socket.onerror = () => {
      // `onclose` trata a reconexão; evitamos duplicar lógica aqui.
    };

    socket.onclose = (event) => {
      this.ws = null;
      if (this.closed) {
        this.onStatus('closed');
        return;
      }
      // 1000 = encerramento normal (ex.: job terminado).
      if (event.code === 1000) {
        this.onStatus('closed');
        return;
      }
      this._scheduleRetry(`código ${event.code}`);
    };
  }

  _scheduleRetry(reason) {
    if (this.closed) return;
    if (this.attempts >= this.maxRetries) {
      this.onStatus('failed');
      return;
    }
    const delay = Math.min(this.baseDelay * 2 ** this.attempts, 15000);
    this.attempts += 1;
    this.onStatus('reconnecting', { attempt: this.attempts, delay, reason });
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => this._open(), delay);
  }

  close() {
    this.closed = true;
    clearTimeout(this.retryTimer);
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) {
      this.ws.close(1000, 'client');
    }
    this.ws = null;
    this.onStatus('closed');
  }
}
