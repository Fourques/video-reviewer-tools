"""Run explicitly with a GUI (or Xvfb/offscreen), not during unit discovery."""
def main():
    import json
    import os
    import socket
    import sys
    import tempfile
    import threading
    import time
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import reviewer
    import start
    import webview

    with tempfile.TemporaryDirectory(prefix='reviewer-native-') as directory:
        root = Path(directory)
        start.SETTINGS_FILE = root / 'launcher-settings.json'
        video = root / 'one.mp4'
        command = reviewer.run_command(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=8', '-t', '2', '-c:v', 'libx264', str(video)])
        assert command.returncode == 0, command.stderr
        original = video.read_bytes()
        errors = []
        def inspect():
            window = None
            try:
                deadline = time.monotonic() + 80
                while time.monotonic() < deadline:
                    if webview.windows:
                        window = webview.windows[0]
                        try:
                            if window.evaluate_js('typeof deck !== "undefined" && deck.readyToReview'):
                                break
                        except Exception:
                            pass
                    time.sleep(.2)
                else:
                    raise AssertionError('Native app did not decode its video in 80 seconds')
                assert window.evaluate_js('deck.preferences.loop')
                window.evaluate_js('chooseLabel("fall")')
                for _ in range(100):
                    if window.evaluate_js('state.pending.length === 0 && state.videos[0].annotation.label === "fall"'):
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError('Native app did not save annotation')
            except Exception as exc:
                errors.append(str(exc))
            finally:
                if window:
                    window.destroy()
                else:
                    from urllib.request import Request, urlopen
                    try:
                        urlopen(Request(f'http://127.0.0.1:{port}/api/shutdown', data=b'{}', headers={'Content-Type': 'application/json'}), timeout=3).close()
                    except Exception:
                        pass
        for key in ('SSH_CONNECTION', 'VSCODE_IPC_HOOK_CLI'):
            os.environ.pop(key, None)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        sys.argv = ['start.py', '--source', str(root), '--desktop', '--port', str(port)]
        inspector = threading.Thread(target=inspect, daemon=True)
        inspector.start()
        result = start.main()
        inspector.join(5)
        assert not errors, errors
        assert result == 0
        assert video.read_bytes() == original
        snapshot = json.loads((root / '.video-reviewer/state.json').read_text(encoding='utf-8'))
        assert next(iter(snapshot['annotations'].values()))['label'] == 'fall'
        with socket.socket() as check:
            assert check.connect_ex(('127.0.0.1', port)) != 0, 'Closed app still owns its port'
        print('Native app passed: renderer, video decoding, durable labeling, window close and port release')


if __name__ == '__main__':
    main()
