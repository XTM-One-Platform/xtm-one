"""The synthetic booth the screen insert tests compose on: no provider, no browser, no database.

A noisy wall around a dark bezel and a flat green panel (:func:`booth`), the keyed insert of a product UI on it
(:func:`source_of`), the planned documents the page is handed (:func:`clip_document`), and :class:`PresenceRender`: a
render of one frame by frame - each scene's insert drawn onto its own footage (``screen_insert.compose``), the scenes'
layers set as the page's ``applyTransition`` sets them (:func:`transition_layers`), its flash and the video's fades laid
over them, each frame put through a lossy encode - which the presence gate reads (``insert_checks.PresenceGate``).
"""

from __future__ import annotations

import io
import math
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from app.services.video_studio import insert_checks, screen_insert, timeline
from app.services.video_studio.screen_insert import Footage, InsertSource, KeySpec

W, H = 480, 270
FPS = 24
GREEN = (32, 230, 40)
BEZEL = (28, 28, 32)
#: The panel on the booth's wall, in pixels of the footage (which fills the frame).
SCREEN = np.array([(150, 66), (330, 66), (330, 200), (150, 200)], dtype=float)
QUAD = [[round(x / W, 6), round(y / H, 6)] for x, y in SCREEN]
#: Each scene's UI ground: two scenes' inserts never read alike.
GROUNDS = {"a": (40, 46, 60), "b": (60, 44, 48), "c": (44, 60, 46)}
#: The page's stage the frames stand for: a blur's pixels are the stage's.
STAGE_W = 1920


def booth(seed: int) -> np.ndarray:
    """A noisy wall, a dark bezel around a flat green panel, through a lens and an encoder."""
    rng = np.random.default_rng(seed)
    picture = Image.fromarray((rng.random((H, W, 3)) * 20 + 95).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    draw.polygon([tuple(p) for p in screen_insert._grown_px(SCREEN, 6.0)], fill=BEZEL)
    draw.polygon([tuple(p) for p in SCREEN], fill=GREEN)
    frame = np.asarray(picture.filter(ImageFilter.GaussianBlur(0.8))).astype(np.float32)
    frame += np.random.default_rng(seed + 1000).normal(0.0, 2.0, frame.shape).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def tiled_booth(seed: int) -> np.ndarray:
    """The booth of :func:`booth` on a wall of coarse tiles: a texture a transition's blur leaves standing."""
    rng = np.random.default_rng(seed)
    tiles = rng.integers(60, 170, (H // 6 + 1, W // 6 + 1, 1)).repeat(3, axis=2).astype(np.uint8)
    picture = Image.fromarray(tiles.repeat(6, axis=0).repeat(6, axis=1)[:H, :W])
    draw = ImageDraw.Draw(picture)
    draw.polygon([tuple(p) for p in screen_insert._grown_px(SCREEN, 6.0)], fill=BEZEL)
    draw.polygon([tuple(p) for p in SCREEN], fill=GREEN)
    frame = np.asarray(picture.filter(ImageFilter.GaussianBlur(0.8))).astype(np.float32)
    frame += np.random.default_rng(seed + 1000).normal(0.0, 2.0, frame.shape).astype(np.float32)
    return np.clip(frame, 0, 255).astype(np.uint8)


def text_ui(ground: tuple[int, int, int]) -> Image.Image:
    """A dark product UI: lines of grey text on a flat ground of little saturation."""
    picture = Image.new("RGB", (600, 450), ground)
    draw = ImageDraw.Draw(picture)
    for row in range(40, 410, 30):
        draw.rectangle((40, row, 360, row + 10), fill=(196, 200, 210))
    return picture


def dim_ui(ground: tuple[int, int, int], lift: int = 16) -> Image.Image:
    """A dark product UI of small, dim text: rows of short labels *lift* grey levels lighter than its ground."""
    picture = Image.new("RGB", (600, 450), ground)
    draw = ImageDraw.Draw(picture)
    for row in range(40, 410, 22):
        for column in range(40, 520, 70):
            draw.rectangle((column, row, column + 50, row + 5), fill=tuple(c + lift for c in ground))
    return picture


def key_of(scene_id: str) -> str:
    """The key of scene *scene_id*'s one screen insert."""
    return f"insert/{scene_id}.0"


def source_of(scene_id: str) -> InsertSource:
    """Scene *scene_id*'s screen insert: a picture keyed onto the booth's green panel."""
    quad = tuple(tuple(p) for p in QUAD)
    return InsertSource(
        f"{scene_id}.png",
        "image",
        quad,
        key=KeySpec("green", 0.4, 3.0),
        footage=Footage("f", "video", float(FPS), f"clip/{scene_id}"),
        box=tuple(screen_insert.grown_box(quad, W / H)),
        label=f"Scene {scene_id}: its screen insert 0",
    )


def clip_document(scenes: list[tuple], *, to_end: bool = True) -> dict:
    """A planned document of clip scenes, each ``(id, start, end, planned length, transition in, its seconds)``: its
    keyed insert from 0 for 14 s, planned when the scene was *planned length* long - then held longer to its end (the
    music's beat, a hold) - its surfaces held as the planner holds them (``timeline.hold_surfaces``). Without *to_end*,
    the insert is planned as before: it ends at the planned length."""
    planned = []
    for scene_id, start, end, length, kind, seconds in scenes:
        source = source_of(scene_id)
        payload = {
            "src": key_of(scene_id),
            "quad": QUAD,
            "box": [list(p) for p in source.box],
            "t0": 0.0,
            "t1": length,
            "keyed": True,
            "matte": {"fps": FPS, "frames": 400},
            **({"to_end": True} if to_end else {}),
        }
        timeline.hold_surfaces([payload], round(end - start, 4))
        planned.append(
            {"id": scene_id, "type": "clip", "start": start, "end": end, "transition_in": {"type": kind, "duration": seconds}, "inserts": [payload]}
        )
    duration = planned[-1]["end"]
    return {"fps": FPS, "frames": math.ceil(duration * FPS), "duration": duration, "fade_in": 0.0, "fade_out": 0.0, "scenes": planned}


def clamp01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def out3(x: float) -> float:
    """The page's ``out3`` ease."""
    return 1.0 - (1.0 - x) ** 3


def smootherstep(p: float) -> float:
    """The page's ``move`` ease."""
    return p * p * p * (p * (p * 6 - 15) + 10)


def weights(scene: dict, at: float) -> tuple[float, float]:
    """``(the scene it leaves, the scene coming in)`` opacities at *at* of the transition into *scene* (the page's
    ``applyTransition``): a fade_black's, else the default dissolve's (the scene left stays whole under it)."""
    tr = scene["transition_in"]
    p = clamp01((at - scene["start"]) / tr["duration"]) if tr["duration"] > 0 else 1.0
    if p >= 1.0:
        return 1.0, 1.0
    if tr["type"] == "fade_black":
        return 1.0 - out3(clamp01(p * 2)), out3(clamp01(p * 2 - 1))
    return 1.0, p


@dataclass(frozen=True)
class LayerStyle:
    """What the page's ``applyTransition`` sets on a scene's layer: its opacity, the scale of its transform (about the
    stage's centre), its blur (CSS pixels of the stage), its brightness, and a wipe's edge (percent of the stage's
    width: its mask opaque to 8 under it, clear from 8 over it)."""

    opacity: float = 1.0
    scale: float = 1.0
    blur: float = 0.0
    brightness: float = 1.0
    wipe: float | None = None

    def mask(self) -> np.ndarray:
        """How much of the layer its mask lets through, per column of the frame."""
        if self.wipe is None:
            return np.ones(W, dtype=np.float32)
        percent = (np.arange(W, dtype=np.float32) + 0.5) / W * 100.0
        return np.clip((self.wipe + 8.0 - percent) / 16.0, 0.0, 1.0)

    def spans(self, box: list[float]) -> float:
        """The share of an insert's *box* (``[x0, y0, x1, y1]``, 0-1 of the frame) the layer spans (the page's
        ``spanned``): all of it, or what lies left of a wipe's edge."""
        if self.wipe is None:
            return 1.0
        x0, _y0, x1, _y1 = box
        right = min(max(self.wipe / 100.0, x0), x1)
        return (right - x0) / max(x1 - x0, 1e-9)

    def covers(self, box: list[float]) -> float:
        """How much of an insert's *box* under it this layer hides (the page's ``coverage``): what it spans of it, at
        its opacity."""
        return self.spans(box) * self.opacity


def transition_layers(kind: str, p: float) -> tuple[LayerStyle, LayerStyle, float]:
    """``(the scene it leaves, the scene coming in, the flash's white)`` at progress *p* of a transition of *kind*, as
    the page's ``applyTransition`` sets them - a crossfade's the page's default."""
    e = smootherstep(p)
    if kind == "fade_black":
        return LayerStyle(opacity=1.0 - out3(clamp01(p * 2))), LayerStyle(opacity=out3(clamp01(p * 2 - 1))), 0.0
    if kind == "zoom":
        leaving = LayerStyle(opacity=1.0 - out3(p), scale=1.0 + 0.35 * e, blur=e * 10)
        return leaving, LayerStyle(opacity=out3(p), scale=1.18 - 0.18 * e, blur=(1.0 - e) * 10), 0.0
    if kind == "blur":
        return LayerStyle(opacity=1.0 - e, blur=e * 14), LayerStyle(opacity=e, blur=(1.0 - e) * 14, scale=1.04 - 0.04 * e), 0.0
    if kind == "wipe":
        return LayerStyle(), LayerStyle(wipe=e * 116 - 8), 0.0
    if kind == "flash":
        swap = clamp01((p - 0.44) / 0.12)
        leaving = LayerStyle(opacity=1.0 - swap, brightness=1.0 + 1.5 * out3(clamp01(p * 2)))
        coming = LayerStyle(opacity=swap, brightness=1.0 + 1.5 * (1.0 - out3(clamp01(p * 2 - 1))))
        return leaving, coming, 0.92 * math.sin(math.pi * p) ** 2
    return LayerStyle(), LayerStyle(opacity=e, scale=1.03 - 0.03 * e), 0.0


def painted_layer(rgb: np.ndarray, layer: LayerStyle) -> np.ndarray:
    """A scene's finished frame *rgb* as its *layer* draws it: scaled about the stage's centre, blurred, brightened."""
    picture = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
    if abs(layer.scale - 1.0) > 1e-4:
        s, cx, cy = layer.scale, W / 2, H / 2
        picture = picture.transform((W, H), Image.Transform.AFFINE, (1 / s, 0, cx - cx / s, 0, 1 / s, cy - cy / s), Image.Resampling.BILINEAR)
    radius = layer.blur * layer.scale * W / STAGE_W
    if radius > 0.01:
        picture = picture.filter(ImageFilter.GaussianBlur(radius))
    return np.clip(np.asarray(picture, dtype=np.float32) * layer.brightness, 0, 255)


def video_fade(document: dict, at: float) -> float:
    """How much of the frame at *at* the video's fades from and to black hide (the page's ``#fade``)."""
    fade_in, fade_out, duration = float(document["fade_in"]), float(document["fade_out"]), float(document["duration"])
    coming = 1.0 - clamp01(at / fade_in) if fade_in > 0 else 0.0
    going = clamp01((at - (duration - fade_out)) / fade_out) if fade_out > 0 else 0.0
    return max(coming, going)


class PresenceRender:
    """The render of a planned *document* frame by frame, as the page and the frame server draw it and an encoder keeps
    it: where the page places each insert and how much of it shows (its ``insertRects``: the layer's transform, its
    opacity and what the layers over it cover), and each layer's audit, kept as the frame server keeps them. *wall*
    draws each scene's footage and *pictures* replaces a scene's UI (``dim_ui`` by default). On the frames *removed* the
    page places the insert of scene ``b`` and its picture is not drawn - its surface shows as the footage has it -, or
    *instead* is drawn there when given."""

    def __init__(
        self,
        document: dict,
        *,
        wall: Callable[[int], np.ndarray] = booth,
        removed: set[int] | frozenset[int] = frozenset(),
        instead: Image.Image | None = None,
        pictures: dict[str, Image.Image] | None = None,
    ) -> None:
        self.document = document
        self.removed = set(removed)
        self.instead = instead
        ids = [scene["id"] for scene in document["scenes"]]
        self.footage = {scene_id: wall(7) for scene_id in ids}
        self.pictures = {key_of(scene_id): dim_ui(GROUNDS[scene_id]) for scene_id in ids}
        self.pictures.update({key_of(scene_id): picture for scene_id, picture in (pictures or {}).items()})
        self.inserts, self.fills, self.surfaces, self.states = {}, {}, {}, {}
        for scene_id in ids:
            key, first = key_of(scene_id), self.footage[scene_id]
            self.inserts[key] = screen_insert.keyed_on(source_of(scene_id), first)
            self.fills[key] = screen_insert.static_fill(self.inserts[key], first, (W, H))
            self.surfaces[key] = screen_insert.Surface.of(first, list(map(tuple, SCREEN)))
            self.states[key] = screen_insert.Matting()
        self.audits: dict[str, dict] = {key: {"frames": {}, "evidence": {}, "placed": {}} for key in self.inserts}
        self._drawn: dict[int, dict[str, np.ndarray]] = {}

    def _compose(self, key: str, picture: Image.Image, scene: dict, frame: int, visibility: float | None, state) -> screen_insert.Layer:
        return screen_insert.compose(
            self.inserts[key],
            picture,
            (W, H),
            frame,
            footage=self.footage[scene["id"]],
            surface=self.surfaces[key],
            fill=self.fills[key],
            state=state,
            window_frame=frame,
            visibility=1.0 if visibility is None else visibility,
        )

    def _scene(self, scene: dict, frame: int, layer: LayerStyle, above: list[LayerStyle]) -> np.ndarray:
        rgb = self.footage[scene["id"]].astype(np.float32)
        t = max(0.0, frame / FPS - float(scene["start"]))
        for payload, opacity, visibility in timeline.insert_layers(scene, t):
            if opacity <= 0:
                continue
            key = payload["src"]
            box = [min(max(float(v), 0.0), 1.0) for corner in (payload["box"][0], payload["box"][2]) for v in corner]
            box = [0.5 + (v - 0.5) * layer.scale for v in box]
            shown = layer.opacity * opacity * layer.spans(box) * math.prod(1.0 - over.covers(box) for over in above)
            self.audits[key]["placed"][frame] = [*box, round(shown, 3)]
            drawn = self._compose(key, self.pictures[key], scene, frame, visibility, self.states[key])
            self.audits[key]["frames"][frame] = drawn.audit.as_dict()
            if scene["id"] == "b" and frame in self.removed:
                if self.instead is None:
                    continue
                drawn = self._compose(key, self.instead, scene, frame, visibility, screen_insert.Matting())
            x0, y0, x1, y1 = drawn.warped.box
            image = np.asarray(drawn.warped.image, dtype=np.float32)
            alpha = image[..., 3:4] / 255.0 * opacity
            rgb[y0:y1, x0:x1] = image[..., :3] * alpha + rgb[y0:y1, x0:x1] * (1.0 - alpha)
        return rgb

    def _transition(self, frame: int) -> tuple[list[tuple[dict, LayerStyle]], float]:
        """``([(scene, its layer)], the flash's white)`` of the scenes painted on *frame*, bottom first."""
        at = frame / FPS
        scenes = self.document["scenes"]
        painted = [s for i, s in enumerate(scenes) if at >= s["start"] and (at < s["end"] or i == len(scenes) - 1)]
        if len(painted) == 1:
            return [(painted[0], LayerStyle())], 0.0
        before, after = painted
        tr = after["transition_in"]
        p = clamp01((at - after["start"]) / tr["duration"]) if tr["duration"] > 0 else 1.0
        if p >= 1.0:
            return [(before, LayerStyle()), (after, LayerStyle())], 0.0
        leaving, coming, flash = transition_layers(tr["type"], p)
        return [(before, leaving), (after, coming)], flash

    def layers(self, frame: int) -> dict[str, np.ndarray]:
        """Each scene painted on *frame*, drawn once (a keyed insert's matting carries from frame to frame)."""
        if frame not in self._drawn:
            stack, _flash = self._transition(frame)
            self._drawn[frame] = {
                scene["id"]: self._scene(scene, frame, layer, [over for _s, over in stack[i + 1 :]]) for i, (scene, layer) in enumerate(stack)
            }
        return self._drawn[frame]

    def _stack(self, frame: int, layers: dict[str, np.ndarray]) -> np.ndarray:
        """The layers over a black stage as the page sets them, its flash's white over them, then the video's fade."""
        stack, flash = self._transition(frame)
        out = np.zeros((H, W, 3), dtype=np.float32)
        for scene, layer in stack:
            alpha = (layer.mask() * layer.opacity)[None, :, None]
            out = painted_layer(layers[scene["id"]], layer) * alpha + out * (1.0 - alpha)
        out = out * (1.0 - flash) + 255.0 * flash
        return out * (1.0 - video_fade(self.document, frame / FPS))

    def frame(self, frame: int) -> np.ndarray:
        """Output *frame* as encoded: the scenes the page paints there, blended as ``applyTransition`` blends them."""
        buffer = io.BytesIO()
        Image.fromarray(np.clip(self._stack(frame, self.layers(frame)), 0, 255).astype(np.uint8)).save(buffer, format="JPEG", quality=90)
        return np.asarray(Image.open(io.BytesIO(buffer.getvalue())).convert("RGB"))

    def keep_blends(self, folder: str) -> None:
        """What the frame server keeps of each presence frame blending an insert (``FrameServer.keep_blend``):
        the frame with its scene painted black, then white, where its layer is - its transform, blur, opacity and
        mask kept, as the page's ``tone()`` paints it -, a lossless capture."""
        for key, audit in self.audits.items():
            scene_id = timeline.insert_scene(key)
            for n, spot in audit["placed"].items():
                if not insert_checks.PRESENCE_MIN_SHOWN <= spot[4] < insert_checks.BLEND_WHOLE:
                    continue
                kept = {}
                for tone, value in (("black", 0.0), ("white", 255.0)):
                    layers = {**self.layers(n), scene_id: np.full((H, W, 3), value, dtype=np.float32)}
                    path = Path(folder) / f"blend-{scene_id}-{n:07d}-{tone}.png"
                    Image.fromarray(np.clip(self._stack(n, layers), 0, 255).astype(np.uint8)).save(path)
                    kept[tone] = str(path)
                audit.setdefault("blend", {})[n] = kept

    def gate(self) -> insert_checks.PresenceGate:
        """The presence gate over every encoded frame, as a render reads its own MP4, each frame blending an insert
        unblended with its scene painted black and white."""
        encoded = {n: self.frame(n) for n in range(int(self.document["frames"]))}
        placed = insert_checks.placed_inserts(self.document, self.inserts)
        with tempfile.TemporaryDirectory() as folder:
            self.keep_blends(folder)
            gate = insert_checks.PresenceGate(self.audits, placed, self.document, self.inserts, fps=FPS)
            for n, picture in encoded.items():
                gate.read(n, picture, {need: self.pictures[need[0]] for need in gate.needs(n)})
        return gate
