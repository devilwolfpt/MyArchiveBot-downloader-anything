# MyArchive Downloader

Motor de download de media **escrito de raiz** — uma alternativa modular ao
`yt-dlp` que não depende de nenhum binário local. Corre num servidor, expõe
uma API REST + WebSocket, e serve um painel web pronto a usar.

O objectivo: um utilizador do motor de busca (ou de uma extensão de browser)
clica em "descarregar" e recebe o ficheiro, **sem instalar nada** no seu
computador. Todo o trabalho pesado — extracção de metadados, parsing de
manifests HLS/DASH, download paralelo com resuming, merge com FFmpeg — acontece
do lado do servidor.

---

## 1. Visão geral da arquitectura

```
┌──────────────────────────────────────────────────────────────────────────┐
│  CLIENTES                                                                │
│    Painel Web (/app)   Extensão Chrome/Edge MV3   Motor de Busca (SSO)   │
└───────────┬───────────────────────┬───────────────────────┬──────────────┘
            │  POST /api/extract-info   POST /api/download     │
            │  WSS  /api/ws/jobs/{id}  (progresso em tempo real)
            ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  EDGE — Nginx: TLS, rate limiting, proxy WebSocket, buffering off        │
└───────────┬──────────────────────────────────────────────────────────────┘
            ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  API ENGINE — FastAPI / Uvicorn                                         │
│    api/routes/     extract · download · stream · health (+ WS)          │
│    api/deps.py     API key · rate limiter · DI                          │
│    services/       ExtractionService · DownloadService (pipeline)       │
│    core/           ── motor puro, sem dependência do FastAPI ──         │
│      extractors/   youtube · vimeo · francetv · dailymotion · jwplatform│
│                    brightcove · kaltura · direct · generic              │
│                    (routing por regex + prioridade)                     │
│      http/         cliente httpx: retry, rotação de UA, refresh de 403,  │
│                    guarda anti-SSRF, cookies                            │
│      manifest/     hls.py (m3u8) · dash.py (mpd)                        │
│      downloader/   plan.py · chunk.py (paralelo + Range + resume) ·      │
│                    crypto.py (AES-128) · manager.py (jobs) · storage.py │
│                    subtitle.py (legendas → SRT)                        │
│      postprocess/  ffmpeg.py (wrapper) · processor.py (merge/remux/mp3)  │
└───────────┬──────────────────────────────────────────────────────────────┘
            ▼
     FFmpeg (subprocess) — merge vídeo+áudio, remux, extracção de mp3,
                           mux das legendas (mov_text/srt)
```

O `core/` não importa FastAPI: pode ser reutilizado tal e qual num worker de
linha de comandos ou num job Celery. A camada HTTP é fina; a orquestração vive
em `services/`.

Detalhe completo em [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## 2. Funcionalidades

| | |
|---|---|
| **Extractors** | **100% Nativos e Modulares**: Arquivos dedicados em `backend/app/core/extractors/` para cada plataforma (**YouTube** InnerTube VisionOS, **TikTok**, **Twitter/X**, **Reddit**, **Instagram**, **Twitch**, **SoundCloud**, **Facebook**, **Pinterest**, **Bilibili**, **Vimeo**, **Dailymotion**, **France TV**, **JW Platform**, **Brightcove**, **Kaltura**, **Direct Media** e **Generic HTML**) — sem dependência de bibliotecas externas |
| **Manifests** | HLS master + media (variantes, `#EXT-X-KEY` AES-128, `#EXT-X-BYTERANGE`) e DASH (`SegmentTemplate`, `Timeline`, `List`, `Base`) |
| **Download** | Segmentos em paralelo e requests `Range` com **resume**; o YouTube usa blocos `Range` sequenciais e limitados (o CDN rejeita intervalos abertos com 403); re-tentativa exponencial e renovação de URLs |
| **Selecção** | Três eixos independentes — **vídeo**, **áudio** e **legendas** — cada um com uma opção *Automático* em que o programa escolhe o melhor disponível; o ficheiro final tem sempre uma faixa de vídeo e, opcionalmente, uma de áudio |
| **Legendas** | Descarga e normalização para **SRT** (WebVTT e JSON3 do YouTube), muxadas no ficheiro final sem recodificar o vídeo (`mov_text` em MP4, `srt` em MKV) |
| **Pós-processamento** | Merge de vídeo + áudio (`mp4`/`mkv`), remux, extracção de áudio (`mp3`/`m4a`/`opus`/`wav`) via FFmpeg |
| **Progresso** | WebSocket com eventos JSON (percent, bytes, velocidade, ETA, segmento actual) + buffer de replay para clientes que chegam atrasados |
| **Segurança** | Chave de API opcional (`X-API-Key`/`Bearer`), rate limiting, guarda anti-SSRF (bloqueia IPs privados por omissão), limite de tamanho de ficheiro |
| **Jobs** | Fila com limite de concorrência (`MAD_MAX_CONCURRENT_JOBS`), cancelamento, TTL e purga automática |

---

## 3. Arranque rápido

### 3.1 Local (desenvolvimento)

Requer **Python 3.11+** e **FFmpeg** (opcional — sem FFmpeg o serviço entrega o
ficheiro original e acrescenta um aviso).

```bash
git clone <repo> myarchive-downloader
cd myarchive-downloader/backend

python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env        # Windows  (Linux: cp .env.example .env)

uvicorn app.main:app --reload --port 8000
```

| Endereço | O quê |
|---|---|
| <http://localhost:8000/app> | Painel web |
| <http://localhost:8000/docs> | Swagger UI |
| <http://localhost:8000/api/health> | Health check |

#### Instalar o FFmpeg

O FFmpeg é **opcional mas recomendado**: sem ele não é possível fundir áudio +
vídeo, nem converter os MP4 fragmentados do YouTube em MP4 progressivos (muitos
players não os reproduzem).

```bash
# Windows (winget)
winget install --id Gyan.FFmpeg --exact

# Windows (chocolatey)
choco install ffmpeg

# macOS
brew install ffmpeg

# Debian / Ubuntu
sudo apt install ffmpeg
```

Depois de instalado, confirma a disponibilidade:

```bash
ffmpeg -version
curl http://localhost:8000/api/health     # -> "ffmpeg": "/usr/bin/ffmpeg"
```

### 3.2 Docker Compose (produção)

```bash
cd myarchive-downloader
cp backend/.env.example backend/.env
# edite backend/.env: defina MAD_API_KEY, MAD_CORS_ORIGINS, MAD_ALLOW_PRIVATE_URLS=false

docker compose up -d --build
docker compose logs -f api
```

O Compose sobe a API (porta interna 8000) e o Nginx (80/443) com o
`deploy/nginx.conf` já preparado para WebSocket. Para expor o certificado,
coloque os ficheiros em `deploy/certs/` (`fullchain.pem` + `privkey.pem`).

### 3.3 systemd (VPS simples)

Ver [`docs/DEPLOY.md`](docs/DEPLOY.md) §3 para a unit file completa e o
`docker-compose` alternativo sem proxy.

---

## 4. Estrutura do projecto

```
myarchive-downloader/
├── backend/
│   ├── app/
│   │   ├── main.py                 # app factory, lifespan, handlers de erro
│   │   ├── config.py               # Settings (pydantic-settings, prefixo MAD_)
│   │   ├── api/routes/             # extract · download · stream · health (+ WS)
│   │   ├── services/               # orquestração (pipeline, cache de extracção)
│   │   └── core/                   # motor puro — http, manifest, downloader, postprocess
│   │       └── extractors/         # Camada de extracção nativa:
│   │           ├── youtube.py      # YouTube (InnerTube VisionOS/Android multi-client)
│   │           ├── tiktok.py       # TikTok (API aweme sem marca de água)
│   │           ├── twitter.py      # X / Twitter (Syndication API + token math)
│   │           ├── reddit.py       # Reddit (JSON API + HLS/DASH)
│   │           ├── instagram.py    # Instagram (Embed captions)
│   │           ├── twitch.py       # Twitch (GraphQL + Usher HLS)
│   │           ├── soundcloud.py   # SoundCloud (v2 resolve + MP3/Opus)
│   │           ├── facebook.py     # Facebook (HD/SD progressive streams)
│   │           ├── pinterest.py    # Pinterest (__PWS_DATA__ node parser)
│   │           ├── bilibili.py     # Bilibili (View API + PlayURL DASH)
│   │           ├── vimeo.py, dailymotion.py, francetv.py, direct.py, generic.py...
│   │           ├── catalog.py      # Adaptador nativo do catálogo universal (+1.750 sites)
│   │           └── sites/          # Catálogo interno integrado:
│   │               └── extractor/  # 940 ficheiros com suporte a +1.750 plataformas
│   ├── tests/                      # 204 testes (unitários, integração e API)
│   ├── Dockerfile
│   └── .env.example
├── frontend/
│   ├── index.html · css/ · js/     # painel web moderno com player embutido, atalhos e histórico
│   └── extension/                  # extensão Chrome/Edge MV3
├── deploy/nginx.conf               # reverse proxy + WebSocket
├── scripts/smoke_test.py           # teste E2E contra um servidor real
├── docker-compose.yml
└── docs/
    ├── ARCHITECTURE.md             # arquitectura + decisões de design
    ├── API.md                      # referência completa da API
    └── DEPLOY.md                   # estratégia de hosting
```

---

## 5. Uso da API

### 5.1 Extrair metadados

```bash
curl -X POST http://localhost:8000/api/extract-info \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

```json
{
  "title": "Never Gonna Give You Up",
  "duration": 212.0,
  "formats": [
    { "id": "137", "height": 1080, "media_type": "video", "vcodec": "avc1.640028" },
    { "id": "140", "abr": 128.0, "media_type": "audio", "acodec": "mp4a.40.2" }
  ]
}
```

### 5.2 Descarregar e acompanhar o progresso

```bash
# 1) cria o job (responde 202 imediatamente)
JOB=$(curl -s -X POST http://localhost:8000/api/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mode": "both", "container": "mp4"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

# 2) progresso em tempo real
websocat ws://localhost:8000/api/ws/jobs/$JOB

# 3) descarrega o ficheiro final
curl -OJ "http://localhost:8000/api/download/$JOB"
```

Evento de progresso típico:

```json
{
  "job_id": "9f2c…",
  "status": "running",
  "phase": "downloading",
  "percent": 42.7,
  "downloaded_bytes": 12345678,
  "total_bytes": 28900000,
  "speed": 5242880.0,
  "eta": 3.2,
  "segment_index": 91,
  "segment_total": 213
}
```

### 5.3 Escolher o que guardar (vídeo, áudio e legendas)

O painel web apresenta **três eixos independentes** — vídeo, áudio e legendas —
e um painel **"Vais obter"** que diz, em texto simples, o que o ficheiro final
vai conter. Cada eixo tem uma opção **Automático**: o programa escolhe o
melhor disponível.

O mesmo contrato está disponível na API, um campo por eixo:

```bash
curl -X POST http://localhost:8000/api/download \
  -H "Content-Type: application/json" \
  -d '{
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "mode": "auto",
        "video_format_id": null,
        "audio_format_id": null,
        "subtitle_lang": "auto"
      }'
```

| Campo | Valores | Significado |
|---|---|---|
| `mode` | `auto` (predefinição) · `video` · `both` · `audio` · `original` | `auto` deixa o programa decidir a combinação |
| `video_format_id` | id de um formato, ou `null` | `null` = o programa escolhe o melhor vídeo |
| `audio_format_id` | id de um formato, ou `null` | `null` = o programa escolhe o melhor áudio |
| `subtitle_lang` | `null` · `"auto"` · `"<idioma>"` · `"all"` · `"<grupo>:<idioma>:<nome>"` | `null` = sem legendas; `"auto"` = a melhor faixa |

Regras do modo automático (o frontend espelha estas regras para o resumo ser
verdadeiro):

| Eixo | Regra |
|---|---|
| Vídeo | Prefere um stream **só de vídeo** (para poder anexar o melhor áudio); desempate por `(height, tbr, filesize)` |
| Áudio | Melhor `(abr, tbr)`; em `mp4` é preferido o **AAC** |
| Legendas | Ordem de idioma `pt, pt-pt, pt-br, en, en-us, en-gb, es, fr, de, it`; dentro do mesmo idioma, as faixas **manuais** ganham às automáticas |

E as regras de merge — o ficheiro final tem sempre **uma** faixa de vídeo e,
opcionalmente, **uma** de áudio:

* vídeo explícito **com** áudio e sem áudio explícito → **sem merge**;
* vídeo explícito **sem** áudio → o melhor áudio é **anexado**;
* vídeo explícito + áudio explícito → o áudio escolhido **substitui** o que
  o vídeo trazia.

As legendas são convertidas para **SRT** e muxadas numa passagem única do
FFmpeg, **sem recodificar o vídeo**. Uma legenda em falta nunca invalida o
download: o ficheiro é entregue na mesma e a razão fica em `result.warnings`.

### 5.4 A partir de JavaScript (painel web / extensão)

```js
// criar o job
const res = await fetch('/api/download', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ url, mode: 'both', container: 'mp4' }),
});
const { job_id } = await res.json();

// progresso via WebSocket (com reconexão automática)
const ws = new WebSocket(`ws://${location.host}/api/ws/jobs/${job_id}`);
ws.onmessage = (e) => {
  const evt = JSON.parse(e.data);
  if (evt.status === 'completed') window.location = `/api/download/${job_id}`;
};
```

Referência completa dos endpoints, campos e códigos de erro:
[`docs/API.md`](docs/API.md).

### 5.5 Notas sobre o YouTube

* A extracção usa a API oficial **InnerTube** com vários *clients* (ANDROID,
  ANDROID_VR, IOS, TVHTML5, WEB). Os clients mobile devolvem URLs já
  assinadas — **não é preciso qualquer solver** de cifras.
* Os formatos adaptativos (1080p, 4K…) são *video-only*. Com `merge: true`
  (predefinição) o serviço descarrega também a melhor faixa de áudio
  compatível e funde tudo num único ficheiro.
* O YouTube serve esses formatos como **MP4 fragmentado** (fMP4). Com FFmpeg
  instalado são convertidos para **MP4 progressivo** (`+faststart`), que
  reproduz em qualquer player. Sem FFmpeg o ficheiro original é entregue e o
  job traz um aviso.
* A extracção e o download têm de sair do **mesmo IP** — as URLs assinadas
  incorporam o IP de origem.

Verificação ponta-a-ponta contra o YouTube:

```bash
python scripts/yt_check.py --format 137
```

### 5.6 Notas sobre o France TV e outros serviços que necessitam de VPN

* Um exemplo de serviço que tem **restrito geograficamente a França** é O conteúdo do France TV. Quando o IP
  de saída está fora de França, o CDN (Akamai) recusa o manifesto e o serviço
  devolve **HTTP 451** com uma mensagem explícita — não é um bug do motor.
* Para descarregar, configure um proxy com IP francês em `MAD_PROXY_URL`
  (ver §6). O proxy é usado em todas as peticiones HTTP (extracção e
  download).
* O extractor resolve o domínio dinâmico (`k7.ftven.fr`), decifra o token
  `TA` e percorre o manifesto DASH. Os formatos adaptativos (vídeo e áudio
  separados) são fundidos num único MP4.

### 5.7 Usar a sessão de uma conta

No painel, abre **Definições → Conta do browser** e escolhe o browser em que
iniciaste sessão no site. A app lê os cookies desse browser no computador onde
o backend está a correr e usa-os tanto na extracção como no download e nas
renovações de URLs expirados. O suporte a cookies usa `browser-cookie3`; em
alguns casos pode ser necessário fechar o browser para permitir a leitura da
base de dados local.

Os cookies não são copiados para as definições nem registados pela app, e são
mantidos com o domínio/path/segurança originais para não serem enviados a
outros sites. Um browser no computador do utilizador não pode autenticar uma
instância alojada remotamente: nesse caso, os cookies teriam de existir no
servidor. A autenticação não contorna DRM, bloqueios geográficos ou regras de
acesso do serviço.

---

## 6. Configuração

Todas as chaves usam o prefixo `MAD_` e são lidas de `.env` ou do ambiente.
O ficheivo `backend/.env.example` tem a lista comentada; as mais relevantes:

| Variável | Default | Descrição |
|---|---|---|
| `MAD_API_KEY` | *(vazio)* | Se definida, exige `X-API-Key` ou `Bearer` |
| `MAD_RATE_LIMIT_PER_MINUTE` | `60` | Requests por minuto e por IP |
| `MAD_ALLOW_PRIVATE_URLS` | `false` | `true` permite alvos em rede privada (**desactivar em produção**) |
| `MAD_MAX_CONCURRENT_JOBS` | `4` | Downloads em simultâneo; o resto fica em `queued` |
| `MAD_DOWNLOAD_CONCURRENCY` | `8` | Ligações paralelas por ficheiro (chunks Range e segmentos); o YouTube descarrega sequencialmente |
| `MAD_PARALLEL_DOWNLOAD` | `true` | Dividir ficheiros compatíveis em vários pedidos `Range` em paralelo; o YouTube usa blocos sequenciais limitados |
| `MAD_PARALLEL_MIN_SIZE` | `1048576` | Abaixo deste tamanho (1 MiB) não se abre paralelismo |
| `MAD_PARALLEL_MIN_CHUNK_SIZE` | `524288` | Chunk mínimo por ligação (512 KiB) |
| `MAD_MAX_FILE_SIZE` | `8589934592` | Limite de bytes (8 GiB) |
| `MAD_JOB_TTL_SECONDS` | `3600` | Tempo que um job e o ficheiro são retidos |
| `MAD_FFMPEG_PATH` | `ffmpeg` | Caminho do binário FFmpeg |
| `MAD_PROXY_URL` | *(vazio)* | Proxy de saída para todas as requisições — necessário para conteúdo restrito geograficamente (France TV) |
| `MAD_CORS_ORIGINS` | `*` | Lista separada por vírgulas |

---

## 7. Testes

```bash
cd backend
python -m pytest -q          # 167 testes
```

Teste end-to-end contra um servidor a correr:

```bash
python scripts/smoke_test.py   # exit 0 = tudo OK
```

---

## 8. Hosting

A estratégia completa (dimensionamento, custos, Docker/systemd/Kubernetes,
Redis para múltiplas réplicas, retenção de ficheiros e checklist de segurança)
está em [`docs/DEPLOY.md`](docs/DEPLOY.md).

Resumo da opção recomendada para produção:

* **VPS 2 vCPU / 4 GB** (≈ 10 €/mês) com **Docker Compose**: API em contentor
  + Nginx à frente para TLS.
* O motor de busca chama a API **server-side**; o utilizador final nunca
  instala nada.
* Os ficheiros ficam num volume persistente com retenção automática.
* Limite de concorrência por VPS e rate limiting por IP como primeira linha
  de defesa.
* Para escalar além de ~1 worker: registry de jobs em Redis + storage partilhado
  (S3/MinIO) e balanceador com *sticky sessions* (ou routing de WebSocket por
  job id).

---

## 9. Nota legal

Esta ferramenta descarrega conteúdo a partir de URLs. Quem **usa** o serviço
é responsável por garantir que os pedidos respeitam os termos de uso dos
sites de origem, as-directivas `robots.txt` e a legislação de copyrights
aplicável (na UE, a.directiva InfoSocmul e as excepções previstas na
legislação nacional). Não use este motor para distribuir conteúdo protegido
sem autorização. Qualquer uso irresponsavel a culpa não será de nenhum desenvolvedor da APP em questão.

**Esta APP destinou-se a fins educativos.** Isto é uma ferramenta de teste de segurança do site, mesmo sendo criada para fazer download de videos.


Esta ferramenta foi desenvolvida para efeitos de processamento de URLs e conteúdos multimédia. O **utilizador** é o único responsável por garantir que qualquer pedido efetuado respeita escrupulosamente os Termos de Serviço dos sites de origem, as diretivas `robots.txt` e a legislação de direitos de autor aplicável (na União Europeia, a Diretiva InfoSoc e as exceções previstas na legislação nacional).

Este motor não deve ser utilizado para descarregar ou distribuir conteúdos protegidos sem a devida autorização dos titulares. Os **criadores e programadores** da aplicação declinam toda e qualquer responsabilidade **civil** ou **criminal** decorrente do uso **indevido** ou **ilícito** da ferramenta.

Esta aplicação destina-se estritamente a fins educativos, de investigação técnica e de auditoria de arquitetura de software.

---

## Licença

MIT.
