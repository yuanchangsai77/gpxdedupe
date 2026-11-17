'use strict';
// Browser-only fixtures: no app server, navigation or external network required.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/7281/scoop/persist/nodejs/cache/_npx/420ff84f11983ee5/node_modules/playwright');
const web=path.resolve(__dirname,'../web');
// A-B-C-D-C-E-A: D and the complete C-D-C retrace must remain highlighted.
const original=[[116,40],[116.004,40],[116.004,40.004],[116.006,40.004],[116.004,40.004],[116,40.004],[116,40]];
const pos=(i,t=0)=>({segment_id:0,edge_start_index:i,edge_end_index:i+1,t,s_m:(i+t)*100});
const interval=(a,b)=>({start:a,end:b});
const occurrence=(id,start,end,extra={})=>({id,direction:'forward',status:'complete',source_intervals:[interval(start,end)],excluded_intervals:[],start_m:start.s_m,end_m:end.s_m,length_m:end.s_m-start.s_m,closure_gap_m:5,...extra});
function fixture(scenario){
 const walk={id:'walk-001',kind:'closed_walk',length_m:600,closure_gap_m:5,contains_retrace:true,contains_subloops:false,warnings:[],occurrences:[occurrence('walk-complete',pos(0),pos(5,1)),occurrence('walk-partial',pos(1,.5),pos(4,.5),{direction:'reverse',status:'partial'})]};
 const candidate={id:'candidate-001',length_m:200,closure_gap_m:7,evidence:{},warnings:['crossing_connectivity_unresolved','narrow_loop_or_displaced_retrace','below_resolution'],occurrences:[occurrence('candidate-partial-1',pos(0,.5),pos(1,.5),{status:'partial'}),occurrence('candidate-partial-2',pos(4),pos(5,.5),{status:'partial'})]};
 const simpleOriginal=[original[0],original[1],original[2],original[5],original[6]];
 const loop={id:'loop-001',length_m:400,closure_gap_m:0,evidence:{},warnings:[],occurrences:[occurrence('simple-complete',pos(0),pos(3,1))]};
 const points=scenario==='simple'?simpleOriginal:original;
 const result={schema_version:'2.0',kind:'loop_recognition',recognition_id:'recognition-only',original:points,input_sha256:'abc',config:{match_radius_m:12},report:{kind:'loop_recognition',input_points:points.length,input_length_m:(points.length-1)*100,whole_track:{is_closed:true,start_end_distance_m:5,tolerance_m:25},has_loops:scenario==='partial'?null:scenario==='simple',loops:scenario==='simple'?[loop]:[],loop_candidates:['empty','simple','walk-only'].includes(scenario)?[]:[candidate],closed_walks:['empty','simple'].includes(scenario)?[]:[walk],completeness:scenario==='partial'?'partial':'complete',limitations:scenario==='partial'?['resource_budget_exceeded']:[],road_identity_verified:false},downloads:{report:'/api/download/recognition-only/report'}};
 const bad=result.report.closed_walks[0];
 if(scenario==='v1')result.schema_version='1.0';
 if(scenario==='multiple')bad.occurrences[0].source_intervals.push(interval(pos(1),pos(2)));
 if(scenario==='excluded')bad.occurrences[0].excluded_intervals.push(interval(pos(2),pos(4)));
 if(scenario==='bounds')bad.occurrences[0].source_intervals[0].end.edge_end_index=points.length;
 if(scenario==='source-distance')bad.occurrences[0].source_intervals[0].start.s_m=10;
 if(scenario==='length')bad.occurrences[0].length_m=400;
 if(scenario==='partial-multiple')bad.occurrences[1].source_intervals.push(interval(pos(0),pos(1)));
 if(scenario==='candidate-excluded')candidate.occurrences[0].excluded_intervals.push(interval(pos(1),pos(2)));
 if(scenario==='flags')delete bad.contains_retrace;
 if(scenario==='walk-kind')bad.kind='simple_loop';
 if(scenario==='no-walks')delete result.report.closed_walks;
 if(scenario==='coordinate')result.original=[[181,40],...original.slice(1)];
 if(scenario.startsWith('new-'))return modernFixture(scenario);
 return result;
}
function modernFixture(scenario){
 const result=structuredClone(fixture('walk'));result.schema_version='2.1';const r=result.report;
 let distances=[0,1000,2000,2022.21,2044.42,3200,4271.15],a=2,t=3,b=4;
 if(scenario==='new-mixed'){result.original=[...original.slice(0,6),[115.999,40.004],original[5],original[6]];distances=[0,1000,2000,2022.21,2044.42,3200,3300,3400,5000];}
 if(scenario==='new-whole'){result.original=[original[0],original[1],original[2],original[3],original[2],original[1],original[0]];distances=[0,10,20,30,40,50,60];a=0;t=3;b=6;}
 if(scenario==='new-duplicates'){result.original=[original[0],original[1],original[2],original[2],original[1],original[0]];distances=[0,10,20,20,30,40];a=0;t=2;b=5;}
 r.input_points=result.original.length;r.input_length_m=distances.at(-1);r.whole_track.start_end_distance_m=0;
 const point=index=>index===result.original.length-1?{...pos(index-1,1),s_m:distances[index]}:{...pos(index),s_m:distances[index]};
 r.loops=[];r.loop_candidates=[];r.closed_walks=[];r.has_loops=false;
 const length=distances[b]-distances[a];
 const annotation={id:'retrace-001',source_interval:interval(point(a),point(b)),start_vertex_index:a,turn_vertex_index:t,end_vertex_index:b,turnaround:point(scenario==='new-duplicates'?3:t),length_m:length,fraction_of_track:length/r.input_length_m,measurement:'exact_original_geometry',is_short_local:a!==0 && length<=100 && length/r.input_length_m<=.05,evidence:{method:'original_reverse_vertex_sequence',boundary_basis:'original_vertices',max_reverse_error_m:0},warnings:['exact_recorded_coordinates_do_not_verify_road_identity']};
 r.retrace_annotations=[annotation];
 r.route_summary={source_interval:interval(point(0),point(result.original.length-1)),length_m:r.input_length_m,closure_kind:'exact_endpoint',retrace_annotation_ids:[annotation.id],retrace_union_length_m:length,retrace_union_fraction:length/r.input_length_m,retrace_measurement:'exact_original_geometry',annotation_status:'complete',limitations:[],method:'original_coordinate_palindromes_v1',short_local_rule:{max_length_m:100,max_fraction:.05}};
 const s=r.route_summary;
 if(['new-partial-empty','new-no-exact'].includes(scenario)){r.retrace_annotations=[];s.retrace_annotation_ids=[];s.retrace_union_length_m=0;s.retrace_union_fraction=0;if(scenario==='new-partial-empty'){s.annotation_status='partial';s.limitations=['resource_budget_exceeded'];}else{r.closed_walks=[{id:'walk-001',kind:'closed_walk',length_m:r.input_length_m,closure_gap_m:0,contains_retrace:true,contains_subloops:false,warnings:[],occurrences:[occurrence('walk-complete',point(0),point(result.original.length-1))]}];}}
 if(scenario==='new-overlap'){r.retrace_annotations.push({...structuredClone(annotation),id:'retrace-002'});s.retrace_annotation_ids.push('retrace-002');}
 if(scenario==='new-mixed'){r.retrace_annotations.push({...structuredClone(annotation),id:'retrace-002',source_interval:interval(point(5),point(7)),start_vertex_index:5,turn_vertex_index:6,end_vertex_index:7,turnaround:point(6),length_m:200,fraction_of_track:200/r.input_length_m,is_short_local:false});s.retrace_annotation_ids.push('retrace-002');s.retrace_union_length_m+=200;s.retrace_union_fraction=s.retrace_union_length_m/r.input_length_m;}
 if(scenario==='new-tolerance'){result.original.at(-1)[0]+=.00001;s.closure_kind='within_tolerance';r.whole_track.start_end_distance_m=1;}
 if(scenario==='new-open'){result.original.at(-1)[0]+=.01;s.closure_kind='open';r.whole_track.is_closed=false;r.whole_track.start_end_distance_m=1000;}
 if(scenario==='new-missing-summary')delete r.route_summary;
 if(scenario==='new-missing-annotations')delete r.retrace_annotations;
 if(scenario==='new-union')s.retrace_union_length_m+=1;
 if(scenario==='new-union-fraction')s.retrace_union_fraction=.5;
 if(scenario==='new-ids')s.retrace_annotation_ids=['unknown'];
 if(scenario==='new-duplicate-id')r.retrace_annotations.push(structuredClone(annotation));
 if(scenario==='new-index')annotation.turn_vertex_index=annotation.end_vertex_index;
 if(scenario==='new-index-bounds')annotation.end_vertex_index=result.original.length;
 if(scenario==='new-index-coordinate')annotation.start_vertex_index=1;
 if(scenario==='new-turn')annotation.turnaround=point(5);
 if(scenario==='new-turn-distance')annotation.turnaround.s_m=2001;
 if(scenario==='new-annotation-length')annotation.length_m+=1;
 if(scenario==='new-annotation-fraction')annotation.fraction_of_track=.8;
 if(scenario==='new-source-bounds')annotation.source_interval.end.edge_end_index=result.original.length;
 if(scenario==='new-route-source')s.source_interval.start.s_m=1;
 if(scenario==='new-closure')s.closure_kind='open';
 if(scenario==='new-short')annotation.is_short_local=false;
 if(scenario==='new-method')s.method='estimate';
 if(scenario==='new-version')result.schema_version='2.2';
 if(scenario.startsWith('new-missing-summary-field-'))delete s[scenario.slice('new-missing-summary-field-'.length)];
 if(scenario.startsWith('new-missing-annotation-field-'))delete annotation[scenario.slice('new-missing-annotation-field-'.length)];
 return result;
}
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 try{
  const context=await browser.newContext();const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  let scenario='walk',delay=false,release,fail=false;const requests=[];
  await context.route('**/*',async route=>{
   const pathname=new URL(route.request().url()).pathname;
   const json=(value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
   if(pathname==='/api/config')return json({default_dir:'D:/mock',sample_dir:'D:/examples'});
   if(pathname==='/api/files')return json({folder:'D:/mock',files:[{id:'a',name:'a.gpx',relative_path:'a.gpx',size_bytes:200},{id:'b',name:'b.gpx',relative_path:'b.gpx',size_bytes:200}]});
   if(pathname==='/api/recognize-loops'){
    requests.push({path:pathname,body:route.request().postDataJSON()});
    if(delay)await new Promise(resolve=>{release=resolve;});
    if(fail)return json({error:'识别失败，请重新识别'},400);
    return json(fixture(scenario));
   }
   if(pathname==='/api/analyze'||pathname==='/api/apply'){requests.push({path:pathname});return json({error:'unexpected transform request'},400);}
   if(pathname==='/' || ['/app.js','/style.css'].includes(pathname)){const name=pathname==='/'?'index.html':pathname.slice(1);return route.fulfill({status:200,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(web,name))});}
   return route.fulfill({status:404,body:'not found'});
  });
  const hidden=async id=>assert.equal(await page.locator('#'+id).isVisible(),false,id);
  const noDownloads=async()=>{for(const id of ['download-gpx','download-geojson','download-report','download-manifest']){await hidden(id);assert.equal(await page.locator('#'+id).getAttribute('href'),null);}};
  const run=async()=>{await page.locator('#run').click();await page.waitForFunction(()=>!document.querySelector('#run').disabled);};
  await page.goto('http://gpx-loops.test/');await page.waitForFunction(()=>!document.querySelector('#run').disabled);
  assert.equal(await page.locator('#work-mode').inputValue(),'recognition');assert.equal(await page.locator('#radius').inputValue(),'12');
  await run();assert.deepEqual(requests[0],{path:'/api/recognize-loops',body:{file_id:'a',radius:12}});
  await hidden('candidate-details');await hidden('confirmation-form');for(const id of ['download-gpx','download-geojson','download-manifest','table-clean'])await hidden(id);
  assert.equal(await page.locator('#download-report').isVisible(),true);assert.match(await page.locator('#conclusion').innerText(),/0 个简单环，1 个待核对简单环候选，另有 1 个闭合行走/);
  assert.equal(await page.locator('#clean-length').innerText(),'0');assert.equal(await page.locator('#removed-length').innerText(),'1');
  assert.match(await page.locator('#loop-limitations').innerText(),/crossing_connectivity_unresolved/);
  assert.equal(await page.locator('.loop-interval').count(),4);
  assert.equal(await page.locator('.loop-interval[data-loop-id="candidate-001"]').first().getAttribute('stroke-dasharray'),'4 4');
  assert.equal(await page.locator('.loop-interval[data-loop-id="walk-001"]').first().getAttribute('stroke-dasharray'),'10 3 2 3');
  assert.match(await page.getByRole('button',{name:/^walk-001/}).innerText(),/闭合行走（非简单环）.*含折返/);
  const keyboardChoice=page.getByRole('button',{name:/walk-complete/});
  await keyboardChoice.focus();await page.keyboard.press('Enter');
  assert.match(await page.evaluate(()=>document.activeElement.textContent),/walk-complete/,'selection preserves keyboard focus after list rebuild');
  await page.keyboard.press('Tab');
  assert.match(await page.evaluate(()=>document.activeElement.textContent),/walk-partial/,'Tab continues to adjacent occurrence');
  await page.keyboard.press('Space');
  assert.match(await page.evaluate(()=>document.activeElement.textContent),/walk-partial/,'Space selection preserves focus');
  await page.getByRole('button',{name:/walk-complete/}).click();assert.equal(await page.locator('.loop-interval').count(),1);assert.equal(await page.locator('.return-marker').count(),1);
  const fullD=await page.locator('.loop-interval').getAttribute('d');
  const originalD=await page.locator('.route-line:not(.loop-interval)').getAttribute('d');
  assert.equal(fullD,originalD,'full closed walk retains D and every original edge, including C-D-C');
  assert.equal((fullD.match(/L/g)||[]).length,6);assert.equal((fullD.match(/M/g)||[]).length,1);
  // Partial records remain individually selectable, continuous and interpolated.
  await page.getByRole('button',{name:/walk-partial/}).click();assert.equal(await page.locator('.loop-interval').count(),1);
  assert.match(await page.getByRole('button',{name:/walk-partial/}).innerText(),/反向.*部分经过/);
  await page.getByRole('button',{name:/candidate-partial-1/}).click();assert.equal(await page.locator('.loop-interval').count(),1);
  const partialD=await page.locator('.loop-interval').getAttribute('d');assert.equal((partialD.match(/L/g)||[]).length,2);
  await page.getByRole('button',{name:/candidate-partial-2/}).click();assert.equal(await page.locator('.loop-interval').count(),1);assert.notEqual(await page.locator('.loop-interval').getAttribute('d'),partialD);
  await page.locator('#original-map svg').focus();await page.keyboard.press('ArrowRight');await page.keyboard.press('Home');
  await page.locator('.data-section > summary').click();assert.equal(await page.locator('#coordinates tr').count(),original.length);
  assert.equal(await page.locator('#coordinates tr').nth(3).locator('td').nth(1).innerText(),'116.0060000','D remains in original table');
  await page.locator('#theme-toggle').click();assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.locator('#radius').fill('13');await hidden('result-details');await noDownloads();
  scenario='walk-only';await run();assert.match(await page.locator('#conclusion').innerText(),/0 个简单环，0 个待核对简单环候选，另有 1 个闭合行走/);assert.match(await page.locator('#conclusion').innerText(),/不等于不存在环/);
  scenario='simple';await run();assert.equal(await page.locator('#clean-length').innerText(),'1');assert.match(await page.locator('#conclusion').innerText(),/1 个简单环.*另有 0 个闭合行走/);assert.equal(await page.locator('.loop-interval').getAttribute('stroke-dasharray'),'none');
  scenario='partial';await run();assert.match(await page.locator('#result-badge').innerText(),/不完整/);assert.match(await page.locator('#conclusion').innerText(),/仍不确定/);assert.match(await page.locator('#loop-limitations').innerText(),/resource_budget_exceeded/);
  scenario='empty';await run();assert.match(await page.locator('#conclusion').innerText(),/不等于不存在环/);assert.equal(await page.locator('.loop-interval').count(),0);
  assert.match(await page.locator('#conclusion').innerText(),/旧报告未提供折返测量，请重新识别获取/);assert.equal(await page.locator('#retrace-list li').count(),0);
  scenario='new-valid';await run();assert.match(await page.locator('#conclusion').innerText(),/^整条原轨迹 4271\.15 米 · 首尾坐标精确闭合；已检出精确折返合计 44\.42 米 \/ 1\.04%/);
  assert.match(await page.locator('#retrace-list').innerText(),/短局部精确折返.*0 基索引 2 → 3 → 4.*44\.42 米 \/ 1\.04%.*非物理道路身份/);
  assert.match(await page.locator('#retrace-note').innerText(),/长度包含去程和回程/);assert.equal(await page.locator('.loop-interval').count(),0,'annotations add no map layers');
  for(const valid of ['new-overlap','new-duplicates','new-tolerance','new-open']){scenario=valid;await run();assert.equal(await page.locator('#result-details').isVisible(),true,valid);}
  assert.match(await page.locator('#conclusion').innerText(),/开放轨迹/);
  scenario='new-whole';await run();assert.match(await page.locator('#retrace-list').innerText(),/整条精确反向折返/);assert.doesNotMatch(await page.locator('#retrace-list').innerText(),/短局部/);
  scenario='new-partial-empty';await run();assert.match(await page.locator('#retrace-note').innerText(),/扫描不完整/);assert.match(await page.locator('#retrace-list').innerText(),/是否存在仍未知/);assert.match(await page.locator('#conclusion').innerText(),/扫描不完整，仅为下限/);
  scenario='new-no-exact';await run();assert.match(await page.locator('#retrace-list').innerText(),/未检出.*不等于没有折返/);assert.match(await page.getByRole('button',{name:/^walk-001/}).innerText(),/含折返/);
  scenario='new-mixed';await run();assert.match(await page.locator('#conclusion').innerText(),/244\.42 米/);assert.doesNotMatch(await page.locator('#conclusion').innerText(),/短局部/);assert.match(await page.locator('#retrace-list li').nth(0).innerText(),/短局部/);assert.doesNotMatch(await page.locator('#retrace-list li').nth(1).innerText(),/短局部/);
  const summaryFields=Object.keys(modernFixture('new-valid').report.route_summary).map(key=>'new-missing-summary-field-'+key);
  const annotationFields=Object.keys(modernFixture('new-valid').report.retrace_annotations[0]).map(key=>'new-missing-annotation-field-'+key);
  for(const invalid of ['v1','multiple','excluded','bounds','source-distance','length','partial-multiple','candidate-excluded','flags','walk-kind','no-walks','coordinate','new-missing-summary','new-missing-annotations','new-union','new-union-fraction','new-ids','new-duplicate-id','new-index','new-index-bounds','new-index-coordinate','new-turn','new-turn-distance','new-annotation-length','new-annotation-fraction','new-source-bounds','new-route-source','new-closure','new-short','new-method','new-version',...summaryFields,...annotationFields]){
   scenario=invalid;await run();await hidden('result-details');await noDownloads();assert.match(await page.locator('#status').innerText(),/v2\.0.*请重新识别/,invalid);
  }
  delay=true;scenario='walk';await page.locator('#run').click();await page.waitForFunction(()=>document.querySelector('#work-mode').disabled);
  // A programmatic mode change must invalidate an in-flight recognition response.
  await page.locator('#work-mode').evaluate(select=>{select.value='transform';select.dispatchEvent(new Event('change'));});delay=false;release();await page.waitForFunction(()=>!document.querySelector('#run').disabled);await hidden('result-details');await noDownloads();
  assert.equal(await page.locator('#radius').inputValue(),'18');await page.locator('#work-mode').selectOption('recognition');assert.equal(await page.locator('#radius').inputValue(),'12');
  fail=true;await run();await hidden('result-details');await noDownloads();assert.match(await page.locator('#status').innerText(),/识别失败/);
  fail=false;await run();await page.locator('.file-item').nth(1).click();await hidden('result-details');await noDownloads();
  assert.equal(requests.some(r=>r.path==='/api/analyze'||r.path==='/api/apply'),false);
  assert.deepEqual(errors,[]);
  if(process.env.LOOPS_SCREENSHOT){await run();await page.setViewportSize({width:1440,height:1100});await page.screenshot({path:process.env.LOOPS_SCREENSHOT,fullPage:true});}
  console.log('Loop browser mock tests passed: v2.1 44.42m/1.04% summary, whole/mixed/partial/duplicate/union measurements, every missing summary/annotation field and malformed index/union/version rejection, v2.0 missing-measurement compatibility, continuous ABCDCEA/D retention, separate structure counts, full/partial selection, report-only, stale/file/radius/failure, keyboard/table/theme/mobile.');
  await context.close();
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
