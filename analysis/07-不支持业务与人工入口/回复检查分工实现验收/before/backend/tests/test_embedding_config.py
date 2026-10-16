"""检索向量和聊天模型分开配置。不访问网络。"""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from settings import embedding_api_key, embedding_base_url, embedding_model_name
from retrieval import hybrid_retrieval as hybrid

_CHAT_ONLY_ENV = Path("/tmp/embedding-config-chat-only.env")


class EmbeddingConfigTest(unittest.TestCase):
    def test_embedding_does_not_follow_the_chat_endpoint(self):
        with patch.dict(os.environ, {
            "AGENT_OPENAI_BASE_URL": "https://api.deepseek.com",
            "AGENT_OPENAI_API_KEY": "sk-chat",
            "AGENT_OPENAI_MODEL": "deepseek-flash",
            "AGENT_EMBEDDING_API_KEY": "sk-silicon",
        }, clear=False):
            os.environ.pop("AGENT_EMBEDDING_MODEL", None)
            os.environ.pop("AGENT_EMBEDDING_BASE_URL", None)
            self.assertEqual(embedding_model_name(), "Qwen/Qwen3-Embedding-8B")
            self.assertEqual(embedding_base_url(), "https://api.siliconflow.cn/v1")
            self.assertEqual(embedding_api_key(), "sk-silicon")

    def test_offline_flag_still_skips_the_embedding_api(self):
        with patch.dict(os.environ, {"AGENT_COURSE_OFFLINE_RAG": "1"}, clear=False):
            os.environ.pop("AGENT_EMBEDDING_API_KEY", None)
            _embeddings, mode = hybrid.build_course_embeddings()
        self.assertEqual(mode, "local_token_embedding_for_explicit_offline_course")

    def test_online_embedding_requires_its_own_key(self):
        _CHAT_ONLY_ENV.write_text(
            "AGENT_OPENAI_API_KEY=sk-chat\n"
            "AGENT_OPENAI_BASE_URL=https://api.deepseek.com\n"
            "AGENT_OPENAI_MODEL=deepseek-flash\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {
            "AGENT_COURSE_OFFLINE_RAG": "0",
            "AGENT_COURSE_ENV": str(_CHAT_ONLY_ENV),
            "AGENT_OPENAI_API_KEY": "sk-chat",
            "AGENT_EMBEDDING_API_KEY": "",
        }, clear=False):
            with self.assertRaisesRegex(RuntimeError, "AGENT_EMBEDDING_API_KEY"):
                hybrid.build_course_embeddings()


if __name__ == "__main__":
    unittest.main()
