/**
 * Cliente da API REST do MyArchive Downloader.
 *
 * Responsável por:
 *  - normalizar os diferentes formatos de erro da API num único `ApiError`;
 *  - injectar a API key quando configurada;
 *  - expor helpers para construir URLs de download/stream.
 */

const DEFAULTS = {
  apiBase: '/api',
  apiKey: '',
};

export class ApiError extends Error {
  constructor(message, { code = 'error', status = 0, details = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

export class ApiClient {
  constructor(options = {}) {
    this.apiBase = (options.apiBase ?? DEFAULTS.apiBase).replace(/\/+$/, '');
    this.apiKey = options.apiKey ?? DEFAULTS.apiKey;
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  /** Lê a configuração persistida no browser (usada pela extensão e pelo painel). */
  static fromStorage(storage = globalThis.localStorage) {
    try {
      return new ApiClient({
        apiBase: storage.getItem('mad.apiBase') ?? DEFAULTS.apiBase,
        apiKey: storage.getItem('mad.apiKey') ?? DEFAULTS.apiKey,
      });
    } catch {
      return new ApiClient();
    }
  }

  save(storage = globalThis.localStorage) {
    try {
      storage.setItem('mad.apiBase', this.apiBase);
      storage.setItem('mad.apiKey', this.apiKey);
    } catch {
      /* modo privado do browser — ignora */
    }
  }

  // ------------------------------------------------------------------ //
  _url(path, params = {}) {
    const url = new URL(`${this.apiBase}${path}`, globalThis.location?.href ?? 'http://localhost');
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, value);
    }
    return url.toString();
  }

  _headers(extra = {}) {
    const headers = { Accept: 'application/json', ...extra };
    if (this.apiKey) headers['X-API-Key'] = this.apiKey;
    return headers;
  }

  async _request(path, { method = 'GET', body, params, signal, raw = false } = {}) {
    const init = {
      method,
      headers: this._headers(body ? { 'Content-Type': 'application/json' } : {}),
      signal,
    };
    if (body) init.body = JSON.stringify(body);

    let response;
    try {
      response = await this.fetchImpl(this._url(path, params), init);
    } catch (err) {
      if (err?.name === 'AbortError') throw err;
      throw new ApiError('Não foi possível contactar o servidor.', {
        code: 'network_error',
        details: String(err),
      });
    }

    if (raw) {
      if (!response.ok) throw await this._toError(response);
      return response;
    }

    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = { message: text };
      }
    }

    if (!response.ok) throw this._normalize(payload, response.status);
    return payload;
  }

  _normalize(payload, status) {
    // Formato do core: { error: { code, message, details } }
    if (payload?.error?.message) {
      return new ApiError(payload.error.message, {
        code: payload.error.code,
        status,
        details: payload.error.details ?? null,
      });
    }
    // Formato do FastAPI: { detail: { code, message } } ou { detail: [...] }
    if (payload?.detail) {
      if (Array.isArray(payload.detail)) {
        const first = payload.detail[0];
        const loc = Array.isArray(first?.loc) ? first.loc.slice(1).join('.') : '';
        return new ApiError(`${loc ? loc + ': ' : ''}${first?.msg ?? 'Pedido inválido'}`, {
          code: 'validation_error',
          status,
          details: payload.detail,
        });
      }
      return new ApiError(payload.detail.message ?? 'Erro do servidor.', {
        code: payload.detail.code ?? 'error',
        status,
        details: payload.detail,
      });
    }
    return new ApiError(`Erro ${status}.`, { code: 'error', status });
  }

  async _toError(response) {
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      payload = { message: text };
    }
    return this._normalize(payload, response.status);
  }

  // ------------------------------------------------------------------ //
  // Endpoints
  // ------------------------------------------------------------------ //

  /** Extrai metadados e formatos de um URL. */
  async extractInfo(url, extra = {}) {
    return this._request('/extract-info', {
      method: 'POST',
      body: { url, ...extra },
    });
  }

  /** Cria um job de download. Devolve { job_id, websocket, poll }. */
  async createDownload(payload) {
    return this._request('/download', { method: 'POST', body: payload });
  }

  /** Estado do job. */
  async getJob(jobId) {
    return this._request(`/jobs/${encodeURIComponent(jobId)}`);
  }

  /** Cancela o job. */
  async cancelJob(jobId) {
    return this._request(`/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
  }

  /** Lista os extractores disponíveis. */
  async listExtractors() {
    return this._request('/formats');
  }

  /** Estado do serviço. */
  async health() {
    return this._request('/health');
  }

  // ------------------------------------------------------------------ //
  // Motor de atualização automática (SyncBot)
  // ------------------------------------------------------------------ //

  /** Estado atual do motor e bot de sincronização. */
  async engineStatus() {
    return this._request('/engine/status');
  }

  /** Verifica se há novas versões do motor de streaming. */
  async engineCheck() {
    return this._request('/engine/check', { method: 'POST' });
  }

  /** Inicia atualização automática do motor em background. */
  async engineUpdate() {
    return this._request('/engine/update', { method: 'POST' });
  }

  /** Reverte o motor para a versão anterior (rollback). */
  async engineRollback() {
    return this._request('/engine/rollback', { method: 'POST' });
  }

  // ------------------------------------------------------------------ //
  /** URL directa para descarregar o ficheiro de um job. */
  fileUrl(jobId) {
    return this._url(`/download/${encodeURIComponent(jobId)}`, this.apiKey ? { api_key: this.apiKey } : {});
  }

  /** URL de streaming (proxy) para media remota. */
  streamUrl(remoteUrl) {
    return this._url('/stream', {
      url: remoteUrl,
      api_key: this.apiKey || undefined,
    });
  }
}
