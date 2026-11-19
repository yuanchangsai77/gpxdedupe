'use strict';
const $ = id => document.getElementById(id);
const root = document.documentElement;
try { root.dataset.theme = localStorage.getItem('gpx-theme') || 'light'; } catch {}
function updateThemeButton() { if ($('theme-toggle')) $('theme-toggle').textContent = root.dataset.theme === 'dark' ? '浅色' : '深色'; }
updateThemeButton();
$('theme-toggle')?.addEventListener('click', () => { root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark'; try { localStorage.setItem('gpx-theme', root.dataset.theme); } catch {} updateThemeButton(); });
if ($('folder-form')) startWorkbench();
function startWorkbench() {
  const state = {config:null, files:[], expanded:new Map(), preview:null, selected:null, result:null, analysis:null, applied:false, busy:false, scanning:false, workMode:'classification', mode:'split', zoom:1, panX:0, panY:0, bounds:null, table:'original', version:0, operation:null};
  const classifying=()=>state.workMode==='classification';
  function modeSettings() {
    const classification=classifying();$('work-mode').value=state.workMode;
    $('radius').value='18';$('radius-options').hidden=classification;
    $('mode-note').textContent=classification?'1 无法判断 · 2 环 · 3 非环 · 4 非标准环':'先分析几何候选，核对后再确认转换与导出。';
    document.querySelector('.intro').textContent=classification?'按整条轨迹的几何形态识别路线类型。':'先分析几何候选，核对后再确认转换与导出。';
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
    return Boolean(!classifying() && a && !state.applied && a.file_id===state.selected?.id && a.radius===Number($('radius').value) && c && c.requires_confirmation===true && ['main_loop','one_lap'].includes(c.goal) && Array.isArray(c.blocked_reasons) && c.blocked_reasons.length===0);
  }
  function canApply() { return candidateReady() && $('goal').value===state.analysis.candidate.goal && $('confirm').checked && $('acknowledge-geometry-only').checked && (!isTail(state.analysis.candidate) || $('acknowledge-access-loss').checked); }
  function controls() {
    const locked=state.busy || state.scanning;
    for(const id of ['scan','use-example','use-default','folder','recursive','radius','filter','work-mode','goal','confirm','acknowledge-geometry-only','acknowledge-access-loss']) $(id).disabled=locked;
    $('run').disabled=locked || !state.selected;
    $('apply').disabled=locked || !canApply();
    $('run').querySelector('span').textContent=state.operation==='analyze' ? (classifying()?'正在识别…':'正在分析…') : (classifying()?'识别路线类型':'仅分析候选');
    $('apply').textContent=state.operation==='apply' ? '正在确认转换…' : '确认转换并导出';
    document.body.classList.toggle('busy',locked);
    document.querySelector('.comparison-area').setAttribute('aria-busy',String(locked));
  }
  const downloadKeys=[['download-gpx','gpx'],['download-geojson','geojson'],['download-report','report'],['download-manifest','manifest']];
  function clearDownloads() { for(const [id] of downloadKeys) { $(id).hidden=true;$(id).removeAttribute('href'); } }
  function setDownloads(downloads, applied) {
    clearDownloads();
    for(const [id,key] of downloadKeys) if((applied || key==='report') && typeof downloads?.[key]==='string' && downloads[key]) { $(id).href=downloads[key];$(id).hidden=false; }
    $('download-report').textContent=classifying()?'路线类型报告 JSON':applied?'转换报告 JSON':'分析报告 JSON';
  }
  function resetConfirmation() { $('goal').value='';for(const id of ['confirm','acknowledge-geometry-only','acknowledge-access-loss']) $(id).checked=false; }
  function invalidate() {
    state.version++;state.analysis=null;state.result=null;state.applied=false;state.bounds=null;state.table='original';resetConfirmation();clearDownloads();
    for(const id of ['summary','result-details','candidate-details','confirmation-form']) $(id).hidden=true;
    $('coordinates').replaceChildren();$('report').textContent='';$('map-empty').hidden=false;
    $('route-title').textContent=state.selected?.name || (classifying()?'识别路线类型':'先核对候选，再决定转换');$('route-subtitle').textContent=classifying()?'请点击「识别路线类型」；仅生成只读识别报告。':'请点击「仅分析候选」生成新的候选预览。';$('result-badge').textContent=classifying()?'等待识别 · 原轨迹未变':'等待分析 · 未导出';
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
      status(`已预览 ${file.name}。可点击下方按钮进行${classifying()?'路线类型识别':'候选分析'}。`);
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
    const classification=classifying();
    $('table-clean').hidden=classification;
    $('clean-control').hidden=classification;
    $('map-stage').className='map-stage '+(classification?'overlay':state.mode);
    document.querySelector('.map-toolbar .segmented').hidden=classification;
    document.querySelector('.notice span').textContent=classification?'仅根据原始轨迹的几何形态分类。':'当前仅提供往返尾巴裁剪与重复整圈的几何候选。未经确认不会导出轨迹。转换后不保留时间戳，端点可能修整。结果不能作为导航保证。';
    if(classification){$('original-pane').querySelector('h3').textContent='原始轨迹';$('table-note').textContent='仅展示原始坐标。';return;}
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
  function renderClassification(result, selected) {
    if (!state.result && state.preview?.id === selected.id) showPreview(state.preview.original);
    const category=result.category+' · '+(result.category===1?'无法判断':result.label);
    $('route-subtitle').textContent=selected.relative_path;
    $('result-badge').textContent=category;
    $('result-details').hidden=false;
    $('conclusion-heading').textContent='路线类型 '+category;
    $('conclusion').textContent=result.reasons.join(' ');
    $('closure-note').textContent='';
    setDownloads(result.downloads,false);
    $('report').textContent=JSON.stringify(result,null,2);
  }
  function validResult(result) { return result && result.report && Array.isArray(result.original) && result.original.length>0 && Array.isArray(result.cleaned) && result.cleaned.length>0; }
  $('work-mode').addEventListener('change',()=>{state.workMode=$('work-mode').value==='transform'?'transform':'classification';modeSettings();invalidate();controls();status('工作模式已切换，旧结果与确认已清除。请重新运行。');});
  $('radius').addEventListener('input',()=>{invalidate();controls();status('匹配半径已改变，原候选与确认已失效。请重新分析。');});
  for(const id of ['folder','recursive']) $(id).addEventListener(id==='folder'?'input':'change',()=>{state.selected=null;state.preview=null;state.files=[];invalidate();renderFiles();controls();status('扫描设置已改变，原候选与确认已失效。请重新读取路线。');});
  for(const id of ['goal','confirm','acknowledge-geometry-only','acknowledge-access-loss']) $(id).addEventListener('change',()=>{controls();$('confirmation-hint').textContent=canApply()?'已完成显式确认，可转换并导出。':'请先选择候选目标，并逐项确认风险。';});
  $('run').addEventListener('click',async()=>{
    if(state.busy || state.scanning || !state.selected)return;
    const radius=Number($('radius').value);
    const workMode=state.workMode;
    if(!classifying() && (!Number.isFinite(radius)||radius<1||radius>100)){status(`匹配半径请输入 1–100 米。`,true);$('radius').focus();return;}
    const selected=state.selected;invalidate();if(classifying() && state.preview?.id===selected.id)showPreview(state.preview.original);const version=state.version;state.busy=true;state.operation='analyze';controls();renderFiles();status(`正在${classifying()?'识别':'分析'} ${selected.name}，不会导出轨迹…`);
    try {
      const result=classifying()?await api('/api/classify',{file_id:selected.id}):await api('/api/analyze',{file_id:selected.id,radius});
      if(version!==state.version || workMode!==state.workMode || selected.id!==state.selected?.id || (!classifying() && radius!==Number($('radius').value)))return;
      if(classifying()){
        if(result.kind!=='route_shape_classification' || ![1,2,3,4].includes(result.category) || typeof result.label!=='string' || !Array.isArray(result.reasons))throw new Error('路线类型报告字段不完整，请重新识别。');
        renderClassification(result,selected);
        status(result.category===1?'识别完成，请查看无法判断的原因。':'路线类型识别完成。');
        return;
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
    const baseWidth=!classifying() && state.mode==='split'?Math.min($('original-map').clientWidth||width,$('clean-map').clientWidth||width):width;
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
  function renderMaps(){if(classifying()){drawMap($('original-map'),[original]);$('clean-map').replaceChildren();return;}cleaned.label=state.applied?'转换结果':'候选预览';drawMap($('original-map'),state.mode==='overlay'?[original,cleaned]:[original]);if(state.mode==='split')drawMap($('clean-map'),[cleaned]);}
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
