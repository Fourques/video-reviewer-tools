import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from source_identity import identity,read_source_identities
EVENT='05e6d777-4678-45a9-8a35-5afa98ffd816'
SITE='ed6b935f-a92f-4f37-b75a-8c8c0ecdbfed'
class SourceIdentityTests(unittest.TestCase):
 def test_direct_name(self):
  self.assertEqual(identity('Z:\\videos\\kamicare_'+SITE+'_CAM1_event_'+EVENT+'.mp4')['did'],'CAM1')
 def test_parent_manifest(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'02'/'errors';source.mkdir(parents=True);name='真实_无__'+EVENT+'.mp4'
   row={'视频相对路径':'02/errors/'+name,'原视频路径':'Z:/kamicare_'+SITE+'_CAM1_alarm_'+EVENT+'.mp4','事件ID':EVENT,'人工标签':'进'}
   (root/'summary.json').write_text(json.dumps({'manifest':[row]}))
   found,warnings=read_source_identities(source,[SimpleNamespace(name=name)])
   self.assertEqual(found[name]['did'],'CAM1');self.assertNotIn('人工标签',found[name]);self.assertEqual(warnings,[])
 def test_conflict(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);name='renamed_'+EVENT+'.mp4'
   (root/'summary.json').write_text(json.dumps({'manifest':[{'event_id':EVENT,'did':'CAM1'},{'event_id':EVENT,'did':'CAM2'}]}))
   found,_=read_source_identities(root,[SimpleNamespace(name=name)]);self.assertTrue(found[name]['conflict'])
 def test_csv_missing(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);name='rename_'+EVENT+'.mp4'
   self.assertEqual(read_source_identities(root,[SimpleNamespace(name=name)])[0],{})
   (root/'视频清单.csv').write_text('事件ID,did,community_id\n'+EVENT+',CAM1,'+SITE+'\n',encoding='utf-8-sig')
   self.assertEqual(read_source_identities(root,[SimpleNamespace(name=name)])[0][name]['did'],'CAM1')
 def test_public_video_exposes_device_without_changing_annotation(self):
  from project_app import ProjectApp
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);name='真实_无__'+EVENT+'.mp4';(root/name).write_bytes(b'fakevideo')
   (root/'summary.json').write_text(json.dumps({'manifest':[{'event_id':EVENT,'did':'CAM1','community_id':SITE}]}))
   with patch('project_app.user_cache_dir',return_value=root/'cache'):
    app=ProjectApp(root,{'preset':'bathroom'})
    try:
     video=app.public_videos()[0];self.assertEqual(video['metadataDeviceId'],'CAM1');self.assertEqual(video['sourceIdentity']['community_id'],SITE);self.assertIsNone(video['annotation']['label'])
    finally:app.close()
