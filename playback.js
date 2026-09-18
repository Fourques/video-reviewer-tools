/* Playback preferences are independent of project labels and review progress. */
(function (root) {
  const RATES = [0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4, 8, 12, 16, 20];
  const DEFAULTS = {rate:1.5, autoplay:true, loop:true, muted:true};
  // How often a speed above the browser ceiling pulls the clip forward. Every pull
  // costs a seek and a seek costs playback time, so the tick has to be long enough to
  // let one land: 120ms measured 20.0x where a 250ms tick only reached 19.7x, and it
  // keeps each jump near a second of media instead of one and a half.
  const CATCH_UP_TICK = 120;
  // How much of a clip's end is left to play at the ceiling rate. A reviewer must never
  // have the last moments of a clip skipped past, so the correction stands down there.
  const TAIL = 0.25;
  const catchUps = new WeakMap();
  // Browsers refuse a playbackRate above their own ceiling (Chromium and Firefox
  // throw NotSupportedError past 16x), so a faster speed is played at the ceiling
  // and the missing media time is skipped, keeping the real speed honest.
  function maxPlaybackRate(player) {
    const restore = player.playbackRate;
    let ceiling = 1;
    for (const candidate of [2, 4, 8, 16, 20, 32, 64]) {
      try { player.playbackRate = candidate; ceiling = candidate; } catch { break; }
    }
    try { player.playbackRate = restore; } catch {}
    return ceiling;
  }
  function stopEffectiveRate(player) {
    const running = catchUps.get(player);
    if (!running) return;
    clearInterval(running.timer);
    catchUps.delete(player);
  }
  // Returns the native rate the browser accepted plus the media seconds per wall
  // second that the skip handler has to make up.
  function applyEffectiveRate(player, rate) {
    const native = Math.min(rate, maxPlaybackRate(player));
    player.defaultPlaybackRate = native;
    player.playbackRate = native;
    const extra = Math.max(0, rate - native);
    const running = catchUps.get(player);
    // Re-applying the same speed must leave the correction alone: canplay fires
    // repeatedly during playback, and rebuilding the handler would drop whatever
    // shortfall has built up since the last slice.
    if (running && running.rate === rate) return {native, extra};
    stopEffectiveRate(player);
    if (extra <= 0.001) return {native, extra:0};
    // The clip has to sit at rate x wall-clock and the ceiling only plays native x of
    // it, so each tick pulls the clip up to where that clock says it should be.
    // Measuring against the clock rather than counting fixed steps keeps the real speed
    // when a seek stalls playback for a moment, which is what a seek per tick costs.
    const state = {rate, wall: Date.now(), media: Number(player.currentTime) || 0};
    state.timer = setInterval(() => {
      const now = Number(player.currentTime) || 0;
      const duration = player.duration;
      const mark = Date.now();
      // A pause stops the clock, not the flow; a seek that has not landed must not be
      // stacked on, or the element never leaves seeking and the clip crawls. Both mean
      // the clock starts again from wherever the clip really is.
      if (player.paused || player.seeking) { state.wall = mark; state.media = now; return; }
      const projected = state.media + rate * (mark - state.wall) / 1000;
      if (!Number.isFinite(duration)) {
        if (now < projected - 0.05) player.currentTime = projected;
        return;
      }
      // A clip that loops wraps on the projection's own schedule, so the projection is
      // read modulo the clip's length: it always names a place inside the pass the clip
      // is in, and the seek that costs time on the way there is still paid back.
      const expected = projected % duration;
      // The last moments of a clip are never skipped past: the tail plays at the ceiling.
      if (now > duration - TAIL) return;
      if (now < expected - 0.05) player.currentTime = Math.min(duration, expected);
    }, CATCH_UP_TICK);
    catchUps.set(player, state);
    return {native, extra};
  }
  // A seek moves the clip somewhere the correction never planned for: the deadline has
  // to start again from there, or the next tick drags the clip back to where it thought
  // it should be and the user's fast-forward looks like it did nothing.
  function reanchorEffectiveRate(player) {
    const running = catchUps.get(player);
    if (!running) return;
    running.wall = Date.now();
    running.media = Number(player.currentTime) || 0;
  }
  function migrateSeekPreference(saved) {
    const value = Number(saved.seekSeconds);
    if (!Number.isFinite(value) || value <= 0) return 1;
    if ((saved.seekDefaultVersion || 0) < 2 && value === 5) return 1;
    return Math.min(600, Math.max(0.1, value));
  }
  class ReviewPlayback {
    constructor(player, options={}) {
      this.player = player;
      this.key = options.key || 'videoReviewer.quickLabel.playback.v1';
      try { this.storage = options.storage || root.localStorage; } catch { this.storage = null; }
      this.onChange = options.onChange || (() => {});
      this.onBlocked = options.onBlocked || (() => {});
      this.settings = {...DEFAULTS};
      try {
        const saved = JSON.parse(this.storage?.getItem(this.key) || '{}');
        if (RATES.includes(Number(saved.rate))) this.settings.rate = Number(saved.rate);
        for (const key of ['autoplay','loop','muted']) if (typeof saved[key] === 'boolean') this.settings[key] = saved[key];
      } catch {}
      this.generation = 0;
      this.wantPlay = false;
      // A deliberate stop stays in force for the following clips until the user plays again.
      this.userStopped = false;
      this.resetting = true;
      this.pending = false;
      this.seekTarget = null;
      player.addEventListener('seeked', () => this.flushSeek());
      player.addEventListener('loadedmetadata', () => { this.resetting = false; this.apply(); });
      player.addEventListener('canplay', () => {
        this.apply();
        if (this.wantPlay && player.paused && !this.pending) this.play();
      });
      player.addEventListener('play', () => { this.wantPlay = true; });
      player.addEventListener('pause', () => {
        // Only a deliberate stop counts. Switching clips, seeking and a clip that
        // simply reached its end fire pause as well, and none of them mean "stop".
        if (this.resetting || player.readyState < 2) return;
        if (player.seeking || this.seekTarget !== null || this.atEnd()) return;
        this.wantPlay = false; this.holdStopped();
      });
      player.addEventListener('volumechange', () => {
        if (!this.resetting && this.settings.muted !== player.muted) {
          this.settings.muted = player.muted; this.save();
        }
      });
      this.apply();
      this.onChange(this.settings, this.status());
    }
    status() {
      return {userStopped: this.userStopped};
    }
    // A clip that played to its end is paused, and a keyboard fast-forward lands
    // there too: neither is the user asking for the flow to stop.
    atEnd() {
      if (this.player.ended) return true;
      const duration = this.player.duration;
      return Number.isFinite(duration) && duration > 0 && this.player.currentTime >= duration - 0.25;
    }
    // Only report actual transitions: the player fires pause for reasons that are
    // not a user stop (switching clips, seeking), and those must stay silent.
    holdStopped() {
      if (this.userStopped) return;
      this.userStopped = true; this.onChange(this.settings, this.status());
    }
    resumeAutoplay() {
      if (!this.userStopped) return;
      this.userStopped = false; this.onChange(this.settings, this.status());
    }
    save() {
      try { this.storage?.setItem(this.key, JSON.stringify(this.settings)); } catch {}
      this.onChange(this.settings, this.status());
    }
    apply() {
      // load() can reset playbackRate: set the default AND restore after metadata.
      applyEffectiveRate(this.player, this.settings.rate);
      this.player.loop = this.settings.loop;
      this.player.muted = this.settings.muted;
    }
    set(key, value) {
      if (key === 'rate' && !RATES.includes(Number(value))) return;
      if (!(key in DEFAULTS)) return;
      this.settings[key] = key === 'rate' ? Number(value) : Boolean(value);
      if (key === 'autoplay' && this.settings.autoplay) this.resumeAutoplay();
      if (key === 'autoplay' && this.player.paused) this.wantPlay = Boolean(value);
      this.apply(); this.save();
    }
    reset() { this.settings = {...DEFAULTS}; this.userStopped = false; this.apply(); this.save(); }
    clear() {
      this.seekTarget = null;
      this.generation++; this.wantPlay = false; this.pending = false; this.resetting = true;
      stopEffectiveRate(this.player);
      this.player.pause(); this.player.removeAttribute('src'); this.player.load();
    }
    load(source) {
      this.seekTarget = null;
      this.generation++; this.resetting = true; this.pending = false;
      this.wantPlay = this.settings.autoplay && !this.userStopped;
      this.apply(); this.player.src = source; this.player.load(); this.apply();
    }
    async play() {
      this.resumeAutoplay();
      this.wantPlay = true; this.apply(); this.pending = true;
      const generation = this.generation;
      try { await this.player.play(); }
      catch (error) {
        if (generation === this.generation && this.wantPlay && error.name !== 'AbortError') {
          this.wantPlay = false; this.onBlocked(error);
        }
      } finally { if (generation === this.generation) this.pending = false; }
    }
    pause() { this.wantPlay = false; this.holdStopped(); this.player.pause(); }
    seekBy(delta, duration) {
      this.seekTarget = Math.min(duration, Math.max(0, (this.seekTarget ?? this.player.currentTime) + delta));
      this.flushSeek();
    }
    flushSeek() {
      // Coalesce repeated A/D presses: overlapping seeks can stall Chromium.
      if (this.player.seeking || this.seekTarget === null) return;
      const target = this.seekTarget; this.seekTarget = null;
      const finished = this.atEnd();
      if (Math.abs(this.player.currentTime - target) > 0.001) {
        this.player.currentTime = target;
        reanchorEffectiveRate(this.player);
      }
      // Fast-forwarding a clip that already ran to its end (loop off) must keep
      // playing: the flow stays "wanted" until the user stops it.
      if (finished && this.wantPlay && this.player.paused && !this.pending) this.play();
    }
    toggle() {
      if (!this.player.paused || (this.wantPlay && this.player.readyState < 3)) this.pause();
      else this.play();
    }
  }
  root.ReviewPlayback = ReviewPlayback;
  root.migrateSeekPreference = migrateSeekPreference;
  root.applyEffectiveRate = applyEffectiveRate;
  root.stopEffectiveRate = stopEffectiveRate;
  root.reanchorEffectiveRate = reanchorEffectiveRate;
  root.maxPlaybackRate = maxPlaybackRate;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {ReviewPlayback, migrateSeekPreference, applyEffectiveRate, stopEffectiveRate, maxPlaybackRate, reanchorEffectiveRate};
  }
})(globalThis);
