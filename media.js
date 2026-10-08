/* Two bounded video slots: the upcoming clip is decoded before activation.
   Playback intent belongs to the reviewer, never to a particular video element. */
(() => {
  const defaults = {rate: 1.5, loop: true, muted: true, autoplay: true};
  class MediaDeck {
    constructor(elements, api, callbacks = {}) {
      this.api = api; this.callbacks = callbacks;
      this.preferences = {...defaults};
      try {Object.assign(this.preferences, JSON.parse(localStorage.getItem('videoReviewer.quickLabel.playback.v1') || '{}'));} catch {}
      if (![0.5,0.75,1,1.25,1.5,2,3,4,8,12,16,20].includes(Number(this.preferences.rate))) this.preferences.rate = 1.5;
      this.wantPlay = this.preferences.autoplay; this.boundary = null; this.selected = null;
      this.slots = elements.map(element => ({element, id: null, token: 0, proxy: false, ready: false, timer: null, controller: null}));
      this.slot = this.slots[0]; this.seeking = null; this.intentional = false;
      for (const slot of this.slots) {
        const video = slot.element;
        video.addEventListener('loadeddata', () => this.ready(slot));
        video.addEventListener('error', () => {if (slot.id) this.proxy(slot);});
        video.addEventListener('timeupdate', () => this.tick(slot));
        video.addEventListener('seeked', () => {if (slot === this.slot) {this.flushSeek(); this.tick(slot);}});
        video.addEventListener('ended', () => {if (slot === this.slot && this.preferences.loop && this.wantPlay) {video.currentTime = this.boundary?.start || 0; this.play();}});
      }
      this.emit();
    }
    get player() {return this.slot.element;}
    get readyToReview() {return this.slot.id === this.selected && this.slot.ready && this.player.readyState >= 2;}
    emit() {this.callbacks.onPreferences?.(this.preferences, this.wantPlay);}
    set(key, value) {
      if (!(key in defaults)) return;
      this.preferences[key] = key === 'rate' ? Number(value) : Boolean(value);
      if (key === 'autoplay') this.wantPlay = Boolean(value);
      try {localStorage.setItem('videoReviewer.quickLabel.playback.v1', JSON.stringify(this.preferences));} catch {}
      this.apply(); this.emit();
      if (key === 'autoplay' && this.wantPlay) this.play();
    }
    apply() {
      this.player.muted = this.preferences.muted;
      this.player.loop = this.preferences.loop && !this.boundary;
      applyEffectiveRate(this.player, this.preferences.rate);
    }
    select(id, next) {
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
      this.apply();
      const slot = this.slot;
      try {await slot.element.play();}
      catch (error) {if (slot === this.slot && error.name !== 'AbortError') this.callbacks.onError?.(error.name === 'NotAllowedError' ? '浏览器限制自动播放，按 S 或点击播放开始' : '播放失败，请重试兼容预览');}
    }
    pause() {this.wantPlay = false; this.player.pause(); stopEffectiveRate(this.player); this.emit();}
    toggle() {this.wantPlay ? this.pause() : this.play();}
    tick(slot) {
      if (slot !== this.slot) return;
      const video = slot.element;
      if (this.boundary && this.preferences.loop && this.wantPlay && video.currentTime >= this.boundary.end - 0.025) {
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
      if (this.wantPlay && this.player.paused) this.play();
    }
    preview(start, end) {this.boundary = {start,end}; this.player.loop = false; this.seek(start, true); this.play();}
    clearBoundary() {this.boundary = null; this.apply();}
    dispose() {for (const slot of this.slots) {clearTimeout(slot.timer); slot.controller?.abort(); stopEffectiveRate(slot.element); slot.element.pause();}}
  }
  window.MediaDeck = MediaDeck;
})();
