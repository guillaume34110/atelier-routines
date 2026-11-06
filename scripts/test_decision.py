"""Decision boundaries, compact fallbacks and deterministic VPS handoff; no real weights."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import decision
import decision_payload as contract
import executor


def sample():
    return {'state':'The build failed.','questions':{'next':{'type':'choice','instructions':'Choose the next inspection.',
            'criteria':{'logs':'Inspect the failed build log','merge':'Merge the changes'}}}}


class DecisionTest(unittest.TestCase):
    def test_closed_contract_and_refusal(self):
        payload = sample()
        self.assertEqual(contract.validate(payload),payload)
        for bad in ({**payload,'argv':['rm']},{**payload,'state':''},
                    {**payload,'images':['https://example.invalid/x.png']},
                    {**payload,'questions':{'x':{'type':'tool','instructions':'run'}}}):
            with self.assertRaises(ValueError):
                contract.validate(bad)
        result = {'answers':{'next':{'choice':'logs','confidence':.995}}}
        summary = contract.summarize(payload,result,.99,'mac_metal',200)
        self.assertTrue(summary['usable'])
        self.assertNotIn('state',summary)
        result['answers']['next']['confidence'] = .97
        self.assertFalse(contract.summarize(payload,result,.99,'mac_metal',200)['usable'])
        low = decision.result(contract.summarize(payload,result,.99,'mac_metal',200))
        self.assertEqual(low['workflow_status'],'ok')
        self.assertTrue(low['summary']['fallback_required'])
        for answer in ({'choice':'shell','confidence':1},{'choice':'logs','confidence':float('nan')},{}):
            with self.assertRaises(ValueError):
                contract.summarize(payload,{'answers':{'next':answer}},.99,'mac_metal',1)

    def test_paths_fixed_recipe_and_compact_result(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = Path(tmp)
            (project/'context.txt').write_text('The build failed.')
            request = sample()
            request.pop('state')
            request['state_file'] = 'context.txt'
            (project/'input.json').write_text(json.dumps(request))
            with patch.dict(os.environ,ATELIER_ROUTINES_ROOTS=json.dumps([tmp])):
                payload = decision.payload(project,'input.json')
                self.assertEqual(payload,sample())
                (project/'link').symlink_to(project/'context.txt')
                for file in ('../context.txt','link','.env'):
                    bad = {**request,'state_file':file}
                    (project/'input.json').write_text(json.dumps(bad))
                    with self.assertRaises((ValueError,OSError)):
                        decision.payload(project,'input.json')
                config = {'vps':{'weights_job':'a'*32,'runtime_job':'b'*32}}
                with patch.object(decision,'private_json',return_value=config):
                    result = decision.prepare(project,payload,.99)
                    args = result['summary']['arguments']
                    self.assertFalse(result['summary']['inference_submitted'])
                    self.assertEqual(args['server'],'hetzner-preprod')
                    self.assertEqual(args['cpus'],2)
                    recipe = json.loads((Path(args['source'])/'.atelier-vps.json').read_text())
                    self.assertEqual(recipe['version'],1)
                    self.assertEqual(recipe['recipes']['decision']['argv'],['rtk','proxy','python3','decision_runtime.py','task'])
                    self.assertEqual(decision.prepare(project,payload,.99),result)
                native = {'answers':{'next':{'choice':'logs','confidence':.995}}}
                with patch.object(decision,'read_endpoint',return_value={'port':12345,'token':'private'}),patch.object(decision,'api',return_value=native):
                    (project/'input.json').write_text(json.dumps(sample()))
                    result = decision.execute('decision.evaluate',project,{'file':'input.json','min_confidence':.99})
                    self.assertEqual(result['workflow_status'],'ok')
                    self.assertNotIn('private',json.dumps(result))
                artifact = contract.summarize(sample(),native,.9,'vps_cpu_2',1)
                artifact['server_closed'] = True
                (project/'result.json').write_text(json.dumps(artifact))
                imported = decision.execute('decision.result',project,{'file':'result.json','request':'input.json','min_confidence':.99})
                self.assertEqual(imported['summary']['min_confidence'],.99)
                artifact['input_sha256'] = 'bad'
                (project/'result.json').write_text(json.dumps(artifact))
                with self.assertRaises(ValueError):
                    decision.execute('decision.result',project,{'file':'result.json','request':'input.json','min_confidence':.99})

    def test_dispatch_schemas(self):
        for identifier in ('decision.status','decision.evaluate','decision.prepare-vps','decision.result'):
            found = executor.find(identifier,1)
            self.assertEqual(found['matches'][0]['id'],identifier)
            self.assertEqual(found['matches'][0]['availability'],'executable')
        with self.assertRaises(ValueError):
            executor.validate('decision.evaluate',{'project':'/tmp','file':'input.json','min_confidence':.2})

    def test_inline_modes_and_bounded_parameters(self):
        params = {'project':'/tmp','input':sample()}
        checked = executor.validate('decision.evaluate',params)
        self.assertEqual(checked['input'],sample())
        self.assertEqual(checked['min_confidence'],.99)
        self.assertNotIn('file',checked)
        for bad in ({'project':'/tmp'}, {**params,'file':'input.json'},
                    {**params,'input':None}, {**params,'input':[]},
                    {**params,'input':{**sample(),'state':'x'*65537}},
                    {**params,'input':{'value':float('nan')}}):
            with self.subTest(params=tuple(bad)):
                with self.assertRaises(ValueError):
                    executor.validate('decision.evaluate',bad)

    def test_inline_reuses_file_guards_without_mutating_input(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = Path(tmp)
            (project/'context.txt').write_text(sample()['state'])
            (project/'link').symlink_to(project/'context.txt')
            (project/'.env').write_text('private')
            request = {k:v for k,v in sample().items() if k!='state'}
            request['state_file'] = 'context.txt'
            original = json.loads(json.dumps(request))
            with patch.dict(os.environ,ATELIER_ROUTINES_ROOTS=json.dumps([tmp])):
                self.assertEqual(decision.payload(project,request=request),sample())
                self.assertEqual(request,original)
                for file in ('../context.txt','link','.env'):
                    with self.assertRaises(ValueError):
                        decision.payload(project,request={**request,'state_file':file})

    def test_inline_stdio_and_legacy_cli_with_model_stub(self):
        import asyncio
        from http.server import BaseHTTPRequestHandler,HTTPServer
        import subprocess
        import sys
        import threading
        from test_mcp import connection,rpc,RTK,HERE

        calls = []
        class ModelStub(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass
            def do_POST(self):
                calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = json.dumps({'answers':{'next':{'choice':'logs','confidence':.995}}}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory(dir=os.environ['TASK_WORKSPACE']) as tmp:
            project = Path(tmp)/'project'
            project.mkdir()
            home = Path(tmp)/'home'
            model = home/'.local/share/atelier-routines-models/d1'
            model.mkdir(parents=True,mode=0o700)
            (project/'input.json').write_text(json.dumps(sample()))
            server = HTTPServer(('127.0.0.1',0),ModelStub)
            (model/'endpoint.json').write_text(json.dumps({'port':server.server_port,
                'token':'test-only','model_sha256':contract.MODEL_SHA}))
            (model/'endpoint.json').chmod(0o600)
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                async def scenario():
                    async with connection(project,HOME=str(home)) as session:
                        self.assertEqual([t.name for t in (await session.list_tools()).tools],
                                         ['routines_find','routines_run'])
                        summaries = []
                        for mode in ({'input':sample()},{'file':'input.json'}):
                            body,result = await rpc(session,'routines_run',{'id':'decision.evaluate',
                                'params':{'project':str(project),**mode}})
                            self.assertFalse(result.isError,body)
                            self.assertEqual(body['status'],'ok',body)
                            summaries.append(body['result']['summary'])
                            self.assertNotIn(sample()['state'],result.content[0].text)
                        self.assertEqual(summaries[0]['input_sha256'],summaries[1]['input_sha256'])
                        for mode in ({},{'file':'input.json','input':sample()},
                                     {'input':{**sample(),'questions':[]}}):
                            body,result = await rpc(session,'routines_run',{'id':'decision.evaluate',
                                'params':{'project':str(project),**mode}})
                            self.assertTrue(result.isError)
                            self.assertEqual(body['status'],'error')
                        self.assertEqual(len(calls),2)
                asyncio.run(scenario())
                env = {**os.environ,'HOME':str(home),'ATELIER_ROUTINES_ROOTS':json.dumps([str(project)]),
                       'ATELIER_ROUTINES_STORE':str(Path(tmp)/'cli-store'),'ATELIER_ROUTINES_USAGE':'off'}
                cli = subprocess.run([RTK,'proxy',sys.executable,str(HERE/'routines.py'),'run',
                    'decision.evaluate','--params',json.dumps({'project':str(project),'file':'input.json'})],
                    env=env,text=True,capture_output=True,timeout=30)
                self.assertEqual(cli.returncode,0,cli.stderr)
                self.assertEqual(json.loads(cli.stdout)['status'],'ok')
                self.assertEqual(calls,[sample()]*3)
            finally:
                server.shutdown()
                worker.join(timeout=3)
                server.server_close()
                self.assertFalse(worker.is_alive())

    def test_local_first_selection_and_fallback_are_observed(self):
        entries = [{'id':'files.logs','description':'Summarize a log','triggers':[]},
                   {'id':'project.errors','description':'Inspect errors','triggers':[]}]
        raw = {'answers':{'routine':{'choice':'project.errors','confidence':.995}}}
        with patch.dict(os.environ,ATELIER_ROUTINES_DECISION='on'), \
             patch.object(executor,'availability',return_value=('executable',[])), \
             patch.object(decision,'read_endpoint',return_value={'port':12345,'token':'private'}), \
             patch.object(decision,'api',return_value=raw) as call:
            selected,info = decision.route_candidates('Why did this operation fail?',entries)
            self.assertEqual(selected[0]['id'],'project.errors')
            self.assertTrue(info['accepted'])
            self.assertEqual(call.call_count,1)
            raw['answers']['routine']['confidence'] = .8
            selected,info = decision.route_candidates('Why did this operation fail?',entries)
            self.assertEqual(selected,entries)
            self.assertTrue(info['fallback_required'])
            before = call.call_count
            self.assertIsNone(decision.route_candidates('files.logs',entries)[1])
            self.assertIsNone(decision.route_candidates('nothing',[entries[0]])[1])
            self.assertEqual(call.call_count,before)
            call.side_effect = TimeoutError('unavailable')
            self.assertTrue(decision.route_candidates('Why did this operation fail?',entries)[1]['fallback_required'])


if __name__=='__main__':
    unittest.main()
