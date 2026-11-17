import unittest
from camera_regions import validate_regions
class RegionsTest(unittest.TestCase):
    def setUp(self):
        self.row={'did':'cam1','width':640,'height':360,'polygon':[[89,3,119,153,238,124,218,1]],'event_id':'sample'}
    def test_valid(self):
        self.assertEqual(validate_regions([self.row]),[self.row])
    def test_invalid(self):
        for patch in ({'did':''},{'width':0},{'height':True},{'polygon':[[0,0,1,1,641,1]]},{'polygon':[[0,0,1,1,float('nan'),1]]},{'polygon':[[1,2,3]]}):
            with self.assertRaises(ValueError):validate_regions([dict(self.row,**patch)])

    def test_persist_regions_separately_and_restore(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from project_app import ProjectApp
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch('project_app.user_cache_dir',return_value=root/'cache'):
                app=ProjectApp(root,{'preset':'custom','cameraRegions':[self.row]})
                self.assertEqual(app.store.read_json(app.regions_path),[self.row])
                self.assertEqual(app.state['assets'],{})
                app.close()
                restored=ProjectApp(root)
                self.assertEqual(restored.store.read_json(restored.regions_path),[self.row])
                restored.close()
