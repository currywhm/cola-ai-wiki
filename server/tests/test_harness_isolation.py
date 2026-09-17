"""Regression tests for official Harness tenant isolation.

The adapter owns tenant paths and the official skill root only. Profile
composition, sessions, permissions, tools, retries and compaction are owned by
the upstream sdk profile.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

RUNNER = r'''
import json, os
from pathlib import Path

from app.services import harness

home = Path(os.environ['COLA_TEST_HOME'])
workspaces = Path(os.environ['COLA_TEST_WORKSPACES'])

out = {}
out['tenant_escape'] = harness._tenant_id('../../etc/passwd')
out['tenant_nested'] = harness._tenant_id('a/../b')
out['tenant_empty'] = harness._tenant_id('')
out['tenant_keep'] = harness._tenant_id('eb48428d90774431a93e2880524f9a5e')

a = harness.user_home('tenant-a')
b = harness.user_home('tenant-b')
wa = harness.user_workspace('tenant-a')
wb = harness.user_workspace('tenant-b')
out['home_distinct'] = str(a) != str(b)
out['home_inside_root'] = str(a).startswith(str(home / 'users'))
out['workspace_distinct'] = str(wa) != str(wb)
out['workspace_inside_root'] = str(wa).startswith(str(workspaces / 'users'))
out['profiles_not_shared'] = not (a / 'profiles').exists()
out['skills_isolated'] = harness.skills_target_dir('tenant-a') != harness.skills_target_dir('tenant-b')

patch = harness.runtime_patch_path()
text = patch.read_text(encoding='utf-8') if patch.exists() else ''
out['patch_official'] = '@cola/' not in text
out['patch_has_system_prompt'] = '- id: system-prompt' in text
out['patch_inserts_official_present'] = "name: '@deepseek-ai/dsh-tool-present'" in text
out['patch_has_custom_permission'] = '- id: sandbox-policy' in text or '- id: approval' in text or '- id: permission' in text
out['patch_disables_official_tools'] = 'disabled: true' in text

print('RESULT ' + json.dumps(out, ensure_ascii=False))
'''


class HarnessIsolationTest(unittest.TestCase):
    def test_official_harness_asset_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.update({
                'HARNESS_ENABLED': 'true',
                'HARNESS_HOME': str(Path(tmp) / 'harness-home'),
                'HARNESS_WORKSPACES': str(Path(tmp) / 'harness-workspaces'),
                'COLA_TEST_HOME': str(Path(tmp) / 'harness-home'),
                'COLA_TEST_WORKSPACES': str(Path(tmp) / 'harness-workspaces'),
                'PYTHONPATH': str(ROOT),
            })
            proc = subprocess.run(
                [sys.executable, '-c', RUNNER],
                env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            line = [row for row in proc.stdout.splitlines() if row.startswith('RESULT ')]
            self.assertTrue(line, proc.stdout[-2000:])
            result = json.loads(line[-1][len('RESULT '):])

        self.assertNotIn('/', result['tenant_escape'])
        self.assertNotIn('..', result['tenant_escape'])
        self.assertNotIn('/', result['tenant_nested'])
        self.assertEqual(result['tenant_empty'], 'anonymous')
        self.assertEqual(result['tenant_keep'], 'eb48428d90774431a93e2880524f9a5e')

        self.assertTrue(result['home_distinct'])
        self.assertTrue(result['home_inside_root'])
        self.assertTrue(result['workspace_distinct'])
        self.assertTrue(result['workspace_inside_root'])
        self.assertTrue(result['profiles_not_shared'])
        self.assertTrue(result['skills_isolated'])

        self.assertTrue(result['patch_official'])
        self.assertTrue(result['patch_has_system_prompt'])
        self.assertTrue(result['patch_inserts_official_present'])
        self.assertFalse(result['patch_has_custom_permission'])
        self.assertFalse(result['patch_disables_official_tools'])


if __name__ == '__main__':
    unittest.main()
