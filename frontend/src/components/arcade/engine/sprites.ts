/**
 * Pixel art of the waiting arcade.
 *
 * Every sprite is a list of frames, every frame a list of rows; a character is
 * a palette slot (`0` = transparent, `1`..`4` = colours given at render time).
 * The invaders are the original cabinet's silhouettes. `SpriteAtlas` bakes a
 * sprite at a pixel size, in a palette, with its glow, into an offscreen canvas
 * once, so a frame of the game is only `drawImage` calls. The data half is pure
 * (no DOM at import) so `node --test` checks it.
 */

type Frame = readonly string[];

export const SPRITES = {
  squid: [
    ["00011000", "00111100", "01111110", "11011011", "11111111", "00100100", "01011010", "10100101"],
    ["00011000", "00111100", "01111110", "11011011", "11111111", "01011010", "10000001", "01000010"],
  ],
  crab: [
    ["00100000100", "00010001000", "00111111100", "01101110110", "11111111111", "10111111101", "10100000101", "00011011000"],
    ["00100000100", "10010001001", "10111111101", "11101110111", "11111111111", "00111111100", "00100000100", "01000000010"],
  ],
  octopus: [
    ["000011110000", "011111111110", "111111111111", "111001100111", "111111111111", "000110011000", "001101101100", "110000000011"],
    ["000011110000", "011111111110", "111111111111", "111001100111", "111111111111", "001110011100", "011001100110", "001100001100"],
  ],
  /** A spotted beetle - always drops a capsule. */
  bug: [
    ["00100000100", "00011111000", "00111111100", "01122122110", "11111111111", "11221112211", "01111111110", "10100000101"],
    ["10000000001", "01011111010", "00111111100", "01122122110", "11111111111", "11221112211", "01111111110", "01010001010"],
  ],
  /** Halloween week: the bottom rows go orange. */
  pumpkin: [
    ["00000300000", "00001300000", "01111111110", "11211111211", "11111111111", "11121212111", "11112121111", "01111111110"],
    ["00000300000", "00003100000", "01111111110", "11111111111", "11211111211", "11121212111", "11112121111", "01111111110"],
  ],
  /** The holiday season: a hat on the top rows. */
  santaHat: [["0000011", "0001111", "0111110", "2222222"]],
  player: [
    ["0000001000000", "0000011100000", "0000011100000", "0111111111110", "1111111111111", "1111111111111", "1111111111111", "1111111111111"],
  ],
  playerExplosion: [
    ["000000100000000", "000000000100000", "000010010000000", "000000000010010", "010010111100000", "000011111110010", "001111111111000", "111111111111111"],
    ["100001000001001", "001000001000100", "000000111000000", "010001110110000", "000011111100010", "001111111111000", "011111111111110", "111111111111111"],
  ],
  ufo: [
    ["0000011111100000", "0001111111111000", "0011111111111100", "0110110110110110", "1111111111111111", "0011100110011100", "0001000000001000"],
  ],
  duck: [
    ["00001111000000", "00011111100000", "00011311110000", "00011111122200", "00011111122220", "00001111100000", "01111111111000", "11111111111100", "11111111111110", "01111111111100", "00111111111000"],
  ],
  explosion: [
    ["0000100010000", "0100010100010", "0010000000100", "0001000001000", "1100000000011", "0001000001000", "0010010100100", "0100100010010", "0000100010000"],
  ],
  shotRolling: [
    ["010", "001", "010", "100", "010", "001", "010"],
    ["010", "100", "010", "001", "010", "100", "010"],
  ],
  shotPlunger: [
    ["010", "010", "010", "010", "010", "010", "111"],
    ["111", "010", "010", "010", "010", "010", "010"],
  ],
  shotSquiggly: [
    ["001", "010", "100", "010", "001", "010", "100"],
    ["100", "010", "001", "010", "100", "010", "001"],
  ],
  hallucination: [
    [
      "00000111111111100000",
      "00011111111111111000",
      "00111111111111111100",
      "01111444444444411110",
      "01114433333333441110",
      "11114333322333341111",
      "11114333222233341111",
      "11114333222233341111",
      "11114333322333341111",
      "01114433333333441110",
      "01111444444444411110",
      "00111111111111111100",
      "01100110011001100110",
      "10011001100110011001",
    ],
    [
      "00000111111111100000",
      "00011111111111111000",
      "00111111111111111100",
      "01111444444444411110",
      "01114433333333441110",
      "11114333333333341111",
      "11114332222333341111",
      "11114332222333341111",
      "11114333333333341111",
      "01114433333333441110",
      "01111444444444411110",
      "00111111111111111100",
      "10011001100110011001",
      "01100110011001100110",
    ],
  ],
  minion: [
    ["00111100", "01111110", "11011011", "11111111", "11111111", "01100110", "11000011"],
    ["00111100", "01111110", "11011011", "11111111", "11111111", "01100110", "00100100"],
  ],
  // Capsule glyphs.
  puRapid: [["1001000", "1101100", "1111110", "1111111", "1111110", "1101100", "1001000"]],
  puSpread: [["1001001", "0101010", "0011100", "0001000", "0001000", "0001000", "0001000"]],
  puLaser: [["0011100", "0011100", "0011100", "0011100", "0111110", "1111111", "0111110"]],
  puShield: [["0111110", "1100011", "1000001", "1000001", "0100010", "0010100", "0001000"]],
  puSlow: [["1111111", "0100010", "0010100", "0001000", "0010100", "0100010", "1111111"]],
  puMissiles: [["0001000", "0011100", "0011100", "0011100", "0111110", "1101011", "1000001"]],
  puSudo: [["0101010", "1111111", "0101010", "0101010", "1111111", "0101010", "0000000"]],
  puOneUp: [["0110110", "1111111", "1111111", "1111111", "0111110", "0011100", "0001000"]],
  puNuke: [["0010100", "0110110", "1110111", "0001000", "0000000", "0011100", "0011100"]],
} as const satisfies Record<string, readonly Frame[]>;

export type SpriteName = keyof typeof SPRITES;

/** Colours for palette slots `1`..`4`. */
export type Palette = Partial<Record<"1" | "2" | "3" | "4", string>>;

export function spriteSize(name: SpriteName): { w: number; h: number } {
  const frame = SPRITES[name][0];
  return { w: frame[0].length, h: frame.length };
}

export function frameCount(name: SpriteName): number {
  return SPRITES[name].length;
}

/** A sprite baked at a pixel size: draw `canvas` at (x - pad, y - pad). */
interface BakedSprite {
  canvas: HTMLCanvasElement;
  pad: number;
  /** Drawn size in CSS pixels, glow excluded. */
  w: number;
  h: number;
}

export class SpriteAtlas {
  private readonly cache = new Map<string, BakedSprite>();

  get(name: SpriteName, frame: number, px: number, palette: Palette, glow: string | null = null): BakedSprite {
    const frames = SPRITES[name];
    const index = ((frame % frames.length) + frames.length) % frames.length;
    const key = `${name}|${index}|${px}|${palette["1"] ?? ""}|${palette["2"] ?? ""}|${palette["3"] ?? ""}|${palette["4"] ?? ""}|${glow ?? ""}`;
    const cached = this.cache.get(key);
    if (cached) return cached;
    const baked = bake(frames[index], px, palette, glow);
    this.cache.set(key, baked);
    return baked;
  }

  clear(): void {
    this.cache.clear();
  }
}

function bake(rows: Frame, px: number, palette: Palette, glow: string | null): BakedSprite {
  const w = rows[0].length * px;
  const h = rows.length * px;
  const pad = glow ? Math.max(4, Math.round(px * 2.5)) : 0;
  const sprite = document.createElement("canvas");
  sprite.width = w;
  sprite.height = h;
  const sctx = sprite.getContext("2d");
  if (sctx) {
    for (let r = 0; r < rows.length; r += 1) {
      const row = rows[r];
      for (let c = 0; c < row.length; c += 1) {
        const slot = row[c] as "0" | "1" | "2" | "3" | "4";
        if (slot === "0") continue;
        const colour = palette[slot];
        if (!colour) continue;
        sctx.fillStyle = colour;
        sctx.fillRect(c * px, r * px, px, px);
      }
    }
  }
  if (!glow) return { canvas: sprite, pad: 0, w, h };
  const glowing = document.createElement("canvas");
  glowing.width = w + pad * 2;
  glowing.height = h + pad * 2;
  const gctx = glowing.getContext("2d");
  if (gctx) {
    gctx.shadowColor = glow;
    gctx.shadowBlur = pad;
    gctx.drawImage(sprite, pad, pad);
    gctx.shadowBlur = 0;
    gctx.drawImage(sprite, pad, pad);
  }
  return { canvas: glowing, pad, w, h };
}
