const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE);
const path=require('node:path');
const {pathToFileURL}=require('node:url');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_PATH});
 try{
  const page=await browser.newPage({viewport:{width:1600,height:1100}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:8767/reconstruction.html#chengdu-18');
  await page.waitForFunction(()=>document.querySelector('#route-select')?.options.length===20);
  const ids=await page.evaluate(()=>routes.map(r=>r.id));
  for(const id of ids){
   await page.selectOption('#route-select',id);
   const state=await page.evaluate(()=>({fingerprint:!!data.fingerprint?.core_fingerprint,version:!!data.fingerprint?.version_fingerprint,dedup:data.dedup_executed,mode:data.hybrid.mode,segments:data.hybrid.segments.map(s=>s.kind),navigation:data.hybrid.navigation_enabled,controls:data.hybrid.controls.length,displayControls:anchors.length,attempts:data.hybrid.attempts.length,review:!!data.reconstruction_diagnosis?.review_preview}));
   assert.equal(state.fingerprint,true);assert.equal(state.version,true);assert.equal(state.dedup,false);assert.equal(state.navigation,false);
   assert.ok(state.controls>=2);assert.equal(state.controls,state.displayControls);assert.ok(state.attempts<=24);
   assert.ok(state.segments.every(kind=>kind==='road'));
   assert.equal(await page.locator('#original').isChecked(),true);
   assert.equal(await page.locator('#candidate').isChecked(),state.review);
   assert.equal(await page.locator('#hybrid').isChecked(),state.mode==='road_candidate');
   assert.equal(await page.locator('#semantic-panel').isVisible(),true);
   assert.match(await page.locator('#coverage-note').textContent(),/取消原轨迹/);
   if(state.mode!=='road_candidate')assert.equal(state.segments.length,0);
  }
  await page.selectOption('#route-select','chengdu-03');assert.equal(await page.locator('#candidate').isChecked(),true);assert.match(await page.locator('#grade-badge').textContent(),/待复核/);assert.ok(await page.evaluate(()=>animation.product.total>0));await page.locator('#motion-progress').evaluate(el=>{el.value='650';el.dispatchEvent(new Event('input'));});await page.waitForTimeout(150);assert.match(await page.locator('#motion-status').textContent(),/仅复核/);await page.screenshot({path:path.resolve(__dirname,'../outputs/chengdu-city/validation/chengdu-review-candidate-03.png'),fullPage:true});
  await page.selectOption('#route-select','chengdu-13');assert.equal(await page.evaluate(()=>data.hybrid.mode),'road_candidate');
  await page.selectOption('#route-select','chengdu-07');
  assert.match(await page.locator('#grade-badge').textContent(),/操场/);
  assert.equal(await page.evaluate(()=>animation.product.total),0);
  assert.equal(await page.locator('#motion-mode option[value=product]').isDisabled(),true);
  await page.selectOption('#route-select','chengdu-12');const first=await page.locator('#core-fingerprint').textContent(),v1=await page.locator('#variant-fingerprint').textContent();
  await page.selectOption('#route-select','chengdu-13');assert.equal(await page.locator('#core-fingerprint').textContent(),first);assert.notEqual(await page.locator('#variant-fingerprint').textContent(),v1);assert.match(await page.locator('#pair-note').textContent(),/未去重/);
  await page.selectOption('#route-select','chengdu-01');
  assert.equal(await page.locator('#playback-panel').isVisible(),true);
  await page.locator('#motion-play').click();await page.waitForTimeout(500);
  assert.ok(await page.evaluate(()=>animation.progress>0));
  await page.locator('#motion-play').click();const paused=await page.evaluate(()=>animation.progress);
  await page.waitForTimeout(200);assert.equal(await page.evaluate(()=>animation.progress),paused);
  await page.locator('#motion-progress').evaluate(el=>{el.value='650';el.dispatchEvent(new Event('input'));});
  assert.equal(await page.evaluate(()=>animation.progress),.65);assert.equal(await page.evaluate(()=>animation.playing),false);
  await page.selectOption('#motion-mode','original');await page.waitForTimeout(100);assert.doesNotMatch(await page.locator('#motion-status').textContent(),/展示·/);
  await page.selectOption('#motion-mode','product');await page.waitForTimeout(100);assert.match(await page.locator('#motion-status').textContent(),/展示·道路/);
  await page.selectOption('#motion-mode','both');await page.selectOption('#motion-speed','8');await page.locator('#motion-reset').click();assert.equal(await page.evaluate(()=>animation.progress),0);
  await page.selectOption('#mode','original');assert.equal(await page.locator('#original').isChecked(),true);assert.equal(await page.locator('#hybrid').isChecked(),false);
  await page.selectOption('#mode','both');assert.equal(await page.locator('#original').isChecked(),true);
  await page.locator('#pois').check();await page.locator('#pois').uncheck();
  await page.selectOption('#route-select','chengdu-18');await page.locator('#anchors').check();await page.locator('#fit').click();
  await page.locator('#motion-progress').evaluate(el=>{el.value='650';el.dispatchEvent(new Event('input'));});await page.waitForTimeout(1200);
  await page.screenshot({path:path.resolve(__dirname,'../outputs/chengdu-city/validation/chengdu-sparse-road-review.png'),fullPage:true});
  await page.selectOption('#route-select','chengdu-12');
  const downloadPromise=page.waitForEvent('download');await page.locator('#download-fingerprint').click();const download=await downloadPromise;assert.match(download.suggestedFilename(),/chengdu-12/);await download.cancel();
  await page.setViewportSize({width:390,height:844});await page.waitForTimeout(100);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  const staticPage=await browser.newPage({javaScriptEnabled:false});await staticPage.goto(pathToFileURL(path.resolve(__dirname,'../web/reconstruction.html')).href);assert.equal(await staticPage.locator('#fallback-map').isVisible(),true);assert.equal(await staticPage.locator('noscript details').count(),20);await staticPage.close();
  assert.deepEqual(errors,[]);console.log('PASS: 20 sparse road-only results, explicit failures, actual controls, restored 13, review preview 03, no raw/snap fallback, fingerprints/no dedup, playback and modes, mobile, no-script');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
