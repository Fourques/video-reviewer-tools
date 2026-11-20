/* Two bounded video slots: the upcoming clip is decoded before activation.
   Playback intent belongs to the reviewer, never to a particular video element. */
(() => {
  const defaults = {rate: 1.5, loop: true, muted: true, autoplay: true};
  const STALL_TIMEOUT = 2000;
  const FRAME_INTERVAL = 80;
  class MediaDeck {
    constructor(elements, api, callbacks = {}) {
      this.api = api; this.callbacks = callbacks;
      this.preferences = {...defaults};
      try {Object.assign(this.preferences, JSON.parse(localStorage.getItem('videoReviewer.quickLabel.playback.v1') || '{}'));} catch {}
      this.preferences.rate = Number(this.preferences.rate);
      if (!REVIEW_PLAYBACK_RATES.includes(this.preferences.rate)) this.preferences.rate = defaults.rate;
      this.wantPlay = this.preferences.autoplay; this.boundary = null; this.selected = null;
      this.slots = elements.map(element => ({element, id: null, token: 0, proxy: false, ready: false, timer: null, controller: null}));
      this.slot = this.slots[0]; this.seeking = null;
      this.watchTimer = null; this.scan = null;
      for (const slot of this.slots) {
        const video = slot.element;
        video.addEventListener('loadeddata', () => this.ready(slot));
        video.addEventListener('error', () => {if (slot.id) this.proxy(slot);});
        video.addEventListener('timeupdate', () => this.tick(slot));
        video.addEventListener('pause', () => {
          if (slot === this.slot && this.watchTimer !== null && this.wantPlay &&
              video.paused && !video.ended && !video.seeking) this.startScan();
        });
        video.addEventListener('seeked', () => {if (slot === this.slot) {this.flushSeek(); this.tick(slot);}});
        video.addEventListener('ended', () => {
          if (slot !== this.slot || this.scan) return;
          if (this.preferences.loop && this.wantPlay) {
            video.currentTime = this.boundary?.start || 0;
            reanchorEffectiveRate(video);
            this.play();
          } else {
            this.stopWatch();
            stopEffectiveRate(video);
          }
        });
      }
      this.emit();
    }
    get player() {return this.slot.element;}
    get readyToReview() {return this.slot.id === this.selected && this.slot.ready && this.player.readyState >= 2;}
    emit() {this.callbacks.onPreferences?.(this.preferences, this.wantPlay);}
    set(key, value) {
      if (!(key in defaults)) return;
      if (key === 'rate' && !REVIEW_PLAYBACK_RATES.includes(Number(value))) return;
      this.preferences[key] = key === 'rate' ? Number(value) : Boolean(value);
      if (key === 'rate') this.stopScan();
      if (key === 'autoplay') this.wantPlay = Boolean(value);
      try {localStorage.setItem('videoReviewer.quickLabel.playback.v1', JSON.stringify(this.preferences));} catch {}
      this.apply(); this.emit();
      if (key === 'autoplay' && this.wantPlay) this.play();
      if (key === 'rate' && this.wantPlay) this.play();
    }
    apply() {
      this.player.muted = this.preferences.muted;
      this.player.loop = !this.scan && this.preferences.loop && !this.boundary;
      if (!this.scan) applyEffectiveRate(this.player, Math.min(this.preferences.rate, maxPlaybackRate(this.player)));
    }
    select(id, next) {
      this.stopWatch();
      this.stopScan();
      const previous = this.slot;
      stopEffectiveRate(previous.element); previous.element.pause();
      this.selected = id; this.boundary = null; this.seeking = null;
      let slot = this.slots.find(item => item.id === id);
      if (!slot) slot = this.slots.find(item => item !== previous) || previous;
      this.slot = slot;
      this.next = next;
      this.callbacks.onLoading?.('正在载入视频…');
      if (slot.id !== id) this.prepare(slot, id, false);
      if (slot.ready && slot.element.readyState >= 2) this.ready(slot);
      // Do not recycle the old visible element until the next frame is ready.
    }
    prepare(slot, id, warm) {
      clearTimeout(slot.timer); slot.controller?.abort();
      slot.token++; slot.id = id; slot.proxy = false; slot.ready = false;
      slot.warm = warm; slot.controller = new AbortController();
      const video = slot.element;
      stopEffectiveRate(video); video.pause(); video.muted = true;
      video.preload = 'auto'; video.src = `/media/${encodeURIComponent(id)}`; video.load();
      const token = slot.token;
      slot.timer = setTimeout(() => {if (slot.token === token && !slot.ready) this.proxy(slot);}, warm ? 5000 : 3000);
      if (!video.canPlayType('video/mp4; codecs="avc1.42E01E"')) this.proxy(slot);
    }
    ready(slot) {
      clearTimeout(slot.timer); slot.ready = true;
      if (slot !== this.slot || slot.id !== this.selected) return;
      for (const other of this.slots) other.element.classList.toggle('visible', other === slot);
      this.apply();
      this.callbacks.onReady?.(slot.id, slot.proxy, slot.element.duration);
      if (this.wantPlay) this.play();
      if (this.next && this.next !== slot.id) {
        const other = this.slots.find(item => item !== slot);
        if (other.id !== this.next) this.prepare(other, this.next, true);
      }
    }
    warm(id) {
      this.next = id;
      if (this.readyToReview && id && id !== this.selected) {
        const other = this.slots.find(item => item !== this.slot);
        if (other.id !== id) this.prepare(other, id, true);
      }
    }
    async proxy(slot, force = false) {
      if (slot.proxy && !force) return;
      if (slot === this.slot) {this.stopWatch(); this.stopScan();}
      clearTimeout(slot.timer); slot.proxy = true; slot.ready = false;
      const id = slot.id, token = slot.token, signal = slot.controller.signal;
      if (slot === this.slot) this.callbacks.onLoading?.('正在生成兼容预览；原视频不会修改…');
      try {
        let result = await this.api('/api/proxy', {method:'POST', body:{id, warm:slot !== this.slot}, signal});
        const began = Date.now();
        while (result.status === 'running' && Date.now() - began < 300000) {
          await new Promise(resolve => setTimeout(resolve, 300));
          if (signal.aborted || slot.token !== token) return;
          result = await this.api(`/api/proxy-status?id=${encodeURIComponent(id)}`, {signal});
        }
        if (signal.aborted || slot.token !== token) return;
        if (result.status !== 'ready') throw new Error(result.message || '兼容预览生成超时，请重试');
        slot.element.src = result.url; slot.element.load();
      } catch (error) {
        if (error.name !== 'AbortError' && slot.token === token && slot === this.slot) this.callbacks.onError?.(error.message);
      }
    }
    async play() {
      this.wantPlay = true; this.emit();
      if (!this.readyToReview) return;
      const slot = this.slot;
      if (this.scan) return;
      if (this.preferences.rate > maxPlaybackRate(slot.element) && this.startScan()) return;
      this.apply();
      const token = slot.token;
      this.watchPlayback(slot);
      try {await slot.element.play();}
      catch (error) {
        if (slot !== this.slot || slot.token !== token || !this.wantPlay || error.name === 'AbortError') return;
        if (this.preferences.rate >= 6 && error.name !== 'NotAllowedError' && this.startScan()) return;
        this.pause();
        this.callbacks.onError?.(error.name === 'NotAllowedError' ? '浏览器限制自动播放，按 S 或点击播放开始' : '播放失败，请重试兼容预览');
      }
    }
    stopWatch() {
      clearInterval(this.watchTimer);
      this.watchTimer = null;
    }
    watchPlayback(slot) {
      this.stopWatch();
      if (this.preferences.rate < 6) return;
      const video = slot.element;
      const token = slot.token;
      let position = video.currentTime;
      let lastProgress = performance.now();
      this.watchTimer = setInterval(() => {
        if (slot !== this.slot || token !== slot.token || !this.wantPlay || video.ended) {
          this.stopWatch();
          return;
        }
        const now = performance.now();
        if (document.hidden || Math.abs(video.currentTime - position) > 0.01) {
          position = video.currentTime;
          lastProgress = now;
          return;
        }
        if (now - lastProgress < STALL_TIMEOUT) return;
        this.startScan();
      }, 250);
    }
    startScan() {
      if (this.scan) return true;
      const video = this.player;
      if (!Number.isFinite(video.duration) || video.duration <= 0) return false;
      this.stopWatch();
      stopEffectiveRate(video);
      video.pause();
      const scan = this.scan = {position:video.ended ? 0 : video.currentTime, began:performance.now(), timer:null};
      this.apply();
      this.callbacks.onFramePreview?.(true);
      // Advance on a wall clock. Leave each seek time to complete so the decoder
      // can display a frame before receiving the next request.
      scan.timer = setInterval(() => {
        if (document.hidden) {this.anchorScan(video.currentTime); return;}
        if (video.seeking || this.seeking !== null) return;
        const start = this.boundary?.start ?? 0;
        const end = this.boundary?.end ?? video.duration;
        if (end <= start) return;
        const position = scan.position + (performance.now() - scan.began) * this.preferences.rate / 1000;
        const looping = this.preferences.loop;
        const target = looping ? start + ((position - start) % (end - start) + end - start) % (end - start)
          : Math.min(position, video.duration);
        if (!looping && target >= video.duration) {
          video.currentTime = video.duration;
          this.pause();
        } else if (Math.abs(video.currentTime - target) > 0.001) {
          video.currentTime = target;
        }
      }, FRAME_INTERVAL);
      return true;
    }
    anchorScan(position) {
      if (!this.scan) return;
      this.scan.position = position;
      this.scan.began = performance.now();
    }
    stopScan() {
      if (!this.scan) return;
      clearInterval(this.scan.timer);
      this.scan = null;
      this.callbacks.onFramePreview?.(false);
    }
    pause() {this.wantPlay = false; this.stopWatch(); this.stopScan(); this.player.pause(); stopEffectiveRate(this.player); this.emit();}
    toggle() {this.wantPlay && !this.player.ended ? this.pause() : this.play();}
    tick(slot) {
      if (slot !== this.slot) return;
      const video = slot.element;
      if (!this.scan && this.boundary && this.preferences.loop && this.wantPlay && video.currentTime >= this.boundary.end - 0.025) {
        video.currentTime = this.boundary.start; reanchorEffectiveRate(video);
        if (video.paused) this.play();
      }
      this.callbacks.onTime?.(video.currentTime, video.duration);
    }
    seek(delta, absolute = false) {
      if (!this.readyToReview || !Number.isFinite(this.player.duration)) return;
      let target = absolute ? delta : (this.seeking ?? this.player.currentTime) + delta;
      const start = this.boundary?.start || 0, end = this.boundary?.end || this.player.duration;
      target = this.preferences.loop && !absolute ? start + ((target - start) % (end-start) + end-start) % (end-start) : Math.max(0, Math.min(this.player.duration, target));
      this.seeking = target; this.flushSeek();
    }
    flushSeek() {
      if (this.player.seeking || this.seeking === null) return;
      const target = this.seeking; this.seeking = null;
      this.player.currentTime = target; reanchorEffectiveRate(this.player);
      this.anchorScan(target);
      if (this.wantPlay && this.player.paused && !this.scan) this.play();
    }
    preview(start, end) {this.boundary = {start,end}; this.player.loop = false; this.seek(start, true); this.play();}
    clearBoundary() {this.boundary = null; this.apply();}
    dispose() {this.stopWatch(); this.stopScan(); for (const slot of this.slots) {clearTimeout(slot.timer); slot.controller?.abort(); stopEffectiveRate(slot.element); slot.element.pause();}}
  }
  window.MediaDeck = MediaDeck;
})();
