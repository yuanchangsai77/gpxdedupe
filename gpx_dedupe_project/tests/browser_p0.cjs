'use strict';
// Fully intercepted HTTP: no local server is started or required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/7281/scoop/persist/nodejs/cache/_npx/420ff84f11983ee5/node_modules/playwright');
const web = path.resolve(__dirname, '../web');
const original = [[116,40],[116.001,40.001],[116.002,40],[116,40]];
const cleaned = [[116.001,40.001],[116.002,40],[116.001,40.001]];
const report = {input_length_m:1000,output_length_m:700,removed_length_m:300,input_points:4,output_points:3,output_closed:true,method:'reversed_tail_collapse',retraced_tail:{prefix_matched_m:110,gap_m:2},config:{match_radius_m:18}};
const candidate = {goal:'main_loop',method:'reversed_tail_collapse',kept_range_m:[100,800],removed_travel_m:300,unmatched_access_m:80,endpoint_adjustment_m:2,warnings:['GPX geometry matching does not establish that observations use the same road; road identity is not verified.','The preview may snap endpoints; inspect against roads before navigation. Original timestamps are omitted.','future_warning_code'],blocked_reasons:[],requires_confirmation:true};
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    const context=await browser.newContext();const page=await context.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    let scenario='tail', failApply=false, delayAnalyze=false, delayScan=false, delayApply=false, releaseAnalyze, releaseScan, releaseApply;
    const requests=[];
    await context.route('**/*',async route=>{
      const request=route.request(), url=new URL(request.url()), pathname=url.pathname;
      const json=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
      if(pathname==='/api/config')return json({default_dir:'D:/mock',sample_dir:'D:/examples'});
      if(pathname==='/api/files') {
        if(delayScan)await new Promise(resolve=>{releaseScan=resolve;});
        return json({folder:'D:/mock',files:[{id:'file-a',name:'a.gpx',relative_path:'a.gpx',size_bytes:4096},{id:'file-b',name:'b.gpx',relative_path:'b.gpx',size_bytes:1024}]});
      }
      if(pathname==='/api/analyze') {
        requests.push({path:pathname,body:request.postDataJSON()});
        if(delayAnalyze)await new Promise(resolve=>{releaseAnalyze=resolve;});
        const c=scenario==='null'?null:{...candidate, ...(scenario==='lap'?{goal:'one_lap',method:'repeated_laps_collapse',unmatched_access_m:0}:{}),blocked_reasons:scenario==='blocked'?['retained_middle_near_linear_or_degenerate','retained_middle_retrace_evidence','stable_lateral_offset_parallel_access_evidence','future_block_code']:[]};
        const unchanged=scenario==='null'||scenario==='blocked';
        const previewReport=unchanged?{...report,method:'unchanged',output_length_m:report.input_length_m,removed_length_m:0,output_points:original.length,proposed_output:{method:report.method,output_length_m:report.output_length_m}}:report;
        return json({analysis_id:'analysis-'+requests.length,plan_digest:'digest-123',report:previewReport,candidate:c,original,cleaned:unchanged?original:cleaned,downloads:{report:'/download/analysis/report'}});
      }
      if(pathname==='/api/apply') {
        requests.push({path:pathname,body:request.postDataJSON()});
        if(delayApply)await new Promise(resolve=>{releaseApply=resolve;});
        if(failApply)return json({error:'源文件已变化'},409);
        return json({run_id:'run-1',report,original,cleaned,downloads:{gpx:'/download/run/gpx',geojson:'/download/run/geojson',report:'/download/run/report',manifest:'/download/run/manifest'}});
      }
      if(pathname==='/' || ['/app.js','/style.css'].includes(pathname)) {
        const name=pathname==='/'?'index.html':pathname.slice(1);
        return route.fulfill({status:200,contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(web,name))});
      }
      return route.fulfill({status:404,body:'mock route not found'});
    });
    const hidden=async id=>assert.equal(await page.locator('#'+id).isVisible(),false,id+' must be hidden');
    const noDownloads=async()=>{for(const id of ['download-gpx','download-geojson','download-report','download-manifest']){await hidden(id);assert.equal(await page.locator('#'+id).getAttribute('href'),null);}};
    const analyze=async()=>{await page.locator('#run').click();await page.waitForFunction(()=>!document.querySelector('#run').disabled);};
    const acknowledge=async(tail=true)=>{await page.locator('#goal').selectOption(tail?'main_loop':'one_lap');await page.locator('#confirm').check();await page.locator('#acknowledge-geometry-only').check();if(tail)await page.locator('#acknowledge-access-loss').check();};
    const selectSecondAfterScan=async()=>{
      const files=page.locator('.file-item');
      assert.equal(await files.count(),2);
      for(const file of await files.all())assert.equal(await file.isDisabled(),false,'files unlock immediately after scanning');
      const before=requests.length;
      await files.nth(1).click();
      assert.equal(await files.nth(1).getAttribute('aria-pressed'),'true');
      assert.equal(requests.length,before,'selecting another file requires no analysis');
      await files.first().click();
    };
    await page.goto('http://gpx-p0.test/');await page.waitForFunction(()=>!document.querySelector('#run').disabled);
    await selectSecondAfterScan();
    await analyze();assert.deepEqual(requests[0],{path:'/api/analyze',body:{file_id:'file-a',radius:18}});
    assert.match(await page.locator('#result-badge').innerText(),/候选.*未导出/);assert.equal(await page.locator('#goal').inputValue(),'');
    assert.equal(await page.locator('#apply').isDisabled(),true);await hidden('download-gpx');await hidden('download-geojson');await hidden('download-manifest');assert.equal(await page.locator('#download-report').isVisible(),true);
    assert.match(await page.locator('#candidate-access').innerText(),/80.00/);assert.match(await page.locator('#candidate-endpoint').innerText(),/2.00/);
    assert.match(await page.locator('#warning-list').innerText(),/道路身份未经核实/);assert.match(await page.locator('#warning-list').innerText(),/不保留原始时间戳/);assert.match(await page.locator('#warning-list').innerText(),/future_warning_code/);
    await page.locator('#confirm').check();await page.locator('#acknowledge-geometry-only').check();await page.locator('#acknowledge-access-loss').check();assert.equal(await page.locator('#apply').isDisabled(),true);
    await page.locator('#goal').selectOption('main_loop');await page.locator('#acknowledge-access-loss').uncheck();assert.equal(await page.locator('#apply').isDisabled(),true);await page.locator('#acknowledge-access-loss').check();
    delayApply=true;await page.locator('#apply').click();await page.waitForFunction(()=>document.querySelector('#apply').textContent.includes('正在确认'));
    for(const id of ['run','scan','folder','recursive','radius','filter','use-example','use-default','goal','confirm','acknowledge-geometry-only','acknowledge-access-loss','apply'])assert.equal(await page.locator('#'+id).isDisabled(),true,id+' locks during apply');
    await noDownloads();delayApply=false;releaseApply();await page.waitForFunction(()=>document.querySelector('#result-badge').textContent.includes('已确认转换'));
    assert.deepEqual(requests.at(-1).body,{analysis_id:'analysis-1',plan_digest:'digest-123',goal:'main_loop',confirm:true,acknowledge_geometry_only:true,acknowledge_access_loss:true});
    for(const id of ['download-gpx','download-geojson','download-report','download-manifest'])assert.equal(await page.locator('#'+id).isVisible(),true);
    await hidden('confirmation-form');
    // File changes immediately clear all output and confirmation state.
    await page.locator('.file-item').nth(1).click();await hidden('confirmation-form');await noDownloads();await hidden('result-details');
    await analyze();assert.equal(requests.at(-1).body.file_id,'file-b');await acknowledge();
    await page.locator('#radius').fill('22');await hidden('confirmation-form');await noDownloads();await hidden('result-details');
    await analyze();assert.equal(requests.at(-1).body.radius,22);assert.equal(await page.locator('#goal').inputValue(),'');
    // Scan starts by invalidating, before its response arrives, and locks inputs.
    delayScan=true;await page.locator('#scan').click();await page.waitForFunction(()=>document.querySelector('#scan').disabled);await hidden('confirmation-form');await noDownloads();
    for(const id of ['run','folder','recursive','radius','filter','use-example','use-default','goal','confirm'])assert.equal(await page.locator('#'+id).isDisabled(),true,id+' locks during scan');
    delayScan=false;releaseScan();await page.waitForFunction(()=>!document.querySelector('#scan').disabled);
    await selectSecondAfterScan();
    // Blocked candidates disclose reasons but cannot submit even by dispatching a form event.
    scenario='blocked';await analyze();await hidden('confirmation-form');assert.match(await page.locator('#blocked-list').innerText(),/近似直线或几何退化/);assert.match(await page.locator('#blocked-list').innerText(),/明显折返/);assert.match(await page.locator('#blocked-list').innerText(),/平行道路/);assert.match(await page.locator('#blocked-list').innerText(),/future_block_code/);assert.equal(await page.locator('#apply').isDisabled(),true);
    const before=requests.length;await page.locator('#confirmation-form').evaluate(form=>form.dispatchEvent(new Event('submit',{cancelable:true})));assert.equal(requests.length,before);await hidden('download-gpx');
    scenario='null';await analyze();await hidden('candidate-details');await hidden('confirmation-form');await hidden('download-gpx');assert.match(await page.locator('#conclusion').innerText(),/不代表不存在重复道路/);
    // A one-lap candidate still requires explicit goal and geometry acknowledgement, but not tail acknowledgement.
    scenario='lap';await analyze();await hidden('access-loss-control');assert.equal(await page.locator('#goal').inputValue(),'');await acknowledge(false);assert.equal(await page.locator('#apply').isDisabled(),false);
    // A failed apply removes even the previous report link and requires a fresh analysis.
    failApply=true;await page.locator('#apply').click();await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('所有下载链接已清除'));await noDownloads();await hidden('confirmation-form');assert.match(await page.locator('#result-badge').innerText(),/确认失败/);
    failApply=false;scenario='tail';delayAnalyze=true;await page.locator('#run').click();await page.waitForFunction(()=>document.querySelector('#radius').disabled);
    for(const id of ['run','scan','folder','recursive','radius','filter','use-example','use-default','goal','confirm'])assert.equal(await page.locator('#'+id).isDisabled(),true,id+' locks during analysis');
    assert.equal(await page.locator('.file-item').first().isDisabled(),true);
    // Programmatic state changes while a request is in flight cannot revive the stale analysis.
    await page.locator('#radius').evaluate(input=>{input.value='23';input.dispatchEvent(new Event('input',{bubbles:true}));});delayAnalyze=false;releaseAnalyze();await page.waitForFunction(()=>!document.querySelector('#run').disabled);await hidden('result-details');await noDownloads();
    await analyze();await page.locator('[data-mode="overlay"]').click();assert.equal(await page.locator('[data-mode="overlay"]').getAttribute('aria-pressed'),'true');assert.match(await page.locator('#original-pane h3').innerText(),/候选预览/);
    await page.locator('.data-section > summary').click();await page.locator('#table-clean').click();assert.equal(await page.locator('#coordinates tr').count(),cleaned.length);
    await page.locator('#theme-toggle').click();assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
    await page.setViewportSize({width:390,height:844});await page.locator('[data-mode="split"]').click();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'mobile has no horizontal overflow');
    await acknowledge();assert.equal(await page.locator('#apply').isDisabled(),false);
    if(process.env.P0_SCREENSHOT) {
      await page.setViewportSize({width:1440,height:1100});
      if(await page.locator('html').getAttribute('data-theme')==='dark')await page.locator('#theme-toggle').click();
      await page.locator('.data-section > summary').click();
      await page.screenshot({path:process.env.P0_SCREENSHOT,fullPage:true});
    }
    assert.deepEqual(errors,[]);console.log('P0 browser mock tests passed: analyze/apply, explicit consent, tail/lap, blocked/null, invalidation, busy/stale, failure, maps/table/theme, mobile.');
    await context.close();
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
