import base64
import socket
import unittest
from unittest.mock import AsyncMock, patch
from attachments import *


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_file_and_image_validation(self):
        data=b'hello private document'
        result=await resolve_attachment({'type':'input_file','filename':'test.txt','file_data':base64.b64encode(data).decode()})
        self.assertEqual(result.data,data);self.assertEqual(result.content_type,'text/plain')
        png=b'\x89PNG\r\n\x1a\n'+b'example'
        result=await resolve_attachment({'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(png).decode()}})
        self.assertEqual(result.data,png)
        for value in ('not-base64', 'data:image/png;base64,aGk=', 'data:text/plain;base64,aGk='):
            with self.assertRaises(AttachmentError):await resolve_attachment({'type':'input_image','image_url':value})
    async def test_private_addresses_and_protocols_are_rejected(self):
        for url in ('file:///etc/passwd','http://example.com/a','https://user:pass@example.com/a','https://example.com:8443/a'):
            with self.assertRaises(AttachmentError):await download_attachment(url)
        loop=__import__('asyncio').get_running_loop()
        with patch.object(loop,'getaddrinfo',new_callable=AsyncMock,return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]):
            with self.assertRaisesRegex(AttachmentError,'private'):await download_attachment('https://example.com/file.txt')
    async def test_size_limit_checked_before_base64_decode(self):
        with patch('attachments.MAX_ATTACHMENT_BYTES',3):
            with self.assertRaises(AttachmentError) as err:decode_attachment(base64.b64encode(b'1234').decode(),'a.txt')
            self.assertEqual(err.exception.status_code,413)
    async def test_file_id_loader_is_validated(self):
        async def load(_):return Attachment('a.txt','text/plain',b'hello')
        self.assertEqual((await resolve_attachment({'type':'input_file','file_id':'file-test'},load)).data,b'hello')
        with self.assertRaises(AttachmentError):await resolve_attachment({'type':'input_image','file_id':'file-test'},load)
    async def test_upload_rejects_bad_destination_before_sending_data(self):
        class R:
            status_code=200
            def json(self):return {'s3_bucket_url':'https://attacker.example/upload','s3_object_url':'https://attacker.example/object','fields':{}}
        session=AsyncMock();session.post.return_value=R()
        with patch('attachments.requests.AsyncSession') as remote:
            with self.assertRaisesRegex(AttachmentError,'unrecognized'):await upload_attachment(session,Attachment('a.txt','text/plain',b'hello'))
            remote.assert_not_called()
    async def test_upload_auth_and_storage_errors_are_visible(self):
        class R:status_code=401
        session=AsyncMock();session.post.return_value=R()
        with self.assertRaisesRegex(AttachmentError,'HTTP 401'):await upload_attachment(session,Attachment('a.txt','text/plain',b'hello'))
    async def test_redirect_targets_are_revalidated_and_no_credentials_forwarded(self):
        import attachments
        class R:status_code=302;headers={'location':'https://127.0.0.1/private'}
        remote=AsyncMock();remote.get.return_value=R()
        manager=AsyncMock();manager.__aenter__.return_value=remote
        parsed=__import__('urllib.parse',fromlist=['urlsplit']).urlsplit('https://example.com/file.txt')
        with patch.object(attachments,'_public_target',new_callable=AsyncMock,side_effect=[(parsed,'93.184.216.34'),AttachmentError('private')]) as target, patch.object(attachments.requests,'AsyncSession',return_value=manager) as constructor:
            with self.assertRaisesRegex(AttachmentError,'private'):await download_attachment('https://example.com/file.txt')
            self.assertEqual(target.await_count,2)
            self.assertFalse(constructor.call_args.kwargs['trust_env'])
            self.assertNotIn('cookies',constructor.call_args.kwargs)
            self.assertFalse(remote.get.await_args.kwargs['allow_redirects'])

class ProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def check_result(self, events):
        from attachments import _wait_for_processing
        class Response:
            status_code=200
            aclose=AsyncMock()
            async def aiter_lines(self):
                for line in events:
                    yield line
        response=Response()
        session=AsyncMock();session.post.return_value=response
        try:
            return await _wait_for_processing(session, "test-id", "https://example.com/original")
        finally:
            response.aclose.assert_awaited_once()
            response.aclose.reset_mock()

    async def test_cancellation_closes_processing_stream(self):
        import asyncio
        from attachments import _wait_for_processing
        entered=asyncio.Event()
        class Response:
            status_code=200
            aclose=AsyncMock()
            async def aiter_lines(self):
                entered.set()
                await asyncio.Event().wait()
                yield ''
        response=Response()
        session=AsyncMock();session.post.return_value=response
        task=asyncio.create_task(_wait_for_processing(session,'test-id','https://example.com/original'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        response.aclose.assert_awaited_once()

    async def test_processed_url_is_used(self):
        result=await self.check_result(['data: {"file_uuid":"test-id","success":true,"s3_url":"https://example.com/parsed"}', ''])
        self.assertEqual(result, 'https://example.com/parsed')

    async def test_failures_never_return_unparsed_document(self):
        for lines in (
            ['data: {"file_uuid":"test-id","success":false}', ''],
            ['data: {"file_uuid":"test-id","success":true,"token_limit_exceeded":true}', ''],
            ['data: invalid', ''],
            ['data: {"file_uuid":"other","success":true}', ''],
            ['data: {"file_uuid":"test-id","success":true}'],
        ):
            with self.assertRaises(AttachmentError):
                await self.check_result(lines)

if __name__=='__main__':unittest.main()
