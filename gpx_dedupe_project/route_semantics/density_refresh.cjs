/* Final display policy shared with the viewer. No routing or source writes. */
const fs=require('fs'),{select}=require('./density_policy.js');
const [htmlPath,output,...rest]=process.argv.slice(2);if(!htmlPath||!output)throw Error('usage: node density_refresh.cjs full.html policy.json [--threshold 10 --low-fraction .2 --minimum-meters 500]');
const options={'--threshold':10,'--low-fraction':.2,'--minimum-meters':500};
for(let i=0;i<rest.length;i+=2){if(!(rest[i] in options)||rest[i+1]===undefined)throw Error('invalid option');options[rest[i]]=Number(rest[i+1]);}
const threshold=options['--threshold'],fraction=options['--low-fraction'],minimum=options['--minimum-meters'];
if(!Number.isFinite(threshold)||threshold<0||!Number.isFinite(fraction)||fraction<0||fraction>1||!Number.isFinite(minimum)||minimum<=0)throw Error('invalid policy limits');
const html=fs.readFileSync(htmlPath,'utf8'),data=JSON.parse(html.split('<script type="application/json" id="data">')[1].split('</script>')[0]);
const cells=new Map(data.cells.filter(c=>c[0]===5000).map(c=>[`${c[1]},${c[2]}`,c[3]])),thresholds={};let records;
for(const t of new Set([2,5,10,20,40,80,threshold])){const result=select(data.routes,cells,t,fraction,minimum);const {decisions,...counts}=result;thresholds[t]=counts;if(t===threshold)records=[...decisions].map(([id,d])=>({id,...d}));}
fs.writeFileSync(output,JSON.stringify({policy:'density-preference-2',total:data.routes.length,threshold,min_low_fraction:fraction,min_low_m:minimum,thresholds,records}));console.log(JSON.stringify(thresholds));
