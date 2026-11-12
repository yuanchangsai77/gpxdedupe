'use strict';
const $ = id => document.getElementById(id);
const root = document.documentElement;
try { root.dataset.theme = localStorage.getItem('gpx-theme') || 'light'; } catch {}
function updateThemeButton() { if ($('theme-toggle')) $('theme-toggle').textContent = root.dataset.theme === 'dark' ? '浅色' : '深色'; }
updateThemeButton();
$('theme-toggle')?.addEventListener('click', () => { root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark'; try { localStorage.setItem('gpx-theme', root.dataset.theme); } catch {} updateThemeButton(); });
if ($('folder-form')) startWorkbench();
function startWorkbench() {
  const state = {config:null, files:[], selected:null, result:null, busy:false, scanning:false, mode:'split', zoom:1, panX:0, panY:0, maps:[], bounds:null, table:'original', scanVersion:0};
  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    let data; try { data = await response.json(); } catch { throw new Error('服务返回了无法读取的结果，请确认本地服务仍在运行。'); }
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
    return data;
  }
  function status(text, error=false) { $('status').textContent=text; $('status').classList.toggle('error',error); }
  function controls() { $('run').disabled=state.busy || state.scanning || !state.selected; $('scan').disabled=state.busy || state.scanning; $('use-example').disabled=state.busy || state.scanning; $('use-default').disabled=state.busy || state.scanning; $('folder').disabled=state.busy; $('recursive').disabled=state.busy; $('radius').disabled=state.busy; document.body.classList.toggle('busy',state.busy); $('run').querySelector('span').textContent=state.busy ? '正在处理…' : '运行去重'; }
  function renderFiles() {
    const list=$('files'); list.replaceChildren();
    const query=$('filter').value.toLocaleLowerCase();
    const files=state.files.filter(f=>`${f.name} ${f.relative_path}`.toLocaleLowerCase().includes(query));
    $('file-count').textContent=state.files.length;
    for (const file of files) {
      const button=document.createElement('button');button.type='button';button.className='file-item';button.setAttribute('aria-pressed',String(state.selected?.id===file.id));button.disabled=state.busy;
      const icon=document.createElement('span');icon.className='file-icon';icon.textContent='GPX';
      const copy=document.createElement('span');copy.className='file-copy';
      const name=document.createElement('strong');name.textContent=file.name;
      const meta=document.createElement('small');meta.textContent=`${(file.size_bytes/1024).toFixed(1)} KB${file.relative_path!==file.name?' / '+file.relative_path:''}`;
      copy.append(name,meta);button.append(icon,copy);
      button.addEventListener('click',()=>{state.selected=file;renderFiles();controls();status(`已选择 ${file.name}。点击「运行去重」查看结果。${state.result?' 下方仍为上一次处理结果。':''}`);});list.append(button);
    }
    if (!files.length) {const p=document.createElement('p');p.className='list-empty';p.textContent=state.files.length?'没有匹配的文件名。':'没有找到 GPX 文件。请检查路径或勾选「包含子文件夹」。';list.append(p);}
  }
  async function scan(folder) {
    if(state.busy || state.scanning) return;
    state.scanning=true;state.selected=null;state.files=[];renderFiles();controls();status('正在读取文件夹…');
    try { const data=await api('/api/files',{folder,recursive:$('recursive').checked});state.files=data.files;$('folder').value=data.folder;state.selected=data.files[0]||null;renderFiles();status(`已读取 ${data.files.length} 个 GPX 文件。${data.truncated?'仅显示前 2000 个文件，请缩小目录范围。':'选择文件后点击「运行去重」。'}`); }
    catch(error){status(error.message,true);}finally{state.scanning=false;controls();}
  }
  $('folder-form').addEventListener('submit',event=>{event.preventDefault();scan($('folder').value.trim());});
  $('filter').addEventListener('input',renderFiles);
  $('use-example').addEventListener('click',()=>{if(state.config){$('folder').value=state.config.sample_dir;scan(state.config.sample_dir);}});
  $('use-default').addEventListener('click',()=>{if(state.config){$('folder').value=state.config.default_dir;scan(state.config.default_dir);}});
  const km=value=>`${(value/1000).toFixed(3)} km`;
  $('run').addEventListener('click',async()=>{
    if(state.busy||!state.selected)return;
    const radius=Number($('radius').value);
    if(!Number.isFinite(radius)||radius<1||radius>100){status('匹配半径请输入 1–100 米。',true);$('radius').focus();return;}
    const selected=state.selected;state.busy=true;controls();renderFiles();status(`正在处理 ${selected.name}，原文件不会被修改…`);
    try {
      const result=await api('/api/run',{file_id:selected.id,radius});state.result=result;state.zoom=1;state.panX=0;state.panY=0;
      const r=result.report;const all=result.original;
      // Web Mercator is used only to display lon/lat geometry; processing uses local UTM.
      state.project=p=>[p[0]*Math.PI/180,Math.log(Math.tan(Math.PI/4+Math.max(-85,Math.min(85,p[1]))*Math.PI/360))];
      const points=all.concat(result.cleaned).map(state.project);let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
      for(const [x,y] of points){minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);}
      state.bounds={cx:(minX+maxX)/2,cy:(minY+maxY)/2,width:Math.max(maxX-minX,1e-7),height:Math.max(maxY-minY,1e-7)};
      $('route-title').textContent=selected.name;$('route-subtitle').textContent=r.title || selected.relative_path;
      $('summary').hidden=false;$('result-details').hidden=false;$('map-empty').hidden=true;
      $('original-length').textContent=km(r.input_length_m);$('clean-length').textContent=km(r.output_length_m);$('removed-length').textContent=km(r.removed_length_m);$('point-count').textContent=`${r.input_points} → ${r.output_points}`;
      $('original-map-info').textContent=km(r.input_length_m);$('clean-map-info').textContent=km(r.output_length_m);
      const conclusions={reversed_tail_collapse:'识别到首尾同路往返的尾巴，裁剪首尾两段，保留中间的主体闭环。',repeated_laps_collapse:`识别到 ${r.identical_laps?.count ?? '多'} 圈近似重复整圈，保留其中一圈。`,unchanged:'没有发现符合当前规则的可去重结构，轨迹保持不变。这不代表轨迹中不存在重复道路。',unchanged_tails_not_connectable:'检测到往返尾巴，但切点间距超出安全拼接范围，因此保持原样。',unchanged_lap_gap_too_large:'检测到重复圈，但单圈端点间距过大，因此保持原样。'};
      $('conclusion').textContent=conclusions[r.method]||r.method;
      $('result-badge').textContent=r.method==='reversed_tail_collapse'?'往返尾巴已裁剪':r.method==='repeated_laps_collapse'?'重复整圈已合并':'保持原样';
      $('closure-note').textContent=`处理后${r.output_closed?'为闭合轨迹':'不是严格闭合轨迹'}。${r.retraced_tail?`检测切点间距 ${r.retraced_tail.gap_m.toFixed(2)} 米。`:''}匹配半径 ${r.config.match_radius_m} 米。`;
      for(const [id,key] of [['download-gpx','gpx'],['download-geojson','geojson'],['download-report','report']]) $(id).href=result.downloads[key];
      $('report').textContent=JSON.stringify(r,null,2);renderMaps();renderTable();status(`处理完成：${selected.name}。结果已另存，可下载 GPX、GeoJSON 和报告。`);
    }catch(error){status(`${error.message}${state.result?' 下方保留上一次成功的结果。':''}`,true);}finally{state.busy=false;controls();renderFiles();}
  });
  function svgNode(name,attrs={}){const node=document.createElementNS('http://www.w3.org/2000/svg',name);for(const [key,value]of Object.entries(attrs))node.setAttribute(key,String(value));return node;}
  function drawMap(host,series){
    host.replaceChildren();const width=host.clientWidth||500,height=host.clientHeight||480;
    const svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`,class:'route-svg',tabindex:'0',role:'img','aria-label':`${series.map(s=>s.label).join('与')}；滚轮缩放，方向键平移，Home 复位`});
    const tip=document.createElement('div');tip.className='tooltip';tip.hidden=true;host.append(svg,tip);
    const g=svgNode('g');svg.append(g);
    for(let x=0;x<width;x+=55)g.append(svgNode('line',{x1:x,y1:0,x2:x,y2:height,stroke:'var(--grid)','stroke-width':.6}));
    for(let y=0;y<height;y+=55)g.append(svgNode('line',{x1:0,y1:y,x2:width,y2:y,stroke:'var(--grid)','stroke-width':.6}));
    if(!state.result)return;
    // Identical map extents and pixel scale keep split-view comparisons honest.
    const baseWidth=state.mode==='split'?Math.min($('original-map').clientWidth||width,$('clean-map').clientWidth||width):width;
    const baseHeight=height;const b=state.bounds;const scale=Math.min((baseWidth-90)/b.width,(baseHeight-115)/b.height)*state.zoom;
    const screen=p=>{const[x,y]=state.project(p);return[(x-b.cx)*scale+width/2+state.panX,-(y-b.cy)*scale+height/2+state.panY+14];};
    const selectable=[];
    for(const item of series){
      if(!$(item.key==='original'?'show-original':'show-clean').checked)continue;
      const projected=state.result[item.key].map(screen);
      const d=projected.map((p,i)=>`${i?'L':'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' ');
      g.append(svgNode('path',{d,fill:'none',stroke:'var(--plot)','stroke-width':5,'stroke-linecap':'round','stroke-linejoin':'round'}));
      g.append(svgNode('path',{d,fill:'none',stroke:`var(--${item.key==='original'?'original':'cleaned'})`,'stroke-width':2.3,'stroke-dasharray':item.key==='original'?'7 4':'none','stroke-linejoin':'round','stroke-linecap':'round',class:'route-line'}));
      const first=projected[0];g.append(svgNode('circle',{cx:first[0],cy:first[1],r:5,fill:`var(--${item.key==='original'?'original':'cleaned'})`,stroke:'var(--plot)','stroke-width':2}));
      const label=svgNode('text',{x:first[0]+10,y:first[1]-10,fill:'var(--ink)','font-size':11});label.textContent=`${item.label}起点`;g.append(label);
      for(let i=0;i<projected.length;i++)selectable.push({xy:projected[i],point:state.result[item.key][i],index:i,label:item.label});
    }
    const marker=svgNode('circle',{r:5,fill:'var(--ink)',stroke:'var(--plot)','stroke-width':2,visibility:'hidden'});svg.append(marker);
    function showNearest(x,y){let nearest=null,dist=Infinity;for(const p of selectable){const d=(p.xy[0]-x)**2+(p.xy[1]-y)**2;if(d<dist){dist=d;nearest=p;}}if(!nearest)return;marker.setAttribute('cx',nearest.xy[0]);marker.setAttribute('cy',nearest.xy[1]);marker.setAttribute('visibility','visible');tip.hidden=false;tip.textContent=`${nearest.label} · 第 ${nearest.index+1} 点\n经度 ${nearest.point[0].toFixed(6)}\n纬度 ${nearest.point[1].toFixed(6)}`;tip.style.left=`${Math.min(width-190,Math.max(8,x+14))}px`;tip.style.top=`${Math.min(height-80,Math.max(42,y+14))}px`;}
    let drag=null;
    svg.addEventListener('pointerdown',event=>{drag={x:event.clientX,y:event.clientY,panX:state.panX,panY:state.panY};svg.setPointerCapture(event.pointerId);});
    svg.addEventListener('pointermove',event=>{const rect=svg.getBoundingClientRect();if(drag){state.panX=drag.panX+event.clientX-drag.x;state.panY=drag.panY+event.clientY-drag.y;requestRender();}else showNearest(event.clientX-rect.left,event.clientY-rect.top);});
    svg.addEventListener('pointerup',()=>{drag=null;renderMaps();});
    svg.addEventListener('pointercancel',()=>{drag=null;renderMaps();});
    svg.addEventListener('pointerleave',()=>{if(!drag){tip.hidden=true;marker.setAttribute('visibility','hidden');}});
    svg.addEventListener('wheel',event=>{event.preventDefault();state.zoom=Math.min(40,Math.max(.5,state.zoom*(event.deltaY<0?1.15:1/1.15)));renderMaps();},{passive:false});
    svg.addEventListener('dblclick',fit);
    svg.addEventListener('focus',()=>showNearest(width/2,height/2));svg.addEventListener('blur',()=>{tip.hidden=true;marker.setAttribute('visibility','hidden');});
    svg.addEventListener('keydown',event=>{const moves={ArrowLeft:[35,0],ArrowRight:[-35,0],ArrowUp:[0,35],ArrowDown:[0,-35]};if(moves[event.key]){event.preventDefault();state.panX+=moves[event.key][0];state.panY+=moves[event.key][1];renderMaps();host.querySelector('svg').focus();}else if(['+','=','-','Home'].includes(event.key)){event.preventDefault();if(event.key==='Home'){fit();}else{state.zoom=Math.min(40,Math.max(.5,state.zoom*(event.key==='-'?1/1.25:1.25)));renderMaps();}host.querySelector('svg').focus();}});
    // While dragging, update transforms without replacing the captured SVG.
    const initialPanX=state.panX,initialPanY=state.panY;
    host._dragUpdate=()=>{const dx=state.panX-initialPanX,dy=state.panY-initialPanY;g.setAttribute('transform',`translate(${dx},${dy})`);tip.hidden=true;marker.setAttribute('visibility','hidden');};
  }
  let pending=false;
  function requestRender(){if(pending)return;pending=true;requestAnimationFrame(()=>{pending=false;for(const id of ['original-map','clean-map'])$(id)._dragUpdate?.();});}
  const original={key:'original',label:'原始'},cleaned={key:'cleaned',label:'处理后'};
  function renderMaps(){drawMap($('original-map'),state.mode==='overlay'?[original,cleaned]:[original]);if(state.mode==='split')drawMap($('clean-map'),[cleaned]);}
  function fit(){state.zoom=1;state.panX=0;state.panY=0;renderMaps();}
  document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>{state.mode=button.dataset.mode;document.querySelectorAll('[data-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));$('map-stage').className=`map-stage ${state.mode}`;$('original-pane').querySelector('h3').textContent=state.mode==='overlay'?'原始与处理后叠加':'原始轨迹';renderMaps();}));
  $('fit').addEventListener('click',fit);$('zoom-in').addEventListener('click',()=>{state.zoom=Math.min(40,state.zoom*1.3);renderMaps();});$('zoom-out').addEventListener('click',()=>{state.zoom=Math.max(.5,state.zoom/1.3);renderMaps();});
  for(const id of ['show-original','show-clean'])$(id).addEventListener('change',renderMaps);
  new ResizeObserver(()=>renderMaps()).observe($('map-stage'));
  function renderTable(){if(!state.result)return;const tbody=$('coordinates');tbody.replaceChildren();const fragment=document.createDocumentFragment();state.result[state.table].forEach((point,index)=>{const tr=document.createElement('tr');for(const value of [index+1,point[0].toFixed(7),point[1].toFixed(7)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}fragment.append(tr);});tbody.append(fragment);for(const [id,key]of [['table-original','original'],['table-clean','cleaned']])$(id).setAttribute('aria-pressed',String(state.table===key));}
  $('table-original').addEventListener('click',()=>{state.table='original';renderTable();});$('table-clean').addEventListener('click',()=>{state.table='cleaned';renderTable();});
  (async()=>{try{state.config=await api('/api/config');$('folder').value=state.config.default_dir;await scan(state.config.default_dir);}catch(error){status(error.message,true);}})();
}
