import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import openrouter_gateway as R
import sample as S
import dashboard as D

MODEL='openai/gpt-4o-mini'
CATALOG={MODEL:{'id':MODEL,'pricing':{'prompt':'0.000001','completion':'0.000002'},
                'context_length':100000,'supported_parameters':['max_tokens']}}
GOOD={'id':'gen-test','model':MODEL,'usage':{'cost':0.001,'prompt_tokens':20,'completion_tokens':40},
      'choices':[{'finish_reason':'stop','message':{'content':'AI Fruit is a tool.',
        'annotations':[{'type':'url_citation','url_citation':{'url':'https://aifruit.app','title':'AI Fruit'}}]}}]}

class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.cfg={'gateway':'openrouter','models':{'openai':MODEL},'max_output_tokens':100,
                  'monthly_budget_cny':300,'budget_cny_per_usd':8,'ledger_path':str(self.root/'ledger.json')}
        self.cp=self.root/'cfg.json';self.cp.write_text(json.dumps(self.cfg))
        self.env=patch.dict(os.environ,{'GEO_OPENROUTER_CONFIG':str(self.cp),'OPENROUTER_API_KEY':'test-secret'},clear=True);self.env.start()
        self.cat=patch.object(R,'_catalog',copy.deepcopy(CATALOG));self.cat.start()
        self.net=patch.object(R.requests,'post');self.post=self.net.start()
        self.post.return_value=Mock(status_code=200,json=lambda:copy.deepcopy(GOOD))
    def tearDown(self):
        self.net.stop();self.cat.stop();self.env.stop();self.temp.cleanup()
    def test_success_transport_accounting_citations(self):
        result=R.ask('openai','recommend a fruit tool')
        self.assertTrue(result['ok']);self.assertFalse(result['searched'])
        self.assertEqual(result['citations'][0]['url'],'https://aifruit.app')
        args,kwargs=self.post.call_args
        self.assertEqual(args[0],'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(kwargs['json']['max_tokens'],100)
        self.assertFalse(kwargs['json']['provider']['allow_fallbacks'])
        self.assertNotIn('plugins',kwargs['json'])
        ledger=json.loads((self.root/'ledger.json').read_text())
        self.assertEqual(ledger['requests'][0]['status'],'settled')
        self.assertEqual(ledger['requests'][0]['cost_cny'],'0.008')
        self.assertNotIn('test-secret',json.dumps(ledger))
    def test_missing_key_no_request(self):
        os.environ.pop('OPENROUTER_API_KEY')
        self.assertFalse(R.ask('openai','hi')['ok']);self.post.assert_not_called()
    def test_unknown_model_no_request(self):
        self.assertFalse(R.ask('claude','hi')['ok']);self.post.assert_not_called()
    def test_budget_reservation_prevents_overspend(self):
        self.cfg['monthly_budget_cny']=0.000001;self.cp.write_text(json.dumps(self.cfg))
        self.assertFalse(R.ask('openai','hi')['ok']);self.post.assert_not_called()
    def test_timeout_does_not_retry_and_blocks_future(self):
        self.post.side_effect=R.requests.exceptions.Timeout('test-secret')
        self.assertFalse(R.ask('openai','hi')['ok'])
        second=R.ask('openai','hi');self.assertFalse(second['ok'])
        self.assertEqual(self.post.call_count,1);self.assertNotIn('test-secret',json.dumps(second))
    def test_401_402_429_500_preserve_unknown_bill(self):
        for status in (401,402,429,500):
            with self.subTest(status=status):
                (self.root/'ledger.json').unlink(missing_ok=True)
                self.post.return_value=Mock(status_code=status)
                before=self.post.call_count
                self.assertFalse(R.ask('openai','hi')['ok'])
                self.assertEqual(self.post.call_count,before+1)
                self.assertEqual(json.loads((self.root/'ledger.json').read_text())['requests'][0]['status'],'unknown')
    def test_missing_cost_blocks_and_retains_generation_id(self):
        payload=copy.deepcopy(GOOD);payload['usage'].pop('cost')
        self.post.return_value.json=lambda:payload
        self.assertFalse(R.ask('openai','hi')['ok'])
        ledger=json.loads((self.root/'ledger.json').read_text())
        self.assertEqual(ledger['requests'][0]['generation_id'],'gen-test')
        self.assertEqual(ledger['requests'][0]['status'],'unknown')
    def test_truncation_and_empty_are_not_success(self):
        for finish,text in [('length','partial'),('stop',''),('content_filter','')]:
            payload=copy.deepcopy(GOOD);payload['choices'][0].update(finish_reason=finish,message={'content':text})
            self.post.return_value.json=lambda:payload
            self.assertFalse(R.ask('openai','hi')['ok'])
    def test_model_substitution_excluded(self):
        payload=copy.deepcopy(GOOD);payload['model']='another/model'
        self.post.return_value.json=lambda:payload
        self.assertFalse(R.ask('openai','hi')['ok'])
    def test_inflight_reservation_blocks_second_process(self):
        R.reserve(self.cfg,MODEL,R.decimal('.01'))
        self.assertFalse(R.ask('openai','hi')['ok']);self.post.assert_not_called()
    def test_ui_cannot_write_secret_or_model(self):
        with patch.object(D.G,'ROOT',self.root):
            with self.assertRaises(PermissionError):D.write_env({'OPENROUTER_API_KEY':'not-written'})
            self.assertFalse((self.root/'.env').exists())
    def test_pin_ignores_old_provider_env(self):
        os.environ['OPENAI_MODEL']='unapproved-model'
        self.assertEqual(R.model_for('openai'),MODEL)
    def test_no_implicit_family_fallback(self):
        self.assertEqual(R.model_for('openai'),MODEL);self.assertIsNone(R.model_for('claude'))
    def test_distinct_models_and_methods_do_not_dedup(self):
        row={'platform':'openai','question_id':'q','round':1,'sample_mode':'api','requested_model':MODEL,'method_version':'v1'}
        self.assertEqual(len(S.dedup_rows([row,{**row,'requested_model':'other/model'},{**row,'method_version':'v2'}])),3)
