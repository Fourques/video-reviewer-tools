import tempfile
import unittest
from pathlib import Path
from rule_templates import RuleTemplates, portable_rules
from project_schema import preset
from project_app import ProjectApp
from unittest.mock import patch
class RuleTemplateTests(unittest.TestCase):
    def test_portability(self):
        rules=portable_rules({**preset('fall'),'inputs':['/private/videos'],'destinations':{'fall':'/private/out'},'output':'/private/out','metadataConfig':{'path':'/private.csv'},'cameraRegions':[1]})
        for field in ('id','name','inputs','destinations','output','metadataConfig','cameraRegions'):self.assertNotIn(field,rules)
        self.assertEqual(rules['labels'][0]['id'],'fall')
    def test_save_reload_replace_and_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'rules.json';store=RuleTemplates(path)
            first=store.save({'name':'跌倒分类','config':preset('fall'),'revision':0})
            self.assertEqual(RuleTemplates(path).read(),first)
            with self.assertRaises(ValueError):store.save({'name':'bad','config':preset('fall'),'revision':0})
            second=store.save({'name':'跌倒分类','config':preset('custom'),'revision':1})
            self.assertEqual(len(second['templates']),1)
            self.assertEqual(second['templates'][0]['config']['labels'][0]['id'],'label_1')
    def test_invalid_rule_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=RuleTemplates(Path(tmp)/'rules.json');cfg=preset('fall');cfg['labels'][1]['key']='j'
            with self.assertRaises(ValueError):store.save({'name':'duplicate','config':cfg,'revision':0})
            self.assertFalse(store.path.exists())
    def test_existing_project_keeps_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch('project_app.user_cache_dir',return_value=root/'cache'):
                app=ProjectApp(root,{'preset':'fall'});original=app.config;app.close()
                app=ProjectApp(root,{'preset':'custom','projectConfig':portable_rules(preset('custom'))})
                self.assertEqual(app.config,original);app.close()

    def test_bathroom_preset(self):
        from project_schema import validate
        config=validate(preset('bathroom'))
        self.assertEqual([l['id'] for l in config['labels']], ['bathroom_entry','bathroom_exit','no_bathroom_event'])
        self.assertEqual(config['exportMode'],'labels')
        self.assertFalse(config['intervals'])
        self.assertEqual(portable_rules(config)['negativeLabels'],['no_bathroom_event'])
