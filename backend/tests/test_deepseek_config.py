"""只检查模型请求配置，不发送网络请求。"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from settings import deepseek_extra_body
from agent.models.answer_client import FinalAnswerModelClient
from agent.models.router_client import RouteModelClient
from agent.tools.langchain_runtime import LangChainToolRunner


class DeepSeekConfigTest(unittest.TestCase):
    def test_all_model_paths_set_thinking_explicitly(self):
        with patch.dict(os.environ, {
            'AGENT_COURSE_ENV': '/tmp/huangye-test-no-env',
            'AGENT_OPENAI_BASE_URL': 'https://api.deepseek.com',
            'AGENT_OPENAI_MODEL': 'deepseek-flash',
            'AGENT_OPENAI_API_KEY': 'test-key',
        }):
            clients = [
                RouteModelClient()._create_chat_model(temperature=0),
                FinalAnswerModelClient()._create_chat_model(temperature=0.2),
                LangChainToolRunner()._create_chat_model(),
            ]
            for client in clients:
                self.assertEqual(client.extra_body, {'thinking': {'type': 'disabled'}})
                self.assertEqual(client.max_retries, 0)
                self.assertLessEqual(client.max_tokens, 1024)
            with patch('openai.OpenAI') as factory:
                factory.return_value.chat.completions.create.return_value = SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='回答', reasoning_content='推理'))]
                )
                FinalAnswerModelClient._invoke_openai_model([], temperature=0.2, enable_thinking=True)
                self.assertEqual(factory.return_value.chat.completions.create.call_args.kwargs['extra_body'],
                                 {'thinking': {'type': 'enabled'}})
        with patch.dict(os.environ, {'AGENT_OPENAI_BASE_URL': 'https://api.siliconflow.cn/v1'}):
            self.assertIsNone(deepseek_extra_body())


if __name__ == '__main__':
    unittest.main()
