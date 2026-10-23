/**
 * Service worker da extensão.
 *
 * Recebe o pedido de entrega do ficheiro final e usa `chrome.downloads`
 * para o descarregar com a UI nativa do browser (tray, pausa, cancelar).
 */
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== 'download') return undefined;

  const { url, filename } = message;

  chrome.downloads.download(
    {
      url,
      filename: filename || undefined,
      saveAs: false,
      conflictAction: 'uniquify',
    },
    (downloadId) => {
      const err = chrome.runtime.lastError;
      if (err) {
        console.error('[MyArchive] Falha ao iniciar o download:', err.message);
        return;
      }
      console.info('[MyArchive] Download iniciado:', downloadId, filename);
    },
  );

  sendResponse?.({ ok: true });
  return true;
});
