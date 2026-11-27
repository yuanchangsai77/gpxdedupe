const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.BROWSER_PATH,headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.COVERAGE_URL || 'http://localhost:8768/',{waitUntil:'domcontentloaded',timeout:120000});
  await page.waitForFunction(()=>Number.parseInt(document.querySelector('#count').textContent)>0);
  if(await page.locator('#mode').inputValue()!=='density')throw Error('default density mode');
  if(await page.evaluate(()=>Boolean(data.overlap))){
   if(await page.locator('#overlapMode').inputValue()!=='compact'||await page.locator('#overlapFraction').inputValue()!=='0.5')throw Error('default compact policy');
   const baseline=await page.evaluate(()=>{const r=DensityPolicy.select(data.routes,densityCells,10);return {original:r.original,rebuilt:r.rebuilt}});
   const stats=await page.locator('#densityStats').textContent();
   if(!stats.includes(`原始 ${baseline.original}`)||!stats.includes('热门精简'))throw Error('original changed or compact explanation missing');
   if(await page.evaluate(()=>data.routes.length===42130)&&!stats.includes('重建 2805'))throw Error('unexpected compact count');
   const kept=await page.evaluate(()=>[...densityDecisions.values()].filter(d=>d.keep&&d.kind==='rebuilt').length);
   const invalid=await page.evaluate(()=>[...densityDecisions.values()].filter(d=>d.keep&&d.coverage&&!d.coverage.protected_reason&&d.coverage.unique_fraction+1e-9<.5&&!d.reason.includes('高密度区域覆盖')).length);
   if(invalid)throw Error('compact percentage bypassed');
   const missed=await page.evaluate(()=>[...densityDecisions.values()].filter(d=>!d.keep&&d.coverage&&d.coverage.unique_fraction+1e-9>=.5).length);if(missed)throw Error('eligible hidden routes remain');
   const hidden=await page.evaluate(()=>[...densityDecisions].find(([id,d])=>!d.keep&&d.coverage?.covered_by?.length)?.[0]);
   if(hidden){await page.selectOption('#route',String(hidden));if(!(await page.locator('#rows').textContent()).includes('全体重建下独有'))throw Error('missing overlap explanation');if(await page.locator('#rows button').count()===0)throw Error('missing covering route links');await page.locator('#rows button').first().click();await page.selectOption('#route','');}
   await page.selectOption('#overlapFraction','0.8');
   const strictKept=await page.evaluate(()=>[...densityDecisions.values()].filter(d=>d.keep&&d.kind==='rebuilt').length);
   if(strictKept>kept)throw Error('80% retained more routes on fixture');
   await page.selectOption('#overlapMode','coverage');await page.selectOption('#overlapFraction','0.1');
   if(await page.evaluate(()=>data.routes.length===42130)&&!(await page.locator('#densityStats').textContent()).includes('重建 4553'))throw Error('legacy policy differs');
   await page.selectOption('#overlapMode','compact');await page.selectOption('#overlapFraction','0.5');
   if((await page.locator('#densityStats').textContent())!==stats)throw Error('mode switch or cached policy differs');
   await page.locator('#overlapEnabled').uncheck();await page.locator('#dedup').uncheck();
   const unfiltered=await page.locator('#densityStats').textContent();if(!unfiltered.includes(`重建 ${baseline.rebuilt}`))throw Error('toggle did not restore rebuilt routes');
   await page.locator('#overlapEnabled').check();
  }
  const before=await page.locator('#densityStats').textContent();await page.locator('#threshold').fill('40');await page.locator('#threshold').dispatchEvent('change');if((await page.locator('#densityStats').textContent())===before)throw Error('threshold did not update');await page.locator('#threshold').fill('10');await page.locator('#threshold').dispatchEvent('change');
  if(await page.evaluate(()=>data.routes.length===42130)&&await page.evaluate(()=>data.routes.find(r=>r.id===22503)?.rebuilt?.flat().length)!==322)throw Error('rebuilt geometry was replaced by coarse overview');
  await page.locator('#base').uncheck();
  const count=Number.parseInt(await page.locator('#count').textContent());if(await page.locator('#route option').count()!==count+1)throw Error('route count');
  await page.selectOption('#route','1');await page.selectOption('#mode','mixed');await page.selectOption('#mode','representatives');await page.selectOption('#route','');await page.selectOption('#mode','density');await page.selectOption('#scale','5000');await page.locator('#city').click();
  await page.waitForTimeout(300);if(errors.length)throw Error(errors.join(';'));
  if(process.env.COVERAGE_SCREENSHOT){await page.screenshot({path:process.env.COVERAGE_SCREENSHOT,fullPage:true});}
  console.log('PASS coverage viewer: compact 50%, 80% comparison, legacy mode, original preserved, covering links, toggles, full geometry, no JS errors');
  console.log(await page.locator('#densityStats').textContent());
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
