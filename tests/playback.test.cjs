const {test} = require('node:test');
const assert = require('node:assert/strict');
const {ReviewPlayback, migrateSeekPreference} = require('../playback.js');
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
  controller.set('rate',16);
  const next=new ReviewPlayback(new Player(),{storage});
  assert.deepEqual(next.settings,{rate:1.25,loop:false,autoplay:false,muted:false});
  next.reset();assert.deepEqual(next.settings,{rate:1.5,loop:true,autoplay:true,muted:true});
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
