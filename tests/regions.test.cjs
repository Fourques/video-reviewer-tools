const {test}=require('node:test'),assert=require('node:assert/strict');const {matchRegion,containRect}=require('../regions.js');
const row={did:'cam1',community_id:'site1',width:640,height:360,polygon:[[0,0,100,0,100,100]],video_path:'/other/kamicare_site1_cam1_event.mp4'};
test('cross-machine filename and camera match; no wrong camera/site',()=>{assert.equal(matchRegion([row],{name:'kamicare_site1_cam1_event.mp4'}).row,row);assert.equal(matchRegion([row],{name:'kamicare_site1_cam1_other.mp4'}).row,row);for(const name of ['kamicare_site2_cam1_other.mp4','kamicare_site1_cam10_other.mp4'])assert.ok(!matchRegion([row],{name}).row);});
test('ambiguous regions not guessed',()=>{assert.ok(!matchRegion([row,{...row,video_path:'/other.mp4',polygon:[[0,0,50,0,50,50]]}],{name:'kamicare_site1_cam1_new.mp4'}).row);});
test('letterbox alignment',()=>{assert.deepEqual(containRect({width:800,height:600},640,360),{left:0,top:75,width:800,height:450});});
test('renamed video identity supports camera match and flags community difference',()=>{
 const v={name:'真实_无__event-1.mp4',sourceIdentity:{did:'cam1',community_id:'site1',event_id:'event-1'}};
 assert.equal(matchRegion([row],v).row,row);
 assert.ok(!matchRegion([row],{...v,sourceIdentity:{...v.sourceIdentity,conflict:true}}).row);
 assert.ok(!matchRegion([row],{...v,sourceIdentity:{did:'missing',community_id:'site1'}}).row);
 const changed=matchRegion([row],{...v,sourceIdentity:{did:'cam1',community_id:'old-site'}});
 assert.equal(changed.row,row);assert.ok(changed.warning);
});
