from __future__ import annotations

import io
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import main


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "DB_PATH", data_dir / "test.sqlite3")
    monkeypatch.setattr(main, "PROJECTS_DIR", data_dir / "projects")
    monkeypatch.setattr(main, "prepare_agent", lambda _key: {"mode": "mock", "model_name": "test-mock", "prompt": "", "prompt_file": "test"})
    main.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


def create_project(client: TestClient, **payload):
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["project"]


def insert_source_records(project: dict, *, selected_image: bool = False) -> tuple[str, str]:
    with main.connect() as conn:
        episode_id = "episode_source_1"
        conn.execute(
            "INSERT INTO episodes (id,project_id,episode_index,filename,file_path,status,created_at) VALUES (?,?,?,?,?,?,?)",
            (episode_id, project["id"], 1, "source.mp4", "C:/test/source.mp4", "已上传", "2026-01-01T00:00:00Z"),
        )
        asset_id = "asset_source_1"
        payload = {
            "原剧核心形象描述": "源片里是一位30岁男性，穿蓝色外套。",
            "本地化核心形象描述": "旧项目海外化女性角色描述，不可继承。",
            "本地化角色名称": "Overseas Alice",
            "剧情身份": "主角",
        }
        conn.execute(
            "INSERT INTO assets (id,project_id,episode_id,asset_type,name,localized_name,variable,status,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (asset_id, project["id"], episode_id, "角色", "原片角色甲", "Overseas Alice", "{{role_1}}", "已确认", json.dumps(payload, ensure_ascii=False), "2026-01-01T00:00:00Z"),
        )
        image_id = "image_source_1"
        if selected_image:
            image_path = main.PROJECTS_DIR / project["id"] / "assets" / "roles" / "old.png"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"parent target image")
            conn.execute(
                "INSERT INTO asset_images (id,project_id,asset_id,status,prompt,file_path,is_selected,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (image_id, project["id"], asset_id, "成功", "old prompt", str(image_path), 1, json.dumps({"source": "old"}), "2026-01-01T00:00:00Z"),
            )
        storyboard_payload = {"引用资产变量": ["@OverseasAlice"], "分镜具体动作描述": "角色向前走三步，然后回头。"}
        conn.execute(
            "INSERT INTO storyboards (id,project_id,episode_id,title,time_range,prompt,payload_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("storyboard_source_1", project["id"], episode_id, "分镜1", "00:00:01-00:00:03", "@OverseasAlice 向前走三步，然后回头。", json.dumps(storyboard_payload, ensure_ascii=False), "已生成", "2026-01-01T00:00:00Z"),
        )
    return asset_id, image_id


def test_legacy_default_and_derived_project_separates_target_outputs(client: TestClient):
    legacy = create_project(client, title="legacy")
    assert legacy["remake_mode"] == "overseas_localization"
    assert legacy["storyboard_visual_style"] == "live_action"
    parent = create_project(client, title="parent", remakeMode="overseas_localization")
    insert_source_records(parent, selected_image=True)

    derived_response = client.post(
        f"/api/projects/{parent['id']}/derive",
        json={"title": "cg child", "remakeMode": "cg_3d", "sourceLanguage": "中文"},
    )
    assert derived_response.status_code == 200, derived_response.text
    derived = derived_response.json()["project"]
    assert derived["parent_project_id"] == parent["id"]
    assert derived["storyboard_visual_style"] == "cg_3d"
    with main.connect() as conn:
        old_asset = conn.execute("SELECT * FROM assets WHERE project_id=?", (parent["id"],)).fetchone()
        new_asset = conn.execute("SELECT * FROM assets WHERE project_id=?", (derived["id"],)).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM asset_images WHERE project_id=?", (derived["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT is_selected FROM asset_images WHERE project_id=?", (parent["id"],)).fetchone()[0] == 1
        old_payload = json.loads(old_asset["payload_json"])
        new_payload = json.loads(new_asset["payload_json"])
        assert old_payload["本地化核心形象描述"] == "旧项目海外化女性角色描述，不可继承。"
        assert new_payload["原片资产描述"] == "源片里是一位30岁男性，穿蓝色外套。"
        assert "本地化核心形象描述" not in new_payload
        assert new_payload["源AssetKey"] == "asset_source_1"
        assert new_payload["目标生成描述"] == new_payload["原片资产描述"]
        derived_asset_id = new_asset["id"]
        derived_variable = new_payload["展示资产变量"]
        # A historical overseas style phrase may remain in the source prompt;
        # changing project mode must clean that phrase without losing actions.
        conn.execute("UPDATE projects SET storyboard_visual_style='cg_3d' WHERE id=?", (derived["id"],))
        board_row = conn.execute("SELECT * FROM storyboards WHERE project_id=?", (derived["id"],)).fetchone()
        board_payload = json.loads(board_row["payload_json"])
        board_payload["源分镜文本"] = "真人实拍电影质感，角色向前走三步，然后回头。"
        conn.execute("UPDATE storyboards SET prompt=?, payload_json=? WHERE id=?", (board_payload["源分镜文本"], json.dumps(board_payload, ensure_ascii=False), board_row["id"]))
    storyboard_response = client.get(f"/api/projects/{derived['id']}/storyboards")
    assert storyboard_response.status_code == 200
    storyboard = storyboard_response.json()["storyboards"][0]
    assert storyboard["payload"]["引用资产变量"] == [derived_variable]
    assert storyboard["payload"]["分镜具体动作描述"] == "角色向前走三步，然后回头。"
    assert storyboard["prompt"].count(main.STORYBOARD_STYLE_TEMPLATES["cg_3d"]) == 1
    assert "角色向前走三步，然后回头" in storyboard["prompt"]
    assert "真人实拍电影质感" not in storyboard["prompt"]
    assert "白色背景" not in storyboard["prompt"]
    assert "源AssetKey asset_source_1" not in storyboard["prompt"]
    mapping = storyboard["payload"]["目标资产映射快照"][0]
    assert mapping["sourceAssetKey"] == "asset_source_1"
    assert mapping["projectAssetId"] == derived_asset_id
    assert mapping["imagePath"] == ""

    # Save -> GET preview -> refresh/list -> retry composer stays idempotent.
    saved = client.patch(
        f"/api/projects/{derived['id']}/storyboards/{storyboard['id']}",
        json={"prompt": "角色向前走三步，然后回头。"},
    )
    assert saved.status_code == 200, saved.text
    preview1 = saved.json()["storyboard"]
    preview2 = client.get(f"/api/projects/{derived['id']}/storyboards").json()["storyboards"][0]
    assert preview1["prompt"] == preview2["prompt"]
    assert preview2["payload"]["源分镜文本"] == "角色向前走三步，然后回头。"
    assert preview2["prompt"].count(main.STORYBOARD_STYLE_TEMPLATES["cg_3d"]) == 1
    run = client.post(
        f"/api/projects/{derived['id']}/tasks/run",
        json={"taskType": "video_generation", "storyboardId": storyboard["id"]},
    )
    assert run.status_code == 200, run.text
    task = run.json()["task"]
    task_state = client.get(f"/api/tasks/{task['id']}").json()["task"]
    assert task_state["status"] == "成功"
    generation_path = Path(task_state["result"]["outputPath"])
    final_text = generation_path.read_text(encoding="utf-8")
    assert final_text.count(main.STORYBOARD_STYLE_TEMPLATES["cg_3d"]) == 1
    assert "真人实拍电影质感" not in final_text

    generated = client.post(
        f"/api/projects/{derived['id']}/assets/{derived_asset_id}/generate-image",
        json={"prompt": "真人实拍电影质感，避免三维CG渲染，一只蓝色卡通猫，圆耳朵。"},
    )
    assert generated.status_code == 200, generated.text
    image = generated.json()["asset"]["images"][0]
    assert image["prompt"].count(main.CG_ROLE_FIXED_TEMPLATE) == 1
    assert "蓝色卡通猫" in image["prompt"]
    assert "真人实拍电影质感" not in image["prompt"]
    assert "避免三维CG渲染" not in image["prompt"]
    assert "男性" not in image["prompt"]
    assert image["payload"]["promptSnapshot"] == image["prompt"]
    assert image["payload"]["remakeMode"] == "cg_3d"
    assert image["payload"]["templateVersion"] == "cg-asset-v1"
    with main.connect() as conn:
        persisted = json.loads(conn.execute("SELECT payload_json FROM assets WHERE id=?", (derived_asset_id,)).fetchone()[0])
        assert persisted["原片资产描述"] == "源片里是一位30岁男性，穿蓝色外套。"
        assert "真人实拍电影质感" in persisted["目标生成描述"]


def test_replacement_requires_target_description_for_generation(client: TestClient):
    project = create_project(client, title="replacement-no-target", remakeMode="asset_replacement", sourceLanguage="中文", replaceCharacters=True)
    asset_id, _ = insert_source_records(project, selected_image=False)
    response = client.post(f"/api/projects/{project['id']}/assets/{asset_id}/generate-image", json={})
    assert response.status_code == 422
    assert "请先填写目标资产外观描述" in response.json()["detail"]
    assert "30岁男性" not in response.json()["detail"]


def test_provider_receives_exact_final_role_prompt_snapshot_and_size(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    project = create_project(client, title="provider-prompt-spy", remakeMode="cg_3d", sourceLanguage="中文")
    asset_id, _ = insert_source_records(project, selected_image=False)
    captured: dict = {}

    class ProviderSpy:
        def generate_image(self, **kwargs):
            captured.update(kwargs)
            image = io.BytesIO()
            Image.new("RGB", (8, 8), (20, 50, 80)).save(image, format="PNG")
            return {"image_bytes": image.getvalue(), "mime_type": "image/png", "size": kwargs["size"], "model": "provider-spy", "raw": {}}

    monkeypatch.setattr(main, "prepare_agent", lambda _key: {"mode": "volcengine", "model_name": "provider-spy", "prompt": "", "prompt_file": "test"})
    monkeypatch.setattr(main, "load_models_config", lambda: {"volcengine": {"imageSize": "2K", "imageWatermark": False}})
    monkeypatch.setattr(main, "get_model_provider", lambda _config: ProviderSpy())
    response = client.post(
        f"/api/projects/{project['id']}/assets/{asset_id}/generate-image",
        json={"prompt": "蓝色卡通猫，黄色雨衣"},
    )
    assert response.status_code == 200, response.text
    image = response.json()["asset"]["images"][0]
    assert captured["prompt"] == image["prompt"] == image["payload"]["promptSnapshot"]
    assert captured["prompt"].count(main.CG_ROLE_FIXED_TEMPLATE) == 1
    assert captured["size"] == "2K"
    assert image["payload"]["mock"] is False


def test_storyboard_preserves_raw_source_hash_and_narrative_layout_words(client: TestClient):
    project = create_project(client, title="storyboard-source-preservation", remakeMode="cg_3d", sourceLanguage="中文")
    insert_source_records(project, selected_image=False)
    source = "角色看向墙上的胸像，镜头拉远到白色背景墙；旁边挂着四视图照片和全身站姿海报。"
    with main.connect() as conn:
        conn.execute("UPDATE storyboards SET prompt=? WHERE project_id=?", (source, project["id"]))

    first = client.get(f"/api/projects/{project['id']}/storyboards").json()["storyboards"][0]
    stored_source = first["payload"]["源分镜文本"]
    assert hashlib.sha256(stored_source.encode("utf-8")).hexdigest() == hashlib.sha256(source.encode("utf-8")).hexdigest()
    for phrase in ("墙上的胸像", "白色背景墙", "四视图照片", "全身站姿海报"):
        assert phrase in first["prompt"]
    second = client.get(f"/api/projects/{project['id']}/storyboards").json()["storyboards"][0]
    assert second["payload"]["源分镜文本"] == source
    assert second["prompt"] == first["prompt"]


def test_effective_asset_source_ignores_historical_candidates_when_reusing_original(client: TestClient, tmp_path: Path):
    project = create_project(client, title="effective-asset-source", remakeMode="asset_replacement", sourceLanguage="中文", replaceCharacters=True)
    asset_id, _ = insert_source_records(project, selected_image=False)
    source_path = tmp_path / "source-character.png"
    Image.new("RGB", (3, 3), (60, 70, 80)).save(source_path, format="PNG")
    with main.connect() as conn:
        row = conn.execute("SELECT payload_json FROM assets WHERE id=?", (asset_id,)).fetchone()
        asset_payload = json.loads(row[0])
        asset_payload["原资产图路径"] = str(source_path)
        conn.execute("UPDATE assets SET payload_json=? WHERE id=?", (json.dumps(asset_payload, ensure_ascii=False), asset_id))

    target_desc = "蓝色卡通猫，穿黄色雨衣。"
    updated = client.patch(
        f"/api/projects/{project['id']}/assets/{asset_id}",
        json={"payload": {"目标生成描述": target_desc}},
    )
    assert updated.status_code == 200, updated.text
    image_bytes = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(image_bytes, format="PNG")
    uploaded = client.post(
        f"/api/projects/{project['id']}/assets/{asset_id}/upload-image",
        files={"file": ("target.png", image_bytes.getvalue(), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    target_image = uploaded.json()["asset"]["images"][0]
    board_url = f"/api/projects/{project['id']}/storyboards"
    target_board = client.get(board_url).json()["storyboards"][0]
    target_map = target_board["payload"]["目标资产映射快照"][0]
    assert target_map["effectiveAssetSource"] == "target"
    assert target_map["imageId"] == target_image["id"]
    assert target_map["targetDescription"] == target_desc

    # Turning the per-type switch off forces the source image and source facts,
    # even while a historical target candidate remains selected in storage.
    switched_off = client.patch(f"/api/projects/{project['id']}", json={"replaceCharacters": False})
    assert switched_off.status_code == 200, switched_off.text
    source_board = client.get(board_url).json()["storyboards"][0]
    source_map = source_board["payload"]["目标资产映射快照"][0]
    assert source_map["effectiveAssetSource"] == "source"
    assert source_map["imagePath"] == str(source_path)
    assert "30岁男性" in source_map["targetDescription"]
    asset_response = client.get(f"/api/projects/{project['id']}/assets").json()["assets"][0]
    assert asset_response["payload"]["effectiveAssetSource"] == "source"
    assert asset_response["payload"]["当前有效外观描述"] == source_map["targetDescription"]

    switched_on = client.patch(f"/api/projects/{project['id']}", json={"replaceCharacters": True})
    assert switched_on.status_code == 200, switched_on.text
    target_again = client.get(board_url).json()["storyboards"][0]["payload"]["目标资产映射快照"][0]
    assert target_again["effectiveAssetSource"] == "target"
    assert target_again["imageId"] == target_image["id"]

    restored = client.post(f"/api/projects/{project['id']}/assets/{asset_id}/restore-original")
    assert restored.status_code == 200, restored.text
    assert restored.json()["asset"]["payload"]["effectiveAssetSource"] == "source"
    assert len(restored.json()["asset"]["images"]) == 1
    assert restored.json()["asset"]["images"][0]["is_selected"] == 0
    restored_board = client.get(board_url).json()["storyboards"][0]["payload"]["目标资产映射快照"][0]
    assert restored_board["effectiveAssetSource"] == "source"
    assert restored_board["imagePath"] == str(source_path)
    assert "30岁男性" in restored_board["targetDescription"]

    with main.connect() as conn:
        row = conn.execute("SELECT payload_json FROM assets WHERE id=?", (asset_id,)).fetchone()
        missing_source_payload = json.loads(row[0])
        missing_source_payload["原资产图路径"] = ""
        conn.execute("UPDATE assets SET payload_json=? WHERE id=?", (json.dumps(missing_source_payload, ensure_ascii=False), asset_id))
    missing_source_asset = client.get(f"/api/projects/{project['id']}/assets").json()["assets"][0]
    assert missing_source_asset["payload"]["effectiveAssetSource"] == "source"
    assert missing_source_asset["payload"]["当前有效资产就绪"] is False
    missing_source_board = client.get(board_url).json()["storyboards"][0]
    missing_source_map = missing_source_board["payload"]["目标资产映射快照"][0]
    assert missing_source_map["imagePath"] == ""
    assert "缺少原片资产图" in missing_source_board["prompt"]


def test_upload_decode_ownership_and_existing_selection_protection(client: TestClient):
    project = create_project(client, title="upload-test", remakeMode="asset_replacement", sourceLanguage="中文", replaceCharacters=True)
    asset_id, _ = insert_source_records(project, selected_image=False)
    valid_image = io.BytesIO()
    Image.new("RGB", (2, 2), (12, 34, 56)).save(valid_image, format="PNG")
    upload = client.post(
        f"/api/projects/{project['id']}/assets/{asset_id}/upload-image",
        files={"file": ("tiny.png", valid_image.getvalue(), "image/png")},
    )
    assert upload.status_code == 200, upload.text
    chosen = upload.json()["asset"]["images"][0]
    assert chosen["is_selected"] == 1
    assert chosen["payload"]["source"] == "user_upload"
    assert Path(chosen["file_path"]).is_file()
    board = client.get(f"/api/projects/{project['id']}/storyboards").json()["storyboards"][0]
    assert "以当前已确认目标图为外观权威" in board["prompt"]
    assert "30岁男性" not in board["prompt"]

    invalid = client.post(
        f"/api/projects/{project['id']}/assets/{asset_id}/upload-image",
        files={"file": ("fake.png", b"\x89PNG\r\n\x1a\nnot-an-image", "image/png")},
    )
    assert invalid.status_code == 415
    with main.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM asset_images WHERE project_id=? AND is_selected=1", (project["id"],)).fetchone()[0] == 1
        selected_id = conn.execute("SELECT id FROM asset_images WHERE project_id=? AND is_selected=1", (project["id"],)).fetchone()[0]
    foreign = create_project(client, title="foreign")
    rejected = client.post(f"/api/projects/{foreign['id']}/assets/{asset_id}/select-image", json={"imageId": selected_id})
    assert rejected.status_code == 404


def test_mode_update_rejects_existing_outputs_and_running_jobs(client: TestClient):
    project = create_project(client, title="mode-lock")
    asset_id, _ = insert_source_records(project, selected_image=True)
    rejected = client.patch(f"/api/projects/{project['id']}", json={"remakeMode": "cg_3d"})
    assert rejected.status_code == 409
    with main.connect() as conn:
        conn.execute("DELETE FROM asset_images WHERE project_id=?", (project["id"],))
        conn.execute(
            "INSERT INTO asset_generation_jobs (id,project_id,asset_id,remake_mode,template_version,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("job_running", project["id"], asset_id, "overseas_localization", "legacy-v1", "running", "now", "now"),
        )
    rejected_inflight = client.patch(f"/api/projects/{project['id']}", json={"remakeMode": "cg_3d"})
    assert rejected_inflight.status_code == 409


def test_generation_preparation_failure_preserves_adopted_image_and_unlocks_mode(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    project = create_project(client, title="provider-failure", remakeMode="cg_3d", sourceLanguage="中文")
    asset_id, selected_id = insert_source_records(project, selected_image=True)

    def fail_prepare(_key: str):
        raise main.ProviderError("test provider unavailable")

    monkeypatch.setattr(main, "prepare_agent", fail_prepare)
    failed = client.post(f"/api/projects/{project['id']}/assets/{asset_id}/generate-image", json={})
    assert failed.status_code == 400
    with main.connect() as conn:
        job = conn.execute("SELECT status FROM asset_generation_jobs WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project["id"],)).fetchone()
        selected = conn.execute("SELECT id FROM asset_images WHERE project_id=? AND asset_id=? AND is_selected=1", (project["id"], asset_id)).fetchone()
        assert job["status"] == "failed"
        assert selected["id"] == selected_id
    unlocked = client.patch(f"/api/projects/{project['id']}", json={"sourceLanguage": "中文（确认）"})
    assert unlocked.status_code == 200


def test_legacy_database_migrates_projects_to_overseas_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "legacy-data"
    data_dir.mkdir()
    db_path = data_dir / "legacy.sqlite3"
    import sqlite3

    with sqlite3.connect(db_path) as legacy:
        legacy.execute("CREATE TABLE projects (id TEXT PRIMARY KEY,title TEXT NOT NULL,target_country TEXT NOT NULL,target_language TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
        legacy.execute("INSERT INTO projects VALUES ('old-1','旧项目','美国','英语','旧项目状态','now','now')")
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "PROJECTS_DIR", data_dir / "projects")
    main.init_db()
    with main.connect() as conn:
        migrated = dict(conn.execute("SELECT * FROM projects WHERE id='old-1'").fetchone())
    assert migrated["remake_mode"] == "overseas_localization"
    assert migrated["storyboard_visual_style"] == "live_action"
    assert migrated["source_language"] == ""


def test_domestic_analysis_allows_unknown_source_language(client: TestClient):
    project = create_project(client, title="source-language-required", remakeMode="cg_3d", sourceLanguage="")
    insert_source_records(project, selected_image=False)
    episode_id = "episode_source_1"
    started = client.post(
        f"/api/projects/{project['id']}/tasks/run",
        json={"taskType": "asset_extraction", "episodeId": episode_id},
    )
    assert started.status_code == 200
    tasks = client.get(f"/api/projects/{project['id']}/tasks").json()["tasks"]
    task = next(item for item in tasks if item["id"] == started.json()["task"]["id"])
    assert task["status"] == "成功", task["message"]
    agent = {"prompt": ""}
    main.route_project_agent_prompt(agent, {"remake_mode": "cg_3d", "source_language": ""})
    assert "具体语言未知时也保持原片语言，不翻译" in agent["prompt"]
    assert "不得将本提示当作语言识别结果" in agent["prompt"]


def test_domestic_storyboard_keeps_english_source_language_without_forbidding_english(client: TestClient):
    project = create_project(client, title="english-source-language", remakeMode="asset_replacement", sourceLanguage="English")
    insert_source_records(project, selected_image=False)
    storyboard = client.get(f"/api/projects/{project['id']}/storyboards").json()["storyboards"][0]
    assert "沿用原片语言（English）" in storyboard["prompt"]
    assert "不得注入目标国家、英语" not in storyboard["prompt"]
    assert "不进行未经选择的翻译或地域本土化" in storyboard["prompt"]


def test_derive_shares_source_file_paths_without_project_delete_endpoint(client: TestClient):
    parent = create_project(client, title="shared-source-parent")
    parent_dir = main.PROJECTS_DIR / parent["id"]
    source_video = parent_dir / "source" / "source.mp4"
    source_frame = parent_dir / "assets" / "original_frames" / "frame.png"
    source_video.parent.mkdir(parents=True, exist_ok=True)
    source_frame.parent.mkdir(parents=True, exist_ok=True)
    source_video.write_bytes(b"test-source-video")
    Image.new("RGB", (2, 2), (1, 2, 3)).save(source_frame)
    with main.connect() as conn:
        conn.execute("INSERT INTO episodes (id,project_id,episode_index,filename,file_path,status,created_at) VALUES (?,?,?,?,?,?,?)",
                     ("episode_shared", parent["id"], 1, "source.mp4", str(source_video), "已上传", "now"))
        payload = {"原剧视觉形象描述": "原片角色", "原资产图路径": str(source_frame), "源AssetKey": "shared-key"}
        conn.execute("INSERT INTO assets (id,project_id,episode_id,asset_type,name,localized_name,variable,status,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("asset_shared", parent["id"], "episode_shared", "角色", "角色", "角色", "{{role}}", "已确认", json.dumps(payload, ensure_ascii=False), "now"))
    derived = client.post(f"/api/projects/{parent['id']}/derive", json={"title": "shared-source-child", "remakeMode": "cg_3d", "sourceLanguage": "中文"}).json()["project"]
    with main.connect() as conn:
        episode = conn.execute("SELECT file_path FROM episodes WHERE project_id=?", (derived["id"],)).fetchone()
        asset = conn.execute("SELECT payload_json FROM assets WHERE project_id=?", (derived["id"],)).fetchone()
        assert episode["file_path"] == str(source_video)
        assert json.loads(asset["payload_json"])["原资产图路径"] == str(source_frame)
        assert conn.execute("SELECT COUNT(*) FROM asset_images WHERE project_id=?", (derived["id"],)).fetchone()[0] == 0
    assert source_video.is_file() and source_frame.is_file()
    assert client.delete(f"/api/projects/{parent['id']}").status_code == 405
    assert source_video.is_file() and source_frame.is_file()
