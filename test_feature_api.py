import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import server
from file_store import FileStore


TOOL={'type':'function','name':'lookup','description':'Look up private value','parameters':{'type':'object','properties':{'key':{'type':'string'}},'required':['key'],'additionalProperties':False},'strict':True}
CALL=json.dumps({'type':'function_calls','calls':[{'name':'lookup','arguments':{'key':'abc'}}]})


class FakeClient:
    def __init__(self):
        self.answers=[CALL]
        self.calls=[]
        self.session=object()
    async def init(self):
        pass
    async def search(self,*args,**kwargs):
        self.calls.append((args,kwargs))
        raw=self.answers.pop(0)
        if isinstance(raw,Exception):
            raise raw
        yield {'delta':raw,'answer':raw}
        yield {'done':True,'answer':raw,'backend_uuid':'backend-test'}


class FeatureApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.client=FakeClient()
        server._responses_reset_memory()
        self.patches=[patch.object(server,'API_KEY',''),patch.object(server,'RESPONSES_FILE',Path(self.temp.name)/'responses.json'),
            patch.object(server,'_file_store',FileStore(Path(self.temp.name)/'uploads')),
            patch.object(server,'get_client',return_value=self.client),patch.object(server,'_session_cache',{}),
            patch.object(server,'_rate_limit',{'remaining_pro':None}),patch.object(server,'_response_suffix',return_value='')]
        for p in self.patches:p.start()
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test')
    async def asyncTearDown(self):
        await self.http.aclose()
        for task in list(server._responses_tasks.values()):task.cancel()
        for p in reversed(self.patches):p.stop()
        server._responses_reset_memory()
        self.temp.cleanup()
    async def create_call(self,**extra):
        r=await self.http.post('/v1/responses',json={'model':'auto','input':'Look up abc','tools':[TOOL],**extra})
        self.assertEqual(r.status_code,200,r.text)
        return r
    async def test_function_round_trip_and_missing_tools_on_followup(self):
        first=(await self.create_call()).json();call=first['output'][0]
        self.assertEqual(call['type'],'function_call');self.assertEqual(first['output_text'],'')
        self.client.answers=[json.dumps({'type':'message','content':'The value is 8492'})]
        r=await self.http.post('/v1/responses',json={'model':'auto','previous_response_id':first['id'],'input':[{'type':'function_call_output','call_id':call['call_id'],'output':'8492'}]})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['output_text'],'The value is 8492')
        query=json.loads(self.client.calls[-1][0][0])
        self.assertEqual(query['input'][-1]['output'],'8492')
        self.assertEqual(query['input'][-2]['call_id'],call['call_id'])
    async def test_plain_followup_keeps_inherited_function_history(self):
        first=(await self.create_call()).json();call=first['output'][0]
        self.client.answers=[json.dumps({'type':'message','content':'The value is 8492'}), json.dumps({'type':'message','content':'8492'})]
        second=await self.http.post('/v1/responses',json={'model':'auto','previous_response_id':first['id'],'input':[{'type':'function_call_output','call_id':call['call_id'],'output':'8492'}]})
        third=await self.http.post('/v1/responses',json={'model':'auto','previous_response_id':second.json()['id'],'input':'Repeat that value'})
        self.assertEqual(third.status_code,200,third.text)
        history=json.loads(self.client.calls[-1][0][0])['input']
        self.assertTrue(any(item.get('output')=='8492' for item in history))

    async def test_invalid_tool_type_returns_400(self):
        for kind in ([], {}):
            response=await self.http.post('/v1/responses',json={'input':'test','tools':[{'type':kind}]})
            self.assertEqual(response.status_code,400)

    async def test_stateless_tool_round_trip(self):
        first=(await self.create_call(store=False)).json()
        self.client.answers=[json.dumps({'type':'message','content':'done'})]
        r=await self.http.post('/v1/responses',json={'model':'auto','input':[{'role':'user','content':'Look up abc'},*first['output'],{'type':'function_call_output','call_id':first['output'][0]['call_id'],'output':'123'}],'tools':[TOOL]})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['output_text'],'done')
    async def test_streamed_arguments_match_completed_call(self):
        r=await self.create_call(stream=True)
        events=[json.loads(line[6:]) for line in r.text.splitlines() if line.startswith('data: ')]
        self.assertEqual([e['sequence_number'] for e in events],list(range(len(events))))
        call=events[-1]['response']['output'][0]
        args=''.join(e['delta'] for e in events if e['type']=='response.function_call_arguments.delta')
        self.assertEqual(args,call['arguments']);self.assertEqual(json.loads(args),{'key':'abc'})
        self.assertNotIn('response.output_text.delta',[e['type'] for e in events])
    async def test_invalid_model_json_is_failure_not_text(self):
        self.client.answers=['not protocol JSON']
        r=await self.http.post('/v1/responses',json={'model':'auto','input':'x','tools':[TOOL]})
        self.assertEqual(r.status_code,502)
        self.assertEqual(r.json()['error']['code'],'tool_protocol_error')
    async def test_invalid_stream_never_completes(self):
        self.client.answers=['not protocol JSON']
        r=await self.create_call(stream=True)
        self.assertIn('response.failed',r.text);self.assertNotIn('response.completed',r.text)
    async def test_unmatched_call_output_is_rejected_before_model(self):
        r=await self.http.post('/v1/responses',json={'model':'auto','tools':[TOOL],'input':[{'type':'function_call_output','call_id':'missing','output':'x'}]})
        self.assertEqual(r.status_code,400);self.assertFalse(self.client.calls)
    async def test_unfulfilled_calls_are_rejected(self):
        first=(await self.create_call()).json()
        count=len(self.client.calls)
        r=await self.http.post('/v1/responses',json={'model':'auto','tools':[TOOL],'previous_response_id':first['id'],'input':'hello'})
        self.assertEqual(r.status_code,400);self.assertEqual(len(self.client.calls),count)
    async def test_early_stream_disconnect_cancels_record(self):
        class Request:
            async def json(self):return {'model':'auto','tools':[TOOL],'input':'hello','stream':True}
        response=await server.responses_api(Request());iterator=response.body_iterator
        await anext(iterator);await iterator.aclose()
        self.assertEqual(next(iter(server._responses_store.values()))['status'],'cancelled')
        self.assertFalse(self.client.calls)
    async def test_file_upload_get_list_content_delete(self):
        r=await self.http.post('/v1/files',data={'purpose':'user_data'},files={'file':('example.txt',b'secret text','text/plain')})
        self.assertEqual(r.status_code,200,r.text);file_id=r.json()['id']
        self.assertEqual((await self.http.get('/v1/files/'+file_id)).json()['bytes'],11)
        self.assertEqual((await self.http.get('/v1/files/'+file_id+'/content')).content,b'secret text')
        self.assertEqual(len((await self.http.get('/v1/files')).json()['data']),1)
        self.assertTrue((await self.http.delete('/v1/files/'+file_id)).json()['deleted'])
        self.assertEqual((await self.http.get('/v1/files/'+file_id)).status_code,404)
    async def test_file_id_is_uploaded_and_preserved_on_response_continuation(self):
        file=(await self.http.post('/v1/files',data={'purpose':'user_data'},files={'file':('example.txt',b'document contents','text/plain')})).json()
        self.client.answers=['The answer','Next answer']
        with patch.object(server,'upload_attachment',new_callable=AsyncMock,return_value='https://storage.example/uploaded') as upload:
            r=await self.http.post('/v1/responses',json={'model':'auto','input':[{'role':'user','content':[{'type':'input_text','text':'Read this'},{'type':'input_file','file_id':file['id']}]}]})
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(upload.await_args.args[1].data,b'document contents')
            self.assertEqual(self.client.calls[-1][1]['attachments'],['https://storage.example/uploaded'])
            r2=await self.http.post('/v1/responses',json={'model':'auto','input':'Tell me more','previous_response_id':r.json()['id']})
            self.assertEqual(r2.status_code,200,r2.text)
            self.assertEqual(self.client.calls[-1][1]['attachments'],['https://storage.example/uploaded'])
            self.assertEqual(upload.await_count,1)
    async def test_chat_inline_file_does_not_put_base64_in_query(self):
        encoded=base64.b64encode(b'The hidden number is 672').decode()
        self.client.answers=['672']
        with patch.object(server,'upload_attachment',new_callable=AsyncMock,return_value='https://storage.example/uploaded'):
            r=await self.http.post('/v1/chat/completions',json={'model':'auto','messages':[{'role':'user','content':[{'type':'text','text':'Read this'},{'type':'file','file':{'filename':'example.txt','file_data':encoded}}]}]})
            self.assertEqual(r.status_code,200,r.text)
            self.assertNotIn(encoded,self.client.calls[-1][0][0])
            self.assertTrue(self.client.calls[-1][1]['attachments'])
    async def test_attachment_and_tool_can_be_combined(self):
        encoded=base64.b64encode(b'Look up abc').decode()
        with patch.object(server,'upload_attachment',new_callable=AsyncMock,return_value='https://storage.example/uploaded'):
            r=await self.http.post('/v1/responses',json={'model':'auto','tools':[TOOL],'input':[{'role':'user','content':[{'type':'input_file','filename':'example.txt','file_data':encoded}]}]})
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(r.json()['output'][0]['type'],'function_call')
            self.assertTrue(self.client.calls[-1][1]['attachments'])
            self.assertNotIn(encoded,json.dumps(server._responses_store))

if __name__=='__main__':unittest.main()
