import { ChangeEvent, useEffect, useMemo, useRef, useState } from "react";
import { VideoComposition } from "./VideoComposition";

const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");
const ROLE_FIXED_CONSTRAINT = `【固定约束】
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
负面限制：避免卡通、绘画、3D渲染、模糊、畸形、低质量`;

type Screen = "home" | "source" | "analysis" | "assets" | "segments" | "compare";
type StepScreen = "home" | "assets" | "segments" | "compare";
type AssetTab = "角色" | "场景" | "道具" | "资产进度";
type UploadMode = "folder" | "single";
type ModalName = "upload-confirm-modal" | "image-view-modal" | null;
type SegmentPanel = "shot-prompt" | "shot-assets" | "shot-history";

type Project = {
  id: string;
  title: string;
  target_country: string;
  target_language: string;
  status: string;
  remake_mode?: "overseas_localization" | "cg_3d" | "asset_replacement";
  source_language?: string;
  storyboard_visual_style?: "live_action" | "cg_3d";
  replace_characters?: number;
  replace_scenes?: number;
  parent_project_id?: string;
};

type Episode = {
  id: string;
  episode_index: number;
  filename: string;
  size_bytes: number;
  status: string;
  source_url: string;
  file_url?: string;
  ark_file_id: string;
  ark_file_status: string;
  ark_file_error: string;
  created_at: string;
};

type Asset = {
  id: string;
  asset_type: string;
  name: string;
  localized_name: string;
  variable: string;
  display_variable?: string;
  status: string;
  payload?: Record<string, any>;
  images?: Array<{ id: string; file_path: string; file_url?: string; is_selected: number; prompt?: string; created_at?: string; payload?: Record<string, any> }>;
};

type Storyboard = {
  id: string;
  episode_id: string;
  title: string;
  time_range: string;
  prompt: string;
  status: string;
  payload?: Record<string, any>;
};

type VideoGeneration = {
  id: string;
  storyboard_id: string;
  status: string;
  output_path: string;
  created_at: string;
  payload?: Record<string, any>;
};

type Task = {
  id: string;
  project_id?: string;
  episode_id: string | null;
  task_type: string;
  status: string;
  message: string;
  result?: Record<string, any>;
  created_at: string;
  updated_at: string;
};

type AssetGroup = {
  key: string;
  type: string;
  name: string;
  localizedName: string;
  items: Asset[];
};

type ImageView = {
  url: string;
  title: string;
} | null;

type SelectedSource = {
  kind: "local";
  uploadMode?: UploadMode;
  title: string;
  meta: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json() as Promise<T>;
}

function currentChild(asset: Asset) {
  return (asset.payload?.当前子资产 as Record<string, any> | undefined) ?? asset.payload ?? {};
}

function pickText(source: Record<string, any> | undefined, keys: string[]) {
  if (!source) return "";
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return "";
}

function mainAssetKey(asset: Asset) {
  return pickText(asset.payload, ["项目主资产Key", "主资产编号", "道具编号"]) || `${asset.asset_type}:${asset.name}:${asset.localized_name}`;
}

function variantName(asset: Asset) {
  const child = currentChild(asset);
  return (
    pickText(child, ["本地化子资产名称", "原剧子资产名称"]) ||
    pickText(asset.payload, ["本地化道具名称", "原剧道具名称"]) ||
    asset.localized_name
  );
}

function variantMeta(asset: Asset) {
  const child = currentChild(asset);
  return [
    pickText(child, ["差异类型"]),
    pickText(child, ["差异标签"]),
    pickText(child, ["子资产编号"]) || pickText(asset.payload, ["道具编号"]),
  ].filter(Boolean).join(" · ");
}

function assetPrompt(asset: Asset) {
  const child = currentChild(asset);
  const prompt =
    pickText(asset.payload, ["最终资产图生成提示词"]) ||
    pickText(child, ["角色资产图生成提示词", "场景资产图生成提示词", "道具资产图生成提示词"]) ||
    pickText(asset.payload, ["角色资产图生成提示词", "场景资产图生成提示词", "道具资产图生成提示词"]);
  if (prompt) return prompt;
  if (asset.asset_type !== "角色") return prompt;
  if (prompt.includes("【基础设定】") && prompt.includes("【固定约束】")) return prompt;
  const field = (section: string, key: string, fallbackKeys: string[] = []) => {
    const structure = child["角色资产图结构化描述"] ?? asset.payload?.["角色资产图结构化描述"];
    if (structure && typeof structure === "object") {
      const sectionValue = structure[section];
      if (sectionValue && typeof sectionValue === "object" && typeof sectionValue[key] === "string" && sectionValue[key].trim()) {
        return sectionValue[key].trim();
      }
    }
    return pickText(child, [key, ...fallbackKeys]) || pickText(asset.payload, [key, ...fallbackKeys]) || inferRolePromptField(asset, section, key);
  };
  return `【基础设定】
性别：${field("基础设定", "性别")}
视觉年龄：${field("基础设定", "视觉年龄")}
国家 / 时代 / 朝代：${field("基础设定", "国家 / 时代 / 朝代", ["国家时代朝代"])}
身份定位：${field("基础设定", "身份定位", ["剧情身份", "稳定人设定位", "剧情作用"])}
角色名称：${asset.localized_name || asset.name}
本地化视觉锚点：${pickText(child, ["本地化视觉形象描述", "本地化子资产描述"]) || pickText(asset.payload, ["本地化核心形象描述"]) || "根据目标国家审美生成稳定真人写实角色资产，不复用原剧演员脸、原剧地域特征或原剧服装细节。"}

【身材体态】
头身比：${field("身材体态", "头身比")}
身高体感：${field("身材体态", "身高体感")}
体型：${field("身材体态", "体型", ["本地化视觉形象描述", "本地化子资产描述"])}
肩颈：${field("身材体态", "肩颈")}
腰腿：${field("身材体态", "腰腿")}
整体体态：${field("身材体态", "整体体态")}

【脸部特征】
脸型：${field("脸部特征", "脸型")}
骨相：${field("脸部特征", "骨相")}
眉形：${field("脸部特征", "眉形")}
眼形：${field("脸部特征", "眼形")}
鼻形：${field("脸部特征", "鼻形")}
唇形：${field("脸部特征", "唇形")}
妆感：${field("脸部特征", "妆感")}
皮肤肤色：${field("脸部特征", "皮肤肤色")}
皮肤质感：${field("脸部特征", "皮肤质感")}
气色状态：${field("脸部特征", "气色状态")}

【发型】
发型名称：${field("发型", "发型名称")}
发色：${field("发型", "发色")}
长度：${field("发型", "长度")}
刘海 /分线：${field("发型", "刘海 /分线", ["刘海", "分线"])}
发丝质感：${field("发型", "发丝质感")}
整体状态：${field("发型", "整体状态")}

【服装】
主色：${field("服装", "主色")}
副色：${field("服装", "副色")}
点缀色：${field("服装", "点缀色")}
内衬：${field("服装", "内衬")}
中间层：${field("服装", "中间层")}
外层：${field("服装", "外层")}
下装：${field("服装", "下装")}
鞋子：${field("服装", "鞋子")}
面料：${field("服装", "面料")}
纹样：${field("服装", "纹样")}
纹样位置：${field("服装", "纹样位置")}
结构与廓形：${field("服装", "结构与廓形")}
工艺与重工程度：${field("服装", "工艺与重工程度")}
穿着状态：${field("服装", "穿着状态")}

${ROLE_FIXED_CONSTRAINT}`;
}

function assetContextText(asset: Asset) {
  const child = currentChild(asset);
  return [
    pickText(asset.payload, ["剧情身份", "稳定人设定位", "剧情作用"]),
    pickText(asset.payload, ["本地化核心形象描述"]),
    pickText(child, ["本地化视觉形象描述", "本地化子资产描述", "差异标签"]),
    pickText(child, ["本地化子资产名称"]),
    pickText(asset.payload, ["本地化角色名称"]),
  ].filter(Boolean).join("，");
}

function inferRolePromptField(asset: Asset, section: string, key: string) {
  const context = assetContextText(asset);
  const identity = pickText(asset.payload, ["剧情身份", "稳定人设定位", "剧情作用"]) || "核心剧情角色";
  const visual = context || identity;
  const lower = visual.toLowerCase();
  if (key === "性别") {
    if (/[女母]/.test(visual) || ["female", "woman", "girl", "mother"].some((token) => lower.includes(token))) return "女";
    if (/[男父]/.test(visual) || ["male", "man", "boy", "father"].some((token) => lower.includes(token))) return "男";
    return "符合目标国家本地化角色设定的性别表现";
  }
  if (key === "视觉年龄") {
    const age = visual.match(/(\d{2})\s*(?:岁|year|years)/i)?.[1];
    if (age) return `${age}岁`;
    if (/[大学学生球员校园]/.test(visual)) return "20-24岁";
    if (/[总裁父亲母亲教练经理]/.test(visual)) return "30-45岁";
    return "符合目标国家本地化角色设定的真人视觉年龄";
  }
  if (key === "国家 / 时代 / 朝代") return "目标国家本地化 / 现代";
  if (key === "身份定位") return identity;
  if (key === "头身比") return "真实真人比例，约7-8头身";
  if (key === "身高体感") return /篮球|球员|运动员/.test(visual) ? "高挑运动员体感，约185-195cm" : "符合目标国家本地化角色设定和同框比例的真实身高体感";
  if (key === "体型") return visual;
  if (key === "肩颈") return "肩颈自然舒展，符合角色身份和体态气质";
  if (key === "腰腿") return "腰腿比例真实自然，符合目标国家本地化角色体态";
  if (key === "整体体态") return `体现目标国家本地化后${identity}的气质、姿态和当前子资产状态`;
  if (key === "脸型") return "符合目标国家本地化真人审美的写实脸型";
  if (key === "骨相") return "骨相真实自然，符合角色年龄感和剧情气质";
  if (key === "眉形") return "自然眉形，符合目标国家本地化五官气质";
  if (key === "眼形") return "真实自然眼形，符合目标国家本地化人物气质";
  if (key === "鼻形") return "真实自然鼻形，符合本地化真人审美";
  if (key === "唇形") return "自然唇形，嘴巴自然闭合";
  if (key === "妆感") return "自然真实妆感";
  if (key === "皮肤肤色") return "符合目标国家本地化真人写实肤色";
  if (key === "皮肤质感") return "皮肤质感真实自然，纹理清晰";
  if (key === "气色状态") return "符合当前剧情状态的自然气色";
  if (key === "发型名称") return "符合目标国家本地化设定的自然发型";
  if (key === "发色") return "符合本地化角色设定的自然发色";
  if (key === "长度") return "符合目标国家本地化设定的自然长度";
  if (key === "刘海 /分线") return "符合目标国家本地化设定的自然刘海或分线";
  if (key === "发丝质感") return "真实自然发丝质感，清晰有层次";
  if (key === "整体状态") return "干净自然，符合角色身份与当前子资产状态";
  const clothing: Record<string, string> = {
    主色: "从当前子资产服装中提取的主视觉颜色",
    副色: "从当前子资产服装中提取的辅助颜色",
    点缀色: "从当前子资产服装细节中提取的点缀颜色",
    内衬: "符合当前子资产服装层次的内衬",
    中间层: "符合当前子资产服装层次的中间层",
    外层: "符合当前子资产身份和场景的外层服装",
    下装: "符合当前子资产服装搭配的下装",
    鞋子: "符合当前子资产身份、场景和服装搭配的鞋子",
    面料: "真实可见材质，符合角色身份",
    纹样: "符合当前子资产服装的自然纹样",
    纹样位置: "位于服装主体或细节区域的自然纹样位置",
    结构与廓形: "符合角色身份、剧情状态和目标国家审美的服装廓形",
    工艺与重工程度: "真实自然，细节清晰",
    穿着状态: "符合当前剧情状态的自然穿着状态",
  };
  if (clothing[key]) return `${clothing[key]}；参考：${visual}`;
  return visual;
}

function assetDisplayName(asset: Asset) {
  return variantName(asset) || asset.localized_name || asset.name || asset.variable;
}

function slugAssetToken(value: string, fallback: string) {
  const tokens = value.match(/[A-Za-z0-9]+/g);
  if (tokens?.length) return tokens.join("_").slice(0, 64);
  return fallback.replace(/[{}]/g, "").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 64) || "Asset";
}

function assetDisplayVariable(asset: Asset) {
  const explicit = asset.display_variable || pickText(asset.payload, ["展示资产变量"]);
  if (explicit) return explicit;
  const child = currentChild(asset);
  const source = [
    asset.localized_name || asset.name,
    pickText(child, ["本地化子资产名称", "原剧子资产名称", "差异标签"]),
  ].filter(Boolean).join(" ");
  return `@${slugAssetToken(source, asset.variable || asset.id)}`;
}

function assetDisplayMeta(asset: Asset) {
  const episodeIndexes = asset.payload?.出现集数;
  const episodeMeta = Array.isArray(episodeIndexes) && episodeIndexes.length
    ? `出现：第 ${episodeIndexes.join("、")} 集`
    : "";
  return [assetDisplayVariable(asset), asset.localized_name && asset.localized_name !== assetDisplayName(asset) ? asset.localized_name : "", episodeMeta]
    .filter(Boolean)
    .join(" · ");
}

function convertPromptVariables(prompt: string, assets: Asset[]) {
  return assets.reduce((text, asset) => text.replaceAll(asset.variable, assetDisplayVariable(asset)), prompt);
}

function parseTimecode(value: string) {
  const parts = value.split(":").map((part) => Number(part));
  if (parts.some((part) => Number.isNaN(part))) return 0;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0] || 0;
}

function parseRange(range: string) {
  const [start = "00:00:00", end = start] = range.split("-");
  return { start: parseTimecode(start.trim()), end: parseTimecode(end.trim()) };
}

function rangesOverlap(a: { start: number; end: number }, b: { start: number; end: number }) {
  return Math.max(a.start, b.start) <= Math.min(a.end || a.start, b.end || b.start);
}

function splitAssetVariableValues(value: any): string[] {
  if (Array.isArray(value)) return value.flatMap((item) => splitAssetVariableValues(item));
  if (typeof value !== "string") return [];
  return value.split(/[\s,，、；;]+/).map((item) => item.trim()).filter(Boolean);
}

function detailRows(asset: Asset) {
  const child = currentChild(asset);
  return [
    ["剧情身份/功能", pickText(asset.payload, ["剧情身份", "剧情功能"])],
    ["剧情作用", pickText(asset.payload, ["剧情作用", "社会阶层与情绪功能"])],
    ["稳定定位", pickText(asset.payload, ["稳定人设定位", "空间结构描述"])],
    ["原剧核心形象", pickText(asset.payload, ["原剧核心形象描述", "原剧场景描述", "原剧道具描述"])],
    ["本地化核心形象", pickText(asset.payload, ["本地化核心形象描述", "本地化场景描述", "本地化道具描述"])],
    ["原剧变体视觉", pickText(child, ["原剧视觉形象描述", "原剧视觉描述", "原剧子资产描述"])],
    ["本地化变体视觉", pickText(child, ["本地化视觉形象描述", "本地化视觉描述", "本地化子资产描述"])],
    ["音色/状态", pickText(child, ["状态音色调整", "场景状态差异"]) || pickText(asset.payload, ["基础音色描述", "目标语言声音要求"])],
  ].filter(([, value]) => value);
}

function originalAssetDescription(asset: Asset) {
  if (asset.payload?.["原片资产描述"] === "原片描述未留存") return "原片描述未留存。此项目兼容沿用，不把海外化描述误标为原片事实。";
  if (typeof asset.payload?.["原片资产描述"] === "string" && asset.payload["原片资产描述"]) return asset.payload["原片资产描述"];
  const child = currentChild(asset);
  return [
    pickText(asset.payload, ["原剧核心形象描述", "原剧场景描述", "原剧道具描述"]),
    pickText(child, ["原剧视觉形象描述", "原剧视觉描述", "原剧子资产描述"]),
    pickText(asset.payload, ["剧情身份", "剧情功能"]),
  ].filter(Boolean).join("\n") || "原片描述未留存";
}

function localizedAssetDescription(asset: Asset) {
  if (typeof asset.payload?.["目标生成描述"] === "string" && asset.payload["目标生成描述"]) return asset.payload["目标生成描述"];
  const child = currentChild(asset);
  return [
    pickText(asset.payload, ["本地化核心形象描述", "本地化场景描述", "本地化道具描述"]),
    pickText(child, ["本地化视觉形象描述", "本地化视觉描述", "本地化子资产描述"]),
    pickText(child, ["差异标签", "差异类型"]),
  ].filter(Boolean).join("\n") || "目标国家资产描述：保留原资产剧情功能，并完成目标国家本地化转化。";
}

function formatBytes(bytes = 0) {
  if (!bytes) return "0 B";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function assetThumbClass(asset?: Asset) {
  if (asset?.asset_type === "角色") return "face-a";
  if (asset?.asset_type === "场景") return "scene";
  if (asset?.asset_type === "道具") return "prop";
  return "text";
}

function apiAssetUrl(path?: string) {
  if (!path) return "";
  if (/^https?:\/\//i.test(path)) return path;
  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

function selectedAssetImage(asset?: Asset) {
  if (asset?.payload?.effectiveAssetSource === "source") return undefined;
  return asset?.images?.find((item) => item.is_selected);
}

function selectedAssetImageUrl(asset?: Asset) {
  const image = selectedAssetImage(asset);
  return apiAssetUrl(image?.file_url || image?.payload?.source_url);
}

function originalAssetImageUrl(asset?: Asset) {
  if (!asset) return "";
  return apiAssetUrl(
    pickText(asset?.payload, ["原资产图URL", "原资产截图URL"]) ||
    pickText(currentChild(asset), ["原资产图URL", "原资产截图URL"])
  );
}

function groupOriginalImageUrl(group: AssetGroup) {
  for (const item of group.items) {
    const url = originalAssetImageUrl(item);
    if (url) return url;
  }
  return "";
}

function groupConfirmedImageUrl(group: AssetGroup) {
  for (const item of group.items) {
    if (item.status !== "已确认") continue;
    const url = selectedAssetImageUrl(item);
    if (url) return url;
  }
  return "";
}

function uploadProjectTitle(files: ArrayLike<File> | null, fallback: string) {
  const first = files?.[0] as (File & { webkitRelativePath?: string }) | undefined;
  if (!first) return fallback;
  const relative = first.webkitRelativePath || "";
  const folder = relative.split("/").filter(Boolean)[0];
  if (folder) return folder;
  return first.name.replace(/\.[^.]+$/, "") || fallback;
}

function isTaskRunning(task?: Task | null) {
  return Boolean(task && ["运行中", "pending", "processing"].includes(task.status));
}

function mockGenerationLabel(status: string) {
  if (["成功", "完成", "completed"].includes(status)) return "模拟请求已完成（未生成视频）";
  if (["失败", "failed"].includes(status)) return "模拟请求失败（未生成视频）";
  return "模拟请求处理中（未生成视频）";
}

export function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [currentProject, setCurrentProject] = useState<Project | null>(null);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [storyboards, setStoryboards] = useState<Storyboard[]>([]);
  const [generations, setGenerations] = useState<VideoGeneration[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [screen, setScreen] = useState<Screen>("home");
  const [assetSidebarCollapsed, setAssetSidebarCollapsed] = useState(false);
  const [previewPanelCollapsed, setPreviewPanelCollapsed] = useState(false);
  const [selectedSource, setSelectedSource] = useState<SelectedSource | null>(null);
  const [selectedFiles, setSelectedFiles] = useState<File[] | null>(null);
  const [uploadState, setUploadState] = useState<"idle" | "selected" | "uploading" | "uploaded" | "failed">("idle");
  const [modal, setModal] = useState<ModalName>(null);
  const [imageView, setImageView] = useState<ImageView>(null);
  const [targetCountry, setTargetCountry] = useState("美国");
  const [targetLanguage, setTargetLanguage] = useState("英语");
  const [remakeMode, setRemakeMode] = useState<Project["remake_mode"]>("overseas_localization");
  const [sourceLanguage, setSourceLanguage] = useState("");
  const [storyboardVisualStyle, setStoryboardVisualStyle] = useState<Project["storyboard_visual_style"]>("live_action");
  const [replaceCharacters, setReplaceCharacters] = useState(false);
  const [replaceScenes, setReplaceScenes] = useState(false);
  const [targetAssetDescription, setTargetAssetDescription] = useState("");
  const [frameRatio, setFrameRatio] = useState("9:16");
  const [resolution, setResolution] = useState("720p");
  const [styleType, setStyleType] = useState("写实通用");
  const [assetTab, setAssetTab] = useState<AssetTab>("角色");
  const [assetView, setAssetView] = useState<"overview" | "detail">("overview");
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | null>(null);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [selectedStoryboardId, setSelectedStoryboardId] = useState<string | null>(null);
  const [segmentPanel, setSegmentPanel] = useState<SegmentPanel>("shot-prompt");
  const [editingStoryboardBody, setEditingStoryboardBody] = useState(false);
  const [activeAnalysisTask, setActiveAnalysisTask] = useState<Task | null>(null);
  const [analysisProgress, setAnalysisProgress] = useState({ story: 0, asset: 0 });
  const [analysisNow, setAnalysisNow] = useState(Date.now());
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);
  const [mentionCaret, setMentionCaret] = useState(0);
  const [storyboardAssetSearch, setStoryboardAssetSearch] = useState("");
  const [storyboardAssetLayout, setStoryboardAssetLayout] = useState<"list" | "grid">("list");
  const [collapsedStoryboardAssetGroups, setCollapsedStoryboardAssetGroups] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [storyboardLoading, setStoryboardLoading] = useState(false);
  const [generatingAssetId, setGeneratingAssetId] = useState<string | null>(null);
  const [message, setMessage] = useState("准备就绪");
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const promptInputRef = useRef<HTMLTextAreaElement | null>(null);
  const storyboardPatchQueue = useRef<Promise<void>>(Promise.resolve());
  const assetImageInputRef = useRef<HTMLInputElement | null>(null);
  const pollingTaskIds = useRef(new Set<string>());

  const selectedEpisode = useMemo(
    () => episodes.find((episode) => episode.id === selectedEpisodeId) ?? null,
    [episodes, selectedEpisodeId]
  );
  const selectedAsset = useMemo(
    () => assets.find((asset) => asset.id === selectedAssetId) ?? assets[0] ?? null,
    [assets, selectedAssetId]
  );
  const selectedStoryboard = useMemo(
    () => storyboards.find((storyboard) => storyboard.id === selectedStoryboardId) ?? storyboards[0] ?? null,
    [storyboards, selectedStoryboardId]
  );
  const assetGroups = useMemo(() => {
    const map = new Map<string, AssetGroup>();
    assets.forEach((asset) => {
      const key = mainAssetKey(asset);
      const existing = map.get(key);
      if (existing) {
        existing.items.push(asset);
      } else {
        map.set(key, {
          key,
          type: asset.asset_type,
          name: asset.name,
          localizedName: asset.localized_name,
          items: [asset],
        });
      }
    });
    return Array.from(map.values());
  }, [assets]);
  const filteredGroups = useMemo(
    () => assetGroups.filter((group) => assetTab === "资产进度" || group.type === assetTab),
    [assetGroups, assetTab]
  );
  const roleCount = assets.filter((asset) => asset.asset_type === "角色").length;
  const sceneCount = assets.filter((asset) => asset.asset_type === "场景").length;
  const propCount = assets.filter((asset) => asset.asset_type === "道具").length;
  const selectedGroup = selectedAsset ? assetGroups.find((group) => group.key === mainAssetKey(selectedAsset)) : null;
  const latestAssetIndex = useMemo(() => {
    const task = tasks.find((item) => item.task_type === "asset_extraction" && item.status === "成功" && item.result?.资产出现片段索引);
    return Array.isArray(task?.result?.资产出现片段索引) ? task.result.资产出现片段索引 : [];
  }, [tasks]);
  const selectedStoryboardAssets = useMemo(() => {
    if (!selectedStoryboard) return [];
    const storyboardRange = parseRange(selectedStoryboard.time_range || "");
    const variableSet = new Set<string>();
    latestAssetIndex.forEach((segment: any) => {
      const segmentRange = parseRange(`${segment.开始时间 || "00:00:00"}-${segment.结束时间 || segment.开始时间 || "00:00:00"}`);
      if (!rangesOverlap(storyboardRange, segmentRange)) return;
      const segmentAssets = Array.isArray(segment.出现资产) ? segment.出现资产 : splitAssetVariableValues(segment.出现资产);
      segmentAssets.forEach((item: any) => {
        if (typeof item === "string") splitAssetVariableValues(item).forEach((value: string) => variableSet.add(value));
        splitAssetVariableValues(item?.资产变量).forEach((value: string) => variableSet.add(value));
        splitAssetVariableValues(item?.展示资产变量).forEach((value: string) => variableSet.add(value));
      });
      splitAssetVariableValues(segment.出现资产变量).forEach((value: string) => variableSet.add(value));
      splitAssetVariableValues(segment.引用资产变量).forEach((value: string) => variableSet.add(value));
    });
    const payloadRefs = selectedStoryboard.payload?.引用资产变量;
    splitAssetVariableValues(payloadRefs).forEach((value) => variableSet.add(value));
    const prompt = selectedStoryboard.prompt || "";
    assets.forEach((asset) => {
      if (prompt.includes(asset.variable) || prompt.includes(assetDisplayVariable(asset))) {
        variableSet.add(asset.variable);
        variableSet.add(assetDisplayVariable(asset));
      }
    });
    return assets.filter((asset) => variableSet.has(asset.variable) || variableSet.has(assetDisplayVariable(asset)));
  }, [assets, latestAssetIndex, selectedStoryboard]);
  const selectedStoryboardPrompt = useMemo(
    () => convertPromptVariables(
      String(selectedStoryboard?.payload?.["分镜生成正文"] ?? selectedStoryboard?.payload?.["源分镜文本"] ?? selectedStoryboard?.prompt ?? ""),
      assets
    ),
    [assets, selectedStoryboard]
  );
  useEffect(() => setEditingStoryboardBody(false), [selectedStoryboardId]);
  const mentionOptions = useMemo(() => {
    const query = (mentionQuery ?? "").toLowerCase();
    return selectedStoryboardAssets.filter((asset) => {
      if (!query) return true;
      return `${assetDisplayName(asset)} ${assetDisplayVariable(asset)}`.toLowerCase().includes(query);
    });
  }, [mentionQuery, selectedStoryboardAssets]);
  const isSingleUpload = selectedSource?.kind === "local" && selectedSource.uploadMode === "single";
  const sourceEpisodeCount = isSingleUpload
    ? 1
    : selectedSource?.kind === "local"
      ? Math.max(episodes.length, selectedFiles?.length ?? 0)
      : Math.max(episodes.length, 40);
  const uploadedEpisodeCount = episodes.filter((episode) => episode.ark_file_id || episode.status.includes("已上传")).length;
  const hasUploadedEpisodes = uploadedEpisodeCount > 0;
  const effectiveUploadState = hasUploadedEpisodes ? "uploaded" : uploadState;
  const failedEpisodeCount = effectiveUploadState === "failed" ? Math.max(1, (selectedFiles?.length ?? 0) - uploadedEpisodeCount) : 0;
  const uploadingEpisodeCount = effectiveUploadState === "uploading" ? Math.max(sourceEpisodeCount - uploadedEpisodeCount, 0) : 0;

  function pickDefaultEpisodeId(items: Episode[], currentId: string | null) {
    if (currentId && items.some((episode) => episode.id === currentId)) return currentId;
    const candidates = [...items].sort((a, b) => {
      const sizeDelta = (b.size_bytes ?? 0) - (a.size_bytes ?? 0);
      if (sizeDelta !== 0) return sizeDelta;
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    });
    return candidates[0]?.id ?? null;
  }

  async function loadProjects() {
    const data = await request<{ projects: Project[] }>("/api/projects");
    setProjects(data.projects);
  }

  async function selectProject(projectId: string) {
    const data = await request<{ project: Project; episodes: Episode[] }>(`/api/projects/${projectId}`);
    const defaultEpisodeId = pickDefaultEpisodeId(data.episodes, selectedEpisodeId);
    setCurrentProject(data.project);
    setTargetCountry(data.project.target_country);
    const savedLanguage = data.project.target_language || "英语";
    setTargetLanguage(({ English: "英语", Japanese: "日语", Korean: "韩语", Spanish: "西班牙语", French: "法语" } as Record<string, string>)[savedLanguage] || savedLanguage);
    setRemakeMode(data.project.remake_mode ?? "overseas_localization");
    setSourceLanguage(data.project.source_language ?? "");
    setStoryboardVisualStyle(data.project.storyboard_visual_style ?? "live_action");
    setReplaceCharacters(Boolean(data.project.replace_characters));
    setReplaceScenes(Boolean(data.project.replace_scenes));
    setEpisodes(data.episodes);
    if (data.episodes.some((episode) => episode.ark_file_id || episode.status.includes("已上传"))) {
      setUploadState("uploaded");
    }
    setSelectedEpisodeId(defaultEpisodeId);
    const [, , , loadedTasks] = await Promise.all([loadAssets(projectId), loadStoryboards(projectId, defaultEpisodeId), loadGenerations(projectId), loadTasks(projectId)]);
    const recoveryTaskId = sessionStorage.getItem("activeAnalysisTaskId");
    const storedRecoveryTask = loadedTasks.find((task) => task.id === recoveryTaskId);
    const recentRunningTask = loadedTasks.find((task) =>
      task.episode_id === defaultEpisodeId
      && task.task_type === "asset_extraction"
      && isTaskRunning(task)
      && Date.now() - new Date(task.created_at).getTime() < 30 * 60 * 1000
    );
    const recoveryTask = storedRecoveryTask || recentRunningTask;
    if (recoveryTask) {
      if (isTaskRunning(recoveryTask)) sessionStorage.setItem("activeAnalysisTaskId", recoveryTask.id);
      setActiveAnalysisTask(recoveryTask);
      setScreen("analysis");
      setAnalysisProgress(isTaskRunning(recoveryTask) ? { story: 8, asset: 8 } : { story: 100, asset: 100 });
      if (isTaskRunning(recoveryTask)) {
        setMessage(recoveryTask.message || "后台分析任务仍在运行");
        if (!pollingTaskIds.current.has(recoveryTask.id)) {
          pollingTaskIds.current.add(recoveryTask.id);
          void pollTask(recoveryTask.id, async (doneTask) => {
            setActiveAnalysisTask(doneTask);
            await Promise.all([loadAssets(projectId), loadTasks(projectId)]);
            setAnalysisProgress({ story: 100, asset: 100 });
            setMessage(doneTask.status === "失败" ? doneTask.message : "分析完成，可以进入设定确认资产");
            sessionStorage.removeItem("activeAnalysisTaskId");
            pollingTaskIds.current.delete(doneTask.id);
          });
        }
      } else {
        setMessage(recoveryTask.status === "失败" ? recoveryTask.message : "分析完成，可以进入设定确认资产");
        sessionStorage.removeItem("activeAnalysisTaskId");
      }
    }
  }

  function clearCurrentWorkspace() {
    setEpisodes([]);
    setAssets([]);
    setStoryboards([]);
    setGenerations([]);
    setTasks([]);
    setSelectedEpisodeId(null);
    setSelectedAssetId(null);
    setSelectedStoryboardId(null);
    setActiveAnalysisTask(null);
    setStoryboardLoading(false);
    setAnalysisProgress({ story: 0, asset: 0 });
  }

  async function createProjectForUpload(title: string) {
    const data = await request<{ project: Project }>("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, targetCountry, targetLanguage, remakeMode, sourceLanguage, storyboardVisualStyle: remakeMode === "cg_3d" ? "cg_3d" : storyboardVisualStyle, replaceCharacters, replaceScenes }),
    });
    setStoryboardVisualStyle(data.project.storyboard_visual_style ?? "live_action");
    setCurrentProject(data.project);
    clearCurrentWorkspace();
    setProjects((items) => [data.project, ...items.filter((item) => item.id !== data.project.id)]);
    return data.project;
  }

  async function loadAssets(projectId = currentProject?.id) {
    if (!projectId) return;
    const data = await request<{ assets: Asset[] }>(`/api/projects/${projectId}/assets`);
    setAssets(data.assets);
  }

  async function loadStoryboards(projectId = currentProject?.id, episodeId?: string | null) {
    if (!projectId) return;
    const epId = episodeId ?? selectedEpisodeId;
    const params = epId ? `?episode_id=${epId}` : "";
    const data = await request<{ storyboards: Storyboard[] }>(`/api/projects/${projectId}/storyboards${params}`);
    setStoryboards(data.storyboards);
  }

  async function loadGenerations(projectId = currentProject?.id) {
    if (!projectId) return;
    const data = await request<{ videoGenerations: VideoGeneration[] }>(`/api/projects/${projectId}/video-generations`);
    setGenerations(data.videoGenerations);
  }

  async function loadTasks(projectId = currentProject?.id) {
    if (!projectId) return [];
    const data = await request<{ tasks: Task[] }>(`/api/projects/${projectId}/tasks`);
    setTasks(data.tasks);
    return data.tasks;
  }

  function scheduleRefresh(projectId: string) {
    [2500, 8000, 20000, 45000, 90000, 180000, 300000].forEach((delay) => {
      window.setTimeout(() => selectProject(projectId).catch((error) => setMessage(error.message)), delay);
    });
  }

  function activeStep(): StepScreen {
    if (screen === "source" || screen === "analysis") return "home";
    return screen;
  }

  function showAssetOverview(tab: AssetTab) {
    setAssetTab(tab);
    setAssetView("overview");
  }

  function selectAssetCard(asset: Asset) {
    setSelectedAssetId(asset.id);
    setAssetTab(asset.asset_type as AssetTab);
    setAssetView("detail");
  }

  function openImage(url: string, title: string) {
    if (!url) return;
    setImageView({ url, title });
    setModal("image-view-modal");
  }

  function handleLocalFiles(files: FileList | null, uploadMode: UploadMode) {
    if (!files?.length) return;
    const fileItems = Array.from(files);
    const firstFile = fileItems[0];
    const title = uploadProjectTitle(fileItems, "未命名本地视频项目");
    setSelectedFiles(fileItems);
    setSelectedSource({
      kind: "local",
      uploadMode,
      title,
      meta: uploadMode === "single"
        ? `本地单个视频 · 1 集 · ${firstFile.name}`
        : `本地文件夹 · ${files.length} 个视频文件`,
    });
    setUploadState("selected");
    setMessage("素材已选择，请确认参数后开始上传");
  }

  async function createProject() {
    setBusy(true);
    try {
      const data = await request<{ project: Project }>("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "六年后再遇首富前男友", targetCountry, targetLanguage, remakeMode, sourceLanguage, storyboardVisualStyle: remakeMode === "cg_3d" ? "cg_3d" : storyboardVisualStyle, replaceCharacters, replaceScenes }),
      });
      setMessage("项目已创建");
      await loadProjects();
      await selectProject(data.project.id);
    } finally {
      setBusy(false);
    }
  }

  async function uploadEpisodes(files = selectedFiles) {
    if (selectedSource?.kind === "local" && !files?.length) {
      setMessage("未读取到本地视频文件，请重新选择文件夹后再上传");
      setModal(null);
      return;
    }
    setBusy(true);
    setUploadState("uploading");
    setScreen("source");
    setModal(null);
    try {
      const project = currentProject && !episodes.length
        ? currentProject
        : await createProjectForUpload(selectedSource?.title ?? "未命名本地视频项目");
      const body = new FormData();
      Array.from(files ?? []).forEach((file) => body.append("files", file));
      await request(`/api/projects/${project.id}/episodes/upload`, { method: "POST", body });
      setMessage("视频已保存并同步 Ark");
      await selectProject(project.id);
      setUploadState("uploaded");
    } catch (error) {
      setUploadState("failed");
      setMessage(error instanceof Error ? error.message : "上传失败");
    } finally {
      setBusy(false);
    }
  }

  async function syncArkFile(episodeId: string) {
    if (!currentProject) return;
    setBusy(true);
    try {
      const data = await request<{ ark: { file_id: string; error: string } }>(
        `/api/projects/${currentProject.id}/episodes/${episodeId}/sync-ark-file`,
        { method: "POST" }
      );
      setMessage(data.ark.file_id ? "Ark 文件同步已完成" : `Ark 文件同步失败：${data.ark.error}`);
      await selectProject(currentProject.id);
    } finally {
      setBusy(false);
    }
  }

  async function runBackendTask(taskType: "asset_extraction" | "storyboard_generation" | "video_generation" | "series_analysis") {
    if (!currentProject) return null;
    if (taskType !== "video_generation" && taskType !== "series_analysis" && !selectedEpisodeId) {
      setMessage("请先选择要处理的视频");
      return null;
    }
    const payload = taskType === "video_generation"
      ? { taskType, storyboardId: selectedStoryboard?.id ?? storyboards[0]?.id }
      : taskType === "series_analysis"
        ? { taskType }
        : { taskType, episodeId: selectedEpisodeId };
    const data = await request<{ task: Task }>(`/api/projects/${currentProject.id}/tasks/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setMessage(data.task.message);
    scheduleRefresh(currentProject.id);
    await loadTasks(currentProject.id);
    return data.task;
  }

  async function pollTask(
    taskId: string,
    onDone: (task: Task) => Promise<void> | void,
    onProgress?: (task: Task) => Promise<void> | void
  ) {
    let consecutiveErrors = 0;
    for (let index = 0; index < 2400; index += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 3000));
      try {
        const data = await request<{ task: Task }>(`/api/tasks/${taskId}`);
        consecutiveErrors = 0;
        setActiveAnalysisTask(data.task);
        setMessage(data.task.message);
        await onProgress?.(data.task);
        if (!isTaskRunning(data.task)) {
          await onDone(data.task);
          sessionStorage.removeItem("activeAnalysisTaskId");
          pollingTaskIds.current.delete(taskId);
          return;
        }
      } catch {
        consecutiveErrors += 1;
        if (consecutiveErrors >= 5) {
          setMessage(`轮询暂时中断：连续 ${consecutiveErrors} 次请求失败，后台任务状态未知，正在继续重试。`);
          consecutiveErrors = 0;
        }
      }
    }
  }

  async function startAnalysis(mode: "normal" | "aigc") {
    if (!currentProject || !selectedEpisodeId) {
      setMessage("请先选择要分析的剧集");
      return;
    }
    setBusy(true);
    setScreen("analysis");
    setAnalysisProgress({ story: 4, asset: mode === "aigc" ? 8 : 4 });
    setMessage(mode === "aigc" ? "正在创建 AIGC 关联分析任务" : "正在创建当前集分析任务");
    setActiveAnalysisTask({
      id: "pending-analysis",
      project_id: currentProject.id,
      episode_id: selectedEpisodeId,
      task_type: "asset_extraction",
      status: "运行中",
      message: "正在创建当前集分析任务",
      created_at: "",
      updated_at: "",
    });
    try {
      const task = await runBackendTask("asset_extraction");
      if (!task) {
        setMessage("任务创建失败，请重试");
        setBusy(false);
        return;
      }
      setAnalysisProgress({ story: 8, asset: mode === "aigc" ? 12 : 8 });
      setMessage(mode === "aigc" ? "正在关联 AIGC 资产并分析" : "正在执行普通视频分析");
      setActiveAnalysisTask(task);
      sessionStorage.setItem("activeAnalysisTaskId", task.id);
      setBusy(false);
      pollingTaskIds.current.add(task.id);
      void pollTask(task.id, async (doneTask) => {
        setActiveAnalysisTask(doneTask);
        if (currentProject) await Promise.all([loadAssets(currentProject.id), loadTasks(currentProject.id)]);
        if (doneTask.status === "失败") {
          setMessage(doneTask.message || "分析失败，请重试");
          setAnalysisProgress({ story: 100, asset: 100 });
          return;
        }
        setAnalysisProgress({ story: 100, asset: 100 });
        const degraded = doneTask.result?.自动降级;
        const modelError = doneTask.result?.模型错误;
        if (degraded) {
          setMessage("已自动切换 mock 模式：" + (doneTask.result?.降级原因 || "外部服务不可用"));
        } else if (modelError) {
          setMessage("方舟分析超时，已先生成基础可编辑资产；可继续进入设定，或稍后重试真实分析：" + String(modelError).slice(0, 80));
        } else {
          setMessage("分析完成，可以进入设定确认资产");
        }
      });
    } catch (error) {
      setBusy(false);
      setMessage(error instanceof Error ? error.message : "任务启动失败");
    }
  }

  async function startSeriesAnalysis(mode: "normal" | "aigc" = "normal") {
    if (!currentProject || !episodes.length) {
      setMessage("请先上传剧集后再分析整剧");
      return;
    }
    setBusy(true);
    setScreen("analysis");
    setAnalysisProgress({ story: 4, asset: mode === "aigc" ? 8 : 4 });
    setMessage(mode === "aigc" ? "正在创建整剧 AIGC 关联分析任务" : "正在创建整剧分析任务");
    setActiveAnalysisTask({
      id: "pending-series-analysis",
      project_id: currentProject.id,
      episode_id: null,
      task_type: "series_analysis",
      status: "运行中",
      message: "正在创建整剧分析任务",
      created_at: "",
      updated_at: "",
    });
    try {
      const task = await runBackendTask("series_analysis");
      if (!task) {
        setMessage("整剧分析任务创建失败，请重试");
        setBusy(false);
        return;
      }
      setScreen("analysis");
      setAnalysisProgress({ story: 6, asset: mode === "aigc" ? 10 : 6 });
      setMessage(mode === "aigc" ? "正在逐集关联 AIGC 资产并分析整剧" : "正在逐集分析整剧，资产将按项目去重");
      setActiveAnalysisTask(task);
      setBusy(false);
      void pollTask(
        task.id,
        async (doneTask) => {
          setActiveAnalysisTask(doneTask);
          if (currentProject) {
            await Promise.all([
              loadAssets(currentProject.id),
              loadStoryboards(currentProject.id),
              loadTasks(currentProject.id),
              selectProject(currentProject.id),
            ]);
          }
          setAnalysisProgress({ story: 100, asset: 100 });
          if (doneTask.status === "失败") {
            setMessage(doneTask.message || "整剧分析失败，请重试");
            return;
          }
          const total = doneTask.result?.总集数 ?? episodes.length;
          const success = doneTask.result?.成功集数 ?? 0;
          const failed = doneTask.result?.失败集数 ?? 0;
          setMessage(`整剧分析完成：共 ${total} 集，成功 ${success} 集，失败 ${failed} 集`);
        },
        (runningTask) => {
          const result = runningTask.result ?? {};
          const total = Number(result.总集数 || episodes.length || 1);
          const current = Number(result.当前序号 || result.成功集数 || 1);
          const stage = String(result.当前阶段 || "");
          const stageOffset = stage.includes("分镜") ? 0.7 : stage.includes("完成") ? 0.95 : stage.includes("资产") ? 0.35 : 0.15;
          const percent = Math.min(96, Math.max(8, Math.round((((current - 1) + stageOffset) / total) * 92 + 4)));
          setAnalysisProgress({ story: percent, asset: percent });
          const episodeLabel = result.当前集 ?? result.当前集编号;
          if (episodeLabel) {
            const episodeText = String(episodeLabel).includes("集") ? String(episodeLabel) : `第 ${episodeLabel} 集`;
            setMessage(`${stage || "正在分析"}${episodeText}（${current}/${total}）`);
          }
        }
      );
    } catch (error) {
      setBusy(false);
      setMessage(error instanceof Error ? error.message : "整剧分析任务启动失败");
    }
  }

  async function enterStoryboards() {
    if (!currentProject || busy) return;
    setBusy(true);
    setStoryboardLoading(true);
    try {
      const task = await runBackendTask("storyboard_generation");
      if (!task) {
        setStoryboardLoading(false);
        return;
      }
      setScreen("segments");
      setSegmentPanel("shot-prompt");
      setActiveAnalysisTask(task);
      setMessage("分镜正在生成，请稍候...");
      setBusy(false);
      void pollTask(task.id, async (doneTask) => {
        if (currentProject) await loadStoryboards(currentProject.id);
        setStoryboardLoading(false);
        if (doneTask.status === "失败") {
          setMessage(doneTask.message || "分镜生成失败，请重试");
        } else {
          setMessage("分镜生成完成");
        }
      });
    } catch (error) {
      setStoryboardLoading(false);
      setMessage(error instanceof Error ? error.message : "分镜生成失败");
    } finally {
      setBusy(false);
    }
  }

  async function runVideoGeneration() {
    if (!currentProject || busy) return;
    setBusy(true);
    try {
      const task = await runBackendTask("video_generation");
      if (task) {
        setScreen("compare");
        setMessage("模拟生成任务已记录；当前没有生成可播放视频");
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "视频生成失败");
    } finally {
      setBusy(false);
    }
  }

  async function confirmAsset(assetId: string) {
    if (!currentProject) return;
    setBusy(true);
    try {
      await request(`/api/projects/${currentProject.id}/assets/${assetId}/confirm`, { method: "POST" });
      setMessage("资产已确认");
      await loadAssets(currentProject.id);
    } finally {
      setBusy(false);
    }
  }

  async function selectAssetImageVersion(assetId: string, imageId: string) {
    if (!currentProject) return;
    setBusy(true);
    try {
      const data = await request<{ asset: Asset }>(`/api/projects/${currentProject.id}/assets/${assetId}/select-image`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ imageId }),
      });
      setAssets((items) => items.map((item) => (item.id === assetId ? data.asset : item)));
      setSelectedAssetId(assetId);
      setMessage("已切换当前采用版本");
    } finally {
      setBusy(false);
    }
  }

  async function generateAssetImage(assetId: string, prompt?: string) {
    if (!currentProject) return;
    setBusy(true);
    setGeneratingAssetId(assetId);
    try {
      await request(`/api/projects/${currentProject.id}/assets/${assetId}/generate-image`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
      });
      setMessage("资产图已生成");
      await loadAssets(currentProject.id);
    } finally {
      setGeneratingAssetId(null);
      setBusy(false);
    }
  }

  async function saveTargetAssetDescription(asset: Asset, description: string) {
    if (!currentProject) return;
    const data = await request<{ asset: Asset }>(`/api/projects/${currentProject.id}/assets/${asset.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ payload: { "目标生成描述": description, "目标资产来源": "手动生成" } }),
    });
    setAssets((items) => items.map((item) => item.id === asset.id ? data.asset : item));
    setMessage("目标生成描述已保存；受影响分镜标记为待更新");
  }

  async function uploadTargetAssetImage(file?: File) {
    if (!currentProject || !selectedAsset || !file) return;
    const body = new FormData();
    body.append("file", file);
    setBusy(true);
    try {
      const data = await request<{ asset: Asset }>(`/api/projects/${currentProject.id}/assets/${selectedAsset.id}/upload-image`, { method: "POST", body });
      setAssets((items) => items.map((item) => item.id === selectedAsset.id ? data.asset : item));
      setMessage("目标图已上传、保存并设为当前采用图");
    } finally { setBusy(false); }
  }

  async function applyProjectMode() {
    if (!currentProject) return;
    const body = { remakeMode, sourceLanguage, storyboardVisualStyle: remakeMode === "cg_3d" ? "cg_3d" : storyboardVisualStyle, replaceCharacters, replaceScenes, targetCountry, targetLanguage };
    setBusy(true);
    try {
      try {
        const data = await request<{ project: Project }>(`/api/projects/${currentProject.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        setCurrentProject(data.project);
        setStoryboardVisualStyle(data.project.storyboard_visual_style ?? "live_action");
        await Promise.all([loadAssets(currentProject.id), loadStoryboards(currentProject.id)]);
        setMessage("项目制作模式已更新；受影响分镜待更新");
      } catch (error) {
        if (!String(error).includes("409")) throw error;
        const data = await request<{ project: Project }>(`/api/projects/${currentProject.id}/derive`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: `${currentProject.title}（新制作模式）`, ...body }) });
        await loadProjects();
        await selectProject(data.project.id);
        setMessage("已另存为新制作项目；源视频、解析与分镜已复用，旧目标图未复制");
      }
    } finally { setBusy(false); }
  }

  async function saveStoryboardVisualStyle(style: "live_action" | "cg_3d") {
    if (!currentProject) return;
    const data = await request<{ project: Project }>(`/api/projects/${currentProject.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ storyboardVisualStyle: style }) });
    setStoryboardVisualStyle(data.project.storyboard_visual_style ?? "live_action");
    setCurrentProject(data.project);
    await loadStoryboards(currentProject.id);
    setMessage("分镜画面类型已更新，既有源拆解保留；提示词已标记待更新");
  }

  async function savePrompt(storyboard: Storyboard, prompt: string) {
    if (!currentProject) return;
    const queueTask = storyboardPatchQueue.current.catch(() => undefined).then(async () => {
      const data = await request<{ storyboard: Storyboard }>(
        `/api/projects/${currentProject.id}/storyboards/${storyboard.id}`,
        { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ generatedBody: prompt }) }
      );
      setStoryboards((items) => items.map((item) => (item.id === storyboard.id ? data.storyboard : item)));
    });
    storyboardPatchQueue.current = queueTask.catch(() => undefined);
    try {
      await queueTask;
      setMessage("提示词已保存");
    } catch (error) {
      setMessage(`提示词保存失败，当前编辑保留：${error instanceof Error ? error.message : "请重试"}`);
    }
  }

  async function toggleStoryboardAsset(asset: Asset) {
    if (!currentProject || !selectedStoryboard) return;
    const payload = { ...(selectedStoryboard.payload ?? {}) };
    const refs = splitAssetVariableValues(payload["引用资产变量"]);
    const variable = asset.variable || assetDisplayVariable(asset);
    const aliases = new Set([asset.variable, assetDisplayVariable(asset), assetDisplayName(asset)]);
    const wasReferenced = refs.some((item) => aliases.has(item));
    const nextRefs = wasReferenced ? refs.filter((item) => !aliases.has(item)) : [...refs, variable];
    const queueTask = storyboardPatchQueue.current.catch(() => undefined).then(async () => {
      const data = await request<{ storyboard: Storyboard }>(`/api/projects/${currentProject.id}/storyboards/${selectedStoryboard.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payload: { "引用资产变量": nextRefs } }),
      });
      setStoryboards((items) => items.map((item) => item.id === data.storyboard.id ? data.storyboard : item));
    });
    storyboardPatchQueue.current = queueTask.catch(() => undefined);
    try {
      await queueTask;
      setMessage(wasReferenced ? `已移除引用 ${assetDisplayName(asset)}` : `已引用资产 ${assetDisplayName(asset)}`);
    } catch (error) {
      setMessage(`资产引用保存失败：${error instanceof Error ? error.message : "请重试"}`);
    }
  }

  async function createStoryboard(duplicateFrom?: Storyboard) {
    if (!currentProject || !selectedEpisodeId) return;
    const body = duplicateFrom
      ? { episodeId: selectedEpisodeId, duplicateFromId: duplicateFrom.id }
      : {
          episodeId: selectedEpisodeId,
          title: `分镜_${String(storyboards.length + 1).padStart(3, "0")}`,
          timeRange: selectedStoryboard?.time_range ?? "00:00:00-00:00:08",
        };
    const data = await request<{ storyboard: Storyboard }>(`/api/projects/${currentProject.id}/storyboards`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await loadStoryboards(currentProject.id, selectedEpisodeId);
    setSelectedStoryboardId(data.storyboard.id);
    setSegmentPanel("shot-prompt");
    setMessage(duplicateFrom ? "分镜已复制" : "分镜已新增");
  }

  async function deleteStoryboard(storyboard: Storyboard) {
    if (!currentProject) return;
    if (!window.confirm(`确认删除 ${storyboard.title}？关联的生成记录也会移除。`)) return;
    await request(`/api/projects/${currentProject.id}/storyboards/${storyboard.id}`, { method: "DELETE" });
    const next = storyboards.find((item) => item.id !== storyboard.id);
    setSelectedStoryboardId(next?.id ?? null);
    await loadStoryboards(currentProject.id, selectedEpisodeId);
    setMessage("分镜已删除");
  }

  async function moveStoryboard(storyboard: Storyboard, direction: "up" | "down") {
    if (!currentProject) return;
    await request<{ storyboard: Storyboard }>(`/api/projects/${currentProject.id}/storyboards/${storyboard.id}/move`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction }),
    });
    await loadStoryboards(currentProject.id, selectedEpisodeId);
    setSelectedStoryboardId(storyboard.id);
  }

  async function renameStoryboard(storyboard: Storyboard) {
    if (!currentProject) return;
    const title = window.prompt("分镜名称", storyboard.title);
    if (title === null || !title.trim()) return;
    const timeRange = window.prompt("时间范围", storyboard.time_range);
    if (timeRange === null || !timeRange.trim()) return;
    const data = await request<{ storyboard: Storyboard }>(
      `/api/projects/${currentProject.id}/storyboards/${storyboard.id}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title.trim(), timeRange: timeRange.trim() }),
      }
    );
    setStoryboards((items) => items.map((item) => (item.id === storyboard.id ? data.storyboard : item)));
    setSelectedStoryboardId(data.storyboard.id);
    setMessage("分镜信息已更新");
  }

  function updateStoryboardPromptText(value: string) {
    if (!selectedStoryboard) return;
    setStoryboards((items) => items.map((item) => item.id === selectedStoryboard.id
      ? { ...item, payload: { ...(item.payload ?? {}), "分镜生成正文": value } }
      : item));
  }

  function handlePromptInput(event: ChangeEvent<HTMLTextAreaElement>) {
    const value = event.target.value;
    const caret = event.target.selectionStart ?? value.length;
    updateStoryboardPromptText(value);
    const beforeCaret = value.slice(0, caret);
    const match = beforeCaret.match(/@([A-Za-z0-9_\u4e00-\u9fa5]*)$/);
    if (match) {
      setMentionQuery(match[1] ?? "");
      setMentionCaret(caret);
    } else {
      setMentionQuery(null);
    }
  }

  function insertMention(asset: Asset) {
    if (!selectedStoryboard || mentionQuery === null) return;
    const value = selectedStoryboardPrompt;
    const tokenStart = Math.max(0, mentionCaret - mentionQuery.length - 1);
    const nextValue = `${value.slice(0, tokenStart)}${assetDisplayVariable(asset)} ${value.slice(mentionCaret)}`;
    updateStoryboardPromptText(nextValue);
    setMentionQuery(null);
    window.setTimeout(() => {
      promptInputRef.current?.focus();
      const nextCaret = tokenStart + assetDisplayVariable(asset).length + 1;
      promptInputRef.current?.setSelectionRange(nextCaret, nextCaret);
    }, 0);
  }

  useEffect(() => {
    loadProjects().catch((error) => setMessage(error.message));
  }, []);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 1100px)");
    const sync = () => {
      if (media.matches) { setAssetSidebarCollapsed(true); setPreviewPanelCollapsed(true); }
    };
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (assets.length && (!selectedAssetId || !assets.some((asset) => asset.id === selectedAssetId))) {
      setSelectedAssetId(assets[0].id);
    }
  }, [assets, selectedAssetId]);

  useEffect(() => {
    setTargetAssetDescription(String(selectedAsset?.payload?.["目标生成描述"] ?? ""));
  }, [selectedAsset?.id, selectedAsset?.payload?.["目标生成描述"]]);

  useEffect(() => {
    if (storyboards.length && (!selectedStoryboardId || !storyboards.some((item) => item.id === selectedStoryboardId))) {
      setSelectedStoryboardId(storyboards[0].id);
    }
  }, [storyboards, selectedStoryboardId]);

  useEffect(() => {
    if (currentProject && screen === "segments") {
      setStoryboardLoading(false);
      loadStoryboards(currentProject.id);
    }
  }, [selectedEpisodeId]);

  useEffect(() => {
    document.body.dataset.screen = screen;
  }, [screen]);

  useEffect(() => {
    if (screen !== "analysis" || !isTaskRunning(activeAnalysisTask)) return;
    const timer = window.setInterval(() => setAnalysisNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [screen, activeAnalysisTask?.id, activeAnalysisTask?.status]);

  const displayStyleType = remakeMode === "cg_3d" || storyboardVisualStyle === "cg_3d" ? "3DCG 画面" : "真人画面";
  const paramSummary = `${frameRatio} · ${resolution}`;
  const analysisFailed = activeAnalysisTask && ["失败", "failed"].includes(activeAnalysisTask.status);
  const analysisDone = activeAnalysisTask && (analysisFailed || ["成功", "完成", "completed"].includes(activeAnalysisTask.status));
  const analysisProgressIsIndeterminate = Boolean(isTaskRunning(activeAnalysisTask) && activeAnalysisTask?.task_type !== "series_analysis");
  const analysisElapsedSeconds = activeAnalysisTask?.created_at
    ? Math.max(0, Math.floor((analysisNow - new Date(activeAnalysisTask.created_at).getTime()) / 1000))
    : 0;
  const analysisIsDegraded =
    analysisDone && !analysisFailed &&
    (Boolean(activeAnalysisTask?.result?.自动降级) || Boolean(activeAnalysisTask?.result?.模型错误));
  const analysisWarning =
    activeAnalysisTask?.result?.降级原因
    || (activeAnalysisTask?.result?.模型错误 ? "方舟分析超时，当前为基础可编辑资产：" + String(activeAnalysisTask.result.模型错误).slice(0, 120) : null);
  const selectedAssetGeneratedImage = selectedAssetImageUrl(selectedAsset);
  const selectedAssetOriginalImage = originalAssetImageUrl(selectedAsset);
  const selectedAssetCurrentImage = selectedAsset?.payload?.effectiveAssetSource === "source"
    ? selectedAssetOriginalImage
    : selectedAssetGeneratedImage;
  const selectedImageVersion = selectedAssetImage(selectedAsset);
  const isGeneratingSelectedAsset = Boolean(selectedAsset && generatingAssetId === selectedAsset.id);

  return (
    <>
      <header className="app-top">
        <div className="project">
          <button className="back-btn" onClick={() => setScreen("home")}>‹</button>
          <div className="home-brand">视频重制工坊</div>
          <div className="project-copy">
          <div className="project-title" onClick={async () => { if (!currentProject) return; const title = window.prompt("项目名称", currentProject.title); if (title === null || !title.trim()) return; const data = await request<{ project: Project }>(`/api/projects/${currentProject.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: title.trim() }) }); setCurrentProject(data.project); await loadProjects(); }}>{currentProject?.title ?? "未命名项目"} <span>✎</span></div>
          <div className="project-meta">{frameRatio}　｜　{resolution}</div>
          </div>
        </div>
        <nav className="steps">
          {[
            ["home", "▻", "原片"],
            ["assets", "▣", "设定"],
            ["segments", "▦", "分镜"],
            ["compare", "⌁", "视频"],
          ].map(([id, icon, label]) => (
            <button className={activeStep() === id ? "step active" : "step"} key={id} onClick={() => setScreen(id as StepScreen)}>
              <span>{icon}</span>{label}
            </button>
          ))}
        </nav>
      </header>

      <main>
        <section className={screen === "home" ? "screen active home-screen" : "screen home-screen"} id="home">
          <div className="upload-card">
            <div className="upload-heading">上传本地视频</div>

            {!selectedSource && (
              <section className="source-empty">
                <div className="drop-icon">▣</div>
                <b>上传本地视频</b>
                <span>可上传整部短剧文件夹，也可先上传单个视频。确认素材后再开始上传。</span>
                <div className="empty-actions">
                  <button className="primary" disabled={busy} onClick={() => folderInputRef.current?.click()}>上传文件夹</button>
                  <button className="ghost" disabled={busy} onClick={() => fileInputRef.current?.click()}>上传单个视频</button>
                </div>
              </section>
            )}

            {selectedSource && (
              <section className="selected-source">
                <div>
                  <span>本地视频</span>
                  <b>{selectedSource.title}</b>
                  <small>{selectedSource.meta}</small>
                </div>
                <button className="ghost" onClick={() => {
                  setSelectedSource(null);
                  setSelectedFiles(null);
                  setUploadState("idle");
                }}>重新选择</button>
              </section>
            )}

            {effectiveUploadState === "uploaded" && (
              <section className="source-summary">
                <article><span>文件夹名称</span><b>{selectedSource?.title ?? currentProject?.title ?? "未选择"}</b></article>
                <article><span>总集数</span><b>{episodes.length || sourceEpisodeCount} 集</b></article>
                <article><span>上传状态</span><b>{uploadedEpisodeCount || episodes.length || sourceEpisodeCount}/{episodes.length || sourceEpisodeCount} 已完成</b></article>
              </section>
            )}

            <div className="config-row">
              <label className="select-field">画面类型<select value={storyboardVisualStyle ?? "live_action"} onChange={(event) => { const style = event.target.value as "live_action" | "cg_3d"; setStoryboardVisualStyle(style); setRemakeMode(style === "cg_3d" ? "cg_3d" : targetLanguage === "沿用原片语言" ? "asset_replacement" : "overseas_localization"); }}><option value="live_action">真人</option><option value="cg_3d">3D</option></select></label>
              <label className="select-field">地区语言<select value={targetLanguage} onChange={(event) => { const language = event.target.value; setTargetLanguage(language); setTargetCountry(language === "沿用原片语言" ? "中国" : language === "日语" ? "日本" : language === "韩语" ? "韩国" : language === "英语" ? "美国" : "海外"); setRemakeMode(storyboardVisualStyle === "cg_3d" ? "cg_3d" : language === "沿用原片语言" ? "asset_replacement" : "overseas_localization"); setReplaceCharacters(language === "沿用原片语言"); setReplaceScenes(language === "沿用原片语言"); }}><option value="沿用原片语言">国内（原片语言）</option><option value="英语">海外·英语</option><option value="日语">海外·日语</option><option value="韩语">海外·韩语</option><option value="西班牙语">海外·西班牙语</option><option value="法语">海外·法语</option></select></label>
              <div className="spacer"></div>
              <details className="param-sheet">
                <summary>生成参数 <b>{paramSummary}</b></summary>
                <div className="param-popover">
                  <div className="param-row"><span>画幅</span><div className="param-options">{["16:9", "9:16", "1:1"].map((item) => <button className={frameRatio === item ? "active" : ""} key={item} onClick={() => setFrameRatio(item)}>{item}</button>)}</div></div>
                  <div className="param-row"><span>清晰度</span><div className="param-options">{["480p", "720p", "1080p"].map((item) => <button className={resolution === item ? "active" : ""} key={item} onClick={() => setResolution(item)}>{item}</button>)}</div></div>
                  {remakeMode === "overseas_localization" && <div className="param-row"><span>风格</span><div className="param-options">{["写实通用", "欧美短剧", "广告质感"].map((item) => <button className={styleType === item ? "active" : ""} key={item} onClick={() => setStyleType(item)}>{item}</button>)}</div></div>}
                </div>
              </details>
              {currentProject && <button className="ghost" disabled={busy} onClick={applyProjectMode}>应用项目模式</button>}
              <button className="create-btn in-card" disabled={busy || !selectedSource} onClick={() => setModal("upload-confirm-modal")}>开始上传</button>
            </div>

            <input hidden multiple type="file" accept="video/*" ref={fileInputRef} onChange={(event: ChangeEvent<HTMLInputElement>) => {
              handleLocalFiles(event.target.files, "single");
              event.currentTarget.value = "";
            }} />
            <input hidden multiple type="file" accept="video/*" ref={folderInputRef} {...{ webkitdirectory: "" }} onChange={(event: ChangeEvent<HTMLInputElement>) => {
              handleLocalFiles(event.target.files, "folder");
              event.currentTarget.value = "";
            }} />
          </div>

          <section className="project-library">
            <div className="library-head">
              <div><h2>项目管理</h2><span>过往重绘项目</span></div>
              <div className="library-actions"><button className="ghost" onClick={createProject}>新建项目</button><button className="ghost" onClick={() => currentProject && selectProject(currentProject.id)}>刷新当前项目</button></div>
            </div>
            <div className="project-grid">
              {projects.slice(0, 4).map((project, index) => (
                <article className="project-card" key={project.id} onClick={() => {
                  void selectProject(project.id);
                  setScreen("source");
                }}>
                  <div className={`project-cover cover-${["a", "b", "c", "d"][index % 4]}`}></div>
                  <div className="project-info">
                  <span>{project.status}</span>
                  <span className="project-mode-tag">{project.remake_mode === "cg_3d" ? "3DCG 风格" : project.remake_mode === "asset_replacement" ? "替换角色/场景" : "海外本土化"}</span>
                    <b>{project.title}</b>
                  <small>{project.remake_mode === "overseas_localization" ? `${project.target_country} · ${project.target_language}` : `源语言：${project.source_language || "待确认"}`} · {project.id}</small>
                  </div>
                </article>
              ))}
            </div>
          </section>
        </section>

        <section className={screen === "source" ? "screen active page-screen" : "screen page-screen"} id="source">
          <div className="info-bar">
            {effectiveUploadState === "uploading" ? `正在上传《${selectedSource?.title ?? currentProject?.title ?? "当前项目"}》原片视频。上传完成后，再选择分析方式。` : effectiveUploadState === "failed" ? "上传失败，请返回重新选择或重试上传。" : "原片视频已上传完成，现在可以选择分析方式。"}
          </div>
          <div className="page-head">
            <h1>原片剧集</h1>
            <div>全部 {episodes.length || sourceEpisodeCount}　已上传 {uploadedEpisodeCount || (effectiveUploadState === "uploaded" ? episodes.length || sourceEpisodeCount : 0)}　上传中 {uploadingEpisodeCount}　失败 {failedEpisodeCount}</div>
          </div>
          <div className="table-card">
            <table>
              <thead><tr><th>剧集信息</th><th>时长</th><th>大小</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>
                {(episodes.length ? episodes : Array.from({ length: Math.min(sourceEpisodeCount, 7) }).map((_, index) => ({
                  id: `pending-${index}`,
                  episode_index: index + 1,
                  filename: selectedFiles?.[index]?.name ?? `${String(index + 1).padStart(3, "0")}.mp4`,
                  size_bytes: selectedFiles?.[index]?.size ?? 0,
                  status: effectiveUploadState,
                  source_url: "",
                  ark_file_id: "",
                  ark_file_status: "",
                  ark_file_error: "",
                  created_at: "",
                }))).slice(0, 20).map((episode) => (
                  <tr key={episode.id}>
                    <td><b>第 {episode.episode_index} 集</b><span>{episode.filename}</span></td>
                    <td>--:--</td>
                    <td>{formatBytes(episode.size_bytes)}</td>
                    <td><i className={`dot ${episode.ark_file_id || episode.status.includes("已上传") ? "green" : effectiveUploadState === "failed" ? "red" : "blue"}`}></i><span>{episode.ark_file_id || episode.status.includes("已上传") ? "上传成功" : effectiveUploadState === "uploading" ? "上传中" : effectiveUploadState === "failed" ? "上传失败" : "等待上传"}</span></td>
                    <td>{episode.id.startsWith("pending") ? <span>待上传</span> : <a onClick={() => syncArkFile(episode.id)}>同步 Ark</a>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {effectiveUploadState === "uploaded" && (
            <section className="analysis-choice source-analysis-choice">
              <div>
                <b>选择分析方式</b>
                <span>本地视频将通过抽帧与视觉理解进行普通分析。</span>
              </div>
              <div className="analysis-actions">
                <button className="primary" disabled={!selectedEpisodeId} onClick={() => selectedEpisodeId ? startAnalysis("normal") : setMessage("后台项目当前是演示链路；真实分析需要先上传一个本地视频生成剧集记录")}>分析当前集</button>
                <button className="primary" disabled={!episodes.length} onClick={() => startSeriesAnalysis("normal")}>分析全部剧集</button>
              </div>
            </section>
          )}
          <div className="footer-actions">
            <button className="ghost" onClick={() => setScreen("home")}>返回</button>
          </div>
        </section>

        <section className={screen === "analysis" ? "screen active page-screen analysis-screen" : "screen page-screen analysis-screen"} id="analysis">
          <div className="analysis-loading-card">
            {analysisFailed ? (
              <>
                <div className="analysis-failed-icon">✕</div>
                <h1>分析失败</h1>
                <p className="analysis-error-message">{activeAnalysisTask?.message || "分析过程中发生错误"}</p>
                <div className="analysis-failed-actions">
                  <button className="ghost" onClick={() => setScreen("source")}>返回上传页</button>
                  <button className="primary" onClick={() => startAnalysis("normal")}>重试分析</button>
                </div>
              </>
            ) : (
              <>
                <div className="loader-ring"></div>
                <h1>正在分析视频资源</h1>
                <p>{message}{isTaskRunning(activeAnalysisTask) && activeAnalysisTask?.task_type !== "series_analysis" && (
                  <small className="analysis-elapsed">已用时 {Math.floor(analysisElapsedSeconds / 60)} 分 {analysisElapsedSeconds % 60} 秒</small>
                )}</p>
                <div className="analysis-progress-list">
                  <article>
                    <div className="analysis-head"><b>剧情解析转化进度</b><span>{analysisProgressIsIndeterminate ? "处理中" : `${analysisProgress.story}%`}</span></div>
                    <div className={analysisProgressIsIndeterminate ? "progress indeterminate" : "progress"}><i style={analysisProgressIsIndeterminate ? undefined : { width: `${analysisProgress.story}%` }}></i></div>
                    <small>识别剧情段落、台词、镜头节奏，并转化为目标国家分镜基础描述。</small>
                  </article>
                  <article>
                    <div className="analysis-head"><b>资产解析转化进度</b><span>{analysisProgressIsIndeterminate ? "处理中" : `${analysisProgress.asset}%`}</span></div>
                    <div className={analysisProgressIsIndeterminate ? "progress indeterminate" : "progress"}><i style={analysisProgressIsIndeterminate ? undefined : { width: `${analysisProgress.asset}%` }}></i></div>
                    <small>通过视频抽帧和视觉理解分析角色、场景、道具与画面文字。</small>
                  </article>
                </div>
                <button className={analysisDone ? "primary" : "primary disabled"} disabled={!analysisDone} onClick={() => {
                  setScreen("assets");
                  showAssetOverview("角色");
                }}>进入设定</button>
                {!analysisDone && (
                  <button className="ghost" style={{ marginTop: 12 }} onClick={() => { sessionStorage.removeItem("activeAnalysisTaskId"); setScreen("source"); }}>取消并返回</button>
                )}
              </>
            )}
          </div>
        </section>

        <section className={screen === "assets" ? "screen active page-screen" : "screen page-screen"} id="assets">
          <div className="page-head">
            <h1>设定</h1>
            <div>角色 {roleCount} / 子资产 {roleCount}　场景 {sceneCount} / 子资产 {sceneCount}　道具 {propCount}</div>
          </div>
          <div className="module-tabs">
            {(["角色", "场景", "道具", "资产进度"] as AssetTab[]).map((tab) => (
              <button className={assetTab === tab ? "active" : ""} key={tab} onClick={() => showAssetOverview(tab)}>{tab}</button>
            ))}
          </div>

          {analysisIsDegraded && assetView !== "detail" && (
            <div className="degradation-banner">
              <span>⚠ {analysisWarning || "当前资产和分镜使用回退测试数据，不具备真实模型分析结果。"}</span>
            </div>
          )}
          {assetView !== "detail" && (
            <div className="asset-overview sub-panel active">
              <div className="overview-head">
                <h2>{assetTab === "资产进度" ? "资产进度" : `${assetTab}资产`}</h2>
                <div><button className="ghost" onClick={() => selectedEpisodeId && startAnalysis("normal")} disabled={busy || !selectedEpisodeId}>重提当前集</button><button className="ghost" onClick={() => startSeriesAnalysis("normal")} disabled={busy || !episodes.length}>重提整剧</button></div>
              </div>
              {assetTab === "资产进度" ? (
                <div className="sub-panel simple-panel active">
                  <div className="metric"><b>待确认</b><span>{assets.filter((asset) => asset.status !== "已确认").length} 个资产</span></div>
                  <div className="metric"><b>已确认</b><span>{assets.filter((asset) => asset.status === "已确认").length} 个子资产</span></div>
                  <div className="review-rule">确认资产后，可在分镜中检查并引用；未完成的目标资产会保留待确认状态。</div>
                </div>
              ) : (
                <div className="asset-grid overview-grid">
                  {filteredGroups.map((group) => {
                    const originalUrl = groupOriginalImageUrl(group);
                    const confirmedUrl = groupConfirmedImageUrl(group);
                    return (
                      <article className="asset-card" key={group.key} onClick={() => selectAssetCard(group.items[0])}>
                        <i>{group.items.every((item) => item.status === "已确认") ? "审核通过" : "待确认"}</i>
                        <div className="asset-card-visuals">
                          <figure>
                            {originalUrl ? (
                              <img className="asset-card-image" src={originalUrl} alt="原资产图" onClick={(event) => {
                                event.stopPropagation();
                                openImage(originalUrl, `${group.name} 原资产图`);
                              }} />
                            ) : (
                              <div className={`thumb ${assetThumbClass(group.items[0])}`}></div>
                            )}
                            <em>原</em>
                            <figcaption>{group.name}</figcaption>
                          </figure>
                          <figure>
                            {confirmedUrl ? (
                              <img className="asset-card-image" src={confirmedUrl} alt="已确认目标资产图" onClick={(event) => {
                                event.stopPropagation();
                                openImage(confirmedUrl, `${group.localizedName} 已确认资产图`);
                              }} />
                            ) : (
                              <div className="asset-placeholder compact">待确认</div>
                            )}
                            <em>新</em>
                            <figcaption>{group.localizedName}</figcaption>
                          </figure>
                        </div>
                        <b>{group.name} → {group.localizedName}</b>
                        <span>{group.items.length} 个子资产 · {assetDisplayMeta(group.items[0])}</span>
                      </article>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {assetView === "detail" && selectedAsset && (
            <div className="asset-detail sub-panel active">
              <div className="detail-topbar">
                <button className="ghost" onClick={() => setAssetView("overview")}>返回{assetTab}概览</button>
                <h2>{selectedAsset.name} → {selectedAsset.localized_name}</h2>
                <button className="primary" onClick={() => confirmAsset(selectedAsset.id)} disabled={busy}>确认{assetTab}</button>
              </div>
              <div className="generation-layout">
                <section className="asset-source-panel">
                  <div className="asset-detail-summary">
                    <article><span>资产变量</span><b>{assetDisplayVariable(selectedAsset)}</b></article>
                    <article><span>出现集数</span><b>{Array.isArray(selectedAsset.payload?.出现集数) && selectedAsset.payload.出现集数.length ? `第 ${selectedAsset.payload.出现集数.join("、")} 集` : "当前集"}</b></article>
                    <article><span>剧情身份/功能</span><b>{pickText(selectedAsset.payload, ["剧情身份", "剧情功能"]) || "来自原片分析"}</b></article>
                    <article><span>当前状态</span><b>{selectedAsset.status}</b></article>
                  </div>
                  <div className="panel-title">子资产候选</div>
                  <div className="subasset-list compact-subasset-list">
                    {(selectedGroup?.items ?? [selectedAsset]).map((item) => (
                      <article className={item.id === selectedAsset.id ? "subasset-card active" : "subasset-card"} key={item.id} onClick={() => setSelectedAssetId(item.id)}>
                        <div className="subasset-thumb-shell">{selectedAssetImageUrl(item) || originalAssetImageUrl(item)
                          ? <img className="subasset-thumb" src={selectedAssetImageUrl(item) || originalAssetImageUrl(item) || ""} alt={`${variantName(item)}资产图`} />
                          : <span className="asset-empty-thumb">暂无图片</span>}</div>
                        <b>{variantName(item)}</b>
                        <span>{variantMeta(item) || assetDisplayVariable(item)}</span>
                      </article>
                    ))}
                  </div>
                  <div className="asset-compare">
                    <article>
                      {selectedAssetOriginalImage ? (
                        <img className="original-asset-image clickable-image" src={selectedAssetOriginalImage} alt="原资产图" onClick={() => openImage(selectedAssetOriginalImage, `${selectedAsset.name} 原资产图`)} />
                      ) : (
                        <div className="asset-empty-thumb asset-empty-large">暂无原资产图</div>
                      )}
                      <b>原资产图</b>
                      <span>源资产事实只读</span>
                      <p>{originalAssetDescription(selectedAsset)}</p>
                    </article>
                    <article>
                      {isGeneratingSelectedAsset ? (
                        <div className="generated-image-loading"><i></i><span>生成中</span></div>
                      ) : selectedAssetCurrentImage ? (
                        <img className="generated-asset-image clickable-image" src={selectedAssetCurrentImage} alt={selectedAsset?.payload?.effectiveAssetSource === "source" ? "当前沿用的原片资产图" : "目标资产图"} onClick={() => openImage(selectedAssetCurrentImage, `${selectedAsset.localized_name} 当前生效资产图`)} />
                      ) : (
                        <div className={`asset-placeholder ${assetThumbClass(selectedAsset)}`}>目标资产图</div>
                      )}
                      <b>{selectedAsset?.payload?.effectiveAssetSource === "source" ? "当前沿用原片资产" : "当前目标资产"}</b>
                      <span>{selectedAsset?.payload?.effectiveAssetSource === "source"
                        ? (selectedAssetCurrentImage ? "原片资产图 · 当前生效" : "缺少原片资产图")
                        : selectedImageVersion?.payload?.source === "user_upload" ? "用户上传 · 当前采用图" : selectedAssetGeneratedImage ? "当前采用图决定目标外观" : "缺少已确认目标图 · 替换未就绪"}</span>
                      <p>{selectedAsset?.payload?.effectiveAssetSource === "source"
                        ? (selectedAsset?.payload?.当前有效资产就绪 === false
                          ? `原片资产未就绪：缺少原片图或原片描述。${selectedAsset.payload?.当前有效外观描述 || ""}`
                          : `已关联原分镜，当前沿用原片外观：${selectedAsset.payload?.当前有效外观描述 || "原片描述未留存"}`)
                        : selectedAsset?.payload?.当前有效资产就绪 === false
                          ? "替换目标未就绪，请上传或生成并采用目标图。"
                          : "已关联原分镜，外观以当前目标图为准。"}</p>
                    </article>
                  </div>
                </section>
                <aside className="generation-panel asset-generation-studio">
                  <div className="studio-toolbar">
                    <div className="studio-title"><b>{variantName(selectedAsset)}</b></div>
                  </div>
                  <div className="studio-preview-row">
                    <div className="studio-preview-main">
                      {isGeneratingSelectedAsset ? (
                        <div className="studio-image-loading"><i></i><span>生成中</span></div>
                      ) : selectedAssetCurrentImage ? (
                        <img className="studio-generated-image clickable-image" src={selectedAssetCurrentImage} alt={selectedAsset?.payload?.effectiveAssetSource === "source" ? "当前沿用的原片资产图" : "当前目标资产图"} onClick={() => openImage(selectedAssetCurrentImage, `${variantName(selectedAsset)} 当前生效资产图`)} />
                      ) : (
                          <div className="asset-empty-thumb asset-empty-large">暂无当前采用图</div>
                      )}
                    </div>
                    <div className="studio-current">
                      <span>当前</span>
                      <div className="studio-current-thumb" onClick={() => selectedAssetCurrentImage && openImage(selectedAssetCurrentImage, "当前生效资产图")}>{isGeneratingSelectedAsset ? <i className="current-loading"></i> : selectedAssetCurrentImage ? <img src={selectedAssetCurrentImage} alt="当前生效资产图" /> : <><i></i><i></i></>}</div>
                      <div className="asset-version-list">
                        {(selectedAsset.images ?? []).map((image, index) => {
                          const imageUrl = apiAssetUrl(image.file_url || image.payload?.source_url);
                          return (
                            <button className={image.id === selectedImageVersion?.id ? "asset-version active" : "asset-version"} key={image.id} onClick={() => selectAssetImageVersion(selectedAsset.id, image.id)} disabled={busy}>
                              {imageUrl ? <img src={imageUrl} alt={`历史版本 ${index + 1}`} /> : <i></i>}
                              <span>v{(selectedAsset.images?.length ?? 0) - index}</span>
                            </button>
                          );
                        })}
                        {!selectedAsset.images?.length && <em>暂无历史版本</em>}
                      </div>
                    </div>
                  </div>
                  <div className="agent-generate-card">
                    <div className="agent-card-head"><h3>Agent 生成{assetTab === "角色" ? "形象" : assetTab}</h3></div>
                    <div className="agent-prompt-box">
                      {currentProject?.remake_mode !== "overseas_localization" && <div className="asset-target-controls">
                        <label>目标生成描述（可编辑）<textarea value={targetAssetDescription} onChange={(event) => setTargetAssetDescription(event.target.value)} onBlur={() => saveTargetAssetDescription(selectedAsset, targetAssetDescription).catch((error) => setMessage(error.message))} placeholder={originalAssetDescription(selectedAsset) === "原片描述未留存" ? "请补充目标外观描述" : "只编辑当前项目目标外观"} /></label>
                        <div><button className="ghost" onClick={() => assetImageInputRef.current?.click()} disabled={busy}>上传目标图</button>{currentProject?.remake_mode === "asset_replacement" && <button className="ghost" onClick={async () => { const data = await request<{ asset: Asset }>(`/api/projects/${currentProject.id}/assets/${selectedAsset.id}/restore-original`, { method: "POST" }); setAssets((items) => items.map((item) => item.id === selectedAsset.id ? data.asset : item)); setTargetAssetDescription(""); setMessage("已设为沿用原貌；受影响分镜待更新"); }}>沿用原貌</button>}</div>
                        <input ref={assetImageInputRef} hidden type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => { const file = event.currentTarget.files?.[0]; void uploadTargetAssetImage(file).catch((error) => setMessage(error.message)); event.currentTarget.value = ""; }} />
                      </div>}
                      <textarea value={assetPrompt(selectedAsset) || "当前资产未返回提示词"} readOnly />
                      <div className="agent-prompt-footer">
                        <div className="agent-model-note">按当前后端模型配置生成</div>
                        <button className="primary agent-cost" onClick={() => generateAssetImage(selectedAsset.id, currentProject?.remake_mode === "overseas_localization" ? undefined : targetAssetDescription)} disabled={busy || (currentProject?.remake_mode !== "overseas_localization" && !targetAssetDescription.trim())}>生成</button>
                      </div>
                    </div>
                  </div>
                </aside>
              </div>
            </div>
          )}

          <div className="footer-actions">
            <button className="ghost" onClick={() => setScreen("source")}>返回</button>
            <button className="primary" disabled={busy || !assets.length} onClick={enterStoryboards}>进入分镜</button>
          </div>
        </section>

        <section className={`screen workbench${screen === "segments" ? " active" : ""}${assetSidebarCollapsed ? " assets-collapsed" : ""}${previewPanelCollapsed ? " preview-collapsed" : ""}`} id="segments">
          <section className={`asset-sidebar${assetSidebarCollapsed ? " folded" : ""}`}>
            <details className="episode-select">
              <summary><b>{selectedEpisode ? `第 ${selectedEpisode.episode_index} 集` : "选择剧集"}</b><span>{episodes.length} 集已解析</span></summary>
              <div className="episode-menu">
                {episodes.map((episode) => (
                  <button className={episode.id === selectedEpisodeId ? "active" : ""} key={episode.id} onClick={() => setSelectedEpisodeId(episode.id)}>第 {episode.episode_index} 集 {episode.filename}</button>
                ))}
              </div>
            </details>
            <div className="inner-tabs compact-asset-tabs"><span className="sidepanel-label">资产库 · 点击引用到当前分镜</span></div>
            <input className="storyboard-asset-search" placeholder="搜索角色、场景、道具" value={storyboardAssetSearch} onChange={(event) => setStoryboardAssetSearch(event.target.value)} />
            <div className="storyboard-asset-view-controls"><span>显示</span><button className={storyboardAssetLayout === "list" ? "active" : ""} onClick={() => setStoryboardAssetLayout("list")}>列表</button><button className={storyboardAssetLayout === "grid" ? "active" : ""} onClick={() => setStoryboardAssetLayout("grid")}>网格</button></div>
            <div className={`asset-panel active storyboard-asset-list ${storyboardAssetLayout}`}>
              {["角色", "场景", "道具"].map((type) => <div className="storyboard-asset-group" key={type}>
                <button className="storyboard-asset-group-toggle" aria-expanded={!collapsedStoryboardAssetGroups.includes(type)} onClick={() => setCollapsedStoryboardAssetGroups((groups) => groups.includes(type) ? groups.filter((item) => item !== type) : [...groups, type])}>{type} <small>{assets.filter((asset) => asset.asset_type === type && `${assetDisplayName(asset)} ${assetDisplayVariable(asset)}`.toLowerCase().includes(storyboardAssetSearch.toLowerCase())).length}</small><span>{collapsedStoryboardAssetGroups.includes(type) ? "＋" : "−"}</span></button>
                {!collapsedStoryboardAssetGroups.includes(type) && <div className="storyboard-asset-group-items">
                {assets.filter((asset) => asset.asset_type === type && `${assetDisplayName(asset)} ${assetDisplayVariable(asset)}`.toLowerCase().includes(storyboardAssetSearch.toLowerCase())).map((asset) => {
                  const thumbUrl = selectedAssetImageUrl(asset) || originalAssetImageUrl(asset);
                  const refs = splitAssetVariableValues(selectedStoryboard?.payload?.["引用资产变量"]);
                  const aliases = [asset.variable, assetDisplayVariable(asset), assetDisplayName(asset)];
                  const isReferenced = aliases.some((alias) => refs.includes(alias)) || selectedStoryboardAssets.some((item) => item.id === asset.id);
                  return <button className={isReferenced ? "storyboard-asset-row referenced" : "storyboard-asset-row"} key={asset.id} disabled={!selectedStoryboard} onClick={() => toggleStoryboardAsset(asset)}>
                    {thumbUrl ? <img src={thumbUrl} alt="" /> : <span className="asset-empty-thumb">—</span>}<span><b>{assetDisplayName(asset)}</b><small>{assetDisplayVariable(asset)}</small></span><i>{isReferenced ? "已引用" : "引用"}</i>
                  </button>;
                })}
                </div>}
              </div>)}
              {!assets.length && <p className="empty-shot-assets">当前项目还没有可用资产。</p>}
            </div>
          </section>
          <section className="shot-main">
            <div className="shot-head"><h2>{selectedStoryboard ? selectedStoryboard.title : "暂无分镜"} <span>{selectedStoryboard?.time_range ?? "等待自动切分"}</span></h2><div className="shot-actions"><button className="ghost small" onClick={() => setAssetSidebarCollapsed((value) => !value)}>{assetSidebarCollapsed ? "显示资产库" : "收起资产库"}</button><button className="ghost small" onClick={() => setPreviewPanelCollapsed((value) => !value)}>{previewPanelCollapsed ? "显示预览" : "收起预览"}</button><button className="ghost small" disabled={!selectedStoryboard} onClick={() => selectedStoryboard && renameStoryboard(selectedStoryboard)}>编辑</button><button className="ghost small" disabled={!selectedStoryboard} onClick={() => selectedStoryboard && createStoryboard(selectedStoryboard)}>复制</button><button className="primary small" disabled={busy || !selectedStoryboard} onClick={runVideoGeneration}>生成模拟任务</button></div></div>
            {segmentPanel === "shot-prompt" && !selectedStoryboard && (
              storyboardLoading ? (
                <div className="shot-empty sub-panel active">
                  <div className="loader-ring"></div>
                  <b>正在生成分镜</b>
                  <span>系统正在调用 Agent 解析视频并生成分镜提示词，请稍候...</span>
                </div>
              ) : (
                <div className="shot-empty sub-panel active">
                  <b>当前项目还没有分镜</b>
                  <span>点击"进入分镜"后系统会根据资产出现片段自动生成；生成完成后这里会显示可编辑提示词。</span>
                </div>
              )
            )}
            {segmentPanel === "shot-prompt" && selectedStoryboard && (
              <div className="shot-text sub-panel active">
                <div className="prompt-body">
                  <div className="tool-row"><button onClick={() => setSegmentPanel("shot-assets")}>引用</button><button onClick={() => setEditingStoryboardBody((value) => !value)}>{editingStoryboardBody ? "完成编辑" : "编辑正文"}</button><span>保存后正文与原始反解文本分开保留。</span></div>
                  <div className="storyboard-ref-chips">{selectedStoryboardAssets.map((asset) => <button key={asset.id} title="点击移除该引用" onClick={() => toggleStoryboardAsset(asset)}>{assetDisplayName(asset)} <span>×</span></button>)}{!selectedStoryboardAssets.length && <span>当前分镜尚未关联资产；可从左侧资产库添加。</span>}</div>
                  <div className="prompt-editor-wrap">
                    {editingStoryboardBody
                      ? <textarea ref={promptInputRef} value={selectedStoryboardPrompt} onChange={handlePromptInput} onBlur={(event) => selectedStoryboard && savePrompt(selectedStoryboard, event.currentTarget.value)} />
                      : <div className="storyboard-body-view">{selectedStoryboardPrompt}</div>}
                    {mentionQuery !== null && (
                      <div className="mention-menu">
                        <b>引用资产</b>
                        {mentionOptions.map((asset) => (
                          <button key={asset.id} onMouseDown={(event) => {
                            event.preventDefault();
                            insertMention(asset);
                          }}>
                            <span className={`mention-thumb ${assetThumbClass(asset)}`}></span>
                            <span><strong>{assetDisplayName(asset)}</strong><small>{assetDisplayVariable(asset)}</small></span>
                          </button>
                        ))}
                        {!mentionOptions.length && <em>无匹配资产</em>}
                      </div>
                    )}
                  </div>
                </div>
                <div className="generation-mode-note">视频生成当前为模拟任务，尚未生成可播放视频。</div>
              </div>
            )}
            {segmentPanel === "shot-assets" && <div className="sub-panel simple-panel active">{selectedStoryboardAssets.map((asset) => <span className="chip" key={asset.id}>{assetDisplayVariable(asset)}</span>)}<button className="ghost full" onClick={() => setSegmentPanel("shot-prompt")}>返回提示词</button></div>}
            {segmentPanel === "shot-history" && <div className="sub-panel simple-panel active">{generations.slice(0, 5).map((item, index) => <div className="log-row" key={item.id}><b>第 {index + 1} 条</b><span>{mockGenerationLabel(item.status)}</span></div>)}<button className="ghost full" onClick={() => setSegmentPanel("shot-prompt")}>返回提示词</button></div>}
          </section>
          <aside className={`video-preview${previewPanelCollapsed ? " folded" : ""}`}>
            <div className="preview-tabs"><button className={segmentPanel === "shot-prompt" ? "active" : ""} onClick={() => setSegmentPanel("shot-prompt")}>分镜生成</button><button className={segmentPanel === "shot-history" ? "active" : ""} onClick={() => setSegmentPanel("shot-history")}>任务记录</button></div>
            {selectedEpisode?.file_url ? <div className="storyboard-source-preview"><div className="source-preview-label">原片参考 · {selectedEpisode.filename}</div><video key={`${selectedEpisode.id}-${selectedStoryboard?.id ?? "none"}`} src={apiAssetUrl(selectedEpisode.file_url)} controls playsInline onLoadedMetadata={(event) => { const range = selectedStoryboard ? parseRange(selectedStoryboard.time_range) : null; const video = event.currentTarget; if (range && Number.isFinite(range.start) && range.start < video.duration) video.currentTime = range.start; }} onError={() => setMessage("原片暂时无法播放，请检查该集视频文件")}/><span>{selectedStoryboard ? `定位时间：${selectedStoryboard.time_range}` : "选择分镜后定位到对应原片时间"}</span></div> : <div className="video-not-ready">当前集没有可播放原片。</div>}
            <div className="video-not-ready compact-note">生成结果尚未产生真实视频；模拟任务不会显示为可播放结果。</div>
          </aside>
          <section className="shot-strip">
            <div className="strip-head"><h3>{storyboards.length} 分镜 · 自动切分，可自由增删改查</h3><div className="strip-actions"><button className="ghost small" disabled={!selectedStoryboard} onClick={() => selectedStoryboard && moveStoryboard(selectedStoryboard, "up")}>上移</button><button className="ghost small" disabled={!selectedStoryboard} onClick={() => selectedStoryboard && moveStoryboard(selectedStoryboard, "down")}>下移</button><button className="ghost small" disabled={!selectedStoryboard} onClick={() => selectedStoryboard && deleteStoryboard(selectedStoryboard)}>删除</button></div></div>
            <div className="thumb-row">
              {storyboards.map((storyboard) => (
                <div className="shot-thumb-wrap" key={storyboard.id}><div className={storyboard.id === selectedStoryboard?.id ? "shot-thumb active" : "shot-thumb"} onClick={() => setSelectedStoryboardId(storyboard.id)}><span>{storyboard.title} · {storyboard.time_range}</span><small>{storyboard.status}</small></div><button className="insert-shot" aria-label={`在${storyboard.title}后新增`} onClick={() => createStoryboard(storyboard)}>+</button></div>
              ))}
              <button className="add-shot-card" onClick={() => createStoryboard()}>+ 新增分镜</button>
            </div>
          </section>
        </section>

        <section className={screen === "compare" ? "screen active video-page" : "screen video-page"} id="compare">
          <VideoComposition projectId={currentProject?.id} episodeId={selectedEpisodeId ?? undefined} episodeIndex={selectedEpisode?.episode_index} onMessage={setMessage} />
        </section>
      </main>

      {modal === "upload-confirm-modal" && (
        <div className="modal-overlay open" aria-hidden="false" onClick={(event) => event.currentTarget === event.target && setModal(null)}>
          <section className="upload-confirm-dialog" role="dialog" aria-modal="true">
            <div className="modal-titlebar light"><h3>确认上传参数</h3><button onClick={() => setModal(null)}>×</button></div>
            <div className="confirm-body">
              <div className="confirm-copy"><b>请确认本项目的重制目标和生成参数</b><span>确认后将进入原片上传进度页，上传完成后再选择分析方式。</span></div>
              <div className="confirm-grid">
                <article><span>视频来源</span><b>本地视频</b></article>
                <article><span>项目名称</span><b>{selectedSource?.title ?? currentProject?.title ?? "未选择项目"}</b></article>
                <article><span>剧集数量</span><b>{selectedSource?.uploadMode === "single" ? "1 集" : `${selectedFiles?.length || sourceEpisodeCount} 集`}</b></article>
                <article><span>画面类型</span><b>{displayStyleType}</b></article>
                <article><span>地区语言</span><b>{targetLanguage === "沿用原片语言" ? "国内（原片语言）" : `海外·${targetLanguage}`}</b></article>
                <article><span>画幅</span><b>{frameRatio}</b></article>
                <article><span>清晰度</span><b>{resolution}</b></article>
              </div>
            </div>
            <div className="confirm-actions"><button className="ghost" onClick={() => setModal(null)}>返回修改</button><button className="primary" onClick={() => uploadEpisodes()}>确认并开始上传</button></div>
          </section>
        </div>
      )}

      {modal === "image-view-modal" && imageView && (
        <div className="modal-overlay open" aria-hidden="false" onClick={(event) => event.currentTarget === event.target && setModal(null)}>
          <section className="image-view-dialog" role="dialog" aria-modal="true">
            <div className="modal-titlebar"><h3>{imageView.title}</h3><button onClick={() => setModal(null)}>×</button></div>
            <div className="image-view-body"><img src={imageView.url} alt={imageView.title} /></div>
          </section>
        </div>
      )}
    </>
  );
}
