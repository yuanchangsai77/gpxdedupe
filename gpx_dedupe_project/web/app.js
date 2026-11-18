'use strict';
const $ = id => document.getElementById(id);
const root = document.documentElement;
try { root.dataset.theme = localStorage.getItem('gpx-theme') || 'light'; } catch {}
function updateThemeButton() { if ($('theme-toggle')) $('theme-toggle').textContent = root.dataset.theme === 'dark' ? '浅色' : '深色'; }
updateThemeButton();
$('theme-toggle')?.addEventListener('click', () => { root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark'; try { localStorage.setItem('gpx-theme', root.dataset.theme); } catch {} updateThemeButton(); });
if ($('folder-form')) startWorkbench();
function startWorkbench() {
  const state = {config:null, files:[], expanded:new Map(), preview:null, selected:null, result:null, analysis:null, applied:false, busy:false, scanning:false, workMode:'recognition', loopSelection:null, mode:'split', zoom:1, panX:0, panY:0, bounds:null, table:'original', version:0, operation:null};
  const recognizing=()=>state.workMode==='recognition';
  function modeSettings() {
    const recognition=recognizing();$('work-mode').value=state.workMode;
    $('radius').max=recognition?'30':'100';$('radius').value=recognition?'12':'18';
    $('radius-note').textContent=recognition?'默认 12 米，范围 1–30 米。空间接近不证明道路相同。':'默认 18 米。过大可能把平行道路误判为重合。';
    $('mode-note').textContent=recognition?'识别原轨迹中的几何环，不裁切、不补线。':'先分析几何候选，核对后再确认转换与导出。';
    document.body.classList.toggle('recognition-mode',recognition);
    document.querySelector('.intro').textContent=recognition?'识别原始轨迹中的几何环，不修改或导出轨迹。':'先分析几何候选，核对后再确认转换与导出。';
    document.querySelector('.map-empty strong').textContent='选择路线以预览';
  }
  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    let data; try { data = await response.json(); } catch { throw new Error('服务返回了无法读取的结果，请确认本地服务仍在运行。'); }
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
    return data;
  }
  function status(text, error=false) { $('status').textContent=text; $('status').classList.toggle('error',error); }
  function isTail(candidate) { return candidate?.goal === 'main_loop' || candidate?.method === 'reversed_tail_collapse'; }
  function candidateReady() {
    const a=state.analysis, c=a?.candidate;
    return Boolean(!recognizing() && a && !state.applied && a.file_id===state.selected?.id && a.radius===Number($('radius').value) && c && c.requires_confirmation===true && ['main_loop','one_lap'].includes(c.goal) && Array.isArray(c.blocked_reasons) && c.blocked_reasons.length===0);
  }
  function canApply() { return candidateReady() && $('goal').value===state.analysis.candidate.goal && $('confirm').checked && $('acknowledge-geometry-only').checked && (!isTail(state.analysis.candidate) || $('acknowledge-access-loss').checked); }
  function controls() {
    const locked=state.busy || state.scanning;
    for(const id of ['scan','use-example','use-default','folder','recursive','radius','filter','work-mode','goal','confirm','acknowledge-geometry-only','acknowledge-access-loss']) $(id).disabled=locked;
    $('run').disabled=locked || !state.selected;
    $('apply').disabled=locked || !canApply();
    $('run').querySelector('span').textContent=state.operation==='analyze' ? (recognizing()?'正在识别…':'正在分析…') : (recognizing()?'识别轨迹中的环':'仅分析候选');
    $('apply').textContent=state.operation==='apply' ? '正在确认转换…' : '确认转换并导出';
    document.body.classList.toggle('busy',locked);
    document.querySelector('.comparison-area').setAttribute('aria-busy',String(locked));
  }
  const downloadKeys=[['download-gpx','gpx'],['download-geojson','geojson'],['download-report','report'],['download-manifest','manifest']];
  function clearDownloads() { for(const [id] of downloadKeys) { $(id).hidden=true;$(id).removeAttribute('href'); } }
  function setDownloads(downloads, applied) {
    clearDownloads();
    for(const [id,key] of downloadKeys) if((applied || key==='report') && typeof downloads?.[key]==='string' && downloads[key]) { $(id).href=downloads[key];$(id).hidden=false; }
    $('download-report').textContent=recognizing()?'环识别报告 JSON':applied?'转换报告 JSON':'分析报告 JSON';
  }
  function resetConfirmation() { $('goal').value='';for(const id of ['confirm','acknowledge-geometry-only','acknowledge-access-loss']) $(id).checked=false; }
  function invalidate() {
    state.version++;state.analysis=null;state.result=null;state.applied=false;state.bounds=null;state.loopSelection=null;state.table='original';resetConfirmation();clearDownloads();
    for(const id of ['summary','result-details','candidate-details','confirmation-form','loop-details']) $(id).hidden=true;
    $('loop-list').replaceChildren();
    $('coordinates').replaceChildren();$('report').textContent='';$('map-empty').hidden=false;
    $('route-title').textContent=state.selected?.name || (recognizing()?'识别原轨迹中的环':'先核对候选，再决定转换');$('route-subtitle').textContent=recognizing()?'请点击「识别轨迹中的环」；仅生成只读识别报告。':'请点击「仅分析候选」生成新的候选预览。';$('result-badge').textContent=recognizing()?'等待识别 · 原轨迹未变':'等待分析 · 未导出';
    $('original-map-info').textContent='未载入';$('clean-map-info').textContent='未载入';updateResultLabels();renderMaps();
  }
  function setOriginalBounds(points) {
    state.project=p=>[p[0]*Math.PI/180,Math.log(Math.tan(Math.PI/4+Math.max(-85,Math.min(85,p[1]))*Math.PI/360))];
    let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
    for(const point of points){const [x,y]=state.project(point);minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);}
    state.bounds={cx:(minX+maxX)/2,cy:(minY+maxY)/2,width:Math.max(maxX-minX,1e-7),height:Math.max(maxY-minY,1e-7)};
  }
  function showPreview(original) {
    state.result={original,cleaned:[]};
    setOriginalBounds(original);
    $('map-empty').hidden=true;
    $('route-subtitle').textContent=state.selected.relative_path;
    $('original-map-info').textContent=`${original.length} 点`;
    fit();
    renderTable();
  }
  async function selectFile(file) {
    state.selected=file;state.preview=null;invalidate();controls();
    $('files').querySelector('[aria-pressed="true"]')?.setAttribute('aria-pressed','false');
    status(`正在加载 ${file.name}…`);
    try {
      const {original}=await api('/api/preview',{file_id:file.id});
      if(state.selected!==file)return;
      state.preview={id:file.id,original};
      if(state.busy || state.scanning || state.result)return;
      showPreview(original);
      status(`已预览 ${file.name}。可点击下方按钮进行${recognizing()?'环识别':'候选分析'}。`);
    } catch(error) {
      if(state.selected===file && !state.busy && !state.result)status(error.message,true);
    }
  }
  function renderFiles() {
    const list=$('files'), scroll=list.scrollTop;
    list.replaceChildren();
    const query=$('filter').value.trim().toLocaleLowerCase();
    const files=state.files.filter(f=>`${f.source || ''} ${f.relative_path} ${f.name}`.toLocaleLowerCase().includes(query));
    $('file-count').textContent=state.files.length;
    const branches=new Map();
    function folder(parent, key, label, defaultOpen) {
      if(branches.has(key))return branches.get(key);
      const details=document.createElement('details');
      details.className='route-folder';
      details.open=Boolean(query) || (state.expanded.get(key) ?? defaultOpen);
      const summary=document.createElement('summary');summary.textContent=label;
      const children=document.createElement('div');
      summary.addEventListener('click',()=>{if(!query)state.expanded.set(key,!details.open);});
      details.append(summary,children);parent.append(details);branches.set(key,children);
      return children;
    }
    for (const file of files) {
      let parent=list;
      if(file.source){
        const branch=[file.source];
        const label=file.source.split(/[\\/]/).filter(Boolean).at(-1)||file.source;
        parent=folder(parent,JSON.stringify(branch),label,!label.toLowerCase().endsWith('.json'));
        for(const part of file.relative_path.split('/').slice(0,-1)){
          branch.push(part);
          parent=folder(parent,JSON.stringify(branch),part,false);
        }
      }
      const button=document.createElement('button');button.type='button';button.className='file-item';
      button.setAttribute('aria-pressed',String(state.selected?.id===file.id));
      button.disabled=state.busy || state.scanning;
      const icon=document.createElement('span');icon.className='file-icon';icon.textContent=file.format || 'GPX';
      const copy=document.createElement('span');copy.className='file-copy';
      const name=document.createElement('strong');name.textContent=file.name;
      name.title='双击复制路线名称';
      name.addEventListener('dblclick',async event=>{
        event.preventDefault();
        try {
          await navigator.clipboard.writeText(file.name);
          status(`已复制路线名称：${file.name}`);
        } catch {
          status('复制路线名称失败。',true);
        }
      });
      copy.append(name);
      if(Number.isFinite(file.size_bytes)){
        const meta=document.createElement('small');meta.textContent=`${(file.size_bytes/1024).toFixed(1)} KB`;
        copy.append(meta);
      }
      button.append(icon,copy);
      button.addEventListener('click',event=>{
        if(event.detail>1)return;
        selectFile(file);
        button.setAttribute('aria-pressed','true');
      });parent.append(button);
    }
    if (!files.length) {
      const p=document.createElement('p');p.className='list-empty';
      p.textContent=state.files.length?'没有匹配的路线。':'没有找到可读取的路线。';list.append(p);
    }
    list.scrollTop=scroll;
  }
  async function scan(folder) {
    if(state.busy || state.scanning) return;
    const paths=folder.split(/\r?\n/).map(path=>path.trim().replace(/^"|"$/g,'')).filter(Boolean);
    state.scanning=true;state.selected=null;state.preview=null;state.files=[];state.expanded.clear();
    invalidate();renderFiles();controls();status('正在读取路线来源…');
    try {
      const data=await api('/api/files',{paths,recursive:$('recursive').checked});
      state.files=data.files;
      const messages=[`已读取 ${data.files.length} 条路线。点击路线即可预览。`];
      if(data.truncated)messages.push('仅显示前 2000 条路线。');
      if(data.errors?.length)messages.push(...data.errors);
      status(messages.join(' '),Boolean(data.errors?.length));
    } catch(error){status(error.message,true);}
    finally{state.scanning=false;renderFiles();controls();}
  }
  $('folder-form').addEventListener('submit',event=>{event.preventDefault();scan($('folder').value.trim());});
  $('filter').addEventListener('input',renderFiles);
  $('use-example').addEventListener('click',()=>{if(state.config){$('folder').value=state.config.sample_dir;scan(state.config.sample_dir);}});
  $('use-default').addEventListener('click',()=>{if(state.config){$('folder').value=state.config.default_dir;scan(state.config.default_dir);}});
  const km=value=>Number.isFinite(value)?`${(value/1000).toFixed(3)} km`:'未提供';
  const metres=value=>Number.isFinite(value)?`${value.toFixed(2)} 米`:'未提供';
  const goalNames={main_loop:'主体闭环裁剪',one_lap:'仅保留一圈'};
  function updateResultLabels() {
    const recognition=recognizing();
    $('table-clean').hidden=recognition;
    $('clean-length-label').textContent=recognition?'已识别简单环':'候选预览长度';
    $('removed-length-label').textContent=recognition?'待核对简单环候选':'候选拟减少行程';
    $('clean-key-label').textContent=recognition?'所选环 / 经过记录':'候选预览';
    $('clean-map-title').textContent=recognition?'原轨迹区间高亮':'候选预览';
    $('original-pane').querySelector('h3').textContent=recognition?'原始轨迹与所选环':'原始轨迹';
    $('map-stage').className=`map-stage ${recognition?'overlay':state.mode}`;
    document.querySelector('.map-toolbar .segmented').hidden=recognition;
    document.querySelector('.notice span').textContent=recognition?'仅根据 GPX 几何识别环，所有坐标来自原轨迹。不裁切、不补闭合线、不生成可导航路线。几何返回不证明同一道路；未识别不代表不存在环。':'当前仅提供往返尾巴裁剪与重复整圈的几何候选，不是任意道路去重。未经确认不会导出轨迹；转换后不保留时间戳，端点可能修整。结果不代表道路身份或通行条件，不能作为导航保证。';
    if(recognition){$('table-note').textContent='仅展示原始坐标；每次经过高亮一个连续原始区间及插值端点，保留其中全部折返与子环，不补闭合线。';return;}
    const label=state.applied?'转换结果':'候选预览';
    $('clean-key-label').textContent=label;$('clean-map-title').textContent=`${label} · ${state.applied?'已导出':'未导出'}`;
    $('clean-length-label').textContent=`${label}长度`;$('removed-length-label').textContent=state.applied?'转换减少行程':'候选拟减少行程';
    $('table-clean').textContent=`${label}坐标`;$('table-note').textContent=`${state.applied?'已确认转换并导出。':'候选预览尚未导出。'}与图中轨迹一一对应；悬停或键盘聚焦地图可读取最近轨迹点。`;
    $('original-pane').querySelector('h3').textContent=state.mode==='overlay'?`原始与${label}叠加`:'原始轨迹';
  }
  const candidateMessages={
    exact_recorded_coordinates_do_not_verify_road_identity:'原始记录坐标精确重合仍不证明物理道路身份相同。',
    crossing_connectivity_unresolved:'交叉处物理道路连通性未核实；几何环不代表真实路口（crossing_connectivity_unresolved）。',
    geometry_only_road_connectivity_unverified:'仅为几何识别，道路身份与物理连通性未经核实（geometry_only_road_connectivity_unverified）。',
    narrow_loop_or_displaced_retrace:'可能是窄环，也可能是有偏移的折返，需人工核对（narrow_loop_or_displaced_retrace）。',
    below_resolution:'几何结构低于当前支持的分辨率（below_resolution）。',
    noise_above_supported_resolution:'轨迹噪声超过当前支持的分辨率（noise_above_supported_resolution）。',
    resource_budget_exceeded:'已达到资源预算，仅返回部分识别结果（resource_budget_exceeded）。',
    retained_middle_near_linear_or_degenerate:'拟保留的中段近似直线或几何退化，不能安全视为主体闭环。',
    retained_middle_retrace_evidence:'拟保留的中段仍有明显折返证据，当前候选不满足安全转换条件。',
    stable_lateral_offset_parallel_access_evidence:'接入段之间存在稳定横向偏移，可能是不同的平行道路，不能安全按同一路段裁剪。',
    'GPX geometry matching does not establish that observations use the same road; road identity is not verified.':'GPX 几何匹配不能证明轨迹位于同一道路，道路身份未经核实。',
    'Known-pattern safety gates are conservative heuristics, not general reliable route or road identification.':'安全门槛只针对已知模式作保守检查，不是通用、可靠的路线或道路识别。',
    'The preview may snap endpoints; inspect against roads before navigation. Original timestamps are omitted.':'候选预览可能修整端点，转换结果不保留原始时间戳；必须另行核对实际道路，不能据此保证导航安全。',
    'Both access legs are removed, including unmatched travel; this is not merely deleting a duplicate traversal.':'主体闭环裁剪会删除首尾两段接入行程，包括无匹配证据的部分；并非只删除一次重复经过。'
  };
  const candidateText=text=>Object.prototype.hasOwnProperty.call(candidateMessages,text)?candidateMessages[text]:String(text);
  function listText(id, items) { $(id).replaceChildren();for(const text of items){const li=document.createElement('li');li.textContent=candidateText(text);$(id).append(li);} }
  function renderCandidate(candidate, report) {
    $('candidate-details').hidden=!candidate;
    $('confirmation-form').hidden=!candidateReady();
    if(!candidate)return;
    const c=candidate, tail=report.retraced_tail, laps=report.identical_laps;
    $('candidate-goal').textContent=goalNames[c.goal] || '不支持的目标（不能确认）';$('candidate-method').textContent=({reversed_tail_collapse:'首尾反向匹配与主体闭环裁剪',repeated_laps_collapse:'近似重复整圈与单圈保留'})[c.method] || c.method || '未提供';
    $('candidate-range').textContent=Array.isArray(c.kept_range_m) && c.kept_range_m.length===2 ? `${metres(c.kept_range_m[0])} 至 ${metres(c.kept_range_m[1])}` : '未提供';
    $('candidate-removed').textContent=metres(c.removed_travel_m);$('candidate-access').textContent=metres(c.unmatched_access_m);$('candidate-endpoint').textContent=metres(c.endpoint_adjustment_m);
    $('candidate-evidence').textContent=isTail(c)?`基于几何距离与方向的首尾反向匹配候选${Number.isFinite(tail?.prefix_matched_m)?`，连续匹配约 ${metres(tail.prefix_matched_m)}`:''}。主体闭环裁剪可能同时删除无匹配证据的接入段，不等于保留全部唯一路段，也不能据此确定为同一道路。`:`基于几何近似的重复整圈候选${Number.isFinite(laps?.count)?`，报告候选圈数 ${laps.count}`:''}。空间重合不证明道路身份相同。`;
    const warnings=Array.isArray(c.warnings)?c.warnings:[];
    const blocked=Array.isArray(c.blocked_reasons)?c.blocked_reasons:['候选缺少有效的阻止原因字段，不能确认。'];
    $('candidate-warnings').hidden=warnings.length===0;listText('warning-list',warnings);
    $('candidate-blocked').hidden=blocked.length===0;listText('blocked-list',blocked);
    $('access-loss-control').hidden=!isTail(c);
    for(const option of $('goal').options) if(option.value) option.disabled=option.value!==c.goal;
    $('confirmation-hint').textContent='请先选择候选目标，并逐项确认风险。';
  }
  function renderResult(result, selected, applied=false) {
    state.result=result;state.applied=applied;state.zoom=1;state.panX=0;state.panY=0;
    const r=result.report;
    // Web Mercator is display-only; the backend computes the geometry plan.
    state.project=p=>[p[0]*Math.PI/180,Math.log(Math.tan(Math.PI/4+Math.max(-85,Math.min(85,p[1]))*Math.PI/360))];
    const points=result.original.concat(result.cleaned).map(state.project);let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
    for(const [x,y] of points){minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);}
    state.bounds={cx:(minX+maxX)/2,cy:(minY+maxY)/2,width:Math.max(maxX-minX,1e-7),height:Math.max(maxY-minY,1e-7)};
    $('route-title').textContent=selected.name;$('route-subtitle').textContent=`${selected.relative_path} · ${applied?'已确认转换':'候选预览，未导出轨迹'}`;
    $('summary').hidden=false;$('result-details').hidden=false;$('map-empty').hidden=true;
    $('original-length').textContent=km(r.input_length_m);$('clean-length').textContent=km(r.output_length_m);$('removed-length').textContent=km(r.removed_length_m);$('point-count').textContent=`${result.original.length} → ${result.cleaned.length}`;
    $('original-map-info').textContent=km(r.input_length_m);$('clean-map-info').textContent=km(r.output_length_m);
    const candidate=state.analysis?.candidate;
    $('conclusion-heading').textContent=applied?'已确认转换 · 已导出':'候选分析 · 未导出';
    $('conclusion').textContent=applied?'已按你明确选择的目标转换并生成导出文件。此结果仅基于几何近似，不证明道路身份，也不保证保留全部唯一路段。':candidate?'下方是待核对的几何转换候选，不是已完成的去重结果。预览可能与原始轨迹相同；只有明确选择目标并确认风险后才会导出。':'当前规则未产生可确认的转换候选。预览保持原样；这不代表不存在重复道路。';
    $('result-badge').textContent=applied?'已确认转换 · 已导出':candidate?.blocked_reasons?.length?'候选已阻止 · 未导出':'候选预览 · 未导出';
    $('closure-note').textContent=`${applied?'转换结果':'候选报告'}${r.output_closed?'几何上闭合':'未标为严格闭合'}。匹配半径 ${state.analysis?.radius ?? Number($('radius').value)} 米。闭合不代表道路可通行。`;
    updateResultLabels();renderCandidate(candidate,r);setDownloads(result.downloads,applied);
    $('report').textContent=JSON.stringify(r,null,2);renderMaps();renderTable();
  }
  function recognitionItems() {
    const report=state.result?.report;
    return [...(report?.loops||[]).map(loop=>({loop,kind:'simple_loop'})),...(report?.loop_candidates||[]).map(loop=>({loop,kind:'candidate'})),...(report?.closed_walks||[]).map(loop=>({loop,kind:'closed_walk'}))];
  }
  function selectedOccurrences() {
    const selected=state.loopSelection;
    return recognitionItems().filter(item=>!selected || item.loop.id===selected.loopId).flatMap(item=>(item.loop.occurrences||[]).filter(occurrence=>!selected?.occurrenceId || occurrence.id===selected.occurrenceId).map(occurrence=>({...item,occurrence})));
  }
  const recognitionNames={simple_loop:'简单环',candidate:'待核对简单环候选',closed_walk:'闭合行走（非简单环）'};
  function renderLoopList() {
    const host=$('loop-list');host.replaceChildren();
    const button=(text,selection)=>{const node=document.createElement('button');node.type='button';node.textContent=text;node.className='loop-choice';node.setAttribute('aria-pressed',String(JSON.stringify(state.loopSelection)===JSON.stringify(selection)));node.addEventListener('click',()=>{const restoreFocus=document.activeElement===node;state.loopSelection=selection;renderLoopList();renderMaps();if(restoreFocus)host.querySelector('button[aria-pressed="true"]')?.focus({preventScroll:true});});host.append(node);};
    button('全部简单环、候选与闭合行走',null);
    for(const {loop,kind} of recognitionItems()){
      const structure=kind==='closed_walk'?` · ${loop.contains_retrace?'含折返':'未标记折返'} / ${loop.contains_subloops?'含子环':'未标记子环'}`:'';
      button(`${loop.id} · ${recognitionNames[kind]} · ${km(loop.length_m)} · ${loop.occurrences.length} 次经过${structure}`,{loopId:loop.id});
      for(const occurrence of loop.occurrences)button(`　${occurrence.id} · ${occurrence.direction==='reverse'?'反向':'正向'} · ${occurrence.status==='complete'?'完整经过':'部分经过'} · ${km(occurrence.length_m)}`,{loopId:loop.id,occurrenceId:occurrence.id});
    }
    $('loop-selection').textContent=state.loopSelection?`当前选择 ${state.loopSelection.loopId}${state.loopSelection.occurrenceId?' / '+state.loopSelection.occurrenceId:''}；每次经过保留一个连续原始区间，包含其中全部行程，不补闭合线。`:'显示全部简单环、候选与闭合行走；可选择单个结构或一次完整 / 部分经过记录。';
    if(!recognitionItems().length)$('loop-selection').textContent='本次没有识别到满足条件的几何环；不代表原轨迹不存在环。';
  }
  const fraction=value=>`${(value*100).toFixed(2)}%`;
  const closureNames={exact_endpoint:'首尾坐标精确闭合',within_tolerance:'首尾在容差内接近（非精确闭合）',open:'开放轨迹'};
  const legacyRetrace='旧报告未提供折返测量，请重新识别获取';
  function renderRetraces(result) {
    const summary=result.report.route_summary, host=$('retrace-list');host.replaceChildren();
    if(result.schema_version==='2.0'){$('retrace-note').textContent=legacyRetrace;return;}
    $('retrace-note').textContent=`按原始记录几何精确测量，不代表物理道路身份；索引从 0 开始，长度包含去程和回程，重叠区间按并集计量。${summary.annotation_status==='partial'?'折返扫描不完整：已检出值只是下限，未检出不代表不存在折返。':'扫描完成；仅覆盖原始顶点序列完全反向一致的折返，不排除有偏移或不同采样的折返。'}`;
    for(const annotation of result.report.retrace_annotations){
      const whole=annotation.source_interval.start.s_m===0 && Math.abs(annotation.source_interval.end.s_m-result.report.input_length_m)<=1e-6;
      const li=document.createElement('li');
      li.textContent=`${annotation.id} · ${whole?'整条精确反向折返':annotation.is_short_local?'短局部精确折返':'精确反向折返'} · 原始 0 基索引 ${annotation.start_vertex_index} → ${annotation.turn_vertex_index} → ${annotation.end_vertex_index} · 沿程 ${metres(annotation.source_interval.start.s_m)} 至 ${metres(annotation.source_interval.end.s_m)} · ${metres(annotation.length_m)} / ${fraction(annotation.fraction_of_track)}（原始记录几何，非物理道路身份）`;
      host.append(li);
    }
    if(!result.report.retrace_annotations.length){const li=document.createElement('li');li.textContent=summary.annotation_status==='partial'?'尚未检出精确折返，扫描不完整，是否存在仍未知。':'本轮未检出精确反向序列；不等于没有折返。';host.append(li);}
  }
  function renderRecognition(result,selected) {
    state.result=result;state.applied=false;state.zoom=1;state.panX=0;state.panY=0;state.loopSelection=null;
    setOriginalBounds(result.original);
    const r=result.report, partial=r.completeness!=='complete';
    $('route-title').textContent=selected.name;$('route-subtitle').textContent=`${selected.relative_path} · 环识别，只读原轨迹`;
    for(const id of ['summary','result-details','loop-details'])$(id).hidden=false;
    for(const id of ['map-empty','candidate-details','confirmation-form'])$(id).hidden=true;
    updateResultLabels();$('original-length').textContent=km(r.input_length_m);$('clean-length').textContent=String(r.loops.length);$('removed-length').textContent=String(r.loop_candidates.length);$('point-count').textContent=String(result.original.length);
    $('original-map-info').textContent=km(r.input_length_m);$('result-badge').textContent=partial?'识别不完整 · 原轨迹未变':'环识别完成 · 原轨迹未变';
    $('conclusion-heading').textContent=partial?'部分识别结果':'环识别结果';
    const summary=r.route_summary, legacy=result.schema_version==='2.0';
    const headline=legacy?`整条原轨迹 ${metres(r.input_length_m)}。${legacyRetrace}。`:`整条原轨迹 ${metres(summary.length_m)} · ${closureNames[summary.closure_kind]}；已检出精确折返合计 ${metres(summary.retrace_union_length_m)} / ${fraction(summary.retrace_union_fraction)}（原始记录几何区间并集${summary.annotation_status==='partial'?'，扫描不完整，仅为下限':''}）。`;
    $('conclusion').textContent=`${headline} 已识别 ${r.loops.length} 个简单环，${r.loop_candidates.length} 个待核对简单环候选，另有 ${r.closed_walks.length} 个闭合行走（不计入简单环）。${r.has_loops===null?'是否存在简单环仍不确定。':r.has_loops?'存在满足当前几何条件的简单环。':'未识别到满足当前条件的简单环，不等于不存在环。'}${partial?'本次覆盖不完整，请查看限制。':''} 未转换或导出任何轨迹。`;
    const whole=r.whole_track;$('closure-note').textContent=`${legacy?`旧报告整条轨迹${whole.is_closed?'在容差内首尾接近':'未判定首尾闭合'}`:closureNames[summary.closure_kind]}；首尾距离 ${metres(whole.start_end_distance_m)}，闭合容差 ${metres(whole.tolerance_m)}。道路身份未经核实，不作导航保证。`;
    renderRetraces(result);
    const warnings=[...r.limitations,...(legacy?[]:summary.limitations)];for(const {loop} of recognitionItems())for(const warning of loop.warnings||[])warnings.push(`${loop.id}: ${candidateText(warning)}`);
    if(!legacy)for(const annotation of r.retrace_annotations)for(const warning of annotation.warnings)warnings.push(`${annotation.id}: ${candidateText(warning)}`);
    listText('loop-limitations',warnings);renderLoopList();setDownloads({report:result.downloads?.report},false);$('report').textContent=JSON.stringify(r,null,2);renderMaps();renderTable();
  }
  // Source positions are offsets on ORIGINAL edges, not indices in resampled geometry.
  function positionOffset(position) {
    if(!position || position.segment_id!==0 || !Number.isInteger(position.edge_start_index) || position.edge_end_index!==position.edge_start_index+1 || !Number.isFinite(position.t) || position.t<0 || position.t>1)return null;
    const index=position.edge_start_index;
    return index>=0 && index+1<state.result.original.length?index+position.t:null;
  }
  function coordinateAt(offset) {
    const points=state.result.original, index=Math.min(Math.floor(offset),points.length-2), t=offset-index;
    return [points[index][0]+(points[index+1][0]-points[index][0])*t,points[index][1]+(points[index+1][1]-points[index][1])*t];
  }
  function occurrenceSegments(occurrence) {
    // V2 validates one continuous original interval. Never erase an excursion.
    const interval=occurrence.source_intervals[0],start=positionOffset(interval.start),end=positionOffset(interval.end);
    const points=[coordinateAt(start)];
    for(let index=Math.floor(start)+1;index<end;index++)points.push(state.result.original[index]);
    points.push(coordinateAt(end));return [points];
  }
  function validRecognition(result) {
    const r=result?.report,nonnegative=value=>Number.isFinite(value)&&value>=0,strings=value=>Array.isArray(value)&&value.every(text=>typeof text==='string');
    if(!['2.0','2.1'].includes(result?.schema_version) || result.kind!=='loop_recognition' || r?.kind!=='loop_recognition' || !Array.isArray(result.original) || result.original.length<2 || !result.original.every(p=>Array.isArray(p)&&p.length>=2&&Number.isFinite(p[0])&&Math.abs(p[0])<=180&&Number.isFinite(p[1])&&Math.abs(p[1])<=90) || typeof result.downloads?.report!=='string' || !result.downloads.report)return false;
    if(r.input_points!==result.original.length || !nonnegative(r.input_length_m) || !['complete','partial'].includes(r.completeness) || !strings(r.limitations) || r.road_identity_verified!==false || ![true,false,null].includes(r.has_loops))return false;
    if(typeof r.whole_track?.is_closed!=='boolean' || !nonnegative(r.whole_track.start_end_distance_m) || !nonnegative(r.whole_track.tolerance_m))return false;
    if(!Array.isArray(r.loops) || !Array.isArray(r.loop_candidates) || !Array.isArray(r.closed_walks))return false;
    if((r.loops.length>0 && r.has_loops!==true) || (r.loops.length===0 && r.has_loops===true))return false;
    const close=(a,b)=>Math.abs(a-b)<=Math.max(1e-6,1e-9*Math.max(Math.abs(a),Math.abs(b)));
    const sourceOffset=position=>{
      if(!position || position.segment_id!==0 || !Number.isInteger(position.edge_start_index) || position.edge_start_index<0 || position.edge_end_index!==position.edge_start_index+1 || position.edge_end_index>=result.original.length || !Number.isFinite(position.t) || position.t<0 || position.t>1 || !nonnegative(position.s_m) || position.s_m>r.input_length_m+1e-6)return null;
      return position.edge_start_index+position.t;
    };
    const ids=new Set();
    for(const [items,closedWalk] of [[r.loops,false],[r.loop_candidates,false],[r.closed_walks,true]])for(const loop of items){
      if(!loop || typeof loop.id!=='string' || !loop.id || ids.has(loop.id) || !nonnegative(loop.length_m) || loop.length_m===0 || !nonnegative(loop.closure_gap_m) || !strings(loop.warnings) || !Array.isArray(loop.occurrences) || !loop.occurrences.length)return false;
      ids.add(loop.id);
      if(closedWalk && (loop.kind!=='closed_walk' || typeof loop.contains_retrace!=='boolean' || typeof loop.contains_subloops!=='boolean'))return false;
      const occurrenceIds=new Set();
      for(const occurrence of loop.occurrences){
        if(!occurrence || typeof occurrence.id!=='string' || !occurrence.id || occurrenceIds.has(occurrence.id) || !['forward','reverse'].includes(occurrence.direction) || !['complete','partial'].includes(occurrence.status) || !nonnegative(occurrence.start_m) || !nonnegative(occurrence.end_m) || occurrence.end_m<=occurrence.start_m || !nonnegative(occurrence.length_m) || !close(occurrence.length_m,occurrence.end_m-occurrence.start_m) || !nonnegative(occurrence.closure_gap_m) || !Array.isArray(occurrence.source_intervals) || occurrence.source_intervals.length!==1 || !Array.isArray(occurrence.excluded_intervals) || occurrence.excluded_intervals.length!==0)return false;
        occurrenceIds.add(occurrence.id);
        const interval=occurrence.source_intervals[0],start=sourceOffset(interval?.start),end=sourceOffset(interval?.end);
        if(start===null || end===null || start>=end || !close(interval.start.s_m,occurrence.start_m) || !close(interval.end.s_m,occurrence.end_m))return false;
      }
    }
    if(result.schema_version==='2.1'){
      const summary=r.route_summary, annotations=r.retrace_annotations;
      if(!summary || !Array.isArray(annotations) || !nonnegative(summary.length_m) || !close(summary.length_m,r.input_length_m) || !['exact_endpoint','within_tolerance','open'].includes(summary.closure_kind) || summary.retrace_measurement!=='exact_original_geometry' || summary.method!=='original_coordinate_palindromes_v1' || !['complete','partial'].includes(summary.annotation_status) || !strings(summary.limitations) || !strings(summary.retrace_annotation_ids) || !nonnegative(summary.retrace_union_length_m) || !nonnegative(summary.retrace_union_fraction) || summary.retrace_union_fraction>1 || summary.short_local_rule?.max_length_m!==100 || summary.short_local_rule.max_fraction!==.05)return false;
      const route=summary.source_interval,routeStart=sourceOffset(route?.start),routeEnd=sourceOffset(route?.end);
      // _position may choose the rightmost zero-length duplicate. Vertex identity
      // is explicit; permit only identical intervening coordinates, never a jump.
      const atVertex=(position,index)=>{
        const offset=sourceOffset(position);if(offset===null)return false;
        const point=result.original[index],same=i=>result.original[i][0]===point[0]&&result.original[i][1]===point[1];
        const a=Math.floor(offset),b=Math.ceil(offset);
        for(let i=Math.min(index,a);i<=Math.max(index,b);i++)if(!same(i))return false;
        return true;
      };
      if(routeStart===null || routeEnd===null || routeStart>routeEnd || !close(route.start.s_m,0) || !close(route.end.s_m,r.input_length_m) || !atVertex(route.start,0) || !atVertex(route.end,result.original.length-1))return false;
      const first=result.original[0],last=result.original.at(-1),exact=first[0]===last[0]&&first[1]===last[1];
      const closure=exact?'exact_endpoint':r.whole_track.is_closed?'within_tolerance':'open';
      if(summary.closure_kind!==closure)return false;
      const annotationIds=new Set(),ranges=[];
      for(const annotation of annotations){
        if(!annotation || typeof annotation.id!=='string' || !annotation.id || annotationIds.has(annotation.id) || annotation.measurement!=='exact_original_geometry' || typeof annotation.is_short_local!=='boolean' || !strings(annotation.warnings) || annotation.evidence?.method!=='original_reverse_vertex_sequence' || annotation.evidence.boundary_basis!=='original_vertices' || annotation.evidence.max_reverse_error_m!==0)return false;
        annotationIds.add(annotation.id);
        const a=annotation.start_vertex_index,t=annotation.turn_vertex_index,b=annotation.end_vertex_index;
        if(![a,t,b].every(Number.isInteger) || a<0 || !(a<t&&t<b) || b>=result.original.length)return false;
        const interval=annotation.source_interval,start=sourceOffset(interval?.start),end=sourceOffset(interval?.end),turn=sourceOffset(annotation.turnaround);
        if(start===null || end===null || turn===null || start>=end || turn<start || turn>end || !atVertex(interval.start,a) || !atVertex(annotation.turnaround,t) || !atVertex(interval.end,b))return false;
        const lo=interval.start.s_m,hi=interval.end.s_m,mid=annotation.turnaround.s_m;
        if(!(lo<mid&&mid<hi) || !nonnegative(annotation.length_m) || annotation.length_m===0 || !close(annotation.length_m,hi-lo) || !close(mid-lo,hi-mid) || !nonnegative(annotation.fraction_of_track) || annotation.fraction_of_track>1 || !close(annotation.fraction_of_track,annotation.length_m/r.input_length_m))return false;
        const whole=close(lo,0)&&close(hi,r.input_length_m);
        if(annotation.is_short_local!==(annotation.length_m<=100 && annotation.fraction_of_track<=.05 && !whole))return false;
        ranges.push([lo,hi]);
      }
      if(summary.retrace_annotation_ids.length!==annotationIds.size || new Set(summary.retrace_annotation_ids).size!==annotationIds.size || summary.retrace_annotation_ids.some(id=>!annotationIds.has(id)))return false;
      ranges.sort((a,b)=>a[0]-b[0]);let union=0,right=0;
      for(const [start,end] of ranges){union+=Math.max(0,end-Math.max(start,right));right=Math.max(right,end);}
      if(!close(summary.retrace_union_length_m,union) || !close(summary.retrace_union_fraction,r.input_length_m?union/r.input_length_m:0))return false;
    }
    return true;
  }
  function validResult(result) { return result && result.report && Array.isArray(result.original) && result.original.length>0 && Array.isArray(result.cleaned) && result.cleaned.length>0; }
  $('work-mode').addEventListener('change',()=>{state.workMode=$('work-mode').value==='transform'?'transform':'recognition';modeSettings();invalidate();controls();status('工作模式已切换，旧结果与确认已清除。请重新运行。');});
  $('radius').addEventListener('input',()=>{invalidate();controls();status('匹配半径已改变，原候选与确认已失效。请重新分析。');});
  for(const id of ['folder','recursive']) $(id).addEventListener(id==='folder'?'input':'change',()=>{state.selected=null;state.preview=null;state.files=[];invalidate();renderFiles();controls();status('扫描设置已改变，原候选与确认已失效。请重新读取路线。');});
  for(const id of ['goal','confirm','acknowledge-geometry-only','acknowledge-access-loss']) $(id).addEventListener('change',()=>{controls();$('confirmation-hint').textContent=canApply()?'已完成显式确认，可转换并导出。':'请先选择候选目标，并逐项确认风险。';});
  $('run').addEventListener('click',async()=>{
    if(state.busy || state.scanning || !state.selected)return;
    const radius=Number($('radius').value);
    const workMode=state.workMode,maxRadius=recognizing()?30:100;
    if(!Number.isFinite(radius)||radius<1||radius>maxRadius){status(`匹配半径请输入 1–${maxRadius} 米。`,true);$('radius').focus();return;}
    const selected=state.selected;invalidate();const version=state.version;state.busy=true;state.operation='analyze';controls();renderFiles();status(`正在${recognizing()?'识别':'分析'} ${selected.name}，不会导出轨迹…`);
    try {
      const result=await api(recognizing()?'/api/recognize-loops':'/api/analyze',{file_id:selected.id,radius});
      if(version!==state.version || workMode!==state.workMode || selected.id!==state.selected?.id || radius!==Number($('radius').value))return;
      if(recognizing()){
        if(!validRecognition(result))throw new Error('环识别结果版本、来源区间或测量无效：需要 v2.0 / v2.1 连续原始区间及对应完整字段，请重新识别。');
        renderRecognition(result,selected);status(result.report.completeness==='complete'?'环识别完成：仅提供原轨迹区间与报告，不转换、不导出轨迹。':'环识别不完整：请查看限制；已有结果不代表覆盖全部轨迹。');return;
      }
      if(!validResult(result) || typeof result.analysis_id!=='string' || !result.analysis_id || typeof result.plan_digest!=='string' || !result.plan_digest)throw new Error('分析结果字段不完整，请重新分析。');
      state.analysis={...result,file_id:selected.id,radius};resetConfirmation();renderResult(result,selected);
      status(result.candidate?.blocked_reasons?.length?'分析完成：候选已被阻止，不能确认；仅可下载分析报告。':result.candidate?'分析完成：仅为候选预览，尚未导出轨迹。请核对证据并手动选择目标。':'分析完成：当前规则未产生可确认候选；轨迹未导出，仅可下载分析报告。');
    }catch(error){if(version===state.version){invalidate();status(error.message,true);}}finally{state.busy=false;state.operation=null;controls();renderFiles();}
  });
  $('confirmation-form').addEventListener('submit',async event=>{
    event.preventDefault();if(state.busy || state.scanning || !canApply())return;
    const analysis=state.analysis, selected=state.selected, version=state.version;
    const body={analysis_id:analysis.analysis_id,plan_digest:analysis.plan_digest,goal:$('goal').value,confirm:true,acknowledge_geometry_only:true,acknowledge_access_loss:$('acknowledge-access-loss').checked};
    state.busy=true;state.operation='apply';clearDownloads();controls();renderFiles();status('正在确认转换并导出，请等待服务返回结果…');
    try {
      const result=await api('/api/apply',body);
      if(version!==state.version || state.analysis!==analysis || selected.id!==state.selected?.id)return;
      if(!validResult(result) || typeof result.run_id!=='string' || !result.run_id || ['gpx','geojson','report','manifest'].some(key=>typeof result.downloads?.[key]!=='string' || !result.downloads[key]))throw new Error('转换结果或下载清单不完整；已隐藏下载，请重新分析。');
      renderResult(result,selected,true);resetConfirmation();$('confirmation-form').hidden=true;status('已按你确认的目标转换并生成导出文件。请核对坐标、报告与转换清单；结果不提供道路或导航保证。');
    }catch(error){if(version===state.version){clearDownloads();state.analysis=null;resetConfirmation();$('confirmation-form').hidden=true;$('result-badge').textContent='确认失败 · 未导出';$('route-subtitle').textContent=`${selected.relative_path} · 确认失败，仅保留候选预览`;$('conclusion').textContent='确认转换失败，当前仍为候选预览。所有下载链接已清除；请重新分析，不要把预览当作导出结果。';status(`${error.message} 所有下载链接已清除，请重新分析。`,true);}}finally{state.busy=false;state.operation=null;controls();renderFiles();}
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
    const baseWidth=!recognizing() && state.mode==='split'?Math.min($('original-map').clientWidth||width,$('clean-map').clientWidth||width):width;
    const baseHeight=height;const b=state.bounds;const scale=Math.min((baseWidth-90)/b.width,(baseHeight-115)/b.height)*state.zoom;
    const screen=p=>{const[x,y]=state.project(p);return[(x-b.cx)*scale+width/2+state.panX,-(y-b.cy)*scale+height/2+state.panY+14];};
    const selectable=[];
    for(const item of series){
      if(!$(item.key==='original'?'show-original':'show-clean').checked)continue;
      const projected=state.result[item.key].map(screen);
      if(!projected.length)continue;
      const d=projected.map((p,i)=>`${i?'L':'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' ');
      g.append(svgNode('path',{d,fill:'none',stroke:'var(--plot)','stroke-width':5,'stroke-linecap':'round','stroke-linejoin':'round'}));
      g.append(svgNode('path',{d,fill:'none',stroke:`var(--${item.key==='original'?'original':'cleaned'})`,'stroke-width':2.3,'stroke-dasharray':item.key==='original'?'7 4':'none','stroke-linejoin':'round','stroke-linecap':'round',class:'route-line'}));
      const first=projected[0];g.append(svgNode('circle',{cx:first[0],cy:first[1],r:5,fill:`var(--${item.key==='original'?'original':'cleaned'})`,stroke:'var(--plot)','stroke-width':2}));
      const label=svgNode('text',{x:first[0]+10,y:first[1]-10,fill:'var(--ink)','font-size':11});label.textContent=`${item.label}起点`;g.append(label);
      for(let i=0;i<projected.length;i++)selectable.push({xy:projected[i],point:state.result[item.key][i],index:i,label:item.label});
    }
    if(recognizing() && $('show-clean').checked){
      for(const {loop,kind,occurrence} of selectedOccurrences()){
        const segments=occurrenceSegments(occurrence);
        for(const segment of segments){
          const points=segment.map(screen),d=points.map((p,i)=>`${i?'L':'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' ');
          g.append(svgNode('path',{d,fill:'none',stroke:'var(--plot)','stroke-width':6,'stroke-linejoin':'round'}));
          const path=svgNode('path',{d,fill:'none',stroke:'var(--cleaned)','stroke-width':2.5,'stroke-dasharray':kind==='candidate'?'4 4':kind==='closed_walk'?'10 3 2 3':'none',class:'route-line loop-interval','data-loop-id':loop.id,'data-occurrence-id':occurrence.id,'data-recognition-kind':kind});
          const title=svgNode('title');title.textContent=`${loop.id} / ${occurrence.id} · ${recognitionNames[kind]} · ${metres(occurrence.length_m)}`;path.append(title);g.append(path);
          segment.forEach((point,index)=>selectable.push({xy:points[index],point,index,label:`${loop.id} / ${occurrence.id}`}));
        }
        const lastInterval=occurrence.source_intervals?.at(-1),end=positionOffset(lastInterval?.end);
        if(end!==null && segments.length){const [x,y]=screen(coordinateAt(end));g.append(svgNode('circle',{cx:x,cy:y,r:7,fill:'var(--plot)',stroke:'var(--cleaned)','stroke-width':2,class:'return-marker'}));const text=svgNode('text',{x:x+11,y:y+18,fill:'var(--ink)','font-size':11});text.textContent=`${loop.id} ${recognitionNames[kind]} · 返回点`;g.append(text);}
      }
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
  const original={key:'original',label:'原始'},cleaned={key:'cleaned',label:'候选预览'};
  function renderMaps(){if(recognizing()){drawMap($('original-map'),[original]);$('clean-map').replaceChildren();return;}cleaned.label=state.applied?'转换结果':'候选预览';drawMap($('original-map'),state.mode==='overlay'?[original,cleaned]:[original]);if(state.mode==='split')drawMap($('clean-map'),[cleaned]);}
  function fit(){state.zoom=1;state.panX=0;state.panY=0;renderMaps();}
  document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>{state.mode=button.dataset.mode;document.querySelectorAll('[data-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));$('map-stage').className=`map-stage ${state.mode}`;updateResultLabels();renderMaps();}));
  $('fit').addEventListener('click',fit);$('zoom-in').addEventListener('click',()=>{state.zoom=Math.min(40,state.zoom*1.3);renderMaps();});$('zoom-out').addEventListener('click',()=>{state.zoom=Math.max(.5,state.zoom/1.3);renderMaps();});
  for(const id of ['show-original','show-clean'])$(id).addEventListener('change',renderMaps);
  new ResizeObserver(()=>renderMaps()).observe($('map-stage'));
  function renderTable(){if(!state.result)return;const tbody=$('coordinates');tbody.replaceChildren();const fragment=document.createDocumentFragment();state.result[state.table].forEach((point,index)=>{const tr=document.createElement('tr');for(const value of [index+1,point[0].toFixed(7),point[1].toFixed(7)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}fragment.append(tr);});tbody.append(fragment);for(const [id,key]of [['table-original','original'],['table-clean','cleaned']])$(id).setAttribute('aria-pressed',String(state.table===key));}
  $('table-original').addEventListener('click',()=>{state.table='original';renderTable();});$('table-clean').addEventListener('click',()=>{state.table='cleaned';renderTable();});
  // Legacy clients can explicitly opt into the unchanged P0 workflow.
  if(new URLSearchParams(location.search).get('mode')==='transform')state.workMode='transform';
  modeSettings();invalidate();controls();
  (async()=>{try{state.config=await api('/api/config');$('folder').value=state.config.default_dir;await scan(state.config.default_dir);}catch(error){status(error.message,true);}})();
}
