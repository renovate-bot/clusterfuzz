# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for upload_testcase."""

import base64
import datetime
import io
import json
import os
import unittest

import flask

from clusterfuzz._internal.datastore import data_handler
from clusterfuzz._internal.datastore import data_types
from clusterfuzz._internal.metrics import events
from clusterfuzz._internal.tests.test_libs import helpers as test_helpers
from clusterfuzz._internal.tests.test_libs import test_utils
from handlers import upload_testcase
from libs import helpers

DATA_DIRECTORY = os.path.join(os.path.dirname(__file__), 'upload_testcase_data')


@test_utils.with_cloud_emulators('datastore')
class FindFuzzTargetTest(unittest.TestCase):
  """Tests for find_fuzz_target."""

  def setUp(self):
    test_helpers.patch_environ(self)

    data_types.FuzzTarget(
        engine='libFuzzer', project='test-project', binary='binary').put()

    data_types.FuzzTarget(
        engine='libFuzzer', project='proj', binary='binary').put()

  def test_without_project_prefix(self):
    """Test find_fuzz_target with a target_name that isn't prefixed with the
    project."""
    data_types.Job(name='job', environment_string='PROJECT_NAME = proj\n').put()
    self.assertEqual(('libFuzzer_proj_binary', 'binary'),
                     upload_testcase.find_fuzz_target('libFuzzer', 'binary',
                                                      'job'))

  def test_with_project_prefix(self):
    """Test find_fuzz_target with a target_name that is prefixed with the
    project."""
    data_types.Job(name='job', environment_string='PROJECT_NAME = proj\n').put()
    self.assertEqual(('libFuzzer_proj_binary', 'binary'),
                     upload_testcase.find_fuzz_target('libFuzzer',
                                                      'proj_binary', 'job'))

  def test_with_main_project(self):
    """Test find_fuzz_target with a target in the main project."""
    data_types.Job(name='job', environment_string='').put()
    self.assertEqual(('libFuzzer_binary', 'binary'),
                     upload_testcase.find_fuzz_target('libFuzzer', 'binary',
                                                      'job'))

  def test_not_found(self):
    """Test target not found."""
    data_types.Job(name='job', environment_string='').put()
    with self.assertRaises(helpers.EarlyExitError):
      self.assertEqual((None, None),
                       upload_testcase.find_fuzz_target('libFuzzer', 'notfound',
                                                        'job'))


# pylint: disable=protected-access
@test_utils.with_cloud_emulators('datastore')
class UploadOAuthTest(unittest.TestCase):
  """OAuth upload tests."""

  def setUp(self):
    self.maxDiff = None
    test_helpers.patch_environ(self)
    test_helpers.patch(self, [
        'clusterfuzz._internal.base.tasks.add_task',
        'clusterfuzz._internal.google_cloud_utils.blobs.get_blob_info',
        'clusterfuzz._internal.google_cloud_utils.blobs.write_blob',
        'libs.access.has_access',
        'libs.helpers.get_user_email',
        'clusterfuzz._internal.base.utils.current_date_time',
        'clusterfuzz._internal.base.utils.utcnow',
        'clusterfuzz._internal.metrics.events.emit',
        'clusterfuzz._internal.metrics.events._get_datetime_now',
    ])

    self.mock._get_datetime_now.return_value = datetime.datetime(2025, 1, 1)

    self.mock.get_blob_info.return_value.filename = 'input'
    self.mock.get_blob_info.return_value.key.return_value = 'blob_key'
    self.mock.write_blob.return_value = 'blob_key'
    self.mock.has_access.return_value = True
    self.mock.current_date_time.return_value = '2021-01-01 00:00:00 UTC'
    self.mock.utcnow.return_value = datetime.datetime(2021, 1, 1)
    self.mock.get_user_email.return_value = 'uploader@email'

    data_types.FuzzTarget(
        engine='libFuzzer', project='test-project', binary='binary').put()

    data_types.Job(
        name='libfuzzer_proj_external',
        environment_string='PROJECT_NAME = proj',
        platform='LINUX',
        external_reproduction_topic='topic',
        external_updates_subscription='sub').put()

    self.app = flask.Flask('testflask')
    self.app.add_url_rule(
        '/', view_func=upload_testcase.UploadHandlerOAuth.as_view(''))

  def _read_test_data(self, name):
    """Helper function to read test data."""
    with open(os.path.join(DATA_DIRECTORY, name)) as handle:
      return handle.read()

  def assert_dict_has_items(self, expected, actual):
    """Assert that all items in `expected` are in `actual`."""
    for key, value in expected.items():
      self.assertEqual(value, actual[key], msg=f'For attribute {key}')

  def test_external_upload_oom(self):
    """Test external upload (oom)."""
    stacktrace = self._read_test_data('oom.txt')
    with self.app.test_client() as client:
      response = client.post(
          '/',
          data={
              'job': 'libfuzzer_proj_external',
              'target': 'target',
              'stacktrace': stacktrace,
              'revision': 1337,
              'file': (io.BytesIO(b'contents'), 'file'),
          })

    self.assertDictEqual({
        'id': '2',
        'uploadUrl': 'http://localhost//upload-testcase/upload-oauth'
    }, response.json)

    testcase = data_handler.get_testcase_by_id(2)

    self.mock.emit.assert_called_once_with(
        events.TestcaseCreationEvent(
            testcase=testcase,
            creation_origin=events.TestcaseOrigin.MANUAL_UPLOAD,
            uploader='uploader@email'))

    self.assert_dict_has_items({
        'absolute_path': 'input',
        'additional_metadata': '{"fuzzer_binary_name": "target"}',
        'archive_filename': None,
        'archive_state': 0,
        'bug_information': '',
        'comments': '[2021-01-01 00:00:00 UTC] uploader@email: '
                    'External testcase upload.\n',
        'crash_address': '',
        'crash_revision': 1337,
        'crash_stacktrace': stacktrace,
        'crash_state': 'target\n',
        'crash_type': 'Out-of-memory',
        'disable_ubsan': False,
        'duplicate_of': None,
        'fixed': '',
        'flaky_stack': False,
        'fuzzed_keys': 'blob_key',
        'fuzzer_name': 'libFuzzer',
        'gestures': [],
        'group_bug_information': 0,
        'group_id': 0,
        'has_bug_flag': False,
        'http_flag': False,
        'impact_beta_version': None,
        'impact_beta_version_likely': None,
        'impact_stable_version': None,
        'impact_stable_version_likely': None,
        'is_a_duplicate_flag': False,
        'is_impact_set_flag': None,
        'is_leader': False,
        'job_type': 'libfuzzer_proj_external',
        'last_tested_crash_stacktrace': None,
        'minidump_keys': None,
        'minimized_arguments': '',
        'minimized_keys': 'NA',
        'one_time_crasher_flag': False,
        'open': True,
        'overridden_fuzzer_name': 'libFuzzer_proj_target',
        'platform': 'linux',
        'platform_id': 'linux',
        'project_name': 'proj',
        'queue': None,
        'redzone': 128,
        'regression': 'NA',
        'security_flag': False,
        'security_severity': None,
        'status': 'Processed',
        'symbolized': False,
        'timeout_multiplier': 1.0,
        'timestamp': datetime.datetime(2021, 1, 1),
        'triaged': False,
        'uploader_email': 'uploader@email',
        'window_argument': ''
    }, testcase._to_dict())

    metadata = data_types.TestcaseUploadMetadata.query(
        data_types.TestcaseUploadMetadata.testcase_id ==
        testcase.key.id()).get()
    self.assertIsNotNone(metadata)
    self.assertDictEqual({
        'additional_metadata_string': None,
        'blobstore_key': 'blob_key',
        'bot_name': None,
        'bug_information': '',
        'bug_summary_update_flag': False,
        'bundled': False,
        'duplicate_of': None,
        'filename': 'input',
        'original_blobstore_key': 'blob_key',
        'path_in_archive': None,
        'quiet_flag': False,
        'retries': None,
        'security_flag': False,
        'status': 'Confirmed',
        'testcase_id': 2,
        'timeout': 0,
        'timestamp': datetime.datetime(2021, 1, 1, 0, 0),
        'uploader_email': 'uploader@email'
    }, metadata._to_dict())

  def test_external_upload_uaf(self):
    """Test external upload (uaf)."""
    stacktrace = self._read_test_data('uaf.txt')
    with self.app.test_client() as client:
      response = client.post(
          '/',
          data={
              'job': 'libfuzzer_proj_external',
              'target': 'target',
              'stacktrace': stacktrace,
              'revision': 1337,
              'file': (io.BytesIO(b'contents'), 'file'),
          })

    self.assertDictEqual({
        'id': '2',
        'uploadUrl': 'http://localhost//upload-testcase/upload-oauth'
    }, response.json)

    testcase = data_handler.get_testcase_by_id(2)
    self.mock.emit.assert_called_once_with(
        events.TestcaseCreationEvent(
            testcase=testcase,
            creation_origin=events.TestcaseOrigin.MANUAL_UPLOAD,
            uploader='uploader@email'))

    self.assert_dict_has_items({
        'absolute_path':
            'input',
        'additional_metadata':
            '{"fuzzer_binary_name": "target"}',
        'archive_filename':
            None,
        'archive_state':
            0,
        'bug_information':
            '',
        'comments':
            '[2021-01-01 00:00:00 UTC] uploader@email: '
            'External testcase upload.\n',
        'crash_address':
            '0x60f00003b280',
        'crash_revision':
            1337,
        'crash_stacktrace':
            stacktrace,
        'crash_state': ('blink::InputTypeView::element\n'
                        'blink::TextFieldInputType::didSetValueByUserEdit\n'
                        'blink::TextFieldInputType::subtreeHasChanged\n'),
        'crash_type':
            'Heap-use-after-free\nREAD 8',
        'disable_ubsan':
            False,
        'duplicate_of':
            None,
        'fixed':
            '',
        'flaky_stack':
            False,
        'fuzzed_keys':
            'blob_key',
        'fuzzer_name':
            'libFuzzer',
        'gestures': [],
        'group_bug_information':
            0,
        'group_id':
            0,
        'has_bug_flag':
            False,
        'http_flag':
            False,
        'impact_beta_version':
            None,
        'impact_beta_version_likely':
            None,
        'impact_stable_version':
            None,
        'impact_stable_version_likely':
            None,
        'is_a_duplicate_flag':
            False,
        'is_impact_set_flag':
            None,
        'is_leader':
            False,
        'job_type':
            'libfuzzer_proj_external',
        'last_tested_crash_stacktrace':
            None,
        'minidump_keys':
            None,
        'minimized_arguments':
            '',
        'minimized_keys':
            'NA',
        'one_time_crasher_flag':
            False,
        'open':
            True,
        'overridden_fuzzer_name':
            'libFuzzer_proj_target',
        'platform':
            'linux',
        'platform_id':
            'linux',
        'project_name':
            'proj',
        'queue':
            None,
        'redzone':
            128,
        'regression':
            'NA',
        'security_flag':
            True,
        'security_severity':
            None,
        'status':
            'Processed',
        'symbolized':
            False,
        'timeout_multiplier':
            1.0,
        'timestamp':
            datetime.datetime(2021, 1, 1),
        'triaged':
            False,
        'uploader_email':
            'uploader@email',
        'window_argument':
            ''
    }, testcase._to_dict())

    metadata = data_types.TestcaseUploadMetadata.query(
        data_types.TestcaseUploadMetadata.testcase_id ==
        testcase.key.id()).get()
    self.assertIsNotNone(metadata)
    self.assertDictEqual({
        'additional_metadata_string': None,
        'blobstore_key': 'blob_key',
        'bot_name': None,
        'bug_information': '',
        'bug_summary_update_flag': False,
        'bundled': False,
        'duplicate_of': None,
        'filename': 'input',
        'original_blobstore_key': 'blob_key',
        'path_in_archive': None,
        'quiet_flag': False,
        'retries': None,
        'security_flag': True,
        'status': 'Confirmed',
        'testcase_id': 2,
        'timeout': 0,
        'timestamp': datetime.datetime(2021, 1, 1, 0, 0),
        'uploader_email': 'uploader@email'
    }, metadata._to_dict())

  def test_external_duplicate(self):
    """Test uploading a duplicate."""
    existing = data_types.Testcase(
        crash_address='',
        crash_state='target\n',
        crash_type='Out-of-memory',
        project_name='proj',
        minimized_keys='NA',
        security_flag=False)
    existing.put()

    stacktrace = self._read_test_data('oom.txt')
    with self.app.test_client() as client:
      response = client.post(
          '/',
          data={
              'job': 'libfuzzer_proj_external',
              'target': 'target',
              'stacktrace': stacktrace,
              'revision': 1337,
              'file': (io.BytesIO(b'contents'), 'file'),
          })

    self.assertDictEqual({
        'id': '3',
        'uploadUrl': 'http://localhost//upload-testcase/upload-oauth'
    }, response.json)

    testcase = data_handler.get_testcase_by_id(3)
    self.mock.emit.assert_called_once_with(
        events.TestcaseCreationEvent(
            testcase=testcase,
            creation_origin=events.TestcaseOrigin.MANUAL_UPLOAD,
            uploader='uploader@email'))

    self.assert_dict_has_items({
        'absolute_path': 'input',
        'additional_metadata': '{"fuzzer_binary_name": "target"}',
        'archive_filename': None,
        'archive_state': 0,
        'bug_information': '',
        'comments': '[2021-01-01 00:00:00 UTC] uploader@email: '
                    'External testcase upload.\n',
        'crash_address': '',
        'crash_revision': 1337,
        'crash_stacktrace': stacktrace,
        'crash_state': 'target\n',
        'crash_type': 'Out-of-memory',
        'disable_ubsan': False,
        'duplicate_of': 2,
        'fixed': 'NA',
        'flaky_stack': False,
        'fuzzed_keys': 'blob_key',
        'fuzzer_name': 'libFuzzer',
        'gestures': [],
        'group_bug_information': 0,
        'group_id': 0,
        'has_bug_flag': False,
        'http_flag': False,
        'impact_beta_version': None,
        'impact_beta_version_likely': False,
        'impact_stable_version': None,
        'impact_stable_version_likely': False,
        'is_a_duplicate_flag': True,
        'is_impact_set_flag': False,
        'is_leader': False,
        'job_type': 'libfuzzer_proj_external',
        'last_tested_crash_stacktrace': None,
        'minidump_keys': None,
        'minimized_arguments': '',
        'minimized_keys': 'NA',
        'one_time_crasher_flag': False,
        'open': False,
        'overridden_fuzzer_name': 'libFuzzer_proj_target',
        'platform': 'linux',
        'platform_id': 'linux',
        'project_name': 'proj',
        'queue': None,
        'redzone': 128,
        'regression': 'NA',
        'security_flag': False,
        'security_severity': None,
        'status': 'Duplicate',
        'symbolized': False,
        'timeout_multiplier': 1.0,
        'timestamp': datetime.datetime(2021, 1, 1),
        'triaged': True,
        'uploader_email': 'uploader@email',
        'window_argument': ''
    }, testcase._to_dict())

    metadata = data_types.TestcaseUploadMetadata.query(
        data_types.TestcaseUploadMetadata.testcase_id ==
        testcase.key.id()).get()
    self.assertIsNotNone(metadata)
    self.assertDictEqual({
        'additional_metadata_string': None,
        'blobstore_key': 'blob_key',
        'bot_name': None,
        'bug_information': '',
        'bug_summary_update_flag': False,
        'bundled': False,
        'duplicate_of': 2,
        'filename': 'input',
        'original_blobstore_key': 'blob_key',
        'path_in_archive': None,
        'quiet_flag': False,
        'retries': None,
        'security_flag': False,
        'status': 'Duplicate',
        'testcase_id': 3,
        'timeout': 0,
        'timestamp': datetime.datetime(2021, 1, 1, 0, 0),
        'uploader_email': 'uploader@email'
    }, metadata._to_dict())

  def test_trusted_uploader_post(self):
    """Test trusted uploader post."""
    email = 'uploader@email'
    data_types.Config(privileged_users=email).put()
    data_types.ExternalUserPermission(
        email=email,
        entity_name=None,
        entity_kind=data_types.PermissionEntityKind.UPLOADER,
        is_prefix=False,
        auto_cc=data_types.AutoCCType.NONE).put()
    data_types.Job(
        name='libfuzzer_proj',
        environment_string='PROJECT_NAME = proj',
        platform='LINUX').put()
    with self.app.test_client() as client:
      response = client.post(
          '/',
          data={
              'job': 'libfuzzer_proj',
              'target': 'target',
              'revision': 1337,
              'file': (io.BytesIO(b'contents'), 'file'),
          })

    self.assertDictEqual({
        'id': '5',
        'uploadUrl': 'http://localhost//upload-testcase/upload-oauth'
    }, response.json)

    testcase = data_handler.get_testcase_by_id(5)
    self.mock.emit.assert_called_once_with(
        events.TestcaseCreationEvent(
            testcase=testcase,
            creation_origin=events.TestcaseOrigin.MANUAL_UPLOAD,
            uploader='uploader@email'))

    self.assert_dict_has_items({
        'absolute_path': 'input',
        'additional_metadata': '{"fuzzer_binary_name": "target"}',
        'archive_filename': None,
        'archive_state': 0,
        'bug_information': '',
        'comments': '[2021-01-01 00:00:00 UTC] uploader@email: '
                    'Analyze task.\n',
        'crash_address': '',
        'crash_revision': 1337,
        'crash_stacktrace': '',
        'crash_state': 'Pending',
        'crash_type': '',
        'disable_ubsan': False,
        'duplicate_of': None,
        'fixed': '',
        'flaky_stack': False,
        'fuzzed_keys': 'blob_key',
        'fuzzer_name': 'libFuzzer',
        'gestures': [],
        'github_issue_num': None,
        'github_repo_id': None,
        'group_bug_information': 0,
        'group_id': 0,
        'has_bug_flag': False,
        'http_flag': False,
        'impact_beta_version': None,
        'impact_beta_version_indices': [],
        'impact_beta_version_likely': None,
        'impact_extended_stable_version': None,
        'impact_extended_stable_version_indices': [],
        'impact_extended_stable_version_likely': None,
        'impact_head_version': None,
        'impact_head_version_indices': [],
        'impact_head_version_likely': None,
        'impact_indices': [],
        'impact_stable_version': None,
        'impact_stable_version_indices': [],
        'impact_stable_version_likely': None,
        'impact_version_indices': [],
        'is_a_duplicate_flag': False,
        'is_impact_set_flag': None,
        'is_leader': False,
        'job_type': 'libfuzzer_proj',
        'last_tested_crash_stacktrace': None,
        'minidump_keys': None,
        'minimized_arguments': '',
        'minimized_keys': '',
        'one_time_crasher_flag': False,
        'open': True,
        'overridden_fuzzer_name': 'libFuzzer_proj_target',
        'platform': None,
        'platform_id': None,
        'project_name': 'proj',
        'queue': None,
        'redzone': 128,
        'regression': '',
        'security_flag': False,
        'security_severity': None,
        'status': 'Pending',
        'symbolized': False,
        'timeout_multiplier': 1.0,
        'timestamp': datetime.datetime(2021, 1, 1, 0, 0),
        'triaged': False,
        'uploader_email': 'uploader@email',
        'window_argument': '',
        'trusted': False,
    }, testcase._to_dict())

    metadata = data_types.TestcaseUploadMetadata.query(
        data_types.TestcaseUploadMetadata.testcase_id ==
        testcase.key.id()).get()
    self.assertIsNotNone(metadata)
    self.assertDictEqual({
        'additional_metadata_string': None,
        'blobstore_key': 'blob_key',
        'bot_name': None,
        'bug_information': '',
        'bug_summary_update_flag': False,
        'bundled': False,
        'duplicate_of': None,
        'filename': 'input',
        'original_blobstore_key': 'blob_key',
        'path_in_archive': None,
        'quiet_flag': False,
        'retries': None,
        'security_flag': False,
        'status': 'Pending',
        'testcase_id': 5,
        'timeout': 0,
        'timestamp': datetime.datetime(2021, 1, 1, 0, 0),
        'uploader_email': 'uploader@email'
    }, metadata._to_dict())
    self.assertDictEqual({
        'binary': 'target',
        'engine': 'libFuzzer',
        'project': 'proj'
    },
                         data_handler.get_fuzz_target('libFuzzer_proj_target')
                         ._to_dict())


# pylint: disable=protected-access
@test_utils.with_cloud_emulators('datastore')
class CrashReplicationTest(unittest.TestCase):
  """OAuth upload tests."""

  def setUp(self):
    self.maxDiff = None
    self.service_account_email = 'compute-sa@project.com'
    self.job_name = 'libfuzzer_proj'
    self.fuzz_target_binary = 'binary'
    self.fuzzer_name = 'libFuzzer'
    test_helpers.patch_environ(self)
    test_helpers.patch(
        self,
        [
            'clusterfuzz._internal.base.utils.service_account_email',
            'clusterfuzz._internal.base.tasks.add_task',  # Publish message becomes no op
            'clusterfuzz._internal.google_cloud_utils.blobs.get_blob_info',
            'clusterfuzz._internal.metrics.events.emit',
            'clusterfuzz._internal.metrics.events._get_datetime_now',
            'libs.auth.get_email_from_bearer_token',
            'libs.helpers.get_user_email',
        ])
    self.mock.get_email_from_bearer_token.return_value = self.service_account_email
    self.mock.service_account_email.return_value = self.service_account_email
    self.mock.get_user_email.return_value = self.service_account_email
    self.mock.get_blob_info.return_value.filename = 'input'
    self.mock.get_blob_info.return_value.key.return_value = 'blob_key'
    self.mock._get_datetime_now.return_value = datetime.datetime(2025, 1, 1)

    data_types.FuzzTarget(
        engine='libFuzzer', project='proj',
        binary=self.fuzz_target_binary).put()

    data_types.Job(
        name=self.job_name,
        environment_string='PROJECT_NAME = proj',
        platform='LINUX',
        external_reproduction_topic=None,
        external_updates_subscription=None).put()

    # Make the appengine SA a privileged user to be able to upload testcases
    data_types.Config(privileged_users=self.service_account_email).put()

    self.app = flask.Flask('testflask')
    self.app.add_url_rule(
        '/upload-testcase/crash-replication',
        view_func=upload_testcase.CrashReplicationUploadHandler.as_view(''))

  def assert_dict_has_items(self, expected, actual):
    """Assert that all items in `expected` are in `actual`."""
    for key, value in expected.items():
      self.assertEqual(value, actual[key], msg=f'For attribute {key}')

  def _make_message(self, data, attributes):
    """Make a message."""
    return json.dumps({
        'message': {
            'data': base64.b64encode(data).decode(),
            'attributes': attributes,
        }
    })

  def test_crash_replication_upload(self):
    """Tests if a sampling message correct generates a testcase through the
        crash-replication endpoint."""

    sampling_message_data = json.dumps({
        'fuzzed_key': 'some-fuzzed-key',
        'job': self.job_name,
        'fuzzer': self.fuzzer_name,
        'target_name': self.fuzz_target_binary,
        'arguments': 'some-arg',
        'application_command_line': 'some-cli-command',
        'gestures': str([]),
        'http_flag': False,
        'original_task_id': 'some-task-id',
    }).encode()

    with self.app.test_client() as client:
      result = client.post(
          '/upload-testcase/crash-replication',
          data=self._make_message(sampling_message_data, {}),
          headers={'Authorization': 'Bearer fake'})
      print(result)

    self.assertEqual('200 OK', result.status)
    self.assertTrue(result.json.get('id', None))

    created_testcase_id = int(result.json['id'])
    testcase = data_handler.get_testcase_by_id(created_testcase_id)

    self.mock.emit.assert_called_once_with(
        events.TestcaseCreationEvent(
            testcase=testcase,
            creation_origin=events.TestcaseOrigin.MANUAL_UPLOAD,
            uploader=self.service_account_email))

    self.assert_dict_has_items({
        'absolute_path':
            'input',
        'additional_metadata': ('{"app_launch_command": "some-cli-command", '
                                '"fuzzer_binary_name": "binary", '
                                '"uploaded_additional_args": "some-arg"}'),
        'fuzzed_keys':
            'blob_key',
        'fuzzer_name':
            'libFuzzer',
        'gestures': [],
        'job_type':
            'libfuzzer_proj',
        'overridden_fuzzer_name':
            'libFuzzer_proj_binary',
        'project_name':
            'proj',
        'uploader_email':
            self.service_account_email,
    }, testcase._to_dict())

    metadata = data_types.TestcaseUploadMetadata.query(
        data_types.TestcaseUploadMetadata.testcase_id ==
        testcase.key.id()).get()
    self.assertIsNotNone(metadata)
    self.assert_dict_has_items({
        'blobstore_key': 'blob_key',
        'original_blobstore_key': 'blob_key',
        'filename': 'input',
        'testcase_id': created_testcase_id,
        'uploader_email': self.service_account_email,
    }, metadata.to_dict())
