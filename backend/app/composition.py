from __future__ import annotations

import json
import hashlib
import mimetypes
import shutil
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
EXPORT_WIDTH = {"9:16": 720, "16:9": 1280, "1:1": 720}
EXPORT_HEIGHT = {"9:16": 1280, "16:9": 720, "1:1": 720}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def media_probe(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise HTTPException(status_code=503, detail="本机未安装 ffprobe，无法检测视频文件")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if result.returncode:
        raise HTTPException(status_code=400, detail="视频文件无法解析：" + result.stderr[-800:])
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="ffprobe 未返回有效媒体信息") from exc
    streams = raw.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video:
        raise HTTPException(status_code=400, detail="文件中没有可用的视频流")
    duration = float(video.get("duration") or raw.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise HTTPException(status_code=400, detail="无法读取视频时长")
    return {
        "durationMs": round(duration * 1000), "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0), "hasAudio": any(s.get("codec_type") == "audio" for s in streams),
        "videoCodec": str(video.get("codec_name") or ""),
        "audioCodec": next((str(s.get("codec_name") or "") for s in streams if s.get("codec_type") == "audio"), ""),
        "mimeType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }


class ClipInput(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    sourceId: str = Field(min_length=1, max_length=100)
    inMs: int = Field(ge=0)
    outMs: int = Field(gt=0)
    order: int = Field(ge=0)


class EditInput(BaseModel):
    revision: int = Field(ge=0)
    aspectRatio: str = "9:16"
    resolution: str = "720p"
    fps: int = Field(default=30, ge=12, le=60)
    clips: list[ClipInput] = Field(default_factory=list, max_length=100)


def make_router(
    connect: Callable[[], sqlite3.Connection], data_dir: Callable[[], Path], projects_dir: Callable[[], Path],
    get_project_or_404: Callable[[sqlite3.Connection, str], dict[str, Any]],
    get_episode_or_first: Callable[[sqlite3.Connection, str, str | None], dict[str, Any] | None],
    new_id: Callable[[str], str], data_file_url: Callable[[str], str],
) -> APIRouter:
    router = APIRouter()
    active_jobs: set[str] = set()
    lock = threading.Lock()

    def job_update(job_id: str, **fields: Any) -> None:
        with connect() as conn:
            assignments = ",".join(f"{key}=?" for key in fields)
            conn.execute(f"UPDATE composition_exports SET {assignments} WHERE id=?", (*fields.values(), job_id))

    def make_thumbnail(project_id: str, episode_id: str, asset_id: str, source_path: Path) -> str:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return ""
        project_root = (projects_dir() / project_id).resolve()
        target = (project_root / "video_media" / episode_id / "thumbnails" / f"{hashlib.sha256(asset_id.encode('utf-8')).hexdigest()[:24]}.jpg").resolve()
        try:
            target.relative_to(project_root)
            source_path.resolve().relative_to(data_dir().resolve())
        except ValueError:
            try:
                source_path.resolve().relative_to(project_root)
            except ValueError:
                return ""
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "0.1", "-i", str(source_path), "-frames:v", "1", "-vf", "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "4", "-y", str(target)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            )
            if result.returncode or not target.is_file():
                target.unlink(missing_ok=True)
                return ""
        return data_file_url(str(target))

    def build_assets(project_id: str, episode_id: str) -> list[dict[str, Any]]:
        with connect() as conn:
            get_project_or_404(conn, project_id)
            episode = get_episode_or_first(conn, project_id, episode_id)
            if not episode or episode["id"] != episode_id:
                raise HTTPException(status_code=404, detail="剧集不存在")
            items: list[dict[str, Any]] = []
            source_path = Path(episode["file_path"]).resolve()
            try:
                source_path.relative_to(data_dir().resolve())
                source_is_safe = True
            except ValueError:
                source_is_safe = False
            if source_is_safe and source_path.is_file():
                meta = media_probe(source_path)
                source_id = f"episode:{episode_id}"
                items.append({"id": source_id, "episodeId": episode_id, "name": episode["filename"], "kind": "episode_video", "source": "本集原片（仅供参考）", "fileUrl": data_file_url(str(source_path)), "thumbnailUrl": make_thumbnail(project_id, episode_id, source_id, source_path), **meta})
            rows = conn.execute("SELECT * FROM video_media WHERE project_id=? AND episode_id=? ORDER BY created_at DESC", (project_id, episode_id)).fetchall()
            for row in rows:
                item = dict(row)
                path = Path(item["file_path"]).resolve()
                try:
                    path.relative_to((projects_dir() / project_id).resolve())
                except ValueError:
                    continue
                if not path.is_file():
                    continue
                meta = json.loads(item["media_json"])
                items.append({"id": item["id"], "episodeId": episode_id, "name": item["filename"], "kind": "uploaded_video", "source": "本地导入", "fileUrl": data_file_url(str(path)), "thumbnailUrl": make_thumbnail(project_id, episode_id, item["id"], path), **meta})
            gens = conn.execute("SELECT * FROM video_generations WHERE project_id=? AND episode_id=? ORDER BY created_at DESC", (project_id, episode_id)).fetchall()
            for row in gens:
                generation = dict(row)
                payload = json.loads(generation.get("payload_json") or "{}")
                if payload.get("mock") or not generation.get("output_path"):
                    continue
                path = Path(generation["output_path"]).resolve()
                try:
                    path.relative_to((projects_dir() / project_id).resolve())
                except ValueError:
                    continue
                if path.is_file():
                    try:
                        meta = media_probe(path)
                    except HTTPException:
                        continue
                    items.append({"id": generation["id"], "episodeId": episode_id, "name": path.name, "kind": "generated_video", "source": "真实生成文件", "fileUrl": data_file_url(str(path)), "thumbnailUrl": make_thumbnail(project_id, episode_id, generation["id"], path), **meta})
            return items

    def resolve_clips(conn: sqlite3.Connection, project_id: str, episode_id: str, clips: list[ClipInput]) -> list[dict[str, Any]]:
        available = {item["id"]: item for item in build_assets(project_id, episode_id)}
        result: list[dict[str, Any]] = []
        for clip in clips:
            media = available.get(clip.sourceId)
            if not media:
                raise HTTPException(status_code=400, detail=f"素材不属于当前项目或当前集，或文件不可用：{clip.sourceId}")
            if media["kind"] == "episode_video" and clip.sourceId != f"episode:{episode_id}":
                raise HTTPException(status_code=400, detail="原片素材ID无效")
            duration = media["durationMs"]
            if clip.outMs <= clip.inMs or clip.outMs > duration:
                raise HTTPException(status_code=400, detail=f"片段裁切范围超出素材时长：{media['name']}")
            if clip.order >= len(clips):
                raise HTTPException(status_code=400, detail="片段顺序必须从0连续编号")
            result.append({"id": clip.id, "sourceId": clip.sourceId, "name": media["name"], "source": media["source"], "kind": media["kind"], "fileUrl": media["fileUrl"], "sourceDurationMs": duration, "inMs": clip.inMs, "outMs": clip.outMs, "order": clip.order})
        if sorted(item["order"] for item in result) != list(range(len(result))):
            raise HTTPException(status_code=400, detail="片段顺序必须唯一且连续")
        return sorted(result, key=lambda item: item["order"])

    def run_export(job_id: str, snapshot: dict[str, Any], output_path: Path, expected_seconds: float) -> None:
        with lock:
            active_jobs.add(job_id)
        try:
            job_update(job_id, status="running", progress=0, stage="正在检查输入素材", started_at=utc_now(), error="")
            ffmpeg = shutil.which("ffmpeg")
            ffprobe = shutil.which("ffprobe")
            if not ffmpeg or not ffprobe:
                raise RuntimeError("本机未安装 FFmpeg/ffprobe")
            width = EXPORT_WIDTH[snapshot["aspectRatio"]]
            height = EXPORT_HEIGHT[snapshot["aspectRatio"]]
            if snapshot["aspectRatio"] in {"9:16", "16:9"}:
                width, height = (720, 1280) if snapshot["aspectRatio"] == "9:16" else (1280, 720)
            if snapshot["resolution"] == "1080p":
                scale = 1.5
                width, height = int(width * scale), int(height * scale)
            args = [ffmpeg, "-y", "-hide_banner", "-nostats", "-loglevel", "warning", "-nostdin"]
            filters: list[str] = []
            concat_inputs: list[str] = []
            for clip in snapshot["clips"]:
                path = Path(clip["path"])
                if not path.is_file():
                    raise RuntimeError("输入视频文件已不存在，请重新导入素材")
                args += ["-i", str(path)]
                args += ["-f", "lavfi", "-t", f"{clip['durationMs'] / 1000:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            # Each source is paired with finite silence; clips without audio select that paired input.
            for index, clip in enumerate(snapshot["clips"]):
                path = Path(clip["path"])
                streams = media_probe(path)
                v = f"v{index}"; a = f"a{index}"
                start = clip["inMs"] / 1000; end = clip["outMs"] / 1000
                filters.append(f"[{index * 2}:v:0]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps={snapshot['fps']},setsar=1,format=yuv420p[{v}]")
                audio_input = f"{index * 2}:a:0" if streams["hasAudio"] else f"{index * 2 + 1}:a:0"
                if streams["hasAudio"]:
                    filters.append(f"[{audio_input}]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[{a}]")
                else:
                    filters.append(f"[{audio_input}]atrim=duration={clip['durationMs'] / 1000:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[{a}]")
                concat_inputs.append(f"[{v}][{a}]")
            filters.append("".join(concat_inputs) + f"concat=n={len(snapshot['clips'])}:v=1:a=1[outv][outa]")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.touch()
            args += ["-filter_complex", ";".join(filters), "-map", "[outv]", "-map", "[outa]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-progress", "pipe:1", "-f", "mp4", str(output_path)]
            log_path = output_path.with_suffix(".ffmpeg.log")
            with log_path.open("wb") as log_stream:
                proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=log_stream, text=True, encoding="utf-8", errors="replace")
                assert proc.stdout is not None
                for line in proc.stdout:
                    key, _, value = line.strip().partition("=")
                    if key == "out_time_us":
                        duration_ms = int(value or 0) / 1000
                        progress = min(99, max(1, round(duration_ms / max(expected_seconds * 1000, 100) * 100)))
                        job_update(job_id, progress=progress, stage="正在编码并拼接所选片段")
                code = proc.wait()
                log_stream.flush()
                if code:
                    log_stream.seek(max(0, log_stream.tell() - 2400))
                    stderr = log_stream.read()[-1200:].decode("utf-8", errors="replace")
                    raise RuntimeError("FFmpeg 导出失败：" + stderr)
            probe = media_probe(output_path)
            if abs(probe["durationMs"] - round(expected_seconds * 1000)) > max(30, round(2000 / snapshot["fps"])):
                raise RuntimeError("导出文件时长与所选片段不一致")
            log_path.unlink(missing_ok=True)
            job_update(job_id, status="succeeded", progress=100, stage="导出完成", output_path=str(output_path), output_url=data_file_url(str(output_path)), output_duration_ms=probe["durationMs"], finished_at=utc_now(), error="")
        except Exception as exc:
            job_update(job_id, status="failed", stage="导出失败，可修改草稿后重试", error=str(exc), finished_at=utc_now())
        finally:
            with lock:
                active_jobs.discard(job_id)

    @router.on_event("startup")
    def initialize_composition_tables() -> None:
        with connect() as conn:
            conn.executescript("""
              CREATE TABLE IF NOT EXISTS video_media (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, episode_id TEXT NOT NULL, filename TEXT NOT NULL, file_path TEXT NOT NULL, media_json TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS idx_video_media_episode ON video_media(project_id, episode_id);
              CREATE TABLE IF NOT EXISTS video_edit_drafts (project_id TEXT NOT NULL, episode_id TEXT NOT NULL, title TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, edit_json TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(project_id, episode_id));
              CREATE TABLE IF NOT EXISTS composition_exports (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, episode_id TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL, snapshot_json TEXT NOT NULL, output_path TEXT NOT NULL DEFAULT '', output_url TEXT NOT NULL DEFAULT '', output_duration_ms INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT '', finished_at TEXT NOT NULL DEFAULT '');
            """)
            # A process restart cannot resume an FFmpeg child safely. Mark interrupted jobs failed so users can retry.
            conn.execute("UPDATE composition_exports SET status='failed',stage='服务重启中断，可重新导出',error='服务在导出期间重启，请重试',finished_at=? WHERE status IN ('queued','running')", (utc_now(),))

    @router.get("/api/projects/{project_id}/episodes/{episode_id}/video-assets")
    def list_video_assets(project_id: str, episode_id: str) -> dict[str, Any]:
        return {"assets": build_assets(project_id, episode_id)}

    @router.post("/api/projects/{project_id}/episodes/{episode_id}/video-assets")
    async def upload_video_asset(project_id: str, episode_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in VIDEO_EXTENSIONS:
            raise HTTPException(status_code=415, detail="仅支持 MP4、MOV、M4V、MKV、WebM 视频")
        with connect() as conn:
            get_project_or_404(conn, project_id)
            episode = get_episode_or_first(conn, project_id, episode_id)
            if not episode or episode["id"] != episode_id:
                raise HTTPException(status_code=404, detail="剧集不存在")
        target_dir = (projects_dir() / project_id / "video_media" / episode_id).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        file_id = new_id("media")
        target = target_dir / f"{file_id}{suffix}"
        size = 0
        try:
            with target.open("wb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="单个视频最大支持 1 GiB")
                    stream.write(chunk)
            meta = media_probe(target)
            with connect() as conn:
                conn.execute("INSERT INTO video_media VALUES (?,?,?,?,?,?,?)", (file_id, project_id, episode_id, Path(file.filename or target.name).name, str(target), json.dumps({**meta, "sizeBytes": size}, ensure_ascii=False), utc_now()))
            return {"asset": {"id": file_id, "episodeId": episode_id, "name": Path(file.filename or target.name).name, "kind": "uploaded_video", "source": "本地导入", "fileUrl": data_file_url(str(target)), **meta, "sizeBytes": size}}
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @router.get("/api/projects/{project_id}/video-edits/{episode_id}")
    def get_video_edit(project_id: str, episode_id: str) -> dict[str, Any]:
        with connect() as conn:
            project = get_project_or_404(conn, project_id)
            episode = get_episode_or_first(conn, project_id, episode_id)
            if not episode or episode["id"] != episode_id:
                raise HTTPException(status_code=404, detail="剧集不存在")
            row = conn.execute("SELECT * FROM video_edit_drafts WHERE project_id=? AND episode_id=?", (project_id, episode_id)).fetchone()
            if row:
                draft = dict(row); draft["edit"] = json.loads(draft.pop("edit_json"))
            else:
                ratio = project.get("frame_ratio") or "9:16"
                draft = {"project_id": project_id, "episode_id": episode_id, "title": f"第{episode['episode_index']}集合成", "revision": 0, "updated_at": "", "edit": {"aspectRatio": ratio if ratio in EXPORT_WIDTH else "9:16", "resolution": "720p", "fps": 30, "clips": []}}
            return {"draft": draft}

    @router.put("/api/projects/{project_id}/video-edits/{episode_id}")
    def put_video_edit(project_id: str, episode_id: str, payload: EditInput) -> dict[str, Any]:
        if payload.aspectRatio not in EXPORT_WIDTH:
            raise HTTPException(status_code=400, detail="不支持的输出画幅")
        if payload.resolution not in {"720p", "1080p"}:
            raise HTTPException(status_code=400, detail="不支持的输出分辨率")
        if len({clip.id for clip in payload.clips}) != len(payload.clips):
            raise HTTPException(status_code=400, detail="同一草稿中的片段ID必须唯一")
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            get_project_or_404(conn, project_id)
            episode = get_episode_or_first(conn, project_id, episode_id)
            if not episode or episode["id"] != episode_id:
                raise HTTPException(status_code=404, detail="剧集不存在")
            clips = resolve_clips(conn, project_id, episode_id, payload.clips)
            edit = {"aspectRatio": payload.aspectRatio, "resolution": payload.resolution, "fps": payload.fps, "clips": [{key: clip[key] for key in ("id", "sourceId", "inMs", "outMs", "order")} for clip in clips]}
            row = conn.execute("SELECT revision,title FROM video_edit_drafts WHERE project_id=? AND episode_id=?", (project_id, episode_id)).fetchone()
            current = row["revision"] if row else 0
            if payload.revision != current:
                raise HTTPException(status_code=409, detail="草稿已被其他操作更新；本地编辑仍保留，请刷新或重新应用后保存")
            next_revision = current + 1
            title = row["title"] if row else f"第{episode['episode_index']}集合成"
            timestamp = utc_now()
            if row:
                cursor = conn.execute("UPDATE video_edit_drafts SET revision=?,edit_json=?,updated_at=? WHERE project_id=? AND episode_id=? AND revision=?", (next_revision, json.dumps(edit, ensure_ascii=False), timestamp, project_id, episode_id, current))
                if cursor.rowcount != 1:
                    raise HTTPException(status_code=409, detail="草稿 revision 冲突；本地编辑已保留")
            else:
                cursor = conn.execute("INSERT OR IGNORE INTO video_edit_drafts VALUES (?,?,?,?,?,?)", (project_id, episode_id, title, next_revision, json.dumps(edit, ensure_ascii=False), timestamp))
                if cursor.rowcount != 1:
                    raise HTTPException(status_code=409, detail="草稿已被其他操作创建；本地编辑仍保留")
            return {"draft": {"project_id": project_id, "episode_id": episode_id, "title": title, "revision": next_revision, "updated_at": timestamp, "edit": edit}}

    @router.post("/api/projects/{project_id}/video-edits/{episode_id}/exports")
    def create_export(project_id: str, episode_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            get_project_or_404(conn, project_id)
            active = conn.execute("SELECT id FROM composition_exports WHERE project_id=? AND episode_id=? AND status IN ('queued','running') LIMIT 1", (project_id, episode_id)).fetchone()
            if active:
                raise HTTPException(status_code=409, detail="本集已有导出任务正在运行，请等待完成后再导出")
            row = conn.execute("SELECT * FROM video_edit_drafts WHERE project_id=? AND episode_id=?", (project_id, episode_id)).fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="请先保存剪辑草稿")
            edit = json.loads(row["edit_json"])
            clips = resolve_clips(conn, project_id, episode_id, [ClipInput(**item) for item in edit["clips"]])
            if not clips:
                raise HTTPException(status_code=400, detail="时间线没有可导出的片段")
            snapshot_clips = []
            for clip in clips:
                # Asset paths are resolved from trusted project/episode/database records, never from client paths.
                if clip["sourceId"].startswith("episode:"):
                    episode = get_episode_or_first(conn, project_id, episode_id)
                    source_path = Path(episode["file_path"]).resolve()
                else:
                    media = conn.execute("SELECT file_path,project_id,episode_id FROM video_media WHERE id=?", (clip["sourceId"],)).fetchone()
                    if not media or media["project_id"] != project_id or media["episode_id"] != episode_id:
                        raise HTTPException(status_code=400, detail="素材不属于当前项目或本集")
                    source_path = Path(media["file_path"]).resolve()
                snapshot_clips.append({**clip, "path": str(source_path), "durationMs": clip["outMs"] - clip["inMs"]})
            snapshot = {"projectId": project_id, "episodeId": episode_id, "revision": row["revision"], "aspectRatio": edit["aspectRatio"], "resolution": edit["resolution"], "fps": edit["fps"], "clips": snapshot_clips}
            expected = sum(item["durationMs"] for item in snapshot_clips) / 1000
            job_id = new_id("export")
            output_path = (projects_dir() / project_id / "outputs" / "videos" / f"{job_id}.mp4").resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            conn.execute("INSERT INTO composition_exports (id,project_id,episode_id,revision,status,progress,stage,snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (job_id, project_id, episode_id, row["revision"], "queued", 0, "排队中", json.dumps(snapshot, ensure_ascii=False), utc_now()))
        background_tasks.add_task(run_export, job_id, snapshot, output_path, expected)
        return {"job": {"id": job_id, "status": "queued", "progress": 0, "stage": "排队中", "revision": snapshot["revision"]}}

    @router.get("/api/projects/{project_id}/video-export-jobs")
    def list_exports(project_id: str, episode_id: str | None = None) -> dict[str, Any]:
        with connect() as conn:
            get_project_or_404(conn, project_id)
            if episode_id:
                rows = conn.execute("SELECT * FROM composition_exports WHERE project_id=? AND episode_id=? ORDER BY created_at DESC", (project_id, episode_id)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM composition_exports WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
            jobs = []
            for source in rows:
                item = dict(source); item.pop("snapshot_json", None)
                if item.get("output_path"):
                    item["downloadUrl"] = data_file_url(item["output_path"]) + "?download=true"
                jobs.append(item)
            return {"jobs": jobs}

    @router.get("/api/projects/{project_id}/video-export-jobs/{job_id}")
    def get_export(project_id: str, job_id: str) -> dict[str, Any]:
        with connect() as conn:
            get_project_or_404(conn, project_id)
            row = conn.execute("SELECT * FROM composition_exports WHERE project_id=? AND id=?", (project_id, job_id)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="导出任务不存在")
            item = dict(row); item.pop("snapshot_json", None)
            if item.get("output_path"):
                item["downloadUrl"] = data_file_url(item["output_path"]) + "?download=true"
            return {"job": item}

    @router.post("/api/projects/{project_id}/video-export-jobs/{job_id}/retry")
    def retry_export(project_id: str, job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            get_project_or_404(conn, project_id)
            row = conn.execute("SELECT * FROM composition_exports WHERE project_id=? AND id=?", (project_id, job_id)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="导出任务不存在")
            if row["status"] not in {"failed", "cancelled"}:
                raise HTTPException(status_code=409, detail="只有失败任务可以重试")
            active = conn.execute("SELECT id FROM composition_exports WHERE project_id=? AND episode_id=? AND status IN ('queued','running') LIMIT 1", (project_id, row["episode_id"])).fetchone()
            if active:
                raise HTTPException(status_code=409, detail="本集已有导出任务正在运行，请等待完成后再重试")
            snapshot = json.loads(row["snapshot_json"])
            new_job_id = new_id("export")
            output_path = (projects_dir() / project_id / "outputs" / "videos" / f"{new_job_id}.mp4").resolve()
            expected = sum(item["durationMs"] for item in snapshot["clips"]) / 1000
            conn.execute("INSERT INTO composition_exports (id,project_id,episode_id,revision,status,progress,stage,snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (new_job_id, project_id, row["episode_id"], row["revision"], "queued", 0, "重试排队中", row["snapshot_json"], utc_now()))
        background_tasks.add_task(run_export, new_job_id, snapshot, output_path, expected)
        return {"job": {"id": new_job_id, "status": "queued", "progress": 0, "stage": "重试排队中", "revision": snapshot["revision"]}}

    return router
