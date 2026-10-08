"""Headless/native renderer smoke check, run separately from unit discovery."""
import sys
import threading
import time
import webview

result = {}
window = webview.create_window('Renderer test', html='<html><body>VideoReviewer</body></html>')


def check():
    try:
        for _ in range(100):
            try:
                result['h264'] = window.evaluate_js('document.createElement("video").canPlayType(\'video/mp4; codecs="avc1.42E01E"\')')
                result['dom'] = window.evaluate_js('document.body.textContent')
                if result['dom'] == 'VideoReviewer':
                    break
            except Exception:
                pass
            time.sleep(.1)
    finally:
        window.destroy()


webview.start(check, gui='qt', private_mode=True)
print(result, flush=True)
sys.exit(0 if result.get('dom') == 'VideoReviewer' else 1)
