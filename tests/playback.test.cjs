const {test} = require('node:test');
const assert = require('node:assert/strict');
const {ReviewPlayback, migrateSeekPreference, applyEffectiveRate, stopEffectiveRate, maxPlaybackRate} = require('../playback.js');
class Player extends EventTarget {
  constructor(){super();this.paused=true;this.readyState=0;this.ended=false;this.muted=false;this.plays=0;}
  emit(name){this.dispatchEvent(new Event(name));}
  load(){this.readyState=0;this.paused=true;this.playbackRate=1;}
  metadata(){this.readyState=1;this.emit('loadedmetadata');}
  ready(){this.readyState=3;this.emit('canplay');}
  play(){this.plays++;this.paused=false;this.emit('play');return Promise.resolve();}
  pause(){this.paused=true;this.emit('pause');}
  removeAttribute(name){delete this[name];}
}
// Browsers throw NotSupportedError past their own ceiling (16x in Chromium/Firefox).
class CappedPlayer extends Player {
  get playbackRate(){return this._rate ?? 1;}
  set playbackRate(value){
    if(value>16)throw Object.assign(new Error('rate'),{name:'NotSupportedError'});
    this._rate=value;
  }
}
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
// A real element plays itself at its native rate; the correction is what has to make
// up the difference, so the fake has to move too or the numbers mean nothing.
function playsItself(player,rate){
  let last=Date.now();
  const timer=setInterval(()=>{
    const now=Date.now();const elapsed=(now-last)/1000;last=now;
    if(!player.paused&&!player.seeking)player.currentTime+=rate*elapsed;
  },20);
  return ()=>clearInterval(timer);
}
function setup(saved){
  let value=saved===undefined?null:JSON.stringify(saved);
  const storage={getItem:()=>value,setItem:(_,v)=>value=v};
  const player=new Player();const blocked=[];
  const controller=new ReviewPlayback(player,{storage,onBlocked:e=>blocked.push(e.name)});
  return {player,controller,storage,blocked};
}
test('default autoplay is muted 1.5x full-video loop; every load restores speed',async()=>{
  const {player,controller}=setup();
  controller.load('first');player.metadata();player.ready();await Promise.resolve();
  assert.equal(player.playbackRate,1.5);assert.equal(player.defaultPlaybackRate,1.5);
  assert.equal(player.loop,true);assert.equal(player.muted,true);assert.equal(player.paused,false);
  controller.set('rate',2);controller.clear();controller.load('next');
  player.playbackRate=1;player.metadata();player.ready();await Promise.resolve();
  assert.equal(player.playbackRate,2);assert.equal(player.paused,false);assert.equal(player.plays,2);
});
test('preferences survive a new controller and invalid rates are ignored',()=>{
  const {controller,storage}=setup();
  for(const [key,value] of Object.entries({rate:1.25,loop:false,autoplay:false,muted:false}))controller.set(key,value);
  controller.set('rate',5);
  const next=new ReviewPlayback(new Player(),{storage});
  assert.deepEqual(next.settings,{rate:1.25,loop:false,autoplay:false,muted:false});
  next.reset();assert.deepEqual(next.settings,{rate:1.5,loop:true,autoplay:true,muted:true});
});
// The fake players below play at the browser ceiling; whatever the clip covers beyond
// that is the correction's doing, which is what these tests are about.
test('rates above the browser ceiling really cover that many seconds per second',async()=>{
  const player=new CappedPlayer();
  player.duration=600;player.currentTime=0;player.readyState=3;player.paused=false;
  const stop=playsItself(player,16);
  try{
    assert.equal(maxPlaybackRate(player),16);
    assert.deepEqual(applyEffectiveRate(player,20),{native:16,extra:4});
    assert.equal(player.playbackRate,16);assert.equal(player.defaultPlaybackRate,16);
    // the clip must sit at 20 x wall-clock even though the ceiling only plays 16 x
    player.currentTime=0;
    const started=Date.now();
    await sleep(900);
    const wall=(Date.now()-started)/1000;
    const covered=player.currentTime/wall;
    assert.ok(Math.abs(covered-20)<=0.9,
      `${wall.toFixed(2)}s of wall clock covered ${player.currentTime.toFixed(2)}s = ${covered.toFixed(2)}x`);
    // a pause stops the correction with the clip
    player.paused=true;
    await sleep(300);
    assert.ok(Math.abs(player.currentTime-20*wall)<=1.5,'a paused clip must not be pulled forward');
  }finally{stop();stopEffectiveRate(player);}
});
test('a rate the browser can play needs no skipping',async()=>{
  const player=new CappedPlayer();
  player.duration=600;player.currentTime=0;player.readyState=3;player.paused=false;
  const stop=playsItself(player,8);
  try{
    assert.deepEqual(applyEffectiveRate(player,16),{native:16,extra:0});
    assert.deepEqual(applyEffectiveRate(player,8),{native:8,extra:0});
    player.currentTime=0;
    const started=Date.now();
    await sleep(600);
    const covered=player.currentTime/((Date.now()-started)/1000);
    assert.ok(Math.abs(covered-8)<=0.9,`a rate the browser can play must stay untouched, got ${covered.toFixed(2)}x`);
  }finally{stop();stopEffectiveRate(player);}
});
test('re-applying the same speed keeps the correction in place',async()=>{
  const player=new CappedPlayer();
  player.duration=600;player.currentTime=0;player.readyState=3;player.paused=false;
  const stop=playsItself(player,16);
  try{
    // canplay fires again and again while a clip plays, and each one re-applies the
    // speed: starting the correction over there drops every second already made up,
    // which is how a browser-capped 20x quietly turns back into 16x.
    player.currentTime=0;
    const started=Date.now();
    for(let i=0;i<12;i++){applyEffectiveRate(player,20);await sleep(80);}
    const covered=player.currentTime/((Date.now()-started)/1000);
    assert.ok(covered>=18.5,`re-applying 20x twelve times covered only ${covered.toFixed(2)}x`);
    // picking a different speed swaps the correction instead of stacking it
    applyEffectiveRate(player,12);
    player.currentTime=0;
    const switched=Date.now();
    await sleep(500);
    const left=player.currentTime/((Date.now()-switched)/1000);
    assert.ok(left<=17,`12x must not keep the 20x pull in place, got ${left.toFixed(2)}x`);
  }finally{stop();stopEffectiveRate(player);}
});
test('a seek at a browser-capped speed is not pulled back by the correction',async()=>{
  const player=new CappedPlayer();
  player.duration=600;player.currentTime=0;player.readyState=0;player.paused=true;
  const stop=playsItself(player,16);
  const controller=new ReviewPlayback(player,{storage:{getItem:()=>null,setItem:()=>{}}});
  try{
    controller.set('rate',20);controller.load('first');player.metadata();player.ready();await Promise.resolve();
    player.paused=false;
    await sleep(700);                       // the clip runs ahead of where it started
    const before=player.currentTime;
    controller.seekBy(3-before,600);        // the user fast-forwards backwards
    assert.equal(player.currentTime,3);
    await sleep(400);
    // 20x means it keeps going from 3, not back to where the deadline expected it
    assert.ok(player.currentTime<=3+20*0.4+1,`the seek was undone: ${before.toFixed(2)} -> 3 -> ${player.currentTime.toFixed(2)}`);
  }finally{stop();stopEffectiveRate(player);}
});
test('a saved 20x preference loads on a capped browser without throwing',async()=>{
  const player=new CappedPlayer();
  const controller=new ReviewPlayback(player,{storage:{getItem:()=>JSON.stringify({rate:20}),setItem:()=>{}}});
  try{
    assert.equal(controller.settings.rate,20);assert.equal(player.playbackRate,16);
    controller.load('first');player.metadata();player.ready();await Promise.resolve();
    assert.equal(player.playbackRate,16);assert.equal(player.paused,false);
    controller.clear();
    const held=player.currentTime;
    await sleep(300);
    assert.equal(player.currentTime,held);
  }finally{stopEffectiveRate(player);}
});
test('a deliberate pause keeps following clips stopped until play resumes',async()=>{
  const {controller,player}=setup();controller.load('first');player.metadata();player.ready();await Promise.resolve();
  controller.toggle();player.ready();assert.equal(player.paused,true);
  controller.load('next');player.metadata();player.ready();await Promise.resolve();
  assert.equal(player.paused,true);assert.equal(controller.wantPlay,false);
  controller.toggle();await Promise.resolve();assert.equal(player.paused,false);
  controller.load('third');player.metadata();player.ready();await Promise.resolve();
  assert.equal(player.paused,false);
  controller.set('autoplay',false);controller.load('fourth');player.metadata();player.ready();assert.equal(player.paused,true);
});
test('pausing from the player controls also stops following clips',async()=>{
  const {controller,player}=setup();controller.load('first');player.metadata();player.ready();await Promise.resolve();
  player.pause();
  controller.load('next');player.metadata();player.ready();await Promise.resolve();
  assert.equal(player.paused,true);
});
test('the loop setting is independent of play and pause',async()=>{
  const {controller,player}=setup();controller.load('first');player.metadata();player.ready();await Promise.resolve();
  controller.set('loop',false);
  assert.equal(player.paused,false);assert.equal(player.loop,false);
  assert.equal(controller.userStopped,false);
  controller.load('next');player.metadata();player.ready();await Promise.resolve();
  assert.equal(player.paused,false);assert.equal(controller.status().userStopped,false);
  controller.set('loop',true);controller.load('third');player.metadata();player.ready();await Promise.resolve();
  assert.equal(player.loop,true);assert.equal(player.paused,false);
});
test('a keyboard fast-forward that lands on the end is not a deliberate stop',async()=>{
  const {controller,player}=setup();controller.load('first');player.metadata();player.ready();await Promise.resolve();
  player.duration=4;player.currentTime=0;controller.seekBy(100,4);
  assert.equal(player.currentTime,4);
  player.seeking=false;player.pause();
  assert.equal(controller.userStopped,false);
  controller.load('next');player.metadata();player.ready();await Promise.resolve();
  assert.equal(player.paused,false);
});
test('seeking a clip that already ran to its end keeps playing',async()=>{
  const {controller,player}=setup();controller.load('first');player.metadata();player.ready();await Promise.resolve();
  player.duration=4;player.currentTime=4;player.ended=true;player.pause();
  assert.equal(controller.userStopped,false);assert.equal(player.paused,true);
  const plays=player.plays;
  controller.seekBy(-2,4);
  assert.equal(player.currentTime,2);assert.equal(player.paused,false);assert.equal(player.plays,plays+1);
});
test('the interface is told once when playback stops and when it resumes',()=>{
  const player=new Player(),seen=[];
  const controller=new ReviewPlayback(player,{storage:{getItem:()=>null,setItem:()=>{}},onChange:(_,status)=>seen.push(status.userStopped)});
  assert.equal(seen.at(-1),false);
  controller.pause();assert.equal(seen.at(-1),true);
  controller.pause();assert.equal(seen.filter(value=>value).length,1);
  controller.play();assert.equal(seen.at(-1),false);
  controller.load('next');assert.equal(seen.at(-1),false);
});
test('switching clips without stopping still follows the autoplay preference',async()=>{
  const {controller,player}=setup();controller.load('first');player.metadata();player.ready();await Promise.resolve();
  controller.load('next');player.metadata();player.ready();await Promise.resolve();assert.equal(player.paused,false);
  controller.set('autoplay',false);controller.load('third');player.metadata();player.ready();assert.equal(player.paused,true);
});
test('S can cancel autoplay while a clip is buffering',()=>{
  const {controller,player}=setup();controller.load('first');controller.toggle();player.metadata();player.ready();
  assert.equal(player.paused,true);assert.equal(player.plays,0);
});
test('old rejected play promises cannot stop a new clip',async()=>{
  const {controller,player,blocked}=setup();let reject;
  player.play=()=>new Promise((_,r)=>reject=r);controller.load('first');player.metadata();player.ready();
  controller.clear();controller.load('next');reject(Object.assign(new Error(),{name:'NotAllowedError'}));await Promise.resolve();
  assert.equal(controller.wantPlay,true);assert.deepEqual(blocked,[]);
});
test('blocked autoplay shows a recoverable prompt',async()=>{
  const {controller,player,blocked}=setup();
  player.play=()=>Promise.reject(Object.assign(new Error(),{name:'NotAllowedError'}));
  controller.load('first');player.metadata();player.ready();await Promise.resolve();
  assert.deepEqual(blocked,['NotAllowedError']);assert.equal(controller.wantPlay,false);
  player.play=Player.prototype.play;controller.toggle();await Promise.resolve();assert.equal(player.paused,false);
});
test('default seek migration preserves nondefault and new explicitly chosen values',()=>{
  assert.equal(migrateSeekPreference({}),1);assert.equal(migrateSeekPreference({seekSeconds:5}),1);
  assert.equal(migrateSeekPreference({seekSeconds:3}),3);
  assert.equal(migrateSeekPreference({seekSeconds:5,seekDefaultVersion:2}),5);
  assert.equal(migrateSeekPreference({seekSeconds:'bad'}),1);
});
test('repeated seeks are coalesced and never interrupt a pending seek',()=>{
  const {controller,player}=setup();player.currentTime=2;player.seeking=true;
  controller.seekBy(1,8);assert.equal(player.currentTime,2);
  controller.seekBy(1,8);controller.seekBy(-1,8);assert.equal(player.currentTime,2);
  player.seeking=false;player.emit('seeked');assert.equal(player.currentTime,3);
  controller.seekBy(100,8);assert.equal(player.currentTime,8);
  controller.seekBy(-100,8);assert.equal(player.currentTime,0);
  controller.load('next');assert.equal(controller.seekTarget,null);
});
