from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.app.providers.model_provider import ProviderError, get_model_provider, get_model_status


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
PROJECTS_DIR = DATA_DIR / "projects"
DB_PATH = DATA_DIR / "workshop.sqlite3"
CONFIG_DIR = ROOT / "config"
MODELS_CONFIG_PATH = CONFIG_DIR / "models.json"


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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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


def capture_asset_original_frames(project_id: str, episode: dict[str, Any], assets: list[dict[str, Any]]) -> None:
    ffmpeg = shutil.which("ffmpeg")
    video_path = Path(str(episode.get("file_path") or ""))
    if not ffmpeg or not video_path.exists():
        return
    output_dir = PROJECTS_DIR / project_id / "assets" / "original_frames"
    output_dir.mkdir(parents=True, exist_ok=True)
    for asset in assets:
        payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
        child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else None
        if payload.get("原资产图路径") or (isinstance(child, dict) and child.get("原资产图路径")):
            continue
        frame_path = output_dir / f"{new_id('source_frame')}.jpg"
        seconds = first_asset_timecode(payload)
        command = [
            ffmpeg,
            "-y",
            "-ss",
            f"{seconds:.2f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-update",
            "1",
            str(frame_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=30)
        except (subprocess.SubprocessError, OSError):
            continue
        if not frame_path.exists():
            continue
        url = data_file_url(str(frame_path))
        payload["原资产图路径"] = str(frame_path)
        payload["原资产图URL"] = url
        payload["原资产截图路径"] = str(frame_path)
        payload["原资产截图URL"] = url
        if isinstance(child, dict):
            child["原资产图路径"] = str(frame_path)
            child["原资产图URL"] = url
            child["原资产截图路径"] = str(frame_path)
            child["原资产截图URL"] = url


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
            """
        )
        ensure_column(conn, "episodes", "source_url", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "episodes", "ark_file_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "episodes", "ark_file_status", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "episodes", "ark_file_error", "TEXT NOT NULL DEFAULT ''")


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
        "model": agent.get("model", ""),
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
    asset["payload"]["最终资产图生成提示词"] = infer_asset_image_prompt(
        asset["asset_type"],
        asset["payload"],
        project.get("target_country", "目标国家"),
        project.get("target_language", "目标语言"),
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
    return value or infer_role_prompt_field(payload, child, section, field)


def role_context_text(payload: dict[str, Any], child: dict[str, Any]) -> str:
    return "，".join(
        item
        for item in [
            pick_payload_text(payload, ["剧情身份", "稳定人设定位", "剧情作用"]),
            pick_payload_text(payload, ["本地化核心形象描述"]),
            pick_payload_text(child, ["本地化视觉形象描述", "本地化子资产描述", "差异标签"]),
            pick_payload_text(child, ["本地化子资产名称"]),
            pick_payload_text(payload, ["本地化角色名称"]),
        ]
        if item
    )


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
    if field == "身份定位":
        return identity
    if field == "头身比":
        return "真实真人比例，约7-8头身"
    if field == "身高体感":
        if any(token in visual for token in ["篮球", "球员", "运动员"]):
            return "高挑运动员体感，约185-195cm"
        return "高挑偏修长的青年男性身高体感，约180-190cm"
    if field == "体型":
        return visual
    if field == "肩颈":
        return "肩线平直，颈部修长，运动员式自然挺拔肩颈"
    if field == "腰腿":
        return "腰身偏窄，腿部修长，运动员式真实腰腿比例"
    if field == "整体体态":
        return f"体现目标国家本地化后{identity}的气质、姿态和当前子资产状态"
    if field == "脸型":
        return "立体椭圆脸，脸部轮廓清晰，下颌线自然"
    if field == "骨相":
        return "眉骨和鼻梁有轻微立体度，颧骨不过分突出，下颌线干净"
    if field == "眉形":
        return "深棕色自然平直眉，眉峰轻微上扬"
    if field == "眼形":
        return "深棕色杏眼，眼神清晰稳定，眼窝有自然阴影"
    if field == "鼻形":
        return "鼻梁较直，鼻头自然，鼻翼宽度适中"
    if field == "唇形":
        return "自然中等厚度唇形，唇线清晰，嘴巴自然闭合"
    if field == "妆感":
        return "无明显妆感，皮肤保持真实运动后自然状态"
    if field == "皮肤肤色":
        return "浅小麦色或白皙偏暖肤色，带自然运动后红润"
    if field == "皮肤质感":
        return "真实皮肤纹理，额头和颈部有轻微运动薄汗"
    if field == "气色状态":
        return "运动后轻微红润气色，精神放松但专注"
    if field == "发型名称":
        return "深棕色美式运动短发，顶部蓬松，鬓角修整干净"
    if field == "发色":
        return "深棕色自然发色"
    if field == "长度":
        return "短发长度，顶部约5-7厘米，两侧较短"
    if field == "刘海 /分线":
        return "自然前额碎发，无明显分线"
    if field == "发丝质感":
        return "发丝清晰有层次，轻微蓬松，运动后略带湿润感"
    if field == "整体状态":
        return "干净运动状态，发型保持自然蓬松不过度造型"
    if field in {"主色", "副色", "点缀色", "内衬", "中间层", "外层", "下装", "鞋子", "面料", "纹样", "纹样位置", "结构与廓形", "工艺与重工程度", "穿着状态"}:
        clothing_context = pick_payload_text(child, ["本地化视觉形象描述", "本地化子资产描述", "差异标签"]) or visual
        basketball_defaults = {
            "主色": "白色篮球背心和白色及膝球裤",
            "副色": "蓝色领口、袖口、裤边和鞋面主装饰",
            "点缀色": "橙色球衣号码描边、裤边细线和球鞋局部点缀",
            "内衬": "无明显内衬，球衣为单层透气运动背心",
            "中间层": "无中间层，保持轻量比赛服结构",
            "外层": "白底 NCAA 风格无袖篮球队服，胸前有23号号码",
            "下装": "白色及膝宽松篮球短裤，蓝橙双色裤边",
            "鞋子": "蓝橙配色高帮专业篮球鞋，白色中底，橡胶外底",
            "面料": "透气网眼聚酯运动面料，轻微反光，能看到细密织纹",
            "纹样": "球衣号码23和简洁队服边线，无复杂花纹",
            "纹样位置": "号码位于胸前中央，蓝橙边线位于领口、袖口和裤边",
            "结构与廓形": "宽肩无袖篮球背心，直筒及膝球裤，运动员修身但不紧身",
            "工艺与重工程度": "队服缝线清晰，号码贴布边缘干净，面料轻薄",
            "穿着状态": "干净比赛服，轻微运动褶皱，皮肤带运动后薄汗",
        }
        return basketball_defaults.get(field, clothing_context)
    return visual


def build_role_asset_image_prompt(
    payload: dict[str, Any],
    target_country: str = "目标国家",
    target_language: str = "目标语言",
) -> str:
    child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
    localized_visual = pick_payload_text(child, ["本地化视觉形象描述", "本地化子资产描述"]) or pick_payload_text(payload, ["本地化核心形象描述"])
    identity = pick_payload_text(payload, ["剧情身份", "稳定人设定位", "剧情作用"])
    localized_name = pick_payload_text(payload, ["本地化角色名称"]) or pick_payload_text(child, ["本地化子资产名称"])
    return f"""【基础设定】
性别：{role_prompt_field(payload, child, "基础设定", "性别")}
视觉年龄：{role_prompt_field(payload, child, "基础设定", "视觉年龄")}
国家 / 时代 / 朝代：{target_country} / 现代
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
脸型：{role_prompt_field(payload, child, "脸部特征", "脸型")}
骨相：{role_prompt_field(payload, child, "脸部特征", "骨相")}
眉形：{role_prompt_field(payload, child, "脸部特征", "眉形")}
眼形：{role_prompt_field(payload, child, "脸部特征", "眼形")}
鼻形：{role_prompt_field(payload, child, "脸部特征", "鼻形")}
唇形：{role_prompt_field(payload, child, "脸部特征", "唇形")}
妆感：{role_prompt_field(payload, child, "脸部特征", "妆感")}
皮肤肤色：{role_prompt_field(payload, child, "脸部特征", "皮肤肤色")}
皮肤质感：{role_prompt_field(payload, child, "脸部特征", "皮肤质感")}
气色状态：{role_prompt_field(payload, child, "脸部特征", "气色状态")}

【发型】
发型名称：{role_prompt_field(payload, child, "发型", "发型名称")}
发色：{role_prompt_field(payload, child, "发型", "发色")}
长度：{role_prompt_field(payload, child, "发型", "长度")}
刘海 /分线：{role_prompt_field(payload, child, "发型", "刘海 /分线", ["刘海", "分线"])}
发丝质感：{role_prompt_field(payload, child, "发型", "发丝质感")}
整体状态：{role_prompt_field(payload, child, "发型", "整体状态")}

【服装】
主色：{role_prompt_field(payload, child, "服装", "主色")}
副色：{role_prompt_field(payload, child, "服装", "副色")}
点缀色：{role_prompt_field(payload, child, "服装", "点缀色")}
内衬：{role_prompt_field(payload, child, "服装", "内衬")}
中间层：{role_prompt_field(payload, child, "服装", "中间层")}
外层：{role_prompt_field(payload, child, "服装", "外层")}
下装：{role_prompt_field(payload, child, "服装", "下装")}
鞋子：{role_prompt_field(payload, child, "服装", "鞋子")}
面料：{role_prompt_field(payload, child, "服装", "面料")}
纹样：{role_prompt_field(payload, child, "服装", "纹样")}
纹样位置：{role_prompt_field(payload, child, "服装", "纹样位置")}
结构与廓形：{role_prompt_field(payload, child, "服装", "结构与廓形")}
工艺与重工程度：{role_prompt_field(payload, child, "服装", "工艺与重工程度")}
穿着状态：{role_prompt_field(payload, child, "服装", "穿着状态")}

{ROLE_FIXED_CONSTRAINT}"""


def significant_asset_text(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def slugify_asset_token(value: str, fallback: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", value)
    slug = "_".join(tokens)
    if not slug:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", fallback).strip("_")
    return slug[:64] or "Asset"


def make_display_variable(asset: dict[str, Any]) -> str:
    payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
    child = payload.get("当前子资产") if isinstance(payload.get("当前子资产"), dict) else {}
    name_parts = [
        str(asset.get("localized_name") or asset.get("name") or ""),
        pick_payload_text(child, ["本地化子资产名称", "原剧子资产名称", "差异标签"]),
    ]
    fallback = str(asset.get("variable") or asset.get("id") or "Asset").replace("{", "").replace("}", "")
    return "@" + slugify_asset_token(" ".join(part for part in name_parts if part), fallback)


def attach_display_variable(asset: dict[str, Any]) -> dict[str, Any]:
    payload = asset.get("payload") if isinstance(asset.get("payload"), dict) else {}
    display_variable = payload.get("展示资产变量") if isinstance(payload.get("展示资产变量"), str) else ""
    if not display_variable:
        display_variable = make_display_variable(asset)
    asset["display_variable"] = display_variable
    payload["展示资产变量"] = display_variable
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


def merge_records(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in ["出现记录"]:
        target_records = target.get(key)
        source_records = source.get(key)
        if isinstance(target_records, list) and isinstance(source_records, list):
            target_records.extend(source_records)


def child_is_significant_duplicate(existing_child: dict[str, Any], child: dict[str, Any]) -> bool:
    existing_type = pick_payload_text(existing_child, ["差异类型"])
    current_type = pick_payload_text(child, ["差异类型"])
    weak_types = ["动作", "表情", "情绪", "状态", "休息", "姿态", "神态"]
    if any(item in f"{existing_type}{current_type}" for item in weak_types):
        existing_visual = significant_asset_text(
            pick_payload_text(existing_child, ["本地化视觉形象描述", "原剧视觉形象描述", "本地化子资产名称", "原剧子资产名称"])
        )
        current_visual = significant_asset_text(
            pick_payload_text(child, ["本地化视觉形象描述", "原剧视觉形象描述", "本地化子资产名称", "原剧子资产名称"])
        )
        if existing_visual and current_visual:
            shorter = min(len(existing_visual), len(current_visual))
            overlap = sum(1 for token in [existing_visual[:12], current_visual[:12]] if token and token in existing_visual and token in current_visual)
            return existing_visual == current_visual or shorter < 18 or overlap > 0
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
        visual = pick_payload_text(payload, ["本地化场景描述", "本地化视觉描述", "空间结构描述", "场景资产图生成提示词"])
        constraint = "真人写实风格，8K超高清，明亮柔和光线，完整展示空间结构，无人物，无水印，无文字，真实摄影质感。"
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


class RunTaskRequest(BaseModel):
    taskType: str
    episodeId: str | None = None
    storyboardId: str | None = None


class StoryboardUpdate(BaseModel):
    prompt: str


class EpisodeSourceUpdate(BaseModel):
    sourceUrl: str


class AssetUpdate(BaseModel):
    name: str | None = None
    localizedName: str | None = None
    status: str | None = None
    payload: dict[str, Any] | None = None


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


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "db": str(DB_PATH), "time": now_iso()}


@app.get("/api/files/{resource_path:path}")
def read_data_file(resource_path: str) -> FileResponse:
    target = (DATA_DIR / resource_path).resolve()
    try:
        target.relative_to(DATA_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="File is outside data directory") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target)


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
            INSERT INTO projects (id, title, target_country, target_language, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                payload.title,
                payload.targetCountry,
                payload.targetLanguage,
                "已创建",
                timestamp,
                timestamp,
            ),
        )
        project = get_project_or_404(conn, project_id)
    return {"project": project}


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
            "SELECT * FROM assets WHERE project_id = ? ORDER BY created_at, asset_type",
            (project_id,),
        ).fetchall()
        assets = rows_to_dicts(rows)
        for asset in assets:
            asset["payload"] = enrich_asset_payload_urls(json.loads(asset.pop("payload_json")))
            asset["payload"]["最终资产图生成提示词"] = infer_asset_image_prompt(
                asset["asset_type"],
                asset["payload"],
                project.get("target_country", "目标国家"),
                project.get("target_language", "目标语言"),
            )
            attach_display_variable(asset)
            image_rows = conn.execute(
                "SELECT * FROM asset_images WHERE asset_id = ? ORDER BY created_at DESC",
                (asset["id"],),
            ).fetchall()
            images = rows_to_dicts(image_rows)
            for image in images:
                enrich_asset_image(image)
            asset["images"] = images
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
        current_payload = json.loads(asset["payload_json"])
        if payload.payload is not None:
            current_payload.update(payload.payload)
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
        return get_asset_response(conn, project_id, asset_id)


@app.post("/api/projects/{project_id}/assets/{asset_id}/confirm")
def confirm_asset(project_id: str, asset_id: str) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE assets SET status = ? WHERE id = ? AND project_id = ?",
            ("已确认", asset_id, project_id),
        )
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
        asset_payload = json.loads(asset["payload_json"])
        prompt = payload.prompt or infer_asset_image_prompt(
            asset["asset_type"],
            asset_payload,
            project.get("target_country", "目标国家"),
            project.get("target_language", "目标语言"),
        )
        try:
            agent = prepare_agent("assetImageGeneration")
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        image_id = new_id("asset_image")
        image_dir = PROJECTS_DIR / project_id / "assets" / asset["asset_type"]
        image_dir.mkdir(parents=True, exist_ok=True)
        if agent.get("mode") == "mock":
            image_path = image_dir / f"{image_id}.svg"
            image_path.write_text(render_mock_asset_svg(asset, prompt), encoding="utf-8")
            image_payload = {
                "mock": True,
                "agent": agent,
                "note": "assetImageGeneration 未启用，使用本地 mock 占位图。"
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
                raise HTTPException(status_code=400, detail=str(exc)) from exc
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
            }
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
        conn.execute("UPDATE asset_images SET is_selected = 0 WHERE asset_id = ?", (asset_id,))
        conn.execute("UPDATE asset_images SET is_selected = 1 WHERE id = ?", (payload.imageId,))
        return get_asset_response(conn, project_id, asset_id)


@app.get("/api/projects/{project_id}/storyboards")
def list_storyboards(project_id: str) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT * FROM storyboards WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        storyboards = rows_to_dicts(rows)
        for item in storyboards:
            item["payload"] = json.loads(item.pop("payload_json"))
        return {"storyboards": storyboards}


@app.patch("/api/projects/{project_id}/storyboards/{storyboard_id}")
def update_storyboard(project_id: str, storyboard_id: str, payload: StoryboardUpdate) -> dict[str, Any]:
    with connect() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE storyboards SET prompt = ? WHERE id = ? AND project_id = ?",
            (payload.prompt, storyboard_id, project_id),
        )
        item = row_to_dict(
            conn.execute(
                "SELECT * FROM storyboards WHERE id = ? AND project_id = ?",
                (storyboard_id, project_id),
            ).fetchone()
        )
        if not item:
            raise HTTPException(status_code=404, detail="Storyboard not found")
        item["payload"] = json.loads(item.pop("payload_json"))
        return {"storyboard": item}


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
        if payload.taskType not in {"asset_extraction", "storyboard_generation", "video_generation"}:
            raise HTTPException(status_code=400, detail="Unsupported task type")
        task = create_task_with_status(
            conn,
            project_id,
            payload.episodeId,
            payload.taskType,
            "运行中",
            "任务已开始，后台执行中",
            {
                "taskType": payload.taskType,
                "episodeId": payload.episodeId,
                "storyboardId": payload.storyboardId,
            },
        )
        background_tasks.add_task(
            execute_task_background,
            task["id"],
            project_id,
            payload.taskType,
            payload.episodeId,
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
                completed = run_asset_extraction(conn, project, episode_id)
            elif task_type == "storyboard_generation":
                completed = run_storyboard_generation(conn, project, episode_id)
            elif task_type == "video_generation":
                completed = run_video_generation(conn, project, storyboard_id)
            else:
                raise RuntimeError(f"Unsupported task type: {task_type}")
            update_task_status(conn, task_id, "成功", completed["message"], completed["result"])
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


def build_fallback_storyboard_prompt(
    time_range: str = "00:00:00-00:00:12",
    references: list[str] | None = None,
    summary: str = "按原视频顺序复刻当前剧情片段，保持人物关系、动作节奏和镜头调度。",
) -> str:
    reference_text = "、".join(references or []) or "已确认资产"
    return f"""分镜时间范围：{time_range}
引用资产变量：{reference_text}
角色音色参考：沿用资产库中对应角色的目标语言音色要求，语气和情绪贴合原片。
分镜场景设定：使用资产出现片段索引中与该时间段匹配的场景、角色和道具。
分镜具体动作描述：{summary}
镜头1：真人写实短剧镜头，严格保留原视频同时间段的人物站位、动作顺序、表情变化、对话节奏和剪辑节奏，画面自然调用 {reference_text}。
视频生成约束：保持原视频画幅和镜头节奏，资产引用必须与当前时间段一致，成片语言使用目标语言，无后期对白字幕，无平台水印。
负面提示词：调用错误资产、画面残留中文、出现后期对白字幕、平台水印、人物站位与原镜头不符"""


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


def run_asset_extraction(conn: sqlite3.Connection, project: dict[str, Any], episode_id: str | None) -> dict[str, Any]:
    episode = get_episode_or_first(conn, project["id"], episode_id)
    if not str(episode.get("ark_file_id") or "").strip() and Path(episode["file_path"]).exists():
        upload_episode_to_ark(conn, episode)
        conn.commit()
        episode = get_episode_or_first(conn, project["id"], episode_id)
    try:
        agent = prepare_agent("assetExtractionLocalization")
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    input_payload = {
        "当前集视频": episode["file_path"],
        "当前集视频URL": episode.get("source_url", ""),
        "当前集Ark文件ID": episode.get("ark_file_id", ""),
        "__video_url": episode.get("source_url", ""),
        "__video_file_id": episode.get("ark_file_id", ""),
        "当前集编号": f"第 {episode['episode_index']} 集",
        "目标国家": project["target_country"],
        "目标语言": project["target_language"],
        "历史资产库": "mock 模式读取当前项目历史资产",
        "资产提取范围要求": "必须提取当前集视频中影响复刻的全部有效资产：主角、配角、群像/队友/对手/裁判/教练等可见人物，关键场景，关键道具、配件、服装号码、球鞋、篮球、哨子、记分牌等。不得用核心资产数量上限截断输出。必须返回完整合法 JSON，不能输出半截 JSON。",
        "systemPromptChars": len(agent.get("prompt", "")),
    }
    raw_model_output = ""
    parsed_model_output: dict[str, Any] = {}
    model_error = ""
    if agent["mode"] == "volcengine":
        if not str(episode.get("ark_file_id") or "").strip() and not str(episode.get("source_url") or "").strip():
            raise HTTPException(status_code=400, detail="真实资产提取需要该集已同步 Ark Files，或填写公网/TOS 视频 URL。")
        try:
            raw_model_output = call_agent_chat("assetExtractionLocalization", agent, input_payload)
        except ProviderError as exc:
            model_error = str(exc)
            raw_model_output = f"模型调用失败：{model_error}"
        parsed_model_output = parse_json_object(raw_model_output, {})
    conn.execute("DELETE FROM assets WHERE project_id = ? AND episode_id = ?", (project["id"], episode["id"]))
    extracted_assets = normalize_assets_from_agent(parsed_model_output)
    used_fallback_assets = not extracted_assets
    assets_to_insert = extracted_assets or build_mock_assets()
    assets_to_insert = [attach_display_variable(asset) for asset in assets_to_insert]
    capture_asset_original_frames(project["id"], episode, assets_to_insert)
    asset_occurrence_index = normalize_asset_occurrence_index(parsed_model_output.get("资产出现片段索引"), assets_to_insert)
    insert_assets(conn, project["id"], episode["id"], assets_to_insert)
    result = {
        "当前集编号": f"第 {episode['episode_index']} 集",
        "模型模式": agent["mode"],
        "提示词文件": agent["prompt_file"],
        "资产数量": len(assets_to_insert),
        "使用回退资产": used_fallback_assets,
        "模型错误": model_error,
        "模型原始输出": raw_model_output,
        "模型解析输出": parsed_model_output,
        "资产变量映射": {asset["variable"]: asset.get("display_variable", "") for asset in assets_to_insert},
        "资产出现片段索引": asset_occurrence_index,
    }
    conn.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", ("资产已提取", now_iso(), project["id"]))
    message = "资产提取完成（模型失败，已使用回退资产）" if model_error else "资产提取完成"
    task = insert_task(conn, project["id"], episode["id"], "asset_extraction", message, result)
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
    if not str(episode.get("ark_file_id") or "").strip() and Path(episode["file_path"]).exists():
        upload_episode_to_ark(conn, episode)
        conn.commit()
        episode = get_episode_or_first(conn, project["id"], episode_id)
    try:
        agent = prepare_agent("storyboardPromptGeneration")
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
            SELECT result_json FROM tasks
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
    asset_summary = [
        {
            "资产类型": asset["asset_type"],
            "原剧资产名称": asset["name"],
            "本地化资产名称": asset["localized_name"],
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
        "目标国家": project["target_country"],
        "目标语言": project["target_language"],
        "资产摘要": asset_summary,
        "资产出现片段索引": asset_occurrence_index,
        "资产变量映射": old_to_display,
        "变量格式要求": "最终视频生成提示词必须使用资产摘要里的 @变量，不得使用 {{角色_001_子资产_001}} 这类旧变量。",
        "systemPromptChars": len(agent.get("prompt", "")),
    }
    raw_model_output = ""
    parsed_model_output: dict[str, Any] = {}
    model_error = ""
    if agent["mode"] == "volcengine":
        if not str(episode.get("ark_file_id") or "").strip() and not str(episode.get("source_url") or "").strip():
            raise HTTPException(status_code=400, detail="真实分镜生成需要该集已同步 Ark Files，或填写公网/TOS 视频 URL。")
        try:
            raw_model_output = call_agent_chat("storyboardPromptGeneration", agent, input_payload)
        except ProviderError as exc:
            model_error = str(exc)
            raw_model_output = f"模型调用失败：{model_error}"
        parsed_model_output = parse_json_object(raw_model_output, {})
    conn.execute("DELETE FROM storyboards WHERE project_id = ? AND episode_id = ?", (project["id"], episode["id"]))
    generated_storyboards = normalize_storyboards_from_agent(parsed_model_output, old_to_display)
    used_fallback_storyboard = not generated_storyboards
    if not generated_storyboards:
        generated_storyboards = build_fallback_storyboards_from_index(asset_occurrence_index)
    insert_storyboards(conn, project["id"], episode["id"], generated_storyboards)
    payload = {
        "分镜编号": "分镜_001",
        "模型模式": agent["mode"],
        "提示词文件": agent["prompt_file"],
        "模型错误": model_error,
        "模型原始输出": raw_model_output,
        "模型解析输出": parsed_model_output,
        "分镜数量": len(generated_storyboards),
        "使用回退分镜": used_fallback_storyboard,
    }
    conn.execute("UPDATE projects SET status = ?, updated_at = ? WHERE id = ?", ("分镜已生成", now_iso(), project["id"]))
    message = "分镜生成完成（模型失败，已使用回退分镜）" if model_error else "分镜生成完成"
    task = insert_task(conn, project["id"], episode["id"], "storyboard_generation", message, payload)
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
