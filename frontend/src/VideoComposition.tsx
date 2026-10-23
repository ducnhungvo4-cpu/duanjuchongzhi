import { ChangeEvent, DragEvent, useEffect, useMemo, useRef, useState } from "react";

const API_BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");
type Media = { id: string; episodeId: string; name: string; kind: string; source: string; fileUrl: string; thumbnailUrl?: string; durationMs: number; width: number; height: number; hasAudio: boolean; sizeBytes?: number };
type Clip = { id: string; sourceId: string; inMs: number; outMs: number; order: number };
type Draft = { project_id: string; episode_id: string; title: string; revision: number; updated_at: string; edit: { aspectRatio: string; resolution: string; fps: number; clips: Clip[] } };
type ExportJob = { id: string; status: string; progress: number; stage: string; error: string; revision: number; downloadUrl?: string; output_duration_ms?: number };
async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) throw new Error((await response.text()) || response.statusText);
  return response.json() as Promise<T>;
}
function fmt(ms: number) { const s = Math.max(0, ms) / 1000; return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`; }

export function VideoComposition({ projectId, episodeId, episodeIndex, onMessage }: { projectId?: string; episodeId?: string; episodeIndex?: number; onMessage: (message: string) => void }) {
  const [assets, setAssets] = useState<Media[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [jobs, setJobs] = useState<ExportJob[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [saveState, setSaveState] = useState("已保存");
  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [activeMediaId, setActiveMediaId] = useState("");
  const [libraryCollapsed, setLibraryCollapsed] = useState(false);
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [playingClipId, setPlayingClipId] = useState("");
  const [seekOnLoad, setSeekOnLoad] = useState<number | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const uploadRef = useRef<HTMLInputElement>(null);
  const dragIndex = useRef<number | null>(null);
  const clips = draft?.edit.clips ?? [];
  const totalMs = clips.reduce((sum, clip) => sum + Math.max(0, clip.outMs - clip.inMs), 0);
  const clipSignature = clips.map((clip) => `${clip.id}:${clip.sourceId}:${clip.inMs}:${clip.outMs}:${clip.order}`).join("|");
  const selectedClip = clips.find((clip) => clip.id === selected) ?? null;
  const selectedMedia = assets.find((item) => item.id === selectedClip?.sourceId) ?? null;
  const filteredAssets = useMemo(() => assets.filter((asset) => `${asset.name} ${asset.source}`.toLowerCase().includes(query.toLowerCase())), [assets, query]);

  async function refresh() {
    if (!projectId || !episodeId) return;
    const [mediaData, draftData, jobData] = await Promise.all([
      api<{ assets: Media[] }>(`/api/projects/${projectId}/episodes/${episodeId}/video-assets`),
      api<{ draft: Draft }>(`/api/projects/${projectId}/video-edits/${episodeId}`),
      api<{ jobs: ExportJob[] }>(`/api/projects/${projectId}/video-export-jobs?episode_id=${episodeId}`),
    ]);
    setAssets(mediaData.assets); setDraft(draftData.draft); setJobs(jobData.jobs);
    if (!selected && draftData.draft.edit.clips.length) setSelected(draftData.draft.edit.clips[0].id);
  }
  useEffect(() => { setDraft(null); setAssets([]); setSelected(null); setPlayhead(0); if (projectId && episodeId) refresh().catch((error) => onMessage(error.message)); }, [projectId, episodeId]);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 1100px)");
    const sync = () => { if (media.matches) { setLibraryCollapsed(true); setInspectorCollapsed(true); } };
    sync(); media.addEventListener("change", sync); return () => media.removeEventListener("change", sync);
  }, []);
  useEffect(() => {
    if (!projectId || !episodeId || !jobs.some((job) => ["queued", "running"].includes(job.status))) return;
    const timer = window.setInterval(() => api<{ jobs: ExportJob[] }>(`/api/projects/${projectId}/video-export-jobs?episode_id=${episodeId}`).then((data) => setJobs(data.jobs)).catch(() => {}), 1200);
    return () => window.clearInterval(timer);
  }, [projectId, episodeId, jobs]);
  useEffect(() => {
    if (!activeMediaId || seekOnLoad === null || !videoRef.current) return;
    const video = videoRef.current;
    const seek = () => { video.currentTime = seekOnLoad / 1000; setSeekOnLoad(null); if (playing) void video.play().catch(() => setPlaying(false)); };
    if (video.readyState >= 1) seek(); else video.addEventListener("loadedmetadata", seek, { once: true });
    return () => video.removeEventListener("loadedmetadata", seek);
  }, [activeMediaId, seekOnLoad, playing]);
  useEffect(() => {
    if (!totalMs) { setPlayhead(0); setPlaying(false); return; }
    const bounded = Math.min(playhead, totalMs - 1);
    seekTo(bounded);
  }, [clipSignature, totalMs]);

  function patchDraft(edit: Draft["edit"]) { setDraft((current) => current ? { ...current, edit } : current); setSaveState("有未保存修改"); }
  function addAsset(asset: Media, replace = false) {
    if (!draft) return;
    const next = replace && selectedClip
      ? clips.map((clip) => clip.id === selectedClip.id ? { ...clip, sourceId: asset.id, inMs: 0, outMs: asset.durationMs } : clip)
      : [...clips, { id: `clip_${crypto.randomUUID()}`, sourceId: asset.id, inMs: 0, outMs: asset.durationMs, order: clips.length }];
    patchDraft({ ...draft.edit, clips: next.map((clip, index) => ({ ...clip, order: index })) });
    const target = replace && selectedClip ? selectedClip : next[next.length - 1];
    setSelected(target.id); setPlayingClipId(target.id); setActiveMediaId(asset.id); setSeekOnLoad(target.inMs);
    if (!replace) setPlayhead(totalMs);
  }
  function reorder(from: number, to: number) {
    if (!draft || from === to || to < 0 || to >= clips.length) return;
    const next = [...clips]; const [item] = next.splice(from, 1); next.splice(to, 0, item);
    patchDraft({ ...draft.edit, clips: next.map((clip, index) => ({ ...clip, order: index })) });
  }
  async function uploadVideo(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0]; event.currentTarget.value = "";
    if (!file || !projectId || !episodeId) return;
    setBusy(true);
    try { const body = new FormData(); body.append("file", file); await api(`/api/projects/${projectId}/episodes/${episodeId}/video-assets`, { method: "POST", body }); await refresh(); onMessage(`已导入并检测 ${file.name}`); }
    catch (error) { onMessage(error instanceof Error ? error.message : "视频导入失败"); }
    finally { setBusy(false); }
  }
  async function saveDraft(): Promise<boolean> {
    if (!draft || !projectId || !episodeId) return false;
    setBusy(true); setSaveState("保存中");
    try {
      const data = await api<{ draft: Draft }>(`/api/projects/${projectId}/video-edits/${episodeId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ revision: draft.revision, ...draft.edit }) });
      setDraft(data.draft); setSaveState("已保存"); onMessage(`剪辑草稿已保存 · revision ${data.draft.revision}`); return true;
    } catch (error) { setSaveState("保存冲突"); onMessage(error instanceof Error ? `${error.message}；当前编辑仍保留` : "保存失败；当前编辑仍保留"); }
    finally { setBusy(false); }
    return false;
  }
  async function exportSelection() {
    if (!projectId || !episodeId || !draft) return;
    setBusy(true);
    try {
      if (saveState !== "已保存" && !(await saveDraft())) return;
      const response = await api<{ job: ExportJob }>(`/api/projects/${projectId}/video-edits/${episodeId}/exports`, { method: "POST" });
      setJobs((items) => [response.job, ...items]); onMessage("已创建所选片段导出任务");
    } catch (error) { onMessage(error instanceof Error ? error.message : "导出任务创建失败"); }
    finally { setBusy(false); }
  }
  function seekTo(ms: number) {
    if (!clips.length) return;
    const target = Math.max(0, Math.min(ms, Math.max(0, totalMs - 1)));
    let offset = 0;
    for (const clip of clips) {
      const duration = clip.outMs - clip.inMs;
      if (target < offset + duration) { const sourceMs = clip.inMs + target - offset; setPlayhead(target); setSelected(clip.id); setPlayingClipId(clip.id); setActiveMediaId(clip.sourceId); setSeekOnLoad(sourceMs); return; }
      offset += duration;
    }
  }
  function playNextClip(clipId: string) {
    const index = clips.findIndex((clip) => clip.id === clipId);
    if (index < 0 || index + 1 >= clips.length) { setPlaying(false); return; }
    const next = clips[index + 1]; const prefix = clips.slice(0, index + 1).reduce((sum, clip) => sum + clip.outMs - clip.inMs, 0);
    setSelected(next.id); setPlayhead(prefix); setPlayingClipId(next.id); setActiveMediaId(next.sourceId); setSeekOnLoad(next.inMs);
  }
  function onTimeUpdate() {
    const video = videoRef.current; const clip = clips.find((item) => item.id === playingClipId && item.sourceId === activeMediaId);
    if (!video || !clip) return;
    if (video.currentTime * 1000 >= clip.outMs - 30) { playNextClip(clip.id); return; }
    const index = clips.findIndex((item) => item.id === clip.id);
    const prefix = clips.slice(0, index).reduce((sum, item) => sum + item.outMs - item.inMs, 0);
    setPlayhead(prefix + Math.max(0, video.currentTime * 1000 - clip.inMs));
  }
  function togglePlayback() {
    if (!videoRef.current || !clips.length) return;
    if (playing) { videoRef.current.pause(); setPlaying(false); }
    else {
      if (!activeMediaId) seekTo(playhead);
      setPlaying(true); void videoRef.current.play().catch((error) => { setPlaying(false); onMessage(`无法播放当前素材：${error.message}`); });
    }
  }
  function updateTrim(field: "inMs" | "outMs", value: number) {
    if (!draft || !selectedClip || !selectedMedia) return;
    const inMs = field === "inMs" ? value : selectedClip.inMs;
    const outMs = field === "outMs" ? value : selectedClip.outMs;
    const safeIn = Math.max(0, Math.min(inMs, selectedMedia.durationMs - 1));
    const safeOut = Math.max(safeIn + 1, Math.min(outMs, selectedMedia.durationMs));
    patchDraft({ ...draft.edit, clips: clips.map((clip) => clip.id === selectedClip.id ? { ...clip, inMs: safeIn, outMs: safeOut } : clip) });
  }
  function dropClip(event: DragEvent<HTMLDivElement>, targetIndex: number) { event.preventDefault(); if (dragIndex.current !== null) reorder(dragIndex.current, targetIndex); dragIndex.current = null; }
  if (!projectId || !episodeId) return <div className="composition-no-project">先选择项目和剧集，再导入要编辑的视频。</div>;
  const activeSource = assets.find((item) => item.id === activeMediaId) ?? assets.find((item) => item.id === clips[0]?.sourceId);

  return <section className={`composition-workspace${libraryCollapsed ? " library-collapsed" : ""}${inspectorCollapsed ? " inspector-collapsed" : ""}`}>
    <header className="composition-toolbar"><div><b>{draft?.title ?? `第${episodeIndex ?? ""}集合成`}</b><span className={saveState === "已保存" ? "save-state saved" : "save-state"}>● {saveState}</span><button onClick={() => setLibraryCollapsed((value) => !value)}>{libraryCollapsed ? "显示素材" : "收起素材"}</button><button onClick={() => setInspectorCollapsed((value) => !value)}>{inspectorCollapsed ? "显示检查器" : "收起检查器"}</button>{saveState === "保存冲突" && <button onClick={() => refresh().then(() => setSaveState("已保存")).catch((error) => onMessage(error.message))}>重新加载服务器版本</button>}</div><div className="composition-settings"><label>画幅<select value={draft?.edit.aspectRatio ?? "9:16"} onChange={(event) => draft && patchDraft({ ...draft.edit, aspectRatio: event.target.value })}><option>9:16</option><option>16:9</option><option>1:1</option></select></label><label>分辨率<select value={draft?.edit.resolution ?? "720p"} onChange={(event) => draft && patchDraft({ ...draft.edit, resolution: event.target.value })}><option>720p</option><option>1080p</option></select></label><button disabled={busy || saveState === "已保存"} onClick={saveDraft}>保存草稿</button><button className="export-action" disabled={busy || !clips.length} onClick={exportSelection}>导出所选片段</button></div></header>
    <div className="composition-body">
      <aside className={`composition-library${libraryCollapsed ? " folded" : ""}`}><div className="library-head"><b>素材库</b><button onClick={() => uploadRef.current?.click()} disabled={busy}>导入本地视频</button><input ref={uploadRef} hidden type="file" accept="video/*,.mkv,.webm" onChange={uploadVideo} /></div><input className="media-search" placeholder="搜索素材" value={query} onChange={(event) => setQuery(event.target.value)} /><div className="media-list">{filteredAssets.map((asset) => { const added = clips.some((clip) => clip.sourceId === asset.id); return <article className="media-item" key={asset.id}>{asset.thumbnailUrl ? <img className="media-thumbnail" src={`${API_BASE}${asset.thumbnailUrl}`} alt="" /> : <div className="media-icon">▶</div>}<div className="media-copy"><b title={asset.name}>{asset.name}</b><span>{fmt(asset.durationMs)} · {asset.width}×{asset.height}</span><small>{asset.source}{asset.hasAudio ? " · 含音轨" : " · 无音轨"}</small></div><div className="media-actions"><button onClick={() => addAsset(asset)}>加入</button>{selectedClip && <button title="替换当前片段素材" onClick={() => addAsset(asset, true)}>替换</button>}</div>{added && <em>已添加</em>}</article>; })}{!filteredAssets.length && <p className="composition-empty">当前集还没有可用视频。原片仅作参考，点击“加入”后才会进入时间线。</p>}</div></aside>
      <div className="composition-center"><div className={`composition-canvas ratio-${(draft?.edit.aspectRatio ?? "9:16").replace(":", "-")}`}><div className="canvas-inner">{activeSource ? <video ref={videoRef} src={`${API_BASE}${activeSource.fileUrl}`} controls={false} onTimeUpdate={onTimeUpdate} onEnded={() => selected && playNextClip(selected)} onError={() => onMessage("当前素材无法在浏览器播放，请重新导入兼容的视频编码")} /> : <div className="preview-empty">选择时间线片段预览</div>}</div></div><div className="transport"><button onClick={togglePlayback} disabled={!clips.length}>{playing ? "暂停" : "播放"}</button><span>{fmt(playhead)} / {fmt(totalMs)}</span><input aria-label="播放位置" type="range" min="0" max={Math.max(1, totalMs)} value={Math.min(playhead, totalMs)} onChange={(event) => seekTo(Number(event.target.value))} disabled={!clips.length} /><label>音量 <input type="range" min="0" max="1" step="0.05" defaultValue="1" onChange={(event) => { if (videoRef.current) videoRef.current.volume = Number(event.target.value); }} /></label></div></div>
      <aside className={`composition-inspector${inspectorCollapsed ? " folded" : ""}`}><b>片段检查器</b>{selectedClip && selectedMedia ? <><div className="inspector-name">{selectedMedia.name}</div><label>入点（毫秒）<input type="number" min="0" max={selectedMedia.durationMs - 1} value={selectedClip.inMs} onChange={(event) => updateTrim("inMs", Number(event.target.value))} /></label><label>出点（毫秒）<input type="number" min={selectedClip.inMs + 1} max={selectedMedia.durationMs} value={selectedClip.outMs} onChange={(event) => updateTrim("outMs", Number(event.target.value))} /></label><div className="inspector-meta">有效时长 <b>{fmt(selectedClip.outMs - selectedClip.inMs)}</b></div><div className="inspector-meta">源素材 <b>{selectedMedia.source}</b></div><div className="inspector-meta">顺序 <b>{clips.findIndex((clip) => clip.id === selectedClip.id) + 1} / {clips.length}</b></div><div className="inspector-buttons"><button onClick={() => reorder(clips.findIndex((clip) => clip.id === selectedClip.id), -1)}>上移</button><button onClick={() => reorder(clips.findIndex((clip) => clip.id === selectedClip.id), clips.findIndex((clip) => clip.id === selectedClip.id) + 1)}>下移</button><button onClick={() => { if (!draft) return; const next = clips.filter((clip) => clip.id !== selectedClip.id).map((clip, index) => ({ ...clip, order: index })); patchDraft({ ...draft.edit, clips: next }); setSelected(next[0]?.id ?? null); }}>移除</button></div></> : <p className="composition-empty">选择一个时间线片段后，可调整入点、出点和顺序。</p>}<div className="export-jobs"><b>导出任务</b>{jobs.map((job) => <article key={job.id}><span>{job.stage || job.status}</span><progress max="100" value={job.progress} /><small>{job.status === "failed" ? job.error : `${job.progress}% · revision ${job.revision}`}</small>{job.status === "succeeded" && job.downloadUrl && <a href={`${API_BASE}${job.downloadUrl}`} download>下载 MP4</a>}{job.status === "failed" && <button onClick={() => api<{ job: ExportJob }>(`/api/projects/${projectId}/video-export-jobs/${job.id}/retry`, { method: "POST" }).then(({ job: retry }) => setJobs((items) => [retry, ...items])).catch((error) => onMessage(error.message))}>重试</button>}</article>)}</div></aside>
    </div>
    <section className="composition-timeline"><div className="timeline-head"><b>时间线 · 单视频轨</b><span>仅导出已加入的片段</span><button onClick={() => seekTo(0)} disabled={!clips.length}>回到开头</button></div><div className="timeline-ruler">{[0, .25, .5, .75, 1].map((point) => <span key={point}>{fmt(totalMs * point)}</span>)}</div><div className="timeline-track">{clips.map((clip, index) => { const media = assets.find((item) => item.id === clip.sourceId); const duration = clip.outMs - clip.inMs; const width = totalMs ? Math.max(7, duration / totalMs * 100) : 0; return <div key={clip.id} className={clip.id === selected ? "timeline-clip active" : "timeline-clip"} style={{ width: `${width}%` }} draggable onDragStart={() => { dragIndex.current = index; }} onDragOver={(event) => event.preventDefault()} onDrop={(event) => dropClip(event, index)} onClick={() => { setSelected(clip.id); seekTo(clips.slice(0, index).reduce((sum, item) => sum + item.outMs - item.inMs, 0)); }}><img src={media?.thumbnailUrl ? `${API_BASE}${media.thumbnailUrl}` : undefined} alt="" /><b>{index + 1} · {media?.name ?? "素材不可用"}</b><small>{fmt(duration)}</small></div>; })}{!clips.length && <div className="timeline-empty">从左侧素材库显式添加视频，素材才会进入合成。</div>}</div><div className="timeline-playhead" style={{ left: `${totalMs ? playhead / totalMs * 100 : 0}%` }} /></section>
  </section>;
}
