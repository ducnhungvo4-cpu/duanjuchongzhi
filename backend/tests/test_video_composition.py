from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import main


@pytest.fixture
def composition_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "data"
    monkeypatch.setattr(main, "DATA_DIR", data)
    monkeypatch.setattr(main, "DB_PATH", data / "test.sqlite3")
    monkeypatch.setattr(main, "PROJECTS_DIR", data / "projects")
    monkeypatch.setattr(main, "prepare_agent", lambda _key: {"mode": "mock", "model_name": "test", "prompt": "", "prompt_file": ""})
    main.init_db()
    with TestClient(main.app) as client:
        yield client, tmp_path


def _make_video(path: Path, *, size: str, color: str, audio: bool) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed")
    args = [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=25:d=2"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2", "-shortest"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac"]
    args.append(str(path))
    subprocess.run(args, check=True, capture_output=True)


def _project_and_episode(client: TestClient, data_dir: Path, title: str):
    project = client.post("/api/projects", json={"title": title}).json()["project"]
    episode_id = f"episode_{title}"
    source = data_dir / "projects" / project["id"] / "source" / "reference.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    _make_video(source, size="160x90", color="green", audio=True)
    with main.connect() as conn:
        conn.execute("INSERT INTO episodes (id,project_id,episode_index,filename,file_path,status,created_at) VALUES (?,?,?,?,?,?,?)", (episode_id, project["id"], 1, source.name, str(source), "已上传", "2026-01-01T00:00:00Z"))
    return project, episode_id


def test_upload_draft_cas_mock_filter_and_real_export(composition_client):
    client, tmp_path = composition_client
    project, episode_id = _project_and_episode(client, tmp_path / "data", "one")
    uploaded_path = tmp_path / "uploaded.mp4"
    _make_video(uploaded_path, size="90x160", color="blue", audio=False)
    with uploaded_path.open("rb") as handle:
        upload = client.post(f"/api/projects/{project['id']}/episodes/{episode_id}/video-assets", files={"file": ("vertical.mp4", handle, "video/mp4")})
    assert upload.status_code == 200, upload.text
    media = upload.json()["asset"]
    assert media["width"] == 90 and media["height"] == 160
    assert media["hasAudio"] is False and media["durationMs"] >= 1900
    assets = client.get(f"/api/projects/{project['id']}/episodes/{episode_id}/video-assets").json()["assets"]
    assert {item["kind"] for item in assets} == {"episode_video", "uploaded_video"}

    with main.connect() as conn:
        conn.execute("INSERT INTO video_generations VALUES (?,?,?,?,?,?,?,?,?,?)", ("mock-gen", project["id"], episode_id, "sb", "成功", "p", str(tmp_path / "fake.txt"), 0, json.dumps({"mock": True}), "2026-01-01T00:00:00Z"))
    assert all(item["id"] != "mock-gen" for item in client.get(f"/api/projects/{project['id']}/episodes/{episode_id}/video-assets").json()["assets"])

    url = f"/api/projects/{project['id']}/video-edits/{episode_id}"
    edit = {"revision": 0, "aspectRatio": "9:16", "resolution": "720p", "fps": 25, "clips": [
        {"id": "clip_a", "sourceId": f"episode:{episode_id}", "inMs": 200, "outMs": 900, "order": 0},
        {"id": "clip_b", "sourceId": media["id"], "inMs": 400, "outMs": 1500, "order": 1},
    ]}
    saved = client.put(url, json=edit)
    assert saved.status_code == 200, saved.text
    assert saved.json()["draft"]["revision"] == 1
    assert client.get(url).json()["draft"]["edit"]["clips"] == edit["clips"]
    conflict = client.put(url, json=edit)
    assert conflict.status_code == 409
    duplicate_ids = {**edit, "revision": 1, "clips": [edit["clips"][0], {**edit["clips"][1], "id": "clip_a"}]}
    assert client.put(url, json=duplicate_ids).status_code == 400
    negative_in = {**edit, "revision": 1, "clips": [{**edit["clips"][0], "inMs": -1}]}
    assert client.put(url, json=negative_in).status_code == 422
    out_of_bounds = {**edit, "revision": 1, "clips": [{**edit["clips"][0], "outMs": 999999}]}
    assert client.put(url, json=out_of_bounds).status_code == 400
    assert client.put(url, json={**edit, "revision": 1, "clips": [{**edit["clips"][1], "sourceId": "media_from_other_project"}]}).status_code == 400

    create_export = client.post(f"/api/projects/{project['id']}/video-edits/{episode_id}/exports")
    assert create_export.status_code == 200, create_export.text
    job = create_export.json()["job"]
    detail = client.get(f"/api/projects/{project['id']}/video-export-jobs/{job['id']}").json()["job"]
    assert detail["status"] == "succeeded", detail
    output = Path(detail["output_path"])
    probe = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)], capture_output=True, text=True, check=True)
    media_info = json.loads(probe.stdout)
    video = next(stream for stream in media_info["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in media_info["streams"] if stream["codec_type"] == "audio")
    assert (video["width"], video["height"]) == (720, 1280)
    assert float(media_info["format"]["duration"]) == pytest.approx(1.8, abs=0.08)
    assert audio["codec_type"] == "audio"
    assert client.get(detail["downloadUrl"]).status_code == 200
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    first_frame_path = tmp_path / "first-frame.png"
    second_frame_path = tmp_path / "second-frame.png"
    subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", "0.2", "-i", str(output), "-frames:v", "1", str(first_frame_path)], capture_output=True, check=True)
    subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", "1.1", "-i", str(output), "-frames:v", "1", str(second_frame_path)], capture_output=True, check=True)
    first_rgb = Image.open(first_frame_path).convert("RGB").getpixel((360, 640))
    second_rgb = Image.open(second_frame_path).convert("RGB").getpixel((360, 640))
    assert first_rgb[1] > first_rgb[2] * 1.4  # first clip is green
    assert second_rgb[2] > second_rgb[1] * 1.4  # second clip is blue


def test_upload_rejects_invalid_video_and_other_project_media(composition_client, tmp_path: Path):
    client, root = composition_client
    project_a, ep_a = _project_and_episode(client, root / "data", "alpha")
    project_b, ep_b = _project_and_episode(client, root / "data", "beta")
    bad = client.post(f"/api/projects/{project_a['id']}/episodes/{ep_a}/video-assets", files={"file": ("bad.mp4", b"not a movie", "video/mp4")})
    assert bad.status_code == 400
    source = root / "small.mp4"
    _make_video(source, size="64x64", color="yellow", audio=False)
    with source.open("rb") as handle:
        uploaded = client.post(f"/api/projects/{project_a['id']}/episodes/{ep_a}/video-assets", files={"file": ("source.mp4", handle, "video/mp4")})
    media_id = uploaded.json()["asset"]["id"]
    response = client.put(f"/api/projects/{project_b['id']}/video-edits/{ep_b}", json={"revision": 0, "clips": [{"id": "x", "sourceId": media_id, "inMs": 0, "outMs": 500, "order": 0}]})
    assert response.status_code == 400


def test_failed_export_can_retry_and_restart_marks_interrupted_job_failed(composition_client, monkeypatch: pytest.MonkeyPatch):
    client, root = composition_client
    project, episode_id = _project_and_episode(client, root / "data", "retry")
    edit_url = f"/api/projects/{project['id']}/video-edits/{episode_id}"
    saved = client.put(edit_url, json={"revision": 0, "clips": [{"id": "clip_retry", "sourceId": f"episode:{episode_id}", "inMs": 0, "outMs": 500, "order": 0}]})
    assert saved.status_code == 200
    import backend.app.composition as composition

    original_which = shutil.which
    with monkeypatch.context() as patch:
        patch.setattr(composition.shutil, "which", lambda command: None if command == "ffmpeg" else original_which(command))
        failed = client.post(f"/api/projects/{project['id']}/video-edits/{episode_id}/exports")
    assert failed.status_code == 200
    failed_job = client.get(f"/api/projects/{project['id']}/video-export-jobs/{failed.json()['job']['id']}").json()["job"]
    assert failed_job["status"] == "failed" and "FFmpeg" in failed_job["error"]
    retried = client.post(f"/api/projects/{project['id']}/video-export-jobs/{failed_job['id']}/retry")
    assert retried.status_code == 200, retried.text
    retry_job = client.get(f"/api/projects/{project['id']}/video-export-jobs/{retried.json()['job']['id']}").json()["job"]
    assert retry_job["status"] == "succeeded"
    assert retry_job["revision"] == failed_job["revision"]

    interrupted_id = "export_interrupted_test"
    with main.connect() as conn:
        conn.execute("INSERT INTO composition_exports (id,project_id,episode_id,revision,status,stage,snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?)", (interrupted_id, project["id"], episode_id, 1, "running", "编码中", json.dumps({"projectId": project["id"], "clips": []}), "2026-01-01T00:00:00Z"))
    with TestClient(main.app) as restarted_client:
        interrupted = restarted_client.get(f"/api/projects/{project['id']}/video-export-jobs/{interrupted_id}").json()["job"]
    assert interrupted["status"] == "failed"
    assert "重启中断" in interrupted["stage"]
