# Guia de Deploy no Render.com — MyArchive Downloader

Este guia explica como colocar o **MyArchive Downloader** a funcionar num servidor Web no [Render.com](https://render.com), mantendo o suporte nativo aos **1.750+ sites de vídeo e áudio** sem necessidade do pacote `yt-dlp` no pip e com total suporte a FFmpeg e WebSockets.

---

## Opção 1: Deploy Automático via Docker (Recomendado — Mais Simples)

O Render cria automaticamente o contentor Linux isolado com **Python 3.12**, **FFmpeg**, **WebSockets** e a interface Web pronta.

### Passos:
1. Cria uma conta ou entra em [dashboard.render.com](https://dashboard.render.com).
2. Clica em **New +** e seleciona **Web Service**.
3. Liga o teu repositório Git (GitHub ou GitLab).
4. Configura as opções do serviço:
   - **Name**: `myarchive-downloader` (ou o nome que preferires)
   - **Region**: Frankfurt (Europa) ou Oregon (EUA)
   - **Environment / Runtime**: `Docker`
   - **Dockerfile Path**: `./Dockerfile` (ou `./backend/Dockerfile`)
   - **Instance Type**: `Free`
5. Em **Advanced**:
   - **Health Check Path**: `/api/health`
6. Clica em **Create Web Service**.

> **Pronto!** O Render vai construir a imagem Docker, instalar o FFmpeg, subir o backend e disponibilizar a aplicação em `https://teu-servico.onrender.com/`.

---

## Opção 2: Deploy com 1 Clique via Blueprint (`render.yaml`)

Se tiveres o repositório sincronizado com o GitHub:
1. No dashboard do Render, clica em **New +** -> **Blueprint**.
2. Seleciona o teu repositório.
3. O Render lê automaticamente o ficheiro [`render.yaml`](../render.yaml) na raiz e configura todo o serviço sozinho.
4. Clica em **Apply**.

---

## Opção 3: Deploy em Ambiente Nativo Python (Sem Docker)

Se preferires correr diretamente no runtime nativo Python do Render em vez de Docker:

1. Cria um **Web Service** no Render e escolhe **Python 3**.
2. Configura:
   - **Build Command**: `./render-build.sh`
   - **Start Command**:
     ```bash
     cd backend && uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2 --proxy-headers
     ```
3. Variáveis de ambiente (**Environment Variables**):
   - `PYTHONPATH`: `/opt/render/project/src/backend`
   - `PATH`: `/opt/render/project/src/bin:/opt/render/bin:/usr/local/bin:/usr/bin:/bin`
   - `MAD_ENV`: `production`
4. Clica em **Create Web Service**.

O script [`render-build.sh`](../render-build.sh) descarrega e configura automaticamente o binário do **FFmpeg** sem necessidade de permissões de root.

---

## Testar o Servidor no Render

Assim que o deploy terminar, podes testar:

- **Interface Web**: Abre `https://teu-servico.onrender.com/` (ou `/app`).
- **Verificação de Saúde e Extractores**:
  ```bash
  curl https://teu-servico.onrender.com/api/health
  ```
  Resposta esperada:
  ```json
  {
    "status": "ok",
    "supported_sites_count": 1754,
    "ffmpeg": "/usr/bin/ffmpeg",
    "extractors": ["direct", "youtube", "vimeo", "kaltura", ...]
  }
  ```
- **Documentação Interativa Swagger**: `https://teu-servico.onrender.com/docs`.
- **Bot de Atualização do Motor**: Podes verificar e atualizar o motor a partir da interface nas **Definições** sem fazer redeploy!
