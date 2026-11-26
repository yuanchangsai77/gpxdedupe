const assert=require('node:assert/strict'),{apply}=require('../route_semantics/rebuilt_dedup.js');
function route(id,rank,cell=1){return {id,dedup:{group:'same',rank},contributions:[[5000,cell,1,1000]]}}
function result(){return {original:0,rebuilt:2,hidden:0,decisions:new Map([[1,{keep:true,kind:'rebuilt'}],[2,{keep:true,kind:'rebuilt'}]])}}
let a=apply([route(1,0),route(2,1)],result(),new Map([['1,1',100]]),10);assert.equal(a.merged,1);assert.equal(a.decisions.get(2).duplicateOf,1);
a=apply([route(1,0),route(2,1,2)],result(),new Map([['1,1',100],['2,1',100]]),10);assert.equal(a.merged,0);assert.equal(a.dedupCoverageProtected,1);
let raw=result();raw.original=1;raw.rebuilt=1;raw.decisions.get(2).kind='original';a=apply([route(1,0),route(2,1)],raw,new Map([['1,1',100]]),10);assert.equal(a.merged,0);
console.log('PASS dedup display: equivalents hidden, raw retained, no high-density coverage loss');
