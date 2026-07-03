import { ChangeEvent, useEffect, useMemo, useRef, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8002";
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
type AssetTab = "角色" | "场景" | "道具" | "批量送审";
type SourceMode = "local" | "backend";
type UploadMode = "folder" | "single";
type BackendType = "ai" | "live";
type ModalName = "backend-project-modal" | "upload-confirm-modal" | "keyframe-modal" | "video-zoom-modal" | "image-view-modal" | null;
type SegmentPanel = "shot-prompt" | "shot-assets" | "shot-history" | "shot-batch";
type ShotSidepanel = "shot-asset-overview" | "shot-frame";
type VideoPanel = "video-compare" | "video-list" | "video-export";

type Project = {
  id: string;
  title: string;
  target_country: string;
  target_language: string;
  status: string;
};

type Episode = {
  id: string;
  episode_index: number;
  filename: string;
  size_bytes: number;
  status: string;
  source_url: string;
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
  kind: SourceMode;
  uploadMode?: UploadMode;
  backendType?: BackendType;
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
  return pickText(asset.payload, ["主资产编号", "道具编号"]) || `${asset.asset_type}:${asset.name}:${asset.localized_name}`;
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
  return [assetDisplayVariable(asset), asset.localized_name && asset.localized_name !== assetDisplayName(asset) ? asset.localized_name : ""]
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
  const child = currentChild(asset);
  return [
    pickText(asset.payload, ["原剧核心形象描述", "原剧场景描述", "原剧道具描述"]),
    pickText(child, ["原剧视觉形象描述", "原剧视觉描述", "原剧子资产描述"]),
    pickText(asset.payload, ["剧情身份", "剧情功能"]),
  ].filter(Boolean).join("\n") || "来自原片分析：保留原资产在剧情中的身份、外观、功能和出现状态。";
}

function localizedAssetDescription(asset: Asset) {
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
  return asset?.images?.find((item) => item.is_selected) ?? asset?.images?.[0];
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

function uploadProjectTitle(files: FileList | null, fallback: string) {
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

export function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [currentProject, setCurrentProject] = useState<Project | null>(null);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [storyboards, setStoryboards] = useState<Storyboard[]>([]);
  const [generations, setGenerations] = useState<VideoGeneration[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [screen, setScreen] = useState<Screen>("home");
  const [sourceMode, setSourceMode] = useState<SourceMode>("local");
  const [selectedSource, setSelectedSource] = useState<SelectedSource | null>(null);
  const [selectedFiles, setSelectedFiles] = useState<FileList | null>(null);
  const [uploadState, setUploadState] = useState<"idle" | "selected" | "uploading" | "uploaded" | "failed">("idle");
  const [modal, setModal] = useState<ModalName>(null);
  const [imageView, setImageView] = useState<ImageView>(null);
  const [backendType, setBackendType] = useState<BackendType>("ai");
  const [targetCountry, setTargetCountry] = useState("美国");
  const [frameRatio, setFrameRatio] = useState("9:16");
  const [resolution, setResolution] = useState("720p");
  const [styleType, setStyleType] = useState("写实通用");
  const [assetTab, setAssetTab] = useState<AssetTab>("角色");
  const [assetView, setAssetView] = useState<"overview" | "detail" | "batch">("overview");
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | null>(null);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [selectedStoryboardId, setSelectedStoryboardId] = useState<string | null>(null);
  const [segmentPanel, setSegmentPanel] = useState<SegmentPanel>("shot-prompt");
  const [shotSidepanel, setShotSidepanel] = useState<ShotSidepanel>("shot-asset-overview");
  const [videoPanel, setVideoPanel] = useState<VideoPanel>("video-compare");
  const [activeAnalysisTask, setActiveAnalysisTask] = useState<Task | null>(null);
  const [analysisProgress, setAnalysisProgress] = useState({ story: 0, asset: 0 });
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);
  const [mentionCaret, setMentionCaret] = useState(0);
  const [busy, setBusy] = useState(false);
  const [generatingAssetId, setGeneratingAssetId] = useState<string | null>(null);
  const [message, setMessage] = useState("准备就绪");
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const promptInputRef = useRef<HTMLTextAreaElement | null>(null);

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
    () => assetGroups.filter((group) => assetTab === "批量送审" || group.type === assetTab),
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
    () => convertPromptVariables(selectedStoryboard?.prompt ?? "", assets),
    [assets, selectedStoryboard]
  );
  const mentionOptions = useMemo(() => {
    const query = (mentionQuery ?? "").toLowerCase();
    return selectedStoryboardAssets.filter((asset) => {
      if (!query) return true;
      return `${assetDisplayName(asset)} ${assetDisplayVariable(asset)}`.toLowerCase().includes(query);
    });
  }, [mentionQuery, selectedStoryboardAssets]);
  const canUseAigc = selectedSource?.kind === "backend" && selectedSource.backendType === "ai";
  const isSingleUpload = selectedSource?.kind === "local" && selectedSource.uploadMode === "single";
  const sourceEpisodeCount = isSingleUpload ? 1 : Math.max(episodes.length, selectedFiles?.length ?? 0, 40);

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
    setCurrentProject(data.project);
    setEpisodes(data.episodes);
    setSelectedEpisodeId((currentId) => pickDefaultEpisodeId(data.episodes, currentId));
    await Promise.all([loadAssets(projectId), loadStoryboards(projectId), loadGenerations(projectId), loadTasks(projectId)]);
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
    setAnalysisProgress({ story: 0, asset: 0 });
  }

  async function createProjectForUpload(title: string) {
    const data = await request<{ project: Project }>("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, targetCountry, targetLanguage: "English" }),
    });
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

  async function loadStoryboards(projectId = currentProject?.id) {
    if (!projectId) return;
    const data = await request<{ storyboards: Storyboard[] }>(`/api/projects/${projectId}/storyboards`);
    setStoryboards(data.storyboards);
  }

  async function loadGenerations(projectId = currentProject?.id) {
    if (!projectId) return;
    const data = await request<{ videoGenerations: VideoGeneration[] }>(`/api/projects/${projectId}/video-generations`);
    setGenerations(data.videoGenerations);
  }

  async function loadTasks(projectId = currentProject?.id) {
    if (!projectId) return;
    const data = await request<{ tasks: Task[] }>(`/api/projects/${projectId}/tasks`);
    setTasks(data.tasks);
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
    setAssetView(tab === "批量送审" ? "batch" : "overview");
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
    const firstFile = files[0];
    const title = uploadProjectTitle(files, "未命名本地视频项目");
    setSelectedFiles(files);
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
        body: JSON.stringify({ title: "六年后再遇首富前男友", targetCountry, targetLanguage: "英语" }),
      });
      setMessage("项目已创建");
      await loadProjects();
      await selectProject(data.project.id);
    } finally {
      setBusy(false);
    }
  }

  async function confirmBackendProject() {
    const isAi = backendType === "ai";
    setSelectedSource({
      kind: "backend",
      backendType,
      title: isAi ? "六年后再遇首富前男友" : "六年后再遇首富前男友-真人投放版",
      meta: isAi
        ? "后台项目 · AI 剧 · 40 集 · 可关联 AIGC 资产"
        : "后台项目 · 真人剧 · 40 集 · 走普通视频分析",
    });
    setUploadState("selected");
    setModal(null);
    setMessage("后台项目已选择，请确认参数后开始上传");
  }

  async function uploadEpisodes(files = selectedFiles) {
    if (selectedSource?.kind === "local" && !files?.length) return;
    setBusy(true);
    setUploadState("uploading");
    setScreen("source");
    setModal(null);
    try {
      const project =
        selectedSource?.kind === "local"
          ? await createProjectForUpload(selectedSource.title)
          : currentProject ?? await createProjectForUpload(selectedSource?.title ?? "后台视频项目");
      if (selectedSource?.kind === "backend") {
        window.setTimeout(() => {
          setUploadState("uploaded");
          setMessage("后台项目链路已模拟拉取完成，可选择分析方式");
        }, 900);
        return;
      }
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

  async function runBackendTask(taskType: "asset_extraction" | "storyboard_generation" | "video_generation") {
    if (!currentProject) return null;
    if (taskType !== "video_generation" && !selectedEpisodeId) {
      setMessage("请先选择要处理的视频");
      return null;
    }
    const payload =
      taskType === "video_generation"
        ? { taskType, storyboardId: selectedStoryboard?.id ?? storyboards[0]?.id }
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

  async function pollTask(taskId: string, onDone: (task: Task) => Promise<void> | void) {
    for (let index = 0; index < 160; index += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 3000));
      const data = await request<{ task: Task }>(`/api/tasks/${taskId}`);
      setActiveAnalysisTask(data.task);
      if (!isTaskRunning(data.task)) {
        await onDone(data.task);
        return;
      }
    }
  }

  async function startAnalysis(mode: "normal" | "aigc") {
    if (!currentProject || !selectedEpisodeId || busy) return;
    setScreen("analysis");
    setBusy(true);
    setAnalysisProgress({ story: 8, asset: mode === "aigc" ? 12 : 8 });
    setMessage(mode === "aigc" ? "正在关联 AIGC 资产并分析" : "正在执行普通视频分析");
    try {
      const task = await runBackendTask("asset_extraction");
      if (!task) return;
      setActiveAnalysisTask(task);
      setBusy(false);
      void pollTask(task.id, async (doneTask) => {
        if (currentProject) await Promise.all([loadAssets(currentProject.id), loadTasks(currentProject.id)]);
        if (doneTask.status === "失败") {
          setMessage(doneTask.message || "分析失败，请重试");
          setAnalysisProgress({ story: 100, asset: 100 });
          return;
        }
        setAnalysisProgress({ story: 100, asset: 100 });
        setMessage("分析完成，可以进入设定确认资产");
      });
    } catch (error) {
      setBusy(false);
      setActiveAnalysisTask({ id: "", episode_id: selectedEpisodeId, task_type: "asset_extraction", status: "失败", message: error instanceof Error ? error.message : "任务启动失败", created_at: "", updated_at: "" });
      setMessage(error instanceof Error ? error.message : "任务启动失败");
    }
  }

  async function enterStoryboards() {
    if (!currentProject || busy) return;
    setBusy(true);
    try {
      const task = await runBackendTask("storyboard_generation");
      if (task) {
        setScreen("segments");
        setSegmentPanel("shot-prompt");
        setMessage("分镜任务已开始，完成后会自动刷新");
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "分镜生成失败");
    } finally {
      setBusy(false);
    }
  }

  async function runVideoGeneration(batch = false) {
    if (!currentProject || busy) return;
    setBusy(true);
    try {
      const task = await runBackendTask("video_generation");
      if (task) {
        setScreen("compare");
        setVideoPanel(batch ? "video-list" : "video-compare");
        setMessage(batch ? "批量视频生成任务已开始" : "视频生成任务已开始");
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

  async function savePrompt(storyboard: Storyboard, prompt: string) {
    if (!currentProject) return;
    const data = await request<{ storyboard: Storyboard }>(
      `/api/projects/${currentProject.id}/storyboards/${storyboard.id}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
      }
    );
    setStoryboards((items) => items.map((item) => (item.id === storyboard.id ? data.storyboard : item)));
    setMessage("提示词已保存");
  }

  function updateStoryboardPromptText(value: string) {
    if (!selectedStoryboard) return;
    setStoryboards((items) => items.map((item) => item.id === selectedStoryboard.id ? { ...item, prompt: value } : item));
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
    if (assets.length && (!selectedAssetId || !assets.some((asset) => asset.id === selectedAssetId))) {
      setSelectedAssetId(assets[0].id);
    }
  }, [assets, selectedAssetId]);

  useEffect(() => {
    if (storyboards.length && (!selectedStoryboardId || !storyboards.some((item) => item.id === selectedStoryboardId))) {
      setSelectedStoryboardId(storyboards[0].id);
    }
  }, [storyboards, selectedStoryboardId]);

  useEffect(() => {
    document.body.dataset.screen = screen;
  }, [screen]);

  useEffect(() => {
    if (screen !== "analysis" || !isTaskRunning(activeAnalysisTask)) return;
    const timer = window.setInterval(() => {
      setAnalysisProgress((current) => ({
        story: Math.min(current.story + 5, 96),
        asset: Math.min(current.asset + 4, 96),
      }));
    }, 1400);
    return () => window.clearInterval(timer);
  }, [screen, activeAnalysisTask]);

  const paramSummary = `${frameRatio} · ${resolution} · ${styleType}`;
  const analysisDone = activeAnalysisTask && !isTaskRunning(activeAnalysisTask) && activeAnalysisTask.status !== "失败";
  const selectedAssetGeneratedImage = selectedAssetImageUrl(selectedAsset);
  const selectedAssetOriginalImage = originalAssetImageUrl(selectedAsset);
  const selectedImageVersion = selectedAssetImage(selectedAsset);
  const isGeneratingSelectedAsset = Boolean(selectedAsset && generatingAssetId === selectedAsset.id);

  return (
    <>
      <header className="app-top">
        <div className="project">
          <button className="back-btn" onClick={() => setScreen("home")}>‹</button>
          <div className="home-brand">视频重制工坊</div>
          <div className="project-copy">
            <div className="project-title">{currentProject?.title ?? "未命名项目"} <span>✎</span></div>
            <div className="project-meta">{frameRatio}　｜　{resolution}　｜　{styleType}　｜　目标语言：{currentProject?.target_language ?? "英语"}</div>
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
        <div className="top-actions">
          <button className="ghost" onClick={() => {
            setScreen("compare");
            setVideoPanel("video-export");
          }}>导出</button>
          <button className="coin">✦ {assets.length + storyboards.length + generations.length}</button>
        </div>
      </header>

      <main>
        <section className={screen === "home" ? "screen active home-screen" : "screen home-screen"} id="home">
          <div className="upload-card">
            <div className="upload-tabs">
              <button className={sourceMode === "local" ? "active" : ""} onClick={() => setSourceMode("local")}>上传本地视频</button>
              <button className={sourceMode === "backend" ? "active" : ""} onClick={() => setSourceMode("backend")}>拉取后台视频项目</button>
            </div>

            {!selectedSource && sourceMode === "local" && (
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

            {!selectedSource && sourceMode === "backend" && (
              <section className="source-empty">
                <div className="drop-icon">▣</div>
                <b>拉取后台视频项目</b>
                <span>从公司视频管理系统选择待重制项目。确认项目后再开始上传。</span>
                <div className="empty-actions">
                  <button className="primary" onClick={() => setModal("backend-project-modal")}>选择后台项目</button>
                </div>
              </section>
            )}

            {selectedSource && (
              <section className="selected-source">
                <div>
                  <span>{selectedSource.kind === "local" ? "本地视频" : selectedSource.backendType === "ai" ? "后台项目 · AI 剧" : "后台项目 · 真人剧"}</span>
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

            {uploadState === "uploaded" && (
              <section className="source-summary">
                <article><span>文件夹名称</span><b>{selectedSource?.title ?? currentProject?.title ?? "未选择"}</b></article>
                <article><span>总集数</span><b>{episodes.length || sourceEpisodeCount} 集</b></article>
                <article><span>上传状态</span><b>{episodes.length || sourceEpisodeCount}/{episodes.length || sourceEpisodeCount} 已完成</b></article>
              </section>
            )}

            <div className="config-row">
              <label className="select-field">目标国家<select value={targetCountry} onChange={(event) => setTargetCountry(event.target.value)}><option>美国</option><option>英国</option><option>日本</option><option>韩国</option></select></label>
              <div className="spacer"></div>
              <details className="param-sheet">
                <summary>生成参数 <b>{paramSummary}</b></summary>
                <div className="param-popover">
                  <div className="param-row"><span>画幅</span><div className="param-options">{["16:9", "9:16", "1:1"].map((item) => <button className={frameRatio === item ? "active" : ""} key={item} onClick={() => setFrameRatio(item)}>{item}</button>)}</div></div>
                  <div className="param-row"><span>清晰度</span><div className="param-options">{["480p", "720p", "1080p"].map((item) => <button className={resolution === item ? "active" : ""} key={item} onClick={() => setResolution(item)}>{item}</button>)}</div></div>
                  <div className="param-row"><span>风格</span><div className="param-options">{["写实通用", "欧美短剧", "广告质感"].map((item) => <button className={styleType === item ? "active" : ""} key={item} onClick={() => setStyleType(item)}>{item}</button>)}</div></div>
                </div>
              </details>
              <button className="create-btn in-card" disabled={busy || !selectedSource} onClick={() => setModal("upload-confirm-modal")}>开始上传</button>
            </div>

            <input hidden multiple type="file" accept="video/*" ref={fileInputRef} onChange={(event: ChangeEvent<HTMLInputElement>) => handleLocalFiles(event.target.files, "single")} />
            <input hidden multiple type="file" accept="video/*" ref={folderInputRef} {...{ webkitdirectory: "" }} onChange={(event: ChangeEvent<HTMLInputElement>) => handleLocalFiles(event.target.files, "folder")} />
          </div>

          <section className="project-library">
            <div className="library-head">
              <div><h2>项目管理</h2><span>过往重绘项目</span></div>
              <div className="library-actions"><button className="ghost" onClick={createProject}>新建项目</button><button className="ghost" onClick={() => currentProject && selectProject(currentProject.id)}>最近更新</button></div>
            </div>
            <div className="project-grid">
              {projects.slice(0, 4).map((project, index) => (
                <article className="project-card" key={project.id} onClick={() => {
                  void selectProject(project.id);
                  setScreen(index % 2 === 0 ? "source" : "assets");
                }}>
                  <div className={`project-cover cover-${["a", "b", "c", "d"][index % 4]}`}></div>
                  <div className="project-info">
                    <span>{project.status}</span>
                    <b>{project.title}</b>
                    <small>{project.target_country} · {project.target_language} · {project.id}</small>
                  </div>
                </article>
              ))}
            </div>
          </section>
        </section>

        <section className={screen === "source" ? "screen active page-screen" : "screen page-screen"} id="source">
          <div className="info-bar">
            {uploadState === "uploading" ? `正在上传《${selectedSource?.title ?? currentProject?.title ?? "当前项目"}》原片视频。上传完成后，再选择分析方式。` : uploadState === "failed" ? "上传失败，请返回重新选择或重试上传。" : "原片视频已上传完成，现在可以选择分析方式。"}
          </div>
          <div className="page-head">
            <h1>原片剧集</h1>
            <div>全部 {episodes.length || sourceEpisodeCount}　已上传 {uploadState === "uploaded" ? episodes.length || sourceEpisodeCount : 0}　上传中 {uploadState === "uploading" ? sourceEpisodeCount : 0}　失败 {uploadState === "failed" ? 1 : 0}</div>
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
                  status: uploadState,
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
                    <td><i className={`dot ${uploadState === "failed" ? "red" : uploadState === "uploaded" || episode.status === "已上传" || episode.ark_file_id ? "green" : "blue"}`}></i><span>{uploadState === "uploading" ? "上传中" : uploadState === "uploaded" || episode.ark_file_id ? "上传成功" : uploadState === "failed" ? "上传失败" : "等待上传"}</span></td>
                    <td>{episode.id.startsWith("pending") ? <a>暂停</a> : <><a onClick={() => syncArkFile(episode.id)}>同步 Ark</a><a className="danger">删除</a></>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {uploadState === "uploaded" && (
            <section className="analysis-choice source-analysis-choice">
              <div>
                <b>选择分析方式</b>
                <span>{canUseAigc ? "该后台项目为 AI 剧，上传完成后可关联 AIGC 资产资料，也可以选择普通分析。" : "当前素材无可复用 AIGC 资产，上传完成后走普通视频分析。"}</span>
              </div>
              <div className="analysis-actions">
                <button className="primary" disabled={busy} onClick={() => selectedEpisodeId ? startAnalysis("normal") : setMessage("后台项目当前是演示链路；真实分析需要先上传一个本地视频生成剧集记录")}>普通分析</button>
                <button className={canUseAigc ? "primary" : "ghost disabled"} disabled={busy || !canUseAigc} onClick={() => startAnalysis("aigc")}>关联 AIGC 资产并分析</button>
              </div>
            </section>
          )}
          <div className="locked-rule">当前只展示上传状态。分析状态会在点击分析方式后进入独立 loading 页面展示。</div>
          <div className="footer-actions">
            <button className="ghost" onClick={() => setScreen("home")}>返回</button>
          </div>
        </section>

        <section className={screen === "analysis" ? "screen active page-screen analysis-screen" : "screen page-screen analysis-screen"} id="analysis">
          <div className="analysis-loading-card">
            <div className="loader-ring"></div>
            <h1>正在分析视频资源</h1>
            <p>{activeAnalysisTask?.status === "失败" ? activeAnalysisTask.message : message}</p>
            <div className="analysis-progress-list">
              <article>
                <div className="analysis-head"><b>剧情解析转化进度</b><span>{analysisProgress.story}%</span></div>
                <div className="progress"><i style={{ width: `${analysisProgress.story}%` }}></i></div>
                <small>识别剧情段落、台词、镜头节奏，并转化为目标国家分镜基础描述。</small>
              </article>
              <article>
                <div className="analysis-head"><b>资产解析转化进度</b><span>{analysisProgress.asset}%</span></div>
                <div className="progress"><i style={{ width: `${analysisProgress.asset}%` }}></i></div>
                <small>{canUseAigc ? "优先复用 AIGC 平台中的原资产图和原资产描述，未命中的内容再通过视频分析补齐。" : "通过视频抽帧和视觉理解分析角色、场景、道具与画面文字。"}</small>
              </article>
            </div>
            <button className={analysisDone ? "primary" : "primary disabled"} disabled={!analysisDone} onClick={() => {
              setScreen("assets");
              showAssetOverview("角色");
            }}>进入设定</button>
          </div>
        </section>

        <section className={screen === "assets" ? "screen active page-screen" : "screen page-screen"} id="assets">
          <div className="page-head">
            <h1>设定</h1>
            <div>角色 {roleCount} / 子资产 {roleCount}　场景 {sceneCount} / 子资产 {sceneCount}　道具 {propCount}</div>
          </div>
          <div className="module-tabs">
            {(["角色", "场景", "道具", "批量送审"] as AssetTab[]).map((tab) => (
              <button className={assetTab === tab ? "active" : ""} key={tab} onClick={() => showAssetOverview(tab)}>{tab}</button>
            ))}
          </div>

          {assetView !== "detail" && (
            <div className="asset-overview sub-panel active">
              <div className="overview-head">
                <h2>{assetTab === "批量送审" ? "资产确认清单" : `${assetTab}资产占位符`}</h2>
                <div><button className="ghost" onClick={() => selectedEpisodeId && startAnalysis("normal")} disabled={busy || !selectedEpisodeId}>重新提取</button><button className="ghost" disabled={busy || !assets.length}>批量生成图片</button></div>
              </div>
              {assetTab === "批量送审" ? (
                <div className="sub-panel simple-panel active">
                  <div className="metric"><b>待生成</b><span>{assets.filter((asset) => asset.status !== "已确认").length} 个子资产</span></div>
                  <div className="metric"><b>已确认</b><span>{assets.filter((asset) => asset.status === "已确认").length} 个子资产</span></div>
                  <div className="metric"><b>待生成视频</b><span>{storyboards.filter((item) => item.status !== "已生成").length} 个分镜</span></div>
                  <div className="review-rule">未生成目标资产图的子资产可以先保留占位，但进入分镜时会提醒用户完善资产生成。</div>
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
                        <span>{group.items.length} 个子资产</span>
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
                    <article><span>剧情身份/功能</span><b>{pickText(selectedAsset.payload, ["剧情身份", "剧情功能"]) || "来自原片分析"}</b></article>
                    <article><span>当前状态</span><b>{selectedAsset.status}</b></article>
                  </div>
                  <div className="panel-title">子资产候选</div>
                  <div className="subasset-list compact-subasset-list">
                    {(selectedGroup?.items ?? [selectedAsset]).map((item) => (
                      <article className={item.id === selectedAsset.id ? "subasset-card active" : "subasset-card"} key={item.id} onClick={() => setSelectedAssetId(item.id)}>
                        <div className="subasset-thumb-shell"><div className={`thumb ${assetThumbClass(item)}`}></div><div className="subasset-inline-actions"><button>删除子资产</button><button>设为主资产</button></div></div>
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
                        <div className={`thumb ${assetThumbClass(selectedAsset)}`}></div>
                      )}
                      <b>原资产图</b>
                      <span>来自原片分析</span>
                      <p>{originalAssetDescription(selectedAsset)}</p>
                    </article>
                    <article>
                      {isGeneratingSelectedAsset ? (
                        <div className="generated-image-loading"><i></i><span>生成中</span></div>
                      ) : selectedAssetGeneratedImage ? (
                        <img className="generated-asset-image clickable-image" src={selectedAssetGeneratedImage} alt="目标资产图" onClick={() => openImage(selectedAssetGeneratedImage, `${selectedAsset.localized_name} 目标资产图`)} />
                      ) : (
                        <div className={`asset-placeholder ${assetThumbClass(selectedAsset)}`}>目标资产图</div>
                      )}
                      <b>目标国家资产</b>
                      <span>生成后可送审</span>
                      <p>{localizedAssetDescription(selectedAsset)}</p>
                    </article>
                  </div>
                </section>
                <aside className="generation-panel asset-generation-studio">
                  <div className="studio-toolbar">
                    <div className="studio-title"><b>{variantName(selectedAsset)}</b><button>编辑</button></div>
                    <button className="studio-add">+ 添加变体</button>
                    <div className="studio-icon-actions"><button title="收藏">☆</button><button title="下载">↓</button><button title="删除">删</button></div>
                    <button className="primary studio-confirm" onClick={() => confirmAsset(selectedAsset.id)} disabled={busy}>确认{assetTab}</button>
                  </div>
                  <div className="studio-preview-row">
                    <div className="studio-preview-main">
                      {isGeneratingSelectedAsset ? (
                        <div className="studio-image-loading"><i></i><span>生成中</span></div>
                      ) : selectedAssetGeneratedImage ? (
                        <img className="studio-generated-image clickable-image" src={selectedAssetGeneratedImage} alt="当前目标资产图" onClick={() => openImage(selectedAssetGeneratedImage, `${variantName(selectedAsset)} 当前采用图`)} />
                      ) : (
                        <>
                          <div className={`studio-portrait ${assetThumbClass(selectedAsset)}`}></div>
                          <div className={`studio-turnaround ${assetThumbClass(selectedAsset)}`}></div>
                        </>
                      )}
                    </div>
                    <div className="studio-current">
                      <span>当前</span>
                      <div className="studio-current-thumb" onClick={() => selectedAssetGeneratedImage && openImage(selectedAssetGeneratedImage, "当前采用图")}>{isGeneratingSelectedAsset ? <i className="current-loading"></i> : selectedAssetGeneratedImage ? <img src={selectedAssetGeneratedImage} alt="当前采用图" /> : <><i></i><i></i></>}</div>
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
                    <div className="agent-card-head"><h3>Agent 生成{assetTab === "角色" ? "形象" : assetTab}</h3><div><button>从角色库选择</button><button>本地上传</button></div></div>
                    <div className="agent-prompt-box">
                      <button className="add-reference">+</button>
                      <textarea value={assetPrompt(selectedAsset) || "当前资产未返回提示词"} readOnly />
                      <div className="agent-prompt-footer">
                        <div className="agent-selects"><button>Doubao-Seedream-5.0-lite</button><button>1张</button></div>
                        <button className="primary agent-cost" onClick={() => generateAssetImage(selectedAsset.id)} disabled={busy}>生成</button>
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

        <section className={screen === "segments" ? "screen active workbench" : "screen workbench"} id="segments">
          <section className="asset-sidebar">
            <details className="episode-select">
              <summary><b>{selectedEpisode ? `第 ${selectedEpisode.episode_index} 集` : "选择剧集"}</b><span>{episodes.length} 集已解析</span></summary>
              <div className="episode-menu">
                {episodes.map((episode) => (
                  <button className={episode.id === selectedEpisodeId ? "active" : ""} key={episode.id} onClick={() => setSelectedEpisodeId(episode.id)}>第 {episode.episode_index} 集 {episode.filename}</button>
                ))}
              </div>
            </details>
            <div className="inner-tabs compact-asset-tabs">
              <button className={shotSidepanel === "shot-asset-overview" ? "active" : ""} onClick={() => setShotSidepanel("shot-asset-overview")}>全部</button>
              <button>角色</button><button>场景</button><button>道具</button>
              <button className={shotSidepanel === "shot-frame" ? "active" : ""} onClick={() => setShotSidepanel("shot-frame")}>参考帧</button>
            </div>
            {shotSidepanel === "shot-asset-overview" && (
              <div className="asset-panel active">
                <h3>参考帧 <button onClick={() => setShotSidepanel("shot-frame")}>查看</button></h3>
                <div className="frame-card" onClick={() => setShotSidepanel("shot-frame")}><div className="frame-card-grid"><span></span><span></span><span></span><span></span></div><b>已选 4 帧</b><span>用于控制构图与站位</span></div>
                <h3>角色 <button>+</button></h3>
                {selectedStoryboardAssets.filter((asset) => asset.asset_type === "角色").map((asset) => {
                  const thumbUrl = selectedAssetImageUrl(asset) || originalAssetImageUrl(asset);
                  return <div className="ref-card shot-asset-card" key={asset.id}>{thumbUrl ? <img src={thumbUrl} alt={assetDisplayName(asset)} onClick={() => openImage(thumbUrl, assetDisplayName(asset))} /> : <div className="mini-img face-b"></div>}<b>{assetDisplayName(asset)}</b></div>;
                })}
                {!selectedStoryboardAssets.some((asset) => asset.asset_type === "角色") && <p className="empty-shot-assets">当前分镜未匹配角色资产</p>}
                <h3>场景 <button>+</button></h3>
                {selectedStoryboardAssets.filter((asset) => asset.asset_type === "场景").map((asset) => {
                  const thumbUrl = selectedAssetImageUrl(asset) || originalAssetImageUrl(asset);
                  return <div className="scene-card shot-asset-card" key={asset.id}>{thumbUrl ? <img src={thumbUrl} alt={assetDisplayName(asset)} onClick={() => openImage(thumbUrl, assetDisplayName(asset))} /> : <div className="scene-shot"></div>}<b>{assetDisplayName(asset)}</b></div>;
                })}
                {!selectedStoryboardAssets.some((asset) => asset.asset_type === "场景") && <p className="empty-shot-assets">当前分镜未匹配场景资产</p>}
                <h3>道具 <button>+</button></h3>
                {selectedStoryboardAssets.filter((asset) => asset.asset_type === "道具").map((asset) => {
                  const thumbUrl = selectedAssetImageUrl(asset) || originalAssetImageUrl(asset);
                  return <div className="ref-card shot-asset-card" key={asset.id}>{thumbUrl ? <img src={thumbUrl} alt={assetDisplayName(asset)} onClick={() => openImage(thumbUrl, assetDisplayName(asset))} /> : <div className="mini-img prop"></div>}<b>{assetDisplayName(asset)}</b></div>;
                })}
                {!selectedStoryboardAssets.some((asset) => asset.asset_type === "道具") && <p className="empty-shot-assets">当前分镜未匹配道具资产</p>}
              </div>
            )}
            {shotSidepanel === "shot-frame" && (
              <div className="asset-panel frame-side-panel active">
                <h3>选择原视频帧 <button onClick={() => setShotSidepanel("shot-asset-overview")}>返回</button></h3>
                <div className="side-frame-picker"><div className="active"></div><div></div><div></div><div></div></div>
                <h3>合成参考帧</h3>
                <div className="side-composite"><span></span><span></span><span></span><span></span></div>
                <p>选中的关键帧用于控制构图、人物站位和画面细节。</p>
                <button className="dark-btn full">添加为参考帧</button>
                <button className="primary full">26</button>
              </div>
            )}
          </section>
          <section className="shot-main">
            <div className="shot-head"><h2>{selectedStoryboard ? selectedStoryboard.title : "暂无分镜"} <span>{selectedStoryboard?.time_range ?? "等待自动切分"}</span></h2><div className="shot-actions"><button className="primary small" disabled={busy || !selectedStoryboard} onClick={() => runVideoGeneration(false)}>生成视频</button></div></div>
            {segmentPanel === "shot-prompt" && !selectedStoryboard && (
              <div className="shot-empty sub-panel active">
                <b>当前项目还没有分镜</b>
                <span>点击“进入分镜”后系统会根据资产出现片段自动生成；生成完成后这里会显示可编辑提示词。</span>
              </div>
            )}
            {segmentPanel === "shot-prompt" && selectedStoryboard && (
              <div className="shot-text sub-panel active">
                <div className="prompt-body">
                  <div className="tool-row"><button>+</button><button onClick={() => setSegmentPanel("shot-assets")}>引用</button><span>系统已把审核通过的资产引用写入提示词，用户可直接编辑。</span></div>
                  <div className="prompt-editor-wrap">
                    <textarea ref={promptInputRef} value={selectedStoryboardPrompt} onChange={handlePromptInput} onBlur={(event) => selectedStoryboard && savePrompt(selectedStoryboard, event.currentTarget.value)} />
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
                <div className="controls"><button>Doubao-Seedance</button><button>8s</button><button>1x</button></div>
              </div>
            )}
            {segmentPanel === "shot-assets" && <div className="sub-panel simple-panel active">{selectedStoryboardAssets.map((asset) => <span className="chip" key={asset.id}>{assetDisplayVariable(asset)}</span>)}<button className="ghost full" onClick={() => setSegmentPanel("shot-prompt")}>返回提示词</button></div>}
            {segmentPanel === "shot-history" && <div className="sub-panel simple-panel active">{generations.slice(0, 5).map((item, index) => <div className="log-row" key={item.id}><b>第 {index + 1} 次</b><span>{item.status}</span></div>)}<button className="ghost full" onClick={() => setSegmentPanel("shot-prompt")}>返回提示词</button></div>}
            {segmentPanel === "shot-batch" && <div className="sub-panel simple-panel active"><button className="primary" onClick={() => runVideoGeneration(true)}>生成当前集</button><button className="ghost" onClick={() => runVideoGeneration(true)}>生成选中分镜</button><button className="ghost" onClick={() => setSegmentPanel("shot-prompt")}>返回提示词</button></div>}
          </section>
          <aside className="video-preview">
            <div className="preview-tabs"><button className={segmentPanel === "shot-prompt" ? "active" : ""} onClick={() => setSegmentPanel("shot-prompt")}>分镜生成</button><button>原视频</button></div>
            <div className="phone-video"><div className="video-tools"><button title="提取关键帧" onClick={() => setModal("keyframe-modal")}>▣</button><button title="放大查看" onClick={() => setModal("video-zoom-modal")}>⛶</button></div><div className="play">▶</div></div>
            <div className="preview-history"><div className="history-head"><b>生成记录</b><span>{generations[0]?.status ?? "未生成"}</span></div><div className="history-thumbs"><button className="history-thumb active" onClick={() => setSegmentPanel("shot-history")}><span>1</span></button><button className="history-thumb" onClick={() => setSegmentPanel("shot-history")}><span>2</span></button><button className="history-thumb" onClick={() => setSegmentPanel("shot-history")}><span>3</span></button></div></div>
          </aside>
          <section className="shot-strip">
            <div className="strip-head"><h3>{storyboards.length} 分镜 · 自动切分，可自由增删改查</h3><button className="primary small" onClick={() => setSegmentPanel("shot-batch")}>批量生成</button></div>
            <div className="thumb-row">
              {storyboards.map((storyboard) => (
                <div className="shot-thumb-wrap" key={storyboard.id}><div className={storyboard.id === selectedStoryboard?.id ? "shot-thumb active" : "shot-thumb"} onClick={() => setSelectedStoryboardId(storyboard.id)}><span>{storyboard.title} · {storyboard.time_range}</span></div><button className="insert-shot" aria-label={`在${storyboard.title}后新增`}>+</button></div>
              ))}
            </div>
          </section>
        </section>

        <section className={screen === "compare" ? "screen active video-page" : "screen video-page"} id="compare">
          <aside className="episode-rail"><span>集数</span>{episodes.slice(0, 8).map((episode) => <button className={episode.id === selectedEpisodeId ? "active" : ""} key={episode.id} onClick={() => setSelectedEpisodeId(episode.id)}>{episode.episode_index}</button>)}</aside>
          <div className="video-toolbar">
            <label className="timeline-toggle"><input type="checkbox" defaultChecked /> 成片时间线对齐</label>
            <div className="module-tabs compact-tabs">
              <button className={videoPanel === "video-compare" ? "active" : ""} onClick={() => setVideoPanel("video-compare")}>对比播放</button>
              <button className={videoPanel === "video-list" ? "active" : ""} onClick={() => setVideoPanel("video-list")}>片段列表</button>
              <button className={videoPanel === "video-export" ? "active" : ""} onClick={() => setVideoPanel("video-export")}>导出</button>
            </div>
          </div>
          {videoPanel === "video-compare" && <div className="video-compare sub-panel active"><div className="player-card"><div className="player-label">原片成片</div><div className="vertical-video"></div><div className="player-controls">00:03 / --:--　已对齐播放</div></div><div className="player-card"><div className="player-label new">重制成片</div><div className="vertical-video remake"></div><div className="player-controls">{generations[0]?.status ?? "未生成"}</div></div></div>}
          {videoPanel === "video-list" && <div className="sub-panel video-list-panel active"><div className="list-summary"><b>{generations.length ? "已有生成片段" : "当前完整成片不可导出"}</b><span>还有 {Math.max(storyboards.length - generations.length, 0)} 个分镜缺少当前采用版本</span></div>{storyboards.map((item) => <div className="video-seg" key={item.id}><b>{item.title}</b><span>{generations.some((gen) => gen.storyboard_id === item.id) ? "已生成 · 可单独导出" : "未生成 · 不参与完整成片"}</span></div>)}</div>}
          {videoPanel === "video-export" && <div className="sub-panel simple-panel active"><button className="primary">导出完整成片</button><button className="ghost">导出选中视频片段</button><button className="ghost">导出当前片段</button><p className="export-note">完整成片要求所有分镜都有当前采用版本；片段导出仅导出已生成视频。</p></div>}
          <div className="video-timeline"><div className="time-ruler">原片侧只播放完整成片；新生成侧可查看重制成片或单独片段。时间线对齐仅在成片对比成片时可用。</div><div className="clip-row">{storyboards.slice(0, 8).map((storyboard, index) => <div className={index === 0 ? "clip active" : generations.some((gen) => gen.storyboard_id === storyboard.id) ? "clip" : "clip missing"} key={storyboard.id}>{storyboard.title}</div>)}</div></div>
        </section>
      </main>

      {modal === "backend-project-modal" && (
        <div className="modal-overlay open" aria-hidden="false" onClick={(event) => event.currentTarget === event.target && setModal(null)}>
          <section className="backend-dialog" role="dialog" aria-modal="true">
            <div className="modal-titlebar light"><h3>选择后台视频项目</h3><button onClick={() => setModal(null)}>×</button></div>
            <div className="backend-search"><input defaultValue="六年后再遇首富前男友" aria-label="搜索项目名或剧 ID" /><button className="ghost">搜索</button></div>
            <div className="backend-tabs"><button className="active">全部</button><button>AI 剧</button><button>真人剧</button></div>
            <div className="backend-projects">
              <article className={backendType === "ai" ? "backend-project active ai" : "backend-project ai"} onClick={() => setBackendType("ai")}><i>AI 剧</i><b>六年后再遇首富前男友</b><span>剧 ID：DRM-20260629-0916</span><small>40 集 · AIGC 短剧生产平台 · 出海短剧组</small><p>可关联制作过程中的角色、场景原资产图和原资产描述。</p></article>
              <article className={backendType === "live" ? "backend-project active live" : "backend-project live"} onClick={() => setBackendType("live")}><i>真人剧</i><b>六年后再遇首富前男友-真人投放版</b><span>剧 ID：DRM-20260621-0448</span><small>40 集 · 视频管理系统 · 投放素材组</small><p>无 AIGC 资产可复用，将通过视频抽帧和视觉理解普通分析。</p></article>
            </div>
            <div className="backend-actions"><button className="primary" onClick={confirmBackendProject}>选择项目</button><button className="ghost" onClick={() => setModal(null)}>取消</button></div>
          </section>
        </div>
      )}

      {modal === "upload-confirm-modal" && (
        <div className="modal-overlay open" aria-hidden="false" onClick={(event) => event.currentTarget === event.target && setModal(null)}>
          <section className="upload-confirm-dialog" role="dialog" aria-modal="true">
            <div className="modal-titlebar light"><h3>确认上传参数</h3><button onClick={() => setModal(null)}>×</button></div>
            <div className="confirm-body">
              <div className="confirm-copy"><b>请确认本项目的重制目标和生成参数</b><span>确认后将进入原片上传进度页，上传完成后再选择分析方式。</span></div>
              <div className="confirm-grid">
                <article><span>视频来源</span><b>{selectedSource?.kind === "backend" ? "后台项目" : "本地视频"}</b></article>
                <article><span>项目名称</span><b>{selectedSource?.title ?? currentProject?.title ?? "未选择项目"}</b></article>
                <article><span>剧集数量</span><b>{selectedSource?.uploadMode === "single" ? "1 集" : `${selectedFiles?.length || sourceEpisodeCount} 集`}</b></article>
                <article><span>目标国家</span><b>{targetCountry}</b></article>
                <article><span>画幅</span><b>{frameRatio}</b></article>
                <article><span>清晰度</span><b>{resolution}</b></article>
                <article><span>风格类型</span><b>{styleType}</b></article>
                <article><span>模型偏好</span><b>{canUseAigc ? "AIGC 资产优先复用 + 默认视频生成模型" : "默认视频生成模型"}</b></article>
              </div>
            </div>
            <div className="confirm-actions"><button className="ghost" onClick={() => setModal(null)}>返回修改</button><button className="primary" onClick={() => uploadEpisodes()}>确认并开始上传</button></div>
          </section>
        </div>
      )}

      {modal === "keyframe-modal" && (
        <div className="modal-overlay open" aria-hidden="false" onClick={(event) => event.currentTarget === event.target && setModal(null)}>
          <section className="keyframe-dialog" role="dialog" aria-modal="true"><div className="modal-titlebar"><h3>视频截帧</h3><button onClick={() => setModal(null)}>×</button></div><div className="keyframe-player"><div className="keyframe-video"></div><div className="keyframe-controls"><button>◀</button><button className="round-play">▶</button><button>▶</button></div></div><div className="frame-timeline"><div className="time-labels">{Array.from({ length: 15 }).map((_, index) => <span key={index}>00:{String(index).padStart(2, "0")}</span>)}</div><div className="timeline-ticks"></div><div className="timeline-frames">{Array.from({ length: 15 }).map((_, index) => <span key={index}></span>)}</div></div><button className="capture-btn">▣</button></section>
        </div>
      )}

      {modal === "video-zoom-modal" && (
        <div className="modal-overlay open" aria-hidden="false" onClick={(event) => event.currentTarget === event.target && setModal(null)}>
          <section className="zoom-dialog" role="dialog" aria-modal="true"><div className="modal-titlebar"><h3>放大查看</h3><button onClick={() => setModal(null)}>×</button></div><div className="zoom-video"><div className="play">▶</div></div></section>
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
