/**
 * Content script.
 *
 * Recolhe media embebida na página para que a extensão a possa sugerir
 * ao utilizador quando a detecção directa não encontra nada.
 */
(function detectEmbeddedMedia() {
  const found = new Set();

  const collect = () => {
    document.querySelectorAll('video[src], video source[src], audio[src], source[src]').forEach((el) => {
      const src = el.currentSrc || el.getAttribute('src');
      if (src) found.add(src);
    });

    document.querySelectorAll('meta[property="og:video"], meta[property="og:video:url"], meta[name="twitter:player:stream"]').forEach((el) => {
      const content = el.getAttribute('content');
      if (content) found.add(content);
    });
  };

  collect();
  window.addEventListener('load', collect, { once: true });

  // Expõe ao popup via mensagem.
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type === 'detect-media') {
      collect();
      sendResponse?.({ media: Array.from(found) });
      return true;
    }
    return undefined;
  });
})();
