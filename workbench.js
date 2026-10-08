'use strict';
/* UI state, persistence queue, list and editor are independent of media loading. */
const $ = id => document.getElementById(id);
const clone = value => JSON.parse(JSON.stringify(value));
const escapeText = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const uid = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const time = value => {value = Math.max(0, Number(value)||0);return `${String(Math.floor(value/60)).padStart(2,'0')}:${(value%60).toFixed(2).padStart(5,'0')}`;};
const keyName = key => ({space:'Space',enter:'Enter',backspace:'⌫',arrowleft:'←',arrowright:'→'}[key] || String(key||'—').toUpperCase());
const statusName = value => ({pending:'未审核',done:'已完成',review:'待复核'}[value] || value);
const collator = new Intl.Collator(undefined, {numeric:true});
const ACTIONS = {play:'播放 / 暂停',loop:'循环',back:'按秒快退',forward:'按秒快进',stepBack:'后退 1 秒',stepForward:'前进 1 秒',previous:'上一视频',next:'下一视频',addSegment:'添加区间',complete:'完成并下一条',undo:'撤销上一步',review:'待复核并下一条'};
Object.assign(ACTIONS,{settings:'项目设置',organize:'整理文件',search:'搜索视频',rescan:'刷新列表',proxy:'兼容当前视频',proxyAll:'后台兼容全部',fullscreen:'视频全屏',mute:'静音',setStart:'用当前画面设起点',setEnd:'用当前画面设终点',previewSegment:'预览区间',saveView:'保存筛选视图',switchProject:'切换项目',clearLabel:'清除整段标签',previousDevice:'上一设备',nextDevice:'下一设备'});
const state = {videos:[], config:null, current:null, seq:0, pending:[], draining:false, failed:false, info:null, infoPromise:null, settings:null, keys:null, draftId:null, folderTarget:null, folderMode:'directory', visibleRows:[], rowOffsets:[], personal:{}};

async function api(path, options={}) {
  const body = options.body === undefined ? undefined : JSON.stringify(options.body);
  let response;
  try {response = await fetch(path, {...options, body, headers:{'Content-Type':'application/json',...options.headers}});}
  catch (error) {if(error.name==='AbortError') throw error;throw new Error('无法连接审核服务，请确认程序仍在运行。未保存记录已保留，可重启后恢复。');}
  const data = await response.json();
  if (!response.ok) {const error = new Error(data.error || `请求失败 ${response.status}`);error.conflict = data.conflict;throw error;}
  if (data.seq) state.seq = Math.max(state.seq, data.seq);
  return data;
}
const post = (path, body={}) => api(path, {method:'POST',body});
let noticeTimer;
function notice(message, persistent=false) {
  clearTimeout(noticeTimer); $('notice').hidden=false; $('notice').textContent=message;
  if (!persistent) noticeTimer=setTimeout(()=>$('notice').hidden=true,6500);
}
function labelName(id) {return state.config?.labels.find(item=>item.id===id)?.name || id || '未标注';}
function labelColor(id) {return state.config?.labels.find(item=>item.id===id)?.color || '#8291a5';}
function projectKey(suffix) {return `videoReviewer.v2.${state.config.id}.${suffix}`;}
function effectiveKeys() {
  return {shortcuts:{...state.config.shortcuts,...state.personal.shortcuts},labels:Object.fromEntries(state.config.labels.filter(item=>item.active).map(item=>[item.id,state.personal.labels?.[item.id]??item.key])),seekSeconds:state.personal.seekSeconds??state.config.seekSeconds};
}
function validKeys(keys) {
  const values = [...Object.values(keys.shortcuts),...Object.values(keys.labels)].filter(Boolean);
  if(new Set(values).size!==values.length) throw new Error('有快捷键冲突，请为各操作选择不同的键');
}
function persistPending() {
  const payload = JSON.stringify(state.pending);
  try {localStorage.setItem(projectKey('pending'),payload);}
  catch {notice('浏览器无法保存未确认队列；请暂停连续标注并检查浏览器存储空间。',true);}
  updateSaveState();
}
function updateSaveState() {
  $('saveState').textContent=state.failed?`保存失败 ${state.pending.length}`:state.pending.length?`保存中 ${state.pending.length}`:'已保存';
  $('saveState').classList.toggle('error',state.failed);
}
function enqueue(annotation, advance=false) {
  if(!state.current) return;
  const video=state.current, before=clone(video.annotation);
  const next=advance?nextVideo(1):null;
  const task={id:video.id, annotation:clone(annotation), revision:before.revision, token:uid()};
  state.pending.push(task); video.annotation={...clone(annotation),revision:before.revision+1};
  persistPending(); renderAnnotation(); renderList(); renderProgress();
  drain();
  if(next) selectVideo(next.id);
  else if(advance) notice('已到当前队列末尾；可调整筛选或复核待确认内容');
}
async function drain() {
  if(state.draining||state.failed) return;
  state.draining=true;
  try {
    while(state.pending.length) {
      const task=state.pending[0];
      try {
        const data=await post('/api/annotation',task);
        state.pending.shift();
        const video=state.videos.find(item=>item.id===task.id);
        if(video&&!state.pending.some(item=>item.id===task.id)) video.annotation=data.annotation;
        persistPending(); renderProgress(); renderList();
      } catch(error) {
        state.failed=true; task.error=error.message; task.conflict=Boolean(error.conflict); persistPending();
        notice(`“${state.videos.find(v=>v.id===task.id)?.name||task.id}”保存失败：${error.message}`,true);
        break;
      }
    }
  } finally {state.draining=false;updateSaveState();}
}
async function flush() {
  if(state.failed) throw new Error('有标注保存失败，请先点击顶栏错误提示处理');
  await drain();
  while(state.draining) await new Promise(resolve=>setTimeout(resolve,50));
  if(state.pending.length) throw new Error('仍有未保存标注，暂不能执行此操作');
}
function patchAnnotation(changes, advance=false) {
  if(!state.current||(!deck.readyToReview&&changes.status!=='review')) {notice('请等待当前视频首帧载入后再标注；无法播放可标记待复核');return;}
  enqueue({...clone(state.current.annotation),...changes},advance);
}
function chooseLabel(id) {
  if(state.current?.annotation.segments.length&&state.config.negativeLabels.includes(id)) {notice('已有区间，与负类标签冲突；请确认后删除区间再选择');return;}
  patchAnnotation({label:id,status:state.config.quickSubmit?'done':state.current.annotation.status},state.config.quickSubmit);
}
function complete() {
  const annotation=state.current?.annotation;
  if(!annotation) return;
  if(!annotation.label&&!annotation.segments.length) {notice('请选择标签或添加区间；没有片段不代表无跌倒。无法判断可按 U 待复核。');return;}
  patchAnnotation({status:'done'},true);
}
function device(video) {
  if(video.metadataDeviceId) return video.metadataDeviceId;
  if(state.config.deviceRegex) {try{return new RegExp(state.config.deviceRegex).exec(video.name)?.[1]||'';}catch{}}
  return '';
}
function matching(video) {
  const query=$('search').value.trim().toLocaleLowerCase();
  const searchable=[video.name,device(video),...video.metadataLabels.map(item=>`${item.column} ${item.value}`),video.annotation.note].join(' ').toLocaleLowerCase();
  return (!query||searchable.includes(query))&&($('statusFilter').value==='all'||video.annotation.status===$('statusFilter').value)&&(!$('labelFilter').value||video.annotation.label===$('labelFilter').value||video.annotation.segments.some(item=>item.label===$('labelFilter').value));
}
function ordered() {const grouped=$('sort').value==='device';return [...state.videos].sort((a,b)=>(grouped?collator.compare(device(a),device(b)):0)||collator.compare(a.name,b.name)||collator.compare(a.relative,b.relative));}
function filtered() {return ordered().filter(matching);}
function nextVideo(delta) {
  const list=ordered(),position=list.findIndex(item=>item.id===state.current?.id);
  const candidates=delta>0?list.slice(position+1):list.slice(0,position).reverse();
  return candidates.find(matching)||null;
}
function renderProgress() {
  const done=state.videos.filter(video=>video.annotation.status==='done').length;
  const review=state.videos.filter(video=>video.annotation.status==='review').length;
  const pending=state.videos.length-done-review;
  $('progressText').textContent=`${done} / ${state.videos.length} 完成 · ${review} 待复核 · ${pending} 未审`;
  $('reviewProgress').value=state.videos.length?100*done/state.videos.length:0;
}
function renderList(scrollToCurrent=false) {
  const list=filtered(),rows=[];let group=null;
  if(state.current&&!matching(state.current)) rows.push({video:state.current,pinned:true});
  for(const video of list) {
    const value=device(video)||'设备未知';
    if($('sort').value==='device'&&value!==group) {group=value;rows.push({group:value});}
    rows.push({video});
  }
  state.visibleRows=rows; state.rowOffsets=[];let height=0;
  for(const row of rows) {state.rowOffsets.push(height);height+=row.group?28:76;}
  state.listHeight=height;
  $('queueCount').textContent=`${list.length} 条`;
  $('deviceSummary').textContent=`${new Set(state.videos.map(device).filter(Boolean)).size} 个设备 · 第一层视频`;
  if(scrollToCurrent&&state.current) {
    const index=rows.findIndex(row=>row.video?.id===state.current.id);
    const offset=state.rowOffsets[index];
    const element=$('videoList');
    if(offset<element.scrollTop||offset+76>element.scrollTop+element.clientHeight) element.scrollTop=Math.max(0,offset-element.clientHeight/2+38);
  }
  paintList();
}
function paintList() {
  const list=$('videoList'),top=list.scrollTop,bottom=top+list.clientHeight;
  if(!state.visibleRows.length) {list.innerHTML='<div class="empty-list">没有符合条件的视频<br>可清除搜索或调整筛选</div>';return;}
  const entries=[];
  for(let index=0;index<state.visibleRows.length;index++) {
    const offset=state.rowOffsets[index];
    if(offset<top-180||offset>bottom+180) continue;
    const row=state.visibleRows[index];
    if(row.group) entries.push(`<div class="device-group" style="position:absolute;top:${offset}px;left:0;right:0;height:28px">${escapeText(row.group)}</div>`);
    else {
      const video=row.video,a=video.annotation;
      entries.push(`<button class="video-item ${video.id===state.current?.id?'active':''}" data-video="${video.id}" style="position:absolute;top:${offset}px;left:0;height:76px" role="option" aria-selected="${video.id===state.current?.id}" title="${escapeText(video.relative)}"><div class="filename">${escapeText(video.name)}</div><div class="item-bottom"><span class="device">${escapeText(device(video)||'—')}</span><span class="badge ${a.status}">${row.pinned?'当前 · 筛选外':statusName(a.status)}</span></div><div style="font-size:11px;color:${labelColor(a.label)};overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeText(labelName(a.label))}${a.segments.length?` · ${a.segments.length} 区间`:''}${video.originLabel&&!a.label?` · 原分类：${escapeText(labelName(video.originLabel))}`:''}</div></button>`);
    }
  }
  list.innerHTML=`<div style="height:${state.listHeight}px;position:relative">${entries.join('')}</div>`;
}
$('videoList').addEventListener('scroll',()=>requestAnimationFrame(paintList));
$('videoList').addEventListener('click',event=>{const button=event.target.closest('[data-video]');if(button)selectVideo(button.dataset.video);});

let cursorTimer;
function selectVideo(id) {
  const video=state.videos.find(item=>item.id===id);if(!video)return;
  state.current=video;state.info=null;state.draftId=null;
  $('videoName').textContent=video.name;$('videoName').title=video.name;
  $('videoDetails').textContent=[device(video)&&`设备 ${device(video)}`,video.relative].filter(Boolean).join(' · ');$('videoDetails').title=video.relative;
  $('segmentStart').value=0;$('segmentEnd').value=state.config.segmentSeconds;
  renderMetadata();renderAnnotation();renderList(true);
  deck.select(id,nextVideo(1)?.id);
  state.infoPromise=state.config.intervals?api(`/api/video-info?id=${encodeURIComponent(id)}`).then(info=>{if(state.current?.id===id){state.info=info;updateDraft();}return info;}).catch(error=>{if(state.current?.id===id)notice(`无法读取原视频区间信息：${error.message}`);return null;}):null;
  clearTimeout(cursorTimer);cursorTimer=setTimeout(()=>post('/api/cursor',{id}).catch(()=>{}),600);
}
function renderMetadata() {
  const video=state.current;if(!video)return;
  let show=true;try{show=localStorage.getItem(projectKey('showPriors'))!=='false';}catch{}
  const badges=[];
  if(video.originLabel)badges.push(`<span class="badge" title="目录带入的原分类，不自动计为本轮完成">原分类：${escapeText(labelName(video.originLabel))}</span>`);
  if(show) for(const item of video.metadataLabels)badges.push(`<span class="badge" title="${escapeText(`${item.column}: ${item.value}`)}">${escapeText(item.column)}：${escapeText(item.value)}</span>`);
  $('metadata').innerHTML=badges.join('');
}
function renderAnnotation() {
  const a=state.current?.annotation;
  if(!a)return;
  $('annotationStatus').textContent=statusName(a.status);$('annotationStatus').className=`badge ${a.status}`;
  $('quickHint').textContent=state.config.quickSubmit?'选择后下一条':'Enter 完成';
  const keys=effectiveKeys(),query=$('labelSearch').value.toLocaleLowerCase();
  $('labelSearch').hidden=state.config.labels.filter(item=>item.active).length<=8;
  $('labelButtons').innerHTML=state.config.labels.filter(item=>item.active&&item.name.toLocaleLowerCase().includes(query)).map(item=>`<button class="label-button ${a.label===item.id?'selected':''}" data-label="${item.id}" style="--label-color:${item.color}" title="${escapeText(item.description)}" ${deck&&!deck.readyToReview?'disabled':''}><span>${escapeText(item.name)}</span><kbd>${keyName(keys.labels[item.id])}</kbd></button>`).join('');
  $('auxTags').innerHTML=state.config.tags.map(tag=>`<button class="aux-tag ${a.tags.includes(tag)?'active':''}" data-tag="${escapeText(tag)}">${escapeText(tag)}</button>`).join('');
  if(document.activeElement!==$('note'))$('note').value=a.note;
  $('segmentSection').hidden=!state.config.intervals;$('intervalTools').hidden=!state.config.intervals;
  $('segmentCount').textContent=a.segments.length;
  $('segmentList').innerHTML=a.segments.length?a.segments.map(item=>`<div class="segment-card"><span style="color:${labelColor(item.label)}">${escapeText(labelName(item.label))}</span> · ${time(item.start)}–${time(item.end)}<div class="row"><button data-preview="${item.id}">预览</button><button data-edit="${item.id}">编辑</button><button data-remove="${item.id}">删除</button></div></div>`).join(''):'<p class="hint">Space 添加当前区间</p>';
  $('position').textContent=`${state.videos.indexOf(state.current)+1} / ${state.videos.length}`;
  $('segmentEnd').readOnly=!state.config.freeSegments;
  renderTimeline();
}
$('labelButtons').onclick=event=>{const button=event.target.closest('[data-label]');if(button)chooseLabel(button.dataset.label);};
$('auxTags').onclick=event=>{const button=event.target.closest('[data-tag]');if(!button)return;const tags=new Set(state.current.annotation.tags);tags.has(button.dataset.tag)?tags.delete(button.dataset.tag):tags.add(button.dataset.tag);patchAnnotation({tags:[...tags]});};
$('segmentList').onclick=event=>{const button=event.target.closest('button');if(!button)return;const id=button.dataset.preview||button.dataset.edit||button.dataset.remove;const segment=state.current.annotation.segments.find(item=>item.id===id);if(!segment)return;if(button.dataset.remove)patchAnnotation({segments:state.current.annotation.segments.filter(item=>item.id!==id)});else if(button.dataset.preview)deck.preview(segment.start,segment.end);else{state.draftId=id;$('segmentStart').value=segment.start;$('segmentEnd').value=segment.end;$('segmentLabel').value=segment.label;$('addSegment').textContent='保存修改';updateDraft();deck.seek(segment.start,true);}};
function renderTimeline() {
  const duration=state.info?.duration||deck?.player.duration||0;
  if(!duration||!Number.isFinite(duration))return;
  $('segmentMarkers').innerHTML=state.current.annotation.segments.map(item=>`<button class="segment-marker" data-marker="${item.id}" style="left:${100*item.start/duration}%;width:${100*(item.end-item.start)/duration}%;background:${labelColor(item.label)}" title="${escapeText(labelName(item.label))} ${time(item.start)}–${time(item.end)}"></button>`).join('');updateDraft();
}
$('segmentMarkers').onclick=event=>{const id=event.target.dataset.marker;const segment=state.current.annotation.segments.find(item=>item.id===id);if(segment)deck.preview(segment.start,segment.end);};
function updateDraft() {
  if(!state.config?.intervals)return;
  const duration=state.info?.duration||deck?.player.duration;
  let start=Number($('segmentStart').value)||0;
  if(duration>0)start=Math.min(Math.max(0,start),Math.max(0,duration-(state.config.freeSegments?0.04:Math.min(state.config.segmentSeconds,duration))));
  $('segmentStart').value=Number(start.toFixed(3));
  if(!state.config.freeSegments)$('segmentEnd').value=Number(Math.min(duration||Infinity,start+state.config.segmentSeconds).toFixed(3));
  const end=Number($('segmentEnd').value);
  $('draftMarker').hidden=!(duration>0);
  if(duration>0){$('draftMarker').style.left=`${100*start/duration}%`;$('draftMarker').style.width=`${100*Math.max(0,end-start)/duration}%`;}
}
async function addSegment() {
  if(!state.current||!deck.readyToReview||!state.config.intervals)return;
  const id=state.current.id;
  if(state.infoPromise)await state.infoPromise;
  if(state.current?.id!==id)return;
  if(!state.info||!(state.info.duration>0)){notice('原视频时长未读到，不能保存区间');return;}
  let start=Number($('segmentStart').value),end=Number($('segmentEnd').value);
  if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start||start<0||end>state.info.duration+0.08){notice('请检查区间起止时间');return;}
  if(state.config.snapKeyframes&&state.info.keyframes.length){const nearest=[...state.info.keyframes].reverse().find(value=>value<=start+1e-6)??0;if(Math.abs(start-nearest)>.02){const length=end-start;start=nearest;end=Math.min(state.info.duration,start+length);notice(`零重编码起点已对齐关键帧：${time(start)}`);}}
  const segments=clone(state.current.annotation.segments).filter(item=>item.id!==state.draftId);
  if(segments.some(item=>start<item.end-.001&&item.start<end-.001)){notice('区间与已有区间重叠，请调整范围');return;}
  segments.push({id:state.draftId||uid(),start,end,label:$('segmentLabel').value});segments.sort((a,b)=>a.start-b.start);
  const label=state.config.negativeLabels.includes(state.current.annotation.label)?null:state.current.annotation.label;
  patchAnnotation({segments,label});state.draftId=null;$('addSegment').textContent='添加区间';$('segmentStart').value=start;$('segmentEnd').value=end;updateDraft();
}
function seek(delta) {deck.clearBoundary();deck.seek(delta);if(state.config.intervals){$('segmentStart').value=deck.seeking??deck.player.currentTime;updateDraft();}}

let deck=new MediaDeck([$ ('videoA'),$('videoB')],api,{
  onPreferences:(preferences,playing)=>{$('rate').value=String(preferences.rate);$('loop').classList.toggle('active',preferences.loop);$('mute').classList.toggle('active',preferences.muted);$('play').textContent=playing?'Ⅱ 暂停':'▶ 播放';$('autoplay').checked=preferences.autoplay;},
  onLoading:message=>{$('mediaOverlay').hidden=false;$('mediaOverlay').textContent=message;$('mediaOverlay').classList.remove('error');if(state.config)renderAnnotation();},
  onReady:(id,proxy,duration)=>{if(state.current?.id!==id)return;$('mediaOverlay').hidden=true;$('previewKind').textContent=proxy?'兼容预览 · 导出使用原视频':'原视频预览 · 不改变源文件';state.current.duration=duration;$('clock').textContent=`${time(deck.player.currentTime)} / ${time(duration)}`;renderAnnotation();renderTimeline();},
  onError:message=>{$('mediaOverlay').hidden=false;$('mediaOverlay').textContent=message;$('mediaOverlay').classList.add('error');},
  onTime:(current,duration)=>{$('clock').textContent=`${time(current)} / ${time(duration)}`;if(duration>0){$('seek').value=100*current/duration;$('playhead').style.left=`${100*current/duration}%`;}}
});
$('mediaOverlay').onclick=()=>deck.play();$('play').onclick=()=>deck.toggle();$('loop').onclick=()=>deck.set('loop',!deck.preferences.loop);$('mute').onclick=()=>deck.set('muted',!deck.preferences.muted);$('rate').onchange=()=>{deck.set('rate',Number($('rate').value));$('rate').blur();};$('autoplay').onchange=()=>deck.set('autoplay',$('autoplay').checked);
$('back').onclick=()=>seek(-effectiveKeys().seekSeconds);$('forward').onclick=()=>seek(effectiveKeys().seekSeconds);
$('seek').oninput=()=>{deck.clearBoundary();deck.seek(Number($('seek').value)*deck.player.duration/100,true);};
$('previous').onclick=()=>{const next=nextVideo(-1);if(next)selectVideo(next.id);};$('next').onclick=()=>{const next=nextVideo(1);if(next)selectVideo(next.id);};
$('complete').onclick=complete;$('markReview').onclick=()=>patchAnnotation({status:'review'},true);$('addSegment').onclick=()=>addSegment().catch(error=>notice(error.message));
$('setStart').onclick=()=>{$('segmentStart').value=deck.player.currentTime;updateDraft();};
$('segmentStart').onchange=()=>{updateDraft();deck.clearBoundary();deck.seek(Number($('segmentStart').value),true);};$('segmentEnd').onchange=updateDraft;
$('previewSegment').onclick=()=>deck.preview(Number($('segmentStart').value),Number($('segmentEnd').value));
$('note').onblur=()=>{if(state.current&&$('note').value!==state.current.annotation.note)patchAnnotation({note:$('note').value});};
$('undo').onclick=async()=>{try{await flush();const result=await post('/api/undo');const video=state.videos.find(item=>item.id===result.id);if(video){video.annotation=result.annotation;selectVideo(video.id);}renderProgress();}catch(error){notice(error.message);}};
$('retryPreview').onclick=()=>deck.proxy(deck.slot,true);
let proxyAllTimer;
$('proxyAll').onclick=async()=>{try{await post('/api/proxy-all');$('proxyAll').disabled=true;const poll=async()=>{try{const job=await api('/api/proxy-all-status');$('proxyAllStatus').textContent=`${job.done||0}/${job.total||0} · 成功 ${job.ready||0} · 失败 ${job.failed||0}`;if(job.status==='running')proxyAllTimer=setTimeout(poll,1000);else{$('proxyAll').disabled=false;$('proxyAllStatus').textContent=job.message;}}catch(error){$('proxyAllStatus').textContent=error.message;$('proxyAll').disabled=false;}};clearTimeout(proxyAllTimer);poll();}catch(error){notice(error.message);}};
$('fullscreen').onclick=()=>{$('videoStage').requestFullscreen?.().catch(()=>notice('此浏览器暂不支持全屏'));};
for(const id of ['search','statusFilter','labelFilter','sort'])$(id).addEventListener(id==='search'?'input':'change',()=>{renderList(true);deck.warm(nextVideo(1)?.id);});
$('labelSearch').oninput=renderAnnotation;
$('collapseList').onclick=()=>{document.body.classList.toggle('queue-hidden');try{localStorage.setItem('videoReviewer.v2.queueHidden',document.body.classList.contains('queue-hidden'));}catch{}};
try{document.body.classList.toggle('queue-hidden',localStorage.getItem('videoReviewer.v2.queueHidden')==='true');const width=Number(localStorage.getItem('videoReviewer.v2.queueWidth'));if(width>=200&&width<=420)document.documentElement.style.setProperty('--side',width+'px');}catch{}
$('resizeHandle').onpointerdown=event=>{event.preventDefault();const handle=event.currentTarget;handle.setPointerCapture(event.pointerId);handle.onpointermove=e=>{const width=Math.min(420,Math.max(200,e.clientX));document.documentElement.style.setProperty('--side',width+'px');try{localStorage.setItem('videoReviewer.v2.queueWidth',width);}catch{}};handle.onpointerup=()=>handle.onpointermove=null;};
$('resizeHandle').onkeydown=event=>{if(!['ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();const width=$('videoList').clientWidth+(event.key==='ArrowRight'?10:-10);document.documentElement.style.setProperty('--side',Math.min(420,Math.max(200,width))+'px');};
function adopt(document, keepCurrent=true) {
  const id=keepCurrent?state.current?.id:null;
  state.config=document.config;state.videos=document.videos;state.seq=Math.max(state.seq,document.seq);state.document=document;
  try{state.personal=JSON.parse(localStorage.getItem(projectKey('keys'))||'{}');validKeys(effectiveKeys());}catch{state.personal={};}
  $('projectName').textContent=state.config.name;$('projectName').title=document.source;
  const selected=$('labelFilter').value;
  $('labelFilter').innerHTML='<option value="">全部标签</option>'+state.config.labels.map(item=>`<option value="${item.id}">${escapeText(item.name)}</option>`).join('');$('labelFilter').value=selected;
  $('segmentLabel').innerHTML=state.config.labels.filter(item=>item.active&&!state.config.negativeLabels.includes(item.id)).map(item=>`<option value="${item.id}">${escapeText(item.name)}</option>`).join('');
  if(!$('segmentLabel').options.length)$('segmentLabel').innerHTML=state.config.labels.filter(item=>item.active).map(item=>`<option value="${item.id}">${escapeText(item.name)}</option>`).join('');
  $('views').innerHTML='<option value="">已保存视图…</option>'+document.views.map((view,index)=>`<option value="${index}">${escapeText(view.name)}</option>`).join('');
  $('back').textContent=`−${effectiveKeys().seekSeconds}s`;$('forward').textContent=`+${effectiveKeys().seekSeconds}s`;
  $('shortcutHelp').innerHTML=Object.entries(effectiveKeys().shortcuts).filter(([,key])=>key).map(([action,key])=>`<span><kbd>${keyName(key)}</kbd> ${escapeText(ACTIONS[action]||action)}</span>`).join('');
  renderProgress();
  const current=state.videos.find(item=>item.id===(id||document.cursor))||state.videos.find(item=>item.annotation.status==='pending')||state.videos[0];
  if(current)selectVideo(current.id);else{$('mediaOverlay').textContent='当前目录没有视频，可在项目设置添加输入目录';renderList();}
  if(document.warning)notice(document.warning,true);
  else if(document.inputWarnings?.length)notice(document.inputWarnings.join('；'),true);
  else if(document.missing.length)notice(`有 ${document.missing.length} 个已记录视频不在当前目录中；标注保留。请检查挂载或额外输入目录。`,true);
}
async function refresh() {await flush();adopt(await api('/api/project'));}
$('rescan').onclick=async()=>{try{await flush();adopt(await post('/api/rescan'));notice('列表已刷新，原标注保留');}catch(error){notice(error.message);}};
$('saveView').onclick=async()=>{const name=prompt('视图名称，例如“设备 A 的未审核视频”');if(!name)return;try{const views=[...state.document.views,{name,search:$('search').value,status:$('statusFilter').value,label:$('labelFilter').value,sort:$('sort').value}];await post('/api/views',{views});state.document.views=views;$('views').innerHTML='<option value="">已保存视图…</option>'+views.map((view,index)=>`<option value="${index}">${escapeText(view.name)}</option>`).join('');}catch(error){notice(error.message);}};
$('views').onchange=()=>{const view=state.document.views[Number($('views').value)];if(!view||$('views').value==='')return;$('search').value=view.search;$('statusFilter').value=view.status;$('labelFilter').value=view.label;$('sort').value=view.sort;renderList(true);};

// Project settings are edited as a draft; Cancel never mutates saved rules.
function showTab(name) {document.querySelectorAll('[data-tab]').forEach(element=>element.classList.toggle('active',element.dataset.tab===name));document.querySelectorAll('[data-pane]').forEach(element=>element.hidden=element.dataset.pane!==name);}
document.querySelectorAll('[data-tab]').forEach(element=>element.onclick=()=>showTab(element.dataset.tab));
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$(button.dataset.close).close());
async function openSettings(tab='labels') {
  try {
    clearTimeout(cursorTimer);await flush();const document=await api('/api/project');state.seq=document.seq;state.document=document;state.settings=clone(document.config);state.keys=projectKeys(state.settings);
    populateSettings();
    try{$('showPriors').checked=localStorage.getItem(projectKey('showPriors'))!=='false';}catch{}
    $('settingsError').textContent='';renderLabelEditor();renderKeyEditor();loadMetadataFields(document);showTab(tab);$('settingsDialog').showModal();
  }catch(error){notice(error.message);}
}
function projectKeys(config) {return {shortcuts:clone(config.shortcuts),labels:Object.fromEntries(config.labels.filter(item=>item.active).map(item=>[item.id,item.key])),seekSeconds:config.seekSeconds};}
function populateSettings() {
    $('configName').value=state.settings.name;$('configTags').value=state.settings.tags.join(', ');
    for(const [id,key] of Object.entries({configIntervals:'intervals',configFree:'freeSegments',configSnap:'snapKeyframes',configQuick:'quickSubmit',configFlat:'clipFlat'}))$(id).checked=state.settings[key];
    for(const [id,key] of Object.entries({configSeconds:'segmentSeconds',configExport:'exportMode',configOutput:'output',configClipOutput:'clipOutput'}))$(id).value=state.settings[key];
    $('configInputs').value=state.settings.inputs.join('\n');$('configWhole').value=state.settings.wholeLabels.join(',');$('configNegative').value=state.settings.negativeLabels.join(',');$('configSeek').value=state.keys.seekSeconds;$('deviceRegex').value=state.settings.deviceRegex||'';
}
function renderLabelEditor() {
  $('labelEditor').innerHTML=state.settings.labels.map((item,index)=>`<div class="label-edit-row" data-index="${index}"><input type="checkbox" data-field="active" ${item.active?'checked':''} aria-label="启用 ${escapeText(item.name)}"><input type="color" data-field="color" value="${item.color}" aria-label="颜色"><div class="name-fields"><input data-field="name" value="${escapeText(item.name)}" aria-label="标签名称"><input data-field="description" value="${escapeText(item.description)}" placeholder="判定说明" aria-label="标签说明"></div><input class="shortcut-key" data-field="key" value="${keyName(item.key===' '? 'space':item.key)}" readonly aria-label="标签快捷键"><input data-field="folder" value="${escapeText(item.folder)}" title="标签 ID：${item.id}" aria-label="输出子目录"><button data-delete="${index}" title="删除未使用标签；已使用标签请停用">×</button></div>`).join('');
  $('destinationEditor').innerHTML=state.settings.labels.map(item=>`<label class="field">${escapeText(item.name)} 的独立目录（空表示默认）<input data-destination="${item.id}" value="${escapeText(state.settings.destinations[item.id]||'')}"></label>`).join('');
  document.querySelectorAll('#labelEditor [data-field]').forEach(input=>{const item=state.settings.labels[Number(input.closest('[data-index]').dataset.index)];const field=input.dataset.field;if(field==='key')input.onkeydown=event=>{event.preventDefault();event.stopPropagation();if(event.key==='Delete'||event.key==='Backspace')item.key='';else if(event.key.length===1&&!event.ctrlKey&&!event.metaKey&&!event.altKey)item.key=event.key.toLowerCase();state.keys.labels[item.id]=item.key;input.value=keyName(item.key);};else input.oninput=()=>item[field]=field==='active'?input.checked:input.value;});
}
$('labelEditor').onclick=event=>{const index=event.target.dataset.delete;if(index===undefined)return;const item=state.settings.labels[Number(index)];if(state.videos.some(video=>video.annotation.label===item.id||video.annotation.segments.some(segment=>segment.label===item.id))){$('settingsError').textContent='该标签已使用，请取消启用，不要删除';return;}state.settings.labels.splice(Number(index),1);delete state.keys.labels[item.id];state.settings.wholeLabels=state.settings.wholeLabels.filter(id=>id!==item.id);state.settings.negativeLabels=state.settings.negativeLabels.filter(id=>id!==item.id);$('configWhole').value=state.settings.wholeLabels.join(',');$('configNegative').value=state.settings.negativeLabels.join(',');renderLabelEditor();renderKeyEditor();};
$('newLabel').onclick=()=>{const id=`label_${uid().replace(/-/g,'').slice(0,12)}`;state.settings.labels.push({id,name:'新标签',description:'',color:'#86afe9',key:'',folder:id,active:true});renderLabelEditor();};
function captureKey(event) {if(event.ctrlKey||event.metaKey||event.altKey)return null;if(['Shift','Control','Alt','Meta','Tab','Escape','CapsLock'].includes(event.key))return null;return event.key===' '?'space':event.key.toLowerCase();}
function renderKeyEditor() {
  state.keys.labels=Object.fromEntries(state.settings.labels.filter(item=>item.active).map(item=>[item.id,state.keys.labels[item.id]??item.key]));
  const fields=[...Object.entries(ACTIONS).map(([id,name])=>({id,name,kind:'shortcuts'})),...state.settings.labels.filter(item=>item.active).map(item=>({id:item.id,name:item.name,kind:'labels'}))];
  $('shortcutEditor').innerHTML=fields.map(field=>`<label>${escapeText(field.name)}<input class="shortcut-key" readonly data-kind="${field.kind}" data-action="${field.id}" value="${keyName(state.keys[field.kind][field.id])}" aria-label="${escapeText(field.name)}的快捷键"></label>`).join('');
  document.querySelectorAll('#shortcutEditor input').forEach(input=>input.onkeydown=event=>{event.preventDefault();event.stopPropagation();const key=event.key==='Delete'?'':captureKey(event);if(key===null)return;const before=state.keys[input.dataset.kind][input.dataset.action];state.keys[input.dataset.kind][input.dataset.action]=key;try{validKeys(state.keys);$('settingsError').textContent='';input.value=keyName(key);}catch(error){state.keys[input.dataset.kind][input.dataset.action]=before;$('settingsError').textContent=error.message;}});
}
function readConfig() {
  const config=clone(state.settings);config.name=$('configName').value;config.tags=$('configTags').value.split(/[,，]/).map(item=>item.trim()).filter(Boolean);
  for(const [id,key] of Object.entries({configIntervals:'intervals',configFree:'freeSegments',configSnap:'snapKeyframes',configQuick:'quickSubmit',configFlat:'clipFlat'}))config[key]=$(id).checked;
  for(const [id,key] of Object.entries({configSeconds:'segmentSeconds',configExport:'exportMode',configOutput:'output',configClipOutput:'clipOutput'}))config[key]=$(id).value;
  config.inputs=$('configInputs').value.split('\n').map(item=>item.trim()).filter(Boolean);config.wholeLabels=$('configWhole').value.split(',').map(item=>item.trim()).filter(Boolean);config.negativeLabels=$('configNegative').value.split(',').map(item=>item.trim()).filter(Boolean);
  config.seekSeconds=Number($('configSeek').value);config.shortcuts=clone(state.keys.shortcuts);
  for(const label of config.labels) {if(state.keys.labels[label.id]!==undefined)label.key=state.keys.labels[label.id];}
  config.destinations={};document.querySelectorAll('[data-destination]').forEach(input=>{if(input.value.trim())config.destinations[input.dataset.destination]=input.value.trim();});
  config.deviceRegex=$('deviceRegex').value.trim();if(config.deviceRegex)new RegExp(config.deviceRegex);
  return config;
}
$('settings').onclick=()=>openSettings();$('playbackSettings').onclick=()=>$('playbackDialog').showModal();$('openKeys').onclick=()=>{$('playbackDialog').close();openSettings('keys');};
$('saveConfig').onclick=async()=>{try{await flush();const config=readConfig();const fresh=await api('/api/project');adopt(await post('/api/project-config',{seq:fresh.seq,configRevision:state.settings.revision||0,config}));$('settingsDialog').close();notice('项目规则已保存，原有标注保留');}catch(error){$('settingsError').textContent=error.message;}};
$('savePersonalKeys').onclick=()=>{try{state.keys.seekSeconds=Number($('configSeek').value);if(!Number.isFinite(state.keys.seekSeconds)||state.keys.seekSeconds<.04||state.keys.seekSeconds>86400)throw new Error('跳转秒数需在 0.04–86400 之间');validKeys(state.keys);state.personal=clone(state.keys);localStorage.setItem(projectKey('keys'),JSON.stringify(state.personal));$('settingsError').textContent='个人快捷键已保存；不影响同事的项目默认值';$('back').textContent=`−${effectiveKeys().seekSeconds}s`;$('forward').textContent=`+${effectiveKeys().seekSeconds}s`;renderAnnotation();}catch(error){$('settingsError').textContent=error.message;}};
$('resetPersonalKeys').onclick=()=>{state.personal={};localStorage.removeItem(projectKey('keys'));state.keys=clone(effectiveKeys());$('configSeek').value=state.keys.seekSeconds;renderKeyEditor();renderAnnotation();};
function download(name,data) {const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const anchor=document.createElement('a');anchor.href=url;anchor.download=name;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$('exportTemplate').onclick=()=>{try{const config=readConfig();for(const key of ['id','metadataConfig','destinations','inputs'])delete config[key];config.output='output';config.clipOutput='output/clips';download('video-reviewer-template.json',config);}catch(error){$('settingsError').textContent=error.message;}};
$('importTemplate').onchange=async event=>{try{const template=JSON.parse(await event.target.files[0].text());if(template.version!==2||!Array.isArray(template.labels))throw new Error('不是有效的 v2 规则模板');state.settings={...state.settings,...template,id:state.config.id,destinations:state.settings.destinations,inputs:state.settings.inputs,metadataConfig:state.settings.metadataConfig};state.keys=projectKeys(state.settings);populateSettings();renderLabelEditor();renderKeyEditor();$('settingsError').textContent='模板已载入草稿，点击保存才生效；已有标签不可删除';}catch(error){$('settingsError').textContent=error.message;}event.target.value='';};
$('showPriors').onchange=()=>{localStorage.setItem(projectKey('showPriors'),$('showPriors').checked);renderMetadata();};
$('newRound').onclick=async()=>{if(!confirm('保留标签和片段，将全部视频作为新一轮未审核内容继续复核？'))return;try{await flush();adopt(await post('/api/new-round'));$('settingsDialog').close();notice('新一轮已开始，原标签与区间保留');}catch(error){$('settingsError').textContent=error.message;}};

function loadMetadataFields(document) {
  const config=document.metadataConfig||{};state.csvColumns=document.metadataColumns||[];$('csvPath').value=document.metadataCsv||'';
  populateColumns(config);$('metadataSummary').textContent=`已匹配 ${document.metadataMatched||0}/${document.videos.length}；冲突 ${document.metadataConflicts||0}。CSV 不会写入修改。`;
}
function populateColumns(config={}) {
  for(const id of ['csvFile','csvDevice','csvLabel1','csvLabel2','csvLabel3'])$(id).innerHTML='<option value="">不选择</option>'+state.csvColumns.map(column=>`<option value="${escapeText(column)}">${escapeText(column)}</option>`).join('');
  $('csvFile').value=config.fileColumn||'';$('csvDevice').value=config.deviceColumn||'';(config.labelColumns||[]).slice(0,3).forEach((column,index)=>$('csvLabel'+(index+1)).value=column);
}
  $('readCsv').onclick=async()=>{try{const data=await api('/api/csv-columns?'+new URLSearchParams({path:$('csvPath').value}));state.csvColumns=data.columns;populateColumns(data.guess||{});}catch(error){$('settingsError').textContent=error.message;}};
async function applyMetadata(payload) {try{await flush();const document=await post('/api/metadata-config',payload);adopt(document);loadMetadataFields(document);state.settings.metadataConfig=document.config.metadataConfig;state.settings.revision=document.config.revision;notice('CSV 对照已更新；人工标注没有改变');}catch(error){$('settingsError').textContent=error.message;}}
$('applyCsv').onclick=()=>applyMetadata({path:$('csvPath').value,fileColumn:$('csvFile').value,deviceColumn:$('csvDevice').value,labelColumns:[$('csvLabel1').value,$('csvLabel2').value,$('csvLabel3').value].filter(Boolean)});
$('autoCsv').onclick=()=>applyMetadata({auto:true});$('disableCsv').onclick=()=>applyMetadata({disabled:true});

async function browseFolder(target,mode='directory') {state.folderTarget=target;state.folderMode=mode;$('folderTitle').textContent=mode==='csv'?'选择 CSV 文件':'选择目录';$('chooseFolder').hidden=mode==='csv';$('folderDialog').showModal();await loadFolder($(target).value||state.document.source);}
async function loadFolder(path) {
  try {
    const data=await api((state.folderMode==='csv'?'/api/csv-browser':'/api/directories')+'?'+new URLSearchParams({path}));state.folder=data;$('folderPath').value=data.path;$('folderSelection').textContent=data.path;$('parentFolder').disabled=!data.parent;$('folderError').textContent='';
    $('folderList').innerHTML=data.directories.map(item=>`<button class="folder-entry" data-folder="${escapeText(item.path)}">▸ ${escapeText(item.name)}</button>`).join('')+(data.csvFiles||[]).map(item=>`<button class="folder-entry" data-file="${escapeText(item.path)}">▦ ${escapeText(item.name)}</button>`).join('');
  }catch(error){$('folderError').textContent=error.message;}
}
document.querySelectorAll('[data-browse]').forEach(button=>button.onclick=()=>browseFolder(button.dataset.browse));$('browseCsv').onclick=()=>browseFolder('csvPath','csv');$('goFolder').onclick=()=>loadFolder($('folderPath').value);$('parentFolder').onclick=()=>loadFolder(state.folder.parent);$('folderPath').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();loadFolder(event.target.value);}};
$('folderList').onclick=event=>{const button=event.target.closest('button');if(button?.dataset.folder)loadFolder(button.dataset.folder);else if(button?.dataset.file){$('csvPath').value=button.dataset.file;$('folderDialog').close();$('readCsv').click();}};
$('chooseFolder').onclick=()=>{$(state.folderTarget).value=state.folder.path;$('folderDialog').close();};

let exportTimer;
async function showExport() {try{await flush();const plan=await api('/api/export-plan');state.exportPlan=plan;$('exportDescription').textContent=state.config.exportMode==='move'?'确认后移动已完成整段视频；区间导出不删除原视频。':'确认后复制整段视频 / 截取区间；原视频保留。';$('exportCounts').innerHTML=Object.entries(plan.counts).map(([label,count])=>`<p>${escapeText(labelName(label))}：${count}</p>`).join('');$('exportMessage').textContent=plan.message;$('exportErrors').textContent=plan.conflicts.join('\n');$('exportProgress').value=0;$('startExport').disabled=!!plan.conflicts.length||!plan.items.length;$('startExport').textContent='确认整理';$('exportDialog').showModal();pollExport();}catch(error){notice(error.message);}}
async function pollExport() {clearTimeout(exportTimer);try{const job=await api('/api/export-status');if(job.status!=='idle'){$('exportProgress').value=job.total?100*job.done/job.total:100;$('exportMessage').textContent=job.message;$('exportErrors').textContent=(job.failures||[]).map(item=>`${item.name}：${item.error}`).join('\n');$('startExport').disabled=job.status==='running';$('startExport').textContent=job.status==='error'?'重试失败项':'重新检查';if(job.status==='running')exportTimer=setTimeout(pollExport,500);else if(state.exportRunning){state.exportRunning=false;adopt(await api('/api/project'));state.exportPlan=await api('/api/export-plan');$('startExport').disabled=!state.exportPlan.items.length;}}}catch(error){$('exportErrors').textContent=error.message;}}
$('organize').onclick=showExport;$('startExport').onclick=async()=>{if(state.exportRunning)return;$('startExport').disabled=true;state.exportRunning=true;try{await flush();const plan=await api('/api/export-plan');if(plan.conflicts.length)throw new Error(plan.conflicts.join('\n'));if(!plan.items.length){state.exportRunning=false;$('exportMessage').textContent='全部完成，无需重复整理';return;}await post('/api/export',{seq:plan.seq});pollExport();}catch(error){state.exportRunning=false;$('startExport').disabled=false;$('exportErrors').textContent=error.message;}};

$('saveState').onclick=()=>{if(!state.failed)return;$('failedSaves').innerHTML=state.pending.map(item=>`<p>${escapeText(state.videos.find(video=>video.id===item.id)?.name||item.id)}：${escapeText(item.error||'排队中')}</p>`).join('');$('failureDialog').showModal();};
$('retrySaves').onclick=async()=>{if(state.pending.some(item=>item.conflict)){notice('存在修改冲突，不能自动覆盖。请下载未保存记录，刷新后确认服务器版本再重新标注。',true);return;}state.failed=false;await drain();if(!state.failed)$('failureDialog').close();};$('downloadFailed').onclick=()=>download('video-reviewer-unsaved.json',{projectId:state.config.id,records:state.pending});
const resolveButton=document.createElement('button');resolveButton.textContent='采用服务器版本解决冲突';$('failureDialog').querySelector('.dialog-footer').append(resolveButton);
resolveButton.onclick=async()=>{const ids=new Set(state.pending.filter(item=>item.conflict).map(item=>item.id));if(!ids.size){notice('没有版本冲突；连接错误请重试保存');return;}if(!confirm('将先下载本地未保存记录，再采用服务器上的这些视频标注。其他视频的排队记录保留。是否继续？'))return;download('video-reviewer-unsaved.json',{projectId:state.config.id,records:state.pending});try{const document=await api('/api/project');state.pending=state.pending.filter(item=>!ids.has(item.id));state.failed=false;adopt(document);for(const task of state.pending){const video=state.videos.find(item=>item.id===task.id);if(video)video.annotation={...task.annotation,revision:task.revision+1};}persistPending();await drain();renderAnnotation();renderList();renderProgress();if(!state.failed)$('failureDialog').close();}catch(error){notice(error.message,true);}};
$('switchProject').onclick=async()=>{try{clearTimeout(cursorTimer);await flush();await post('/api/project-switch');deck.dispose();location.assign('/');}catch(error){notice(error.message);}};
$('quit').onclick=async()=>{try{await flush();if(!confirm('退出工具并释放服务端口？已保存进度会保留。'))return;await post('/api/shutdown');deck.dispose();$('mediaOverlay').hidden=false;$('mediaOverlay').textContent='工具已退出，进度已保存。下次重新运行程序即可继续。';}catch(error){notice(error.message);}};
document.addEventListener('keydown',event=>{
  if(event.ctrlKey||event.altKey||event.metaKey||document.querySelector('dialog[open]')||event.target.closest('input,textarea,select,[contenteditable=true]'))return;
  const key=captureKey(event);if(!key||!state.config)return;
  const keys=effectiveKeys(),label=Object.entries(keys.labels).find(([,value])=>value===key)?.[0],action=Object.entries(keys.shortcuts).find(([,value])=>value===key)?.[0];
  if(!label&&!action)return;if(event.repeat&&!['back','forward','stepBack','stepForward'].includes(action))return;event.preventDefault();
  if(label){chooseLabel(label);return;}
  const actions={play:()=>deck.toggle(),loop:()=>deck.set('loop',!deck.preferences.loop),back:()=>seek(-keys.seekSeconds),forward:()=>seek(keys.seekSeconds),stepBack:()=>seek(-1),stepForward:()=>seek(1),previous:()=>$('previous').click(),next:()=>$('next').click(),addSegment:()=>addSegment().catch(error=>notice(error.message)),complete,undo:()=>$('undo').click(),review:()=>patchAnnotation({status:'review'},true)};
  Object.assign(actions,{settings:()=>$('settings').click(),organize:()=>$('organize').click(),search:()=>$('search').focus(),rescan:()=>$('rescan').click(),proxy:()=>$('retryPreview').click(),proxyAll:()=>post('/api/proxy-all').then(()=>notice('已开始后台兼容，当前视频优先')).catch(error=>notice(error.message)),fullscreen:()=>$('fullscreen').click(),mute:()=>$('mute').click(),setStart:()=>$('setStart').click(),setEnd:()=>{if(state.config.freeSegments){$('segmentEnd').value=deck.player.currentTime;updateDraft();}},previewSegment:()=>$('previewSegment').click(),saveView:()=>$('saveView').click(),switchProject:()=>$('switchProject').click(),clearLabel:()=>patchAnnotation({label:null,status:'pending'}),previousDevice:()=>moveDevice(-1),nextDevice:()=>moveDevice(1)});
  actions[action]?.();
});
function moveDevice(delta) {const current=device(state.current),devices=[...new Set(ordered().map(device).filter(Boolean))];const index=devices.indexOf(current)+delta;if(index>=0&&index<devices.length){$('search').value=devices[index];renderList();const video=filtered()[0];if(video)selectVideo(video.id);}}
addEventListener('beforeunload',event=>{if(state.pending.length){event.preventDefault();event.returnValue='';}});
addEventListener('pagehide',()=>{clearTimeout(cursorTimer);deck.dispose();});
async function init() {
  const document=await api('/api/project');adopt(document,false);
  try{state.pending=JSON.parse(localStorage.getItem(projectKey('pending'))||'[]');if(!Array.isArray(state.pending))state.pending=[];}catch{state.pending=[];}
  for(const task of state.pending){const video=state.videos.find(item=>item.id===task.id);if(video)video.annotation={...task.annotation,revision:task.revision+1};}
  if(state.pending.length){renderAnnotation();renderProgress();renderList();notice(`恢复 ${state.pending.length} 条上次未确认保存的记录，正在重试`);drain();}
  updateSaveState();
  if(document.migration.imported)notice(`已迁入 ${document.migration.imported} 条旧进度，旧文件保留。目录原分类与本轮完成状态已分开。`);
  const warnings=[document.warning,document.metadataWarning,...(document.migration.warnings||[])].filter(Boolean);if(warnings.length)notice(warnings.slice(0,4).join('；'),true);
}
init().catch(error=>{$('mediaOverlay').textContent=`打开项目失败：${error.message}`;$('mediaOverlay').classList.add('error');});
