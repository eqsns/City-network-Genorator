'use strict';
const $ = id => document.getElementById(id);
const token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
const preset = [118.745,32.010,118.835,32.090];
const MAX_AREA_KM2 = 1000;
const labels = {running:'生成中',pausing:'暂停中',paused:'已暂停',resuming:'恢复中',cancelling:'取消中',cancelled:'已取消',failed:'失败',complete:'已完成',interrupted:'已中断'};
const activeStatuses = ['running','pausing','paused','resuming','cancelling'];
let jobs=[], selected=null, active=null, bbox=null, rectangle=null, loaded=null, loading=null, version=0, pending=false, historySignature='', progressId=null;
let selectionVersion=0, refreshVersion=0, controlIntent=0, resultRequest=null, cancelPending=false, pausePending=false;
const map=L.map('map',{preferCanvas:true,minZoom:3,maxZoom:19}).setView([32.05,118.79],12);
const tiles=L.tileLayer(`/api/tiles/{z}/{x}/{y}.png?token=${encodeURIComponent(token)}`,{maxZoom:19,keepBuffer:1,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'}).addTo(map);
let goodTiles=0, badTiles=0, tileTimer=null, tileErrorTimer=null;
function visibleTiles(){const bounds=$('map').getBoundingClientRect();return [...$('map').querySelectorAll('img.leaflet-tile')].filter(img=>{const b=img.getBoundingClientRect();return b.right>bounds.left&&b.left<bounds.right&&b.bottom>bounds.top&&b.top<bounds.bottom;});}
function tileWaiting(){clearTimeout(tileTimer);tileTimer=setTimeout(()=>{if(!visibleTiles().some(img=>img.complete&&img.naturalWidth>0)){$('tile-message').textContent='底图响应较慢，可继续输入坐标，或检查网络后重试。';$('tile-warning').hidden=false;}},20000);}
function updateTileWarning(){clearTimeout(tileErrorTimer);tileErrorTimer=setTimeout(async()=>{
  // Requests discarded after panning/resizing must not leave a false failure banner.
  const failed=visibleTiles().filter(img=>img.dataset.cngFailed==='1');
  if(!failed.length){$('tile-warning').hidden=true;return;}
  $('tile-warning').hidden=false;$('tile-message').textContent='部分底图未加载，正在读取连接原因…';
  try{const status=await (await api('/api/network')).json();if(visibleTiles().some(img=>img.dataset.cngFailed==='1'))$('tile-message').textContent='底图加载失败：'+(status.last_error||'本地地图服务暂未响应，请重试');}
  catch(e){$('tile-message').textContent=e.message;}
},200);}
tiles.on('loading',()=>{goodTiles=0;badTiles=0;tileWaiting();});
tiles.on('tileload',event=>{goodTiles++;delete event.tile.dataset.cngFailed;updateTileWarning();});
tiles.on('tileerror',event=>{badTiles++;event.tile.dataset.cngFailed='1';updateTileWarning();});
tiles.on('tileunload',updateTileWarning);
tiles.on('load',()=>{clearTimeout(tileTimer);updateTileWarning();});
tileWaiting();
$('retry-map').onclick=()=>{badTiles=0;goodTiles=0;$('tile-warning').hidden=true;tiles.redraw();tileWaiting();};
let checkingNetwork=false;
async function checkNetwork(){
  if(!$('network-dialog').open)$('network-dialog').showModal();
  if(checkingNetwork)return;checkingNetwork=true;
  $('network-content').textContent='正在检查底图、高程、建筑及 OSM 服务，通常需要 10–20 秒…';
  try{const result=await (await api('/api/network/check',{method:'POST',body:'{}'})).json();
    $('network-content').textContent=result.services.map(s=>`${s.ok?'✓':'×'} ${s.name} (${s.host})\n  ${s.message} · ${s.seconds} 秒`).join('\n\n')+'\n\n这是服务连通性检查，不代表数据下载或管网生成已经完成。\n底图经本地服务获取并缓存；连接详情保存在成果目录 network.log。\n若全部失败，请检查系统代理或软件联网权限，也可切换手机热点再试。';
  }catch(e){$('network-content').textContent='网络检查失败：'+e.message;}finally{checkingNetwork=false;}
}
$('check-network').onclick=checkNetwork;$('map-network').onclick=checkNetwork;
$('close-network').onclick=()=>$('network-dialog').close();
const renderer=L.canvas({padding:.3});
const layers={pipe:L.layerGroup().addTo(map),node:L.layerGroup(),outfall:L.layerGroup().addTo(map)};
const layerControl=L.control.layers(null,{'合成管线':layers.pipe,'节点':layers.node,'排口':layers.outfall},{collapsed:false}).addTo(map);
layerControl.getContainer().hidden=true;
L.control.scale({imperial:false,position:'bottomright'}).addTo(map);
L.drawLocal.draw.handlers.rectangle.tooltip.start='按住鼠标拖动，框选生成范围';
L.drawLocal.draw.handlers.simpleshape.tooltip.end='松开鼠标完成框选';
const draw=new L.Draw.Rectangle(map,{shapeOptions:{color:'#148a7e',weight:2,fillOpacity:.08}});

function message(text=''){ $('message').textContent=text;$('message').hidden=!text; }
function readBbox(){
  const values=['west','south','east','north'].map(id=>$(id).value.trim());
  if(values.some(v=>v===''))throw Error('请填写全部四个边界坐标');
  return validateBbox(values.map(Number));
}
function validateBbox(values){
  const [w,s,e,n]=values;
  if(values.length!==4||!values.every(Number.isFinite))throw Error('请输入有效数字坐标');
  if(!(w>=-180&&w<e&&e<=180&&s>=-85.051129&&s<n&&n<=85.051129))throw Error('范围无效：西侧经度必须小于东侧，南侧纬度必须小于北侧');
  const area=(e-w)*111.32*Math.cos((n+s)/2*Math.PI/180)*(n-s)*111.32;
  if(area>MAX_AREA_KM2)throw Error(`范围超过约 ${MAX_AREA_KM2} km²，请缩小范围`);
  return values;
}
function setBbox(values,fit=true){
  validateBbox(values);bbox=[...values];
  ['west','south','east','north'].forEach((id,i)=>{$(id).value=values[i];});
  if(rectangle)map.removeLayer(rectangle);
  rectangle=L.rectangle([[values[1],values[0]],[values[3],values[2]]],{color:'#12877e',weight:2,fillOpacity:.06,dashArray:'6 4',interactive:false}).addTo(map);
  const area=(values[2]-values[0])*111.32*Math.cos((values[3]+values[1])/2*Math.PI/180)*(values[3]-values[1])*111.32;
  $('area').textContent=area.toFixed(2)+' km²';
  if(fit)map.fitBounds(rectangle.getBounds(),{padding:[35,35]});
  saveView();
}
function clearBbox(){
  draw.disable();if(rectangle)map.removeLayer(rectangle);rectangle=null;bbox=null;
  ['west','south','east','north'].forEach(id=>{$(id).value='';});$('area').textContent='—';saveView();
}
let viewTimer=null;
function saveView(){const view={center:map.getCenter(),zoom:map.getZoom(),bbox};try{localStorage.setItem('studio-map',JSON.stringify(view));}catch{}
  if(window.pywebview?.api?.save_view){clearTimeout(viewTimer);viewTimer=setTimeout(()=>window.pywebview.api.save_view(view),250);}
}
try{const old=JSON.parse(localStorage.getItem('studio-map')||'null');if(old){if(old.bbox)setBbox(old.bbox,false);else clearBbox();map.setView(old.center,old.zoom);}else setBbox(preset);}catch{setBbox(preset);}
map.on('moveend',saveView);
async function restoreDesktopView(){if(!window.pywebview?.api?.load_view)return;try{const old=await window.pywebview.api.load_view();if(old){if(old.bbox)setBbox(old.bbox,false);else clearBbox();map.setView(old.center,old.zoom);}}catch{}}
window.addEventListener('pywebviewready',restoreDesktopView);
if(window.pywebview?.api?.load_view)restoreDesktopView();
map.on(L.Draw.Event.CREATED,event=>{const b=event.layer.getBounds();try{setBbox([b.getWest(),b.getSouth(),b.getEast(),b.getNorth()].map(v=>Number(v.toFixed(6))),false);message();}catch(e){message(e.message);}});
map.on('draw:drawstop',()=>{$('draw').textContent='▣ 框选范围';});
$('draw').onclick=()=>{map.closePopup();draw.enable();$('draw').textContent='拖动地图框选…';message();};
$('clear').onclick=()=>{clearBbox();message();};
$('fit').onclick=()=>{if(rectangle)map.fitBounds(rectangle.getBounds(),{padding:[35,35]});else message('请先框选或输入范围');};
$('apply').onclick=()=>{try{setBbox(readBbox());message();}catch(e){message(e.message);}};
$('preset').onclick=()=>{setBbox(preset);message();};
// Editing coordinates invalidates the old selection until the values are applied.
['west','south','east','north'].forEach(id=>$(id).addEventListener('input',()=>{bbox=null;$('area').textContent='待应用';if(rectangle){map.removeLayer(rectangle);rectangle=null;}}));

async function api(path,options={}){
  const response=await fetch(path,{...options,headers:{'X-Session-Token':token,'Content-Type':'application/json',...options.headers}});
  if(!response.ok){let error;try{error=await response.json();}catch{}throw Error(error?.error||'本地服务连接失败');}
  return response;
}
function activeJob(){return jobs.find(j=>activeStatuses.includes(j.status));}
function currentJob(){return jobs.find(j=>j.id===selected);}
function progressJob(){const running=activeJob();if(running)progressId=running.id;return running||jobs.find(j=>j.id===progressId)||currentJob();}
function renderProgress(){
  const job=progressJob(),running=activeJob();active=running?.id||null;
  $('generate').disabled=!!active||pending;
  for(const id of ['cancel','cancel-side']){$(id).hidden=!active;$(id).disabled=cancelPending||running?.status==='cancelling';$(id).textContent=running?.status==='cancelling'?'正在取消…':'停止并取消';}
  for(const id of ['pause','pause-side']){$(id).hidden=!active;$(id).disabled=pausePending||cancelPending||['resuming','cancelling'].includes(running?.status);$(id).textContent=running?.status==='paused'?'恢复生成':running?.status==='pausing'?'撤销暂停':running?.status==='resuming'?'正在恢复…':'暂停生成';}
  $('task-controls').hidden=!active;
  $('show-log').hidden=!job?.files?.includes('log');
  const percent=job?.progress||0;$('percent').textContent=percent+'%';$('fill').style.width=percent+'%';$('progress').setAttribute('aria-valuenow',percent);
  $('task-title').textContent=job?`${labels[job.status]} · ${job.name}`:'准备就绪';
  const stage=({pausing:'暂停中，当前下载分块或计算步骤结束后暂停',paused:'已暂停，点击“恢复生成”继续；关闭软件将结束本次任务',resuming:'正在恢复生成，请稍候…'})[job?.status]||(job?.status==='failed'&&job.error)||job?.stage||'选好范围后，点击左侧“生成管网”';
  $('stage').textContent=[stage,job?.persistence_warning,job?.cache_warning].filter(Boolean).join(' · ');
  $('elapsed').textContent=job?.elapsed?`用时 ${Math.floor(job.elapsed/60)}分${job.elapsed%60}秒`:'';
  document.querySelector('.progress-panel').classList.toggle('failed',job?.status==='failed');
  document.querySelector('.progress-panel').classList.toggle('paused',['paused','pausing'].includes(job?.status));
  renderCacheControls();renderDeleteControls();
}
function renderHistory(){
  const signature=JSON.stringify([selected,jobs.map(j=>[j.id,j.status,j.name])]);
  if(signature===historySignature)return;historySignature=signature;
  const parent=$('jobs');parent.replaceChildren();
  for(const job of jobs){const card=document.createElement('div');card.className='job-card'+(job.id===selected?' selected':'');
    const button=document.createElement('button');button.className='job';button.setAttribute('aria-pressed',String(job.id===selected));button.title=job.name;
    const title=document.createElement('b');title.textContent=job.name;const detail=document.createElement('small');detail.textContent=`${labels[job.status]} · ${job.created_at.slice(0,10)}`;
    button.append(title,detail);button.onclick=()=>selectProject(job.id);
    const actions=document.createElement('div');actions.className='job-actions';
    const rename=document.createElement('button');rename.className='text-button';rename.textContent='重命名';rename.setAttribute('aria-label',`重命名 ${job.name}`);rename.onclick=()=>openRename(job.id);
    const remove=document.createElement('button');remove.className='text-button danger';remove.textContent='删除';remove.setAttribute('aria-label',`删除 ${job.name}`);remove.disabled=activeStatuses.includes(job.status);remove.title=remove.disabled?'请先停止并取消任务，再删除成果':'删除项目及其本地成果';remove.onclick=()=>openDelete(job.id);
    actions.append(rename,remove);card.append(button,actions);parent.append(card);
  }
  if(!jobs.length){const p=document.createElement('p');p.className='hint empty-history';p.textContent='暂无历史成果。在主地图选择范围，生成第一个管网项目。';parent.append(p);}
}
function clearResultLayers(){
  version++;resultRequest?.abort();resultRequest=null;loaded=null;loading=null;
  map.closePopup();Object.values(layers).forEach(layer=>layer.clearLayers());$('map-loading').hidden=true;
}
function selectProject(id){
  const job=jobs.find(item=>item.id===id);if(!job)return;
  message();selectionVersion++;if(selected!==id)clearResultLayers();selected=id;
  if(!activeJob())progressId=id;
  setBbox(job.bbox);$('project-name').value=job.name;renderHistory();renderResult();renderProgress();
}
function showHome(){
  selectionVersion++;selected=null;clearResultLayers();if(!activeJob())progressId=null;
  $('project-name').value='新建管网';message();renderHistory();renderResult();renderProgress();
}
$('home').onclick=showHome;

let renameId=null,deleteId=null,renamePending=false,deletePending=false;
function dialogError(id,text=''){$(id).textContent=text;$(id).hidden=!text;}
function openRename(id){
  const job=jobs.find(item=>item.id===id);if(!job)return;
  renameId=id;$('rename-name').value=job.name;dialogError('rename-error');$('rename-dialog').showModal();$('rename-name').focus();$('rename-name').select();
}
function openDelete(id){
  const job=jobs.find(item=>item.id===id);if(!job||activeStatuses.includes(job.status))return;
  deleteId=id;$('delete-name').textContent=job.name;$('delete-cache').checked=false;dialogError('delete-error');renderDeleteControls();$('delete-dialog').showModal();$('delete-back').focus();
}
function renderDeleteControls(){
  const blocked=!!activeJob()&&$('delete-cache').checked;
  $('delete-confirm').disabled=deletePending||blocked;$('delete-back').disabled=deletePending;$('delete-cache').disabled=deletePending;
  $('delete-cache-busy').hidden=!activeJob();
}
$('delete-cache').onchange=renderDeleteControls;
$('rename-back').onclick=()=>{if(!renamePending)$('rename-dialog').close();};
$('delete-back').onclick=()=>{if(!deletePending)$('delete-dialog').close();};
$('rename-dialog').addEventListener('cancel',event=>{if(renamePending)event.preventDefault();});
$('delete-dialog').addEventListener('cancel',event=>{if(deletePending)event.preventDefault();});
$('rename-form').onsubmit=async event=>{
  event.preventDefault();if(renamePending||!renameId)return;
  const id=renameId,name=$('rename-name').value.trim();if(!name){dialogError('rename-error','请输入项目名称');return;}
  renamePending=true;for(const control of ['rename-name','rename-back','rename-submit'])$(control).disabled=true;dialogError('rename-error');
  try{const job=await (await api(`/api/jobs/${id}/rename`,{method:'POST',body:JSON.stringify({name})})).json();
    refreshVersion++;jobs=jobs.map(item=>item.id===id?job:item);if(selected===id)$('project-name').value=job.name;
    $('rename-dialog').close();renderHistory();renderProgress();renderResult();message('项目名称已更新');await refresh();
  }catch(e){dialogError('rename-error',e.message);}finally{renamePending=false;for(const control of ['rename-name','rename-back','rename-submit'])$(control).disabled=false;}
};
$('delete-confirm').onclick=async()=>{
  if(deletePending||!deleteId||($('delete-cache').checked&&activeJob()))return;const id=deleteId,clearCache=$('delete-cache').checked;
  deletePending=true;renderDeleteControls();dialogError('delete-error');
  try{await api(`/api/jobs/${id}/delete`,{method:'POST',body:JSON.stringify({clear_cache:clearCache})});refreshVersion++;jobs=jobs.filter(item=>item.id!==id);
    if(progressId===id)progressId=null;$('delete-dialog').close();
    if(selected===id)showHome();else{renderHistory();renderProgress();renderResult();}
    message(clearCache?'项目及本地成果已删除，基础数据下载缓存已清除':'项目及本地成果已删除，可复用的下载缓存已保留');await refresh();
  }catch(e){dialogError('delete-error',e.message);}finally{deletePending=false;renderDeleteControls();}
};
let cacheInfo=null,cacheLoading=false,cachePending=false,cacheRefreshQueued=false;
function formatBytes(bytes){const amount=Math.max(0,Number(bytes)||0);if(amount<1024)return `${amount} B`;if(amount<1024**2)return `${(amount/1024).toFixed(1)} KB`;if(amount<1024**3)return `${(amount/1024**2).toFixed(1)} MB`;return `${(amount/1024**3).toFixed(2)} GB`;}
function renderCacheControls(){
  const busy=!!activeJob()||cacheInfo?.busy;
  $('cache-busy').hidden=!busy;
  $('cache-confirm').disabled=cacheLoading||cachePending||!cacheInfo||busy||(!$('cache-generation').checked&&!$('cache-map').checked);
  for(const id of ['cache-generation','cache-map','cache-back','cache-refresh'])$(id).disabled=cachePending||cacheLoading;
  $('cache-confirm').textContent=cachePending?'正在清除…':'清除所选缓存';
}
async function loadCacheInfo(){
  if(cachePending)return;if(cacheLoading){cacheRefreshQueued=true;return;}
  cacheLoading=true;cacheInfo=null;dialogError('cache-error');renderCacheControls();
  $('cache-generation-size').textContent='正在统计…';$('cache-map-size').textContent='正在统计…';
  try{cacheInfo=await (await api('/api/cache')).json();for(const kind of ['generation','map'])$(`cache-${kind}-size`).textContent=`${formatBytes(cacheInfo[kind].bytes)} · ${cacheInfo[kind].files} 个文件`;}
  catch(e){cacheInfo=null;dialogError('cache-error','读取缓存失败：'+e.message);$('cache-generation-size').textContent='暂时无法统计';$('cache-map-size').textContent='暂时无法统计';}
  finally{
    cacheLoading=false;
    // A task may finish while this inventory is still in flight. Re-read once
    // before enabling deletion so its old busy flag cannot strand the dialog.
    const reload=cacheRefreshQueued&&$('cache-dialog').open;cacheRefreshQueued=false;
    if(reload)await loadCacheInfo();else renderCacheControls();
  }
}
async function openCache(){
  if(cachePending)return;$('cache-generation').checked=true;$('cache-map').checked=false;dialogError('cache-error');$('cache-dialog').showModal();await loadCacheInfo();
}
$('manage-cache').onclick=openCache;$('cache-refresh').onclick=loadCacheInfo;
$('cache-generation').onchange=renderCacheControls;$('cache-map').onchange=renderCacheControls;
$('cache-back').onclick=()=>{if(!cachePending&&!cacheLoading)$('cache-dialog').close();};
$('cache-dialog').addEventListener('cancel',event=>{if(cachePending||cacheLoading)event.preventDefault();});
$('cache-confirm').onclick=async()=>{
  if(cachePending||cacheLoading||!cacheInfo||cacheInfo.busy||activeJob())return;
  const generation=$('cache-generation').checked,mapCache=$('cache-map').checked;if(!generation&&!mapCache)return;
  cachePending=true;renderCacheControls();dialogError('cache-error');
  try{const result=await (await api('/api/cache/clear',{method:'POST',body:JSON.stringify({generation,map:mapCache})})).json();
    $('cache-dialog').close();message(`缓存已清除，释放 ${formatBytes(result.cleared_bytes)}（${result.cleared_files} 个文件）。已有模型成果保留。`);
  }catch(e){dialogError('cache-error','清除缓存失败：'+e.message);}finally{cachePending=false;renderCacheControls();}
};
function popup(feature){const div=document.createElement('div');const p=feature.properties;
  const keys={id:'编号',diameter:'管径 (m)',length:'长度 (m)',chamber_floor:'井底高程 (m)',surface_elevation:'地面高程 (m)'};
  for(const [key,label] of Object.entries(keys))if(p[key]!==null&&p[key]!==undefined){const row=document.createElement('div');row.textContent=`${label}：${p[key]}`;div.append(row);}return div;
}
async function renderResult(){
  const job=currentJob(),complete=job?.status==='complete',hasMap=complete&&job.files?.includes('geojson');
  $('home').setAttribute('aria-pressed',String(!job));
  $('map-title').textContent=job?.name||'主地图';$('map-subtitle').textContent=job?(complete?'合成管网 · WGS84 地图叠加':`任务${labels[job.status]} · 完成后可查看管网`):'框选或输入范围构建管网；选择历史成果后显示对应管网';
  for(const [id,key] of [['stat-edges','street_conduits'],['stat-nodes','nodes'],['stat-outfalls','outfalls']])$(id).textContent=complete&&job.stats?.[key]!=null?job.stats[key].toLocaleString('zh-CN'):'—';
  $('export-kind').disabled=!complete;$('export').disabled=!complete||!job.files?.includes($('export-kind').value);
  layerControl.getContainer().hidden=!hasMap;$('network-legend').hidden=!hasMap;
  if(!hasMap){if(loaded||loading)clearResultLayers();return;}
  if(loaded===job.id||loading===job.id)return;
  clearResultLayers();const current=version;loading=job.id;resultRequest=new AbortController();$('map-loading').hidden=false;
  try{const geojson=await (await api(`/api/files/${job.id}/geojson`,{signal:resultRequest.signal})).json();if(current!==version||selected!==job.id)return;
    // Add in chunks so the desktop window remains responsive for large histories.
    for(let index=0;index<geojson.features.length;index+=800){if(current!==version||selected!==job.id)return;
      for(const f of geojson.features.slice(index,index+800)){const kind=f.properties.kind;if(!layers[kind])continue;
        L.geoJSON(f,{renderer,style:{color:'#087a95',weight:2,opacity:.85},pointToLayer:(feature,latlng)=>L.circleMarker(latlng,{renderer,radius:kind==='outfall'?4:2,color:kind==='outfall'?'#c76b36':'#4b8b98',fillColor:kind==='outfall'?'#ed9854':'#4b8b98',fillOpacity:.9,weight:1}),onEachFeature:(feature,layer)=>layer.bindPopup(()=>popup(feature))}).addTo(layers[kind]);
      }await new Promise(resolve=>setTimeout(resolve,0));
    }if(current===version&&selected===job.id)loaded=job.id;
  }catch(e){if(current===version&&e.name!=='AbortError')message('管网图层加载失败：'+e.message);}
  finally{if(current===version){loading=null;resultRequest=null;$('map-loading').hidden=true;}}
}
async function refresh(){
  const current=++refreshVersion;
  try{const updated=await (await api('/api/jobs')).json();if(current!==refreshVersion)return;const wasActive=!!activeJob();jobs=updated;
    if(selected&&!jobs.some(job=>job.id===selected)){selected=null;selectionVersion++;clearResultLayers();}
    if($('cache-dialog').open&&(wasActive!==!!activeJob()||(!activeJob()&&cacheInfo?.busy&&!cacheLoading)))loadCacheInfo();
    renderHistory();renderProgress();await renderResult();}
  catch(e){if(current===refreshVersion)message(e.message);}
}
$('generate').onclick=async()=>{
  if(pending||active)return;
  try{const values=readBbox();setBbox(values,false);const name=$('project-name').value.trim();if(!name)throw Error('请输入项目名称');
    pending=true;const selectionAtStart=selectionVersion;renderProgress();message();const job=await (await api('/api/jobs',{method:'POST',body:JSON.stringify({name,bbox:values,method:$('method').value})})).json();
    progressId=job.id;if(selectionVersion===selectionAtStart){clearResultLayers();selected=job.id;selectionVersion++;}await refresh();
  }catch(e){message(e.message);}finally{pending=false;renderProgress();}
};
async function cancelGeneration(){
  const running=activeJob();if(!running||cancelPending||running.status==='cancelling')return;
  controlIntent++;cancelPending=true;renderProgress();
  try{await api(`/api/jobs/${running.id}/cancel`,{method:'POST',body:'{}'});await refresh();}
  catch(e){message(e.message);}finally{cancelPending=false;renderProgress();}
}
$('cancel').onclick=cancelGeneration;$('cancel-side').onclick=cancelGeneration;
async function togglePause(){
  const running=activeJob();if(!running||pausePending||cancelPending||['cancelling','resuming'].includes(running.status))return;
  const action=['paused','pausing'].includes(running.status)?'resume':'pause';
  const intent=++controlIntent;pausePending=true;renderProgress();message();
  try{const job=await (await api(`/api/jobs/${running.id}/${action}`,{method:'POST',body:'{}'})).json();
    // A newer poll or cancellation takes precedence over this request's old
    // snapshot, including cancellation that is still waiting for its response.
    if(intent===controlIntent&&jobs.find(item=>item.id===running.id)===running){
      refreshVersion++;jobs=jobs.map(item=>item.id===job.id?job:item);renderHistory();renderProgress();
    }
    await refresh();
  }catch(e){if(intent===controlIntent)message((action==='pause'?'暂停失败：':'恢复失败：')+e.message);}finally{pausePending=false;renderProgress();}
}
$('pause').onclick=togglePause;$('pause-side').onclick=togglePause;
$('show-log').onclick=async()=>{const job=progressJob();if(!job)return;try{$('log-content').textContent=await (await api(`/api/files/${job.id}/log`)).text();$('log-dialog').showModal();}catch(e){message(e.message);}};
$('close-log').onclick=()=>$('log-dialog').close();
$('export-kind').onchange=()=>{$('export').disabled=currentJob()?.status!=='complete'||!currentJob()?.files?.includes($('export-kind').value);};
$('export').onclick=async()=>{const job=currentJob(),kind=$('export-kind').value;if(!job)return;
  try{if(window.pywebview?.api){const result=await window.pywebview.api.export_file(job.id,kind);if(result.error)throw Error(result.error);if(result.path)message('已导出：'+result.path);}
    else{const blob=await (await api(`/api/files/${job.id}/${kind}`)).blob();const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=({bundle:'SWMM-model.zip',inp:'model.inp',geojson:'network.geojson',map:'network.png'})[kind]||kind+'.geoparquet';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);}}
  catch(e){message(e.message);}
};
$('open-output').onclick=()=>{if(window.pywebview?.api)window.pywebview.api.open_output();else message('请在桌面窗口中打开成果目录');};
new ResizeObserver(()=>map.invalidateSize()).observe($('map'));
(async function poll(){await refresh();setTimeout(poll,1000);})();
