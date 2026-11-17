/**
 * Chiptune sound of the waiting arcade, synthesized with WebAudio.
 *
 * No asset ships: every effect is an oscillator or a noise burst with an
 * envelope, the classic four-note march included. The context is created on
 * the first user gesture (browsers refuse audio before one), and every call is
 * guarded - sound is garnish, it must never break the game.
 */

type SfxName =
  | "shoot"
  | "invaderDie"
  | "playerDie"
  | "ufoHit"
  | "powerUp"
  | "missile"
  | "explosion"
  | "crumble"
  | "extraLife"
  | "bossAlarm"
  | "bossHit"
  | "bossDie"
  | "gameOver"
  | "answerReady"
  | "waveClear"
  | "quack"
  | "nukeSiren"
  | "shieldHit"
  | "deny"
  | "achievement"
  | "laser"
  | "rival";

/** The march: four descending bass notes, one per formation step. */
const MARCH_NOTES = [98, 87.31, 77.78, 73.42];
const MASTER_GAIN = 0.2;

type AudioContextCtor = typeof AudioContext;

function audioContextCtor(): AudioContextCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as Window & { webkitAudioContext?: AudioContextCtor };
  return window.AudioContext ?? w.webkitAudioContext ?? null;
}

export class ArcadeAudio {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private noise: AudioBuffer | null = null;
  private ufo: { osc: OscillatorNode; lfo: OscillatorNode; gain: GainNode } | null = null;
  private muted: boolean;

  constructor(muted: boolean) {
    this.muted = muted;
  }

  /** Create or resume the context. Call from a user gesture. */
  unlock(): void {
    try {
      if (!this.ctx) {
        const Ctor = audioContextCtor();
        if (!Ctor) return;
        this.ctx = new Ctor();
        this.master = this.ctx.createGain();
        this.master.gain.value = this.muted ? 0 : MASTER_GAIN;
        this.master.connect(this.ctx.destination);
        const length = Math.floor(this.ctx.sampleRate * 0.6);
        this.noise = this.ctx.createBuffer(1, length, this.ctx.sampleRate);
        const data = this.noise.getChannelData(0);
        for (let i = 0; i < length; i += 1) data[i] = Math.random() * 2 - 1;
      }
      if (this.ctx.state === "suspended") void this.ctx.resume();
    } catch {
      this.ctx = null;
    }
  }

  get isMuted(): boolean {
    return this.muted;
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    try {
      if (this.master && this.ctx) {
        this.master.gain.setTargetAtTime(muted ? 0 : MASTER_GAIN, this.ctx.currentTime, 0.02);
      }
    } catch {
      /* ignore */
    }
  }

  /** Pause everything (tab hidden, game paused). */
  suspend(): void {
    try {
      void this.ctx?.suspend();
    } catch {
      /* ignore */
    }
  }

  resume(): void {
    try {
      if (this.ctx?.state === "suspended") void this.ctx.resume();
    } catch {
      /* ignore */
    }
  }

  march(step: number): void {
    const note = MARCH_NOTES[((step % 4) + 4) % 4];
    this.tone({ from: note, to: note * 0.96, duration: 0.09, type: "square", gain: 0.5 });
  }

  play(name: SfxName): void {
    if (!this.ctx || this.muted) return;
    try {
      switch (name) {
        case "shoot":
          this.tone({ from: 1400, to: 380, duration: 0.09, type: "square", gain: 0.18 });
          break;
        case "laser":
          this.tone({ from: 2200, to: 900, duration: 0.14, type: "sawtooth", gain: 0.12 });
          break;
        case "invaderDie":
          this.burst({ duration: 0.16, gain: 0.35, from: 3200, to: 300 });
          this.tone({ from: 220, to: 80, duration: 0.12, type: "square", gain: 0.12 });
          break;
        case "playerDie":
          this.burst({ duration: 0.9, gain: 0.5, from: 2400, to: 120 });
          this.tone({ from: 440, to: 55, duration: 0.8, type: "sawtooth", gain: 0.18 });
          break;
        case "ufoHit":
          [880, 660, 520, 440, 330].forEach((f, i) =>
            this.tone({ from: f, to: f * 0.9, duration: 0.08, type: "square", gain: 0.2, delay: i * 0.06 }),
          );
          break;
        case "powerUp":
          [523, 659, 784, 1046].forEach((f, i) =>
            this.tone({ from: f, to: f, duration: 0.08, type: "triangle", gain: 0.3, delay: i * 0.06 }),
          );
          break;
        case "missile":
          this.burst({ duration: 0.35, gain: 0.25, from: 600, to: 2600, filter: "bandpass" });
          break;
        case "explosion":
          this.burst({ duration: 0.6, gain: 0.55, from: 1800, to: 90 });
          this.tone({ from: 120, to: 40, duration: 0.5, type: "sine", gain: 0.5 });
          break;
        case "crumble":
          this.burst({ duration: 0.45, gain: 0.3, from: 5000, to: 900, filter: "highpass" });
          break;
        case "extraLife":
          [523, 659, 784, 659, 784, 1046].forEach((f, i) =>
            this.tone({ from: f, to: f, duration: 0.09, type: "square", gain: 0.2, delay: i * 0.08 }),
          );
          break;
        case "bossAlarm":
          [0, 0.3, 0.6].forEach((d) => this.tone({ from: 740, to: 520, duration: 0.25, type: "square", gain: 0.22, delay: d }));
          break;
        case "bossHit":
          this.tone({ from: 300, to: 180, duration: 0.07, type: "square", gain: 0.2 });
          break;
        case "bossDie":
          this.burst({ duration: 1.4, gain: 0.6, from: 2600, to: 60 });
          [392, 523, 659, 784, 1046].forEach((f, i) =>
            this.tone({ from: f, to: f, duration: 0.12, type: "square", gain: 0.18, delay: 0.5 + i * 0.1 }),
          );
          break;
        case "gameOver":
          [523, 494, 440, 392, 349, 330, 262].forEach((f, i) =>
            this.tone({ from: f, to: f * 0.98, duration: 0.18, type: "triangle", gain: 0.3, delay: i * 0.16 }),
          );
          break;
        case "answerReady":
          [659, 988].forEach((f, i) => this.tone({ from: f, to: f, duration: 0.22, type: "sine", gain: 0.4, delay: i * 0.14 }));
          break;
        case "waveClear":
          [392, 523, 659, 784].forEach((f, i) =>
            this.tone({ from: f, to: f, duration: 0.1, type: "square", gain: 0.18, delay: i * 0.07 }),
          );
          break;
        case "quack":
          this.tone({ from: 520, to: 340, duration: 0.14, type: "sawtooth", gain: 0.3 });
          this.tone({ from: 480, to: 320, duration: 0.14, type: "sawtooth", gain: 0.3, delay: 0.18 });
          break;
        case "nukeSiren":
          [0, 0.5, 1, 1.5, 2].forEach((d) => this.tone({ from: 440, to: 880, duration: 0.45, type: "sawtooth", gain: 0.16, delay: d }));
          break;
        case "shieldHit":
          this.tone({ from: 1600, to: 2400, duration: 0.12, type: "triangle", gain: 0.25 });
          break;
        case "deny":
          this.tone({ from: 180, to: 140, duration: 0.18, type: "square", gain: 0.2 });
          break;
        case "achievement":
          [784, 1046, 1318].forEach((f, i) => this.tone({ from: f, to: f, duration: 0.12, type: "triangle", gain: 0.3, delay: i * 0.09 }));
          break;
        case "rival":
          [659, 784, 988].forEach((f, i) => this.tone({ from: f, to: f, duration: 0.1, type: "square", gain: 0.16, delay: i * 0.07 }));
          break;
      }
    } catch {
      /* sound is garnish */
    }
  }

  startUfo(): void {
    if (!this.ctx || !this.master || this.ufo) return;
    try {
      const osc = this.ctx.createOscillator();
      const lfo = this.ctx.createOscillator();
      const lfoGain = this.ctx.createGain();
      const gain = this.ctx.createGain();
      osc.type = "square";
      osc.frequency.value = 520;
      lfo.frequency.value = 7;
      lfoGain.gain.value = 110;
      lfo.connect(lfoGain);
      lfoGain.connect(osc.frequency);
      gain.gain.value = 0.05;
      osc.connect(gain);
      gain.connect(this.master);
      osc.start();
      lfo.start();
      this.ufo = { osc, lfo, gain };
    } catch {
      this.ufo = null;
    }
  }

  stopUfo(): void {
    if (!this.ufo) return;
    try {
      this.ufo.osc.stop();
      this.ufo.lfo.stop();
      this.ufo.gain.disconnect();
    } catch {
      /* ignore */
    }
    this.ufo = null;
  }

  dispose(): void {
    this.stopUfo();
    try {
      void this.ctx?.close();
    } catch {
      /* ignore */
    }
    this.ctx = null;
    this.master = null;
  }

  private tone(opts: {
    from: number;
    to: number;
    duration: number;
    type: OscillatorType;
    gain: number;
    delay?: number;
  }): void {
    if (!this.ctx || !this.master || this.muted) return;
    try {
      const start = this.ctx.currentTime + (opts.delay ?? 0);
      const osc = this.ctx.createOscillator();
      const env = this.ctx.createGain();
      osc.type = opts.type;
      osc.frequency.setValueAtTime(Math.max(1, opts.from), start);
      osc.frequency.exponentialRampToValueAtTime(Math.max(1, opts.to), start + opts.duration);
      env.gain.setValueAtTime(0.0001, start);
      env.gain.exponentialRampToValueAtTime(opts.gain, start + 0.008);
      env.gain.exponentialRampToValueAtTime(0.0001, start + opts.duration);
      osc.connect(env);
      env.connect(this.master);
      osc.start(start);
      osc.stop(start + opts.duration + 0.02);
    } catch {
      /* ignore */
    }
  }

  private burst(opts: {
    duration: number;
    gain: number;
    from: number;
    to: number;
    filter?: BiquadFilterType;
    delay?: number;
  }): void {
    if (!this.ctx || !this.master || !this.noise || this.muted) return;
    try {
      const start = this.ctx.currentTime + (opts.delay ?? 0);
      const source = this.ctx.createBufferSource();
      source.buffer = this.noise;
      const filter = this.ctx.createBiquadFilter();
      filter.type = opts.filter ?? "lowpass";
      filter.frequency.setValueAtTime(opts.from, start);
      filter.frequency.exponentialRampToValueAtTime(Math.max(20, opts.to), start + opts.duration);
      const env = this.ctx.createGain();
      env.gain.setValueAtTime(opts.gain, start);
      env.gain.exponentialRampToValueAtTime(0.0001, start + opts.duration);
      source.connect(filter);
      filter.connect(env);
      env.connect(this.master);
      source.start(start);
      source.stop(start + opts.duration + 0.02);
    } catch {
      /* ignore */
    }
  }
}
