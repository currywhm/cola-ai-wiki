import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def test_paths_do_not_depend_on_working_directory(self):
        with tempfile.TemporaryDirectory(prefix='zhi-path-test-') as cwd:
            result = subprocess.run([sys.executable, '-c',
                "from app.config import Settings, BACKEND_ROOT; "
                "s=Settings(_env_file=None, database_url='sqlite+aiosqlite:///./data/check.db', upload_dir='./uploads'); "
                "assert s.database_path.resolve() == (BACKEND_ROOT/'data/check.db').resolve(); "
                "assert s.resolve_path(s.upload_dir).resolve() == (BACKEND_ROOT/'uploads').resolve(); "
                "assert s.resolve_path(s.wechat_pay_private_key_path).is_absolute()"],
                cwd=cwd, env={**os.environ, 'PYTHONPATH': str(ROOT)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_generic_llm_environment_takes_precedence(self):
        sys.path.insert(0, str(ROOT))
        from app.services import llm
        with (
            mock.patch.object(llm.settings, 'llm_api_key', 'generic-key'),
            mock.patch.object(llm.settings, 'llm_base_url', 'https://llm.example.com/v1'),
            mock.patch.object(llm.settings, 'llm_model', 'cloud-model'),
            mock.patch.object(llm.settings, 'deepseek_api_key', 'legacy-key'),
        ):
            self.assertEqual(
                llm.provider_config('deepseek-chat'),
                ('https://llm.example.com/v1', 'generic-key', 'cloud-model'),
            )

    def test_unsafe_runtime_config_is_rejected(self):
        sys.path.insert(0, str(ROOT))
        from app.config import Settings
        with self.assertRaises(ValueError):
            Settings(_env_file=None, jwt_secret='change-me-in-production').validate_runtime()
        with self.assertRaises(ValueError):
            Settings(_env_file=None, jwt_secret='x'*48, app_env='production', wechat_appid='', wechat_secret='').validate_runtime()
        with self.assertRaises(ValueError):
            _ = Settings(_env_file=None, database_url='postgresql://localhost/db').database_path
        with self.assertRaisesRegex(ValueError, 'WECHAT_APPID 格式'):
            Settings(_env_file=None, jwt_secret='x'*48, app_env='production', wechat_appid='wx-short', wechat_secret='a'*32).validate_runtime()
        with self.assertRaisesRegex(ValueError, 'WECHAT_SECRET 格式'):
            Settings(_env_file=None, jwt_secret='x'*48, app_env='production', wechat_appid='wx1234567890abcdef', wechat_secret='short').validate_runtime()


if __name__ == '__main__':
    unittest.main()
