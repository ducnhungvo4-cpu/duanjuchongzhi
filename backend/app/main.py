from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from PIL import Image, UnidentifiedImageError
from json_repair import repair_json

from backend.app.providers.model_provider import ProviderError, get_model_provider, get_model_status
from backend.app.composition import make_router


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
PROJECTS_DIR = DATA_DIR / "projects"
DB_PATH = DATA_DIR / "workshop.sqlite3"
CONFIG_DIR = ROOT / "config"
MODELS_CONFIG_PATH = CONFIG_DIR / "models.json"

REMAKE_MODES = {"overseas_localization", "cg_3d", "asset_replacement"}
STORYBOARD_STYLE_TEMPLATES = {
    "live_action": "真人实拍电影质感。角色与环境采用真实世界的比例、材质、自然光照及摄影成像，保持原分镜的动作、情绪、站位、构图和运镜；避免三维CG与卡通渲染质感。",
    "cg_3d": "高端三维游戏CG过场动画质感，Unreal Engine 5 cinematic render，Final Fantasy CG style，非摄影式三维网格与数字雕刻材质，PBR材质和电影级三维光照；保持原分镜的动作、情绪、站位、构图和运镜，角色与场景风格统一；避免真人摄影皮肤、照片颗粒与二维插画。",
}
STORYBOARD_STYLE_TEMPLATE_VERSION = "storyboard-visual-style-v1"
CG_ROLE_FIXED_TEMPLATE = """[Style & Render] High-end 3D game character asset sheet, Next-gen game character model, Unreal Engine 5 cinematic render, Octane render, Zbrush digital sculpt, Final Fantasy CG style, hyper-stylized 3D, non-photorealistic mesh rendering.
[3D Surface Shader] Stylized 3D character skin shader, heavy subsurface scattering (SSS), PBR material maps, clean 3D topology. All facial lines, age signs, and structural details are defined by 3D digital sculpting, ABSOLUTELY NO real-world photographic skin pores, photo grain, or noise.
[Layout & Light] Multi-view layout: Left panel features a front portrait bust facing camera. Right panels show three full-body standing poses (front view, 90-degree right side view, back view). Pure clean white background, no text, single-row grid. Studio 3D lighting, three-point lighting, ambient occlusion, sharp rim light, ray tracing, 8k resolution, masterpiece."""
CG_SCENE_FIXED_TEMPLATE = """[Style & Render] High-end 3D CG environment, Unreal Engine 5 cinematic render, Octane render, Final Fantasy CG style, hyper-stylized 3D, non-photorealistic mesh rendering.
[Scene & Light] Preserve the architectural era, layout, materials, furniture, props and spatial relationships described in the source scene. PBR materials, detailed digital geometry, cinematic 3D lighting, ambient occlusion, ray tracing. Single scene view, no characters, no text, no collage, no multi-view asset-sheet layout."""


def load_local_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_local_env()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def data_file_url(file_path: str) -> str:
    path = Path(file_path)
    if not path.is_absolute():
        path = ROOT / path
    try:
        relative = path.resolve().relative_to(DATA_DIR.resolve())
    except ValueError:
        return ""
    return f"/api/files/{relative.as_posix()}"


def enrich_asset_image(image: dict[str, Any]) -> dict[str, Any]:
    image["payload"] = json.loads(image.pop("payload_json"))
    image["file_url"] = data_file_url(str(image.get("file_path") or ""))
    return image


def image_extension(mime_type: str) -> str:
    if mime_type == "image/jpeg":
        return ".jpg"
    if mime_type == "image/svg+xml":
        return ".svg"
    extension = mimetypes.guess_extension(mime_type or "image/png")
    return extension or ".png"


def enrich_asset_payload_urls(payload: dict[str, Any]) -> dict[str, Any]:
    for target in [payload, payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else None]:
        if not isinstance(target, dict):
            continue
        frame_path = target.get("原资产图路径") or target.get("原资产截图路径")
        if isinstance(frame_path, str) and frame_path:
            target["原资产图URL"] = data_file_url(frame_path)
            target["原资产截图URL"] = target["原资产图URL"]
    return payload


def parse_asset_timecode(value: Any) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if not isinstance(value, str):
        return 0.0
    value = value.strip()
    if not value:
        return 0.0
    parts = re.split(r"[:：]", value)
    try:
        if len(parts) == 3:
            return max(0.0, int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2]))
        if len(parts) == 2:
            return max(0.0, int(parts[0]) * 60 + float(parts[1]))
        return max(0.0, float(parts[0]))
    except ValueError:
        return 0.0


def format_timecode(seconds: float) -> str:
    seconds = max(0.0, seconds)
    whole = int(seconds)
    fraction = seconds - whole
    hours = whole // 3600
    minutes = (whole % 3600) // 60
    secs = whole % 60
    if fraction:
        return f"{hours:02d}:{minutes:02d}:{secs + fraction:05.2f}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_time_range_seconds(start_value: Any, end_value: Any) -> tuple[float, float]:
    start = parse_asset_timecode(start_value)
    end = parse_asset_timecode(end_value)
    if end and end < start:
        end = start
    return start, end or start


def asset_capture_time_candidates(payload: dict[str, Any]) -> list[float]:
    child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
    candidates: list[float] = []
    for source in [child, payload]:
        if not isinstance(source, dict):
            continue
        for key in ["原剧截图时间点候选", "截图时间点候选", "原剧截图时间点", "截图时间点", "时间码"]:
            value = source.get(key)
            values = value if isinstance(value, list) else [value]
            for item in values:
                seconds = parse_asset_timecode(item)
                if seconds > 0 or str(item).strip() in {"0", "0.0", "00:00:00"}:
                    candidates.append(seconds)
    first = first_asset_timecode(payload)
    if first > 0 or not candidates:
        candidates.append(first)
    deduped: list[float] = []
    for seconds in candidates:
        rounded = round(seconds, 2)
        if rounded not in deduped:
            deduped.append(rounded)
    return deduped[:5]


def first_asset_timecode(payload: dict[str, Any]) -> float:
    child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
    for source in [child, payload]:
        first = source.get("首次出现信息") if isinstance(source.get("首次出现信息"), dict) else {}
        timecode = first.get("时间码")
        if timecode:
            return parse_asset_timecode(timecode)
        records = source.get("出现记录") if isinstance(source.get("出现记录"), list) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            ranges = record.get("出现时间段") if isinstance(record.get("出现时间段"), list) else []
            for item in ranges:
                if isinstance(item, dict) and item.get("开始时间"):
                    return parse_asset_timecode(item.get("开始时间"))
    return 0.0


def capture_asset_original_frames(project_id: str, episode: dict[str, Any], assets: list[dict[str, Any]]) -> dict[str, int]:
    ffmpeg = shutil.which("ffmpeg")
    video_path = Path(str(episode.get("file_path") or ""))
    if not ffmpeg or not video_path.exists():
        return {"候选时码数": 0, "解码进程数": 0, "成功帧数": 0}
    output_dir = PROJECTS_DIR / project_id / "assets" / "original_frames"
    output_dir.mkdir(parents=True, exist_ok=True)
    asset_candidates: dict[int, list[float]] = {}
    time_to_index: dict[float, int] = {}
    for asset in assets:
        payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
        child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else None
        if payload.get("原资产图路径") or (isinstance(child, dict) and child.get("原资产图路径")):
            continue
        base_candidates = asset_capture_time_candidates(payload)
        candidates: list[float] = []
        for seconds in base_candidates:
            candidates.extend([max(0.0, seconds - 1.0), seconds, seconds + 1.0])
        deduped_candidates: list[float] = []
        for seconds in candidates:
            rounded = round(seconds, 2)
            if rounded not in deduped_candidates:
                deduped_candidates.append(rounded)
        asset_candidates[id(asset)] = deduped_candidates[:9]
        for seconds in deduped_candidates[:9]:
            time_to_index.setdefault(seconds, -1)
    ordered_times = list(time_to_index)
    frame_for_time: dict[float, Path] = {}
    process_count = 0
    group_size = 48
    for group_start in range(0, len(ordered_times), group_size):
        group = ordered_times[group_start : group_start + group_size]
        group_dir = output_dir / f"decode_{new_id('frames')}"
        group_dir.mkdir(parents=True, exist_ok=True)
        clauses = [f"gte(t\\,{seconds:.3f})*(isnan(prev_selected_t)+lt(prev_selected_t\\,{seconds:.3f}))" for seconds in group]
        expression = "+".join(clauses)
        command = [ffmpeg, "-y", "-i", str(video_path), "-vf", f"select={expression},showinfo", "-vsync", "0", "-q:v", "2", str(group_dir / "%03d.jpg")]
        process_count += 1
        try:
            decoded = subprocess.run(command, check=True, capture_output=True, timeout=90, text=True, errors="replace")
        except (subprocess.SubprocessError, OSError):
            shutil.rmtree(group_dir, ignore_errors=True)
            continue
        images = sorted(group_dir.glob("*.jpg"))
        decoded_times = [float(value) for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", decoded.stderr)]
        for frame_time, frame_path in zip(decoded_times, images):
            matches = [seconds for seconds in group if seconds <= frame_time and seconds not in frame_for_time]
            for target in matches:
                if target not in frame_for_time:
                    frame_for_time[target] = frame_path
    successful_frames = len(frame_for_time)
    for asset in assets:
        payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
        child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else None
        candidates = asset_candidates.get(id(asset), [])
        captured: list[dict[str, Any]] = []
        for seconds in candidates:
            frame_path = frame_for_time.get(seconds)
            if frame_path is None or not frame_path.exists():
                continue
            captured.append(
                {
                    "时间点": format_timecode(seconds),
                    "路径": str(frame_path),
                    "URL": data_file_url(str(frame_path)),
                }
            )
        if not captured:
            continue
        main_capture = captured[min(1, len(captured) - 1)]
        payload["原资产图路径"] = main_capture["路径"]
        payload["原资产图URL"] = main_capture["URL"]
        payload["原资产截图路径"] = main_capture["路径"]
        payload["原资产截图URL"] = main_capture["URL"]
        payload["原资产候选截图"] = captured
        if isinstance(child, dict):
            child["原资产图路径"] = main_capture["路径"]
            child["原资产图URL"] = main_capture["URL"]
            child["原资产截图路径"] = main_capture["路径"]
            child["原资产截图URL"] = main_capture["URL"]
            child["原资产候选截图"] = captured
    return {"候选时码数": len(ordered_times), "解码进程数": process_count, "成功帧数": successful_frames}


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              target_country TEXT NOT NULL,
              target_language TEXT NOT NULL,
              status TEXT NOT NULL,
              remake_mode TEXT NOT NULL DEFAULT 'overseas_localization',
              source_language TEXT NOT NULL DEFAULT '',
              storyboard_visual_style TEXT NOT NULL DEFAULT 'live_action',
              replace_characters INTEGER NOT NULL DEFAULT 0,
              replace_scenes INTEGER NOT NULL DEFAULT 0,
              parent_project_id TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS episodes (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              episode_index INTEGER NOT NULL,
              filename TEXT NOT NULL,
              file_path TEXT NOT NULL,
              source_url TEXT NOT NULL DEFAULT '',
              ark_file_id TEXT NOT NULL DEFAULT '',
              ark_file_status TEXT NOT NULL DEFAULT '',
              ark_file_error TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(project_id, episode_index)
            );

            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              episode_id TEXT,
              task_type TEXT NOT NULL,
              status TEXT NOT NULL,
              message TEXT NOT NULL,
              result_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_runs (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              project_id TEXT NOT NULL,
              episode_id TEXT,
              agent_name TEXT NOT NULL,
              model_mode TEXT NOT NULL,
              model_name TEXT NOT NULL,
              prompt_file TEXT NOT NULL,
              input_json TEXT NOT NULL,
              output_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS assets (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              episode_id TEXT,
              asset_type TEXT NOT NULL,
              name TEXT NOT NULL,
              localized_name TEXT NOT NULL,
              variable TEXT NOT NULL,
              status TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS asset_images (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              asset_id TEXT NOT NULL,
              status TEXT NOT NULL,
              prompt TEXT NOT NULL,
              file_path TEXT NOT NULL,
              is_selected INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS asset_generation_jobs (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              asset_id TEXT NOT NULL,
              remake_mode TEXT NOT NULL,
              template_version TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS storyboards (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              episode_id TEXT NOT NULL,
              title TEXT NOT NULL,
              time_range TEXT NOT NULL,
              prompt TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS video_generations (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              episode_id TEXT NOT NULL,
              storyboard_id TEXT NOT NULL,
              status TEXT NOT NULL,
              prompt TEXT NOT NULL,
              output_path TEXT NOT NULL,
              is_selected INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS video_media (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              episode_id TEXT NOT NULL,
              filename TEXT NOT NULL,
              file_path TEXT NOT NULL,
              media_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS video_edit_drafts (
              project_id TEXT NOT NULL,
              episode_id TEXT NOT NULL,
              title TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 0,
              edit_json TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(project_id, episode_id)
            );

            CREATE TABLE IF NOT EXISTS composition_exports (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              episode_id TEXT NOT NULL,
              revision INTEGER NOT NULL,
              status TEXT NOT NULL,
              progress INTEGER NOT NULL DEFAULT 0,
              stage TEXT NOT NULL,
              snapshot_json TEXT NOT NULL,
              output_path TEXT NOT NULL DEFAULT '',
              output_url TEXT NOT NULL DEFAULT '',
              output_duration_ms INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              started_at TEXT NOT NULL DEFAULT '',
              finished_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_video_media_episode ON video_media(project_id, episode_id);
            """
        )
        ensure_column(conn, "episodes", "source_url", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "episodes", "ark_file_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "episodes", "ark_file_status", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "episodes", "ark_file_error", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "projects", "remake_mode", "TEXT NOT NULL DEFAULT 'overseas_localization'")
        ensure_column(conn, "projects", "source_language", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "projects", "storyboard_visual_style", "TEXT NOT NULL DEFAULT 'live_action'")
        ensure_column(conn, "projects", "replace_characters", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "projects", "replace_scenes", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "projects", "parent_project_id", "TEXT NOT NULL DEFAULT ''")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def load_models_config() -> dict[str, Any]:
    if not MODELS_CONFIG_PATH.exists():
        return {"mode": "mock", "agents": {}}
    return json.loads(MODELS_CONFIG_PATH.read_text(encoding="utf-8"))


def load_agent_config(agent_key: str) -> dict[str, Any]:
    config = load_models_config()
    agent = config.get("agents", {}).get(agent_key, {})
    prompt_file = agent.get("promptFile", "")
    prompt_path = ROOT / prompt_file if prompt_file else None
    prompt = prompt_path.read_text(encoding="utf-8") if prompt_path and prompt_path.exists() else ""
    return {
        "mode": config.get("mode", "mock"),
        "provider": config.get("provider", "volcengine"),
        "provider": agent.get("provider", ""),
        "model": agent.get("model_name") or agent.get("model", ""),
        "enabled": agent.get("enabled", True),
        "prompt_file": prompt_file,
        "prompt": prompt,
        "temperature": agent.get("temperature"),
        "output_format": agent.get("outputFormat", "json"),
    }


def prepare_agent(agent_key: str) -> dict[str, Any]:
    config = load_models_config()
    agent = load_agent_config(agent_key)
    if not agent.get("enabled", True):
        mock_config = dict(config)
        mock_config["mode"] = "mock"
        provider = get_model_provider(mock_config)
        prepared = provider.prepare_agent(agent_key, agent)
        agent.update(
            {
                "mode": prepared.mode,
                "provider": prepared.provider,
                "model_name": prepared.model_name,
                "model_key": prepared.model_key,
                "ready": prepared.ready,
            }
        )
        return agent
    provider = get_model_provider(config)
    prepared = provider.prepare_agent(agent_key, agent)
    agent.update(
        {
            "mode": prepared.mode,
            "provider": prepared.provider,
            "model_name": prepared.model_name,
            "model_key": prepared.model_key,
            "ready": prepared.ready,
        }
    )
    return agent


def call_agent_chat(agent_key: str, agent: dict[str, Any], input_payload: dict[str, Any]) -> str:
    config = load_models_config()
    provider = get_model_provider(config)
    video_url = str(input_payload.get("__video_url") or "").strip()
    video_file_id = str(input_payload.get("__video_file_id") or "").strip()
    public_payload = {key: value for key, value in input_payload.items() if not key.startswith("__")}
    user_content = json.dumps(public_payload, ensure_ascii=False, indent=2)
    return provider.chat_completion(
        model_name=agent.get("model_name", ""),
        system_prompt=agent.get("prompt", ""),
        user_content=user_content,
        temperature=agent.get("temperature"),
        response_format={"type": "json_object"} if agent.get("output_format") == "json" else None,
        video_url=video_url or None,
        video_file_id=video_file_id or None,
        video_fps=1 if video_url else None,
    )


def call_compact_video_json(agent: dict[str, Any], input_payload: dict[str, Any], compact_prompt: str) -> str:
    config = load_models_config()
    provider = get_model_provider(config)
    video_url = str(input_payload.get("__video_url") or "").strip()
    video_file_id = str(input_payload.get("__video_file_id") or "").strip()
    public_payload = {key: value for key, value in input_payload.items() if not key.startswith("__")}
    user_content = json.dumps(public_payload, ensure_ascii=False, indent=2)
    return provider.chat_completion(
        model_name=agent.get("model_name", ""),
        system_prompt=compact_prompt,
        user_content=user_content,
        temperature=agent.get("temperature"),
        response_format={"type": "json_object"},
        video_url=video_url or None,
        video_file_id=video_file_id or None,
        video_fps=1 if video_url else None,
    )


def call_compact_video_json_stream(
    agent: dict[str, Any],
    input_payload: dict[str, Any],
    compact_prompt: str,
    on_progress: Callable[[str, int], None] | None = None,
) -> tuple[str, int, list[tuple[str, int]], str]:
    config = load_models_config()
    provider = get_model_provider(config)
    if not hasattr(provider, "chat_completion_stream"):
        try:
            raw = call_compact_video_json(agent, input_payload, compact_prompt)
            return raw, len(raw), [], ""
        except ProviderError as exc:
            return "", 0, [], str(exc)
    video_url = str(input_payload.get("__video_url") or "").strip()
    video_file_id = str(input_payload.get("__video_file_id") or "").strip()
    public_payload = {key: value for key, value in input_payload.items() if not key.startswith("__")}
    user_content = json.dumps(public_payload, ensure_ascii=False, indent=2)
    accumulated = ""
    events: list[tuple[str, int]] = []
    stream_error = ""
    try:
        for event_type, chunk_data in provider.chat_completion_stream(
            model_name=agent.get("model_name", ""),
            system_prompt=compact_prompt,
            user_content=user_content,
            temperature=agent.get("temperature"),
            response_format={"type": "json_object"},
            video_url=video_url or None,
            video_file_id=video_file_id or None,
            video_fps=1 if video_url else None,
        ):
            stage = "processing"
            if event_type == "response.output_text.delta":
                delta = str(chunk_data.get("delta") or "")
                accumulated += delta
                stage = "generating"
            elif event_type in {"response.completed", "done"}:
                stage = "done"
            if not events or events[-1][0] != stage:
                events.append((stage, len(accumulated)))
            if on_progress:
                on_progress(stage, len(accumulated))
    except ProviderError as exc:
        stream_error = str(exc)
        if on_progress:
            on_progress("failed", len(accumulated))
    return accumulated, len(accumulated), events, stream_error


ASSET_EXTRACTION_COMPACT_PROMPT = """你是短剧重制资产识别引擎。读取视频后只输出合法 JSON，不输出 Markdown。
目标：完整提取当前集影响复刻的角色、群像角色、场景、道具/配件，以及资产出现片段索引。不要输出超长资产图结构化模板。
要求：
1. 角色包含主角、配角、教练、裁判、队友、对手、明确有画面功能的群像代表。
2. 道具包含篮球、哨子、记分牌、战术板、球衣号码、球鞋、水瓶、毛巾等影响复刻的物件或配件。
3. 每个视觉描述必须具体，写清原剧视觉事实和目标国家本地化结果，禁止“符合目标国家审美”这类空话。
4. 每个角色至少 1 个子资产；子资产只因服装/发型/状态显著变化而拆分。
5. 资产出现片段索引必须按时间顺序覆盖全片关键剧情段，每段出现资产变量必须准确映射到子资产。
6. 每个角色子资产、场景子资产和道具必须输出独立的原剧截图时间点候选，时间点必须能清楚看到该资产本体；不要所有资产复用同一个时间点。
输出结构：
{
  "当前集编号": "",
  "目标国家": "",
  "目标语言": "",
  "资产库": {
    "角色资产": [
      {
        "主资产编号": "角色_001",
        "原剧角色名称": "",
        "本地化角色名称": "",
        "剧情身份": "",
        "剧情作用": "",
        "人物关系概述": "",
        "稳定人设定位": "",
        "原剧核心形象描述": "",
        "本地化核心形象描述": "",
        "基础音色描述": "",
        "目标语言声音要求": "",
        "子资产": [
          {
            "子资产编号": "角色_001_子资产_001",
            "角色资产变量": "{{角色_001_子资产_001}}",
            "原剧子资产名称": "",
            "本地化子资产名称": "",
            "差异类型": "服装",
            "差异标签": "",
            "原剧视觉形象描述": "",
            "本地化视觉形象描述": "",
            "状态音色调整": "",
            "原剧截图时间点候选": ["00:00:09.50", "00:00:10.50"],
            "截图对象描述": "",
            "截图理由": "",
            "角色资产图生成提示词": ""
          }
        ]
      }
    ],
    "场景资产": [
      {
        "主资产编号": "场景_001",
        "原剧场景名称": "",
        "本地化场景名称": "",
        "空间结构描述": "",
        "社会阶层与情绪功能": "",
        "原剧场景描述": "",
        "本地化场景描述": "",
        "子资产": [
          {
            "子资产编号": "场景_001_子资产_001",
            "场景资产变量": "{{场景_001_子资产_001}}",
            "原剧子资产名称": "",
            "本地化子资产名称": "",
            "状态差异": "",
            "原剧视觉描述": "",
            "本地化视觉描述": "",
            "原剧截图时间点候选": ["00:00:03.00", "00:00:04.00"],
            "截图对象描述": "",
            "截图理由": "",
            "场景资产图生成提示词": ""
          }
        ]
      }
    ],
    "道具资产": [
      {
        "道具编号": "道具_001",
        "道具资产变量": "{{道具_001}}",
        "原剧道具名称": "",
        "本地化道具名称": "",
        "剧情功能": "",
        "原剧道具描述": "",
        "本地化道具描述": "",
        "原剧截图时间点候选": ["00:00:12.00", "00:00:13.00"],
        "截图对象描述": "",
        "截图理由": "",
        "道具资产图生成提示词": ""
      }
    ]
  },
  "资产出现片段索引": [
    {
      "片段编号": "片段_001",
      "开始时间": "00:00:00",
      "结束时间": "00:00:08",
      "片段剧情简述": "",
      "出现资产变量": ["{{角色_001_子资产_001}}", "{{场景_001_子资产_001}}"]
    }
  ]
}"""


ASSET_DETAIL_COMPLETION_PROMPT = """你只补全输入资产的“缺失字段”。只输出 JSON：{"资产补全":[{"资产变量":"原变量","payload_patch":{},"current_child_patch":{},"needsReview":[]}]}
只返回输入批次中的资产，变量原样复制。仅写入缺失字段清单列出的键；不得改写已存在值、资产名称、变量或编号。只依据给出的原片描述及已知事实生成本地化文字。原片未明确的外貌、族裔、肤色、脸部、眼睛、发型、服装或文字不得推断，字段写 unknown 并在 needsReview 说明。不得补入原文不存在的情节/资产。"""


def call_agent_chat_stream(
    agent_key: str,
    agent: dict[str, Any],
    input_payload: dict[str, Any],
    on_progress: Callable[[str, int], None] | None = None,
) -> tuple[str, int, list[tuple[str, int]]]:
    """Call agent with SSE streaming. Returns (full_text, total_chars, progress_events).
    on_progress(stage, chars_so_far) is called for each meaningful event.
    """
    config = load_models_config()
    provider = get_model_provider(config)
    video_url = str(input_payload.get("__video_url") or "").strip()
    video_file_id = str(input_payload.get("__video_file_id") or "").strip()
    public_payload = {key: value for key, value in input_payload.items() if not key.startswith("__")}
    user_content = json.dumps(public_payload, ensure_ascii=False, indent=2)

    accumulated = ""
    total_chars = 0
    events: list[tuple[str, int]] = []
    stage = "connecting"

    for event_type, chunk_data in provider.chat_completion_stream(
        model_name=agent.get("model_name", ""),
        system_prompt=agent.get("prompt", ""),
        user_content=user_content,
        temperature=agent.get("temperature"),
        response_format={"type": "json_object"} if agent.get("output_format") == "json" else None,
        video_url=video_url or None,
        video_file_id=video_file_id or None,
        video_fps=1 if video_url else None,
    ):
        if event_type == "response.in_progress":
            stage = "processing"
        elif event_type == "response.output_text.delta":
            stage = "generating"
            delta = str(chunk_data.get("delta") or "")
            accumulated += delta
            total_chars += len(delta)
        elif event_type == "response.completed":
            stage = "done"

        if not events or stage != events[-1][0]:
            events.append((stage, total_chars))
            if on_progress:
                on_progress(stage, total_chars)

    if on_progress:
        on_progress("done", total_chars)
    return accumulated, total_chars, events


def parse_json_object(raw: str, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else fallback
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(raw[start : end + 1])
                return parsed if isinstance(parsed, dict) else fallback
            except json.JSONDecodeError:
                return fallback
        return fallback


def parse_json_object_with_repair(raw: str) -> tuple[dict[str, Any], bool]:
    try:
        parsed = json.loads(raw)
        return (parsed, False) if isinstance(parsed, dict) else ({}, False)
    except json.JSONDecodeError:
        try:
            parsed = repair_json(raw, return_objects=True)
        except Exception:
            return {}, False
        return (parsed, True) if isinstance(parsed, dict) else ({}, False)


def extract_episode_index(filename: str, fallback: int) -> int:
    stem = Path(filename).stem
    matches = re.findall(r"\d+", stem)
    if not matches:
        return fallback
    return int(matches[-1])


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    project = row_to_dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def get_episode_or_first(conn: sqlite3.Connection, project_id: str, episode_id: str | None) -> dict[str, Any]:
    if episode_id:
        episode = row_to_dict(
            conn.execute(
                "SELECT * FROM episodes WHERE id = ? AND project_id = ?",
                (episode_id, project_id),
            ).fetchone()
        )
    else:
        episode = row_to_dict(
            conn.execute(
                "SELECT * FROM episodes WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        )
    if not episode:
        raise HTTPException(status_code=400, detail="Upload an episode before running this task")
    return episode


def enrich_episode(episode: dict[str, Any] | None) -> dict[str, Any] | None:
    if not episode:
        return episode
    file_path = Path(str(episode.get("file_path") or ""))
    episode["size_bytes"] = file_path.stat().st_size if file_path.exists() else 0
    episode["file_url"] = data_file_url(str(file_path)) if file_path.is_file() else ""
    return episode


def enrich_episodes(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [enrich_episode(episode) or episode for episode in episodes]


def upload_episode_to_ark(conn: sqlite3.Connection, episode: dict[str, Any]) -> dict[str, str]:
    try:
        config = load_models_config()
        provider = get_model_provider(config)
        upload_result = provider.upload_file(episode["file_path"], purpose="user_data", video_fps=1)
        file_id = str(upload_result.get("id") or "")
        status = str(upload_result.get("status") or "processing")
        if file_id:
            for _ in range(3):
                if status in {"processed", "completed", "success"}:
                    break
                time.sleep(2)
                try:
                    file_info = provider.retrieve_file(file_id)
                    status = str(file_info.get("status") or status)
                except ProviderError:
                    break
        conn.execute(
            """
            UPDATE episodes
            SET ark_file_id = ?, ark_file_status = ?, ark_file_error = ?
            WHERE id = ?
            """,
            (file_id, status, "", episode["id"]),
        )
        return {"file_id": file_id, "status": status, "error": ""}
    except ProviderError as exc:
        error = str(exc)
        conn.execute(
            """
            UPDATE episodes
            SET ark_file_status = ?, ark_file_error = ?
            WHERE id = ?
            """,
            ("upload_failed", error, episode["id"]),
        )
        return {"file_id": "", "status": "upload_failed", "error": error}


def get_asset_response(conn: sqlite3.Connection, project_id: str, asset_id: str) -> dict[str, Any]:
    project = get_project_or_404(conn, project_id)
    asset = row_to_dict(
        conn.execute(
            "SELECT * FROM assets WHERE id = ? AND project_id = ?",
            (asset_id, project_id),
        ).fetchone()
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset["payload"] = enrich_asset_payload_urls(json.loads(asset.pop("payload_json")))
    source_description = asset_source_description(asset["asset_type"], asset["payload"])
    asset["payload"].setdefault("原片资产描述", source_description or "原片描述未留存")
    asset["payload"].setdefault("目标生成描述", source_description if project.get("remake_mode") == "cg_3d" else "")
    if project.get("remake_mode") == "asset_replacement":
        asset["payload"].setdefault("目标资产来源", "沿用原貌")
    selected_image = conn.execute(
        "SELECT id,file_path,payload_json FROM asset_images WHERE project_id=? AND asset_id=? AND is_selected=1 ORDER BY created_at DESC LIMIT 1",
        (project_id, asset_id),
    ).fetchone()
    effective = resolve_effective_asset_reference(asset, project, selected_image)
    asset["payload"]["effectiveAssetSource"] = effective["effectiveAssetSource"]
    asset["payload"]["当前有效外观描述"] = effective["description"]
    asset["payload"]["当前有效资产名"] = effective["name"]
    asset["payload"]["当前有效资产就绪"] = effective["ready"]
    asset["payload"]["最终资产图生成提示词"] = build_project_asset_prompt(
        asset["asset_type"], asset["payload"], project
    )
    attach_display_variable(asset)
    images = rows_to_dicts(
        conn.execute(
            "SELECT * FROM asset_images WHERE asset_id = ? ORDER BY created_at DESC",
            (asset_id,),
        ).fetchall()
    )
    for image in images:
        enrich_asset_image(image)
    asset["images"] = images
    return {"asset": asset}


def asset_source_description(asset_type: str, payload: dict[str, Any]) -> str:
    keys = {
        "角色": ["原剧视觉形象描述", "原剧角色描述", "原始角色描述", "角色原始描述", "角色描述"],
        "场景": ["原剧场景描述", "原始场景描述", "空间结构描述", "场景描述"],
        "道具": ["原剧道具描述", "原始道具描述", "道具描述", "剧情功能"],
    }.get(asset_type, ["原始描述"])
    child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
    value = pick_payload_text(child, keys) or pick_payload_text(payload, keys)
    if not value and asset_type == "角色":
        value = pick_payload_text(payload, ["原剧核心形象描述", "原剧人物外观"])
    return value


def strip_previous_target_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_previous_target_fields(item)
            for key, item in value.items()
            if not key.startswith("本地化") and key not in {"目标生成描述", "目标资产来源", "展示资产变量", "最终资产图生成提示词", "最终视频生成提示词"}
        }
    if isinstance(value, list):
        return [strip_previous_target_fields(item) for item in value]
    return value


def clean_target_asset_description(text: str, style: str) -> str:
    """Clean style/layout conflicts from generation-only text; never mutate source facts."""
    cleaned = str(text or "")
    if style == "cg_3d":
        patterns = (
            r"真人(?:实拍|写实|摄影)(?:电影质感)?", r"照片级真实", r"真实摄影质感",
            r"photorealistic", r"photographic skin", r"真实世界摄影成像",
        )
        for pattern in patterns:
            cleaned = re.sub(rf"(?:避免|不要|禁止)(?:采用|使用)?{pattern}", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(?:避免|不要|禁止)(?:采用|使用)?(?:三维)?CG(?:渲染)?(?:与卡通渲染质感)?", "", cleaned, flags=re.IGNORECASE)
    else:
        for pattern in (r"(?:三维)?CG渲染", r"三维游戏CG", r"数字雕刻材质", r"3D render", r"CG style"):
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[，,、；;：:]\s*[，,、；;：:]", "，", cleaned)
    return cleaned.strip(" \t\n，,、；;：:")


def resolve_effective_asset_reference(
    asset: dict[str, Any], project: dict[str, Any], selected_image: sqlite3.Row | dict[str, Any] | None = None
) -> dict[str, Any]:
    """Resolve the one asset appearance that both the UI and storyboard should use."""
    payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
    asset_type = str(asset.get("asset_type") or "")
    mode = str(project.get("remake_mode") or "overseas_localization")
    source_description = asset_source_description(asset_type, payload) or "原片描述未留存"
    source_image_path = str(payload.get("原资产图路径") or "")
    target_description = pick_payload_text(payload, ["目标生成描述"])
    replacement_enabled = mode == "asset_replacement" and (
        (asset_type == "角色" and bool(project.get("replace_characters")))
        or (asset_type == "场景" and bool(project.get("replace_scenes")))
    )
    effective_source = "target"
    if mode == "asset_replacement" and (
        not replacement_enabled or payload.get("目标资产来源") == "沿用原貌"
    ):
        effective_source = "source"
    if effective_source == "source":
        image_path = source_image_path
        image_id = ""
        image_source = "project_source_frame" if source_image_path else ""
        description = source_description
        name = str(asset.get("name") or asset.get("localized_name") or "")
        ready = bool(source_image_path and source_description != "原片描述未留存")
    else:
        candidate = dict(selected_image) if selected_image else None
        image_path = str(candidate["file_path"]) if candidate else ("" if mode == "asset_replacement" and replacement_enabled else source_image_path)
        image_id = str(candidate["id"]) if candidate else ""
        candidate_payload = json.loads(candidate["payload_json"]) if candidate and candidate.get("payload_json") else {}
        image_source = str(candidate_payload.get("source") or ("project_source_frame" if image_path == source_image_path and image_path else ""))
        description = target_description or source_description
        name = str(asset.get("localized_name") or asset.get("name") or "")
        if mode == "asset_replacement" and replacement_enabled:
            ready = bool(candidate)
        else:
            ready = bool(image_path and description != "原片描述未留存")
    return {
        "effectiveAssetSource": effective_source,
        "description": description,
        "name": name,
        "imageId": image_id,
        "imagePath": image_path,
        "imageSource": image_source,
        "ready": ready,
    }


def build_project_asset_prompt(asset_type: str, payload: dict[str, Any], project: dict[str, Any]) -> str:
    mode = str(project.get("remake_mode") or "overseas_localization")
    if mode == "overseas_localization":
        return infer_asset_image_prompt(
            asset_type, payload, project.get("target_country", "目标国家"), project.get("target_language", "目标语言")
        )
    target = pick_payload_text(payload, ["目标生成描述"])
    if mode == "asset_replacement" and ((asset_type == "角色" and not project.get("replace_characters")) or (asset_type == "场景" and not project.get("replace_scenes"))):
        return "当前资产沿用原片外观与原片资产图；不使用历史目标候选。"
    if mode == "asset_replacement" and payload.get("目标资产来源") == "沿用原貌":
        return "当前资产沿用原片外观与原片资产图；不使用历史目标候选。"
    if not target:
        if mode == "asset_replacement":
            return "请先填写目标资产外观描述；已确认目标图以目标图为准，不沿用原片外观描述。"
        target = asset_source_description(asset_type, payload)
        if not target:
            return "请先补充目标资产外观描述；原片描述未留存，不能据此生成。"
    if mode == "cg_3d" and asset_type == "角色":
        target = clean_target_asset_description(target.replace(CG_ROLE_FIXED_TEMPLATE, ""), "cg_3d")
        return f"{target}\n\n{CG_ROLE_FIXED_TEMPLATE}"
    if mode == "cg_3d" and asset_type == "场景":
        target = clean_target_asset_description(target.replace(CG_SCENE_FIXED_TEMPLATE, ""), "cg_3d")
        return f"{target}\n\n{CG_SCENE_FIXED_TEMPLATE}"
    if mode == "asset_replacement":
        return target.strip()
    return target


def route_project_agent_prompt(agent: dict[str, Any], project: dict[str, Any], storyboard: bool = False) -> None:
    if (project.get("remake_mode") or "overseas_localization") == "overseas_localization":
        return
    source_language = project.get("source_language") or "原片语言（具体语言未知时也保持原片语言，不翻译）"
    route = (
        "\n\n【项目模式强约束】当前是国内重制项目。不得执行目标国家替换、跨文化本土化、英文/目标语言翻译；保留源视频人物、场景、道具和文字事实，只做用户指定的视觉风格重制。"
        f"成片语言保持：{source_language}。不得根据空字段猜测普通话、美国英语或其他语言，也不得将本提示当作语言识别结果。"
    )
    prompt = str(agent.get("prompt") or "")
    prompt = prompt.replace("短剧海外重制系统", "短剧重制系统")
    prompt = prompt.replace("目标国家为空时，默认使用美国。", "国内模式下目标国家不适用；海外模式才读取目标国家。")
    prompt = prompt.replace("目标语言为空时，默认使用英语。", "国内模式下使用源语言；海外模式才读取目标语言。")
    prompt = prompt.replace("目标国家", "项目目标国家（国内模式不适用）")
    agent["prompt"] = prompt + route


def project_asset_history_summary(
    conn: sqlite3.Connection,
    project_id: str,
    exclude_episode_id: str | None = None,
) -> list[dict[str, Any]]:
    query = """
        SELECT assets.*, episodes.episode_index
        FROM assets
        LEFT JOIN episodes ON episodes.id = assets.episode_id
        WHERE assets.project_id = ?
    """
    params: list[Any] = [project_id]
    if exclude_episode_id:
        query += " AND (assets.episode_id IS NULL OR assets.episode_id != ?)"
        params.append(exclude_episode_id)
    query += " ORDER BY assets.created_at, assets.asset_type"
    rows = rows_to_dicts(conn.execute(query, params).fetchall())
    summary_by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = json.loads(row.pop("payload_json"))
        asset = {
            "asset_type": row["asset_type"],
            "name": row["name"],
            "localized_name": row["localized_name"],
            "variable": row["variable"],
            "payload": payload,
        }
        attach_display_variable(asset)
        annotate_project_asset_keys(asset, row.get("episode_index"))
        payload = asset["payload"]
        key = str(payload.get("项目子资产Key") or asset_project_variant_key(asset))
        item = summary_by_key.setdefault(
            key,
            {
                "资产类型": asset["asset_type"],
                "原剧资产名称": asset["name"],
                "本地化资产名称": asset["localized_name"],
                "展示资产变量": asset.get("display_variable", ""),
                "项目主资产Key": payload.get("项目主资产Key", ""),
                "项目子资产Key": key,
                "剧情身份或功能": pick_payload_text(payload, ["剧情身份", "剧情功能", "空间结构描述"]),
                "出现集数": [],
            },
        )
        episode_index = row.get("episode_index")
        if isinstance(episode_index, int) and episode_index not in item["出现集数"]:
            item["出现集数"].append(episode_index)
    for item in summary_by_key.values():
        item["出现集数"] = sorted(item["出现集数"])
    return list(summary_by_key.values())


ROLE_FIXED_CONSTRAINT = """【固定约束】
风格：真人写实风格 画质：模拟真实拍摄效果，8K超高清
光线：明亮柔和
构图：左侧正面胸像（正面面对镜头，目光看向镜头），右侧三视图（依次是正面、右侧面90°、背面），不允许出现第4个全身人物，不允许上下分两行或多行网格。
背景：干净的白色背景，无文字 人物肤感：皮肤质感真实自然，纹理清晰
姿态：自然直立站姿，双臂自然下垂贴于身体两侧，手指自然微曲，双脚平行与肩同宽
表情：角色默认微表情神态，嘴巴自然闭合，禁止夸张表情 道具：无。双手空握不持任何物品，腰间不挂载任何悬挂物，画面中无任何道具武器
约束：无水印，无文字，无logo，无畸形肢体，角色完整不裁切，脚下有自然投影
摄影强化：专业人像摄影、85mm镜头、浅景深、影棚布光、高分辨率、对焦清晰
光效建议：柔光、伦勃朗光、蝴蝶光、侧逆光
画质与质感要求：8K超清、极致细节、照片级真实
负面限制：避免卡通、绘画、3D渲染、模糊、畸形、低质量"""


def pick_payload_text(payload: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def pick_nested_text(source: dict[str, Any], section_keys: list[str], field_keys: list[str]) -> str:
    for section_key in section_keys:
        section = source.get(section_key)
        if isinstance(section, dict):
            value = pick_payload_text(section, field_keys)
            if value:
                return value
    return ""


def role_prompt_field(payload: dict[str, Any], child: dict[str, Any], section: str, field: str, fallbacks: list[str] | None = None) -> str:
    fallbacks = fallbacks or []
    value = ""
    for source in [child, payload]:
        structure = source.get("角色资产图结构化描述")
        if isinstance(structure, dict):
            section_value = structure.get(section)
            if isinstance(section_value, dict):
                value = pick_payload_text(section_value, [field, *fallbacks])
                if value:
                    break
    if not value:
        value = pick_payload_text(child, [field, *fallbacks]) or pick_payload_text(payload, [field, *fallbacks])
    if value and not is_basketball_uniform_context(role_context_text(payload, child)) and contains_basketball_clothing_terms(value):
        value = ""
    return value or infer_role_prompt_field(payload, child, section, field)


def role_context_text(payload: dict[str, Any], child: dict[str, Any]) -> str:
    identity = pick_payload_text(payload, ["剧情身份", "稳定人设定位", "剧情作用"])
    items = [
        identity,
        pick_payload_text(payload, ["本地化核心形象描述"]),
        pick_payload_text(child, ["本地化视觉形象描述", "本地化子资产描述", "差异标签"]),
        pick_payload_text(child, ["本地化子资产名称"]),
        pick_payload_text(payload, ["本地化角色名称"]),
    ]
    cleaned: list[str] = []
    for item in items:
        if not item:
            continue
        if contains_basketball_clothing_terms(item) and not identity_allows_basketball_uniform(identity):
            continue
        cleaned.append(item)
    return "，".join(cleaned)


def is_female_context(text: str) -> bool:
    lower = text.lower()
    return any(token in text for token in ["女性", "女人", "女主", "母亲", "女孩", "妻子", "姐姐", "妹妹", "女儿"]) or any(token in lower for token in ["female", "woman", "girl", "mother", "wife", "sister", "daughter"])


def is_elder_context(text: str) -> bool:
    lower = text.lower()
    return any(token in text for token in ["老人", "老年", "爷爷", "奶奶", "外公", "外婆", "父亲", "母亲"]) or any(token in lower for token in ["elder", "old", "senior", "grandfather", "grandmother", "father", "mother"])


def identity_allows_basketball_uniform(text: str) -> bool:
    lower = text.lower()
    return any(token in text for token in ["篮球运动员", "篮球球员", "篮球队员", "球员", "运动员"]) or any(token in lower for token in ["basketball player", "athlete"])


def infer_role_prompt_field(payload: dict[str, Any], child: dict[str, Any], section: str, field: str) -> str:
    context = role_context_text(payload, child)
    identity = pick_payload_text(payload, ["剧情身份", "稳定人设定位", "剧情作用"]) or "核心剧情角色"
    visual = context or identity
    lower = visual.lower()
    if field == "性别":
        if any(token in visual for token in ["女性", "女人", "女主", "母亲", "女孩"]) or any(token in lower for token in ["female", "woman", "girl", "mother"]):
            return "女"
        if any(token in visual for token in ["男性", "男人", "男主", "父亲", "男孩"]) or any(token in lower for token in ["male", "man", "boy", "father"]):
            return "男"
        return "男性" if any(token in visual for token in ["球员", "男主", "男性"]) else "目标国家本地化成年人性别表现"
    if field == "视觉年龄":
        age_match = re.search(r"(\d{2})\s*(?:岁|year|years)", visual, re.IGNORECASE)
        if age_match:
            return f"{age_match.group(1)}岁"
        if any(token in visual for token in ["大学", "学生", "球员", "校园"]):
            return "20-24岁"
        if any(token in visual for token in ["总裁", "父亲", "母亲", "教练", "经理"]):
            return "30-45岁"
        return "20-30岁真人视觉年龄"
    if field == "国家 / 时代 / 朝代":
        return "目标国家本地化 / 现代"
    if field == "国家 / 时代":
        return "目标国家本地化 / 现代"
    if field == "族裔外貌":
        return "目标国家本地常见族裔外貌，五官骨相、肤色、发色和虹膜颜色必须与目标国家一致，不复用原剧东亚面部特征"
    if field == "身份定位":
        return identity
    if field == "头身比":
        return "真实真人比例，约7-8头身"
    if field == "身高体感":
        if any(token in visual for token in ["篮球", "球员", "运动员"]):
            return "高挑运动员体感，约185-195cm"
        if is_female_context(visual):
            return "符合目标国家同年龄女性的自然身高体感"
        if is_elder_context(visual):
            return "符合目标国家同年龄长辈角色的自然身高体感"
        return "符合目标国家同年龄、同性别、同身份人物的自然身高体感"
    if field == "体型":
        return visual
    if field == "肩颈":
        return "肩颈比例符合角色年龄、性别和身份，体态自然真实"
    if field == "腰腿":
        return "腰腿比例符合角色年龄、性别和身份，不使用运动员模板"
    if field == "整体体态":
        return f"体现目标国家本地化后{identity}的气质、姿态和当前子资产状态"
    if field == "脸型":
        return "立体椭圆脸，脸部轮廓清晰，下颌线自然"
    if field == "骨相":
        return "眉骨和鼻梁有轻微立体度，颧骨不过分突出，下颌线干净"
    if field == "眉形":
        return "深棕色自然平直眉，眉峰轻微上扬"
    if field == "眼形":
        return "深邃杏仁形眼睛，清晰双眼皮，眼窝较深，外眼角自然平直，不使用丹凤眼或东亚眼型特征"
    if field == "虹膜颜色":
        return "灰蓝色、浅灰绿色或榛褐色虹膜，虹膜纹理清晰，避免纯黑色眼珠"
    if field == "鼻形":
        return "鼻梁较直，鼻头自然，鼻翼宽度适中"
    if field == "唇形":
        return "自然中等厚度唇形，唇线清晰，嘴巴自然闭合"
    if field == "面部毛发":
        if is_female_context(visual):
            return "无明显面部毛发，保持真实自然肤质"
        return "根据角色性别、年龄和身份决定是否有胡茬或鬓角，保持真实自然"
    if field == "妆感":
        return "无明显妆感，皮肤保持真实运动后自然状态"
    if field == "皮肤肤色":
        return "浅小麦色或白皙偏暖肤色，带自然运动后红润"
    if field == "皮肤质感":
        return "真实皮肤纹理，保留与当前剧情状态一致的自然光泽和细节"
    if field == "气色状态":
        return "气色符合角色年龄、身份和当前剧情情绪，不套用运动后状态"
    if field == "发型名称":
        return "符合目标国家本土同年龄、同性别、同身份人物的自然发型"
    if field == "发色":
        return "深棕色自然发色"
    if field == "长度":
        return "短发长度，顶部约5-7厘米，两侧较短"
    if field == "刘海 /分线":
        return "自然前额碎发，无明显分线"
    if field == "发丝质感":
        return "发丝清晰有层次，轻微蓬松，运动后略带湿润感"
    if field == "整体状态":
        return "符合目标国家本土人物日常造型和当前剧情状态，不过度模板化"
    if field in {"主色", "副色", "点缀色", "内衬", "中间层", "外层", "下装", "鞋子", "面料", "纹样", "纹样位置", "结构与廓形", "工艺与重工程度", "穿着状态"}:
        clothing_context = pick_payload_text(child, ["本地化视觉形象描述", "本地化子资产描述", "差异标签"]) or visual
        generic_defaults = {
            "主色": f"根据原片可见服装和{identity}身份确定主色，不套用固定队服颜色",
            "副色": "只保留原片可见或本地化合理的辅助色，不添加无依据配色",
            "点缀色": "如原片有饰边、纽扣、图案或配饰颜色则本地化保留；无明显点缀则写无明显点缀",
            "内衬": "根据原片服装层次判断；不可见时写无明显内衬",
            "中间层": "根据原片服装层次判断；不可见时写无中间层或不明显",
            "外层": f"符合{identity}身份、年龄、性别和当前剧情状态的外层服装，基于原片可见服装类别本地化",
            "下装": "符合原片可见下装类别和目标国家日常穿着逻辑，不套用运动短裤模板",
            "鞋子": "符合角色身份、场景和原片可见鞋型的本地化鞋子，不默认运动鞋模板",
            "面料": "根据原片服装类别推断真实面料质感，如棉、针织、牛仔、制服面料或西装面料",
            "纹样": "仅描述原片可见或身份必要的纹样；无明显纹样则写素色或无明显纹样",
            "纹样位置": "仅描述真实存在的纹样位置；无明显纹样则写无明显纹样位置",
            "结构与廓形": "符合角色年龄、性别、身份和服装类别的自然廓形，不套用运动员模板",
            "工艺与重工程度": "真实服装缝线和材质细节，复杂度与角色身份和服装类别一致",
            "穿着状态": "穿着状态与当前剧情一致，可为整洁、凌乱、疲惫、受伤、雨淋或日常自然褶皱",
        }
        return generic_defaults.get(field, clothing_context)
    return visual


def is_basketball_uniform_context(context: str) -> bool:
    lower = context.lower()
    has_basketball_uniform = any(token in context for token in ["篮球球衣", "篮球队服", "比赛服", "球衣号码", "无袖篮球服", "篮球鞋"]) or any(token in lower for token in ["basketball jersey", "basketball uniform", "basketball shoes"])
    has_role_guard = any(token in context for token in ["篮球运动员", "篮球球员", "篮球队员", "球员", "运动员"]) or any(token in lower for token in ["basketball player", "athlete"])
    excluded = any(token in context for token in ["观众", "路人", "家属", "老人", "母亲", "父亲", "老师", "医生", "护士", "经理", "总裁", "女性", "女主"]) or any(token in lower for token in ["audience", "passerby", "family", "elder", "mother", "father", "teacher", "doctor", "nurse", "manager", "ceo"])
    return has_basketball_uniform and has_role_guard and not excluded


def contains_basketball_clothing_terms(text: str) -> bool:
    lower = text.lower()
    return any(
        token in text
        for token in ["篮球背心", "篮球短裤", "篮球队服", "篮球比赛服", "篮球鞋", "球衣号码", "NORTH STATE", "NCAA", "蓝橙配色", "白色球裤"]
    ) or any(token in lower for token in ["basketball uniform", "basketball jersey", "basketball shorts", "basketball shoes", "north state", "ncaa"])


def localized_ethnicity_text(target_country: str, context: str, gender: str = "") -> str:
    country = f"{target_country}".lower()
    if any(token in f"{gender}{context}" for token in ["男女混合", "群像", "观众", "人群", "多名", "多人"]):
        gender_text = "群像人物"
    else:
        gender_text = "女性" if "女" in gender else "男性" if "男" in gender else "人物"
    if country in {"美国", "us", "usa", "united states", "america"} or "美国" in f"{target_country}":
        if any(token in context for token in ["多元族裔", "群像", "观众", "人群", "多人", "多名"]):
            return f"多元族裔美国{gender_text}，年龄、肤色、发色、体型和穿着具有真实人群差异，符合美国本土公共场景，不复用单一角色模板"
        if any(token in context for token in ["非裔", "African", "黑人"]):
            return f"非裔美国{gender_text}，外貌、肤色、发型和气质符合美国本土同年龄、同身份人群特征，不复用原剧地域面部特征"
        if any(token in context for token in ["拉丁", "Latino", "Hispanic"]):
            return f"拉丁裔美国{gender_text}，外貌、肤色、发型和气质符合美国本土同年龄、同身份人群特征，不复用原剧地域面部特征"
        return f"欧洲裔美国白人{gender_text}，五官骨相、肤色、发色和气质符合美国本土同年龄、同身份人群特征，不呈现东亚、东南亚或混血亚洲人的面部特征"
    if any(token in f"{target_country}" for token in ["英国", "英格兰", "UK"]):
        return f"英国本土白人{gender_text}外貌，符合英国同年龄、同身份人群气质，不呈现东亚或原剧地域面部特征"
    if "日本" in f"{target_country}":
        return "日本本土青年男性外貌，五官、发型、肤色和校园运动气质均转写为日本当代语境"
    if "韩国" in f"{target_country}":
        return "韩国本土青年男性外貌，五官、发型、肤色和校园运动气质均转写为韩国当代语境"
    return f"{target_country}本土常见族裔{gender_text}外貌，五官骨相、肤色、发色、虹膜颜色和发型均匹配目标国家、年龄和身份，不保留原剧地域面部特征"


def role_clothing_text_constraints(payload: dict[str, Any], child: dict[str, Any], target_language: str) -> str:
    context = role_context_text(payload, child)
    if not is_basketball_uniform_context(context):
        return "服装上如有可见文字，必须使用目标语言和目标国家常见排版；不得出现中文汉字、真实品牌名称、受版权保护 logo、水印、平台角标或无关标识。"
    number_match = re.search(r"(?:号码|number|#|号)\s*(\d{1,3})|(\d{1,3})\s*号", context, re.IGNORECASE)
    number = next((group for group in (number_match.groups() if number_match else []) if group), "")
    school_text = "目标语言虚构校名"
    number_text = f"可保留原片可见号码“{number}”" if number else "只有原片清楚可见号码时才写号码，不得默认添加固定数字"
    return (
        f"球衣如有可见文字，使用{school_text}并匹配目标语言排版；{number_text}。"
        "不出现中文汉字，不出现真实品牌名称或受版权保护的品牌 logo。除原片必要的球衣文字和号码外，画面中不得出现任何其他文字、水印、平台角标或标识。"
    )


def localized_role_prompt_field(
    payload: dict[str, Any],
    child: dict[str, Any],
    section: str,
    field: str,
    target_country: str,
    fallbacks: list[str] | None = None,
) -> str:
    country = f"{target_country}".lower()
    is_us = country in {"美国", "us", "usa", "united states", "america"} or "美国" in f"{target_country}"
    context = role_context_text(payload, child)
    if is_us:
        us_fields = {
            "脸型": "符合美国本土同年龄、同性别、同身份人群的真实脸型，脸部轮廓自然清晰，不复用原剧地域脸型",
            "骨相": "符合美国本土同年龄、同性别、同身份人群的骨相结构，眉骨、鼻梁、颧骨和下颌线真实自然",
            "眉形": "符合美国本土同年龄、同性别人物的自然眉形，眉毛密度和眉峰不过度模板化",
            "眼形": "符合美国本土同年龄、同性别人物的真实眼形，眼窝深浅、眼裂形态和眼尾走势自然，不使用丹凤眼或东亚眼型特征",
            "虹膜颜色": "符合目标国家本地人群的自然虹膜颜色，可为蓝色、绿色、榛褐色或棕色，虹膜纹理清晰，避免纯黑色眼珠",
            "鼻形": "符合美国本土同年龄、同性别人物的真实鼻形，鼻梁、鼻翼和鼻尖比例自然",
            "唇形": "符合美国本土同年龄、同性别人物的自然唇形，唇线清晰，不过度妆容化",
            "面部毛发": "根据性别、年龄和身份决定是否有胡茬、鬓角或无明显面部毛发，保持真实自然",
            "皮肤肤色": "符合美国本土对应族裔的真实肤色，带自然血色和皮肤细节，不保留原剧地域肤色模板",
            "发型名称": "符合美国本土同年龄、同性别、同身份人群的日常发型",
            "发色": "符合目标国家本地族裔设定的自然发色，避免无依据保留原剧纯黑色块感",
            "长度": "符合角色年龄、性别、职业和当前状态的自然发长",
            "刘海 /分线": "符合角色年龄、性别、职业和目标国家审美的自然刘海或分线",
            "发丝质感": "真实自然发丝质感，粗细、蓬松度和湿润状态与角色身份和场景一致",
            "整体状态": "符合目标国家本土人物日常造型和当前剧情状态，不过度模板化",
        }
        basketball_fields = {
            "主色": "根据原片篮球比赛服可见颜色转化为目标国家同级别球队配色，不默认白色",
            "副色": "根据原片领口、袖口、裤边和鞋面可见辅助色本地化，不默认藏蓝色",
            "点缀色": "只保留原片可见的号码描边、裤边细线或球鞋局部点缀，不默认橙色",
            "内衬": "无明显内衬，球衣为单层透气运动背心",
            "中间层": "无中间层，保持轻量比赛服结构",
            "外层": "目标国家本地篮球比赛服；如原片可见校名、队名或号码，则转为目标语言虚构文字和原片可见号码，不默认 NORTH STATE 或 23",
            "下装": "目标国家本地篮球短裤，长度、宽松度和侧边条纹根据原片可见信息本地化，不默认印数字",
            "鞋子": "符合原片鞋型和角色身份的专业篮球鞋，不出现真实品牌标识，不默认蓝橙配色",
            "面料": "透气网眼聚酯运动面料，轻微反光，能看到细密织纹",
            "纹样": "仅包含原片可见且复刻必要的虚构队名、原片可见号码和队服边线，无中文汉字，无真实品牌 logo",
            "纹样位置": "根据原片可见位置描述队名、号码和边线；不可见时不添加",
            "结构与廓形": "宽肩无袖篮球背心，直筒及膝球裤，运动员修身但不紧身",
            "工艺与重工程度": "队服缝线清晰，号码贴布边缘干净，面料轻薄，无复杂装饰",
            "穿着状态": "比赛服穿着状态根据原片剧情决定，可为干净、汗湿、脏污、受伤或比赛后褶皱",
        }
        if field in us_fields:
            return us_fields[field]
        if is_basketball_uniform_context(context) and field in basketball_fields:
            return basketball_fields[field]
    return role_prompt_field(payload, child, section, field, fallbacks)


def localized_visual_anchor_text(payload: dict[str, Any], child: dict[str, Any], target_country: str) -> str:
    context = role_context_text(payload, child)
    country = f"{target_country}".lower()
    if (country in {"美国", "us", "usa", "united states", "america"} or "美国" in f"{target_country}") and is_basketball_uniform_context(context):
        return (
            "A target-country basketball player whose uniform colors, number, shoes, body type, age and gender are derived from the source footage. "
            "Use only fictional team or school text when the source clearly shows uniform text; do not default to a fixed school name, fixed number, or fixed gender."
        )
    visual = pick_payload_text(child, ["本地化视觉形象描述", "本地化子资产描述"]) or pick_payload_text(payload, ["本地化核心形象描述"])
    if visual and contains_basketball_clothing_terms(visual) and not is_basketball_uniform_context(context):
        identity = pick_payload_text(payload, ["剧情身份", "稳定人设定位", "剧情作用"]) or "当前角色"
        return f"根据原片可见信息生成{target_country}本地化{identity}，服装、年龄、性别、发型和状态必须跟随原片，不套用运动员或球衣模板。"
    return visual


def build_role_asset_image_prompt(
    payload: dict[str, Any],
    target_country: str = "目标国家",
    target_language: str = "目标语言",
) -> str:
    child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
    localized_visual = localized_visual_anchor_text(payload, child, target_country)
    identity = pick_payload_text(payload, ["剧情身份", "稳定人设定位", "剧情作用"])
    localized_name = pick_payload_text(payload, ["本地化角色名称"]) or pick_payload_text(child, ["本地化子资产名称"])
    context = role_context_text(payload, child)
    return f"""【基础设定】
性别：{role_prompt_field(payload, child, "基础设定", "性别")}
视觉年龄：{role_prompt_field(payload, child, "基础设定", "视觉年龄")}
国家 / 时代：{target_country} / 现代
族裔外貌：{role_prompt_field(payload, child, "基础设定", "族裔外貌") if pick_payload_text(payload, ["族裔外貌"]) else localized_ethnicity_text(target_country, context, role_prompt_field(payload, child, "基础设定", "性别"))}
身份定位：{role_prompt_field(payload, child, "基础设定", "身份定位", ["剧情身份", "稳定人设定位", "剧情作用"])}
角色名称：{localized_name or "未命名本地化角色"}
目标语言：{target_language}
本地化视觉锚点：{localized_visual or "根据目标国家审美生成稳定真人写实角色资产，不复用原剧演员脸、原剧地域特征或原剧服装细节。"}

【身材体态】
头身比：{role_prompt_field(payload, child, "身材体态", "头身比")}
身高体感：{role_prompt_field(payload, child, "身材体态", "身高体感")}
体型：{role_prompt_field(payload, child, "身材体态", "体型", ["本地化视觉形象描述", "本地化子资产描述"])}
肩颈：{role_prompt_field(payload, child, "身材体态", "肩颈")}
腰腿：{role_prompt_field(payload, child, "身材体态", "腰腿")}
整体体态：{role_prompt_field(payload, child, "身材体态", "整体体态", ["本地化视觉形象描述"])}

【脸部特征】
脸型：{localized_role_prompt_field(payload, child, "脸部特征", "脸型", target_country)}
骨相：{localized_role_prompt_field(payload, child, "脸部特征", "骨相", target_country)}
眉形：{localized_role_prompt_field(payload, child, "脸部特征", "眉形", target_country)}
眼形：{localized_role_prompt_field(payload, child, "脸部特征", "眼形", target_country)}
虹膜颜色：{localized_role_prompt_field(payload, child, "脸部特征", "虹膜颜色", target_country)}
鼻形：{localized_role_prompt_field(payload, child, "脸部特征", "鼻形", target_country)}
唇形：{localized_role_prompt_field(payload, child, "脸部特征", "唇形", target_country)}
面部毛发：{localized_role_prompt_field(payload, child, "脸部特征", "面部毛发", target_country)}
妆感：{role_prompt_field(payload, child, "脸部特征", "妆感")}
皮肤肤色：{localized_role_prompt_field(payload, child, "脸部特征", "皮肤肤色", target_country)}
皮肤质感：{role_prompt_field(payload, child, "脸部特征", "皮肤质感")}
气色状态：{role_prompt_field(payload, child, "脸部特征", "气色状态")}

【发型】
发型名称：{localized_role_prompt_field(payload, child, "发型", "发型名称", target_country)}
发色：{localized_role_prompt_field(payload, child, "发型", "发色", target_country)}
长度：{localized_role_prompt_field(payload, child, "发型", "长度", target_country)}
刘海 /分线：{localized_role_prompt_field(payload, child, "发型", "刘海 /分线", target_country, ["刘海", "分线"])}
发丝质感：{localized_role_prompt_field(payload, child, "发型", "发丝质感", target_country)}
整体状态：{localized_role_prompt_field(payload, child, "发型", "整体状态", target_country)}

【服装】
主色：{localized_role_prompt_field(payload, child, "服装", "主色", target_country)}
副色：{localized_role_prompt_field(payload, child, "服装", "副色", target_country)}
点缀色：{localized_role_prompt_field(payload, child, "服装", "点缀色", target_country)}
内衬：{localized_role_prompt_field(payload, child, "服装", "内衬", target_country)}
中间层：{localized_role_prompt_field(payload, child, "服装", "中间层", target_country)}
外层：{localized_role_prompt_field(payload, child, "服装", "外层", target_country)}
下装：{localized_role_prompt_field(payload, child, "服装", "下装", target_country)}
鞋子：{localized_role_prompt_field(payload, child, "服装", "鞋子", target_country)}
面料：{localized_role_prompt_field(payload, child, "服装", "面料", target_country)}
纹样：{localized_role_prompt_field(payload, child, "服装", "纹样", target_country)}
纹样位置：{localized_role_prompt_field(payload, child, "服装", "纹样位置", target_country)}
结构与廓形：{localized_role_prompt_field(payload, child, "服装", "结构与廓形", target_country)}
工艺与重工程度：{localized_role_prompt_field(payload, child, "服装", "工艺与重工程度", target_country)}
穿着状态：{localized_role_prompt_field(payload, child, "服装", "穿着状态", target_country)}

【服装文字约束】
{role_clothing_text_constraints(payload, child, target_language)}

{ROLE_FIXED_CONSTRAINT}"""


def significant_asset_text(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def compact_asset_token_part(value: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", value or "")
    if not parts:
        return ""
    compacted: list[str] = []
    for part in parts:
        if re.fullmatch(r"[A-Za-z0-9]+", part):
            compacted.append(part[:1].upper() + part[1:])
        else:
            compacted.append(part)
    return "".join(compacted)


def remove_duplicate_asset_prefix(base: str, variant: str) -> str:
    base_token = compact_asset_token_part(base)
    variant_token = compact_asset_token_part(variant)
    if base_token and variant_token.lower().startswith(base_token.lower()):
        return variant_token[len(base_token):]
    return variant


def make_display_variable(asset: dict[str, Any]) -> str:
    payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
    child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
    asset_type = str(asset.get("asset_type") or "")
    base = compact_asset_token_part(str(asset.get("localized_name") or asset.get("name") or ""))
    variant_source = pick_payload_text(child, ["差异标签", "本地化子资产名称", "原剧子资产名称"])
    variant = compact_asset_token_part(remove_duplicate_asset_prefix(str(asset.get("localized_name") or asset.get("name") or ""), variant_source))
    if asset_type in {"角色", "场景"} and variant and variant != base:
        token = f"{base}-{variant}" if base else variant
    else:
        token = base
    fallback = str(asset.get("variable") or asset.get("id") or "Asset").replace("{", "").replace("}", "")
    if not token:
        token = compact_asset_token_part(fallback) or "Asset"
    return "@" + token[:64]


def attach_display_variable(asset: dict[str, Any]) -> dict[str, Any]:
    payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
    display_variable = payload.get("展示资产变量") if isinstance(payload.get("展示资产变量"), str) else ""
    if not display_variable or "_" in display_variable or len(display_variable) > 48:
        display_variable = make_display_variable(asset)
    asset["display_variable"] = display_variable
    payload["展示资产变量"] = display_variable
    asset["payload"] = payload
    return asset


def asset_project_main_key(asset: dict[str, Any]) -> str:
    payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
    child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
    asset_type = str(asset.get("asset_type") or "")
    if asset_type == "角色":
        source = " ".join(
            value
            for value in [
                str(asset.get("localized_name") or asset.get("name") or ""),
                pick_payload_text(payload, ["本地化角色名称", "原剧角色名称", "剧情身份", "稳定人设定位"]),
            ]
            if value
        )
    elif asset_type == "场景":
        source = " ".join(
            value
            for value in [
                str(asset.get("localized_name") or asset.get("name") or ""),
                pick_payload_text(payload, ["本地化场景名称", "原剧场景名称", "空间结构描述"]),
            ]
            if value
        )
    else:
        source = " ".join(
            value
            for value in [
                str(asset.get("localized_name") or asset.get("name") or ""),
                pick_payload_text(payload, ["本地化道具名称", "原剧道具名称", "剧情功能"]),
            ]
            if value
        )
    compacted = significant_asset_text(source) or significant_asset_text(str(asset.get("variable") or asset.get("id") or "asset"))
    return f"{asset_type}:{compacted[:96]}"


def asset_project_variant_key(asset: dict[str, Any]) -> str:
    payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
    child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
    variant = significant_asset_text(
        " ".join(
            value
            for value in [
                pick_payload_text(child, ["差异类型", "差异标签", "本地化子资产名称", "原剧子资产名称", "状态差异"]),
                pick_payload_text(payload, ["本地化道具名称", "原剧道具名称"]),
            ]
            if value
        )
    )
    return f"{asset_project_main_key(asset)}:{variant[:96]}"


def annotate_project_asset_keys(asset: dict[str, Any], episode_index: int | None = None) -> dict[str, Any]:
    payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
    payload["项目主资产Key"] = asset_project_main_key(asset)
    payload["项目子资产Key"] = asset_project_variant_key(asset)
    if episode_index is not None:
        appearances = payload.get("出现集数") if isinstance(payload.get("出现集数"), list) else []
        if episode_index not in appearances:
            appearances.append(episode_index)
        payload["出现集数"] = sorted(appearances)
    asset["payload"] = payload
    return asset


def variable_maps(assets: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    old_to_display: dict[str, str] = {}
    by_any_variable: dict[str, dict[str, Any]] = {}
    for asset in assets:
        attach_display_variable(asset)
        old_variable = str(asset.get("variable") or "")
        display_variable = str(asset.get("display_variable") or "")
        if old_variable and display_variable:
            old_to_display[old_variable] = display_variable
        for key in [old_variable, display_variable]:
            if key:
                by_any_variable[key] = asset
    return old_to_display, by_any_variable


def convert_asset_variables(text: str, old_to_display: dict[str, str]) -> str:
    converted = text
    for old, display in sorted(old_to_display.items(), key=lambda item: len(item[0]), reverse=True):
        converted = converted.replace(old, display)
    return converted


def split_variable_text(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    return [item for item in re.split(r"[\s,，、；;]+", text) if item]


def deep_merge_dict(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if value in (None, ""):
            continue
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge_dict(target[key], value)
        else:
            target[key] = value
    return target


def merge_records(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in ["出现记录"]:
        target_records = target.get(key)
        source_records = source.get(key)
        if isinstance(target_records, list) and isinstance(source_records, list):
            target_records.extend(source_records)


def child_is_significant_duplicate(existing_child: dict[str, Any], child: dict[str, Any]) -> bool:
    existing_id = pick_payload_text(existing_child, ["子资产编号", "角色资产变量", "场景资产变量", "道具资产变量"])
    current_id = pick_payload_text(child, ["子资产编号", "角色资产变量", "场景资产变量", "道具资产变量"])
    if existing_id and current_id and existing_id == current_id:
        return True
    key_fields = ["本地化视觉形象描述", "原剧视觉形象描述", "差异标签"]
    existing_key = significant_asset_text(" ".join(pick_payload_text(existing_child, [field]) for field in key_fields))
    current_key = significant_asset_text(" ".join(pick_payload_text(child, [field]) for field in key_fields))
    return bool(existing_key and current_key and existing_key == current_key)


def compact_role_children(role: dict[str, Any]) -> list[dict[str, Any]]:
    raw_children = role.get("子资产") if isinstance(role.get("子资产"), list) else []
    children = [child for child in raw_children if isinstance(child, dict)] or [{}]
    compacted: list[dict[str, Any]] = []
    main_id = role.get("主资产编号") or "角色_001"
    for child in children:
        duplicate = next((item for item in compacted if child_is_significant_duplicate(item, child)), None)
        if duplicate:
            merge_records(duplicate, child)
            continue
        next_index = len(compacted) + 1
        child = dict(child)
        child.setdefault("子资产编号", f"{main_id}_子资产_{next_index:03d}")
        child.setdefault("角色资产变量", f"{{{{{child['子资产编号']}}}}}")
        compacted.append(child)
    return compacted


def build_non_role_asset_image_prompt(
    asset_type: str,
    payload: dict[str, Any],
    target_country: str,
    target_language: str,
) -> str:
    if asset_type == "场景" or "本地化场景名称" in payload:
        name = pick_payload_text(payload, ["本地化场景名称", "本地化子资产名称"]) or "本地化场景"
        visual = pick_payload_text(payload, ["本地化场景描述", "本地化视觉描述", "空间结构描述"])
        child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
        child_visual = pick_payload_text(child, ["本地化视觉描述", "本地化子资产名称", "状态差异"])
        panorama_hint = (
            f"{target_country}本地化{name}场景全景设定。"
            "如果原片能判断室内空间，输出室内全景：完整展示空间布局、入口、窗户、家具/设施、墙面、地面、天花板和关键道具位置。"
            "如果原片能判断室外空间，输出室外全景：完整展示建筑外立面、入口、周边道路/环境、标识位置和空间尺度。"
            "只生成稳定可复用的场景全景资产，不生成正面/侧面/三视图，不生成局部特写，不出现人物。"
        )
        visual = "；".join(part for part in [visual, child_visual, panorama_hint] if part)
        constraint = "真人写实风格，8K超高清，明亮柔和光线，广角全景构图，完整展示空间结构，无人物，无水印，无文字，无三视图，无局部特写，真实摄影质感。"
    else:
        name = pick_payload_text(payload, ["本地化道具名称", "本地化子资产名称"]) or "本地化道具"
        visual = pick_payload_text(payload, ["本地化道具描述", "本地化视觉描述", "道具资产图生成提示词"])
        constraint = "真人写实风格，8K超高清，白色或自然背景，主体道具完整居中展示，无人物手持，无多余物体，无水印，无logo，真实材质细节。"
    return f"""【目标国家本地化资产图】
资产类型：{asset_type}
资产名称：{name}
目标国家：{target_country}
目标语言：{target_language}
本地化视觉锚点：{visual or f"根据{target_country}语境生成稳定可复用的{asset_type}资产图。"}

【固定约束】
{constraint}"""


def infer_asset_image_prompt(
    asset_type: str,
    payload: dict[str, Any],
    target_country: str = "目标国家",
    target_language: str = "目标语言",
) -> str:
    if asset_type == "角色" or "原剧角色名称" in payload or "本地化角色名称" in payload:
        return build_role_asset_image_prompt(payload, target_country, target_language)
    if asset_type == "场景" or "原剧场景名称" in payload or "本地化场景名称" in payload:
        return build_non_role_asset_image_prompt(asset_type, payload, target_country, target_language)
    if asset_type == "道具" or "原剧道具名称" in payload or "本地化道具名称" in payload:
        return build_non_role_asset_image_prompt(asset_type, payload, target_country, target_language)
    return ""


def render_mock_asset_svg(asset: dict[str, Any], prompt: str) -> str:
    title = f"{asset['asset_type']} - {asset['localized_name']}"
    safe_title = (
        title.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    safe_prompt = (
        prompt[:160]
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800" viewBox="0 0 1200 800">
  <rect width="1200" height="800" fill="#f8fafc"/>
  <rect x="80" y="80" width="1040" height="640" rx="28" fill="#ffffff" stroke="#cfd7e6" stroke-width="4"/>
  <circle cx="360" cy="320" r="120" fill="#dbeafe"/>
  <rect x="260" y="450" width="200" height="180" rx="60" fill="#bfdbfe"/>
  <rect x="620" y="170" width="300" height="44" rx="8" fill="#eef2ff"/>
  <rect x="620" y="250" width="420" height="26" rx="6" fill="#e2e8f0"/>
  <rect x="620" y="298" width="360" height="26" rx="6" fill="#e2e8f0"/>
  <rect x="620" y="346" width="390" height="26" rx="6" fill="#e2e8f0"/>
  <text x="620" y="202" font-family="Arial, sans-serif" font-size="30" font-weight="700" fill="#1f2937">{safe_title}</text>
  <text x="620" y="450" font-family="Arial, sans-serif" font-size="20" fill="#475569">Mock asset image placeholder</text>
  <foreignObject x="620" y="480" width="430" height="150">
    <div xmlns="http://www.w3.org/1999/xhtml" style="font-family:Arial,sans-serif;font-size:18px;line-height:1.5;color:#64748b;">{safe_prompt}</div>
  </foreignObject>
</svg>"""


def record_agent_run(
    conn: sqlite3.Connection,
    task_id: str,
    project_id: str,
    episode_id: str | None,
    agent_name: str,
    agent: dict[str, Any],
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO agent_runs
        (id, task_id, project_id, episode_id, agent_name, model_mode, model_name, prompt_file, input_json, output_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("agent_run"),
            task_id,
            project_id,
            episode_id,
            agent_name,
            agent.get("mode", "mock"),
            agent.get("model_name") or agent.get("model", ""),
            agent.get("prompt_file", ""),
            json.dumps(input_payload, ensure_ascii=False),
            json.dumps(output_payload, ensure_ascii=False),
            now_iso(),
        ),
    )


class ProjectCreate(BaseModel):
    title: str = "未命名短剧重制项目"
    targetCountry: str = "美国"
    targetLanguage: str = "英语"
    remakeMode: str = "overseas_localization"
    sourceLanguage: str = ""
    storyboardVisualStyle: str | None = None
    replaceCharacters: bool = False
    replaceScenes: bool = False


class ProjectUpdate(BaseModel):
    title: str | None = None
    remakeMode: str | None = None
    sourceLanguage: str | None = None
    storyboardVisualStyle: str | None = None
    replaceCharacters: bool | None = None
    replaceScenes: bool | None = None
    targetCountry: str | None = None
    targetLanguage: str | None = None


class ProjectDeriveRequest(ProjectCreate):
    pass


class RunTaskRequest(BaseModel):
    taskType: str
    episodeId: str | None = None
    storyboardId: str | None = None
    onlyUnprocessed: bool = False


class StoryboardUpdate(BaseModel):
    title: str | None = None
    timeRange: str | None = None
    prompt: str | None = None
    generatedBody: str | None = None
    payload: dict[str, Any] | None = None


class StoryboardCreate(BaseModel):
    episodeId: str
    title: str | None = None
    timeRange: str | None = None
    prompt: str | None = None
    duplicateFromId: str | None = None


class StoryboardMoveRequest(BaseModel):
    direction: str


class EpisodeSourceUpdate(BaseModel):
    sourceUrl: str


class AssetUpdate(BaseModel):
    name: str | None = None
    localizedName: str | None = None
    status: str | None = None
    payload: dict[str, Any] | None = None


class AssetUploadRequest(BaseModel):
    pass


class AssetImageRequest(BaseModel):
    prompt: str | None = None


class SelectAssetImageRequest(BaseModel):
    imageId: str


class ModelChatTestRequest(BaseModel):
    prompt: str = "请用一句话回复：模型连接正常。"


app = FastAPI(title="视频重制工坊 API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5174", "http://127.0.0.1:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(make_router(connect, lambda: DATA_DIR, lambda: PROJECTS_DIR, get_project_or_404, get_episode_or_first, new_id, data_file_url))


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "db": str(DB_PATH), "time": now_iso()}


@app.get("/api/files/{resource_path:path}")
def read_data_file(resource_path: str, download: bool = False) -> FileResponse:
    target = (DATA_DIR / resource_path).resolve()
    try:
        target.relative_to(DATA_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="File is outside data directory") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target, filename=target.name if download else None)


@app.get("/api/model-status")
def model_status() -> dict[str, Any]:
    try:
        return get_model_status(load_models_config())
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/model-test/chat")
def model_chat_test(payload: ModelChatTestRequest) -> dict[str, Any]:
    config = load_models_config()
    provider = get_model_provider(config)
    text_model = config.get("volcengine", {}).get("textModel", "")
    if config.get("mode") == "mock":
        return {
            "mode": "mock",
            "model": text_model,
            "content": "mock 模式：模型连接测试未调用外部接口。"
        }
    try:
        content = provider.chat_completion(
            model_name=text_model,
            system_prompt="你是一个用于连通性测试的助手。只需简短回答。",
            user_content=payload.prompt,
            temperature=0.1,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"mode": config.get("mode"), "model": text_model, "content": content}


@app.get("/api/projects")
def list_projects() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return {"projects": rows_to_dicts(rows)}


@app.post("/api/projects")
def create_project(payload: ProjectCreate) -> dict[str, Any]:
    if payload.remakeMode not in REMAKE_MODES:
        raise HTTPException(status_code=400, detail="不支持的制作模式")
    style = payload.storyboardVisualStyle or ("cg_3d" if payload.remakeMode == "cg_3d" else "live_action")
    if style not in STORYBOARD_STYLE_TEMPLATES:
        raise HTTPException(status_code=400, detail="不支持的分镜画面类型")
    if payload.remakeMode == "cg_3d":
        style = "cg_3d"
    project_id = new_id("project")
    timestamp = now_iso()
    project_dir = PROJECTS_DIR / project_id
    (project_dir / "source").mkdir(parents=True, exist_ok=True)
    (project_dir / "agents").mkdir(parents=True, exist_ok=True)
    (project_dir / "assets").mkdir(parents=True, exist_ok=True)
    (project_dir / "outputs" / "videos").mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO projects (id, title, target_country, target_language, status, remake_mode, source_language, storyboard_visual_style, replace_characters, replace_scenes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                payload.title,
                payload.targetCountry,
                payload.targetLanguage,
                "已创建",
                payload.remakeMode,
                payload.sourceLanguage,
                style,
                int(payload.replaceCharacters),
                int(payload.replaceScenes),
                timestamp,
                timestamp,
            ),
        )
        project = get_project_or_404(conn, project_id)
    return {"project": project}


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, payload: ProjectUpdate) -> dict[str, Any]:
    with connect() as conn:
        project = get_project_or_404(conn, project_id)
        mode = payload.remakeMode or project.get("remake_mode") or "overseas_localization"
        style = payload.storyboardVisualStyle or project.get("storyboard_visual_style") or "live_action"
        if mode not in REMAKE_MODES or style not in STORYBOARD_STYLE_TEMPLATES:
            raise HTTPException(status_code=400, detail="不支持的制作模式或分镜画面类型")
        target_images = conn.execute("SELECT COUNT(*) FROM asset_images WHERE project_id = ?", (project_id,)).fetchone()[0]
        if mode != project.get("remake_mode") and target_images:
            raise HTTPException(status_code=409, detail="当前项目已有目标资产图，请使用‘另存为新制作项目’切换模式。")
        if conn.execute("SELECT COUNT(*) FROM asset_generation_jobs WHERE project_id=? AND status='running'", (project_id,)).fetchone()[0]:
            raise HTTPException(status_code=409, detail="当前项目仍有资产图生成任务，请等待任务完成后切换模式或另存项目。")
        if mode == "cg_3d":
            style = "cg_3d"
        elif mode == "overseas_localization" and not payload.storyboardVisualStyle:
            style = "live_action"
        conn.execute(
            """UPDATE projects SET title=?, remake_mode=?, source_language=?, storyboard_visual_style=?, replace_characters=?, replace_scenes=?, target_country=?, target_language=?, updated_at=? WHERE id=?""",
            (payload.title.strip() if payload.title and payload.title.strip() else project["title"], mode, payload.sourceLanguage if payload.sourceLanguage is not None else project.get("source_language", ""), style,
             int(payload.replaceCharacters if payload.replaceCharacters is not None else project.get("replace_characters", 0)),
             int(payload.replaceScenes if payload.replaceScenes is not None else project.get("replace_scenes", 0)),
             payload.targetCountry or project["target_country"], payload.targetLanguage or project["target_language"], now_iso(), project_id),
        )
        if (mode != project.get("remake_mode") or style != project.get("storyboard_visual_style")
                or int(payload.replaceCharacters if payload.replaceCharacters is not None else project.get("replace_characters", 0)) != int(project.get("replace_characters", 0))
                or int(payload.replaceScenes if payload.replaceScenes is not None else project.get("replace_scenes", 0)) != int(project.get("replace_scenes", 0))):
            conn.execute("UPDATE storyboards SET status='待更新' WHERE project_id=?", (project_id,))
        updated = get_project_or_404(conn, project_id)
        return {"project": updated}


@app.post("/api/projects/{project_id}/derive")
def derive_project(project_id: str, payload: ProjectDeriveRequest) -> dict[str, Any]:
    if payload.remakeMode not in REMAKE_MODES:
        raise HTTPException(status_code=400, detail="不支持的制作模式")
    style = payload.storyboardVisualStyle or ("cg_3d" if payload.remakeMode == "cg_3d" else "live_action")
    if payload.remakeMode == "cg_3d":
        style = "cg_3d"
    if style not in STORYBOARD_STYLE_TEMPLATES:
        raise HTTPException(status_code=400, detail="不支持的分镜画面类型")
    with connect() as conn:
        source = get_project_or_404(conn, project_id)
        new_project_id = new_id("project")
        timestamp = now_iso()
        title = payload.title if payload.title != "未命名短剧重制项目" else f"{source['title']}（{payload.remakeMode}）"
        conn.execute(
            """INSERT INTO projects (id,title,target_country,target_language,status,remake_mode,source_language,storyboard_visual_style,replace_characters,replace_scenes,parent_project_id,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_project_id,title,payload.targetCountry or source["target_country"],payload.targetLanguage or source["target_language"],"派生项目已创建",
             payload.remakeMode,payload.sourceLanguage or source.get("source_language", ""),style,int(payload.replaceCharacters),int(payload.replaceScenes),project_id,timestamp,timestamp),
        )
        for subdir in ["source", "agents", "assets", "outputs/videos"]:
            (PROJECTS_DIR / new_project_id / subdir).mkdir(parents=True, exist_ok=True)
        episode_map: dict[str, str] = {}
        for episode in rows_to_dicts(conn.execute("SELECT * FROM episodes WHERE project_id=? ORDER BY episode_index", (project_id,)).fetchall()):
            episode_id = new_id("episode")
            episode_map[episode["id"]] = episode_id
            conn.execute("""INSERT INTO episodes (id,project_id,episode_index,filename,file_path,source_url,ark_file_id,ark_file_status,ark_file_error,status,created_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                         (episode_id,new_project_id,episode["episode_index"],episode["filename"],episode["file_path"],episode["source_url"],episode["ark_file_id"],episode["ark_file_status"],episode["ark_file_error"],episode["status"],episode["created_at"]))
        variable_map: dict[str, str] = {}
        old_assets = rows_to_dicts(conn.execute("SELECT * FROM assets WHERE project_id=? ORDER BY created_at", (project_id,)).fetchall())
        for asset in old_assets:
            asset_id = new_id("asset")
            original_payload = json.loads(asset["payload_json"])
            source_description = asset_source_description(asset["asset_type"], original_payload) or "原片描述未留存"
            old_for_variable = dict(asset)
            old_for_variable["payload"] = original_payload
            attach_display_variable(old_for_variable)
            asset_payload = strip_previous_target_fields(original_payload)
            source_key = str(asset_payload.get("源AssetKey") or asset_payload.get("sourceAssetKey") or asset["id"])
            asset_payload["源AssetKey"] = source_key
            asset_payload["原片资产描述"] = source_description
            asset_payload["目标生成描述"] = source_description if payload.remakeMode == "cg_3d" and source_description != "原片描述未留存" else ""
            asset_payload["目标资产来源"] = "沿用原貌"
            new_for_variable = {**asset, "localized_name": asset["name"], "payload": asset_payload}
            attach_display_variable(new_for_variable)
            variable_map[str(old_for_variable.get("display_variable") or "")] = str(new_for_variable.get("display_variable") or "")
            conn.execute("INSERT INTO assets (id,project_id,episode_id,asset_type,name,localized_name,variable,status,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (asset_id,new_project_id,episode_map.get(asset.get("episode_id")),asset["asset_type"],asset["name"],asset["name"],asset["variable"],asset["status"],json.dumps(asset_payload,ensure_ascii=False),asset["created_at"]))
        for storyboard in rows_to_dicts(conn.execute("SELECT * FROM storyboards WHERE project_id=? ORDER BY created_at", (project_id,)).fetchall()):
            storyboard_id = new_id("storyboard")
            sb_payload = strip_previous_target_fields(json.loads(storyboard["payload_json"]))
            source_prompt = sb_payload.get("源分镜文本") or storyboard["prompt"]
            for old_variable, new_variable in variable_map.items():
                if old_variable:
                    source_prompt = source_prompt.replace(old_variable, new_variable)
            sb_payload["源分镜文本"] = source_prompt
            refs = sb_payload.get("引用资产变量") if isinstance(sb_payload.get("引用资产变量"), list) else []
            sb_payload["引用资产变量"] = [variable_map.get(str(ref), str(ref)) for ref in refs]
            sb_payload["资产映射校验"] = {**(sb_payload.get("资产映射校验") if isinstance(sb_payload.get("资产映射校验"), dict) else {}), "派生项目目标资产重映射": True}
            sb_payload["最终提示词快照"] = ""
            sb_payload["待更新原因"] = "新制作项目尚未按当前目标资产与画面类型刷新提示词"
            conn.execute("INSERT INTO storyboards (id,project_id,episode_id,title,time_range,prompt,payload_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (storyboard_id,new_project_id,episode_map.get(storyboard["episode_id"], storyboard["episode_id"]),storyboard["title"],storyboard["time_range"],source_prompt,json.dumps(sb_payload,ensure_ascii=False),"待更新",storyboard["created_at"]))
        return {"project": get_project_or_404(conn, new_project_id), "parentProjectId": project_id, "assetsCopied": conn.execute("SELECT COUNT(*) FROM assets WHERE project_id=?", (new_project_id,)).fetchone()[0], "targetImagesCopied": 0}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    with connect() as conn:
        project = get_project_or_404(conn, project_id)
        episodes = rows_to_dicts(
            conn.execute(
                "SELECT * FROM episodes WHERE project_id = ? ORDER BY episode_index",
                (project_id,),
            ).fetchall()
        )
        return {"project": project, "episodes": enrich_episodes(episodes)}


@app.post("/api/projects/{project_id}/episodes/upload")
async def upload_episodes(project_id: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    with connect() as conn:
        get_project_or_404(conn, project_id)
        project_dir = PROJECTS_DIR / project_id / "source"
        project_dir.mkdir(parents=True, exist_ok=True)
        saved: list[dict[str, Any]] = []
        sorted_files = sorted(
            enumerate(files, start=1),
            key=lambda item: (extract_episode_index(item[1].filename or "", item[0]), item[1].filename or ""),
        )
        for fallback_index, upload in sorted_files:
            filename = Path(upload.filename or f"episode_{fallback_index:03d}.mp4").name
            episode_index = extract_episode_index(filename, fallback_index)
            suffix = Path(filename).suffix or ".mp4"
            stored_name = f"episode_{episode_index:03d}{suffix}"
            target_path = project_dir / stored_name
            with target_path.open("wb") as target:
                shutil.copyfileobj(upload.file, target)
            episode_id = new_id("episode")
            timestamp = now_iso()
            conn.execute(
                """
                INSERT OR REPLACE INTO episodes
                (id, project_id, episode_index, filename, file_path, source_url, ark_file_id, ark_file_status, ark_file_error, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode_id,
                    project_id,
                    episode_index,
                    filename,
                    str(target_path),
                    "",
                    "",
                    "",
                    "",
                    "已上传",
                    timestamp,
                ),
            )
            episode_record = {
                "id": episode_id,
                "project_id": project_id,
                "episode_index": episode_index,
                "filename": filename,
                "file_path": str(target_path),
                "source_url": "",
                "ark_file_id": "",
                "ark_file_status": "",
                "ark_file_error": "",
                "status": "已上传",
                "created_at": timestamp,
            }
            enrich_episode(episode_record)
            ark_result = upload_episode_to_ark(conn, episode_record)
            episode_record["ark_file_id"] = ark_result["file_id"]
            episode_record["ark_file_status"] = ark_result["status"]
            episode_record["ark_file_error"] = ark_result["error"]
            episode_record["status"] = "已上传并同步 Ark" if ark_result["file_id"] else "已上传，Ark 同步失败"
            conn.execute(
                "UPDATE episodes SET status = ? WHERE id = ?",
                (episode_record["status"], episode_id),
            )
            saved.append(episode_record)
        conn.execute(
            "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
            ("已上传", now_iso(), project_id),
        )
    return {"episodes": saved}


@app.get("/api/projects/{project_id}/episodes")
def list_episodes(project_id: str) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT * FROM episodes WHERE project_id = ? ORDER BY episode_index",
            (project_id,),
        ).fetchall()
        return {"episodes": enrich_episodes(rows_to_dicts(rows))}


@app.patch("/api/projects/{project_id}/episodes/{episode_id}/source-url")
def update_episode_source_url(project_id: str, episode_id: str, payload: EpisodeSourceUpdate) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        episode = row_to_dict(
            conn.execute(
                "SELECT * FROM episodes WHERE id = ? AND project_id = ?",
                (episode_id, project_id),
            ).fetchone()
        )
        if not episode:
            raise HTTPException(status_code=404, detail="Episode not found")
        conn.execute(
            "UPDATE episodes SET source_url = ? WHERE id = ? AND project_id = ?",
            (payload.sourceUrl.strip(), episode_id, project_id),
        )
        updated = row_to_dict(
            conn.execute(
                "SELECT * FROM episodes WHERE id = ? AND project_id = ?",
                (episode_id, project_id),
            ).fetchone()
        )
        return {"episode": enrich_episode(updated)}


@app.post("/api/projects/{project_id}/episodes/{episode_id}/sync-ark-file")
def sync_episode_ark_file(project_id: str, episode_id: str) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        episode = row_to_dict(
            conn.execute(
                "SELECT * FROM episodes WHERE id = ? AND project_id = ?",
                (episode_id, project_id),
            ).fetchone()
        )
        if not episode:
            raise HTTPException(status_code=404, detail="Episode not found")
        result = upload_episode_to_ark(conn, episode)
        updated = row_to_dict(
            conn.execute(
                "SELECT * FROM episodes WHERE id = ? AND project_id = ?",
                (episode_id, project_id),
            ).fetchone()
        )
        return {"episode": enrich_episode(updated), "ark": result}


@app.get("/api/projects/{project_id}/assets")
def list_assets(project_id: str) -> dict[str, Any]:
    with connect() as conn:
        project = get_project_or_404(conn, project_id)
        rows = conn.execute(
            """
            SELECT assets.*, episodes.episode_index
            FROM assets
            LEFT JOIN episodes ON episodes.id = assets.episode_id
            WHERE assets.project_id = ?
            ORDER BY assets.created_at, assets.asset_type
            """,
            (project_id,),
        ).fetchall()
        raw_assets = rows_to_dicts(rows)
        merged_assets: dict[str, dict[str, Any]] = {}
        for asset in raw_assets:
            episode_index = asset.pop("episode_index", None)
            asset["payload"] = enrich_asset_payload_urls(json.loads(asset.pop("payload_json")))
            asset["payload"]["最终资产图生成提示词"] = infer_asset_image_prompt(
                asset["asset_type"], asset["payload"], project.get("target_country", "目标国家"), project.get("target_language", "目标语言"),
            ) if project.get("remake_mode") == "overseas_localization" else build_project_asset_prompt(asset["asset_type"], asset["payload"], project)
            asset["payload"].setdefault("原片资产描述", asset_source_description(asset["asset_type"], asset["payload"]) or "原片描述未留存")
            asset["payload"].setdefault("目标生成描述", "")
            selected_image = conn.execute(
                "SELECT id,file_path,payload_json FROM asset_images WHERE project_id=? AND asset_id=? AND is_selected=1 ORDER BY created_at DESC LIMIT 1",
                (project_id, asset["id"]),
            ).fetchone()
            effective = resolve_effective_asset_reference(asset, project, selected_image)
            asset["payload"]["effectiveAssetSource"] = effective["effectiveAssetSource"]
            asset["payload"]["当前有效外观描述"] = effective["description"]
            asset["payload"]["当前有效资产名"] = effective["name"]
            asset["payload"]["当前有效资产就绪"] = effective["ready"]
            attach_display_variable(asset)
            annotate_project_asset_keys(asset, episode_index if isinstance(episode_index, int) else None)
            image_rows = conn.execute(
                "SELECT * FROM asset_images WHERE asset_id = ? ORDER BY created_at DESC",
                (asset["id"],),
            ).fetchall()
            images = rows_to_dicts(image_rows)
            for image in images:
                enrich_asset_image(image)
            asset["images"] = images
            key = str(asset["payload"].get("项目子资产Key") or asset_project_variant_key(asset))
            if key not in merged_assets:
                merged_assets[key] = asset
                continue
            target = merged_assets[key]
            appearances = set(target["payload"].get("出现集数") if isinstance(target["payload"].get("出现集数"), list) else [])
            for item in asset["payload"].get("出现集数", []):
                if isinstance(item, int):
                    appearances.add(item)
            target["payload"]["出现集数"] = sorted(appearances)
            target["payload"]["去重来源资产数"] = int(target["payload"].get("去重来源资产数") or 1) + 1
            if not target.get("images") and asset.get("images"):
                target["images"] = asset["images"]
        assets = list(merged_assets.values())
        return {"assets": assets}


@app.patch("/api/projects/{project_id}/assets/{asset_id}")
def update_asset(project_id: str, asset_id: str, payload: AssetUpdate) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        asset = row_to_dict(
            conn.execute(
                "SELECT * FROM assets WHERE id = ? AND project_id = ?",
                (asset_id, project_id),
            ).fetchone()
        )
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        if conn.execute("SELECT 1 FROM asset_generation_jobs WHERE project_id=? AND asset_id=? AND status='running' LIMIT 1", (project_id, asset_id)).fetchone():
            raise HTTPException(status_code=409, detail="该资产仍在生成中，请完成后再修改目标描述。")
        current_payload = json.loads(asset["payload_json"])
        if payload.payload is not None:
            current_payload.update(payload.payload)
        if payload.payload and "目标生成描述" in payload.payload:
            current_payload["目标资产来源"] = "手动生成"
        conn.execute(
            """
            UPDATE assets
            SET name = ?, localized_name = ?, status = ?, payload_json = ?
            WHERE id = ? AND project_id = ?
            """,
            (
                payload.name or asset["name"],
                payload.localizedName or asset["localized_name"],
                payload.status or asset["status"],
                json.dumps(current_payload, ensure_ascii=False),
                asset_id,
                project_id,
            ),
        )
        if payload.payload and ("目标生成描述" in payload.payload or "目标资产来源" in payload.payload) or payload.localizedName:
            mark_storyboards_asset_stale(conn, project_id, asset, "目标资产描述或名称发生变化")
        return get_asset_response(conn, project_id, asset_id)


def mark_storyboards_asset_stale(conn: sqlite3.Connection, project_id: str, asset: dict[str, Any], reason: str) -> None:
    candidate = dict(asset)
    candidate["payload"] = json.loads(asset["payload_json"]) if isinstance(asset.get("payload_json"), str) else asset.get("payload", {})
    attach_display_variable(candidate)
    keys = {str(asset.get("variable") or ""), str(candidate.get("display_variable") or "")}
    keys.discard("")
    for row in conn.execute("SELECT id,prompt,payload_json FROM storyboards WHERE project_id=?", (project_id,)).fetchall():
        payload = json.loads(row["payload_json"])
        refs = payload.get("引用资产变量") if isinstance(payload.get("引用资产变量"), list) else []
        if not (keys.intersection({str(value) for value in refs}) or any(key in str(row["prompt"]) for key in keys)):
            continue
        payload["待更新原因"] = reason
        conn.execute("UPDATE storyboards SET status='待更新',payload_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), row["id"]))


@app.post("/api/projects/{project_id}/assets/{asset_id}/confirm")
def confirm_asset(project_id: str, asset_id: str) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE assets SET status = ? WHERE id = ? AND project_id = ?",
            ("已确认", asset_id, project_id),
        )
        return get_asset_response(conn, project_id, asset_id)


@app.post("/api/projects/{project_id}/assets/{asset_id}/restore-original")
def restore_original_asset(project_id: str, asset_id: str) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        asset = row_to_dict(conn.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (asset_id, project_id)).fetchone())
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        conn.execute("UPDATE asset_images SET is_selected=0 WHERE project_id=? AND asset_id=?", (project_id, asset_id))
        data = json.loads(asset["payload_json"])
        data["目标资产来源"] = "沿用原貌"
        data["目标生成描述"] = ""
        conn.execute("UPDATE assets SET status='待确认',payload_json=? WHERE id=? AND project_id=?", (json.dumps(data, ensure_ascii=False), asset_id, project_id))
        mark_storyboards_asset_stale(conn, project_id, asset, "目标资产切换为沿用原貌")
        return get_asset_response(conn, project_id, asset_id)


@app.post("/api/projects/{project_id}/assets/{asset_id}/generate-image")
def generate_asset_image(project_id: str, asset_id: str, payload: AssetImageRequest) -> dict[str, Any]:
    with connect() as conn:
        project = get_project_or_404(conn, project_id)
        asset = row_to_dict(
            conn.execute(
                "SELECT * FROM assets WHERE id = ? AND project_id = ?",
                (asset_id, project_id),
            ).fetchone()
        )
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        if project.get("remake_mode") == "asset_replacement" and ((asset["asset_type"] == "角色" and not project.get("replace_characters")) or (asset["asset_type"] == "场景" and not project.get("replace_scenes"))):
            raise HTTPException(status_code=409, detail="该类型替换开关未启用，请在项目设置中开启后再生成。")
        asset_payload = json.loads(asset["payload_json"])
        if payload.prompt is not None:
            asset_payload["目标生成描述"] = payload.prompt
            asset_payload["目标资产来源"] = "手动生成"
        target_description = pick_payload_text(asset_payload, ["目标生成描述"])
        if project.get("remake_mode") == "asset_replacement" and not target_description:
            raise HTTPException(status_code=422, detail="替换模式请先填写目标资产外观描述，或上传目标图后再生成。")
        if project.get("remake_mode") == "cg_3d" and not target_description and not asset_source_description(asset["asset_type"], asset_payload):
            raise HTTPException(status_code=422, detail="原片描述未留存，请先补充目标资产外观描述后再生成。")
        prompt = build_project_asset_prompt(asset["asset_type"], asset_payload, project)
        job_id = new_id("asset_job")
        template_version = "cg-asset-v1" if project.get("remake_mode") == "cg_3d" else "legacy-overseas-v1"
        conn.execute("INSERT INTO asset_generation_jobs (id,project_id,asset_id,remake_mode,template_version,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (job_id,project_id,asset_id,project.get("remake_mode", "overseas_localization"),template_version,"running",now_iso(),now_iso()))
        conn.commit()
        try:
            agent = prepare_agent("assetImageGeneration")
        except ProviderError as exc:
            conn.execute("UPDATE asset_generation_jobs SET status='failed',updated_at=? WHERE id=?", (now_iso(), job_id))
            conn.commit()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            conn.execute("UPDATE asset_generation_jobs SET status='failed',updated_at=? WHERE id=?", (now_iso(), job_id))
            conn.commit()
            raise HTTPException(status_code=500, detail=f"资产生图准备失败：{exc}") from exc
        image_id = new_id("asset_image")
        image_dir = PROJECTS_DIR / project_id / "assets" / asset["asset_type"]
        image_dir.mkdir(parents=True, exist_ok=True)
        if agent.get("mode") == "mock":
            image_path = image_dir / f"{image_id}.svg"
            image_path.write_text(render_mock_asset_svg(asset, prompt), encoding="utf-8")
            image_payload = {
                "mock": True,
                "agent": agent,
                "note": "assetImageGeneration 未启用，使用本地 mock 占位图。",
                "remakeMode": project.get("remake_mode", "overseas_localization"),
                "templateVersion": "cg-asset-v1" if project.get("remake_mode") == "cg_3d" else "legacy-overseas-v1",
                "targetDescription": asset_payload.get("目标生成描述", ""),
                "promptSnapshot": prompt,
            }
        else:
            try:
                config = load_models_config()
                provider = get_model_provider(config)
                image_result = provider.generate_image(
                    model_name=agent.get("model_name", ""),
                    prompt=prompt,
                    size=str(config.get("volcengine", {}).get("imageSize") or "2K"),
                    response_format="url",
                    sequential_image_generation="disabled",
                    watermark=bool(config.get("volcengine", {}).get("imageWatermark", True)),
                    stream=False,
                )
            except ProviderError as exc:
                conn.execute("UPDATE asset_generation_jobs SET status='failed',updated_at=? WHERE id=?", (now_iso(), job_id))
                conn.commit()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                conn.execute("UPDATE asset_generation_jobs SET status='failed',updated_at=? WHERE id=?", (now_iso(), job_id))
                conn.commit()
                raise HTTPException(status_code=500, detail=f"资产生图失败：{exc}") from exc
            mime_type = str(image_result.get("mime_type") or "image/png")
            image_path = image_dir / f"{image_id}{image_extension(mime_type)}"
            image_path.write_bytes(image_result["image_bytes"])
            image_payload = {
                "mock": False,
                "agent": agent,
                "model": image_result.get("model"),
                "source_url": image_result.get("image_url", ""),
                "size": image_result.get("size", "2K"),
                "mime_type": mime_type,
                "raw": image_result.get("raw", {}),
                "remakeMode": project.get("remake_mode", "overseas_localization"),
                "templateVersion": "cg-asset-v1" if project.get("remake_mode") == "cg_3d" else "legacy-overseas-v1",
                "targetDescription": asset_payload.get("目标生成描述", ""),
                "promptSnapshot": prompt,
            }
        current_project = get_project_or_404(conn, project_id)
        if current_project.get("remake_mode", "overseas_localization") != project.get("remake_mode", "overseas_localization"):
            image_path.unlink(missing_ok=True)
            conn.execute("UPDATE asset_generation_jobs SET status='stale',updated_at=? WHERE id=?", (now_iso(), job_id))
            conn.commit()
            raise HTTPException(status_code=409, detail="项目模式已变化，旧模式生成结果已丢弃，请在当前项目重新生成。")
        timestamp = now_iso()
        conn.execute(
            "UPDATE asset_images SET is_selected = 0 WHERE asset_id = ?",
            (asset_id,),
        )
        conn.execute(
            """
            INSERT INTO asset_images
            (id, project_id, asset_id, status, prompt, file_path, is_selected, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                image_id,
                project_id,
                asset_id,
                "成功",
                prompt,
                str(image_path),
                1,
                json.dumps(image_payload, ensure_ascii=False),
                timestamp,
            ),
        )
        conn.execute(
            "UPDATE assets SET status = ? WHERE id = ? AND project_id = ?",
            ("已生成资产图", asset_id, project_id),
        )
        conn.execute("UPDATE assets SET payload_json=? WHERE id=? AND project_id=?", (json.dumps(asset_payload, ensure_ascii=False), asset_id, project_id))
        conn.execute("UPDATE asset_generation_jobs SET status='succeeded',updated_at=? WHERE id=?", (now_iso(), job_id))
        asset["payload_json"] = json.dumps(asset_payload, ensure_ascii=False)
        mark_storyboards_asset_stale(conn, project_id, asset, "目标资产图已更新")
        return get_asset_response(conn, project_id, asset_id)


@app.post("/api/projects/{project_id}/assets/{asset_id}/upload-image")
async def upload_asset_image(project_id: str, asset_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    allowed_types = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    mime_type = (file.content_type or "").lower()
    if mime_type not in allowed_types:
        raise HTTPException(status_code=415, detail="仅支持 PNG、JPEG、WebP 图片")
    content = await file.read(20 * 1024 * 1024 + 1)
    if not content or len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="图片不能为空且不能超过 20 MB")
    signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff", "image/webp": b"RIFF"}
    if not content.startswith(signatures[mime_type]) or (mime_type == "image/webp" and content[8:12] != b"WEBP"):
        raise HTTPException(status_code=415, detail="图片文件内容与格式不匹配")
    try:
        with Image.open(BytesIO(content)) as decoded:
            decoded.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=415, detail="图片无法解码，请重新选择有效的 PNG、JPEG 或 WebP 文件") from exc
    with connect() as conn:
        project = get_project_or_404(conn, project_id)
        asset = row_to_dict(conn.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (asset_id, project_id)).fetchone())
        if not asset:
            raise HTTPException(status_code=404, detail="Asset not found")
        image_id = new_id("asset_image")
        image_dir = PROJECTS_DIR / project_id / "assets" / asset["asset_type"]
        image_dir.mkdir(parents=True, exist_ok=True)
        image_path = image_dir / f"{image_id}{allowed_types[mime_type]}"
        image_path.write_bytes(content)
        image_payload = {"mock": False, "source": "user_upload", "filename": Path(file.filename or "asset").name, "mime_type": mime_type, "remakeMode": project.get("remake_mode", "overseas_localization")}
        timestamp = now_iso()
        conn.execute("UPDATE asset_images SET is_selected=0 WHERE project_id=? AND asset_id=?", (project_id, asset_id))
        conn.execute("INSERT INTO asset_images (id,project_id,asset_id,status,prompt,file_path,is_selected,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (image_id,project_id,asset_id,"成功","用户上传目标资产图",str(image_path),1,json.dumps(image_payload,ensure_ascii=False),timestamp))
        current = json.loads(asset["payload_json"])
        current["目标资产来源"] = "用户上传"
        conn.execute("UPDATE assets SET status='已生成资产图',payload_json=? WHERE id=? AND project_id=?", (json.dumps(current,ensure_ascii=False),asset_id,project_id))
        asset["payload_json"] = json.dumps(current, ensure_ascii=False)
        mark_storyboards_asset_stale(conn, project_id, asset, "目标资产图已上传并采用")
        return get_asset_response(conn, project_id, asset_id)


@app.post("/api/projects/{project_id}/assets/{asset_id}/select-image")
def select_asset_image(project_id: str, asset_id: str, payload: SelectAssetImageRequest) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        image = row_to_dict(
            conn.execute(
                "SELECT * FROM asset_images WHERE id = ? AND asset_id = ? AND project_id = ?",
                (payload.imageId, asset_id, project_id),
            ).fetchone()
        )
        if not image:
            raise HTTPException(status_code=404, detail="Asset image not found")
        conn.execute("UPDATE asset_images SET is_selected = 0 WHERE project_id=? AND asset_id = ?", (project_id, asset_id))
        conn.execute("UPDATE asset_images SET is_selected = 1 WHERE id = ?", (payload.imageId,))
        asset = row_to_dict(conn.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (asset_id, project_id)).fetchone())
        project = get_project_or_404(conn, project_id)
        if project.get("remake_mode") == "asset_replacement":
            image_payload = json.loads(image.get("payload_json") or "{}")
            source = "用户上传" if image_payload.get("source") == "user_upload" else "手动生成"
            asset_payload = json.loads(asset["payload_json"])
            asset_payload["目标资产来源"] = source
            conn.execute("UPDATE assets SET payload_json=? WHERE id=? AND project_id=?", (json.dumps(asset_payload, ensure_ascii=False), asset_id, project_id))
        mark_storyboards_asset_stale(conn, project_id, asset, "当前采用目标图已切换")
        return get_asset_response(conn, project_id, asset_id)


@app.get("/api/projects/{project_id}/storyboards")
def list_storyboards(project_id: str, episode_id: str | None = None) -> dict[str, Any]:
    with connect() as conn:
        project = get_project_or_404(conn, project_id)
        if episode_id:
            rows = conn.execute(
                "SELECT * FROM storyboards WHERE project_id = ? AND episode_id = ? ORDER BY created_at",
                (project_id, episode_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM storyboards WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        storyboards = rows_to_dicts(rows)
        for item in storyboards:
            item["payload"] = json.loads(item.pop("payload_json"))
            apply_storyboard_project_prompt(conn, project, item)
        return {"storyboards": storyboards}


def get_storyboard_response(conn: sqlite3.Connection, project_id: str, storyboard_id: str) -> dict[str, Any]:
    item = row_to_dict(
        conn.execute(
            "SELECT * FROM storyboards WHERE id = ? AND project_id = ?",
            (storyboard_id, project_id),
        ).fetchone()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Storyboard not found")
    item["payload"] = json.loads(item.pop("payload_json"))
    project = get_project_or_404(conn, project_id)
    apply_storyboard_project_prompt(conn, project, item)
    return {"storyboard": item}


def apply_storyboard_project_prompt(conn: sqlite3.Connection, project: dict[str, Any], storyboard: dict[str, Any]) -> None:
    payload = storyboard.get("payload") if isinstance(storyboard.get("payload"), dict) else {}
    style = project.get("storyboard_visual_style") or ("cg_3d" if project.get("remake_mode") == "cg_3d" else "live_action")
    if project.get("remake_mode") == "cg_3d":
        style = "cg_3d"
    constraint = STORYBOARD_STYLE_TEMPLATES.get(style, STORYBOARD_STYLE_TEMPLATES["live_action"])
    stored_source = payload.get("源分镜文本")
    source_text = str(stored_source if isinstance(stored_source, str) and stored_source else storyboard.get("prompt") or "")
    original_source = source_text
    if not (isinstance(stored_source, str) and stored_source):
        # Older records may only have the rendered prompt. Strip only known
        # composer suffixes; preserve all narrative words and layout nouns.
        markers = ("当前项目确认资产：", "保持海外本土化项目", "国内重制：", "分镜画面类型约束：")
        source_lines = original_source.splitlines()
        for index, line in enumerate(source_lines):
            if line.startswith(markers):
                original_source = "\n".join(source_lines[:index]).rstrip()
                break
        else:
            for old_template in STORYBOARD_STYLE_TEMPLATES.values():
                suffix = "\n" + old_template
                if original_source.endswith(suffix):
                    original_source = original_source[:-len(suffix)].rstrip()
                    break
    edited_body = payload.get("分镜生成正文")
    base = str(edited_body if isinstance(edited_body, str) else original_source).strip()
    # An exact legacy style phrase at the beginning is an explicit style field;
    # don't erase similarly named words inside actions, dialogue, or scenery.
    if style == "cg_3d":
        base = re.sub(r"^(?:真人实拍电影质感|照片级真实|真实摄影质感|photorealistic|photographic skin)[，,；;。]?\s*", "", base, flags=re.IGNORECASE)
    else:
        base = re.sub(r"^(?:三维游戏CG过场动画质感|高端三维游戏CG|三维CG渲染|3D render|CG style)[，,；;。]?\s*", "", base, flags=re.IGNORECASE)
    refs = payload.get("引用资产变量") if isinstance(payload.get("引用资产变量"), list) else []
    asset_details: list[str] = []
    target_asset_snapshot: list[dict[str, Any]] = []
    for ref in refs:
        rows = conn.execute("SELECT * FROM assets WHERE project_id=? ORDER BY created_at", (project["id"],)).fetchall()
        matched = None
        for row in rows:
            candidate = dict(row)
            candidate_payload = json.loads(candidate["payload_json"])
            candidate["payload"] = candidate_payload
            attach_display_variable(candidate)
            if str(ref) in {str(candidate.get("variable") or ""), str(candidate.get("display_variable") or ""), str(candidate.get("localized_name") or "")}:
                matched = candidate
                break
        if not matched:
            continue
        asset = matched
        ap = asset["payload"]
        selected_image = conn.execute("SELECT id,file_path,payload_json FROM asset_images WHERE project_id=? AND asset_id=? AND is_selected=1 ORDER BY created_at DESC LIMIT 1", (project["id"], asset["id"])).fetchone()
        effective = resolve_effective_asset_reference(asset, project, selected_image)
        target_description = pick_payload_text(ap, ["目标生成描述"])
        effective_source = effective["effectiveAssetSource"]
        source_image_path = str(ap.get("原资产图路径") or "")
        image_path = effective["imagePath"]
        target_name = effective["name"]
        if effective_source == "target" and project.get("remake_mode") == "asset_replacement" and selected_image and not target_description:
            description = "以当前已确认目标图为外观权威；不沿用原片外观描述"
        elif effective_source == "target" and project.get("remake_mode") == "asset_replacement" and not effective["imageId"]:
            description = target_description or "替换目标尚未设置；缺少已确认目标图，不能视为就绪"
        else:
            description = effective["description"]
        source_key = str(ap.get("源AssetKey") or asset["id"])
        if effective_source == "target" and project.get("remake_mode") == "asset_replacement" and not effective["imageId"]:
            image_status = "；缺少目标图，当前替换资产未就绪"
        elif image_path:
            image_status = "；当前确认资产图已关联" if effective_source == "target" and effective["imageId"] else "；原片资产图已关联"
        elif effective_source == "source":
            image_status = "；缺少原片资产图，当前沿用原貌未就绪"
        else:
            image_status = "；未设置目标图，沿用原貌"
        asset_details.append(f"{ref}={target_name}；目标外观：{description}{image_status}")
        target_asset_snapshot.append({
            "reference": str(ref),
            "sourceAssetKey": source_key,
            "projectAssetId": str(asset["id"]),
            "targetName": str(target_name or ""),
            "targetDescription": str(description or ""),
            "effectiveAssetSource": effective_source,
            "imageId": effective["imageId"],
            "imagePath": image_path,
            "imageSource": effective["imageSource"],
        })
    source_language = project.get("source_language") or "原片语言"
    language_rule = (
        "保持海外本土化项目的目标国家和目标语言规则。"
        if project.get("remake_mode") == "overseas_localization"
        else f"3D画面使用指定目标语言（{project.get('target_language')}），保持3D资产策略，不进行地域本土化。"
        if project.get("remake_mode") == "cg_3d" and project.get("target_language") not in {"沿用原片语言", "原片语言"}
        else f"国内重制：沿用原片语言（{source_language}），不进行未经选择的翻译或地域本土化。"
    )
    addition = "\n".join(part for part in ["当前项目确认资产：" + "；".join(asset_details) if asset_details else "", language_rule, f"分镜画面类型约束：{constraint}"] if part)
    final_prompt = (base + "\n" + addition).strip()
    payload["源分镜文本"] = original_source
    payload["目标资产映射快照"] = target_asset_snapshot
    payload["最终提示词快照"] = final_prompt
    payload["分镜画面类型"] = style
    payload["分镜画面模板版本"] = STORYBOARD_STYLE_TEMPLATE_VERSION
    payload["待更新原因"] = payload.get("待更新原因", "")
    storyboard["payload"] = payload
    storyboard["prompt"] = final_prompt


@app.post("/api/projects/{project_id}/storyboards")
def create_storyboard(project_id: str, payload: StoryboardCreate) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        episode = row_to_dict(
            conn.execute(
                "SELECT * FROM episodes WHERE id = ? AND project_id = ?",
                (payload.episodeId, project_id),
            ).fetchone()
        )
        if not episode:
            raise HTTPException(status_code=404, detail="Episode not found")
        source: dict[str, Any] | None = None
        if payload.duplicateFromId:
            source = row_to_dict(
                conn.execute(
                    "SELECT * FROM storyboards WHERE id = ? AND project_id = ?",
                    (payload.duplicateFromId, project_id),
                ).fetchone()
            )
            if not source:
                raise HTTPException(status_code=404, detail="Source storyboard not found")
        source_payload = json.loads(source["payload_json"]) if source else {}
        title = payload.title or (f"{source['title']} 副本" if source else "新增分镜")
        time_range = payload.timeRange or (source["time_range"] if source else "00:00:00-00:00:08")
        prompt = payload.prompt or (source["prompt"] if source else "请在此输入当前分镜的视频生成提示词。")
        storyboard_payload = dict(source_payload) if isinstance(source_payload, dict) else {}
        storyboard_payload.setdefault("引用资产变量", [])
        storyboard_payload.setdefault("镜头列表", [])
        storyboard_id = new_id("storyboard")
        conn.execute(
            """
            INSERT INTO storyboards
            (id, project_id, episode_id, title, time_range, prompt, payload_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                storyboard_id,
                project_id,
                payload.episodeId,
                title,
                time_range,
                prompt,
                json.dumps(storyboard_payload, ensure_ascii=False),
                "可生成",
                now_iso(),
            ),
        )
        return get_storyboard_response(conn, project_id, storyboard_id)


@app.patch("/api/projects/{project_id}/storyboards/{storyboard_id}")
def update_storyboard(project_id: str, storyboard_id: str, payload: StoryboardUpdate) -> dict[str, Any]:
    with connect() as conn:
        # Serialize blur saves and asset-reference toggles so concurrent PATCH
        # requests merge into the latest payload instead of dropping a field.
        conn.execute("BEGIN IMMEDIATE")
        get_project_or_404(conn, project_id)
        current = row_to_dict(
            conn.execute(
                "SELECT * FROM storyboards WHERE id = ? AND project_id = ?",
                (storyboard_id, project_id),
            ).fetchone()
        )
        if not current:
            raise HTTPException(status_code=404, detail="Storyboard not found")
        current_payload = json.loads(current["payload_json"])
        if payload.payload:
            deep_merge_dict(current_payload, payload.payload)
        if payload.generatedBody is not None:
            current_payload["分镜生成正文"] = payload.generatedBody
        elif payload.prompt is not None:
            # Keep the legacy API contract for older clients; the workspace uses
            # generatedBody so editing never mutates the reverse-analysis source.
            current_payload["源分镜文本"] = payload.prompt
        conn.execute(
            """
            UPDATE storyboards
            SET title = ?, time_range = ?, prompt = ?, payload_json = ?
            WHERE id = ? AND project_id = ?
            """,
            (
                payload.title or current["title"],
                payload.timeRange or current["time_range"],
                payload.prompt if payload.prompt is not None else current["prompt"],
                json.dumps(current_payload, ensure_ascii=False),
                storyboard_id,
                project_id,
            ),
        )
        conn.execute("UPDATE storyboards SET status='待更新' WHERE id=? AND project_id=?", (storyboard_id, project_id))
        response = get_storyboard_response(conn, project_id, storyboard_id)
        conn.execute("UPDATE storyboards SET prompt=?,payload_json=? WHERE id=? AND project_id=?", (response["storyboard"]["prompt"], json.dumps(response["storyboard"]["payload"], ensure_ascii=False), storyboard_id, project_id))
        return response


@app.delete("/api/projects/{project_id}/storyboards/{storyboard_id}")
def delete_storyboard(project_id: str, storyboard_id: str) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        existing = row_to_dict(
            conn.execute(
                "SELECT * FROM storyboards WHERE id = ? AND project_id = ?",
                (storyboard_id, project_id),
            ).fetchone()
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Storyboard not found")
        conn.execute("DELETE FROM video_generations WHERE storyboard_id = ? AND project_id = ?", (storyboard_id, project_id))
        conn.execute("DELETE FROM storyboards WHERE id = ? AND project_id = ?", (storyboard_id, project_id))
        return {"ok": True, "deletedId": storyboard_id}


@app.post("/api/projects/{project_id}/storyboards/{storyboard_id}/move")
def move_storyboard(project_id: str, storyboard_id: str, payload: StoryboardMoveRequest) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        current = row_to_dict(
            conn.execute(
                "SELECT * FROM storyboards WHERE id = ? AND project_id = ?",
                (storyboard_id, project_id),
            ).fetchone()
        )
        if not current:
            raise HTTPException(status_code=404, detail="Storyboard not found")
        if payload.direction == "up":
            neighbor = row_to_dict(
                conn.execute(
                    """
                    SELECT * FROM storyboards
                    WHERE project_id = ? AND episode_id = ? AND created_at < ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (project_id, current["episode_id"], current["created_at"]),
                ).fetchone()
            )
        elif payload.direction == "down":
            neighbor = row_to_dict(
                conn.execute(
                    """
                    SELECT * FROM storyboards
                    WHERE project_id = ? AND episode_id = ? AND created_at > ?
                    ORDER BY created_at ASC LIMIT 1
                    """,
                    (project_id, current["episode_id"], current["created_at"]),
                ).fetchone()
            )
        else:
            raise HTTPException(status_code=400, detail="direction must be up or down")
        if neighbor:
            conn.execute("UPDATE storyboards SET created_at = ? WHERE id = ?", (neighbor["created_at"], current["id"]))
            conn.execute("UPDATE storyboards SET created_at = ? WHERE id = ?", (current["created_at"], neighbor["id"]))
        return get_storyboard_response(conn, project_id, storyboard_id)


@app.get("/api/projects/{project_id}/video-generations")
def list_video_generations(project_id: str) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT * FROM video_generations WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        generations = rows_to_dicts(rows)
        for item in generations:
            item["payload"] = json.loads(item.pop("payload_json"))
        return {"videoGenerations": generations}


@app.post("/api/projects/{project_id}/tasks/run")
def run_task(project_id: str, payload: RunTaskRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    with connect() as conn:
        project = get_project_or_404(conn, project_id)
        if payload.taskType not in {"asset_extraction", "storyboard_generation", "video_generation", "series_analysis"}:
            raise HTTPException(status_code=400, detail="Unsupported task type")
        if payload.taskType == "series_analysis":
            episode_id = None
        else:
            episode_id = payload.episodeId
        task = create_task_with_status(
            conn,
            project_id,
            episode_id,
            payload.taskType,
            "运行中",
            "任务已开始，后台执行中",
            {
                "taskType": payload.taskType,
                "episodeId": payload.episodeId,
                "storyboardId": payload.storyboardId,
                "onlyUnprocessed": payload.onlyUnprocessed,
            },
        )
        background_tasks.add_task(
            execute_task_background,
            task["id"],
            project_id,
            payload.taskType,
            episode_id,
            payload.storyboardId,
        )
        return {"task": task}


@app.get("/api/projects/{project_id}/tasks")
def list_tasks(project_id: str) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        tasks = rows_to_dicts(rows)
        for task in tasks:
            task["result"] = json.loads(task.pop("result_json"))
        return {"tasks": tasks}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    with connect() as conn:
        task = row_to_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        task["result"] = json.loads(task.pop("result_json"))
        runs = rows_to_dicts(
            conn.execute(
                "SELECT * FROM agent_runs WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        )
        for run in runs:
            run["input"] = json.loads(run.pop("input_json"))
            run["output"] = json.loads(run.pop("output_json"))
        return {"task": task, "agentRuns": runs}


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str) -> dict[str, Any]:
    with connect() as conn:
        task = row_to_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        project = get_project_or_404(conn, task["project_id"])
        if task["task_type"] == "asset_extraction":
            retried = run_asset_extraction(conn, project, task["episode_id"])
        elif task["task_type"] == "storyboard_generation":
            retried = run_storyboard_generation(conn, project, task["episode_id"])
        elif task["task_type"] == "video_generation":
            retried = run_video_generation(conn, project, None)
        else:
            raise HTTPException(status_code=400, detail="Unsupported task type")
        return {"task": retried}


def create_task(
    conn: sqlite3.Connection,
    project_id: str,
    episode_id: str | None,
    task_type: str,
    message: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    task_id = new_id("task")
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO tasks (id, project_id, episode_id, task_type, status, message, result_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, project_id, episode_id, task_type, "成功", message, json.dumps(result, ensure_ascii=False), timestamp, timestamp),
    )
    return {
        "id": task_id,
        "project_id": project_id,
        "episode_id": episode_id,
        "task_type": task_type,
        "status": "成功",
        "message": message,
        "result": result,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def create_task_with_status(
    conn: sqlite3.Connection,
    project_id: str,
    episode_id: str | None,
    task_type: str,
    status: str,
    message: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    task_id = new_id("task")
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO tasks (id, project_id, episode_id, task_type, status, message, result_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, project_id, episode_id, task_type, status, message, json.dumps(result, ensure_ascii=False), timestamp, timestamp),
    )
    return {
        "id": task_id,
        "project_id": project_id,
        "episode_id": episode_id,
        "task_type": task_type,
        "status": status,
        "message": message,
        "result": result,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def update_task_status(
    conn: sqlite3.Connection,
    task_id: str,
    status: str,
    message: str,
    result: dict[str, Any],
) -> None:
    conn.execute(
        """
        UPDATE tasks
        SET status = ?, message = ?, result_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, message, json.dumps(result, ensure_ascii=False), now_iso(), task_id),
    )


def update_task_stream_progress(conn: sqlite3.Connection, task_id: str, stage: str, chars: int) -> None:
    task_row = conn.execute("SELECT result_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task_row:
        return
    progress_result = json.loads(task_row["result_json"] or "{}")
    phase = "模型正在返回文本" if stage == "generating" else "正在解析模型返回" if stage == "postprocessing" else "模型请求失败，正在收口" if stage == "failed" else "模型处理中，等待返回文本"
    now = now_iso()
    progress_result["实时进度"] = {"阶段": stage, "已接收字符": chars, "最后事件时间": now}
    conn.execute(
        "UPDATE tasks SET message = ?, result_json = ?, updated_at = ? WHERE id = ?",
        (f"{phase}（已收到 {chars} 字符）" if chars else phase, json.dumps(progress_result, ensure_ascii=False), now, task_id),
    )
    conn.commit()


def execute_task_background(
    task_id: str,
    project_id: str,
    task_type: str,
    episode_id: str | None,
    storyboard_id: str | None,
) -> None:
    with connect() as conn:
        try:
            project = get_project_or_404(conn, project_id)
            if task_type == "asset_extraction":
                completed = run_asset_extraction(conn, project, episode_id, task_id)
            elif task_type == "storyboard_generation":
                completed = run_storyboard_generation(conn, project, episode_id)
            elif task_type == "video_generation":
                completed = run_video_generation(conn, project, storyboard_id)
            elif task_type == "series_analysis":
                completed = run_series_analysis(conn, project, task_id)
            else:
                raise RuntimeError(f"Unsupported task type: {task_type}")
            update_task_status(conn, task_id, completed.get("status", "成功"), completed["message"], completed["result"])
            conn.commit()
        except Exception as exc:
            update_task_status(
                conn,
                task_id,
                "失败",
                str(exc),
                {
                    "error": str(exc),
                    "taskType": task_type,
                    "episodeId": episode_id,
                    "storyboardId": storyboard_id,
                },
            )
            conn.commit()


def insert_task(
    conn: sqlite3.Connection,
    project_id: str,
    episode_id: str | None,
    task_type: str,
    message: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    return create_task(conn, project_id, episode_id, task_type, message, result)


def build_mock_assets() -> list[dict[str, Any]]:
    return [
        {
            "asset_type": "角色",
            "name": "张三",
            "localized_name": "John Carter",
            "variable": "{{角色_001_子资产_001}}",
            "payload": {
                "主资产编号": "角色_001",
                "子资产编号": "角色_001_子资产_001",
                "剧情身份": "男主，久别重逢的前任恋人",
                "本地化核心形象描述": "三十岁左右的美国男性，都市精英气质，克制但有压迫感。",
                "角色资产图生成提示词": "三十岁左右美国男性，都市精英气质，深色西装，克制冷峻。"
            },
        },
        {
            "asset_type": "场景",
            "name": "办公大楼",
            "localized_name": "Downtown Office Tower",
            "variable": "{{场景_001_子资产_001}}",
            "payload": {
                "主资产编号": "场景_001",
                "子资产编号": "场景_001_子资产_001",
                "空间结构描述": "现代办公楼入口，玻璃幕墙，雨夜街道。",
                "场景资产图生成提示词": "美国都市中心现代办公大楼入口，玻璃幕墙，雨夜，真实摄影质感。"
            },
        },
        {
            "asset_type": "道具",
            "name": "残破的笔记",
            "localized_name": "Torn Notebook",
            "variable": "{{道具_001}}",
            "payload": {
                "道具编号": "道具_001",
                "剧情功能": "承载旧日秘密的关键证物。",
                "道具资产图生成提示词": "一本边角磨损的英文笔记本，纸张泛黄，真实道具摄影。"
            },
        },
    ]


def normalize_assets_from_agent(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    asset_root = parsed.get("资产库")
    if not isinstance(asset_root, dict):
        return []
    normalized: list[dict[str, Any]] = []

    for role in asset_root.get("角色资产") or []:
        if not isinstance(role, dict):
            continue
        children = compact_role_children(role)
        main_id = role.get("主资产编号") or f"角色_{len([item for item in normalized if item.get('asset_type') == '角色']) + 1:03d}"
        for index, child in enumerate(children, start=1):
            if not isinstance(child, dict):
                child = {}
            child_id = child.get("子资产编号") or f"{main_id}_子资产_{index:03d}"
            variable = child.get("角色资产变量") or f"{{{{{child_id}}}}}"
            payload = dict(role)
            payload["子资产"] = children
            payload["当前子资产"] = child
            payload["子资产编号"] = child_id
            payload["角色资产图生成提示词"] = child.get("角色资产图生成提示词", "") or role.get("角色资产图生成提示词", "")
            asset = {
                "asset_type": "角色",
                "name": role.get("原剧角色名称") or child.get("原剧子资产名称") or role.get("主资产编号") or "未命名角色",
                "localized_name": role.get("本地化角色名称") or child.get("本地化子资产名称") or "Unnamed Character",
                "variable": variable,
                "payload": payload,
            }
            normalized.append(attach_display_variable(asset))

    for scene in asset_root.get("场景资产") or []:
        if not isinstance(scene, dict):
            continue
        children = scene.get("子资产") if isinstance(scene.get("子资产"), list) else []
        if not children:
            children = [{}]
        for index, child in enumerate(children, start=1):
            if not isinstance(child, dict):
                child = {}
            variable = child.get("场景资产变量") or f"{{{{{scene.get('主资产编号') or f'场景_{index:03d}'}_子资产_{index:03d}}}}}"
            payload = dict(scene)
            payload["当前子资产"] = child
            payload["子资产编号"] = child.get("子资产编号", "")
            payload["场景资产图生成提示词"] = child.get("场景资产图生成提示词", "")
            asset = {
                "asset_type": "场景",
                "name": scene.get("原剧场景名称") or child.get("原剧子资产名称") or scene.get("主资产编号") or "未命名场景",
                "localized_name": scene.get("本地化场景名称") or child.get("本地化子资产名称") or "Unnamed Scene",
                "variable": variable,
                "payload": payload,
            }
            normalized.append(attach_display_variable(asset))

    for prop in asset_root.get("道具资产") or []:
        if not isinstance(prop, dict):
            continue
        variable = prop.get("道具资产变量") or f"{{{{{prop.get('道具编号') or '道具_001'}}}}}"
        asset = {
            "asset_type": "道具",
            "name": prop.get("原剧道具名称") or prop.get("道具编号") or "未命名道具",
            "localized_name": prop.get("本地化道具名称") or "Unnamed Prop",
            "variable": variable,
            "payload": prop,
        }
        normalized.append(attach_display_variable(asset))

    return [asset for asset in normalized if asset.get("variable")]


def insert_assets(
    conn: sqlite3.Connection,
    project_id: str,
    episode_id: str,
    assets: list[dict[str, Any]],
    status: str = "待确认",
) -> None:
    timestamp = now_iso()
    for asset in assets:
        conn.execute(
            """
            INSERT INTO assets
            (id, project_id, episode_id, asset_type, name, localized_name, variable, status, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("asset"),
                project_id,
                episode_id,
                asset["asset_type"],
                asset["name"],
                asset["localized_name"],
                asset["variable"],
                status,
                json.dumps(asset["payload"], ensure_ascii=False),
                timestamp,
            ),
        )


def normalize_asset_occurrence_index(raw_index: Any, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_to_display, by_variable = variable_maps(assets)
    normalized: list[dict[str, Any]] = []
    source_items = raw_index if isinstance(raw_index, list) and raw_index else []
    if not source_items:
        return []
    for index, segment in enumerate(source_items, start=1):
        if not isinstance(segment, dict):
            continue
        raw_assets = segment.get("出现资产") if isinstance(segment.get("出现资产"), list) else split_variable_text(segment.get("出现资产"))
        raw_variables = segment.get("出现资产变量") if isinstance(segment.get("出现资产变量"), list) else split_variable_text(segment.get("出现资产变量"))
        raw_assets.extend(raw_variables)
        segment_assets: list[dict[str, Any]] = []
        for raw_asset in raw_assets:
            raw_variable = raw_asset.get("资产变量") if isinstance(raw_asset, dict) else raw_asset
            if not isinstance(raw_variable, str) or not raw_variable.strip():
                continue
            for variable in split_variable_text(raw_variable):
                matched = by_variable.get(variable) or by_variable.get(old_to_display.get(variable, ""))
                display_variable = old_to_display.get(variable, variable if variable.startswith("@") else "")
                segment_assets.append(
                    {
                        "资产类型": (raw_asset.get("资产类型", "") if isinstance(raw_asset, dict) else matched.get("asset_type", "") if matched else ""),
                        "资产变量": matched.get("variable", variable) if matched else variable,
                        "展示资产变量": matched.get("display_variable", display_variable) if matched else display_variable,
                        "本地化资产名称": (raw_asset.get("本地化资产名称", "") if isinstance(raw_asset, dict) else matched.get("localized_name", "") if matched else ""),
                        "片段剧情作用": raw_asset.get("片段剧情作用", "") if isinstance(raw_asset, dict) else "",
                    }
                )
        deduped_assets: list[dict[str, Any]] = []
        seen_variables: set[str] = set()
        for asset_item in segment_assets:
            key = asset_item.get("展示资产变量") or asset_item.get("资产变量")
            if key in seen_variables:
                continue
            seen_variables.add(key)
            deduped_assets.append(asset_item)
        normalized.append(
            {
                "片段编号": segment.get("片段编号") or f"片段_{index:03d}",
                "开始时间": segment.get("开始时间", ""),
                "结束时间": segment.get("结束时间", ""),
                "片段剧情简述": segment.get("片段剧情简述", ""),
                "出现资产": deduped_assets,
                "出现资产变量": [item["展示资产变量"] for item in deduped_assets if item.get("展示资产变量")],
            }
        )
    return normalized


def apply_capture_times_from_index(assets: list[dict[str, Any]], asset_occurrence_index: list[dict[str, Any]]) -> None:
    _, by_variable = variable_maps(assets)
    asset_times: dict[str, list[float]] = {}
    asset_reasons: dict[str, str] = {}
    for segment in asset_occurrence_index:
        if not isinstance(segment, dict):
            continue
        start, end = parse_time_range_seconds(segment.get("开始时间"), segment.get("结束时间"))
        midpoint = start + max(0.0, end - start) / 2
        segment_times = [midpoint, start + 0.5 if end > start else start, max(start, end - 0.5)]
        reason = str(segment.get("片段剧情简述") or "")
        for item in segment.get("出现资产") or []:
            if not isinstance(item, dict):
                continue
            for variable in [item.get("资产变量"), item.get("展示资产变量")]:
                matched = by_variable.get(str(variable or ""))
                if not matched:
                    continue
                key = str(matched.get("variable") or "")
                asset_times.setdefault(key, [])
                for seconds in segment_times:
                    if round(seconds, 2) not in [round(existing, 2) for existing in asset_times[key]]:
                        asset_times[key].append(round(seconds, 2))
                if reason and key not in asset_reasons:
                    asset_reasons[key] = reason
    for asset in assets:
        key = str(asset.get("variable") or "")
        times = asset_times.get(key, [])
        if not times:
            continue
        payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
        child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
        target = child if isinstance(child, dict) else payload
        if not target.get("原剧截图时间点候选"):
            target["原剧截图时间点候选"] = [format_timecode(seconds) for seconds in times[:3]]
        if not target.get("原剧截图时间点"):
            target["原剧截图时间点"] = format_timecode(times[0])
        if not target.get("截图对象描述"):
            target["截图对象描述"] = " / ".join(
                item for item in [str(asset.get("name") or ""), str(asset.get("localized_name") or ""), str(asset.get("display_variable") or "")] if item
            )
        if not target.get("截图理由"):
            target["截图理由"] = asset_reasons.get(key, "资产出现片段索引中首次清晰出现的时间点")


def asset_detail_completion_input(asset: dict[str, Any]) -> dict[str, Any]:
    payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
    child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
    return {
        "资产类型": asset.get("asset_type", ""),
        "原剧资产名称": asset.get("name", ""),
        "本地化资产名称": asset.get("localized_name", ""),
        "资产变量": asset.get("variable", ""),
        "展示资产变量": asset.get("display_variable", ""),
        "主资产编号": payload.get("主资产编号") or payload.get("道具编号", ""),
        "子资产编号": payload.get("子资产编号") or child.get("子资产编号", ""),
        "剧情身份": payload.get("剧情身份", ""),
        "剧情作用": payload.get("剧情作用") or payload.get("剧情功能", ""),
        "原剧核心形象描述": payload.get("原剧核心形象描述") or payload.get("原剧场景描述") or payload.get("原剧道具描述", ""),
        "本地化核心形象描述": payload.get("本地化核心形象描述") or payload.get("本地化场景描述") or payload.get("本地化道具描述", ""),
        "当前子资产": {
            "原剧子资产名称": child.get("原剧子资产名称", ""),
            "本地化子资产名称": child.get("本地化子资产名称", ""),
            "差异类型": child.get("差异类型") or child.get("状态差异", ""),
            "差异标签": child.get("差异标签", ""),
            "原剧视觉形象描述": child.get("原剧视觉形象描述") or child.get("原剧视觉描述", ""),
            "本地化视觉形象描述": child.get("本地化视觉形象描述") or child.get("本地化视觉描述", ""),
            "原剧截图时间点候选": child.get("原剧截图时间点候选") or payload.get("原剧截图时间点候选", []),
            "截图对象描述": child.get("截图对象描述") or payload.get("截图对象描述", ""),
            "截图理由": child.get("截图理由") or payload.get("截图理由", ""),
        },
    }


def asset_detail_missing_fields(asset: dict[str, Any]) -> list[str]:
    payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
    child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
    source_description = str(
        payload.get("原剧核心形象描述") or payload.get("原剧场景描述") or payload.get("原剧道具描述")
        or child.get("原剧视觉形象描述") or child.get("原剧视觉描述") or ""
    ).strip()
    if not source_description:
        return []
    if asset.get("asset_type") == "角色":
        missing = []
        if not str(child.get("本地化视觉形象描述") or "").strip():
            missing.append("本地化视觉形象描述")
        if not (child.get("角色资产图结构化描述") or payload.get("角色资产图结构化描述")):
            missing.append("角色资产图结构化描述")
        if not str(payload.get("角色资产图生成提示词") or child.get("角色资产图生成提示词") or "").strip():
            missing.append("角色资产图生成提示词")
        return missing
    if asset.get("asset_type") == "场景":
        return [field for field, value in (("本地化场景描述", payload.get("本地化场景描述")), ("场景资产图生成提示词", payload.get("场景资产图生成提示词"))) if not str(value or "").strip()]
    if asset.get("asset_type") == "道具":
        return [field for field, value in (("本地化道具描述", payload.get("本地化道具描述")), ("道具资产图生成提示词", payload.get("道具资产图生成提示词"))) if not str(value or "").strip()]
    return []


def source_file_sha256(file_path: str) -> str:
    path = Path(file_path)
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_source_cache_key(file_sha256: str, agent: dict[str, Any], project: dict[str, Any], history_assets: list[dict[str, Any]]) -> str:
    model_config = load_models_config()
    provider_config = model_config.get("volcengine", {})
    configuration = {
        "source_sha256": file_sha256,
        "model_mode": agent.get("mode", ""),
        "model": agent.get("model_name") or agent.get("model", ""),
        "temperature": agent.get("temperature"),
        "output_format": agent.get("output_format", "json"),
        "max_output_tokens": provider_config.get("maxOutputTokens"),
        "thinking": provider_config.get("thinking"),
        "prompt_file": agent.get("prompt_file", ""),
        "prompt": agent.get("prompt", ""),
        "extraction_prompt": ASSET_EXTRACTION_COMPACT_PROMPT,
        "detail_prompt": ASSET_DETAIL_COMPLETION_PROMPT,
        "pipeline_version": "asset-source-v3",
        "remake_mode": project.get("remake_mode", "overseas_localization"),
        "target_country": project.get("target_country", ""),
        "target_language": project.get("target_language", ""),
        "source_language": project.get("source_language", ""),
        "storyboard_visual_style": project.get("storyboard_visual_style", "live_action"),
        "replace_characters": project.get("replace_characters", False),
        "replace_scenes": project.get("replace_scenes", False),
        "history_assets": history_assets,
    }
    encoded = json.dumps(configuration, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def strip_asset_image_references(value: Any) -> Any:
    if isinstance(value, list):
        return [strip_asset_image_references(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).lower().replace("_", "").replace("-", "")
        is_image_reference = ("path" in normalized or "url" in normalized or "路径" in normalized) and any(token in normalized for token in ["image", "asset", "screenshot", "frame", "图", "截图"])
        if is_image_reference or key in {"原资产候选截图", "原资产截图候选", "目标资产图"}:
            continue
        cleaned[key] = strip_asset_image_references(item)
    return cleaned


def find_cached_asset_source(conn: sqlite3.Connection, cache_key: str) -> dict[str, Any] | None:
    rows = conn.execute("SELECT id, result_json FROM tasks WHERE task_type = ? AND status = ? ORDER BY created_at DESC", ("asset_extraction", "成功")).fetchall()
    for row in rows:
        try:
            result = json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if result.get("资产源缓存键") == cache_key and isinstance(result.get("资产源事实快照"), list):
            result["_source_task_id"] = row["id"]
            return result
    return None


def merge_asset_detail_completion(
    assets: list[dict[str, Any]],
    completion: dict[str, Any],
    target_country: str = "目标国家",
    target_language: str = "目标语言",
) -> int:
    _, by_variable = variable_maps(assets)
    items = completion.get("资产补全") if isinstance(completion.get("资产补全"), list) else []
    merged = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        variable = str(item.get("资产变量") or item.get("展示资产变量") or "")
        asset = by_variable.get(variable)
        if not asset:
            continue
        payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
        child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
        payload_patch = item.get("payload_patch") if isinstance(item.get("payload_patch"), dict) else {}
        child_patch = item.get("current_child_patch") if isinstance(item.get("current_child_patch"), dict) else {}
        review_notes = item.get("needsReview") if isinstance(item.get("needsReview"), list) else []
        missing_fields = set(asset_detail_missing_fields(asset))
        payload_patch = {
            key: value for key, value in payload_patch.items()
            if key in missing_fields and key in {"本地化场景描述", "场景资产图生成提示词", "本地化道具描述", "道具资产图生成提示词"}
        }
        child_allowed = {
            "角色": {"本地化视觉形象描述": "本地化视觉形象描述", "角色资产图结构化描述": "角色资产图结构化描述", "角色资产图生成提示词": "角色资产图生成提示词"},
        }.get(asset.get("asset_type"), {})
        child_patch = {key: value for key, value in child_patch.items() if child_allowed.get(key) in missing_fields}
        for protected_key in [
            "资产变量",
            "展示资产变量",
            "角色资产变量",
            "场景资产变量",
            "道具资产变量",
            "主资产编号",
            "子资产编号",
            "道具编号",
            "子资产",
            "当前子资产",
        ]:
            payload_patch.pop(protected_key, None)
            child_patch.pop(protected_key, None)
        if not payload_patch and not child_patch and not review_notes:
            continue
        if payload_patch:
            deep_merge_dict(payload, payload_patch)
        if review_notes:
            existing_review = payload.get("needsReview") if isinstance(payload.get("needsReview"), list) else []
            payload["needsReview"] = list(dict.fromkeys([*existing_review, *[str(note) for note in review_notes if str(note).strip()]]))
        if child_patch and isinstance(child, dict):
            deep_merge_dict(child, child_patch)
            for detail_key in [
                "角色资产图结构化描述",
                "角色资产图生成提示词",
                "场景资产图结构化描述",
                "场景资产图生成提示词",
                "道具资产图结构化描述",
                "道具资产图生成提示词",
            ]:
                if detail_key in child:
                    payload[detail_key] = child[detail_key]
            child_id = child.get("子资产编号")
            children = payload.get("子资产") if isinstance(payload.get("子资产"), list) else []
            for candidate in children:
                if isinstance(candidate, dict) and candidate.get("子资产编号") == child_id:
                    deep_merge_dict(candidate, child_patch)
        if asset.get("asset_type") == "角色":
            prompt = pick_payload_text(child, ["角色资产图生成提示词"]) or pick_payload_text(payload, ["角色资产图生成提示词"])
            if prompt:
                payload["角色资产图生成提示词"] = prompt
        elif asset.get("asset_type") == "场景":
            payload["场景资产图生成提示词"] = build_non_role_asset_image_prompt("场景", payload, target_country, target_language)
        elif asset.get("asset_type") == "道具":
            prompt = pick_payload_text(payload, ["道具资产图生成提示词"])
            if prompt:
                payload["道具资产图生成提示词"] = prompt
        asset["payload"] = payload
        attach_display_variable(asset)
        merged += 1
    return merged


def complete_asset_details(
    agent: dict[str, Any],
    base_input_payload: dict[str, Any],
    assets: list[dict[str, Any]],
    batch_size: int = 3,
) -> dict[str, Any]:
    eligible = [asset for asset in assets if asset_detail_missing_fields(asset)]
    if agent.get("mode") != "volcengine" or not eligible:
        return {"补全批次": 0, "补全资产数": 0, "补全跳过数": len(assets) - len(eligible), "补全调用数": 0, "补全错误": [], "补全原始输出": []}
    outputs: list[dict[str, Any]] = []
    errors: list[str] = []
    merged_total = 0
    for start in range(0, len(eligible), batch_size):
        batch = eligible[start : start + batch_size]
        detail_payload = {
            "目标国家": base_input_payload.get("目标国家", ""),
            "目标语言": base_input_payload.get("目标语言", ""),
            "待补全资产": [{**asset_detail_completion_input(asset), "缺失字段": asset_detail_missing_fields(asset)} for asset in batch],
            "不可修改字段": ["资产变量", "展示资产变量", "主资产编号", "子资产编号", "道具编号"],
            "约束": "只回填每项缺失字段清单中的目标字段；原剧可见事实未知时保留 unknown 和 needsReview，不得补造人物外貌、族裔或服装细节。",
        }
        try:
            raw = call_compact_video_json(agent, detail_payload, ASSET_DETAIL_COMPLETION_PROMPT)
            parsed = parse_json_object(raw, {})
            merged = merge_asset_detail_completion(
                batch,
                parsed,
                str(base_input_payload.get("目标国家") or "目标国家"),
                str(base_input_payload.get("目标语言") or "目标语言"),
            )
            merged_total += merged
            outputs.append({"批次": start // batch_size + 1, "字符数": len(raw), "合并资产数": merged})
        except ProviderError as exc:
            errors.append(str(exc))
    return {"补全批次": len(outputs) + len(errors), "补全资产数": merged_total, "补全跳过数": len(assets) - len(eligible), "补全调用数": len(outputs) + len(errors), "补全错误": errors, "补全原始输出": outputs}


def extract_display_variables(text: str) -> list[str]:
    if not isinstance(text, str):
        return []
    variables = re.findall(r"@[A-Za-z0-9\u4e00-\u9fff_-]+", text)
    deduped: list[str] = []
    for variable in variables:
        if variable not in deduped:
            deduped.append(variable)
    return deduped


def join_asset_refs(refs: list[str]) -> str:
    return "、".join(refs) if refs else "无"


def build_fallback_storyboard_prompt(
    time_range: str = "00:00:00-00:00:12",
    references: list[str] | None = None,
    summary: str = "按原视频顺序复刻当前剧情片段，保持人物关系、动作节奏和镜头调度。",
) -> str:
    reference_text = join_asset_refs(references or [])
    first_ref = (references or ["当前时间段资产"])[0]
    return f"""分镜时间范围：{time_range}
镜头时长：{time_range}
出镜角色：无
场景：无
画面描述：
片段1：{first_ref} 所在的当前剧情画面按原视频同时间段复刻，{summary}，保持人物站位、动作顺序、表情变化、对话节奏和剪辑节奏。
视频生成约束：保持原视频画幅和镜头节奏，资产引用必须与当前时间段一致，成片语言使用目标语言，无后期对白字幕，无平台水印，无上传者水印，无播放器界面，无平台角标。
负面提示词：调用错误资产、画面残留中文、出现后期对白字幕、平台水印、人物站位与原镜头不符
引用资产变量：{reference_text}"""


def build_fallback_storyboards_from_index(asset_occurrence_index: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_segments = asset_occurrence_index or [
        {
            "片段编号": "片段_001",
            "开始时间": "00:00:00",
            "结束时间": "00:00:12",
            "片段剧情简述": "按原视频首段剧情生成分镜。",
            "出现资产变量": [],
        }
    ]
    storyboards: list[dict[str, Any]] = []
    for index, segment in enumerate(source_segments, start=1):
        start = segment.get("开始时间") or "00:00:00"
        end = segment.get("结束时间") or start or "00:00:12"
        time_range = f"{start}-{end}"
        references = [str(value) for value in segment.get("出现资产变量", []) if str(value).strip()]
        prompt = build_fallback_storyboard_prompt(
            time_range=time_range,
            references=references,
            summary=segment.get("片段剧情简述") or "按原视频当前时间段复刻剧情、动作和镜头节奏。",
        )
        storyboards.append(
            {
                "title": f"分镜 {index:03d}",
                "time_range": time_range,
                "prompt": prompt,
                "payload": {
                    "分镜编号": f"分镜_{index:03d}",
                    "来源片段编号": segment.get("片段编号") or f"片段_{index:03d}",
                    "分镜时间范围": {"开始时间": start, "结束时间": end},
                    "引用资产变量": references,
                    "最终视频生成提示词": prompt,
                },
            }
        )
    return storyboards


def source_segments_are_reusable(segments: list[dict[str, Any]]) -> bool:
    return bool(segments) and all(
        isinstance(segment, dict)
        and bool(str(segment.get("开始时间") or "").strip())
        and bool(str(segment.get("结束时间") or "").strip())
        and bool(str(segment.get("片段剧情简述") or "").strip())
        for segment in segments
    )


def parse_storyboard_range(range_text: str) -> tuple[float, float]:
    if not isinstance(range_text, str) or not range_text.strip():
        return 0.0, 0.0
    start_text, _, end_text = range_text.partition("-")
    start = parse_asset_timecode(start_text)
    end = parse_asset_timecode(end_text or start_text)
    if end < start:
        end = start
    return start, end


def ranges_overlap_seconds(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return max(a[0], b[0]) <= min(a[1] or a[0], b[1] or b[0])


def allowed_asset_variables_for_time_range(time_range: str, asset_occurrence_index: list[dict[str, Any]]) -> set[str]:
    storyboard_range = parse_storyboard_range(time_range)
    allowed: set[str] = set()
    for segment in asset_occurrence_index:
        if not isinstance(segment, dict):
            continue
        segment_range = parse_time_range_seconds(segment.get("开始时间"), segment.get("结束时间"))
        if not ranges_overlap_seconds(storyboard_range, segment_range):
            continue
        for value in segment.get("出现资产变量") or []:
            if isinstance(value, str) and value.strip():
                allowed.add(value.strip())
        for item in segment.get("出现资产") or []:
            if not isinstance(item, dict):
                continue
            for key in ["展示资产变量", "资产变量"]:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    allowed.add(value.strip())
    return allowed


def allowed_asset_items_for_time_range(time_range: str, asset_occurrence_index: list[dict[str, Any]]) -> list[dict[str, str]]:
    storyboard_range = parse_storyboard_range(time_range)
    allowed: list[dict[str, str]] = []
    seen: set[str] = set()
    for segment in asset_occurrence_index:
        if not isinstance(segment, dict):
            continue
        segment_range = parse_time_range_seconds(segment.get("开始时间"), segment.get("结束时间"))
        if not ranges_overlap_seconds(storyboard_range, segment_range):
            continue
        for item in segment.get("出现资产") or []:
            if not isinstance(item, dict):
                continue
            display_variable = str(item.get("展示资产变量") or item.get("资产变量") or "").strip()
            if not display_variable or display_variable in seen:
                continue
            seen.add(display_variable)
            allowed.append(
                {
                    "资产变量": display_variable,
                    "资产类型": str(item.get("资产类型") or ""),
                    "本地化资产名称": str(item.get("本地化资产名称") or ""),
                }
            )
    return allowed


def storyboard_refs_by_type(refs: list[str], allowed_items: list[dict[str, str]]) -> tuple[list[str], list[str], list[str]]:
    type_by_variable = {item["资产变量"]: item.get("资产类型", "") for item in allowed_items}
    roles: list[str] = []
    scenes: list[str] = []
    props: list[str] = []
    for ref in refs:
        asset_type = type_by_variable.get(ref, "")
        if asset_type == "角色":
            roles.append(ref)
        elif asset_type == "场景":
            scenes.append(ref)
        elif asset_type == "道具":
            props.append(ref)
    return roles, scenes, props


def prompt_segments_from_payload(payload: dict[str, Any], prompt: str, refs: list[str]) -> list[str]:
    segments: list[str] = []
    action = payload.get("分镜具体动作描述")
    if isinstance(action, str) and action.strip():
        segments.append(action.strip())
    shots = payload.get("镜头列表")
    if isinstance(shots, list):
        for shot in shots:
            if isinstance(shot, dict):
                text = str(shot.get("镜头描述") or "").strip()
                if text:
                    segments.append(text)
    if not segments:
        body = re.sub(r"^(分镜时间范围|镜头时长|出镜角色|场景|画面描述|引用资产变量|角色音色参考|视频生成约束|负面提示词)：.*$", "", prompt, flags=re.MULTILINE)
        body = body.replace("画面描述：", "").strip()
        for part in re.split(r"\n+", body):
            clean = part.strip()
            if clean:
                segments.append(clean)
    if not segments:
        segments.append("按原视频当前时间段复刻剧情、动作和镜头节奏。")
    if refs and not any(ref in " ".join(segments) for ref in refs):
        segments[0] = f"{join_asset_refs(refs)} 参与当前片段，{segments[0]}"
    return segments


def format_storyboard_prompt(
    time_range: str,
    payload: dict[str, Any],
    prompt: str,
    refs: list[str],
    allowed_items: list[dict[str, str]],
) -> str:
    roles, scenes, _ = storyboard_refs_by_type(refs, allowed_items)
    segments = prompt_segments_from_payload(payload, prompt, refs)
    lines = [
        f"镜头时长：{time_range}",
        f"出镜角色：{join_asset_refs(roles)}",
        f"场景：{join_asset_refs(scenes)}",
        "画面描述：",
    ]
    for index, segment in enumerate(segments, start=1):
        clean = re.sub(r"^片段\d+[：:]\s*", "", segment).strip()
        lines.append(f"片段{index}：{clean}")
    constraints = payload.get("视频生成约束")
    if not isinstance(constraints, str) or not constraints.strip():
        constraints = "保持原视频画幅和镜头节奏，资产引用必须与当前时间段一致，成片语言使用目标语言，无后期对白字幕，无平台水印，无上传者水印，无播放器界面，无平台角标。"
    lines.append(f"视频生成约束：{constraints.strip()}")
    negative = payload.get("负面提示词")
    if isinstance(negative, list):
        negative_text = "、".join(str(value) for value in negative if str(value).strip())
    else:
        negative_text = str(negative or "").strip()
    if not negative_text:
        negative_text = "调用错误资产、画面残留中文、出现后期对白字幕、平台水印、人物站位与原镜头不符"
    lines.append(f"负面提示词：{negative_text}")
    lines.append(f"引用资产变量：{join_asset_refs(refs)}")
    return "\n".join(lines)


def enforce_no_subtitle_generation(text: str) -> str:
    if not text:
        return text
    cleaned = text
    cleaned = re.sub(r"底部[^。；;]*字幕[^。；;]*[。；;]?", "", cleaned)
    cleaned = re.sub(r"[^。；;，,]*字幕同步呈现[^。；;]*[。；;]?", "旁白以画外音呈现。", cleaned)
    cleaned = cleaned.replace("旁白字幕", "旁白画外音")
    cleaned = cleaned.replace("对白字幕同步", "对白以画外音同步")
    cleaned = cleaned.replace("底部字幕", "画外音")
    return cleaned


def sanitize_storyboard_asset_references(
    storyboards: list[dict[str, Any]],
    asset_occurrence_index: list[dict[str, Any]],
    old_to_display: dict[str, str],
) -> None:
    all_display_variables = set(old_to_display.values())
    all_old_variables = set(old_to_display.keys())
    for storyboard in storyboards:
        allowed = allowed_asset_variables_for_time_range(storyboard.get("time_range", ""), asset_occurrence_index)
        allowed_items = allowed_asset_items_for_time_range(storyboard.get("time_range", ""), asset_occurrence_index)
        allowed_display: list[str] = []
        for value in sorted(allowed):
            display_variable = old_to_display.get(value, value if value.startswith("@") else "")
            if display_variable and display_variable not in allowed_display:
                allowed_display.append(display_variable)
        payload = storyboard.get("payload") if isinstance(storyboard.get("payload"), dict) else {}
        prompt = str(storyboard.get("prompt") or "")
        refs = payload.get("引用资产变量") if isinstance(payload.get("引用资产变量"), list) else []
        normalized_refs = []
        for value in refs:
            text = old_to_display.get(str(value), str(value))
            if text in allowed_display and text not in normalized_refs:
                normalized_refs.append(text)
        for value in extract_display_variables(prompt):
            if value not in normalized_refs:
                if value in allowed_display:
                    normalized_refs.append(value)
        if not normalized_refs:
            for value in allowed_display:
                if value not in normalized_refs:
                    normalized_refs.append(value)
        else:
            normalized_refs = [value for value in normalized_refs if value in prompt or not extract_display_variables(prompt)]
        for value in normalized_refs:
            if value not in prompt:
                prompt = f"{value} {prompt}"
        for value in allowed_display:
            if value in prompt and value not in normalized_refs:
                normalized_refs.append(value)
        invalid_display = (all_display_variables | all_old_variables) - allowed - set(allowed_display) - set(normalized_refs)
        for variable in sorted(invalid_display, key=len, reverse=True):
            prompt = prompt.replace(variable, "")
        payload["引用资产变量"] = normalized_refs
        payload["资产映射校验"] = {
            "允许资产变量": sorted(allowed),
            "采用资产变量": normalized_refs,
        }
        storyboard["payload"] = payload
        formatted_prompt = format_storyboard_prompt(storyboard.get("time_range", ""), payload, prompt, normalized_refs, allowed_items)
        storyboard["prompt"] = enforce_no_subtitle_generation(formatted_prompt)
        payload["最终视频生成提示词"] = storyboard["prompt"]
        for text_key in ["分镜具体动作描述", "视频生成约束"]:
            if isinstance(payload.get(text_key), str):
                payload[text_key] = enforce_no_subtitle_generation(payload[text_key])


def normalize_storyboards_from_agent(parsed: dict[str, Any], old_to_display: dict[str, str] | None = None) -> list[dict[str, Any]]:
    old_to_display = old_to_display or {}
    items = parsed.get("分镜列表") or parsed.get("镜头列表") or parsed.get("storyboards") or parsed.get("shots")
    if not isinstance(items, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        prompt = item.get("最终视频生成提示词") or item.get("视频生成提示词") or item.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            prompt_parts = [
                item.get("角色音色参考", ""),
                item.get("分镜场景设定", ""),
                item.get("分镜具体动作描述", ""),
            ]
            shots = item.get("镜头列表")
            if isinstance(shots, list):
                for shot in shots:
                    if isinstance(shot, dict):
                        prompt_parts.append(f"{shot.get('镜头编号', '')}：{shot.get('镜头描述', '')}")
            prompt_parts.append(item.get("视频生成约束", ""))
            negative = item.get("负面提示词")
            if isinstance(negative, list):
                prompt_parts.append("负面提示词：" + "、".join(str(value) for value in negative))
            prompt = "\n".join(part for part in prompt_parts if isinstance(part, str) and part.strip())
        prompt = convert_asset_variables(prompt, old_to_display)
        if not prompt.strip():
            continue
        references = item.get("引用资产变量") or item.get("引用资产") or item.get("资产变量")
        if isinstance(references, list):
            item["引用资产变量"] = [old_to_display.get(str(value), str(value)) for value in references]
        item["最终视频生成提示词"] = prompt
        time_range = item.get("分镜时间范围") or item.get("时间范围") or item.get("time_range")
        if isinstance(time_range, dict):
            start = time_range.get("开始时间", "00:00:00")
            end = time_range.get("结束时间", "00:00:12")
            time_range_text = f"{start}-{end}"
        else:
            start = item.get("开始时间")
            end = item.get("结束时间")
            time_range_text = f"{start}-{end}" if start and end else str(time_range or "00:00:00-00:00:12")
        normalized.append(
            {
                "title": item.get("分镜编号") or item.get("镜头编号") or f"分镜 {index:03d}",
                "time_range": time_range_text,
                "prompt": prompt,
                "payload": item,
            }
        )
    return normalized


def insert_storyboards(
    conn: sqlite3.Connection,
    project_id: str,
    episode_id: str,
    storyboards: list[dict[str, Any]],
) -> None:
    timestamp = now_iso()
    for item in storyboards:
        conn.execute(
            """
            INSERT INTO storyboards
            (id, project_id, episode_id, title, time_range, prompt, payload_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("storyboard"),
                project_id,
                episode_id,
                item["title"],
                item["time_range"],
                item["prompt"],
                json.dumps(item["payload"], ensure_ascii=False),
                "可生成",
                timestamp,
            ),
        )


def run_series_analysis(conn: sqlite3.Connection, project: dict[str, Any], parent_task_id: str) -> dict[str, Any]:
    episodes = rows_to_dicts(
        conn.execute(
            "SELECT * FROM episodes WHERE project_id = ? ORDER BY episode_index",
            (project["id"],),
        ).fetchall()
    )
    if not episodes:
        raise HTTPException(status_code=400, detail="No episodes uploaded")
    total = len(episodes)
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, episode in enumerate(episodes, start=1):
        def update_series_progress(stage: str, extra: dict[str, Any] | None = None) -> None:
            progress_result = {
                "总集数": total,
                "当前序号": index,
                "当前集ID": episode["id"],
                "当前集编号": episode["episode_index"],
                "当前阶段": stage,
                "成功集数": len(successes),
                "失败集数": len(failures),
                "成功明细": successes,
                "失败明细": failures,
            }
            if extra:
                progress_result.update(extra)
            update_task_status(
                conn,
                parent_task_id,
                "运行中",
                f"{stage}第 {episode['episode_index']} 集（{index}/{total}）",
                progress_result,
            )
            conn.commit()

        update_series_progress("正在提取资产")
        try:
            asset_task = run_asset_extraction(conn, project, episode["id"])
            update_series_progress(
                "正在生成分镜",
                {"当前资产任务ID": asset_task["id"], "当前资产数量": asset_task.get("result", {}).get("资产数量", 0)},
            )
            storyboard_task = run_storyboard_generation(conn, project, episode["id"])
            successes.append(
                {
                    "episodeId": episode["id"],
                    "episodeIndex": episode["episode_index"],
                    "assetTaskId": asset_task["id"],
                    "storyboardTaskId": storyboard_task["id"],
                    "assetCount": asset_task.get("result", {}).get("资产数量", 0),
                    "storyboardCount": storyboard_task.get("result", {}).get("分镜数量", 0),
                }
            )
            update_series_progress("已完成本集", {"当前分镜任务ID": storyboard_task["id"]})
        except Exception as exc:
            failures.append(
                {
                    "episodeId": episode["id"],
                    "episodeIndex": episode["episode_index"],
                    "error": str(exc),
                }
            )
            update_series_progress("本集分析失败", {"当前错误": str(exc)})
        conn.commit()
    result = {
        "总集数": total,
        "成功集数": len(successes),
        "失败集数": len(failures),
        "成功明细": successes,
        "失败明细": failures,
        "项目级资产数": len(project_asset_history_summary(conn, project["id"])),
    }
    conn.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", ("整剧分析完成", now_iso(), project["id"]))
    message = f"整剧分析完成：成功 {len(successes)} 集，失败 {len(failures)} 集"
    return {"message": message, "result": result}


def run_asset_extraction(
    conn: sqlite3.Connection,
    project: dict[str, Any],
    episode_id: str | None,
    task_id: str | None = None,
) -> dict[str, Any]:
    episode = get_episode_or_first(conn, project["id"], episode_id)
    ark_upload_error = ""
    if not str(episode.get("ark_file_id") or "").strip() and Path(episode["file_path"]).exists():
        ark_result = upload_episode_to_ark(conn, episode)
        conn.commit()
        episode = get_episode_or_first(conn, project["id"], episode_id)
        if not ark_result["file_id"]:
            ark_upload_error = ark_result.get("error") or "Ark 文件上传失败"
    try:
        agent = prepare_agent("assetExtractionLocalization")
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    route_project_agent_prompt(agent, project)
    history_assets = project_asset_history_summary(conn, project["id"], episode["id"])
    overseas_mode = project.get("remake_mode", "overseas_localization") == "overseas_localization"
    input_payload = {
        "当前集视频": episode["file_path"],
        "当前集视频URL": episode.get("source_url", ""),
        "当前集Ark文件ID": episode.get("ark_file_id", ""),
        "__video_url": episode.get("source_url", ""),
        "__video_file_id": episode.get("ark_file_id", ""),
        "当前集编号": f"第 {episode['episode_index']} 集",
        "目标国家": project["target_country"] if overseas_mode else "不适用（国内模式）",
        "目标语言": project["target_language"] if overseas_mode else "保留原片语言，不翻译",
        "重制模式": project.get("remake_mode", "overseas_localization"),
        "源语言": project.get("source_language", ""),
        "模式路由强约束": "仅海外模式执行本土化；国内模式只记录原片事实，目标外观与源事实分别保存。" if not overseas_mode else "按既有海外本土化规则处理。",
        "历史资产库": history_assets,
        "历史资产归并要求": "当前集如出现历史资产库中的同一角色、同一场景或同一道具，必须复用其本地化名称和资产身份，只能新增子资产或出现记录；不得重复创建主资产。",
        "资产提取范围要求": "必须提取当前集视频中影响复刻的全部有效资产：主角、配角、群像/队友/对手/裁判/教练等可见人物，关键场景，关键道具、配件、服装号码、球鞋、篮球、哨子、记分牌等。不得用核心资产数量上限截断输出。必须返回完整合法 JSON，不能输出半截 JSON。",
        "systemPromptChars": len(agent.get("prompt", "")),
    }
    source_sha256 = source_file_sha256(str(episode.get("file_path") or ""))
    source_cache_key = asset_source_cache_key(source_sha256, agent, project, history_assets) if source_sha256 else ""
    has_existing_assets = conn.execute(
        "SELECT 1 FROM assets WHERE project_id = ? AND episode_id = ? LIMIT 1",
        (project["id"], episode["id"]),
    ).fetchone() is not None
    cached_source = find_cached_asset_source(conn, source_cache_key) if source_cache_key and not has_existing_assets else None
    raw_model_output = ""
    parsed_model_output: dict[str, Any] = {}
    model_error = ""
    model_json_repaired = False
    stream_chars = 0
    stream_events: list[tuple[str, int]] = []
    if agent["mode"] == "volcengine" and not cached_source:
        if not str(episode.get("ark_file_id") or "").strip() and not str(episode.get("source_url") or "").strip():
            model_error = "Ark 视频文件未就绪，未调用模型；请先重试同步 Ark 后再提取资产。"
            if ark_upload_error:
                model_error = f"{model_error} 上传错误：{ark_upload_error}"
        else:
            try:
                progress_saved_at = 0.0
                progress_saved_chars = -1
                progress_saved_stage = ""

                def save_stream_progress(stage: str, chars: int) -> None:
                    nonlocal progress_saved_at, progress_saved_chars, progress_saved_stage
                    now = time.monotonic()
                    if stage == progress_saved_stage and chars - progress_saved_chars < 4096 and now - progress_saved_at < 3:
                        return
                    progress_saved_at = now
                    progress_saved_chars = chars
                    progress_saved_stage = stage
                    if task_id:
                        update_task_stream_progress(conn, task_id, stage, chars)

                if task_id:
                    save_stream_progress("processing", 0)
                raw_model_output, stream_chars, stream_events, stream_error = call_compact_video_json_stream(
                    agent,
                    input_payload,
                    ASSET_EXTRACTION_COMPACT_PROMPT,
                    on_progress=save_stream_progress if task_id else None,
                )
                if stream_error:
                    model_error = stream_error
                if task_id and not stream_error:
                    save_stream_progress("postprocessing", stream_chars)
            except ProviderError as exc:
                model_error = str(exc)
                raw_model_output = f"模型调用失败：{model_error}"
                stream_chars = 0
                stream_events = []
            if not model_error:
                parsed_model_output, model_json_repaired = parse_json_object_with_repair(raw_model_output)
    if cached_source:
        parsed_model_output = cached_source.get("模型解析输出") if isinstance(cached_source.get("模型解析输出"), dict) else {}
        raw_model_output = str(cached_source.get("模型原始输出") or "")
        extracted_assets = json.loads(json.dumps(cached_source["资产源事实快照"], ensure_ascii=False))
    else:
        source_analysis = json.loads(json.dumps(parsed_model_output, ensure_ascii=False))
        extracted_assets = normalize_assets_from_agent(source_analysis)
    used_fallback_assets = agent["mode"] != "volcengine" and not extracted_assets
    if agent["mode"] == "volcengine" and not cached_source and (model_error or not extracted_assets):
        if not model_error:
            model_error = "模型已返回内容，但 JSON 修复后仍没有可用资产结构。"
        result = {
            "当前集编号": f"第 {episode['episode_index']} 集",
            "模型模式": agent["mode"],
            "提示词文件": agent["prompt_file"],
            "资产数量": 0,
            "使用回退资产": False,
            "模型错误": model_error,
            "模型JSON已修复": model_json_repaired,
            "模型原始输出": raw_model_output,
            "模型解析输出": parsed_model_output,
            "流式输出字符数": stream_chars,
            "流式事件": [(stage, chars) for stage, chars in stream_events],
        }
        message = f"资产提取失败：{model_error}"
        task = create_task_with_status(conn, project["id"], episode["id"], "asset_extraction", "失败", message, result)
        record_agent_run(
            conn,
            task["id"],
            project["id"],
            episode["id"],
            "assetExtractionLocalization",
            agent,
            input_payload,
            result,
        )
        return task
    assets_to_insert = extracted_assets or build_mock_assets()
    assets_to_insert = [attach_display_variable(asset) for asset in assets_to_insert]
    assets_to_insert = [annotate_project_asset_keys(asset, int(episode["episode_index"])) for asset in assets_to_insert]
    asset_occurrence_index = normalize_asset_occurrence_index(parsed_model_output.get("资产出现片段索引"), assets_to_insert)
    apply_capture_times_from_index(assets_to_insert, asset_occurrence_index)
    detail_completion_result = {"补全批次": 0, "补全资产数": 0, "补全跳过数": len(extracted_assets), "补全调用数": 0, "补全错误": [], "补全原始输出": [], "缓存复用": bool(cached_source)}
    if extracted_assets and not cached_source:
        detail_completion_result = complete_asset_details(agent, input_payload, assets_to_insert)
    if cached_source:
        detail_completion_result["sourceTaskId"] = cached_source.get("_source_task_id", "")
    source_cache_ready = bool(cached_source) or (
        bool(extracted_assets)
        and not used_fallback_assets
        and not detail_completion_result.get("补全错误")
        and not any(asset_detail_missing_fields(asset) for asset in assets_to_insert)
    )
    source_fact_snapshot = strip_asset_image_references(json.loads(json.dumps(assets_to_insert, ensure_ascii=False))) if source_cache_ready else []
    conn.execute("DELETE FROM assets WHERE project_id = ? AND episode_id = ?", (project["id"], episode["id"]))
    frame_stats = capture_asset_original_frames(project["id"], episode, assets_to_insert)
    insert_assets(conn, project["id"], episode["id"], assets_to_insert)
    result = {
        "当前集编号": f"第 {episode['episode_index']} 集",
        "模型模式": agent["mode"],
        "提示词文件": agent["prompt_file"],
        "资产数量": len(assets_to_insert),
        "使用回退资产": used_fallback_assets,
        "模型错误": model_error,
        "模型JSON已修复": model_json_repaired,
        "模型原始输出": raw_model_output,
        "模型解析输出": parsed_model_output,
        "资产变量映射": {asset["variable"]: asset.get("display_variable", "") for asset in assets_to_insert},
        "资产出现片段索引": asset_occurrence_index,
        "资产详情补全": detail_completion_result,
        "资产源缓存键": source_cache_key if source_cache_ready else "",
        "sourceTaskId": cached_source.get("_source_task_id", "") if cached_source else "",
        "cache_hit": bool(cached_source),
        "资产源事实快照": source_fact_snapshot if extracted_assets and not used_fallback_assets else [],
        "原片抽帧统计": frame_stats,
        "流式输出字符数": stream_chars,
        "流式事件": [(stage, chars) for stage, chars in stream_events],
    }
    conn.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", ("资产已提取", now_iso(), project["id"]))
    message_parts: list[str] = []
    if cached_source:
        message_parts.append(f"复用同源同配置资产源结果（来源任务 {cached_source.get('_source_task_id', '')}），当前项目独立建立资产与原片帧记录")
    elif used_fallback_assets:
        message_parts.append("当前为显式 mock 模式，已生成示例资产")
    elif model_json_repaired:
        message_parts.append(f"资产提取完成（已自动修复模型 JSON，共生成 {stream_chars} 字符）")
    else:
        message_parts.append(f"资产提取完成（共生成 {stream_chars} 字符）")
    task = insert_task(conn, project["id"], episode["id"], "asset_extraction", "；".join(message_parts), result)
    record_agent_run(
        conn,
        task["id"],
        project["id"],
        episode["id"],
        "assetExtractionLocalization",
        agent,
        input_payload,
        result,
    )
    return task


def run_storyboard_generation(conn: sqlite3.Connection, project: dict[str, Any], episode_id: str | None) -> dict[str, Any]:
    episode = get_episode_or_first(conn, project["id"], episode_id)
    ark_upload_error = ""
    if not str(episode.get("ark_file_id") or "").strip() and Path(episode["file_path"]).exists():
        ark_result = upload_episode_to_ark(conn, episode)
        conn.commit()
        episode = get_episode_or_first(conn, project["id"], episode_id)
        if not ark_result["file_id"]:
            ark_upload_error = ark_result.get("error") or "Ark 文件上传失败"
    try:
        agent = prepare_agent("storyboardPromptGeneration")
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    route_project_agent_prompt(agent, project, storyboard=True)
    overseas_mode = project.get("remake_mode", "overseas_localization") == "overseas_localization"
    asset_count = conn.execute(
        "SELECT COUNT(*) FROM assets WHERE project_id = ? AND episode_id = ?",
        (project["id"], episode["id"]),
    ).fetchone()[0]
    if asset_count == 0:
        raise HTTPException(status_code=400, detail="Run asset extraction before storyboard generation")
    assets = rows_to_dicts(
        conn.execute(
            "SELECT asset_type, name, localized_name, variable, payload_json FROM assets WHERE project_id = ? AND episode_id = ? ORDER BY created_at",
            (project["id"], episode["id"]),
        ).fetchall()
    )
    for asset in assets:
        asset["payload"] = json.loads(asset.pop("payload_json"))
        attach_display_variable(asset)
    old_to_display, _ = variable_maps(assets)
    asset_occurrence_index: list[dict[str, Any]] = []
    latest_asset_task = row_to_dict(
        conn.execute(
            """
            SELECT id, result_json FROM tasks
            WHERE project_id = ? AND episode_id = ? AND task_type = ? AND status = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project["id"], episode["id"], "asset_extraction", "成功"),
        ).fetchone()
    )
    if latest_asset_task:
        try:
            asset_task_result = json.loads(latest_asset_task["result_json"])
            raw_index = asset_task_result.get("资产出现片段索引")
            asset_occurrence_index = normalize_asset_occurrence_index(raw_index, assets)
        except (TypeError, json.JSONDecodeError):
            asset_occurrence_index = []
    if not asset_occurrence_index:
        asset_occurrence_index = normalize_asset_occurrence_index([], assets)
    source_segments_reused = source_segments_are_reusable(asset_occurrence_index)
    asset_summary = [
        {
            "资产类型": asset["asset_type"],
            "原剧资产名称": asset["name"],
            "本地化资产名称": asset["localized_name"] if overseas_mode else asset["name"],
            "原变量": asset["variable"],
            "资产变量": asset["display_variable"],
        }
        for asset in assets
    ]
    input_payload = {
        "当前集视频": episode["file_path"],
        "当前集视频URL": episode.get("source_url", ""),
        "当前集Ark文件ID": episode.get("ark_file_id", ""),
        "__video_url": episode.get("source_url", ""),
        "__video_file_id": episode.get("ark_file_id", ""),
        "当前集编号": f"第 {episode['episode_index']} 集",
        "目标国家": project["target_country"] if overseas_mode else "不适用（国内模式）",
        "目标语言": project["target_language"] if overseas_mode else "保留原片语言，不翻译",
        "重制模式": project.get("remake_mode", "overseas_localization"),
        "源语言": project.get("source_language", ""),
        "分镜画面类型": project.get("storyboard_visual_style", "live_action"),
        "模式路由强约束": "仅海外模式使用目标国家/翻译；国内模式保留原片语言且不翻译，具体语言未知时也不猜测；资产描述读取当前项目已确认目标资产，保持源动作和时间码不变。" if not overseas_mode else "按既有海外本土化规则处理。",
        "资产摘要": asset_summary,
        "资产出现片段索引": asset_occurrence_index,
        "资产变量映射": old_to_display,
        "变量格式要求": "最终视频生成提示词必须使用资产摘要里的 @变量，不得使用 {{角色_001_子资产_001}} 这类旧变量。",
        "systemPromptChars": len(agent.get("prompt", "")),
    }
    raw_model_output = ""
    parsed_model_output: dict[str, Any] = {}
    model_error = ""
    model_json_repaired = False
    stream_chars = 0
    stream_events: list[tuple[str, int]] = []
    if agent["mode"] == "volcengine" and not source_segments_reused:
        if not str(episode.get("ark_file_id") or "").strip() and not str(episode.get("source_url") or "").strip():
            model_error = "Ark 视频文件未就绪，未调用模型；请先重试同步 Ark 后再生成分镜。"
            if ark_upload_error:
                model_error = f"{model_error} 上传错误：{ark_upload_error}"
        else:
            try:
                raw_model_output = call_agent_chat("storyboardPromptGeneration", agent, input_payload)
                stream_chars = len(raw_model_output)
                stream_events = []
            except ProviderError as exc:
                model_error = str(exc)
                raw_model_output = f"模型调用失败：{model_error}"
                stream_chars = 0
                stream_events = []
            if not model_error:
                parsed_model_output, model_json_repaired = parse_json_object_with_repair(raw_model_output)
    generated_storyboards = normalize_storyboards_from_agent(parsed_model_output, old_to_display)
    used_fallback_storyboard = agent["mode"] != "volcengine" and not generated_storyboards and not source_segments_reused
    if agent["mode"] == "volcengine" and not source_segments_reused and (model_error or not generated_storyboards):
        if not model_error:
            model_error = "模型已返回内容，但 JSON 修复后仍没有可用分镜结构。"
        payload = {
            "模型模式": agent["mode"],
            "提示词文件": agent["prompt_file"],
            "模型错误": model_error,
            "模型JSON已修复": model_json_repaired,
            "模型原始输出": raw_model_output,
            "模型解析输出": parsed_model_output,
            "分镜数量": 0,
            "使用回退分镜": False,
            "流式输出字符数": stream_chars,
            "流式事件": [(stage, chars) for stage, chars in stream_events],
        }
        message = f"分镜生成失败：{model_error}"
        task = create_task_with_status(conn, project["id"], episode["id"], "storyboard_generation", "失败", message, payload)
        record_agent_run(
            conn,
            task["id"],
            project["id"],
            episode["id"],
            "storyboardPromptGeneration",
            agent,
            input_payload,
            payload,
        )
        return task
    if not generated_storyboards:
        generated_storyboards = build_fallback_storyboards_from_index(asset_occurrence_index)
    sanitize_storyboard_asset_references(generated_storyboards, asset_occurrence_index, old_to_display)
    for storyboard in generated_storyboards:
        apply_storyboard_project_prompt(conn, project, storyboard)
    conn.execute("DELETE FROM storyboards WHERE project_id = ? AND episode_id = ?", (project["id"], episode["id"]))
    insert_storyboards(conn, project["id"], episode["id"], generated_storyboards)
    payload = {
        "分镜编号": "分镜_001",
        "模型模式": agent["mode"],
        "提示词文件": agent["prompt_file"],
        "模型错误": model_error,
        "模型JSON已修复": model_json_repaired,
        "模型原始输出": raw_model_output,
        "模型解析输出": parsed_model_output,
        "分镜数量": len(generated_storyboards),
        "使用回退分镜": used_fallback_storyboard,
        "复用源片段": source_segments_reused,
        "sourceTaskId": latest_asset_task.get("id", "") if source_segments_reused and latest_asset_task else "",
        "流式输出字符数": stream_chars,
        "流式事件": [(stage, chars) for stage, chars in stream_events],
    }
    conn.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", ("分镜已生成", now_iso(), project["id"]))
    message_parts: list[str] = []
    if used_fallback_storyboard:
        message_parts.append("当前为显式 mock 模式，已按资产片段生成示例分镜")
    elif source_segments_reused:
        message_parts.append(f"分镜生成完成（复用已提取源片段，共 {len(generated_storyboards)} 段；未再次分析整片）")
    elif model_json_repaired:
        message_parts.append(f"分镜生成完成（已自动修复模型 JSON，共生成 {stream_chars} 字符）")
    else:
        message_parts.append(f"分镜生成完成（共生成 {stream_chars} 字符）")
    task = insert_task(conn, project["id"], episode["id"], "storyboard_generation", "；".join(message_parts), payload)
    record_agent_run(
        conn,
        task["id"],
        project["id"],
        episode["id"],
        "storyboardPromptGeneration",
        agent,
        input_payload,
        payload,
    )
    return task


def run_video_generation(conn: sqlite3.Connection, project: dict[str, Any], storyboard_id: str | None) -> dict[str, Any]:
    try:
        agent = prepare_agent("videoGeneration")
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not storyboard_id:
        storyboard = row_to_dict(
            conn.execute(
                "SELECT * FROM storyboards WHERE project_id = ? ORDER BY created_at LIMIT 1",
                (project["id"],),
            ).fetchone()
        )
    else:
        storyboard = row_to_dict(
            conn.execute(
                "SELECT * FROM storyboards WHERE id = ? AND project_id = ?",
                (storyboard_id, project["id"]),
            ).fetchone()
        )
    if not storyboard:
        raise HTTPException(status_code=400, detail="Generate a storyboard before video generation")
    storyboard["payload"] = json.loads(storyboard.pop("payload_json"))
    apply_storyboard_project_prompt(conn, project, storyboard)
    generation_id = new_id("video")
    output_dir = PROJECTS_DIR / project["id"] / "outputs" / "videos"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{generation_id}.txt"
    output_path.write_text(
        f"Mock video generation placeholder\n\nStoryboard: {storyboard['id']}\n\nPrompt:\n{storyboard['prompt']}\n",
        encoding="utf-8",
    )
    payload = {"mock": True, "note": "真实视频生成接入后这里保存 mp4 输出路径"}
    conn.execute(
        """
        INSERT INTO video_generations
        (id, project_id, episode_id, storyboard_id, status, prompt, output_path, is_selected, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            generation_id,
            project["id"],
            storyboard["episode_id"],
            storyboard["id"],
            "成功",
            storyboard["prompt"],
            str(output_path),
            1,
            json.dumps(payload, ensure_ascii=False),
            now_iso(),
        ),
    )
    conn.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", ("视频片段已生成", now_iso(), project["id"]))
    result = {
        "generationId": generation_id,
        "outputPath": str(output_path),
        "模型模式": agent["mode"],
        "提示词文件": agent["prompt_file"],
    }
    task = insert_task(conn, project["id"], storyboard["episode_id"], "video_generation", "mock 视频生成完成", result)
    record_agent_run(
        conn,
        task["id"],
        project["id"],
        storyboard["episode_id"],
        "videoGeneration",
        agent,
        {
            "分镜编号": storyboard["id"],
            "最终视频生成提示词": storyboard["prompt"],
            "systemPromptChars": len(agent.get("prompt", "")),
        },
        result,
    )
    return task
