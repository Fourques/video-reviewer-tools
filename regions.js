(function(){
const basename=v=>String(v||'').replace(/\\/g,'/').split('/').pop();
function matchRegion(rows,video){
 if(!video)return {message:'请选择视频'};
 const source=video.sourceIdentity||{};
 if(source.conflict)return {message:'设备来源存在冲突，请核对清单'};
 const names=[video.name||basename(video.relative),source.original_name].filter(Boolean);
 let found=rows.filter(r=>names.includes(basename(r.video_path)));
 if(!found.length&&source.event_id)found=rows.filter(r=>r.event_id?.toLowerCase()===source.event_id.toLowerCase());
 if(!found.length)found=rows.filter(r=>{
  const parts=names.flatMap(n=>n.split('_'));
  const device=source.did||video.metadataDeviceId;
  const community=source.community_id;
  return (device===r.did||parts.includes(r.did))&&(!r.community_id||(community?community.toLowerCase()===r.community_id.toLowerCase():parts.includes(r.community_id)||device===r.did));
 });
 const fallbackDevice=source.did||video.metadataDeviceId;
 let warning='';
 if(!found.length&&fallbackDevice){found=rows.filter(r=>r.did===fallbackDevice);if(found.length)warning='按设备匹配 · 社区 ID 不同，请核对画面';}
 const unique=new Map(found.map(r=>[JSON.stringify([r.width,r.height,r.polygon]),r]));
 return unique.size>1?{message:'该摄像头存在不同区域，请核对 JSON'}:unique.size?{row:[...unique.values()][0],warning}:{message:source.did||video.metadataDeviceId?'当前设备缺少区域坐标':'未识别设备，请检查上级视频清单或配置设备字段'};
}
function containRect(box,w,h){const scale=Math.min(box.width/w,box.height/h);return {left:(box.width-w*scale)/2,top:(box.height-h*scale)/2,width:w*scale,height:h*scale};}
if(typeof module!=='undefined')module.exports={matchRegion,containRect};if(typeof document==='undefined')return;
const svg=document.getElementById('cameraRegionOverlay'),status=document.getElementById('regionStatus'),toggle=document.getElementById('regionVisible'),stage=document.getElementById('videoStage');let rows=[],current=null,loadError='';
function draw(){svg.replaceChildren();svg.setAttribute("hidden","");if(loadError){status.textContent=loadError;return;}if(!rows.length){status.textContent='未导入区域';return;}const match=matchRegion(rows,current);if(!match.row){status.textContent=match.message;return;}const r=match.row,v=stage.querySelector('video.visible');if(!v?.videoWidth){status.textContent='区域已匹配 · 等待画面';return;}if(Math.abs((v.videoWidth/v.videoHeight)/(r.width/r.height)-1)>0.02){status.textContent='区域参考尺寸与视频比例不符，请核对';return;}status.textContent=toggle.checked?(match.warning||'洗手间区域'):'区域已隐藏';if(!toggle.checked)return;const b=v.getBoundingClientRect(),p=stage.getBoundingClientRect(),rect=containRect(b,v.videoWidth,v.videoHeight);Object.assign(svg.style,{left:`${b.left-p.left+rect.left}px`,top:`${b.top-p.top+rect.top}px`,width:`${rect.width}px`,height:`${rect.height}px`});svg.setAttribute('viewBox',`0 0 ${r.width} ${r.height}`);for(const coords of r.polygon){const el=document.createElementNS('http://www.w3.org/2000/svg','polygon');el.setAttribute('points',coords.reduce((s,v,i)=>s+(i%2?',':' ')+v,'').trim());svg.append(el);}svg.removeAttribute("hidden");}
window.cameraRegions={select(video){current=video;draw();}};toggle.onchange=draw;new ResizeObserver(draw).observe(stage);new MutationObserver(draw).observe(stage,{subtree:true,attributes:true,attributeFilter:['class']});stage.querySelectorAll('video').forEach(v=>{v.addEventListener('loadedmetadata',draw);v.addEventListener('emptied',draw);});
fetch('/api/camera-regions').then(async r=>{if(!r.ok)throw Error('区域读取失败');return r.json();}).then(d=>{rows=d.rows;draw();}).catch(e=>{loadError=e.message;draw();});
document.getElementById('regionImport').onchange=async e=>{try{const file=e.target.files[0];if(!file)return;if(file.size>900000)throw Error('JSON 文件不能超过 900 KB');const imported=JSON.parse(await file.text());if(rows.length&&!confirm('替换本项目已有的洗手间区域？原有视频与标注保持不变。'))return;const res=await fetch('/api/camera-regions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows:imported})});const data=await res.json();if(!res.ok)throw Error(data.error||'区域导入失败');rows=data.rows;loadError='';draw();}catch(error){status.textContent=`导入失败：${error.message}`;}finally{e.target.value='';}};
})();
