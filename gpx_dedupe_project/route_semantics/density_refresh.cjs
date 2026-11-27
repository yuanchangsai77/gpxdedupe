/* Final display policy shared with the viewer. No routing or source writes. */
const fs=require('fs'),{evaluate}=require('./final_policy.js');
const [htmlPath,output,...rest]=process.argv.slice(2);if(!htmlPath||!output)throw Error('usage: node density_refresh.cjs full.html policy.json [--threshold 10 --low-fraction .2 --minimum-meters 500 --overlap-mode off|compact|coverage --overlap-fraction .5]');
const options={'--threshold':10,'--low-fraction':.2,'--minimum-meters':500,'--overlap-mode':'off','--overlap-fraction':.5};
for(let i=0;i<rest.length;i+=2){if(!(rest[i] in options)||rest[i+1]===undefined)throw Error('invalid option');options[rest[i]]=rest[i]==='--overlap-mode'?rest[i+1]:Number(rest[i+1]);}
const threshold=options['--threshold'],fraction=options['--low-fraction'],minimum=options['--minimum-meters'],mode=options['--overlap-mode'],overlapFraction=options['--overlap-fraction'];
if(!Number.isFinite(threshold)||threshold<0||!Number.isFinite(fraction)||fraction<0||fraction>1||!Number.isFinite(minimum)||minimum<=0||!['off','compact','coverage'].includes(mode)||!Number.isFinite(overlapFraction)||overlapFraction<0||overlapFraction>1)throw Error('invalid policy limits');
const raw=fs.readFileSync(htmlPath,'utf8'),data=htmlPath.endsWith('.json')?JSON.parse(raw):JSON.parse(raw.split('<script type="application/json" id="data">')[1].split('</script>')[0]);
const thresholds={};let records,active;
for(const t of new Set([2,5,10,20,40,80,threshold])){const result=evaluate(data,{threshold:t,lowFraction:fraction,minimumMeters:minimum,overlapEnabled:mode!=='off',overlapMode:mode,overlapFraction,strictDedup:false});const {decisions,...counts}=result;thresholds[t]=counts;if(t===threshold){records=[...decisions].map(([id,d])=>({id,...d}));active=counts;}}
fs.writeFileSync(output,JSON.stringify({policy:mode==='off'?'density-preference-2':'route-final-policy-3',total:data.routes.length,threshold,min_low_fraction:fraction,min_low_m:minimum,overlap:active.overlapSettings,thresholds,records}));console.log(JSON.stringify(thresholds));
