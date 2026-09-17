import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.db import _translate_mysql
from app.services import storage


class DatabaseBackendTests(unittest.TestCase):
    def test_mysql_and_cos_settings_are_valid(self):
        config = Settings(
            _env_file=None,
            database_url='mysql+aiomysql://user:pass@10.0.0.8:3306/cola?charset=utf8mb4',
            storage_backend='cos',
            cos_bucket='cola-123',
            cos_region='ap-shanghai',
            jwt_secret='x' * 48,
        )
        self.assertEqual(config.database_backend, 'mysql')
        config.validate_runtime()
        with self.assertRaises(ValueError):
            _ = config.database_path

    def test_sqlite_remains_the_local_default(self):
        config = Settings(_env_file=None)
        self.assertEqual(config.database_backend, 'sqlite')

    def test_wechat_cloud_mysql_environment_builds_connection_url(self):
        config = Settings(
            _env_file=None,
            mysql_address='10.0.0.8:3306',
            mysql_username='cola-user',
            mysql_password='p@ss:/word',
            mysql_database='cola',
            jwt_secret='x' * 48,
        )
        self.assertEqual(config.database_backend, 'mysql')
        self.assertEqual(
            config.database_url,
            'mysql+aiomysql://cola-user:p%40ss%3A%2Fword@10.0.0.8:3306/cola?charset=utf8mb4',
        )
        config.validate_runtime()

    def test_explicit_database_url_wins_over_cloud_mysql_variables(self):
        config = Settings(
            _env_file=None,
            database_url='sqlite+aiosqlite:///./data/explicit.db',
            mysql_address='10.0.0.8:3306',
            mysql_username='cola-user',
            mysql_password='secret',
            mysql_database='cola',
            jwt_secret='x' * 48,
        )
        self.assertEqual(config.database_backend, 'sqlite')
        self.assertTrue(config.database_url.endswith('/data/explicit.db'))

    def test_partial_cloud_mysql_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'MYSQL_USERNAME'):
            Settings(_env_file=None, mysql_address='10.0.0.8:3306')

    def test_cos_bucket_accepts_cloud_run_native_variables(self):
        config = Settings(
            _env_file=None,
            storage_backend='cos',
            bucket='cola-123456',
            region='ap-guangzhou',
            jwt_secret='x' * 48,
        )
        self.assertEqual(config.cos_bucket_name, 'cola-123456')
        self.assertEqual(config.cos_region_name, 'ap-guangzhou')
        config.validate_runtime()

    def test_mysql_translation_handles_sqlite_specific_sql(self):
        fts = (
            "INSERT INTO chunks_fts(rowid,content,chunk_id,knowledge_id,filename,page_number) "
            "VALUES((SELECT COALESCE(MAX(rowid),0)+1 FROM chunks_fts),?,?,?,?,?)"
        )
        translated_fts = _translate_mysql(fts)
        self.assertNotIn('rowid', translated_fts.lower())
        self.assertNotIn('?', translated_fts)
        self.assertIn('VALUES(%s,%s,%s,%s,%s)', translated_fts)

        upsert = (
            "INSERT INTO knowledge_suggestions(knowledge_id,fingerprint,questions_json,created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(knowledge_id) DO UPDATE SET "
            "fingerprint=excluded.fingerprint,questions_json=excluded.questions_json"
        )
        translated_upsert = _translate_mysql(upsert)
        self.assertIn('ON DUPLICATE KEY UPDATE', translated_upsert)
        self.assertIn('fingerprint=VALUES(fingerprint)', translated_upsert)
        self.assertNotIn('excluded.', translated_upsert)
        self.assertNotIn('?', translated_upsert)


class StorageCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_run_cos_credentials_are_parsed_and_cached(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'TmpSecretId': 'tmp-id',
            'TmpSecretKey': 'tmp-key',
            'Token': 'tmp-token',
            'ExpiredTime': 4102444800,
        }
        client = mock.MagicMock()
        client.__aenter__ = mock.AsyncMock(return_value=client)
        client.__aexit__ = mock.AsyncMock(return_value=False)
        client.get = mock.AsyncMock(return_value=response)

        storage._cos_credentials.clear()
        with (
            mock.patch.object(storage.settings, 'cos_secret_id', ''),
            mock.patch.object(storage.settings, 'cos_secret_key', ''),
            mock.patch.object(storage.settings, 'wechat_openapi_base', 'http://api.weixin.qq.com'),
            mock.patch.object(storage.httpx, 'AsyncClient', return_value=client),
        ):
            credentials = await storage._get_cos_credentials()
            cached = await storage._get_cos_credentials()

        self.assertEqual(credentials, ('tmp-id', 'tmp-key', 'tmp-token'))
        self.assertEqual(cached, credentials)
        client.get.assert_awaited_once_with('http://api.weixin.qq.com/_/cos/getauth')


if __name__ == '__main__':
    unittest.main()
