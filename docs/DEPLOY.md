# Estratégia de Deploy

## 1. Panorama

| Opção | Quando | Complexidade | Custo |
|---|---|---|---|
| **A. Docker Compose num VPS** | produção normal, 1 servidor | Baixa | 5–20 €/mês |
| **B. systemd + uvicorn num VPS** | servidor já existente, sem Docker | Média | 5–20 €/mês |
| **C. Kubernetes / Fargate** | org grande, >1M req/dia | Alta | variável |
| **D. Cloudflare Workers + obj storage** | escala elástica, stateless | Média | pay-per-use |

**Recomendação para o motor de busca**: **Opção A**, num VPS com 2 vCPU / 4 GB
e um volume SSD de 100 GB. O serviço é *stateless* excepto o volume de
ficheiros — o que significa que pode correr em qualquer lado, e a mesma
imagem Docker serve para o servidor de produção e para o CI.

```
Utilizadores do motor de busca
            │
            │  HTTPS
            ▼
   ┌──────────────────┐
   │  Nginx / Caddy   │  TLS, rate limit, proxy WS
   └────────┬─────────┘
            │  http://api:8000  (rede interna)
            ▼
   ┌──────────────────┐
   │  API (2 workers) │  FastAPI + FFmpeg
   └────────┬─────────┘
            ▼
   ┌──────────────────┐
   │  Volume SSD      │  /app/data/downloads
   └──────────────────┘
```

---

## 2. Opção A — Docker Compose num VPS (recomendada)

### 2.1 Requisitos
* Um VPS com **2 vCPU, 4 GB RAM, 60+ GB SSD** (Hetzner CPX21, DigitalOcean
  2 vCPU / 4 GB, OVH VPS-2, Contabo CX22 — ~5–12 €/mês).
* Um domínio com DNS apontando para o IP do servidor.
* Portas 22, 80 e 443 abertas.

### 2.2 Instalação

```bash
# 1. Preparar o servidor
ssh root@SEU_IP
apt update && apt install -y git curl ca-certificates ufw
ufw allow OpenSSH && ufw allow 'Nginx Full' && ufw enable

# 2. Instalar Docker
curl -fsSL https://get.docker.com | sh
usermod -aG docker $USER && newgrp docker

# 3. Clonar o projecto
git clone <URL_DO_REPO> myarchive-downloader
cd myarchive-downloader

# 4. Configurar
cp backend/.env.example backend/.env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # -> MAD_API_KEY
nano backend/.env
```

`backend/.env` — mínimo para produção:
```ini
MAD_API_KEY=<chave-gerada>
MAD_ALLOW_PRIVATE_URLS=false
MAD_RATE_LIMIT_PER_MINUTE=60
MAD_CORS_ORIGINS="https://meu-motor-de-busca.pt"
MAD_DOWNLOAD_CONCURRENCY=8
MAD_MAX_FILE_SIZE=8589934592
```

### 2.3 Subir

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f api
```

Testar:
```bash
curl http://localhost/api/health
curl -H "X-API-Key: <chave>" http://localhost/api/extract-info \
     -H "Content-Type: application/json" \
     -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

### 2.4 TLS com Let's Encrypt

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d downloads.exemplo.com
# activar auto-renovação:
systemctl enable --now certbot.timer
```

Descomentar o bloco `listen 443 ssl` em `deploy/nginx.conf` e substituir
`/etc/nginx/certs/` pelos caminhos do certbot (`/etc/letsencrypt/live/…`).

Alternativa mais simples: **Caddy** (TLS automático, 5 linhas de config):
```caddy
downloads.exemplo.com {
    reverse_proxy api:8000 {
        header_up Upgrade {http.request.header.Upgrade}
        header_up Connection {http.request.header.Connection}
    }
}
```

### 2.5 Actualizar

```bash
git pull
docker compose up -d --build api
# os ficheiros já descarregados permanecem no volume `downloads`
```

---

## 3. Opção B — systemd (sem Docker)

```bash
sudo useradd -r -s /bin/false -d /opt/mad mad
sudo mkdir -p /opt/mad && sudo chown mad:mad /opt/mad
sudo -u mad git clone <REPO> /opt/mad/app
sudo -u mad python3 -m venv /opt/mad/venv
sudo -u mad /opt/mad/venv/bin/pip install -r /opt/mad/app/backend/requirements.txt
sudo -u mad mkdir -p /var/lib/mad/{downloads,cache,tmp}
sudo -u mad chown -R mad:mad /var/lib/mad
sudo cp /opt/mad/app/backend/.env.example /etc/mad.env
sudo nano /etc/mad.env    # MAD_DATA_DIR=/var/lib/mad ...
```

`/etc/systemd/system/mad.service`:
```ini
[Unit]
Description=MyArchive Downloader API
After=network.target

[Service]
Type=simple
User=mad
Group=mad
WorkingDirectory=/opt/mad/app/backend
EnvironmentFile=/etc/mad.env
ExecStart=/opt/mad/venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8000 --workers 2 --proxy-headers
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/mad

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mad
sudo journalctl -u mad -f
```

---

## 4. Dimensionamento

### 4.1 Por tipo de conteúdo

| Carga | vCPU | RAM | SSD | Workers |
|---|---|---|---|---|
| Uso pessoal / protótipo | 1 | 1 GB | 20 GB | 1 |
| Motor de busca, até 100 req/dia | 2 | 4 GB | 60 GB | 2 |
| Motor de busca, 1–10k req/dia | 4 | 8 GB | 200 GB | 4 |
| Streaming concorrente pesado | 8+ | 16 GB | 500 GB SSD | 8+ |

### 4.2 Onde está o consumo de recursos
* **Rede** — o downloader é *egress-bound*: a largura de banda da VPS é o
  limite real. 1 Gbps = ~100 MB/s agregado.
* **Conexões** — cada ficheiro abre até `MAD_DOWNLOAD_CONCURRENCY` ligações
  HTTP em paralelo (pedidos `Range`). No YouTube isto é decisivo: o CDN
  limita a taxa **por ligação** (um stream único fica a ~3 KB/s; com 8
  ligações o mesmo ficheiro baixa a ~4 MB/s). Por isso `MAD_HTTP_MAX_CONNECTIONS`
  tem de ser >= `MAD_DOWNLOAD_CONCURRENCY * MAD_MAX_CONCURRENT_JOBS`.
* **FFmpeg** — o merge consome ~1 vCPU por job. Com `MAD_MAX_CONCURRENT_JOBS`
  baixo (4) num host de 2 vCPU, tudo bem.
* **Disco** — cada download ocupa o tamanho do ficheiro. Implementar uma
  retenção automática (ver §7).

### 4.3 Tuning sugerido para 2 vCPU / 4 GB
```ini
MAD_DOWNLOAD_CONCURRENCY=6
MAD_MAX_CONCURRENT_JOBS=3
MAD_HTTP_MAX_CONNECTIONS=24
MAD_MAX_FILE_SIZE=4294967296
```

---

## 5. Escala horizontal

### 5.1 O problema
O `JobManager` guarda os jobs **em memória**. Com `uvicorn --workers 4`, um
pedido de criação de job pode aterrar no worker A, e o WebSocket no worker B
— que não conhece o job.

### 5.2 Soluções

**a) Sticky sessions (simples, 1 VM)**
Nginx com `ip_hash`:
```nginx
upstream mad_api {
    ip_hash;
    server 127.0.0.1:8001;
    server 127.0.0.1:8002;
}
```
Resolve para um único servidor, mas não distribui a carga.

**b) Redis como backend de estado (recomendado para escala real)**
- Jobs, eventos de progresso e estado de "cancelado" passam para Redis.
- O WebSocket pode ser ligado a qualquer worker: o worker subscreve o canal
  `job:{id}:events` do Redis.
- Rate limiting distribuído com `INCR` + `EXPIRE`.
- Fila de trabalho: `BRPOP` num worker dedicado.

Estrutura sugerida de chaves:
```
mad:job:{job_id}            HASH    estado do job
mad:job:{job_id}:events     LIST    buffer de eventos (MAXLEN 50)
mad:job:{job_id}:channel    PUB/SUB canal de progresso
mad:rl:{ip}:{minute}        STRING  contador de rate limit
mad:queue                   LIST    fila de downloads
```

**c) Separar a API do worker**
```
        Nginx
          │
    ┌─────┴─────┐
    │ API (2x)  │  ← só extrai, cria jobs, serve ficheiros
    └─────┬─────┘
          │  Redis queue
    ┌─────▼─────┐
    │ Workers   │  ← descarregam + fazem merge (FFmpeg)
    │ (N x 2)   │
    └───────────┘
```
É o desenho correcto para produção a sério: a API fica sempre rápida e a
CPU pesada fica isolada.

### 5.3 Ficheiros em object storage
Para múltiplas réplicas, os ficheiros não devem ficar em disco local. Opções:
* **MinIO / S3** — o downloader escreve directamente para o bucket.
* **NFS / CephFS** — simplesto, com as desvantagens de latência.

---

## 6. Segurança em produção

```ini
# /etc/mad.env ou backend/.env
MAD_API_KEY=<secrets.token_urlsafe(32)>
MAD_ALLOW_PRIVATE_URLS=false          # bloqueia SSRF para redes internas
MAD_RATE_LIMIT_PER_MINUTE=60
MAD_CORS_ORIGINS="https://meu-motor.pt"   # não usar "*" em produção
```

Checklist adicional:
* [ ] TLS válido (certbot) e HSTS
* [ ] `ufw` só com 22/80/443
* [ ] Runs como utilizador não-root (a imagem Docker já faz isso)
* [ ] `MAD_MAX_FILE_SIZE` definido (evita esgotar o disco)
* [ ] Rate limiting no Nginx **e** na aplicação
* [ ] Rotação do ficheiro de cookies (nunca no repositório)
* [ ] Content-Security-Policy nas respostas da API
* [ ] Log centralizado (journald / Loki / CloudWatch)

### Limites de recursos (systemd)
```ini
LimitNOFILE=65535
MemoryMax=3G
TasksMax=512
```

---

## 7. Gestão do disco (importante)

Sem retenção, o disco enche em poucos dias. Implementar um job agendado:

```bash
# /etc/cron.d/mad-cleanup
0 3 * * * mad find /var/lib/mad/downloads -type f -mtime +7 -delete
```

Ou dentro da aplicação (o `JobManager` já remove jobs expirados; falta
ligar a remoção dos ficheiros). Sugestão de implementação em
`services/pipeline.py`: ao criar o job, agendar a remoção para
`MAD_JOB_TTL_SECONDS` após a conclusão.

Alternativa: **streaming direto sem ficheiro intermédio** — em vez de
descarregar e depois servir, fazer proxy do ficheiro para o browser
(`/api/stream?url=...`). É mais leve em disco, mas perde a resume, o merge e
o suporte a Range no servidor.

---

## 8. Observabilidade

Métricas recomendadas (Prometheus):
* `mad_jobs_total{status}` — jobs criados/concluídos/falhados
* `mad_job_duration_seconds` — duração do download
* `mad_download_bytes_total` — bytes transferidos
* `mad_active_jobs` — jobs em curso
* `mad_http_requests_total{status}` — tráfego da API

Logs: o backend já usa `logging` estruturado. Para produção, trocar o
`basicConfig` por JSON logs (`python-json-logger`) e enviar para Loki /
CloudWatch / Datadog.

Alertas a configurar:
* Disco > 80% → paging
* Taxa de falhas > 5% → aviso
* p95 de `/api/extract-info` > 3 s → aviso

---

## 9. Custos estimados

| Valor | VPS | Tráfego (100 GB/mês) | Total |
|---|---|---|---|
| Hetzner CX22 (2 vCPU, 4 GB, 40 GB) | ~€4.5/mês | ~€5 | **~€10/mês** |
| DigitalOcean 2 vCPU / 4 GB | €24/mês | €5 | ~€29/mês |
| OVH VPS-2 (4 vCPU, 8 GB, 75 GB) | ~€7/mês | ~€7 | ~€14/mês |
| AWS EC2 t3.medium | ~€30/mês | ~€9 | ~€39/mês |

O custo dominante é a **largura de banda de saída**. Com caching de
resultados de extracção (`MAD_CACHE_TTL_SECONDS=1800`), o mesmo vídeo pedido
várias vezes só é *scraped* uma vez — mas o download é sempre repetido.

---

## 10. Nota legal

Este software é um **motor técnico**. A responsabilidade pela utilização
recai sobre quem o aloja e sobre quem o utiliza. Para um motor de busca em
produção:

* respeitar os Termos de Serviço e robots.txt de cada plataforma;
* limitar-se a conteúdos com licença, domínio público ou autorização;
* manter logs de pedidos (URL, timestamp, IP) para rastreabilidade;
* publicar uma política de abuso e um mecanismo de denúncia;
* validar a legislação de copyright aplicável na jurisdição (a Directiva
  InfoSocmul na UE obriga a respeitar procedimentos de retirada legal).

A Decision 2021/1173 do TJUE versou exatamente sobre a PARTED — e a
UE scrapped a Directiva InfoSocmul em 2024, o que **aumentou** a responsabilidade
das plataformas de intermediação. Trate a conformidade como parte do
produto, não como um extra.
