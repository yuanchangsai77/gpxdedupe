const assert=require('node:assert/strict'),{evaluate}=require('../route_semantics/final_policy.js');
const atoms={1:[0,1,2,3,4,5],2:[0,1,6,7],3:[2,3,8,9],4:[4,5]},routes=[];const index={weights:Array(10).fill(100),routes:{}};
for(const [id,list]of Object.entries(atoms)){index.routes[id]={id:+id,atoms:list,walk:list,physical_m:list.length*100,traveled_m:list.length*100,protect:null,quality:0};routes.push({id:+id,length:1000,decision:{kind:'rebuilt'},reliableRebuilt:true,rebuilt:[[[104,30],[104.01,30]]],contributions:[[5000,1,1,1000]]});}
routes.push({id:9,length:1000,decision:{kind:'original'},contributions:[[5000,2,2,1000]]});
const data={routes,cells:[[5000,1,1,100,4000],[5000,2,2,1,1000]],overlap:index};
const result=evaluate(data,{overlapMode:'compact',overlapFraction:.5});assert.equal(result.decisions.get(4).keep,true);assert.equal(result.decisions.get(9).kind,'original');assert.equal(result.original,1);assert.equal(result.rebuilt,3);
const off=evaluate(data,{overlapMode:'off'});assert.equal(off.rebuilt,4);assert.equal(off.original,1);
assert.throws(()=>evaluate({...data,overlap:null},{overlapMode:'compact'}));
console.log('PASS final policy: shared default evaluator, P1 repair, raw unchanged, explicit off and missing-index rejection');
