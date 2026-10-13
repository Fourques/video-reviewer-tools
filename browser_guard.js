/* Intentionally ES5: this message must work even when app scripts cannot parse. */
(function () {
  function supported() {
    try {
      return !document.documentMode && window.fetch && window.Promise && window.AbortController &&
        window.CSS && CSS.supports('display', 'grid') && CSS.supports('color', 'var(--probe)') &&
        CSS.supports('width', 'min(900px, 100%)') && window.HTMLDialogElement &&
        typeof HTMLDialogElement.prototype.showModal === 'function' &&
        (new Function('return ({x: 1})?.x ?? 0;'))() === 1;
    } catch (error) { return false; }
  }
  var compatible = Boolean(supported());
  window.videoReviewerBrowserSupported = compatible;
  document.addEventListener('DOMContentLoaded', function () {
    var styled = window.getComputedStyle && getComputedStyle(document.documentElement).getPropertyValue('--bg').replace(/\s/g, '') === '#11151c';
    if (compatible && styled) return;
    for (var i = 0; i < document.body.children.length; i++) document.body.children[i].style.display = 'none';
    document.body.style.cssText = 'margin:0;background:#11151c;color:#e2e7ef;font:16px/1.7 Arial,"Microsoft YaHei",sans-serif;';
    var panel = document.createElement('div');
    panel.id = 'browserCompatibilityError';
    panel.setAttribute('role', 'alert');
    panel.style.cssText = 'max-width:720px;margin:50px auto;padding:28px;background:#191e27;border:1px solid #465367;border-radius:12px;';
    var title = document.createElement('h1');
    title.style.fontSize = '24px';
    title.appendChild(document.createTextNode(compatible ? '页面资源未完整加载' : '当前浏览器内核无法运行审核工具'));
    panel.appendChild(title);
    var text = document.createElement('p');
    text.appendChild(document.createTextNode(compatible ? '样式文件未加载或应用文件不完整。请完整解压新版应用，不要只复制 exe 或混用新旧文件，然后重新打开。' : '检测到旧 IE 内核或缺少必要的浏览器能力。请升级应用，安装 Microsoft WebView2 Runtime，或使用最新版 Edge / Chrome / Firefox 打开下方地址。'));
    panel.appendChild(text);
    var saved = document.createElement('p');
    saved.appendChild(document.createTextNode('无需删除项目、output 或 .video-reviewer；此提示不会清空审核标签和整理记录。'));
    panel.appendChild(saved);
    var address = document.createElement('input');
    address.readOnly = true; address.value = location.href;
    address.style.cssText = 'box-sizing:border-box;width:100%;padding:10px;background:#11151c;color:#e2e7ef;border:1px solid #60738e;';
    address.onclick = function () { address.select(); };
    panel.appendChild(address);
    var info = document.createElement('p');
    info.style.cssText = 'font-size:12px;word-wrap:break-word;color:#b6c3d3;';
    info.appendChild(document.createTextNode('诊断：' + (compatible ? 'stylesheet unavailable' : 'unsupported renderer') + ' · ' + navigator.userAgent));
    panel.appendChild(info);
    document.body.appendChild(panel);
  });
}());
