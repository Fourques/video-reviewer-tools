// A persistent connection tracks each open page without background timer expiry.
(() => {
  let connection;
  let id = '';
  let enabled = false;
  let pageActive = true;
  let retryTimer;
  const connect = () => {
    if (pageActive && enabled && !connection) {
      id = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
      connection = new EventSource('/api/session?id=' + encodeURIComponent(id));
    }
  };
  const discover = () => {
    if (!pageActive) return;
    fetch('/api/runtime').then(r => r.json()).then(data => {
      enabled = data.autoClose;
      connect();
    }).catch(() => {
      if (pageActive) retryTimer = setTimeout(discover, 1000);
    });
  };
  discover();
  addEventListener('pagehide', () => {
    pageActive = false;
    if (retryTimer) clearTimeout(retryTimer);
    if (!enabled) return;
    connection?.close();
    connection = null;
    navigator.sendBeacon('/api/session-close?id=' + encodeURIComponent(id), '');
  });
  addEventListener('pageshow', () => { pageActive = true; if (enabled) connect(); else discover(); });
})();
