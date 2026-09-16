"""多租户隔离 / 计划评审桥的回归测试。

覆盖三件容易回退的事：
1. 租户目录名收敛（user_id 里的路径穿越不能逃出 DSH_HOME 根）；
2. 每个租户的 DSH_HOME / 工作区 / 计划桥目录彼此独立，且 profiles 用软链共享；
3. 计划模式的开关与评审结论能通过文件队列往返，且超时/不存在的评审不会被误判为已批准。

测试跑在子进程里：harness 配置来自环境变量，直接 import 会污染其他用例的 settings。
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
shared_profiles = home / 'profiles'
shared_profiles.mkdir(parents=True, exist_ok=True)
(shared_profiles / 'sdk').mkdir(parents=True, exist_ok=True)

out = {}

# 1) 路径穿越与空值都要收敛
out['tenant_escape'] = harness._tenant_id('../../etc/passwd')
out['tenant_nested'] = harness._tenant_id('a/../b')
out['tenant_empty'] = harness._tenant_id('')
out['tenant_keep'] = harness._tenant_id('eb48428d90774431a93e2880524f9a5e')

# 2) 两个租户的目录必须互不相同、且都在根之内
a = harness.user_home('tenant-a')
b = harness.user_home('tenant-b')
wa = harness.user_workspace('tenant-a')
wb = harness.user_workspace('tenant-b')
out['home_distinct'] = str(a) != str(b)
out['home_inside_root'] = str(a).startswith(str(home / 'users'))
out['workspace_distinct'] = str(wa) != str(wb)
out['workspace_inside_root'] = str(wa).startswith(str(workspaces / 'users'))
out['profiles_shared'] = (a / 'profiles').is_symlink() and (a / 'profiles').resolve() == shared_profiles.resolve()

# 3) 技能根按租户隔离
out['skills_isolated'] = harness.skills_target_dir('tenant-a') != harness.skills_target_dir('tenant-b')

# 4) 计划模式开关：写入即被对应租户读到，未知评审不能冒充已批准
out['mode_written'] = harness.set_plan_mode('tenant-a', 'sess-1', True)
mode_file = harness.plan_bridge_dir('tenant-a') / 'sess-1.mode.json'
out['mode_payload'] = json.loads(mode_file.read_text(encoding='utf-8'))

out['review_unknown_rejected'] = harness.submit_plan_review('tenant-a', 'nope', True) is False
out['review_pending_none'] = harness.pending_plan_review('tenant-a', 'nope') is None

pending = harness.plan_bridge_dir('tenant-a') / 'sess-1.review.json'
pending.write_text(json.dumps({'review_id': 'sess-1', 'plan': '# 计划'}), encoding='utf-8')
out['review_pending_read'] = (harness.pending_plan_review('tenant-a', 'sess-1') or {}).get('plan')
out['review_submit'] = harness.submit_plan_review('tenant-a', 'sess-1', True)
out['review_answer'] = json.loads((harness.plan_bridge_dir('tenant-a') / 'sess-1.answer.json').read_text(encoding='utf-8'))
# 租户隔离：b 看不到 a 的评审
out['review_cross_tenant'] = harness.pending_plan_review('tenant-b', 'sess-1') is None

# 5) 部署级运行时资产：profile patch 与计划桥插件都要同步到共享 profiles
imported = harness.sync_runtime_assets()
patch = home / 'profiles' / 'sdk' / 'cordis.patch.yml'
text = patch.read_text(encoding='utf-8') if patch.exists() else ''
out['assets_synced'] = imported > 0
out['patch_has_bridge'] = 'cola-plan-bridge' in text
out['patch_has_never'] = 'policy: never' in text
out['patch_has_workspace_root'] = 'DSH_WORKSPACE_ROOT' in text
plugin = home / 'profiles' / 'node_modules' / '@cola' / 'dsh-plan-bridge' / 'lib' / 'index.js'
out['plugin_installed'] = plugin.is_file()

print('RESULT ' + json.dumps(out, ensure_ascii=False))
'''


class HarnessIsolationTest(unittest.TestCase):
    def test_isolation_and_plan_bridge(self) -> None:
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

        # 路径穿越：分隔符与 . 被剔除，最终仍是根内的单层目录名
        self.assertNotIn('/', result['tenant_escape'])
        self.assertNotIn('..', result['tenant_escape'])
        self.assertNotIn('/', result['tenant_nested'])
        self.assertEqual(result['tenant_empty'], 'anonymous')
        self.assertEqual(result['tenant_keep'], 'eb48428d90774431a93e2880524f9a5e')

        # 隔离边界
        self.assertTrue(result['home_distinct'])
        self.assertTrue(result['home_inside_root'])
        self.assertTrue(result['workspace_distinct'])
        self.assertTrue(result['workspace_inside_root'])
        self.assertTrue(result['profiles_shared'])
        self.assertTrue(result['skills_isolated'])

        # 计划模式开关与评审往返
        self.assertTrue(result['mode_written'])
        self.assertEqual(result['mode_payload'], {'plan': True})
        self.assertTrue(result['review_unknown_rejected'])
        self.assertTrue(result['review_pending_none'])
        self.assertEqual(result['review_pending_read'], '# 计划')
        self.assertTrue(result['review_submit'])
        self.assertEqual(result['review_answer'], {'approved': True, 'feedback': ''})
        # 跨租户不可见：a 的评审不会被 b 读到
        self.assertTrue(result['review_cross_tenant'])

        # 部署级资产
        self.assertTrue(result['assets_synced'])
        self.assertTrue(result['patch_has_bridge'])
        self.assertTrue(result['patch_has_never'])
        self.assertTrue(result['patch_has_workspace_root'])
        self.assertTrue(result['plugin_installed'])


if __name__ == '__main__':
    unittest.main()
