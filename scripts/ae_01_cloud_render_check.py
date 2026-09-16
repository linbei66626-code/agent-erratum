#!/usr/bin/env python3
"""Offline pinned-Letta request rendering, not a running Letta/database test."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--letta-source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise FileExistsError('output exists')
    src=args.letta_source.resolve(strict=True)
    def git(*a):return subprocess.check_output(['git','-C',str(src),*a],text=True).strip()
    assert git('rev-parse','HEAD')=='56ba9c25552605eec89de8ed3dc6394b625c1993'
    assert not git('status','--porcelain','--untracked-files=no')
    # Set before any framework imports; no external or loopback socket allowed.
    def no_network(*a,**k):raise RuntimeError('offline renderer forbids network')
    socket.socket.connect=socket.socket.connect_ex=socket.create_connection=no_network
    from ae_cloud_task import CloudExecutionProfile
    from ae_cloud_proxy import CloudConfig,normalize_request
    from ae_probe import probe_payload, PROBE_TOOL
    from letta.llm_api.openai_client import OpenAIClient
    from letta.schemas.agent import CreateAgent
    from letta.schemas.message import Message
    from letta.schemas.enums import AgentType,MessageRole
    import letta
    assert Path(letta.__file__).resolve().is_relative_to(src)
    c=json.loads((ROOT/'configs/ae-01__cloud-connection__siliconflow.prototype.json').read_text())
    profile=CloudExecutionProfile('connection');profile.validate(c)
    p=profile.payload(c,probe_payload(c,name='ae-cloud-offline-render'))
    parsed=CreateAgent.model_validate(p)
    assert parsed.model is None and parsed.llm_config is not None
    assert parsed.llm_config.model_dump()['context_window']==c['context_window']
    cloud=CloudConfig(**json.loads((ROOT/'configs/ae-01__cloud-transport__siliconflow.letta-probe.json').read_text()))
    client=OpenAIClient(actor=SimpleNamespace(id='user-offline-render'))
    base=[Message(role=MessageRole.system,content=[{'type':'text','text':p['system']}]),
          Message(role=MessageRole.user,content=[{'type':'text','text':'Call read_probe then repeat its nonce.'}])]
    call={'id':'call-offline-1','type':'function','function':{'name':'read_probe','arguments':'{}'}}
    nonce='OFFLINE_NONCE_NOT_A_MODEL_RESULT'
    full=base+[Message(role=MessageRole.assistant,content=None,tool_calls=[call]),
               Message(role=MessageRole.tool,content=[{'type':'text','text':json.dumps({'nonce':nonce})}],
                       tool_call_id=call['id'])]
    captures=[]
    for name,messages,tools in [('no_tools',base,None),('tool_request',base,[PROBE_TOOL]),
                                ('tool_continuation',full,[PROBE_TOOL])]:
        request=client.build_request_data(AgentType.letta_v1_agent,messages,parsed.llm_config,tools=tools)
        raw=json.dumps(request,ensure_ascii=False).encode()
        normalized,changes=normalize_request(raw,cloud)
        final=json.loads(normalized)
        assert final['messages']==request['messages'] and final.get('tools')==request.get('tools')
        assert final.get('parallel_tool_calls')==request.get('parallel_tool_calls')
        assert final['user']==request['user'] and final['max_tokens']==256
        assert 'seed' not in final and 'max_completion_tokens' not in final
        if name=='tool_continuation':
            assert final['messages'][-1]['tool_call_id']==call['id']
            assert nonce in final['messages'][-1]['content']
            assert final['messages'][-2]['tool_calls'][0]['id']==call['id']
        captures.append({'case':name,'actual_letta_request':request,'cloud_request':final,'changes':changes})
    files=['letta/server/server.py','letta/schemas/agent.py','letta/llm_api/openai_client.py']
    report={'status':'OFFLINE_RENDER_PASS','model_called':False,'database_tested':False,
            'api_compatibility_verified':False,'letta_revision':git('rev-parse','HEAD'),
            'letta_source_sha256':{f:hashlib.sha256((src/f).read_bytes()).hexdigest() for f in files},
            'adapter_sha256':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in
                              ['ae_cloud_task.py','ae_cloud_proxy.py','scripts/ae_01_cloud_render_check.py']},
            'captures':captures,'scientific_result':None}
    with args.output.open('x') as out:
        os.chmod(args.output,0o600);json.dump(report,out,indent=2,ensure_ascii=False)
    print('OFFLINE_RENDER_PASS: 3 actual Letta request shapes; no model/database/network')


if __name__=='__main__':main()
