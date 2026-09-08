/* Playback preferences are independent of project labels and review progress. */
(function (root) {
  const RATES = [0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4];
  const DEFAULTS = {rate:1.5, autoplay:true, loop:true, muted:true};
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
        if (!this.resetting && player.readyState >= 2 && !player.ended) this.wantPlay = false;
      });
      player.addEventListener('volumechange', () => {
        if (!this.resetting && this.settings.muted !== player.muted) {
          this.settings.muted = player.muted; this.save();
        }
      });
      this.apply();
      this.onChange(this.settings);
    }
    save() {
      try { this.storage?.setItem(this.key, JSON.stringify(this.settings)); } catch {}
      this.onChange(this.settings);
    }
    apply() {
      // load() can reset playbackRate: set the default AND restore after metadata.
      this.player.defaultPlaybackRate = this.settings.rate;
      this.player.playbackRate = this.settings.rate;
      this.player.loop = this.settings.loop;
      this.player.muted = this.settings.muted;
    }
    set(key, value) {
      if (key === 'rate' && !RATES.includes(Number(value))) return;
      if (!(key in DEFAULTS)) return;
      this.settings[key] = key === 'rate' ? Number(value) : Boolean(value);
      if (key === 'autoplay' && this.player.paused) this.wantPlay = Boolean(value);
      this.apply(); this.save();
    }
    reset() { this.settings = {...DEFAULTS}; this.apply(); this.save(); }
    clear() {
      this.seekTarget = null;
      this.generation++; this.wantPlay = false; this.pending = false; this.resetting = true;
      this.player.pause(); this.player.removeAttribute('src'); this.player.load();
    }
    load(source) {
      this.seekTarget = null;
      this.generation++; this.resetting = true; this.pending = false;
      this.wantPlay = this.settings.autoplay;
      this.apply(); this.player.src = source; this.player.load(); this.apply();
    }
    async play() {
      this.wantPlay = true; this.apply(); this.pending = true;
      const generation = this.generation;
      try { await this.player.play(); }
      catch (error) {
        if (generation === this.generation && this.wantPlay && error.name !== 'AbortError') {
          this.wantPlay = false; this.onBlocked(error);
        }
      } finally { if (generation === this.generation) this.pending = false; }
    }
    pause() { this.wantPlay = false; this.player.pause(); }
    seekBy(delta, duration) {
      this.seekTarget = Math.min(duration, Math.max(0, (this.seekTarget ?? this.player.currentTime) + delta));
      this.flushSeek();
    }
    flushSeek() {
      // Coalesce repeated A/D presses: overlapping seeks can stall Chromium.
      if (this.player.seeking || this.seekTarget === null) return;
      const target = this.seekTarget; this.seekTarget = null;
      if (Math.abs(this.player.currentTime - target) > 0.001) this.player.currentTime = target;
    }
    toggle() {
      if (!this.player.paused || (this.wantPlay && this.player.readyState < 3)) this.pause();
      else this.play();
    }
  }
  root.ReviewPlayback = ReviewPlayback;
  root.migrateSeekPreference = migrateSeekPreference;
  if (typeof module !== 'undefined' && module.exports) module.exports = {ReviewPlayback, migrateSeekPreference};
})(globalThis);
