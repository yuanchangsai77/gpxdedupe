'use strict';
// Intercepted browser checks; no route data or tile requests leave this process.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const web = path.resolve(__dirname, '../web');
const original = [[116.404,39.915],[116.405,39.916],[116.406,39.915]];
(async()=>{
  const browser = await chromium.launch({headless:true,executablePath:process.env.BROWSER_PATH});
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000}});
    const page = await context.newPage();
    const errors=[], tiles=[], delayed=[];
    let generation=0,hold=false,failConfig=false;
    page.on('pageerror',e=>errors.push(e.message));
    await context.route('**/*',async route=>{
      const request=route.request(),url=new URL(request.url()),p=url.pathname;
      const json=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
      if(url.hostname.endsWith('.is.autonavi.com')){
        tiles.push(url);return route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64')});
      }
      if(p==='/api/config')return failConfig?json({error:'配置读取失败'},500):json({default_dir:'C:/Users/7281/Downloads/route',sample_dir:'D:/examples'});
      if(p==='/api/route-labels')return json({labels:[]});
      if(p==='/api/files'){
        generation++;
        return json({files:Array.from({length:6},(_,i)=>({id:`${generation}-${i}`,name:`路线${i}.gpx`,relative_path:`路线${i}.gpx`})),truncated:generation===3});
      }
      if(p==='/api/preview'){
        const id=request.postDataJSON().file_id;
        if(hold)await new Promise(resolve=>delayed.push(resolve));
        return id.endsWith('-5')?json({error:'无效轨迹'},400):json({original});
      }
      if(p==='/' || ['/app.js','/style.css'].includes(p)){
        const name=p==='/'?'index.html':p.slice(1);let body=fs.readFileSync(path.join(web,name),'utf8');
        if(name==='app.js')body=body.replace('  const labelGroups = [','  window.__overviewTest={overview,gpsToOverviewCoordinate,projectOverviewRoute,overviewBaseScale,zoomOverview};\n  const labelGroups = [');
        return route.fulfill({contentType:name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html',body});
      }
      return route.abort();
    });
    await page.goto('http://gpx-overview.test/');
    await page.waitForFunction(()=>document.querySelector('#overview-status').textContent.includes('1 条无法预览'));
    assert.equal(await page.locator('body').getAttribute('data-view'),'overview');
    assert.equal(await page.locator('#nav-overview').getAttribute('aria-current'),'page');
    assert.equal(await page.locator('.comparison-area').isVisible(),false);
    assert.equal(await page.locator('#folder').inputValue(),'C:/Users/7281/Downloads/route');
    assert(tiles.length>0);assert(tiles.every(u=>u.searchParams.get('style')==='7' && u.searchParams.get('z')<=18));
    const desktop=await page.locator('#overview-map').boundingBox();assert(desktop.height>=800);
    // Validate coordinate correction against a known Beijing GPS/GCJ pair.
    const converted=await page.evaluate(()=>window.__overviewTest.gpsToOverviewCoordinate([116.404,39.915]));
    assert(Math.abs(converted[0]-116.41024449916938)<1e-9);assert(Math.abs(converted[1]-39.91640428150164)<1e-9);
    await page.selectOption('#overview-crs','gcj02');
    assert.deepEqual(await page.evaluate(()=>window.__overviewTest.gpsToOverviewCoordinate([116.404,39.915])),[116.404,39.915]);
    await page.selectOption('#overview-crs','wgs84');
    // The same projected point must remain under the cursor after an actual wheel event.
    const mouse=[desktop.x+180,desktop.y+220];await page.mouse.move(...mouse);
    const pointAtMouse=()=>page.evaluate(()=>{
      const {overview:o,overviewBaseScale}=window.__overviewTest,c=document.querySelector('#overview-map');
      const scale=overviewBaseScale()*o.zoom;
      return [(o.bounds.minX+o.bounds.maxX)/2+(180-c.clientWidth/2-o.x)/scale,(o.bounds.minY+o.bounds.maxY)/2-(220-c.clientHeight/2-o.y)/scale];
    });
    const before=await pointAtMouse();await page.mouse.wheel(0,-120);const after=await pointAtMouse();
    before.forEach((n,i)=>assert(Math.abs(n-after[i])<1e-7));
    await page.evaluate(()=>window.__overviewTest.zoomOverview(1e-12));
    assert(Math.abs(await page.evaluate(()=>window.__overviewTest.overviewBaseScale()*window.__overviewTest.overview.zoom)-256/(2*Math.PI))<1e-8);
    await page.click('#overview-fit');
    await page.locator('.file-item').first().click();
    await page.waitForFunction(()=>document.body.dataset.view==='workbench');
    assert.equal(await page.locator('.comparison-area').isVisible(),true);
    assert.equal(await page.locator('#nav-workbench').getAttribute('aria-current'),'page');
    await page.click('#nav-overview');await page.waitForFunction(()=>document.body.dataset.view==='overview');
    // Switching sources invalidates all in-flight previews from the previous scan.
    hold=true;await page.click('#scan');await page.waitForFunction(()=>!document.querySelector('#scan').disabled);
    await page.waitForTimeout(100);assert.equal(delayed.length,4);
    hold=false;await page.click('#scan');await page.waitForFunction(()=>document.querySelector('#overview-status').textContent.includes('仅展示前 2000 条'));
    for(const resolve of delayed)resolve();
    await page.waitForFunction(()=>window.__overviewTest.overview.routes.length===5);
    await page.waitForTimeout(100);assert.equal(await page.evaluate(()=>window.__overviewTest.overview.routes.length),5);
    await page.setViewportSize({width:390,height:844});
    const mobile=await page.locator('#overview-map').boundingBox();assert(mobile.width<=390);assert(mobile.height>=633);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    failConfig=true;await page.reload();await page.waitForFunction(()=>document.querySelector('#overview-status').textContent==='配置读取失败');
    assert.deepEqual(errors,[]);
    console.log('PASS: overview navigation, tiles, coordinates, cursor zoom, national extent, failed previews, stale scans, responsive layout, config errors.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
