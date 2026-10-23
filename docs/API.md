# Referência da API

Base URL por omissão: `http://localhost:8000/api`
Documentação interactiva: `/docs` (Swagger) · `/redoc` (ReDoc) · `/openapi.json`

Todas as respostas de erro seguem o mesmo formato:

```json
{
  "error": {
    "code": "no_format_found",
    "message": "Nenhum formato disponível para este URL",
    "details": { "available": ["137", "140", "141"] }
  }
}
```

---

## POST /api/extract-info

Extrai metadados e a lista de formatos de um URL.

**Request**
```json
{
  "url": "https://www.youtube.com/watch?v=XXXXXXXX",
  "headers": {},
  "cookies": { "SID": "..." },
  "cookies_from_browser": "chrome",
  "preferred_extractor": "youtube"
}
```

| Campo | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `url` | string | sim | URL do recurso (4–2048 chars) |
| `headers` | object | não | Headers HTTP extra a enviar |
| `cookies` | object | não | Cookies de sessão; limitados ao domínio inicial |
| `cookies_from_browser` | string | não | `chrome` \| `firefox` \| `edge` \| `brave` \| `safari`; lido na máquina do servidor, mantendo o âmbito original dos cookies |
| `preferred_extractor` | string | não | Forçar um extractor específico |

**Response 200**
```json
{
  "id": "9f2c…",
  "extractor": "youtube",
  "webpage_url": "https://www.youtube.com/watch?v=XXXXXXXX",
  "original_url": "https://www.youtube.com/watch?v=XXXXXXXX",
  "title": "Título do vídeo",
  "description": "…",
  "thumbnail": "https://i.ytimg.com/vi/XXXXXXXX/hqdefault.jpg",
  "duration": 213.45,
  "uploader": "Canal",
  "upload_date": "2025-03-11",
  "is_live": false,
  "formats": [
    {
      "id": "137",
      "url": "https://…/137.mp4",
      "media_type": "video",
      "container": "mp4",
      "protocol": "https",
      "width": 1920,
      "height": 1080,
      "fps": 30.0,
      "tbr": 4392.5,
      "vcodec": "avc1.640028",
      "acodec": "none",
      "filesize": 12582912,
      "manifest_url": null,
      "manifest_kind": null,
      "unavailable": false
    }
  ],
  "subtitles": {
    "pt": [{ "language": "pt", "ext": "vtt", "url": "https://…", "automatic": false }]
  },
  "created_at": 1760000000.0
}
```

**Erros**: `400 unsupported_url` · `422 extraction_failed` · `401 unauthorized` · `429 rate_limited`

---

## POST /api/download

Cria um job de download. Responde imediatamente (202); o progresso é
consumido por WebSocket ou por polling.

**Request**
```json
{
  "url": "https://www.youtube.com/watch?v=XXXXXXXX",
  "mode": "auto",
  "video_format_id": null,
  "audio_format_id": null,
  "subtitle_lang": "auto",
  "container": "mp4",
  "extract_audio": false,
  "audio_format": "mp3",
  "max_height": 1080,
  "merge": true,
  "headers": {},
  "cookies": {},
  "cookies_from_browser": "chrome",
  "filename": null
}
```

| Campo | Tipo | Default | Descrição |
|---|---|---|---|
| `url` | string | — | URL a descarregar |
| `format_id` | string | `null` | Id exacto de um formato (de `extract-info`). Ignorado se `video_format_id`/`audio_format_id` forem indicados |
| `video_format_id` | string | `null` | Id exacto do stream de **vídeo**. `null` = o programa escolhe |
| `audio_format_id` | string | `null` | Id exacto da faixa de **áudio**. `null` = o programa escolhe |
| `subtitle_lang` | string | `null` | `null` = sem legendas · `"auto"` = o programa escolhe · `"<lang>"` = todas as faixas desse idioma · `"all"` = todas · `"<grupo>:<idioma>:<nome>"` = uma faixa exacta (o `track_id` devolvido em `extract-info`) |
| `mode` | `auto`\|`video`\|`both`\|`audio`\|`original` | `auto` | `auto` deixa o programa escolher tudo; `both` força streams separados + merge; `original` entrega o stream tal como está |
| `container` | string | `mp4` | Container de saída (`mp4`, `mkv`, `webm`) |
| `extract_audio` | bool | `false` | Extrair apenas o áudio |
| `audio_format` | `mp3`\|`m4a`\|`opus`\|`wav` | `mp3` | Formato do áudio extraído |
| `max_height` | int | — | Limitar a resolução máxima |
| `merge` | bool | `true` | Liga a melhor faixa de áudio a um formato **video-only** (comportamento `-f best` do yt-dlp). `false` entrega vídeo sem som |
| `cookies_from_browser` | string | `null` | Reutilizar a sessão autenticada do browser na extracção, download e renovação (`chrome`, `firefox`, `edge`, `brave` ou `safari`) |

### Selecção em modo automático

Com `mode=auto` (predefinição) **e** sem selectors explícitos, o serviço
escolhe sozinho:

| Eixo | Regra |
|---|---|
| Vídeo | Prefere um stream **só de vídeo** (para poder anexar o melhor áudio); desempate por `(height, tbr, filesize)` |
| Áudio | Melhor `(abr, tbr)`; em `mp4` é preferido o **AAC** (o Opus dentro de MP4 não é reproduzido por muitos players) |
| Legendas | Ordem de idioma `pt, pt-pt, pt-br, en, en-us, en-gb, es, fr, de, it`; dentro do mesmo idioma, as faixas **manuais** ganham às automáticas |

Regras de merge (o ficheiro final tem sempre **uma** faixa de vídeo e,
opcionalmente, **uma** de áudio):

- vídeo explícito **com** áudio e sem áudio explícito → **sem merge**;
- vídeo explícito **sem** áudio → o melhor áudio é **anexado**;
- vídeo explícito + áudio explícito → o áudio escolhido **substitui** o que
  o vídeo trazia.

> **Nota sobre `merge`:** os formatos adaptativos do YouTube (1080p, 4K…) são
> *video-only*. Com `merge=true` (predefinição) o serviço descarrega também a
> melhor faixa de áudio compatível com o `container` e funde tudo num único
> ficheiro.
>
> O `merge` exige o FFmpeg instalado. Sem ele, o serviço entrega o vídeo sem
> som e adiciona um aviso em `result.warnings`.

### Legendas

As legendas são descarregadas, convertidas para **SRT** (o WebVTT e o JSON3
do YouTube são normalizados) e anexadas ao ficheiro final numa passagem
única do FFmpeg, **sem recodificar o vídeo** (`mov_text` em MP4, `srt` em
MKV; WebM não suporta texto).

Uma legenda em falta **nunca** invalida o download: o ficheiro é entregue sem
ela e a razão fica em `result.warnings`.

> O mux exige o FFmpeg instalado. Sem ele, o ficheiro é entregue sem
> legendas e com o aviso `FFmpeg indisponível: ficheiro entregue sem legendas`.

**Response 202**
```json
{
  "job_id": "4c1a9e…",
  "status": "queued",
  "websocket": "/api/ws/jobs/4c1a9e…",
  "poll": "/api/jobs/4c1a9e…"
}
```

---

## WS /api/ws/jobs/{job_id}

WebSocket que emite eventos de progresso.

**Mensagens recebidas**
```json
{
  "job_id": "4c1a9e…",
  "status": "running",
  "phase": "downloading",
  "percent": 42.73,
  "downloaded_bytes": 5368709,
  "total_bytes": 12582912,
  "speed": 1048576.0,
  "eta": 6.9,
  "segment_index": 18,
  "segment_total": 54,
  "message": "A descarregar…",
  "filename": "Titulo_video.mp4",
  "timestamp": 1760000000.0
}
```

| `status` | `phase` | Significado |
|---|---|---|
| `queued` | `idle` | Job criado, à espera |
| `queued` | `queued` | Job à espera de vaga (limite `MAD_MAX_CONCURRENT_JOBS`) |
| `running` | `extracting` | A extrair metadados |
| `running` | `downloading` | A transferring segmentos/bytes |
| `running` | `merging` | A fundir áudio + vídeo (FFmpeg) |
| `running` | `finalizing` | A preparar o ficheiro final |
| `completed` | `done` | Sucesso — usar `GET /api/download/{job_id}` |
| `failed` | `done` | Erro (ver `error` no `GET /api/jobs/{id}`) |
| `cancelled` | `done` | Cancelado pelo utilizador |

Ao subscrever, o servidor **replay** dos últimos 50 eventos, por isso um
cliente que se liga a meio não perde o início. A ligação termina
automaticamente no evento terminal.

Exemplo (JavaScript):
```js
const ws = new WebSocket(`ws://localhost:8000/api/ws/jobs/${jobId}`);
ws.onmessage = (e) => {
  const ev = JSON.parse(e.data);
  if (ev.status === 'completed') window.location = `/api/download/${jobId}`;
};
```

---

## GET /api/jobs/{job_id}

Estado completo do job (mesmo formato do evento de progresso + `result`).

```json
{
  "id": "4c1a9e…",
  "url": "https://…",
  "status": "completed",
  "phase": "done",
  "created_at": 1760000000.0,
  "updated_at": 1760000042.1,
  "title": "Título do vídeo",
  "filename": "Titulo_video.mp4",
  "error": null,
  "progress": { "percent": 100.0, "…": "…" },
  "result": {
    "job_id": "4c1a9e…",
    "status": "completed",
    "filename": "Titulo.mp4",
    "path": "/app/data/downloads/Titulo.mp4",
    "size": 12582912,
    "mime_type": "video/mp4",
    "download_url": "/api/download/4c1a9e…",
    "thumbnail": "https://…",
    "title": "Título do vídeo",
    "duration": 213.45,
    "warnings": []
  }
}
```

### `result.warnings`

Avisos não bloqueantes. Exemplos reais:

| Aviso | Causa |
|---|---|
| `FFmpeg indisponível: vídeo e áudio entregues em ficheiros separados` | Sem FFmpeg não é possível fundir áudio + vídeo |
| `O ficheiro é um MP4 fragmentado (formato do YouTube). Alguns players não o reproduzem…` | FFmpeg ausente e o formato adaptativo do YouTube é fMP4 |
| `Remux falhou (…); entregue o original` | A conversão de fMP4 → MP4 progressivo falhou |

---

## DELETE /api/jobs/{job_id}

Cancela um job em execução. Devolve `{"job_id": "…", "status": "cancelled"}`.

---

## GET /api/download/{job_id}

Entrega o ficheiro final. Suporta **Range requests** (HTTP 206), o que
permite retomar downloads interrompidos e fazer seeking no browser.

```
Accept-Ranges: bytes
Content-Disposition: attachment; filename="Titulo_video.mp4"
```

## HEAD /api/download/{job_id}

Metadados do ficheiro sem o transferir.

---

## GET /api/stream?url=…

Proxy de streaming para media remota — resolve problemas de CORS e permite
reproduzir HLS/DASH directamente no browser (com `hls.js`).

Suporta `Range`. Exemplo:
```html
<video controls src="/api/stream?url=https%3A%2F%2Fcdn.exemplo%2Fvideo.mp4"></video>
```

## GET /api/probe?url=…

Devolve os cabeçalhos HTTP de um recurso remoto (`status`, `headers`,
`final_url`).

---

## GET /api/health

```json
{
  "status": "ok",
  "app": "MyArchive Downloader",
  "uptime_seconds": 4210.5,
  "version": "1.0.0",
  "python": "3.12.3",
  "platform": "Linux",
  "ffmpeg": "/usr/bin/ffmpeg",
  "extractors": ["youtube", "vimeo", "francetv", "dailymotion", "direct", "generic"],
  "cache_entries": 12
}
```

## GET /api/storage

Uso de disco, directórios e número de ficheiros.

## GET /api/formats

Extractores registados com as suas regex e prioridades.

---

## Autenticação

Se `MAD_API_KEY` estiver definido, todos os endpoints (excepto `/api/health`
e `/`) exigem:

```
X-API-Key: <chave>
```
ou
```
Authorization: Bearer <chave>
```
ou (para downloads/stream) `?api_key=<chave>`.

Sem API key, o serviço está aberto — aceitável apenas em redes internas.
