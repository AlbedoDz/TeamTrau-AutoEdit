"""Unit tests for FreeCut Project Writer (Spike S0.2).

Validates:
- Frame-integer timeline math
- FreeCut ID formatting conventions
- LINK mode media metadata & zero-copy filesystem layout
- TeamTrau AI analysis sidecars (schemaVersion 1, kind: teamtrau)
- JSON validity and schema round trip
"""

import json
from pathlib import Path

from src.core.types import (
    EditClip,
    EditTimeline,
    MusicalBarGrid,
    SongSection,
    SongSectionType,
)
from src.integrations.freecut_writer import (
    sanitize_workspace_filename,
    write_freecut_project,
)
from src.modules.proxy_generator import compute_proxy_key


def test_sanitize_workspace_filename():
    assert sanitize_workspace_filename("sample:name*.mp4") == "sample_name_.mp4"
    assert sanitize_workspace_filename("CON.mp4") == "CON_.mp4"
    assert sanitize_workspace_filename("  clip.mov  ") == "clip.mov"
    assert sanitize_workspace_filename("") == "source.bin"


def test_freecut_writer_frame_math_and_ids(tmp_path: Path):
    workspace = tmp_path / "studio_ws"
    media_file_1 = tmp_path / "shot_01.mp4"
    media_file_1.write_bytes(b"dummy video data 1")
    media_file_2 = tmp_path / "shot_02.mp4"
    media_file_2.write_bytes(b"dummy video data 2")

    clip1 = EditClip(
        source_path=media_file_1,
        source_in=1.5,
        frames=60,
        available=10.0,
        label="Shot 1",
    )
    clip2 = EditClip(
        source_path=media_file_2,
        source_in=0.0,
        frames=90,
        available=10.0,
        label="Shot 2",
    )

    timeline = EditTimeline(
        name="Test Timeline",
        fps=30.0,
        canvas_width=1920,
        canvas_height=1080,
        clips=[clip1, clip2],
    )

    project_id = write_freecut_project(timeline, workspace, "Test Project")
    assert project_id.startswith("p-")

    project_file = workspace / "projects" / project_id / "project.json"
    assert project_file.exists()
    project = json.loads(project_file.read_text(encoding="utf-8"))

    assert project["schemaVersion"] == 15
    assert project["duration"] == (60 + 90) / 30.0
    assert project["metadata"]["width"] == 1920
    assert project["metadata"]["height"] == 1080
    assert project["metadata"]["fps"] == 30

    items = project["timeline"]["items"]
    # 2 video items + 2 companion audio items = 4 items
    assert len(items) == 4

    video_items = [i for i in items if i["type"] == "video"]
    audio_items = [i for i in items if i["type"] == "audio"]
    assert len(video_items) == 2
    assert len(audio_items) == 2

    # Frame math: sequential placement
    assert video_items[0]["from"] == 0
    assert video_items[0]["durationInFrames"] == 60
    assert video_items[0]["sourceStart"] == int(round(1.5 * 30.0))
    assert video_items[0]["sourceEnd"] == int(round(1.5 * 30.0)) + 60

    assert video_items[1]["from"] == 60
    assert video_items[1]["durationInFrames"] == 90
    assert video_items[1]["sourceStart"] == 0
    assert video_items[1]["sourceEnd"] == 90

    # Companion audio items share exact 'from', 'durationInFrames', and linkedGroupId
    for v_item, a_item in zip(video_items, audio_items):
        assert a_item["from"] == v_item["from"]
        assert a_item["durationInFrames"] == v_item["durationInFrames"]
        assert a_item["mediaId"] == v_item["mediaId"]
        assert a_item["trackId"] == "track-a1"
        assert v_item["trackId"] == "track-v1"
        assert a_item["linkedGroupId"] == v_item["linkedGroupId"]
        assert a_item["linkedGroupId"].startswith("link-")


def test_freecut_writer_link_mode_and_sidecars(tmp_path: Path):
    workspace = tmp_path / "studio_ws"
    media_file = tmp_path / "nature.mp4"
    media_file.write_bytes(b"dummy video content")

    soundtrack = tmp_path / "beat.mp3"
    soundtrack.write_bytes(b"dummy audio content")

    clip = EditClip(
        source_path=media_file,
        source_in=2.0,
        frames=120,
        available=15.0,
        is_teaser=True,
    )

    section1 = SongSection(
        section_type=SongSectionType.INTRO,
        start_time=0.0,
        end_time=2.0,
        duration=2.0,
        avg_energy=0.3,
    )
    section2 = SongSection(
        section_type=SongSectionType.DROP,
        start_time=2.0,
        end_time=4.0,
        duration=2.0,
        avg_energy=0.9,
        is_climax=True,
    )
    grid = MusicalBarGrid(
        tempo_bpm=120.0,
        bar_times=[0.0, 1.0, 2.0, 3.0],
    )

    timeline = EditTimeline(
        name="Music Cut",
        fps=60.0,
        canvas_width=3840,
        canvas_height=2160,
        clips=[clip],
        soundtrack_path=soundtrack,
        soundtrack_in=0.5,
        sections=[section1, section2],
        bar_grid=grid,
    )

    project_id = write_freecut_project(timeline, workspace, "Music Cut Project")

    # Workspace marker
    marker_file = workspace / ".freecut-workspace.json"
    assert marker_file.exists()
    marker = json.loads(marker_file.read_text(encoding="utf-8"))
    assert marker["schemaVersion"] == "2.0"

    # Index
    index_file = workspace / "index.json"
    assert index_file.exists()
    index = json.loads(index_file.read_text(encoding="utf-8"))
    assert index["version"] == "1.0"
    assert any(p["id"] == project_id for p in index["projects"])

    # Media links
    links_file = workspace / "projects" / project_id / "media-links.json"
    assert links_file.exists()
    links = json.loads(links_file.read_text(encoding="utf-8"))
    assert len(links["mediaIds"]) == 2  # video clip + soundtrack

    # Media metadata in LINK mode (storageType == "handle")
    for link_entry in links["mediaIds"]:
        mid = link_entry["id"]
        meta_file = workspace / "media" / mid / "metadata.json"
        assert meta_file.exists()
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        assert meta["storageType"] == "handle"
        assert meta["id"] == mid

        # Check that the media file was zero-copy linked
        target_file = workspace / "media" / mid / sanitize_workspace_filename(meta["fileName"])
        assert target_file.exists()

        # Check TeamTrau AI sidecar
        ai_sidecar = workspace / "media" / mid / "cache" / "ai" / "teamtrau.json"
        assert ai_sidecar.exists()
        sidecar = json.loads(ai_sidecar.read_text(encoding="utf-8"))
        assert sidecar["schemaVersion"] == 1
        assert sidecar["kind"] == "teamtrau"
        assert sidecar["mediaId"] == mid
        assert sidecar["service"] == "teamtrau-curate"
        assert sidecar["model"] == "teamtrau-v3"
        assert "role" in sidecar["data"]

    # Verify project markers (sections & bars)
    project_file = workspace / "projects" / project_id / "project.json"
    project = json.loads(project_file.read_text(encoding="utf-8"))
    markers = project["timeline"]["markers"]
    assert len(markers) >= 2
    assert any(m["label"] == "INTRO" and m["color"] == "#3b82f6" for m in markers)
    assert any(m["label"] == "DROP" and m["color"] == "#f59e0b" for m in markers)

    # Verify 3 tracks (V1, A1, A2)
    tracks = project["timeline"]["tracks"]
    assert len(tracks) == 3
    track_names = [t["name"] for t in tracks]
    assert track_names == ["V1", "A1", "A2"]


def test_freecut_writer_attaches_proxies(tmp_path: Path):
    workspace = tmp_path / "studio_ws_proxies"
    media_file = tmp_path / "clip.mp4"
    media_file.write_bytes(b"dummy clip data")
    proxy_file = tmp_path / "proxy_clip_abc123.mp4"
    proxy_file.write_bytes(b"dummy proxy data")

    clip = EditClip(
        source_path=media_file,
        source_in=0.0,
        frames=30,
        available=5.0,
        label="Shot 1",
    )
    timeline = EditTimeline(
        name="Proxy Timeline",
        fps=30.0,
        canvas_width=1920,
        canvas_height=1080,
        clips=[clip],
    )

    project_id = write_freecut_project(
        timeline=timeline,
        workspace=workspace,
        project_name="Proxy Test",
        proxies={media_file: proxy_file},
    )

    # Verify content/proxies/{key}/proxy.mp4 and meta.json
    proxy_key = compute_proxy_key(media_file)
    proxy_dir = workspace / "content" / "proxies" / proxy_key
    assert proxy_dir.exists()
    assert (proxy_dir / "proxy.mp4").exists()
    assert (proxy_dir / "meta.json").exists()

    meta = json.loads((proxy_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["version"] == 4
    assert meta["status"] == "ready"
    assert meta["codec"] == "avc"

    # Verify media metadata contains contentHash
    links_file = workspace / "projects" / project_id / "media-links.json"
    links = json.loads(links_file.read_text(encoding="utf-8"))
    media_id = links["mediaIds"][0]["id"]
    media_meta = json.loads((workspace / "media" / media_id / "metadata.json").read_text(encoding="utf-8"))
    assert media_meta.get("contentHash") == proxy_key


def test_freecut_writer_reuses_existing_workspace_media(tmp_path: Path):
    workspace = tmp_path / "studio_ws"
    media_file_1 = tmp_path / "shot_01.mp4"
    media_file_1.write_bytes(b"dummy video data 1")

    clip1 = EditClip(
        source_path=media_file_1,
        source_in=0.0,
        frames=60,
        available=10.0,
        label="Shot 1",
    )
    timeline1 = EditTimeline(
        name="Timeline 1",
        fps=30.0,
        canvas_width=1920,
        canvas_height=1080,
        clips=[clip1],
    )

    # First write creates media folder
    p1 = write_freecut_project(timeline1, workspace, "Project 1")
    media_folders_p1 = list((workspace / "media").glob("m-*"))
    assert len(media_folders_p1) == 1
    media_id_1 = media_folders_p1[0].name

    p1_links = json.loads((workspace / "projects" / p1 / "media-links.json").read_text(encoding="utf-8"))
    assert p1_links["mediaIds"][0]["id"] == media_id_1

    # Second write for another project in same workspace referencing the same media
    timeline2 = EditTimeline(
        name="Timeline 2",
        fps=30.0,
        canvas_width=1920,
        canvas_height=1080,
        clips=[clip1],
    )
    p2 = write_freecut_project(timeline2, workspace, "Project 2")

    # MUST reuse existing media ID, NOT create a second m-* folder
    media_folders_p2 = list((workspace / "media").glob("m-*"))
    assert len(media_folders_p2) == 1

    p2_links = json.loads((workspace / "projects" / p2 / "media-links.json").read_text(encoding="utf-8"))
    assert len(p2_links["mediaIds"]) == 1
    assert p2_links["mediaIds"][0]["id"] == media_id_1

    p2_project = json.loads((workspace / "projects" / p2 / "project.json").read_text(encoding="utf-8"))
    video_item = [i for i in p2_project["timeline"]["items"] if i["type"] == "video"][0]
    assert video_item["mediaId"] == media_id_1

