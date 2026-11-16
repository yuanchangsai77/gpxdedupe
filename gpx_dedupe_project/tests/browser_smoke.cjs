'use strict';
// Real-service acceptance test. Requires an already-running service; never starts one.
// Run with PLAYWRIGHT_MODULE pointing at an installed playwright package.
// For server-free intercepted checks, run browser_p0.cjs instead.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
(async()=>{
  const output=path.resolve(__dirname,'../outputs/browser-smoke');fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({executablePath:process.env.BROWSER_PATH || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1100},deviceScaleFactor:1});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    const external=[];page.on('request',r=>{if(!r.url().startsWith('http://127.0.0.1:8765/')&&!r.url().startsWith('data:'))external.push(r.url());});
    await page.goto('http://127.0.0.1:8765/');
    await page.waitForFunction(()=>!document.querySelector('#scan').disabled);
    await page.waitForFunction(()=>Number(document.querySelector('#file-count').textContent)>0);
    let applyRequests=0;page.on('request',request=>{if(request.url().endsWith('/api/apply'))applyRequests++;});
    const assertNoGeometryDownloads=async()=>{for(const id of ['download-gpx','download-geojson','download-manifest']){assert.equal(await page.locator('#'+id).isVisible(),false);assert.equal(await page.locator('#'+id).getAttribute('href'),null);}};
    const analyzeAndMaybeApply=async()=>{
      const before=applyRequests;
      const analysisResponse=page.waitForResponse(response=>response.url().endsWith('/api/analyze')&&response.request().method()==='POST');
      await page.locator('#run').click();const response=await analysisResponse;assert.equal(response.ok(),true);
      const analysis=await response.json();await page.waitForFunction(()=>!document.querySelector('#run').disabled,{},{timeout:120000});
      assert.equal(applyRequests,before,'analysis alone must not call apply');await assertNoGeometryDownloads();
      assert.match(await page.locator('#result-badge').innerText(),/未导出/);assert.equal(await page.locator('#download-report').isVisible(),true);
      const candidate=analysis.candidate;
      if(!candidate || candidate.blocked_reasons.length) {
        await page.locator('#confirmation-form').isVisible().then(visible=>assert.equal(visible,false));
        assert.deepEqual(analysis.cleaned,analysis.original,'blocked/no-candidate analysis must retain original geometry');
        assert.equal(analysis.report.output_length_m,analysis.report.input_length_m);
        assert.equal(analysis.report.removed_length_m,0);
        if(candidate){assert.equal(await page.locator('#candidate-blocked').isVisible(),true);assert.equal(await page.locator('#blocked-list li').count(),candidate.blocked_reasons.length);}
        return {analysis,result:analysis,applied:false};
      }
      assert.equal(await page.locator('#goal').inputValue(),'','goal must not be automatically selected');
      assert.equal(await page.locator('#apply').isDisabled(),true);
      await page.locator('#goal').selectOption(candidate.goal);await page.locator('#confirm').check();await page.locator('#acknowledge-geometry-only').check();
      if(candidate.goal==='main_loop'||candidate.method==='reversed_tail_collapse')await page.locator('#acknowledge-access-loss').check();
      const appliedResponse=page.waitForResponse(r=>r.url().endsWith('/api/apply')&&r.request().method()==='POST');
      await page.locator('#apply').click();const applied=await appliedResponse;assert.equal(applied.ok(),true);const result=await applied.json();
      const body=applied.request().postDataJSON();assert.equal(body.analysis_id,analysis.analysis_id);assert.equal(body.plan_digest,analysis.plan_digest);assert.equal(body.goal,candidate.goal);assert.equal(body.confirm,true);assert.equal(body.acknowledge_geometry_only,true);
      await page.waitForFunction(()=>document.querySelector('#result-badge').textContent.includes('已确认转换'),{},{timeout:120000});
      for(const id of ['download-gpx','download-geojson','download-report','download-manifest'])assert.equal(await page.locator('#'+id).isVisible(),true);
      assert.equal(applyRequests,before+1);return {analysis,result,applied:true};
    };
    console.log('User folder listed GPX files:',await page.locator('#file-count').innerText());
    const userFile=await page.locator('.file-item[aria-pressed=true] strong').innerText();
    await analyzeAndMaybeApply();
    console.log('User folder first file:',userFile,'->',await page.locator('#status').innerText());
    await page.locator('#filter').fill('37002172.gpx');
    const privateRoute=page.locator('.file-item').filter({has:page.locator('strong',{hasText:/^37002172\.gpx$/})}).first();
    if (await privateRoute.count()) {
      await privateRoute.click();
      const target=await analyzeAndMaybeApply();
      assert.equal(target.analysis.report.source,'37002172.gpx');
      if(target.analysis.candidate) {
        assert.equal(target.analysis.candidate.method,'reversed_tail_collapse');
        assert.ok(target.analysis.report.retraced_tail.prefix_matched_m>3000);
        assert.ok(target.analysis.candidate.unmatched_access_m>2000);
        assert.match(await page.locator('#candidate-evidence').innerText(),/无匹配证据.*不等于保留全部唯一路段/);
      }
      console.log('Reported route:',JSON.stringify({applied:target.applied,report:target.result.report}));
      await page.screenshot({path:path.join(output,'37002172-desktop.png'),fullPage:true});
      await page.locator('[data-mode=overlay]').click();
      await page.screenshot({path:path.join(output,'37002172-overlay.png'),fullPage:true});
      await page.locator('[data-mode=split]').click();
    } else {
      console.log('SKIP: optional private 37002172.gpx route is not listed.');
    }
    await page.locator('#filter').fill('');
    await page.locator('#use-example').click();
    await page.waitForFunction(()=>!document.querySelector('#scan').disabled&&document.querySelector('#folder').value.endsWith('examples'));
    const publicRoute=page.locator('.file-item').filter({has:page.locator('strong',{hasText:/^34044834\.gpx$/})}).first();
    await publicRoute.click();
    const publicResult=await analyzeAndMaybeApply();
    const metric=value=>`${(value/1000).toFixed(3)} km`;
    assert.equal(await page.locator('#original-length').innerText(),metric(publicResult.result.report.input_length_m));
    assert.equal(await page.locator('#clean-length').innerText(),metric(publicResult.result.report.output_length_m));
    assert.equal(await page.locator('#removed-length').innerText(),metric(publicResult.result.report.removed_length_m));
    console.log('Public route:',JSON.stringify({applied:publicResult.applied,blocked_reasons:publicResult.analysis.candidate?.blocked_reasons || [],report:publicResult.result.report}));
    assert.equal(await page.locator('.route-line').count(),2);
    await page.screenshot({path:path.join(output,'comparison-desktop.png'),fullPage:true});
    await page.locator('[data-mode=overlay]').click();
    assert.equal(await page.locator('#clean-pane').isVisible(),false);
    const before=await page.locator('#original-map .route-line').first().getAttribute('d');
    await page.locator('#zoom-in').click();
    const after=await page.locator('#original-map .route-line').first().getAttribute('d');
    assert.notEqual(before,after);
    const map=page.locator('#original-map svg');const box=await map.boundingBox();
    await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await page.mouse.move(box.x+box.width/2+60,box.y+box.height/2+30,{steps:4});await page.mouse.up();
    const panned=await page.locator('#original-map .route-line').first().getAttribute('d');assert.notEqual(after,panned);
    await page.locator('#fit').click();
    await page.locator('#show-original').uncheck();assert.equal(await page.locator('#original-map .route-line').count(),1);
    await page.locator('#show-original').check();
    if(publicResult.applied) {
      const downloadPromise=page.waitForEvent('download');await page.locator('#download-gpx').click();const download=await downloadPromise;
      assert.equal(download.suggestedFilename(),'cleaned.gpx');await download.saveAs(path.join(output,'downloaded.gpx'));
    } else {
      await assertNoGeometryDownloads();console.log('Public candidate not applicable: verified original geometry and no GPX/GeoJSON/manifest export.');
    }
    await page.locator('#theme-toggle').click();assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
    await page.screenshot({path:path.join(output,'comparison-overlay-dark.png'),fullPage:true});
    await page.locator('#theme-toggle').click();
    await page.locator('.data-section > summary').click();assert.equal(await page.locator('#coordinates tr').count(),publicResult.result.original.length);
    await page.locator('#table-clean').click();assert.equal(await page.locator('#coordinates tr').count(),publicResult.result.cleaned.length);
    await page.locator('.data-section > summary').click();
    await page.setViewportSize({width:390,height:844});
    await page.locator('[data-mode=split]').click();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:path.join(output,'comparison-mobile.png'),fullPage:true});
    await page.goto('http://127.0.0.1:8765/about');
    assert.equal(await page.title(),'项目说明 · GPX 路线');
    assert.equal(await page.locator('.brand').innerText(),'GPX 路线');
    assert.equal(await page.locator('body').innerText().then(text=>text.includes('实验室')),false);
    assert.equal(await page.locator('#support tbody tr').count(),12);
    assert.equal(await page.locator('.topology-case').count(),12);
    assert.equal(await page.locator('#benchmark-table tbody tr').count(),10);
    assert.equal(await page.locator('#noise-table tbody tr').count(),4);
    assert.match(await page.locator('#benchmark-results').innerText(),/65\/120（54.17%）/);
    assert.match(await page.locator('#benchmark-results').innerText(),/9\/24 被误裁/);
    assert.match(await page.locator('#rates').innerText(),/真实.*成功概率.*未知/);
    const badAnchors=await page.evaluate(()=>[...document.querySelectorAll('a[href^="#"]')].filter(a=>!document.getElementById(a.hash.slice(1))).map(a=>a.hash));
    assert.deepEqual(badAnchors,[]);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:path.join(output,'about-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1440,height:1100});
    await page.screenshot({path:path.join(output,'about-desktop.png'),fullPage:true});
    await page.evaluate(()=>scrollTo(0,0));
    await page.screenshot({path:path.join(output,'guide-top.png')});
    await page.locator('#benchmark-results').evaluate(node=>node.scrollIntoView({block:'start'}));
    await page.screenshot({path:path.join(output,'guide-rates.png')});
    await page.locator('#theme-toggle').click();
    assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
    await page.locator('#benchmark-results').evaluate(node=>node.scrollIntoView({block:'start'}));
    await page.screenshot({path:path.join(output,'guide-rates-dark.png')});
    await page.setViewportSize({width:390,height:844});
    await page.locator('#cases').evaluate(node=>node.scrollIntoView({block:'start'}));
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:path.join(output,'guide-cases-mobile.png')});
    assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    console.log('PASS: folder, real runs, comparison modes, zoom, pan, layers, download, themes, data table, mobile layouts, about page; no JS errors or external requests.');
    console.log('Screenshots:',output);
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
