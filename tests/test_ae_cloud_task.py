"""Cloud driver fixtures; no real API or scientific result."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ae_cloud_task import CloudExecutionProfile, TASK_PROFILE
from ae_cloud_proxy import COMPAT_PROFILE, PROFILE, MODEL, CloudConfig, CloudAuditProxy, normalize_request
from ae_cloud_audit import audit_cloud_journal
from ae_model_proxy import ProxyBlocked
from ae_capability import build_plan, execute_capability, validate_config
from ae_probe import execute_probe, probe_payload
from test_ae_probe import LettaFixture, ModelFixture
from test_ae_capability import sample, CapServices, CapRuntime, CapEnv, done, config as old_config
from test_ae_cloud_proxy import Reply, KEY

ROOT = Path(__file__).resolve().parents[1]


def cfg(kind):
    name = ('ae-01__capability__siliconflow.prototype.json' if kind == 'capability'
            else 'ae-01__cloud-connection__siliconflow.prototype.json')
    return json.loads((ROOT/'configs'/name).read_text())


class CloudLetta(LettaFixture):
    def request(self, method, path, body=None):
        if path == '/v1/health/':
            return {'status':'ok', 'version':'0.16.8'}
        if path == '/v1/agents/' and method == 'POST':
            assert 'model' not in body and 'model_settings' not in body
            self.actual_payload = deepcopy(body)
            # Only adapt to the pre-existing fixture's expected shape, not the
            # production API. The actual supplied legacy config is checked below.
            p = deepcopy(body); llm=p['llm_config']
            p.update(model=llm['handle'], context_window_limit=llm['context_window'],
                     model_settings={'max_output_tokens':llm['max_tokens'], 'temperature':llm['temperature']})
            result = super().request(method,path,p)
            self.state['llm_config'] = deepcopy(llm) | self.llm_changes
            return result
        return super().request(method,path,body)


class CloudTaskTests(unittest.TestCase):
    def test_key_writer_private_exclusive_and_no_links(self):
        spec=importlib.util.spec_from_file_location('key_cli',ROOT/'scripts/ae_01_set_cloud_key.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp).resolve()/'private/key'
            mod.save_key(path,KEY)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777,0o700)
            with self.assertRaises(FileExistsError):mod.save_key(path,KEY+'second')
            before=path.read_bytes()
            link=path.parent/'link';link.symlink_to(path)
            with self.assertRaises(FileExistsError):mod.save_key(link,KEY)
            self.assertEqual(path.read_bytes(),before)
            os.chmod(path.parent,0o755)
            with self.assertRaises(ValueError):mod.save_key(path.parent/'new',KEY)
            self.assertFalse((path.parent/'new').exists())

    def test_config_must_explicitly_select_cloud_and_provenance(self):
        for kind in ('connection','capability'):
            c=cfg(kind); p=CloudExecutionProfile(kind)
            self.assertEqual(p.validate(c), c)
            for key,value in [('context_window_source','server_measured'),('expected_model','other'),
                              ('transport_profile',PROFILE),('context_window',999999),
                              ('model_origin','https://api.siliconflow.cn')]:
                with self.subTest(kind=kind,key=key), self.assertRaises(ValueError):
                    p.validate(dict(c,**{key:value}))
        with self.assertRaises(ValueError): validate_config(cfg('capability'))
        with self.assertRaises(ValueError): CloudExecutionProfile('capability').validate(old_config())

    def test_plan_preserves_task_content_not_old_model_claims(self):
        old=build_plan(old_config(),sample())
        new=build_plan(cfg('capability'),sample(),execution_profile=CloudExecutionProfile('capability'))
        self.assertEqual(old['inputs'],new['inputs'])
        for key in ('system','memory_blocks','initial_message_sequence'):
            self.assertEqual(old['agent_payload'][key],new['agent_payload'][key])
        self.assertNotIn('model',new['agent_payload'])
        self.assertFalse(new['agent_payload']['llm_config']['enable_reasoner'])
        self.assertIsNone(new['scientific_result'])
        self.assertEqual(new['schema_version'],TASK_PROFILE)
        self.assertFalse(new['cloud_execution']['server_capacity_verified'])

    def test_catalog_is_not_fabricated_and_requires_exact_identity(self):
        p=CloudExecutionProfile('connection'); c=cfg('connection')
        row={'id':MODEL}; listed={'data':[row]}
        self.assertEqual(p.check_catalog(listed,c),row)
        self.assertEqual(listed,{'data':[{'id':MODEL}]})
        for data in [[],[row,row],[{'id':'other'}],None]:
            with self.assertRaises(ValueError): p.check_catalog({'data':data},c)

    def test_connection_real_local_nonce_path_with_catalog_without_window(self):
        c=cfg('connection'); m=ModelFixture(MODEL); m.data=[{'id':MODEL}]
        letta=CloudLetta(config=c)
        result=execute_probe(c,model_transport=m,letta_transport=letta,
                             execution_profile=CloudExecutionProfile('connection'))
        self.assertTrue(result['connectivity_passed'],result['invalid_reasons'])
        self.assertEqual(result['tool_invocations'],1)
        self.assertEqual(result['provider_model'],{'id':MODEL})
        self.assertIsNone(result['scientific_result'])

    def test_config_drift_blocks_before_messages(self):
        c=cfg('connection'); m=ModelFixture(MODEL)
        letta=CloudLetta(config=c,llm_changes={'context_window':999})
        result=execute_probe(c,model_transport=m,letta_transport=letta,
                             execution_profile=CloudExecutionProfile('connection'))
        self.assertFalse(result['connectivity_passed'])
        self.assertEqual(letta.posts,0)

    def test_capability_shared_loop_does_not_turn_completion_into_success(self):
        c=cfg('capability'); services=CapServices(c,[done('fixture ###STOP###')])
        result=execute_capability(c,sample(),model_transport=services,letta_transport=services,
                 runtime_factory=lambda:CapRuntime(CapEnv()),execution_profile=CloudExecutionProfile('capability'))
        self.assertTrue(result['execution_complete'],result['invalid_reasons'])
        self.assertIsNone(result['task_success'])
        self.assertFalse(result['task_oracle']['oracle_passed'])
        self.assertEqual(result['schema_version'],TASK_PROFILE)
        creation=next(r[2] for r in services.requests if r[0:2]==('POST','/v1/agents/'))
        self.assertIn('llm_config',creation)
        self.assertNotIn('model',creation)

    def test_compat_passthrough_only_and_old_profile_remains_strict(self):
        c=CloudConfig(MODEL,256,10000,10000,6,60,profile=COMPAT_PROFILE)
        body={'model':MODEL,'messages':[{'role':'user','content':'hello'}],
              'max_completion_tokens':256,'user':'user-fixture','parallel_tool_calls':False,'tools':None,'tool_choice':None}
        raw=json.dumps(body).encode(); mapped,changes=normalize_request(raw,c)
        want=deepcopy(body); want['max_tokens']=want.pop('max_completion_tokens')
        self.assertEqual(json.loads(mapped),want)
        self.assertEqual(len(changes),1)
        with self.assertRaises(ProxyBlocked): normalize_request(raw,replace(c,profile=PROFILE))
        for extra in [{'parallel_tool_calls':True},{'seed':300},{'chat_template_kwargs':{}},{'user':[]}]:
            with self.assertRaises(ProxyBlocked):normalize_request(json.dumps(body|extra).encode(),c)

    def test_compat_journal_audits_new_profile(self):
        c=CloudConfig(MODEL,256,10000,10000,6,60,profile=COMPAT_PROFILE)
        body={'model':MODEL,'messages':[{'role':'user','content':'hello'}], 'max_tokens':256,'user':'user-fixture'}
        reply={'model':MODEL,'choices':[{'message':{'role':'assistant','content':'fixture'},'finish_reason':'stop'}],
               'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.jsonl'; p=CloudAuditProxy(c,path,api_key=KEY)
            class Fake:
                def open(self,*a,**k): return Reply(json.dumps(reply).encode())
            p.opener=Fake()
            p.dispatch('POST','/v1/chat/completions',json.dumps(body).encode());p.close()
            report=audit_cloud_journal(path)
            self.assertTrue(report['transport_capture_checked'],report)
            self.assertEqual(report['profile'],COMPAT_PROFILE)

    def test_default_connection_cli_no_network_and_no_overwrite(self):
        spec=importlib.util.spec_from_file_location('cloud_cli',ROOT/'scripts/ae_01_cloud_connection.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp, patch('socket.socket.connect',side_effect=AssertionError('offline')):
            out=Path(tmp)/'new'
            args=['--config',str(ROOT/'configs/ae-01__cloud-connection__siliconflow.prototype.json'),'--output-dir',str(out)]
            self.assertEqual(mod.main(args),0)
            before=(out/'plan.json').read_bytes()
            self.assertEqual(mod.main(args),2)
            self.assertEqual(before,(out/'plan.json').read_bytes())
