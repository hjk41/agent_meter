import unittest
from unittest.mock import patch
import requests
from agent_meter.providers import collect


class CursorErrorTests(unittest.TestCase):
    def http_failure(self,status):
        response=requests.Response(); response.status_code=status
        error=requests.HTTPError('private request details',response=response)
        with patch('agent_meter.providers.collect_cursor',side_effect=error):
            result=collect('cursor',{'network':{'mode':'system','agents':['cursor']}},'.')
        self.assertEqual(result.status,'error')
        self.assertNotIn('private',result.message)
        return result.message

    def test_server_failure_is_identified_as_service_error(self):
        self.assertIn('服务异常',self.http_failure(500))
        self.assertNotIn('代理',self.http_failure(503))

    def test_rate_limit_is_not_reported_as_proxy_failure(self):
        self.assertIn('请求限流',self.http_failure(429))

    def test_slow_quota_response_preserves_safe_timeout_message(self):
        with patch('agent_meter.providers.collect_cursor',side_effect=requests.ReadTimeout('private')):
            result=collect('cursor',{'network':{'mode':'system','agents':['cursor']}},'.')
        self.assertIn('响应超时',result.message)
        self.assertIn('等待自动刷新',result.message)
        self.assertNotIn('private',result.message)


if __name__=='__main__': unittest.main()
