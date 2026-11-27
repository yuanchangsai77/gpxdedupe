const assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.BROWSER_PATH});try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.route('https://**',r=>r.abort());
 await page.goto(pathToFileURL(process.argv[2]).href,{waitUntil:'domcontentloaded',timeout:120000});await page.waitForFunction(()=>document.querySelector('#counts').textContent.includes('最终 '));
 const counts=await page.evaluate(()=>({total:routes.length,rebuilt:routes.filter(r=>r.kind==='rebuilt').length,original:routes.filter(r=>r.kind==='original').length,geo:features.length}));
 assert.equal(counts.total,counts.geo);assert.equal(await page.locator('#route option').count(),counts.total+1);
 if(counts.total===3275){assert.equal(counts.rebuilt,2805);assert.equal(counts.original,470)}
 await page.selectOption('#kind','original');assert.equal(await page.locator('#route option').count(),counts.original+1);
 const id=await page.locator('#route option').nth(1).getAttribute('value');await page.selectOption('#route',id);assert.ok((await page.locator('#info').textContent()).includes('历史原始'));assert.equal(await page.evaluate(()=>selected.id),Number(id));
 await page.locator('#base').uncheck();await page.locator('#home').click();assert.equal(await page.evaluate(()=>selected),null);
 if(counts.rebuilt){await page.selectOption('#kind','rebuilt');assert.equal(await page.locator('#route option').count(),counts.rebuilt+1);await page.selectOption('#route',await page.locator('#route option').nth(1).getAttribute('value'));assert.ok((await page.locator('#info').textContent()).includes('可靠重建'))}
 const anchor=await page.evaluate(()=>[(cx+100)/2**(z-18),(cy-60)/2**(z-18)]);
 const b=await page.locator('#map').boundingBox();await page.mouse.move(b.x+b.width/2+100,b.y+b.height/2-60);await page.mouse.wheel(0,-120);await page.waitForTimeout(250);
 const after=await page.evaluate(()=>[(cx+100)/2**(z-18),(cy-60)/2**(z-18)]);assert.ok(Math.abs(anchor[0]-after[0])<1e-6&&Math.abs(anchor[1]-after[1])<1e-6);
 await page.selectOption('#kind','all');await page.locator('#city').click();await page.waitForTimeout(200);assert.deepEqual(errors,[]);
 console.log('PASS final viewer: exact final collection, geometry kinds, selection, bounds, mouse-centred zoom, offline tiles, no script errors',JSON.stringify(counts));
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});
