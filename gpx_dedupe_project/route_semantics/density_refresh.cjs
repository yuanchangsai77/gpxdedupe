/* Recompute all thresholds with the same policy as the viewer. */
const fs=require('fs'),{select}=require('./density_policy.js');
const [htmlPath,output]=process.argv.slice(2);if(!htmlPath||!output)throw Error('usage: node density_refresh.cjs full.html policy.json');
const html=fs.readFileSync(htmlPath,'utf8'),data=JSON.parse(html.split('<script type="application/json" id="data">')[1].split('</script>')[0]);
const cells=new Map(data.cells.filter(c=>c[0]===5000).map(c=>[`${c[1]},${c[2]}`,c[3]])),thresholds={};let records;
for(const t of [2,5,10,20,40,80]){const result=select(data.routes,cells,t,.2);const {decisions,...counts}=result;thresholds[t]=counts;if(t===10)records=[...decisions].map(([id,d])=>({id,...d}));}
fs.writeFileSync(output,JSON.stringify({policy:'density-preference-2',total:data.routes.length,threshold:10,thresholds,records}));console.log(JSON.stringify(thresholds));
