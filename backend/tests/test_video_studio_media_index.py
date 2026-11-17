"""The one media reference index a purge reads. Synthetic specs, params and rows.

A purge used to read only the media a video's scenes play: the files a spec
names, what the AI shots it plays are made from and what its ready renders
drew were never in ``video_projects.asset_refs``.

* the spec's part names every medium wherever the spec names it: a screen
  insert's source and a wall's panes, a backdrop, an image scene and its
  animation, an overlay, a figure's pictures, the logo, the music;
* a shot names what it is made from: its start picture, the recording, shot
  or file a frame was cut from and the frame kept, its reference images and
  its ingredients;
* the index of a video adds, shot by shot, what the shots it plays are made
  from (a shot written before its sources were stored read from its params),
  and what its ready renders drew; a loop of shots ends;
* a deletion keeps the files a video needs.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services.video_studio import assets, clips, retention
from app.services.video_studio.spec import asset_refs


def _ids(n: int) -> list[str]:
    return [str(uuid.uuid4()) for _ in range(n)]


def test_the_spec_names_every_medium_wherever_it_names_it():
    take, insert_take, shot, insert_shot, wall_shot, backdrop_shot, animation, track = _ids(8)
    picture, insert_picture, backdrop_picture, overlay_picture, figure_picture, logo, sound, clip_file = _ids(8)
    quad = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
    spec = {
        "title": "Film",
        "theme": {"logo_file_id": logo},
        "music": {"file_id": sound, "music_id": track},
        "scenes": [
            {
                "type": "recording",
                "take_id": take,
                "overlays": [
                    {"type": "screen_insert", "source": {"take_id": insert_take}, "quad": quad},
                    {"type": "screen_insert", "source": {"file_id": insert_picture}, "quad": quad},
                    {"type": "image", "file_id": overlay_picture},
                ],
            },
            {
                "type": "clip",
                "clip_id": shot,
                "overlays": [
                    {"type": "screen_insert", "source": {"clip_id": insert_shot}, "quad": quad},
                    {"type": "screen_insert", "panes": 2, "sources": [{"clip_id": wall_shot}, {"asset": "logo"}]},
                ],
            },
            {"type": "clip", "file_id": clip_file},
            {"type": "image", "file_id": picture, "animate": {"clip_id": animation}},
            {"type": "statement", "lines": ["Hi"], "backdrop": {"clip_id": backdrop_shot}},
            {"type": "statement", "lines": ["Hi"], "backdrop": {"file_id": backdrop_picture}},
            {"type": "figure", "block_spec": {"type": "image", "image_file_id": figure_picture}},
        ],
    }
    keys = set(asset_refs(spec))
    assert {f"take:{take}", f"take:{insert_take}", f"music:{track}"} <= keys
    assert {f"clip:{c}" for c in (shot, insert_shot, wall_shot, backdrop_shot, animation)} <= keys
    files = (picture, insert_picture, backdrop_picture, overlay_picture, figure_picture, logo, sound, clip_file)
    assert {f"file:{f}" for f in files} <= keys
    # A brand mark of the template is the template's, never a medium a purge deletes.
    assert all(":" in key and key.split(":", 1)[0] in ("take", "clip", "music", "file") for key in keys)


def test_a_shot_names_what_it_is_made_from():
    start, frame, recording, shot, video_file, reference, ingredient = _ids(7)
    assert clips.source_refs({"reference_file_id": start}) == [f"file:{start}"]
    cut = {"reference_video": {"kind": "recording", "id": recording, "file_id": _ids(1)[0], "frame_file_id": frame}}
    assert clips.source_refs(cut) == sorted([f"take:{recording}", f"file:{frame}"])
    assert clips.source_refs({"reference_video": {"kind": "shot", "id": shot}}) == [f"clip:{shot}"]
    assert clips.source_refs({"reference_video": {"kind": "file", "id": video_file}}) == [f"file:{video_file}"]
    made = {
        clips.REFERENCE_IMAGES: [reference, "not-a-uuid"],
        clips.INGREDIENTS: [{"file_id": ingredient, "role": "logo"}, "nonsense"],
    }
    assert clips.source_refs(made) == sorted([f"file:{reference}", f"file:{ingredient}"])
    assert clips.source_refs({"reference_video": {"kind": "elsewhere", "id": shot}}) == []
    assert clips.source_refs(None) == [] and clips.source_refs({}) == []


class _Connection:
    """A sync connection over the renders and the shots a video's index reads (``retention.media_index``)."""

    def __init__(self, *, drawn: list[list[str]], shots: dict[str, tuple[list[str] | None, dict]]):
        self.drawn, self.shots = drawn, shots
        self.asked: list[str] = []

    def execute(self, statement):
        table = statement.get_final_froms()[0].name
        self.asked.append(table)
        if table == "video_renders":
            return SimpleNamespace(all=lambda: [(refs,) for refs in self.drawn])
        wanted = {str(value) for param in statement.compile().params.values() if isinstance(param, list) for value in param}
        rows = [
            (refs, None if refs is not None else params)
            for shot_id, (refs, params) in self.shots.items()
            if shot_id in wanted
        ]
        return SimpleNamespace(all=lambda: rows)


def test_a_videos_index_follows_its_shots_and_what_its_ready_renders_drew():
    shot, earlier_shot, recording, picture, drawn_take, video = _ids(6)
    spec = {"title": "Film", "scenes": [{"type": "clip", "clip_id": shot}]}
    connection = _Connection(
        drawn=[[f"take:{drawn_take}"], []],
        shots={
            # Cut from a frame of an earlier shot, and from a recording.
            shot: ([f"clip:{earlier_shot}", f"take:{recording}"], {}),
            # Written before its sources were stored: read from its params.
            earlier_shot: (None, {"reference_file_id": picture}),
        },
    )
    keys = retention.media_index(connection, spec, uuid.UUID(video))
    assert keys == sorted(
        [f"clip:{shot}", f"clip:{earlier_shot}", f"take:{recording}", f"file:{picture}", f"take:{drawn_take}"]
    )
    # The renders once, then a shot's sources a level at a time.
    assert connection.asked == ["video_renders", "video_clips", "video_clips"]
    # A video being created has no render to read.
    fresh = _Connection(drawn=[], shots={})
    retention.media_index(fresh, spec, None)
    assert fresh.asked == ["video_clips"]


def test_a_loop_of_shots_ends():
    first, second = _ids(2)
    connection = _Connection(drawn=[], shots={first: ([f"clip:{second}"], {}), second: ([f"clip:{first}"], {})})
    keys = retention.media_index(connection, {"scenes": [{"type": "clip", "clip_id": first}]}, None)
    assert keys == sorted([f"clip:{first}", f"clip:{second}"]) and len(connection.asked) == 2


class _FileDb:
    """The videos' index and the file rows a deletion reads; the deletion it writes."""

    def __init__(self, needed: list[str], names: dict[str, str]):
        self.needed, self.names = needed, names
        self.deleted: list[str] = []

    async def execute(self, statement):
        if statement.is_dml:
            self.deleted = [
                str(value)
                for param in statement.compile().params.values()
                if isinstance(param, list)
                for value in param
            ]
            return None
        table = statement.get_final_froms()[0].name
        if table == "video_projects":
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [[f"file:{f}" for f in self.needed]]))
        wanted = {str(value) for param in statement.compile().params.values() if isinstance(param, list) for value in param}
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [name for fid, name in self.names.items() if fid in wanted])
        )


async def test_a_deletion_keeps_the_files_a_video_needs():
    """A shot's poster another video shows as a picture stays when the shot goes."""
    poster, video_file = _ids(2)
    db = _FileDb(needed=[poster], names={poster: "store/poster.jpg", video_file: "store/shot.mp4"})
    purged = await assets.forget_files(db, [poster, video_file])
    assert purged == ["store/shot.mp4"] and db.deleted == [video_file]
    # Every one needed: nothing is deleted.
    db = _FileDb(needed=[poster], names={poster: "store/poster.jpg"})
    assert await assets.forget_files(db, [poster]) == [] and db.deleted == []
