import copy,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import replicate_gateway as R
class ReplicateTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.cfg=json.loads((Path(__file__).parents[1]/'replicate.json').read_text());self.cfg['ledger_path']=str(self.root/'ledger.json');self.cfg['historical_ledgers']=[]
  p=self.root/'config.json';p.write_text(json.dumps(self.cfg));self.env=patch.dict(os.environ,{'GEO_REPLICATE_CONFIG':str(p),'REPLICATE_API_TOKEN':'private-test'},clear=True);self.env.start()
  self.postpatch=patch.object(R.requests,'post');self.post=self.postpatch.start();self.getpatch=patch.object(R.requests,'get');self.get=self.getpatch.start()
  self.good={'id':'pred123','status':'succeeded','model':'openai/gpt-5.6-luna','version':'hidden','output':['Complete answer.'],'metrics':{'token_input_count':20,'token_output_count':40}}
  self.post.return_value=Mock(status_code=201,json=lambda:copy.deepcopy(self.good));self.get.return_value=Mock(status_code=200,json=lambda:copy.deepcopy(self.good))
 def tearDown(self):self.postpatch.stop();self.getpatch.stop();self.env.stop();self.tmp.cleanup()
 def rows(self):return json.loads((self.root/'ledger.json').read_text())['requests']
 def test_terminal_uses_estimate_retains_reservation_and_same_key_never_reposts(self):
  x=R.ask('openai','question',request_key='same');self.assertTrue(x['ok']);self.assertEqual(x['estimated_cost_usd'],'0.00026');self.assertFalse(x['searched'])
  self.assertEqual(self.rows()[0]['status'],'completed_unbilled');self.assertNotIn('cost_usd',self.rows()[0]);self.assertNotIn('private-test',json.dumps(self.rows()))
  self.assertTrue(R.ask('openai','question',request_key='same')['ok']);self.assertEqual(self.post.call_count,1)
 def test_timeout_without_id_blocks_all_new_submissions(self):
  self.post.side_effect=R.requests.exceptions.Timeout('private-test')
  x=R.ask('openai','q',request_key='same');self.assertFalse(x['ok']);self.assertNotIn('private-test',json.dumps(x))
  self.assertFalse(R.ask('openai','q',request_key='same')['ok']);self.assertFalse(R.ask('gemini','new',request_key='next')['ok']);self.assertEqual(self.post.call_count,1)
 def test_known_prediction_resume_get_only(self):
  self.post.return_value.json=lambda:{'id':'pred123','status':'processing'}
  self.assertFalse(R.ask('openai','q',timeout=0,request_key='same')['ok'])
  self.assertTrue(R.ask('openai','q',request_key='same')['ok']);self.assertEqual(self.post.call_count,1);self.assertEqual(self.get.call_count,1)
 def test_cap_and_substitution_are_excluded(self):
  for key,value in [('model','other/model'),('metrics',{'token_input_count':20,'token_output_count':2048}),('output',[])]:
   self.good[key]=value
   self.assertFalse(R.ask('openai','q',request_key=key)['ok'])
 def test_missing_key_and_budget_stop_before_post(self):
  with patch.dict(os.environ,{'REPLICATE_API_TOKEN':''}):self.assertFalse(R.ask('openai','q')['ok'])
  with patch.object(R,'config',return_value={**self.cfg,'monthly_budget_cny':.000001,'spend_policy':None}):self.assertFalse(R.ask('openai','q')['ok'])
  self.post.assert_not_called()
 def test_metrics_aliases_not_added_twice(self):
  self.good['metrics']={'input_token_count':20,'token_input_count':20,'output_token_count':40,'token_output_count':40}
  self.assertEqual(R.ask('openai','q')['estimated_cost_usd'],'0.00026')
 def test_failed_terminal_without_metrics_is_held_not_free(self):
  self.good.update(status='failed',output=None,metrics={})
  self.assertFalse(R.ask('openai','q')['ok']);self.assertIsNone(self.rows()[0]['estimated_cost_usd']);self.assertGreater(float(self.rows()[0]['reserved_cny']),0)

 def test_existing_reservation_cannot_be_claimed_by_second_process(self):
  c=R.config();model=c['models']['openai'];key=R.hashlib.sha256(('same|'+R.measurement_id(c,model)+'|q').encode()).hexdigest()
  first=R.reserve(c,model,'q',key);self.assertTrue(first['_new'])
  self.assertFalse(R.ask('openai','q',request_key='same')['ok']);self.post.assert_not_called()
  self.assertEqual(self.rows()[0]['status'],'reserved')

class UncappedTests(ReplicateTests):
 def test_uncapped_over_300_keeps_idempotency(self):
  c=R.config()
  self.assertEqual(c['spend_policy'],'uncapped')
  self.assertIsNone(c['monthly_budget_cny'])
  with R.ledger_locked(c) as d:
   d['requests'].append({'id':'historical','month':R.datetime.now(R.TZ).strftime('%Y-%m'),'status':'completed_unbilled','reserved_cny':'99999','cost_cny':'99999'})
  self.assertTrue(R.ask('openai','q',request_key='new')['ok'])
  self.assertTrue(R.ask('openai','q',request_key='new')['ok'])
  self.assertEqual(self.post.call_count,1)
  self.assertEqual(R.budget_summary()['confirmed_billed_cny'],'99999')
 def test_uncapped_must_be_explicit(self):
  c={**self.cfg};c.pop('spend_policy',None)
  Path(R.os.environ['GEO_REPLICATE_CONFIG']).write_text(json.dumps(c))
  with self.assertRaises(ValueError):R.config()
