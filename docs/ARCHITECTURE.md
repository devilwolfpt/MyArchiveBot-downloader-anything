# Arquitectura

## 1. Visão geral

```
┌────────────────────────────────────────────────────────────────────────────┐
│                         CLIENTES (Browser / Mobile)                        │
│                                                                            │
│   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────────┐  │
│   │  Painel Web      │   │  Extensão MV3    │   │  Motor de Busca      │  │
│   │  /app (React-ish)│   │  (popup + SW)    │   │  (integr. server-side)│  │
│   └────────┬─────────┘   └────────┬─────────┘   └──────────┬───────────┘  │
└────────────┼──────────────────────┼───────────────────────┼───────────────┘
             │  REST  /api/extract-info, /api/download      │
             │  WSS   /api/ws/jobs/{id}   (progresso)      │
             ▼                                             ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                          EDGE  (Nginx / Caddy)                             │
│      TLS · rate limiting · proxy WebSocket (Upgrade) · buffering off      │
└────────────┬───────────────────────────────────────────────┬───────────────┘
             │                                               │
             ▼                                               ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                        API ENGINE  (FastAPI / Uvicorn)                    │
│                                                                            │
│  ┌──────────────────┐   ┌───────────────────┐   ┌────────────────────────┐ │
│  │ api/routes/      │   │ api/deps.py       │   │ Exception handlers     │ │
│  │  extract.py      │   │  auth · rate-limit│   │  DownloaderError → JSON│ │
│  │  download.py     │   │  DI de serviços   │   └────────────────────────┘ │
│  │  stream.py       │   └───────────────────┘                            │
│  │  health.py       │                                                    │
│  └────────┬─────────┘                                                    │
│           │                                                              │
│  ┌────────▼─────────────────────────────────────────────────────────────┐ │
│  │                        SERVICES  (orquestração)                     │ │
│  │   ExtractionService          DownloadService (pipeline)             │ │
│  │   cache TTL · cookies        extract→select→plan→download→merge     │ │
│  └────────┬─────────────────────────────────────────────────────────────┘ │
│           │                                                              │
│  ┌────────▼─────────────────────────────────────────────────────────────┐ │
│  │                              CORE                                   │ │
│  │                                                                    │ │
│  │  ┌───────────────┐  ┌──────────────────┐  ┌──────────────────────┐  │ │
│  │  │ extractors/   │  │ http/            │  │ manifest/            │  │ │
│  │  │  base.py      │  │  client.py       │  │  hls.py  (m3u8)     │  │ │
│  │  │  registry.py  │  │  headers.py      │  │  dash.py (mpd)       │  │ │
│  │  │  youtube.py   │  │  cookies.py      │  │  parser.py           │  │ │
│  │  │  vimeo.py     │  │  (UA rotation,   │  └──────────────────────┘  │ │
│  │  │  francetv.py  │  │   retry, 403     │                            │ │
│  │  │  dailymotion  │  │   refresh, SSRF) │  ┌──────────────────────┐  │ │
│  │  │  direct.py    │  └──────────────────┘  │ downloader/           │  │ │
│  │  │  generic.py   │                        │  chunk.py (parallel,  │  │ │
│  │  └───────────────┘                        │   Range, resume)     │  │ │
│  │                                           │  plan.py (DownloadPlan)│ │ │
│  │                                           │  crypto.py (AES-128) │  │ │
│  │                                           │  manager.py (jobs)    │  │ │
│  │                                           │  subtitle.py (SRT)    │  │ │
│  │                                           │  storage.py          │  │ │
│  │                                           └──────────────────────┘  │ │
│  │                                           ┌──────────────────────┐  │ │
│  │                                           │ postprocess/         │  │ │
│  │                                           │  ffmpeg.py (wrapper) │  │ │
│  │                                           │  processor.py        │  │ │
│  │                                           └──────────┬───────────┘  │ │
│  └──────────────────────────────────────────────────────┼──────────────┘ │
└─────────────────────────────────────────────────────────┼────────────────┘
                                                          ▼
                                            ┌───────────────────────────┐
                                            │  FFmpeg (subprocess)      │
                                            │  merge / remux / mp3      │
                                            │  mux legendas (SRT)       │
                                            └───────────────────────────┘
```

## 2. Árvore de ficheiros

```
myarchive-downloader/
│
├── backend/                                  # API Engine (Python 3.11+)
│   ├── app/
│   │   ├── main.py                           # App factory, lifespan, error handlers
│   │   ├── config.py                         # Settings (pydantic-settings, env MAD_*)
│   │   │
│   │   ├── api/                              # Camada HTTP
│   │   │   ├── deps.py                       # DI, API key, rate limiter
│   │   │   └── routes/
│   │   │       ├── extract.py                 # POST /api/extract-info
│   │   │       ├─ download.py                 # POST /api/download (cria job)
│   │   │       ├── stream.py                  # GET  /api/stream (proxy Range)
│   │   │       └── health.py                  # GET  /api/health, /api/storage
│   │   │
│   │   ├── services/                         # Orquestração (não conhece HTTP)
│   │   │   ├── extraction.py                  # Cache TTL, cookies, escolha de extractor
│   │   │   └── pipeline.py                    # extract → select → plan → download → merge
│   │   │
│   │   ├── core/                             # Motor (independente do FastAPI)
│   │   │   ├── exceptions.py                  # Hierarquia de erros + códigos HTTP
│   │   │   ├── models.py                      # Contratos Pydantic (MediaInfo, StreamFormat…)
│   │   │   │
│   │   │   ├── http/
│   │   │   │   ├── client.py                  # AsyncClient: retry, 403 refresh, SSRF guard
│   │   │   │   ├── headers.py                 # Rotação de User-Agent, Referer
│   │   │   │   └── cookies.py                 # Netscape cookies.txt, cookies de browser
│   │   │   │
│   │   │   ├── extractors/
│   │   │   │   ├── base.py                    # BaseExtractor + ExtractContext
│   │   │   │   ├── registry.py                # Routing por regex + prioridade
│   │   │   │   ├── youtube.py                 # InnerTube (multi-client)
│   │   │   │   ├── vimeo.py                   # playerConfig
│   │   │   │   ├── francetv.py                # config JSON da página
│   │   │   │   ├── dailymotion.py             # /player/metadata/video/{id}
│   │   │   │   ├── direct.py                  # .mp4/.mp3/.m3u8/.mpd directos
│   │   │   │   └── generic.py                 # Fallback HTML (og:, JSON-LD, <video>)
│   │   │   │
│   │   │   ├── manifest/
│   │   │   │   ├── hls.py                     # m3u8 master + media, AES-128, byterange
│   │   │   │   ├── dash.py                    # MPD: SegmentTemplate/Timeline/List/Base
│   │   │   │   └── parser.py                  # Dispatcher HLS|DASH → VariantInfo
│   │   │   │
│   │   │   ├── downloader/
│   │   │   │   ├── plan.py                    # DownloadPlan / SegmentPlan + PlanBuilder
│   │   │   │   ├── chunk.py                   # ChunkDownloader (paralelo, Range, resume)
│   │   │   │   ├── crypto.py                  # AES-128-CBC (HLS cifrado)
│   │   │   │   ├── subtitle.py                # Descarga + normalização de legendas (→ SRT)
│   │   │   │   ├── storage.py                 # Paths, ficheiros .part, purga
│   │   │   │   └── manager.py                 # JobManager: eventos, WS fan-out, cancel
│   │   │   │
│   │   │   └── postprocess/
│   │   │       ├── ffmpeg.py                  # Wrapper assíncrono do FFmpeg
│   │   │       └── processor.py               # merge / remux / extract_audio / legendas
│   │   │
│   │   └── __init__.py
│   │
│   ├── tests/                                # 167 testes (unit, integração, API)
│   │   ├── conftest.py
│   │   ├── test_hls_parser.py
│   │   ├── test_dash_parser.py
│   │   ├── test_crypto.py
│   │   ├── test_downloader.py
│   │   ├── test_extractors.py
│   │   ├── test_api.py
│   │   ├├ test_selection.py                # Regras de merge e escolha de formato
│   │   ├├ test_selection_auto.py           # Modo automático + escolha de legendas
│   │   ├├ test_subtitles.py                # Conversão VTT/JSON3 -> SRT
│   │   ├├ test_subtitle_e2e.py             # E2E: mux das legendas (ffprobe)
│   │   └── test_integration.py              # E2E com servidor HTTP real
│   │
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── pyproject.toml
│   └── .env.example
│
├── frontend/                                 # UI (ES modules, sem build step)
│   ├── index.html                            # Painel principal
│   ├── css/style.css
│   ├── js/
│   │   ├── api.js                            # ApiClient (Fetch) + normalização de erros
│   │   ├── ws.js                             # ProgressSocket (reconexão automática)
│   │   ├── ui.js                             # Formatação + renderização DOM
│   │   └── app.js                            # Orquestração do fluxo
│   └── extension/                            # Extensão Chrome/Edge MV3
│       ├── manifest.json
│       ├── popup.html / popup.js
│       ├── background.js                     # chrome.downloads
│       └── content.js                        # Detecção de media embebida
│
├── deploy/
│   └── nginx.conf                            # Reverse proxy + WebSocket
│
├── scripts/
│   └── smoke_test.py                         # Teste E2E contra servidor real
│
├── docker-compose.yml
├── docs/
│   ├── ARCHITECTURE.md                       # Este documento
│   ├── API.md                                # Referência da API
│   └── DEPLOY.md                             # Estratégia de deploy
├── LICENSE
└── README.md
```

## 3. Fluxo de um download

```
Cliente                API                    Services                 Core
   │                     │                        │                     │
   │ POST /api/extract-info                       │                     │
   ├────────────────────►│                        │                     │
   │                     │──── extract_info() ────►│                     │
   │                     │                        │── registry.resolve() │
   │                     │                        │   (regex + priority) │
   │                     │                        │── extractor.extract ►│
   │                     │                        │   HTTP GET página   │
   │                     │                        │◄── MediaInfo ───────┤
   │◄── MediaInfo + format_list ────────────────────────────────────────┤
   │                     │                        │                     │
   │ POST /api/download {mode, video_format_id,   │                     │
   │                  audio_format_id,            │                     │
   │                  subtitle_lang}              │                     │
   ├────────────────────►│── create job ─────────►│                     │
   │◄── 202 {job_id} ───┤                        │                     │
   │                     │                        │                     │
   │ WSS /api/ws/jobs/{id}                        │                     │
   │◄═══════ eventos de progresso (JSON) ════════│◄─ publish ─────────┤
   │                     │                        │                     │
   │                     │                        │── PlanBuilder ─────►│
   │                     │                        │   (HLS/DASH → segmentos)
   │                     │                        │── ChunkDownloader ──►│
   │                     │                        │   paralelo + Range  │
   │                     │                        │   + resume + retry  │
   │                     │                        │-- SubtitleDownloader►│
   │                     │                        │   legendas → SRT     │
   │                     │                        │── PostProcessor ───►│
   │                     │                        │   FFmpeg merge      │
   │                     │                        │   + mux de legendas │
   │◄── evento "completed" + download_url ───────┤                     │
   │                     │                        │                     │
   │ GET /api/download/{job_id}  (Range)          │                     │
   ├────────────────────►│──────────────────────────────────────────► disco
```

## 4. Decisões de design

### 4.1 Núcleo independente do framework
O pacote `app/core/` não importa FastAPI em lado nenhum. Pode ser reutilizado
num CLI, num worker Celery ou num Lambda sem alterações. A API é apenas uma
camada fina por cima (`app/api/`), e a orquestração vive em `app/services/`.

### 4.2 Routing por regex + prioridade
Cada extractor declara `patterns` (regex) e `priority`. O `ExtractorRegistry`
resolve o URL para o extractor com menor `priority` que faça match.
O extractor `generic` tem `catch_all = True` e `priority = 900`, portanto
é sempre o último recurso.

**Adicionar uma plataforma** = criar um ficheiro em `core/extractors/` e
registá-lo em `registry.load_defaults()`. Nada mais muda.

### 4.3 Plano de download explícito
`PlanBuilder` transforma um `StreamFormat` num `DownloadPlan` — uma estrutura
intermédia que descreve exactamente os bytes a transferir:

```python
DownloadPlan(
    kind="segmented",           # ou "progressive"
    url="https://cdn/.../media.m3u8",
    filename="video_720p.mp4",
    headers={...},
    segments=[
        SegmentPlan(index=0, url=".../seg0.ts", duration=4.0,
                    aes_key_hex="...", aes_iv_hex="0x..."),
        ...
    ],
)
```

Vantagens: o downloader não sabe nada de HLS/DASH; o parser não sabe nada de
HTTP; e é fácil persistir/replayar um plano.

### 4.4 Retoma e paralelismo
* **Progressivo** — `Range: bytes=N-` a partir do ficheiro `.part`.
* **Paralelo (Range)** — quando o tamanho total é conhecido, o ficheiro é
  dividido em N intervalos (`bytes=inicio-fim`) descarregados em paralelo e
  depois concatenados por ordem. Isto é essencial no YouTube: o CDN limita a
  taxa **por ligação** (um stream único fica a ~3 KB/s, enquanto 8 ligações
  em paralelo somam ~4 MB/s). Se o servidor ignorar o `Range` (responder 200
  em vez de 206), o código degrada automaticamente para o stream único.
* **Segmentado** — cada segmento é descarregado para `parts/{index}.part`;
  o download só é considerado concluído quando **todos** os segmentos existem
  com o tamanho esperado. Um reinício retoma exactamente onde parou.
* **Concorrência** — `asyncio.Semaphore(download_concurrency)` (default 8).
  Os chunks já completos em disco são reaproveitados (resume).

### 4.4.b Container de saída e compatibilidade
O YouTube serve os formatos *adaptativos* (1080p, 4K…) como **MP4
fragmentado** (fMP4): o `moov` contém `mvex` e o media vive em blocos
`moof`+`mdat`. O FFmpeg e o VLC reproduzem-no, mas muitos players (Windows
Media Player, webviews sem MSE) mostram um vídeo de 0 s sem imagem nem som.

Por isso o pós-processamento:
1. detecta o fMP4 (`core/postprocess/mp4.py`, sem depender do FFmpeg);
2. converte-o para **MP4 progressivo** com `-c copy -movflags +faststart`;
3. funde vídeo + áudio quando o formato escolhido é *video-only* — em MP4 é
   preferido áudio **AAC** (Opus dentro de MP4 não é suportado por muitos
   players), transcodificando apenas o áudio quando necessário;
4. degrada com clareza: sem FFmpeg, o ficheiro original é entregue e o job
   traz um aviso em `result.warnings`.

### 4.4.c Selecção em modo automático
O `DownloadService.select_formats()` resolve **três eixos independentes** —
vídeo, áudio e legendas — a partir de um único pedido. Cada eixo aceita um
selector explícito (`video_format_id`, `audio_format_id`, `subtitle_lang`) ou
`null`, o que significa *«o programa escolhe»*.

O contrato tem de ser **idêntico** no backend e no frontend: o painel mostra
um resumo («Vais obter») que é apenas informativo e, por isso, tem de dizer a
verdade. O frontend reproduz as regras do backend em `frontend/js/app.js`
(`pickAutoVideo`, `pickMergedAudio`, `pickAutoSubtitle`), e o backend
re-resolve sempre do seu lado — quem decide é sempre o servidor.

Regras de merge (o ficheiro final tem sempre **uma** faixa de vídeo e,
opcionalmente, **uma** de áudio):

* vídeo explícito **com** áudio e sem áudio explícito → **sem merge**;
* vídeo explícito **sem** áudio → o melhor áudio é **anexado**;
* vídeo explícito + áudio explícito → o áudio escolhido **substitui** o que
  o vídeo trazia.

### 4.4.d Legendas
`core/downloader/subtitle.py` descarrega a faixa e normaliza-a para **SRT**
(SubRip), que o FFmpeg lê em qualquer container: o WebVTT e o JSON3 do
YouTube são convertidos em Python puro (`vtt_to_srt`, `json3_to_srt`); o
formato real é deduzido do *conteúdo* e não da extensão declarada.

O mux é feito numa **única passagem** do FFmpeg (`FFmpegWrapper.mux_subtitles`):
vídeo e áudio são copiados sem recodificar (`-c copy`) e só a pista de texto é
convertida (`mov_text` em MP4, `srt` em MKV — o WebM não suporta texto).

Uma legenda em falta **nunca** invalida o download: o ficheiro é entregue na
mesma e a razão fica em `result.warnings`.

### 4.5 Tratamento de 403 (token expirado)
O `HttpClient` aceita um `token_provider` opcional. Quando recebe 403 e o
provider existe, pede headers actualizados e repete o pedido. É o ponto de
extensão para solvers de assinatura (ver `core/extractors/youtube.py`).

### 4.6 Eventos de progresso
O `JobManager` mantém:
* o último `ProgressEvent` (para `GET /jobs/{id}`);
* um buffer dos últimos 50 eventos (replay para clientes que se ligam tarde);
* filas `asyncio.Queue` por subscritor (fan-out para WebSockets).

O `JobManager.run()` publica **sempre** um evento terminal, o que garante que
o WebSocket fecha em vez de ficar pendurado.

### 4.7 YouTube sem reverse-engineering de JavaScript
Em vez de reverter a ofuscação do `player.js` (extremamente frágil), o
extractor usa a API oficial InnerTube com vários *clients* (ANDROID, IOS,
TVHTML5, WEB). Os clients mobile devolvem URLs já assinadas, o que dispensa
qualquer solver. O ponto de extensão `CipherSolver` permite ligar um solver
externo se for necessário.

### 4.8 Segurança
* **SSRF**: `is_private_host()` bloqueia loopback, RFC1918, link-local e
  endpoints de metadata cloud (169.254.169.254). Desactivável via
  `MAD_ALLOW_PRIVATE_URLS` (apenas para testes).
* **Path traversal**: `sanitize_filename()` neutraliza `..` e separadores.
* **API key** opcional + **rate limiting** por IP (janela deslizante).
* **Tamanho máximo** de ficheiro (`MAD_MAX_FILE_SIZE`).

## 5. Modelo de threading / concorrência

| Componente | Modelo |
|---|---|
| Uvicorn | async (1 loop por processo) |
| `httpx.AsyncClient` | pool de conexões assíncrono (`MAD_HTTP_MAX_CONNECTIONS`) |
| Segmentos HLS/DASH | `asyncio.Semaphore` — N em paralelo por job |
| Chunks de um ficheiro (Range) | `asyncio.Semaphore` — N em paralelo por ficheiro (`MAD_DOWNLOAD_CONCURRENCY`) |
| Jobs | `asyncio.create_task` — até `MAD_MAX_CONCURRENT_JOBS` em paralelo (semáforo no `JobManager`; o excedente fica `queued`) |
| FFmpeg | `asyncio.create_subprocess_exec` (não bloqueia o loop) |

Limitação conhecida: o `JobManager` é **in-memory**. Com várias réplicas
(workers > 1) o WebSocket pode ligar-se a um worker diferente do que detém o
job. Ver `docs/DEPLOY.md` para a variante com Redis.

## 6. Como estender

### Nova plataforma
```python
# backend/app/core/extractors/meusite.py
class MeuSiteExtractor(BaseExtractor):
    name = "meusite"
    priority = 25
    patterns = [r"^https?://(?:www\.)?meusite\.com/v/(\w+)"]

    async def extract(self, url, ctx):
        result = await ctx.http.get(url, headers=ctx.request_headers())
        # ... construir StreamFormat(s)
        return MediaInfo(extractor=self.name, webpage_url=url, original_url=url,
                         title="...", formats=[...])
```
Registar em `core/extractors/registry.py::load_defaults()`.

### Novo formato de manifesto
Implementar um parser que produza `VariantInfo` e registá-lo no
`ManifestResolver.resolve()`.

### Nova fonte de progresso
Implementar `JobManager.publish()` — ou publicar directamente no job, que
propaga a todos os subscritores.

### Novo backend de pós-processamento
Implementar `PostProcessor.process()` — ou substituir `FFmpegWrapper` por
outra ferramenta (ex.: `mpv`, `shaka-packager`).
