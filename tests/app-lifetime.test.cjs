const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync(require.resolve('../app.js'),'utf8');
test('late runtime response cannot open a connection after page close',async()=>{
 const events={},connections=[],beacons=[];let resolveRuntime;
 const runtime=new Promise(resolve=>resolveRuntime=resolve);
 vm.runInNewContext(source,{
  fetch:()=>Promise.resolve({json:()=>runtime}),
  addEventListener:(event,fn)=>events[event]=fn,
  crypto:{randomUUID:()=>String(connections.length)},
  EventSource:class{constructor(url){this.url=url;connections.push(this)}close(){this.closed=true}},
  navigator:{sendBeacon:(url)=>beacons.push(url)}
 });
 events.pagehide();resolveRuntime({autoClose:true});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(connections.length,0);
 events.pageshow();assert.equal(connections.length,1);
 events.pagehide();assert.equal(connections[0].closed,true);assert.equal(beacons.length,1);
 events.pageshow();assert.equal(connections.length,2);
});
