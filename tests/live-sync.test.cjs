const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../workbench.js'),'utf8');
const code=source.split('// LIVE_MERGE_BEGIN')[1].split('\n').slice(1).join('\n').split('// LIVE_MERGE_END')[0];
const context=vm.createContext({});vm.runInContext(code,context);
const merge=context.mergeLiveAnnotations;
const current={id:'a',annotation:{revision:0,label:null},label:null};
const videos=[current,{id:'b',annotation:{revision:5,label:'human'},label:'human'}];
assert.equal(merge(videos,[{id:'a',annotation:{revision:1,label:'fall'}}]),true);
assert.equal(videos[0],current,'Live updates replaced the playing video object');
assert.equal(current.annotation.label,'fall');
assert.equal(current.label,'fall');
assert.equal(merge(videos,[{id:'a',annotation:{revision:1,label:'no_fall'}}]),false);
assert.equal(merge(videos,[{id:'b',annotation:{revision:3,label:'stale'}}]),false);
assert.equal(videos[1].annotation.label,'human','Newer manual work was overwritten');
assert.equal(merge(videos,[{id:'a',annotation:{revision:2,label:'no_fall'}}],new Set(['a'])),false);
assert.equal(current.annotation.label,'fall');
assert.equal(merge(videos,[{id:'missing',annotation:{revision:1}}]),false);
assert(!code.includes('deck.select'),'Annotation deltas must not reload playback');
console.log('Live annotation merge tests passed');

const resumeCode=source.split('// WORKSPACE_RESUME_BEGIN')[1].split('\n').slice(1).join('\n').split('// WORKSPACE_RESUME_END')[0];
const resumeContext=vm.createContext({});vm.runInContext(resumeCode,resumeContext);
const project={config:{id:'p',labels:[{id:'no_fall'},{id:'fall'}]},round:1,videos:[{id:'a'},{id:'b'}]};
const bookmark={projectId:'p',round:1,id:'b',updatedAt:100,filters:{search:'',label:'no_fall',status:'all',sort:'filename'}};
project.workspace=bookmark;
assert.equal(resumeContext.chooseWorkspace(project,null).id,'b','Server bookmark was not restored on another browser');
assert.equal(resumeContext.chooseWorkspace(project,{...bookmark,id:'a',updatedAt:200}).id,'a','Newer offline local bookmark was lost');
assert.equal(resumeContext.chooseWorkspace(project,{...bookmark,id:'a',updatedAt:50}).id,'b','Stale browser state replaced newer server state');
for(const invalid of [{...bookmark,round:2},{...bookmark,projectId:'other'},{...bookmark,id:'missing'},{...bookmark,filters:{...bookmark.filters,label:'removed'}},{...bookmark,filters:{...bookmark.filters,status:'invalid'}}]){
 assert.equal(resumeContext.chooseWorkspace({...project,workspace:null},invalid),null);
}
assert(source.includes('rememberWorkspace(); // Save locally before'));
assert(source.includes('saveBrowseRemote(state.workspace,true)'),'Closing page must flush its last position');
console.log('Workspace resume tests passed: filters, project isolation, new round, offline and cross-browser restore');
