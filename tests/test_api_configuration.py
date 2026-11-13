import copy
import json
import tempfile
import unittest
import os
import subprocess
from pathlib import Path

from snapshot_pipeline.configuration import RuntimeConfigurationError, configure_tikhub_runtime


CONFIG = {"token_env":"SNAPSHOT_EGRESS_PROXY", "provider":{
    "source_name":"Example Publisher", "expected_biz":"synthetic-biz", "cookies":[]}}
PROFILE = {"provider":{"expected_biz":"synthetic-biz"}, "intelligence_intake":{
    "publisher":"Example Publisher", "tikhub_username":"gh_fixture123",
    "enabled":True, "verification_status":"verified"}}


class APIConfigurationTests(unittest.TestCase):
    def test_missing_api_credential_fails_before_collection_and_cleans_working_files(self):
        script=Path(__file__).resolve().parents[1]/'scripts/run_scheduled.sh'
        with tempfile.TemporaryDirectory() as directory:
            environment={'PATH':os.defpath,'RUNNER_TEMP':directory,
                'SNAPSHOT_COLLECTION_METHOD':'tikhub','RUNTIME_AGE_IDENTITY':'synthetic'}
            result=subprocess.run(['bash',str(script)],env=environment,capture_output=True,text=True,timeout=5)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('api-profile-or-credential-unavailable',result.stderr)
            self.assertNotIn('stage=tool',result.stdout)
            self.assertEqual(list(Path(directory).iterdir()),[])

    def configure(self, root, profile, limit=10):
        config_path, profile_path = root/'config.json', root/'profile.json'
        config_path.write_text(json.dumps(CONFIG))
        profile_path.write_text(json.dumps(profile))
        configure_tikhub_runtime(config_path, profile_path, limit)
        return json.loads(config_path.read_text())

    def test_approved_matching_source_switches_only_private_runtime_to_api(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.configure(Path(directory),PROFILE)
            self.assertEqual(result['token_env'],'TIKHUB_WECHAT_TOKEN')
            self.assertEqual(result['provider']['collection_method'],'tikhub')
            self.assertEqual(result['provider']['max_new_articles_per_run'],10)
            self.assertEqual(result['intelligence_intake'],PROFILE['intelligence_intake'])
            self.assertEqual(CONFIG['token_env'],'SNAPSHOT_EGRESS_PROXY')

    def test_unverified_disabled_or_cross_account_profiles_are_rejected_without_changes(self):
        variants=[]
        for key,value in [('enabled',False),('verification_status','pending'),
                          ('publisher','Other Publisher'),('tikhub_username','invalid')]:
            profile=copy.deepcopy(PROFILE);profile['intelligence_intake'][key]=value
            variants.append(profile)
        profile=copy.deepcopy(PROFILE);profile['provider']['expected_biz']='other-biz'
        variants.append(profile)
        for profile in variants:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                with self.assertRaises(RuntimeConfigurationError):
                    self.configure(root,profile)
                self.assertEqual(json.loads((root/'config.json').read_text()),CONFIG)

    def test_article_limit_is_bounded(self):
        for limit in (0,21,True):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(RuntimeConfigurationError):
                    self.configure(Path(directory),PROFILE,limit)

    def test_malformed_profiles_do_not_replace_original_configuration(self):
        for profile in ([], {}, {'provider':{},'intelligence_intake':[]}):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                with self.assertRaises(RuntimeConfigurationError):
                    self.configure(root,profile)
                self.assertEqual(json.loads((root/'config.json').read_text()),CONFIG)
