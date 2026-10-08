/* Optional real-browser integration. Requires Playwright and its browsers.
   Fixtures, output files and screenshots stay entirely in an OS temp directory. */
const {chromium}=require(process.env.REVIEWER_PLAYWRIGHT || 'playwright');
const {spawn,execFileSync}=require('node:child_process');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const assert=require('node:assert/strict');
const net=require('node:net');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'reviewer-v2-ui-'));
const source=path.join(root,'project');fs.mkdirSync(source);
execFileSync('ffmpeg',['-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=640x360:rate=16','-t','12','-c:v','libx264','-g','16','-pix_fmt','yuv420p',path.join(source,'01.mp4')]);
for(let i=2;i<=8;i++)fs.copyFileSync(path.join(source,'01.mp4'),path.join(source,`${String(i).padStart(2,'0')}.mp4`));
fs.writeFileSync(path.join(source,'index.csv'),'file,device_id,label,model\n'+Array.from({length:8},(_,i)=>`${String(i+1).padStart(2,'0')}.mp4,DEVICE-${i%2},unknown,positive`).join('\n'));
const wait=ms=>new Promise(r=>setTimeout(r,ms));
const freePort=()=>new Promise(resolve=>{const probe=net.createServer();probe.listen(0,'127.0.0.1',()=>{const port=probe.address().port;probe.close(()=>resolve(port));});});
(async()=>{
 const port=await freePort();const base=`http://127.0.0.1:${port}`;
 const service=spawn('python3',['start.py','--source',source,'--preset','fall','--port',String(port),'--no-browser'],{cwd:path.join(__dirname,'..'),env:{...process.env,VIDEO_REVIEWER_SETTINGS_FILE:path.join(root,'launcher-settings.json')}});
 let output='';service.stdout.on('data',data=>output+=data);service.stderr.on('data',data=>output+=data);
 let browser;
 try{
  for(let i=0;i<100;i++){try{if((await fetch(base+'/api/project')).ok)break}catch{}await wait(100);}
  browser=await chromium.launch({headless:true,channel:process.env.REVIEWER_CHROMIUM?undefined:'chromium',executablePath:process.env.REVIEWER_CHROMIUM||undefined,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1366,height:768}});const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base);await page.waitForFunction(()=>typeof deck!=='undefined'&&deck.readyToReview);
  assert.equal(await page.evaluate(()=>state.videos.length),8);
  // Large queues remain virtualized, even while the full project is fetched.
  await page.route('**/api/project',async route=>{const response=await route.fetch();const data=await response.json();const original=data.videos[0];data.videos.push(...Array.from({length:11992},(_,i)=>({...original,id:`synthetic-${i}`,name:`synthetic-${i}.mp4`,relative:`synthetic-${i}.mp4`})));await route.fulfill({response,json:data});});
  const largeBegan=Date.now();await page.reload();await page.waitForFunction(()=>state.videos.length===12000&&deck.readyToReview);
  assert.equal(await page.locator('#queueCount').textContent(),'12000 条');assert(await page.locator('.video-item').count()<30);
  await page.locator('#sort').selectOption('device');await page.locator('#search').fill('synthetic-11990.mp4');await page.waitForFunction(()=>document.getElementById('queueCount').textContent==='1 条');
  assert(await page.locator('.video-item.active').count(),'Current video disappeared outside filter');
  await page.unroute('**/api/project');await page.reload();await page.waitForFunction(()=>state.videos.length===8&&deck.readyToReview);
  console.log(JSON.stringify({largeQueueEntries:12000,largeQueueFlowMs:Date.now()-largeBegan}));
  await page.screenshot({path:path.join(root,'workspace.png')});
  for(const size of [{width:1920,height:1080},{width:1366,height:768},{width:1093,height:614},{width:1024,height:640}]){
   await page.setViewportSize(size);await wait(100);
   const result=await page.evaluate(()=>({width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,list:document.querySelector('#videoList').clientHeight,video:document.querySelector('#videoStage').clientHeight}));
   assert(result.width<=size.width,JSON.stringify({size,result}));assert(result.height<=size.height,JSON.stringify({size,result}));assert(result.list>300,JSON.stringify({size,result}));assert(result.video>120,JSON.stringify({size,result}));
  }
  await page.setViewportSize({width:1366,height:768});
  const first=await page.evaluate(()=>state.current.id);
  await page.keyboard.press('j');await page.waitForFunction(()=>state.pending.length===0&&deck.readyToReview);
  assert.notEqual(await page.evaluate(()=>state.current.id),first);
  await page.locator('#rate').selectOption('1.25');await page.keyboard.press('s');
  await page.keyboard.press('k');await page.waitForFunction(()=>state.pending.length===0&&deck.readyToReview);
  assert.equal(await page.evaluate(()=>deck.preferences.rate),1.25);assert.equal(await page.evaluate(()=>deck.wantPlay),false);
  await page.locator('#undo').click();await page.waitForFunction(()=>deck.readyToReview);
  assert.equal(await page.evaluate(()=>state.current.annotation.label),null);
  await page.locator('#settings').click();await page.locator('#settingsDialog').waitFor({state:'visible'});
  await page.locator('#newLabel').click();const row=page.locator('.label-edit-row').last();await row.locator('[data-field=name]').fill('通用新类别');await row.locator('[data-field=key]').press('m');
  await page.locator('#saveConfig').click();await page.locator('#settingsDialog').waitFor({state:'hidden'});await page.waitForFunction(()=>deck.readyToReview);
  assert(await page.locator('[data-label]').filter({hasText:'通用新类别'}).count());
  await page.keyboard.press('m');await page.waitForFunction(()=>state.pending.length===0&&deck.readyToReview);
  // Failure preserves an explicit local queue and recovers without losing work.
  await page.route('**/api/annotation',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'simulated write failure'})}));
  await page.keyboard.press('j');await page.waitForFunction(()=>state.failed);assert.equal(await page.evaluate(()=>state.pending.length),1);
  await page.unroute('**/api/annotation');await page.locator('#saveState').click();await page.locator('#retrySaves').click();await page.waitForFunction(()=>state.pending.length===0);
  await page.locator('#settings').click();await page.locator('[data-tab=workflow]').click();await page.locator('#configIntervals').check();await page.locator('#configSeconds').fill('4');await page.locator('#configSnap').uncheck();await page.locator('#configQuick').uncheck();await page.locator('#saveConfig').click();await page.locator('#settingsDialog').waitFor({state:'hidden'});await page.waitForFunction(()=>deck.readyToReview&&state.info?.duration>0);
  await page.keyboard.press('Space');await page.waitForFunction(()=>state.pending.length===0);
  assert.equal(await page.evaluate(()=>state.current.annotation.segments.length),1);
  await page.keyboard.press('Enter');await page.waitForFunction(()=>deck.readyToReview&&state.pending.length===0);
  await page.locator('#organize').click();await page.locator('#exportDialog').waitFor({state:'visible'});
  const [exportResponse]=await Promise.all([page.waitForResponse(response=>response.url().endsWith('/api/export')&&response.request().method()==='POST'),page.locator('#startExport').click()]);assert(exportResponse.ok());
  await page.waitForFunction(()=>!state.exportRunning,{timeout:30000});
  assert(fs.existsSync(path.join(source,'output','fall','01.mp4')));assert(fs.existsSync(path.join(source,'output','clips')));
  await page.locator('[data-close=exportDialog]').click();await page.screenshot({path:path.join(root,'intervals.png')});
  assert.deepEqual(errors,[]);
  await page.reload();await page.waitForFunction(()=>deck.readyToReview);assert.equal(await page.evaluate(()=>state.config.labels.length),4);
  // Show a slow CSV stage and exercise the skip control in the actual page.
  let skipped=false;await page.route('**/api/startup-status',route=>route.fulfill({json:{status:skipped?'ready':'scanning',stage:'匹配 CSV 字段',done:12000,total:null,message:'index.csv · CSV 行数',elapsedSeconds:25,idleSeconds:21,canSkipMetadata:!skipped}}));
  await page.route('**/api/startup-skip-metadata',route=>{skipped=true;return route.fulfill({json:{ok:true}});});
  await page.goto(base+'/projects');await page.locator('#skipCsv').waitFor({state:'visible'});assert.equal(await page.locator('#scanStage').textContent(),'匹配 CSV 字段');await page.locator('#skipCsv').click();await page.waitForFunction(()=>typeof deck!=='undefined'&&deck.readyToReview);
  await page.unroute('**/api/startup-status');await page.unroute('**/api/startup-skip-metadata');await page.locator('#switchProject').click();await page.waitForSelector('#start');
  await page.screenshot({path:path.join(root,'project-center.png')});
  await page.locator('#source').fill(source);await page.locator('#start').click();await page.waitForFunction(()=>typeof deck!=='undefined'&&deck.readyToReview);assert.equal(await page.evaluate(()=>state.config.labels.length),4);
  await page.waitForFunction(()=>globalThis.videoReviewerSessionReady?.());
  await wait(200);
  // CDP's closeTarget can terminate the renderer without pagehide/beacon in
  // headless-shell. Leave the document normally to exercise its real lifecycle;
  // owned app-window close is separately covered by native integration.
  await page.goto('about:blank');
  await page.close({runBeforeUnload:true});for(let i=0;i<200&&service.exitCode===null;i++)await wait(100);
  if(service.exitCode===null)console.error('Lifetime diagnostic:',await (await fetch(base+'/api/runtime')).json());
  assert.equal(service.exitCode,0,'Last tab close did not release service');
  console.log(JSON.stringify({status:'passed',screenshots:root,checks:'layouts, labels, playback state, undo, queue retry, segments, export, resume, project switch, shutdown'}));
 }catch(error){console.error(output.slice(-20000));throw error;}
 finally{if(browser)await browser.close();if(service.exitCode===null)service.kill('SIGTERM');fs.writeFileSync(path.join(root,'service.log'),output);}
})().catch(error=>{console.error(error);process.exitCode=1;});
