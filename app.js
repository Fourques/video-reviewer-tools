// A persistent connection tracks each open page without background timer expiry.
(() => {
  let connection;
  let id = '';
  let enabled = false;
  let pageActive = true;
  const connect = () => {
    if (pageActive && enabled && !connection) {
      id = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
      connection = new EventSource('/api/session?id=' + encodeURIComponent(id));
    }
  };
  fetch('/api/runtime').then(r => r.json()).then(data => {
    enabled = data.autoClose;
    connect();
  }).catch(() => {});
  addEventListener('pagehide', () => {
    pageActive = false;
    if (!enabled) return;
    connection?.close();
    connection = null;
    navigator.sendBeacon('/api/session-close?id=' + encodeURIComponent(id), '');
  });
  addEventListener('pageshow', () => { pageActive = true; connect(); });
})();
