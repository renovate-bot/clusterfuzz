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
"""fuzz_task tests."""
# pylint: disable=protected-access

import datetime
import json
import os
import queue
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import parameterized
from pyfakefs import fake_filesystem_unittest

from clusterfuzz import stacktraces
from clusterfuzz._internal.base import utils
from clusterfuzz._internal.bot import testcase_manager
from clusterfuzz._internal.bot.fuzzers.libFuzzer import \
    engine as libfuzzer_engine
from clusterfuzz._internal.bot.tasks.utasks import fuzz_task
from clusterfuzz._internal.bot.tasks.utasks import uworker_io
from clusterfuzz._internal.bot.untrusted_runner import file_host
from clusterfuzz._internal.build_management import build_manager
from clusterfuzz._internal.datastore import data_handler
from clusterfuzz._internal.datastore import data_types
from clusterfuzz._internal.fuzzing import corpus_manager
from clusterfuzz._internal.google_cloud_utils import big_query
from clusterfuzz._internal.metrics import events
from clusterfuzz._internal.metrics import monitor
from clusterfuzz._internal.metrics import monitoring_metrics
from clusterfuzz._internal.protos import uworker_msg_pb2
from clusterfuzz._internal.system import environment
from clusterfuzz._internal.tests.test_libs import helpers
from clusterfuzz._internal.tests.test_libs import test_utils
from clusterfuzz._internal.tests.test_libs import untrusted_runner_helpers
from clusterfuzz.fuzz import engine


class TrackFuzzerRunResultTest(unittest.TestCase):
  """Test _track_fuzzer_run_result."""

  def setUp(self):
    monitor.metrics_store().reset_for_testing()
    helpers.patch(self, ['clusterfuzz._internal.system.environment.platform'])
    self.mock.platform.return_value = 'some_platform'

  def test_fuzzer_run_result(self):
    """Ensure _track_fuzzer_run_result set the right metrics."""
    fuzz_task._track_fuzzer_run_result('fuzzer', 'job', 10, 100, 2)
    fuzz_task._track_fuzzer_run_result('fuzzer', 'job', 100, 200, 2)
    fuzz_task._track_fuzzer_run_result('fuzzer', 'job', 1000, 2000, 2)
    fuzz_task._track_fuzzer_run_result('fuzzer', 'job', 1000, 500, 0)
    fuzz_task._track_fuzzer_run_result('fuzzer', 'job', 0, 1000, -1)
    fuzz_task._track_fuzzer_run_result('fuzzer', 'job', 0, 0, 2)

    self.assertEqual(
        4,
        monitoring_metrics.FUZZER_RETURN_CODE_COUNT.get({
            'fuzzer': 'fuzzer',
            'return_code': 2,
            'platform': 'some_platform',
            'job': 'job',
        }))
    self.assertEqual(
        1,
        monitoring_metrics.FUZZER_RETURN_CODE_COUNT.get({
            'fuzzer': 'fuzzer',
            'return_code': 0,
            'platform': 'some_platform',
            'job': 'job',
        }))
    self.assertEqual(
        1,
        monitoring_metrics.FUZZER_RETURN_CODE_COUNT.get({
            'fuzzer': 'fuzzer',
            'return_code': -1,
            'platform': 'some_platform',
            'job': 'job',
        }))

    testcase_count_ratio = (
        monitoring_metrics.FUZZER_TESTCASE_COUNT_RATIO.get({
            'fuzzer': 'fuzzer'
        }))
    self.assertEqual(3.1, testcase_count_ratio.sum)
    self.assertEqual(5, testcase_count_ratio.count)

    expected_buckets = [0 for _ in range(22)]
    expected_buckets[1] = 1
    expected_buckets[3] = 1
    expected_buckets[11] = 2
    expected_buckets[21] = 1
    self.assertListEqual(expected_buckets, testcase_count_ratio.buckets)


class TrackBuildRunResultTest(unittest.TestCase):
  """Test _track_build_run_result."""

  def setUp(self):
    monitor.metrics_store().reset_for_testing()

  def test_build_run_result(self):
    """Ensure _track_build_run_result set the right metrics."""
    fuzz_task._track_build_run_result('name', 10000, True)
    fuzz_task._track_build_run_result('name', 10001, True)
    fuzz_task._track_build_run_result('name', 10002, False)

    self.assertEqual(
        2,
        monitoring_metrics.JOB_BAD_BUILD_COUNT.get({
            'job': 'name',
            'bad_build': True
        }))
    self.assertEqual(
        1,
        monitoring_metrics.JOB_BAD_BUILD_COUNT.get({
            'job': 'name',
            'bad_build': False
        }))


class TrackTestcaseRunResultTest(unittest.TestCase):
  """Test _track_testcase_run_result."""

  def setUp(self):
    monitor.metrics_store().reset_for_testing()
    helpers.patch(self, ['clusterfuzz._internal.system.environment.platform'])
    self.mock.platform.return_value = 'some_platform'

  def test_testcase_run_result(self):
    """Ensure _track_testcase_run_result sets the right metrics."""
    fuzz_task._track_testcase_run_result('fuzzer', 'job', 2, 5)
    fuzz_task._track_testcase_run_result('fuzzer', 'job', 5, 10)

    self.assertEqual(
        7,
        monitoring_metrics.JOB_NEW_CRASH_COUNT.get({
            'job': 'job',
            'platform': 'some_platform',
        }))
    self.assertEqual(
        15,
        monitoring_metrics.JOB_KNOWN_CRASH_COUNT.get({
            'job': 'job',
            'platform': 'some_platform',
        }))
    self.assertEqual(
        7,
        monitoring_metrics.FUZZER_NEW_CRASH_COUNT.get({
            'fuzzer': 'fuzzer',
            'platform': 'some_platform',
        }))
    self.assertEqual(
        15,
        monitoring_metrics.FUZZER_KNOWN_CRASH_COUNT.get({
            'fuzzer': 'fuzzer',
            'platform': 'some_platform',
        }))


class TruncateFuzzerOutputTest(unittest.TestCase):
  """Truncate fuzzer output tests."""

  def test_no_truncation(self):
    """No truncation."""
    self.assertEqual('aaaa', fuzz_task.truncate_fuzzer_output('aaaa', 10))

  def test_truncation(self):
    """Truncate."""
    self.assertEqual(
        '123456\n...truncated...\n54321',
        fuzz_task.truncate_fuzzer_output(
            '123456xxxxxxxxxxxxxxxxxxxxxxxxxxx54321', 28))

  def test_error(self):
    """Error if limit is too low."""
    with self.assertRaises(AssertionError):
      self.assertEqual(
          '', fuzz_task.truncate_fuzzer_output('123456xxxxxx54321', 10))


class TrackFuzzTimeTest(unittest.TestCase):
  """Test _TrackFuzzTime."""

  def setUp(self):
    monitor.metrics_store().reset_for_testing()
    helpers.patch(self, ['clusterfuzz._internal.system.environment.platform'])
    self.mock.platform.return_value = 'some_platform'

  def _test(self, timeout):
    """Test helper."""
    time_module = helpers.MockTime()
    with fuzz_task._TrackFuzzTime('fuzzer', 'job', time_module) as tracker:
      time_module.advance(5)
      tracker.timeout = timeout

    fuzzer_total_time = monitoring_metrics.FUZZER_TOTAL_FUZZ_TIME.get({
        'fuzzer': 'fuzzer',
        'timeout': timeout,
        'platform': 'some_platform',
    })
    self.assertEqual(5, fuzzer_total_time)

  def test_success(self):
    """Test report metrics."""
    self._test(False)

  def test_timeout(self):
    """Test timeout."""
    self._test(True)


class GetFuzzerMetadataFromOutputTest(unittest.TestCase):
  """Test get_fuzzer_metadata_from_output."""

  def test_no_metadata(self):
    """Tests no metadata in output."""
    data = 'abc\ndef\n123123'
    self.assertDictEqual(fuzz_task.get_fuzzer_metadata_from_output(data), {})

    data = ''
    self.assertDictEqual(fuzz_task.get_fuzzer_metadata_from_output(data), {})

  def test_metadata(self):
    """Tests parsing of metadata."""
    data = ('abc\n'
            'def\n'
            'metadata:invalid: invalid\n'
            'metadat::invalid: invalid\n'
            'metadata::foo: bar\n'
            '123123\n'
            'metadata::blah: 1\n'
            'metadata::test:abcd\n'
            'metadata::test2:   def\n')
    self.assertDictEqual(
        fuzz_task.get_fuzzer_metadata_from_output(data), {
            'blah': '1',
            'test': 'abcd',
            'test2': 'def',
            'foo': 'bar'
        })


class GetRegressionTest(unittest.TestCase):
  """Test get_regression."""

  def setUp(self):
    helpers.patch(self, [
        'clusterfuzz._internal.build_management.build_manager.is_custom_binary'
    ])

  def test_one_time_crasher(self):
    """Test when one_time_crasher_flag is True."""
    self.mock.is_custom_binary.return_value = False
    self.assertEqual('NA', fuzz_task.get_regression(True))

  def test_custom_binary(self):
    """Test for custom binary."""
    self.mock.is_custom_binary.return_value = True
    self.assertEqual('NA', fuzz_task.get_regression(False))

  def test_reproducible_non_custom_binary(self):
    """Test for reproducible non-custom binary."""
    self.mock.is_custom_binary.return_value = False
    self.assertEqual('', fuzz_task.get_regression(False))


class GetFixedOrMinimizedKeyTest(unittest.TestCase):
  """Test get_fixed_or_minimized_key."""

  def test_one_time_crasher(self):
    """Test when one_time_crasher_flag is True."""
    self.assertEqual('NA', fuzz_task.get_fixed_or_minimized_key(True))

  def test_reproducible(self):
    """Test for reproducible."""
    self.assertEqual('', fuzz_task.get_fixed_or_minimized_key(False))


class CrashInitTest(fake_filesystem_unittest.TestCase):
  """Test Crash.__init__."""

  def setUp(self):
    """Setup for crash init test."""
    helpers.patch(self, [
        'clusterfuzz._internal.bot.tasks.setup.archive_testcase_and_dependencies_in_gcs',
        'clusterfuzz._internal.crash_analysis.stack_parsing.stack_analyzer.get_crash_data',
        'clusterfuzz._internal.bot.testcase_manager.get_additional_command_line_flags',
        'clusterfuzz._internal.bot.testcase_manager.get_command_line_for_application',
        'clusterfuzz._internal.base.utils.get_crash_stacktrace_output',
        'clusterfuzz._internal.crash_analysis.crash_analyzer.ignore_stacktrace',
        'clusterfuzz._internal.crash_analysis.crash_analyzer.is_security_issue',
    ])
    helpers.patch_environ(self)
    test_utils.set_up_pyfakefs(self)

    self.mock.get_command_line_for_application.return_value = 'cmd'
    dummy_state = stacktraces.CrashInfo()
    dummy_state.crash_type = 'type'
    dummy_state.crash_address = 'address'
    dummy_state.crash_state = 'state'
    dummy_state.crash_stacktrace = 'orig_trace'
    dummy_state.frames = ['frame 1', 'frame 2']
    self.mock.get_crash_data.return_value = dummy_state
    self.mock.get_crash_stacktrace_output.return_value = 'trace'
    self.mock.archive_testcase_and_dependencies_in_gcs.return_value = (
        True, 'absolute_path', 'archive_filename')

    environment.set_value('FILTER_FUNCTIONAL_BUGS', False)

    with open('/stack_file_path', 'w') as f:
      f.write('unsym')

  def test_error(self):
    """Test failing to reading stacktrace file."""
    crash = fuzz_task.Crash.from_testcase_manager_crash(
        testcase_manager.Crash('dir/path-http-name', 123, 11, ['res'], 'ges',
                               '/no_stack_file'))
    self.assertIsNone(crash)

  def _test_crash(self, should_be_ignored, security_flag):
    """Test crash."""
    self.mock.get_command_line_for_application.reset_mock()
    self.mock.get_crash_data.reset_mock()
    self.mock.get_crash_stacktrace_output.reset_mock()
    self.mock.is_security_issue.reset_mock()
    self.mock.ignore_stacktrace.reset_mock()

    self.mock.is_security_issue.return_value = security_flag
    self.mock.ignore_stacktrace.return_value = should_be_ignored

    crash = fuzz_task.Crash.from_testcase_manager_crash(
        testcase_manager.Crash('dir/path-http-name', 123, 11, ['res'], 'ges',
                               '/stack_file_path'))

    self.assertEqual('dir/path-http-name', crash.file_path)
    self.assertEqual(123, crash.crash_time)
    self.assertEqual(11, crash.return_code)
    self.assertListEqual(['res'], crash.resource_list)
    self.assertEqual('ges', crash.gestures)

    self.assertEqual('path-http-name', crash.filename)
    self.assertTrue(crash.http_flag)

    self.assertEqual('cmd', crash.application_command_line)
    self.mock.get_command_line_for_application.assert_called_once_with(
        'dir/path-http-name', needs_http=True)

    self.assertEqual('unsym', crash.unsymbolized_crash_stacktrace)

    self.assertEqual('type', crash.crash_type)
    self.assertEqual('address', crash.crash_address)
    self.assertEqual('state', crash.crash_state)
    self.assertListEqual(['frame 1', 'frame 2'], crash.crash_frames)
    self.mock.get_crash_data.assert_called_once_with('unsym')

    self.assertEqual('trace', crash.crash_stacktrace)
    self.mock.get_crash_stacktrace_output.assert_called_once_with(
        'cmd', 'orig_trace', 'unsym')

    self.assertEqual(security_flag, crash.security_flag)
    self.mock.is_security_issue.assert_called_once_with('unsym', 'type',
                                                        'address')

    self.assertEqual('type,state,%s' % security_flag, crash.key)

    self.assertEqual(should_be_ignored, crash.should_be_ignored)
    self.mock.ignore_stacktrace.assert_called_once_with('orig_trace')

    self.assertFalse(crash.fuzzed_key)
    return crash

  def _test_validity_and_get_functional_crash(self):
    """Test validity of different crashes and return functional crash."""
    security_crash = self._test_crash(
        should_be_ignored=False, security_flag=True)
    self.assertIsNone(security_crash.get_error())
    self.assertTrue(security_crash.is_valid())

    ignored_crash = self._test_crash(should_be_ignored=True, security_flag=True)
    self.assertIn('False crash', ignored_crash.get_error())
    self.assertFalse(ignored_crash.is_valid())

    functional_crash = self._test_crash(
        should_be_ignored=False, security_flag=False)
    return functional_crash

  def test_valid_functional_bug(self):
    """Test valid because of functional bug."""
    functional_crash = self._test_validity_and_get_functional_crash()

    self.assertIsNone(functional_crash.get_error())
    self.assertTrue(functional_crash.is_valid())

  def test_invalid_functional_bug(self):
    """Test invalid because of functional bug."""
    environment.set_value('FILTER_FUNCTIONAL_BUGS', True)
    functional_crash = self._test_validity_and_get_functional_crash()

    self.assertIn('Functional crash', functional_crash.get_error())
    self.assertFalse(functional_crash.is_valid())

  def test_hydrate_fuzzed_key(self):
    """Test hydrating fuzzed_key."""
    crash = self._test_crash(should_be_ignored=False, security_flag=True)

    self.assertFalse(crash.is_uploaded())
    self.assertIsNone(crash.get_error())
    self.assertTrue(crash.is_valid())

    fuzzed_key = 'fuzzed_key'
    crash.archive_testcase_in_blobstore(
        uworker_msg_pb2.BlobUploadUrl(key=fuzzed_key))
    self.assertTrue(crash.is_uploaded())
    self.assertIsNone(crash.get_error())
    self.assertTrue(crash.is_valid())

    self.assertEqual(fuzzed_key, crash.fuzzed_key)
    self.assertEqual('absolute_path', crash.absolute_path)
    self.assertEqual('archive_filename', crash.archive_filename)

  def test_args_from_testcase_manager(self):
    """Test args from testcase_manager.Crash."""
    testcase_manager_crash = testcase_manager.Crash('path', 0, 0, [], [],
                                                    '/stack_file_path')
    self.mock.get_additional_command_line_flags.return_value = 'minimized'
    environment.set_value('APP_ARGS', 'app')

    crash = fuzz_task.Crash.from_testcase_manager_crash(testcase_manager_crash)
    self.assertEqual('app minimized', crash.arguments)


class CrashGroupTest(unittest.TestCase):
  """Test CrashGroup."""

  def setUp(self):
    helpers.patch(self, [
        'clusterfuzz._internal.bot.tasks.utasks.fuzz_task.find_main_crash',
        'clusterfuzz._internal.datastore.data_handler.find_testcase',
        'clusterfuzz._internal.datastore.data_handler.get_project_name',
    ])

    self.mock.get_project_name.return_value = 'some_project'
    self.crashes = [self._make_crash('g1'), self._make_crash('g2')]
    self.context = mock.MagicMock(
        test_timeout=99, fuzzer_name='test', fuzz_target=None)
    self.reproducible_testcase = self._make_testcase(
        project_name='some_project',
        bug_information='',
        one_time_crasher_flag=False)
    self.unreproducible_testcase = self._make_testcase(
        project_name='some_project',
        bug_information='',
        one_time_crasher_flag=True)

  def _make_crash(self, gestures):
    crash = mock.MagicMock(
        crash_type='type',
        crash_state='state',
        security_flag=True,
        file_path='file_path',
        http_flag=True,
        gestures=gestures)
    return crash

  def _make_testcase(self,
                     project_name,
                     bug_information,
                     one_time_crasher_flag,
                     timestamp=datetime.datetime.now()):
    """Make testcase."""
    testcase = data_types.Testcase()
    testcase.timestamp = timestamp
    testcase.one_time_crasher_flag = one_time_crasher_flag
    testcase.bug_information = bug_information
    testcase.project_name = project_name
    return testcase

  def test_no_existing_testcase(self):
    """Tests that is_new=True and _should_create_testcase returns True when
        there's no existing testcase."""
    self.mock.find_testcase.return_value = None
    self.mock.find_main_crash.return_value = self.crashes[0], True

    upload_urls = _get_upload_urls()
    group = fuzz_task.CrashGroup(self.crashes, self.context, upload_urls)

    self.assertTrue(fuzz_task._should_create_testcase(group, None))
    self.mock.find_main_crash.assert_called_once_with(
        self.crashes, None, self.context.test_timeout, upload_urls)

    self.assertEqual(self.crashes[0], group.main_crash)

  def test_has_existing_reproducible_testcase(self):
    """Tests that should_create_testcase returns False when there's an existing
      reproducible testcase."""
    self.mock.find_main_crash.return_value = (self.crashes[0], True)

    upload_urls = _get_upload_urls()
    group = fuzz_task.CrashGroup(self.crashes, self.context, upload_urls)

    self.assertEqual(self.crashes[0].gestures, group.main_crash.gestures)
    self.mock.find_main_crash.assert_called_once_with(
        self.crashes, None, self.context.test_timeout, upload_urls)
    # TODO(metzman): Replace group in calls to _should_create_testcase with a
    # proto group.
    self.assertFalse(
        fuzz_task._should_create_testcase(group, self.reproducible_testcase))

  def test_reproducible_crash(self):
    """should_create_testcase=True when the group is reproducible."""
    self.mock.find_main_crash.return_value = (self.crashes[0], False)

    upload_urls = _get_upload_urls()
    group = fuzz_task.CrashGroup(self.crashes, self.context, upload_urls)

    self.assertEqual(self.crashes[0].gestures, group.main_crash.gestures)
    self.mock.find_main_crash.assert_called_once_with(
        self.crashes, None, self.context.test_timeout, upload_urls)
    self.assertTrue(
        fuzz_task._should_create_testcase(group, self.unreproducible_testcase))
    self.assertFalse(group.one_time_crasher_flag)

  def test_has_existing_unreproducible_testcase(self):
    """should_create_testcase=False when the unreproducible testcase already
    exists."""
    self.mock.find_main_crash.return_value = (self.crashes[0], True)

    upload_urls = _get_upload_urls()
    group = fuzz_task.CrashGroup(self.crashes, self.context, upload_urls)

    self.assertFalse(
        fuzz_task._should_create_testcase(group, self.unreproducible_testcase))

    self.assertEqual(self.crashes[0].gestures, group.main_crash.gestures)
    self.mock.find_main_crash.assert_called_once_with(
        self.crashes, None, self.context.test_timeout, upload_urls)
    self.assertTrue(group.one_time_crasher_flag)


class FindMainCrashTest(unittest.TestCase):
  """Test find_main_crash."""

  def setUp(self):
    helpers.patch(self, [
        'clusterfuzz._internal.bot.testcase_manager.test_for_reproducibility',
        'clusterfuzz._internal.datastore.data_handler.get_fuzz_target',
    ])
    self.crashes = [
        self._make_crash('g1'),
        self._make_crash('g2'),
        self._make_crash('g3'),
        self._make_crash('g4')
    ]
    self.reproducible_crashes = []

    # pylint: disable=unused-argument
    def test_for_repro(fuzz_target,
                       file_path,
                       crash_type,
                       state,
                       security_flag,
                       test_timeout,
                       http_flag,
                       gestures,
                       arguments=None):
      """Mock test_for_reproducibility."""
      for c in self.reproducible_crashes:
        if c.gestures == gestures:
          return True
      return False

    self.mock.test_for_reproducibility.side_effect = test_for_repro

  def _make_crash(self, gestures):
    crash = mock.Mock(
        file_path='file_path',
        crash_state='state',
        security_flag=True,
        test_timeout=999,
        gestures=gestures)
    return crash

  def test_reproducible_crash(self):
    """Find that the 2nd crash is reproducible."""
    for c in self.crashes:
      c.is_valid.return_value = True
    self.crashes[0].is_valid.return_value = False
    self.reproducible_crashes = [self.crashes[2]]

    self.assertEqual((self.crashes[2], False),
                     fuzz_task.find_main_crash(self.crashes, 'test', 99,
                                               _get_upload_urls()))

    self.assertEqual(self.crashes[0].archive_testcase_in_blobstore.call_count,
                     1)
    self.assertEqual(self.crashes[1].archive_testcase_in_blobstore.call_count,
                     1)
    self.assertEqual(self.crashes[2].archive_testcase_in_blobstore.call_count,
                     1)
    self.assertEqual(self.crashes[3].archive_testcase_in_blobstore.call_count,
                     0)

    # Calls for self.crashes[1] and self.crashes[2].
    self.assertEqual(2, self.mock.test_for_reproducibility.call_count)

  def test_unreproducible_crash(self):
    """No reproducible crash. Find the first valid one."""
    for c in self.crashes:
      c.is_valid.return_value = True
    self.crashes[0].is_valid.return_value = False
    self.reproducible_crashes = []

    result = fuzz_task.find_main_crash(self.crashes, 'test', 99,
                                       _get_upload_urls())
    self.assertEqual((self.crashes[1], True), result)

    # TODO(metzman): Figure out what weirdness is causing this not to work
    # properly.
    for crash in self.crashes:
      self.assertEqual(crash.archive_testcase_in_blobstore.call_count, 1)

    # Calls for every crash except self.crashes[0] because it's invalid.
    self.assertEqual(
        len(self.crashes) - 1, self.mock.test_for_reproducibility.call_count)

  def test_no_valid_crash(self):
    """No valid crash."""
    for c in self.crashes:
      c.is_valid.return_value = False
    self.reproducible_crashes = []

    result = fuzz_task.find_main_crash(self.crashes, 'test', 99,
                                       _get_upload_urls())
    self.assertEqual((None, None), result)

    # TODO(metzman): Figure out what weirdness is causing this not to work
    # properly.
    for crash in self.crashes:
      self.assertEqual(crash.archive_testcase_in_blobstore.call_count, 1)

    self.assertEqual(0, self.mock.test_for_reproducibility.call_count)


@test_utils.with_cloud_emulators('datastore')
class ProcessCrashesTest(fake_filesystem_unittest.TestCase):
  """Test process_crashes."""

  def setUp(self):
    """Setup for process crashes test."""
    helpers.patch(self, [
        'clusterfuzz._internal.bot.tasks.utasks.fuzz_task.get_unsymbolized_crash_stacktrace',
        'clusterfuzz._internal.bot.tasks.task_creation.create_tasks',
        'clusterfuzz._internal.bot.tasks.setup.archive_testcase_and_dependencies_in_gcs',
        'clusterfuzz._internal.crash_analysis.stack_parsing.stack_analyzer.get_crash_data',
        'clusterfuzz._internal.build_management.revisions.get_real_revision',
        'clusterfuzz._internal.bot.testcase_manager.get_command_line_for_application',
        'clusterfuzz._internal.bot.testcase_manager.test_for_reproducibility',
        'clusterfuzz._internal.base.utils.get_crash_stacktrace_output',
        'clusterfuzz._internal.crash_analysis.crash_analyzer.ignore_stacktrace',
        'clusterfuzz._internal.crash_analysis.crash_analyzer.is_security_issue',
        'clusterfuzz._internal.datastore.data_handler.get_issue_tracker_name',
        'clusterfuzz._internal.datastore.data_handler.get_project_name',
        'clusterfuzz._internal.google_cloud_utils.big_query.Client.insert',
        'clusterfuzz._internal.google_cloud_utils.big_query.get_api_client',
        'time.sleep', 'time.time'
    ])
    test_utils.set_up_pyfakefs(self)

    self.mock.time.return_value = 987

    self.mock.get_issue_tracker_name.return_value = 'some_issue_tracker'
    self.mock.get_project_name.return_value = 'some_project'
    self.mock.archive_testcase_and_dependencies_in_gcs.return_value = (
        True, 'absolute_path', 'archive_filename')

  def _make_crash(self, trace, state='state'):
    """Make crash."""
    self.mock.get_real_revision.return_value = 'this.is.fake.ver'

    self.mock.get_command_line_for_application.return_value = 'cmd'
    dummy_state = stacktraces.CrashInfo()
    dummy_state.crash_type = 'type'
    dummy_state.crash_address = 'address'
    dummy_state.crash_state = state
    dummy_state.crash_stacktrace = 'orig_trace'
    dummy_state.crash_frames = ['frame 1', 'frame 2']
    self.mock.get_crash_data.return_value = dummy_state
    self.mock.get_crash_stacktrace_output.return_value = trace
    self.mock.get_unsymbolized_crash_stacktrace.return_value = trace
    self.mock.is_security_issue.return_value = True
    self.mock.ignore_stacktrace.return_value = False

    with open('/stack_file_path', 'w') as f:
      f.write('unsym')

    crash = fuzz_task.Crash.from_testcase_manager_crash(
        testcase_manager.Crash('dir/path-http-name', 123, 11, ['res'], ['ges'],
                               '/stack_file_path'))
    return crash

  # def _make_crash(self, trace, state='state'):
  #   """Make crash."""
  #   self.mock.get_real_revision.return_value = 'this.is.fake.ver'

  #   self.mock.get_command_line_for_application.return_value = 'cmd'
  #   dummy_state = stacktraces.CrashInfo()
  #   dummy_state.crash_type = 'type'
  #   dummy_state.crash_address = 'address'
  #   dummy_state.crash_state = state
  #   dummy_state.crash_stacktrace = 'orig_trace'
  #   dummy_state.crash_frames = ['frame 1', 'frame 2']
  #   self.mock.get_crash_data.return_value = dummy_state
  #   self.mock.get_crash_stacktrace_output.return_value = trace
  #   self.mock.get_unsymbolized_crash_stacktrace.return_value = trace
  #   self.mock.is_security_issue.return_value = True
  #   self.mock.ignore_stacktrace.return_value = False

  #   with open('/stack_file_path', 'w') as f:
  #     f.write('unsym')
  #   return uworker_msg_pb2.FuzzTaskCrash(
  #       file_path='dir/path-http-name',
  #       crash_time=123,
  #       return_code=11,
  #       resource_list=['res'],
  #       gestures=['ges'],
  #       is_valid=True,
  #       unsymbolized_crash_stacktrace='unsym')

  def test_existing_unreproducible_testcase(self):
    """Test existing unreproducible testcase."""
    crashes = [self._make_crash('c1'), self._make_crash('c2')]
    self.mock.test_for_reproducibility.return_value = False

    existing_testcase = data_types.Testcase()
    existing_testcase.crash_stacktrace = 'existing'
    existing_testcase.crash_type = crashes[0].crash_type
    existing_testcase.crash_state = crashes[0].crash_state
    existing_testcase.security_flag = crashes[0].security_flag
    existing_testcase.one_time_crasher_flag = True
    existing_testcase.job_type = 'existing_job'
    existing_testcase.timestamp = datetime.datetime.now()
    existing_testcase.project_name = 'some_project'
    existing_testcase.put()

    variant = data_types.TestcaseVariant()
    variant.status = data_types.TestcaseVariantStatus.UNREPRODUCIBLE
    variant.job_type = 'job'
    variant.testcase_id = existing_testcase.key.id()
    variant.put()

    crash_revision = 1234

    groups = fuzz_task.process_crashes(
        crashes=crashes,
        context=fuzz_task.Context(
            project_name='some_project',
            bot_name='bot',
            job_type='job',
            fuzz_target=data_types.FuzzTarget(engine='engine', binary='binary'),
            redzone=111,
            disable_ubsan=True,
            platform_id='platform',
            crash_revision=crash_revision,
            fuzzer_name='fuzzer',
            window_argument='win_args',
            fuzzer_metadata={'issue_metadata': {}},
            testcases_metadata={},
            timeout_multiplier=1,
            test_timeout=2,
            data_directory='/data',
        ),
        upload_urls=_get_upload_urls_from_proto())

    self.assertEqual(1, len(groups))
    self.assertEqual(2, len(groups[0].crashes))
    self.assertEqual(crashes[0].crash_type, groups[0].main_crash.crash_type)
    self.assertEqual(crashes[0].crash_state, groups[0].main_crash.crash_state)
    self.assertEqual(crashes[0].security_flag,
                     groups[0].main_crash.security_flag)

    testcases = list(data_types.Testcase.query())
    self.assertEqual(1, len(testcases))
    self.assertEqual('existing', testcases[0].crash_stacktrace)

    # TODO(metzman): Make this test work again by using postprocess.
    # self.assertEqual('fuzzed_key', variant.reproducer_key)
    # self.assertEqual(1234, variant.revision)
    # self.assertEqual('type', variant.crash_type)
    # self.assertEqual('state', variant.crash_state)
    # self.assertEqual(True, variant.security_flag)
    # self.assertEqual(True, variant.is_similar)

  @parameterized.parameterized.expand(['some_project', 'chromium'])
  def test_create_many_groups(self, project_name):
    """Test creating many groups."""
    self.mock.get_project_name.return_value = project_name

    self.mock.insert.return_value = {'insertErrors': [{'index': 0}]}

    # TODO(metzman): Add a separate test for strategies.
    r2_stacktrace = 'r2\ncf::fuzzing_strategies: value_profile\n'

    crashes = [
        self._make_crash('r1', state='reproducible1'),
        self._make_crash(r2_stacktrace, state='reproducible1'),
        self._make_crash('r3', state='reproducible1'),
        self._make_crash('r4', state='reproducible2'),
        self._make_crash('u1', state='unreproducible1'),
        self._make_crash('u2', state='unreproducible2'),
        self._make_crash('u3', state='unreproducible2'),
        self._make_crash('u4', state='unreproducible3')
    ]

    self.mock.test_for_reproducibility.side_effect = [
        False,  # For r1. It returns False. So, r1 is demoted.
        True,  # For r2. It returns True. So, r2 becomes primary for its group.
        True,  # For r4.
        False,  # For u1.
        False,  # For u2.
        False,  # For u3.
        False
    ]  # For u4.

    upload_urls = _get_upload_urls_from_proto()

    groups = fuzz_task.process_crashes(
        crashes=crashes,
        context=fuzz_task.Context(
            project_name=project_name,
            bot_name='bot',
            job_type='job',
            fuzz_target=data_types.FuzzTarget(engine='engine', binary='binary'),
            redzone=111,
            disable_ubsan=False,
            platform_id='platform',
            crash_revision=1234,
            fuzzer_name='fuzzer',
            window_argument='win_args',
            fuzzer_metadata={'issue_metadata': {}},
            testcases_metadata={},
            timeout_multiplier=1,
            test_timeout=2,
            data_directory='/data'),
        upload_urls=upload_urls)

    self.assertEqual(5, len(groups))
    self.assertEqual([
        'reproducible1', 'reproducible2', 'unreproducible1', 'unreproducible2',
        'unreproducible3'
    ], [group.main_crash.crash_state for group in groups])
    self.assertEqual([3, 1, 1, 2, 1], [len(group.crashes) for group in groups])

    testcases = [group.main_crash for group in groups if group.main_crash]
    self.assertEqual(5, len(testcases))
    self.assertSetEqual({r2_stacktrace, 'r4', 'u1', 'u2', 'u4'},
                        {t.crash_stacktrace for t in testcases})

    # TODO(metzman): Make this test use the postprocess function as well so we
    # can test more functionality that was deleted in this PR.


class WriteCrashToBigQueryTest(unittest.TestCase):
  """Test write_crash_to_big_query."""

  def setUp(self):
    self.client = mock.Mock(spec_set=big_query.Client)
    helpers.patch(self, [
        'clusterfuzz._internal.system.environment.get_value',
        'clusterfuzz._internal.datastore.data_handler.get_project_name',
        'clusterfuzz._internal.google_cloud_utils.big_query.Client',
        'time.time',
    ])
    monitor.metrics_store().reset_for_testing()

    self.mock.get_project_name.return_value = 'some_project'
    self.mock.get_value.return_value = 'bot'
    self.mock.Client.return_value = self.client
    self.mock.time.return_value = 99
    self.crashes = [
        self._make_crash('c1'),
        self._make_crash('c2'),
        self._make_crash('c3')
    ]

    newly_created_testcase = mock.MagicMock()
    newly_created_testcase.key.id.return_value = 't'
    self.group = mock.MagicMock(
        crashes=self.crashes,
        main_crash=self.crashes[0],
        one_time_crasher_flag=False,
        newly_created_testcase=newly_created_testcase)

  def _make_crash(self, state):
    crash = mock.Mock(
        crash_type='type',
        crash_state=state,
        crash_time=111,
        security_flag=True,
        key='key')
    return crash

  def _make_crash(self, state):
    crash = mock.Mock(
        crash_type='type',
        crash_state=state,
        crash_time=111,
        security_flag=True,
        key='key')
    return crash

  def _json(self, job, platform, state, new_flag, testcase_id):
    return {
        'crash_type': 'type',
        'crash_state': state,
        'created_at': 99,
        'platform': platform,
        'crash_time_in_ms': 111000,
        'parent_fuzzer_name': 'engine',
        'fuzzer_name': 'engine_binary',
        'job_type': job,
        'security_flag': True,
        'reproducible_flag': True,
        'revision': '1234',
        'new_flag': new_flag,
        'project': 'some_project',
        'testcase_id': testcase_id
    }

  def test_all_succeed(self):
    """Test writing succeeds."""
    self.client.insert.return_value = {}
    output = self._create_output()
    uworker_input = _create_uworker_input(job='job')
    # TODO(metzman): Use correct type of group.
    fuzz_task.write_crashes_to_big_query(
        self.group, self.group.newly_created_testcase, None, uworker_input,
        output, 'engine_binary')

    success_count = monitoring_metrics.BIG_QUERY_WRITE_COUNT.get({
        'success': True
    })
    failure_count = monitoring_metrics.BIG_QUERY_WRITE_COUNT.get({
        'success': False
    })

    self.assertEqual(3, success_count)
    self.assertEqual(0, failure_count)

    self.mock.Client.assert_called_once_with(
        dataset_id='main', table_id='crashes$19700101')
    self.client.insert.assert_called_once_with([
        big_query.Insert(
            self._json('job', 'linux', 'c1', True, 't'), 'key:bot:99:0'),
        big_query.Insert(
            self._json('job', 'linux', 'c2', False, None), 'key:bot:99:1'),
        big_query.Insert(
            self._json('job', 'linux', 'c3', False, None), 'key:bot:99:2')
    ])

  def _create_output(self, platform='linux', crash_revision='1234'):
    fuzz_task_output = uworker_msg_pb2.FuzzTaskOutput(
        crash_revision=crash_revision)
    output = uworker_msg_pb2.Output(
        bot_name='bot', platform_id=platform, fuzz_task_output=fuzz_task_output)
    return output

  def test_succeed(self):
    """Test writing succeeds."""
    self.client.insert.return_value = {'insertErrors': [{'index': 1}]}
    output = self._create_output()
    uworker_input = _create_uworker_input()
    fuzz_task.write_crashes_to_big_query(
        self.group, self.group.newly_created_testcase, None, uworker_input,
        output, 'engine_binary')

    success_count = monitoring_metrics.BIG_QUERY_WRITE_COUNT.get({
        'success': True
    })
    failure_count = monitoring_metrics.BIG_QUERY_WRITE_COUNT.get({
        'success': False
    })

    self.assertEqual(2, success_count)
    self.assertEqual(1, failure_count)

    self.mock.Client.assert_called_once_with(
        dataset_id='main', table_id='crashes$19700101')
    self.client.insert.assert_called_once_with([
        big_query.Insert(
            self._json('job', 'linux', 'c1', True, 't'), 'key:bot:99:0'),
        big_query.Insert(
            self._json('job', 'linux', 'c2', False, None), 'key:bot:99:1'),
        big_query.Insert(
            self._json('job', 'linux', 'c3', False, None), 'key:bot:99:2')
    ])

  def test_chromeos_platform(self):
    """Test ChromeOS platform is written in stats."""
    self.client.insert.return_value = {'insertErrors': [{'index': 1}]}
    output = self._create_output()
    uworker_input = _create_uworker_input(job='job_chromeos')
    fuzz_task.write_crashes_to_big_query(
        self.group, self.group.newly_created_testcase, None, uworker_input,
        output, 'engine_binary')

    success_count = monitoring_metrics.BIG_QUERY_WRITE_COUNT.get({
        'success': True
    })
    failure_count = monitoring_metrics.BIG_QUERY_WRITE_COUNT.get({
        'success': False
    })

    self.assertEqual(2, success_count)
    self.assertEqual(1, failure_count)

    self.mock.Client.assert_called_once_with(
        dataset_id='main', table_id='crashes$19700101')
    self.client.insert.assert_called_once_with([
        big_query.Insert(
            self._json('job_chromeos', 'chrome', 'c1', True, 't'),
            'key:bot:99:0'),
        big_query.Insert(
            self._json('job_chromeos', 'chrome', 'c2', False, None),
            'key:bot:99:1'),
        big_query.Insert(
            self._json('job_chromeos', 'chrome', 'c3', False, None),
            'key:bot:99:2')
    ])

  def test_exception(self):
    """Test writing raising an exception."""
    self.client.insert.side_effect = Exception('error')
    output = self._create_output()
    uworker_input = _create_uworker_input()
    fuzz_task.write_crashes_to_big_query(
        self.group, self.group.newly_created_testcase, None, uworker_input,
        output, 'engine_binary')

    success_count = monitoring_metrics.BIG_QUERY_WRITE_COUNT.get({
        'success': True
    })
    failure_count = monitoring_metrics.BIG_QUERY_WRITE_COUNT.get({
        'success': False
    })

    self.assertEqual(0, success_count)
    self.assertEqual(3, failure_count)


class TestCorpusSync(fake_filesystem_unittest.TestCase):
  """Test corpus sync."""

  def setUp(self):
    """Setup for test corpus sync."""
    helpers.patch(self, [
        'clusterfuzz._internal.fuzzing.corpus_manager.ProtoFuzzTargetCorpus.rsync_to_disk',
        'clusterfuzz._internal.fuzzing.corpus_manager.ProtoFuzzTargetCorpus.upload_files',
        'clusterfuzz._internal.google_cloud_utils.storage.last_updated',
        'clusterfuzz._internal.google_cloud_utils.storage.list_blobs',
        'clusterfuzz._internal.google_cloud_utils.storage.get_arbitrary_signed_upload_urls'
    ])

    helpers.patch_environ(self)

    os.environ['FAIL_RETRIES'] = '1'
    os.environ['CORPUS_BUCKET'] = 'bucket'

    self.mock.get_arbitrary_signed_upload_urls.return_value = ['https://a'
                                                              ] * 1000
    test_utils.set_up_pyfakefs(self)
    self.fs.create_dir('/dir')
    self.fs.create_dir('/dir1')
    self.mock.list_blobs.return_value = []
    self.mock.last_updated.return_value = None
    self.corpus = corpus_manager.get_fuzz_target_corpus('parent',
                                                        'child').serialize()

  def _write_corpus_files(self, *args, **kwargs):  # pylint: disable=unused-argument
    self.fs.create_file('/dir/a')
    self.fs.create_file('/dir/b')
    return True

  def test_sync(self):
    """Test corpus sync."""
    corpus = fuzz_task.GcsCorpus('parent', 'child', '/dir', '/dir1',
                                 self.corpus)

    self.mock.rsync_to_disk.side_effect = self._write_corpus_files
    corpus.upload_files(corpus.get_new_files())
    self.assertTrue(corpus.sync_from_gcs())
    assert len(os.listdir('/dir')) == 2, os.listdir('/dir')
    self.assertTrue(os.path.exists('/dir1/.child_sync'))
    self.assertEqual(('/dir',), self.mock.rsync_to_disk.call_args[0][1:])
    self.fs.create_file('/dir/c')
    self.assertListEqual(['/dir/c'], corpus.get_new_files())

    corpus.upload_files(corpus.get_new_files())
    self.assertEqual((['/dir/c'],), self.mock.upload_files.call_args[0][1:])

    self.assertListEqual([], corpus.get_new_files())

  def test_no_sync(self):
    """Test no corpus sync when bundle is not updated since last sync."""
    corpus = fuzz_task.GcsCorpus('parent', 'child', '/dir', '/dir1',
                                 self.corpus)

    utils.write_data_to_file(time.time(), '/dir1/.child_sync')
    self.mock.last_updated.return_value = (
        datetime.datetime.utcnow() - datetime.timedelta(days=1))
    self.assertTrue(corpus.sync_from_gcs())
    self.assertEqual(0, self.mock.rsync_to_disk.call_count)

  def test_sync_with_failed_last_update(self):
    """Test corpus sync when failed to get last update info from gcs."""
    corpus = fuzz_task.GcsCorpus('parent', 'child', '/dir', '/dir1',
                                 self.corpus)

    utils.write_data_to_file(time.time(), '/dir1/.child_sync')
    self.mock.last_updated.return_value = None
    self.assertTrue(corpus.sync_from_gcs())
    self.assertEqual(1, self.mock.rsync_to_disk.call_count)


@test_utils.with_cloud_emulators('datastore')
class DoBlackboxFuzzingTest(fake_filesystem_unittest.TestCase):
  """do_blackbox_fuzzing tests."""

  def setUp(self):
    """Setup for blackbox fuzzing test."""
    helpers.patch_environ(self)
    helpers.patch(self, [
        'clusterfuzz._internal.base.utils.random_element_from_list',
        'clusterfuzz._internal.base.utils.random_number',
        'clusterfuzz._internal.bot.fuzzers.engine_common.current_timestamp',
        'clusterfuzz._internal.bot.tasks.utasks.fuzz_task_knobs.pick_gestures',
        'clusterfuzz._internal.bot.testcase_manager.upload_log',
        'clusterfuzz._internal.bot.testcase_manager.upload_testcase',
        'clusterfuzz._internal.build_management.revisions.get_component_list',
        'clusterfuzz._internal.crash_analysis.crash_analyzer.is_crash',
        'clusterfuzz._internal.crash_analysis.stack_parsing.stack_analyzer.get_crash_data',
        'clusterfuzz._internal.datastore.ndb_init.context',
        'clusterfuzz._internal.metrics.fuzzer_stats.upload_stats',
        'random.random',
        'clusterfuzz._internal.system.process_handler.close_queue',
        'clusterfuzz._internal.system.process_handler.get_process',
        'clusterfuzz._internal.system.process_handler.get_queue',
        'clusterfuzz._internal.system.process_handler.run_process',
        'clusterfuzz._internal.system.process_handler.terminate_hung_threads',
        'clusterfuzz._internal.system.process_handler.'
        'terminate_stale_application_instances',
    ])

    os.environ['APP_ARGS'] = '-x'
    os.environ['APP_ARGS_APPEND_TESTCASE'] = 'True'
    os.environ['APP_DIR'] = '/app'
    os.environ['APP_NAME'] = 'app_1'
    os.environ['APP_PATH'] = '/app/app_1'
    os.environ['BOT_TMPDIR'] = '/tmp'
    os.environ['CRASH_STACKTRACES_DIR'] = '/crash'
    os.environ['ENABLE_GESTURES'] = 'False'
    os.environ['FAIL_RETRIES'] = '1'
    os.environ['FUZZER_DIR'] = '/fuzzer'
    os.environ['INPUT_DIR'] = '/input'
    os.environ['JOB_NAME'] = 'asan_test'
    os.environ['MAX_FUZZ_THREADS'] = '1'
    os.environ['MAX_TESTCASES'] = '3'
    os.environ['RANDOM_SEED'] = '-r'
    os.environ['ROOT_DIR'] = '/root'
    os.environ['THREAD_ALIVE_CHECK_INTERVAL'] = '0.001'
    os.environ['THREAD_DELAY'] = '0.001'
    os.environ['USER_PROFILE_IN_MEMORY'] = 'True'

    test_utils.set_up_pyfakefs(self)
    self.fs.create_dir('/crash')
    self.fs.create_dir('/root/bot/logs')

    # Value picked as timeout multiplier.
    self.mock.random_element_from_list.return_value = 2.0
    # Choose window_arg, timeout multiplier, random seed.
    self.mock.random_number.side_effect = [0, 0, 3]
    # One trial profile for the session.
    self.mock.random.side_effect = [0.3, 0.3]
    self.mock.pick_gestures.return_value = []
    self.mock.get_component_list.return_value = [{
        'component': 'component',
        'link_text': 'rev',
    }]
    self.mock.current_timestamp.return_value = 0.0

    # Dummy output when running tests. E.g. exit code 0 and no output.
    self.mock.run_process.return_value = (0, 0, '')

    # Treat first and third run as crashed.
    self.mock.is_crash.side_effect = [True, False, True]

    self.mock.get_queue.return_value = queue.Queue()
    self.mock.get_process.return_value = threading.Thread

  def test_trials(self):
    """Test fuzzing session with trials."""
    data_types.Trial(app_name='app_1', probability=0.5, app_args='-y').put()
    data_types.Trial(app_name='app_1', probability=0.2, app_args='-z').put()

    uworker_input = uworker_msg_pb2.Input(
        fuzzer_name='fantasy_fuzz', job_type='asan_test')

    session = fuzz_task.FuzzingSession(uworker_input, 10)
    self.assertEqual(20, session.test_timeout)

    # Mock out actual test-case generation for 3 tests.
    session.generate_blackbox_testcases = mock.MagicMock()
    expected_testcase_file_paths = ['/tests/0', '/tests/1', '/tests/2']
    session.generate_blackbox_testcases.return_value = (
        fuzz_task.GenerateBlackboxTestcasesResult(
            True, expected_testcase_file_paths,
            {'fuzzer_binary_name': 'fantasy_fuzz'}))

    fuzzer = data_types.Fuzzer()
    fuzzer.name = 'fantasy_fuzz'

    fuzzer_metadata, testcase_file_paths, testcases_metadata, crashes = (
        session.do_blackbox_fuzzing(fuzzer, '/fake-fuzz-dir', 'asan_test'))

    self.assertEqual({'fuzzer_binary_name': 'fantasy_fuzz'}, fuzzer_metadata)
    self.assertEqual(expected_testcase_file_paths, testcase_file_paths)
    self.assertEqual(
        {t: {
            'gestures': []
        } for t in expected_testcase_file_paths}, testcases_metadata)

    self.assertEqual(3, len(self.mock.is_crash.call_args_list))

    # Verify the three test runs are called with the correct arguments.
    calls = self.mock.run_process.call_args_list
    self.assertEqual(3, len(calls))
    self.assertEqual('/app/app_1 -r=3 -x -y /tests/0', calls[0][0][0])
    self.assertEqual('/app/app_1 -r=3 -x -y /tests/1', calls[1][0][0])
    self.assertEqual('/app/app_1 -r=3 -x -y /tests/2', calls[2][0][0])

    # Verify the two crashes store the correct arguments.
    self.assertEqual(2, len(crashes))
    self.assertEqual('/app/app_1 -r=3 -x -y /tests/0',
                     crashes[0].application_command_line)
    self.assertEqual('/app/app_1 -r=3 -x -y /tests/2',
                     crashes[1].application_command_line)


@test_utils.with_cloud_emulators('datastore')
class DoEngineFuzzingTest(fake_filesystem_unittest.TestCase):
  """do_engine_fuzzing tests."""

  def setUp(self):
    """Setup for do engine fuzzing test."""
    helpers.patch_environ(self)
    helpers.patch(self, [
        'clusterfuzz._internal.bot.fuzzers.engine_common.current_timestamp',
        'clusterfuzz._internal.bot.tasks.utasks.fuzz_task.GcsCorpus.sync_from_gcs',
        'clusterfuzz._internal.bot.tasks.utasks.fuzz_task.GcsCorpus.upload_files',
        'clusterfuzz._internal.build_management.revisions.get_component_list',
        'clusterfuzz._internal.bot.testcase_manager.upload_testcase',
        'clusterfuzz._internal.google_cloud_utils.storage.list_blobs',
        'clusterfuzz._internal.google_cloud_utils.storage.get_arbitrary_signed_upload_urls',
        'clusterfuzz._internal.google_cloud_utils.storage.last_updated',
    ])
    test_utils.set_up_pyfakefs(self)

    os.environ['JOB_NAME'] = 'libfuzzer_asan_test'
    os.environ['FUZZ_INPUTS'] = '/fuzz-inputs'
    os.environ['FUZZ_INPUTS_DISK'] = '/fuzz-inputs-disk'
    os.environ['BUILD_DIR'] = '/build_dir'
    os.environ['MAX_TESTCASES'] = '2'
    os.environ['AUTOMATIC_LABELS'] = 'auto_label,auto_label1'
    os.environ['AUTOMATIC_COMPONENTS'] = 'auto_component,auto_component1'

    self.fs.create_file('/build_dir/test_target')
    self.fs.create_file(
        '/build_dir/test_target.labels', contents='label1\nlabel2')
    self.fs.create_file(
        '/build_dir/test_target.owners', contents='owner1@email.com')
    self.fs.create_file(
        '/build_dir/test_target.components', contents='component1\ncomponent2')
    self.fs.create_file('/input')

    self.mock.sync_from_gcs.return_value = True
    self.mock.upload_files.return_value = True
    self.mock.get_component_list.return_value = [{
        'component': 'component',
        'link_text': 'rev',
    }]
    self.mock.current_timestamp.return_value = 0.0
    self.mock.list_blobs.return_value = []
    self.mock.get_arbitrary_signed_upload_urls.return_value = ['http://a'] * 100
    self.mock.last_updated.return_value = None

  def test_basic(self):
    """Test basic fuzzing session."""
    target = 'test_target'
    fuzz_task_input = uworker_msg_pb2.FuzzTaskInput(
        fuzz_target=uworker_io.entity_to_protobuf(
            data_types.FuzzTarget(engine='libFuzzer', binary=target)),)
    uworker_input = uworker_msg_pb2.Input(
        fuzzer_name='libFuzzer_fuzz',
        job_type='libfuzzer_asan_test',
        fuzz_task_input=fuzz_task_input)
    session = fuzz_task.FuzzingSession(uworker_input, 60)
    session.testcase_directory = os.environ['FUZZ_INPUTS']
    session.data_directory = '/data_dir'

    os.environ['FUZZ_TARGET'] = target
    os.environ['APP_REVISION'] = '1'
    os.environ['FUZZ_TEST_TIMEOUT'] = '2000'
    os.environ['BOT_NAME'] = 'hostname.company.com'
    os.environ['FUZZ_LOGS_BUCKET'] = '/fuzz-logs'

    expected_crashes = [engine.Crash('/input', 'stack', ['args'], 1.0)]

    engine_impl = mock.Mock()
    engine_impl.name = 'libFuzzer'
    engine_impl.prepare.return_value = engine.FuzzOptions(
        '/corpus', ['arg'], {
            'strategy_1': 1,
            'strategy_2': 50,
        })
    engine_impl.fuzz.side_effect = lambda *_: engine.FuzzResult(
        'logs', ['cmd'], expected_crashes, {'stat': 1}, 42.0)
    engine_impl.fuzz_additional_processing_timeout.return_value = 1337

    crashes, fuzzer_metadata = session.do_engine_fuzzing(engine_impl)

    engine_impl.fuzz.assert_called_with('/build_dir/test_target',
                                        engine_impl.prepare.return_value,
                                        '/fuzz-inputs', 663)
    self.assertDictEqual({
        'fuzzer_binary_name':
            'test_target',
        'issue_components':
            'component1,component2,auto_component,auto_component1',
        'issue_labels':
            'label1,label2,auto_label,auto_label1',
        'issue_owners':
            'owner1@email.com',
    }, fuzzer_metadata)

    self.assertEqual(2, len(crashes))
    for i in range(2):
      self.assertEqual('/input', crashes[i].file_path)
      self.assertEqual(1, crashes[i].return_code)
      self.assertEqual('stack', crashes[i].unsymbolized_crash_stacktrace)
      self.assertEqual(1.0, crashes[i].crash_time)
      self.assertEqual('args', crashes[i].arguments)

    for i in range(2):
      testcase_run = json.loads(session.fuzz_task_output.testcase_run_jsons[i])
      self.assertDictEqual({
          'build_revision': 1,
          'command': ['cmd'],
          'fuzzer': 'libFuzzer_test_target',
          'job': 'libfuzzer_asan_test',
          'kind': 'TestcaseRun',
          'stat': 1,
          'strategy_strategy_1': 1,
          'strategy_strategy_2': 50,
          'timestamp': 0.0,
      }, testcase_run)
      # TODO(metzman): We need a test for fuzzing end to end with
      # preprocess/main/postprocess.


class UntrustedRunEngineFuzzerTest(
    untrusted_runner_helpers.UntrustedRunnerIntegrationTest):
  """Engine fuzzing tests for untrusted."""

  def setUp(self):
    """Set up."""
    super().setUp()
    environment.set_value('JOB_NAME', 'libfuzzer_asan_job')

    job = data_types.Job(
        name='libfuzzer_asan_job',
        environment_string=(
            'RELEASE_BUILD_BUCKET_PATH = '
            'gs://clusterfuzz-test-data/test_libfuzzer_builds/'
            'test-libfuzzer-build-([0-9]+).zip\n'
            'REVISION_VARS_URL = https://commondatastorage.googleapis.com/'
            'clusterfuzz-test-data/test_libfuzzer_builds/'
            'test-libfuzzer-build-%s.srcmap.json\n'))
    job.put()

    self.temp_dir = tempfile.mkdtemp(dir=environment.get_value('FUZZ_INPUTS'))
    environment.set_value('USE_MINIJAIL', False)

  def tearDown(self):
    super().tearDown()
    shutil.rmtree(self.temp_dir, ignore_errors=True)

  def test_run_engine_fuzzer(self):
    """Test running engine fuzzer."""
    self._setup_env(job_type='libfuzzer_asan_job')
    environment.set_value('FUZZ_TEST_TIMEOUT', 3600)
    environment.set_value('STRATEGY_SELECTION_METHOD', 'multi_armed_bandit')
    environment.set_value(
        'STRATEGY_SELECTION_DISTRIBUTION',
        '[{"strategy_name": "value_profile", '
        '"probability": 1.0, "engine": "libFuzzer"}]')

    build_manager.setup_build()
    corpus_directory = os.path.join(self.temp_dir, 'corpus')
    testcase_directory = os.path.join(self.temp_dir, 'artifacts')
    os.makedirs(file_host.rebase_to_worker_root(corpus_directory))
    os.makedirs(file_host.rebase_to_worker_root(testcase_directory))

    result, fuzzer_metadata, strategies = fuzz_task.run_engine_fuzzer(
        libfuzzer_engine.Engine(), 'test_fuzzer', corpus_directory,
        testcase_directory)
    self.assertIn(
        'ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000',
        result.logs)
    self.assertEqual(1, len(result.crashes))
    self.assertTrue(result.crashes[0].input_path.startswith(
        os.environ['ROOT_DIR']))
    self.assertTrue(os.path.exists(result.crashes[0].input_path))
    self.assertIsInstance(result.stats.get('number_of_executed_units'), int)
    self.assertIsInstance(result.stats.get('oom_count'), int)
    self.assertIsInstance(result.stats.get('strategy_selection_method'), str)

    self.assertDictEqual({'fuzzer_binary_name': 'test_fuzzer'}, fuzzer_metadata)
    self.assertDictEqual({'value_profile': 1}, strategies)


class AddIssueMetadataFromEnvironmentTest(unittest.TestCase):
  """Tests for _add_issue_metadata_from_environment."""

  def setUp(self):
    helpers.patch_environ(self)

  def test_add_no_existing(self):
    """Test adding issue metadata when there are none existing."""
    os.environ['AUTOMATIC_LABELS'] = 'auto_label'
    os.environ['AUTOMATIC_LABELS_1'] = 'auto_label1'
    os.environ['AUTOMATIC_COMPONENTS'] = 'auto_component'
    os.environ['AUTOMATIC_COMPONENTS_1'] = 'auto_component1'

    metadata = {}
    fuzz_task._add_issue_metadata_from_environment(metadata)
    self.assertDictEqual({
        'issue_components': 'auto_component,auto_component1',
        'issue_labels': 'auto_label,auto_label1',
    }, metadata)

  def test_add_append(self):
    """Test adding issue metadata when there are already existing metadata."""
    os.environ['AUTOMATIC_LABELS'] = 'auto_label'
    os.environ['AUTOMATIC_LABELS_1'] = 'auto_label1'
    os.environ['AUTOMATIC_COMPONENTS'] = 'auto_component'
    os.environ['AUTOMATIC_COMPONENTS_1'] = 'auto_component1'

    metadata = {
        'issue_components': 'existing_component',
        'issue_labels': 'existing_label'
    }
    fuzz_task._add_issue_metadata_from_environment(metadata)
    self.assertDictEqual({
        'issue_components':
            'existing_component,auto_component,auto_component1',
        'issue_labels':
            'existing_label,auto_label,auto_label1',
    }, metadata)

  def test_add_numeric(self):
    """Tests adding a numeric label."""
    os.environ['AUTOMATIC_LABELS'] = '123,456'

    metadata = {}
    fuzz_task._add_issue_metadata_from_environment(metadata)
    self.assertDictEqual({
        'issue_labels': '123,456',
    }, metadata)


class PreprocessStoreFuzzerRunResultsTest(unittest.TestCase):
  """Tests for preprocess_store_fuzzer_run_results."""

  SIGNED_URL = 'https://signed'

  def setUp(self):
    helpers.patch(self, [
        'clusterfuzz._internal.google_cloud_utils.storage._sign_url',
        'clusterfuzz._internal.google_cloud_utils.blobs.get_signed_upload_url',
    ])
    self.mock._sign_url.side_effect = (
        lambda remote_path, method, minutes: remote_path)
    self.mock.get_signed_upload_url.return_value = self.SIGNED_URL
    helpers.patch_environ(self)
    os.environ['JOB_NAME'] = 'linux_chrome_asan'

  def test_preprocess_store_fuzzer_run_results(self):
    fuzz_task_input = uworker_msg_pb2.FuzzTaskInput()
    fuzz_task.preprocess_store_fuzzer_run_results(fuzz_task_input)
    self.assertEqual(fuzz_task_input.sample_testcase_upload_url,
                     self.SIGNED_URL)

    self.assertEqual(fuzz_task_input.script_log_upload_url, self.SIGNED_URL)


@test_utils.with_cloud_emulators('datastore')
class PostprocessStoreFuzzerRunResultsTest(unittest.TestCase):
  """Tests for postprocess_store_fuzzer_run_results."""

  def test_postprocess_store_fuzzer_run_results(self):
    """Tests postprocess_store_fuzzer_run_results."""
    helpers.patch_environ(self)
    job_name = 'linux_chrome_asan'
    os.environ['JOB_NAME'] = job_name
    fuzzer_name = 'myfuzzer'
    revision = 1
    fuzzer = data_types.Fuzzer(name=fuzzer_name, revision=revision)
    fuzzer.put()
    console_output = 'OUTPUT'
    generated_testcase_string = 'GENERATED'
    fuzzer_return_code = 9
    fuzzer_run_results = uworker_msg_pb2.StoreFuzzerRunResultsOutput(
        console_output=console_output,
        generated_testcase_string=generated_testcase_string,
        fuzzer_return_code=fuzzer_return_code)
    sample_testcase_upload_key = 'sample-key'
    fuzz_task_input = uworker_msg_pb2.FuzzTaskInput(
        sample_testcase_upload_key=sample_testcase_upload_key)
    uworker_input = uworker_msg_pb2.Input(
        fuzzer_name=fuzzer_name,
        fuzz_task_input=fuzz_task_input,
        job_type=job_name)
    output = uworker_msg_pb2.Output(
        fuzz_task_output=uworker_msg_pb2.FuzzTaskOutput(
            fuzzer_run_results=fuzzer_run_results, fuzzer_revision=revision),
        uworker_input=uworker_input)
    fuzz_task.postprocess_store_fuzzer_run_results(output)
    fuzzer = fuzzer.key.get()
    self.assertEqual(fuzzer.return_code, fuzzer_return_code)
    self.assertEqual(fuzzer.console_output, console_output)
    self.assertEqual(fuzzer.result, generated_testcase_string)


class UploadTestcaseRunJsons(unittest.TestCase):
  """Tests for upload_testcase_run_jsons."""

  def setUp(self):
    helpers.patch(self, [
        'clusterfuzz._internal.metrics.fuzzer_stats.upload_stats',
    ])

  def test_upload_testcase_run_jsons(self):
    """Tests that upload_testcase_run_jsons works as intended."""
    testcase_run_json_path = os.path.join(
        os.path.dirname(__file__), 'test_data', 'testcase_run.json')
    with open(testcase_run_json_path, 'r') as fp:
      testcase_run_jsons = [fp.read(), None]
    fuzz_task._upload_testcase_run_jsons(testcase_run_jsons)
    self.assertEqual(self.mock.upload_stats.call_count, 1)


@test_utils.with_cloud_emulators('datastore')
class PickFuzzTargetTest(unittest.TestCase):
  """Tests for _pick_fuzz_target."""

  def setUp(self):
    helpers.patch_environ(self)
    helpers.patch(self, [
        'clusterfuzz._internal.build_management.build_manager._split_target_build_list_targets'
    ])
    self.mock._split_target_build_list_targets.return_value = ['target']

  def test_split_build(self):
    """Tests that we don't pick a target for a split build."""
    os.environ[
        'FUZZ_TARGET_BUILD_BUCKET_PATH'] = 'gs://fuzz_target/%TARGET%/path'
    os.environ['JOB_NAME'] = 'libfuzzer_chrome_asan'
    self.assertEqual(fuzz_task._pick_fuzz_target(), 'target')


@test_utils.with_cloud_emulators('datastore')
class EmitTestcaseCreationEventTest(unittest.TestCase):
  """Test testcase creation event is emitted when created during fuzz task."""

  def setUp(self):
    helpers.patch(self, [
        'clusterfuzz._internal.metrics.events.emit',
        'clusterfuzz._internal.metrics.events._get_datetime_now',
        'clusterfuzz._internal.bot.tasks.task_creation.create_tasks',
        'clusterfuzz._internal.datastore.data_handler.get_project_name',
        'clusterfuzz._internal.datastore.data_handler.store_testcase'
    ])
    self.mock._get_datetime_now.return_value = datetime.datetime(2025, 1, 1)
    self.mock.get_project_name.return_value = 'project'

    # Needed mocks for calling create_testcase in fuzz task postprocess.
    context = mock.MagicMock(
        timeout_multiplier=1,
        test_timeout=99,
        fuzzer_name='engine',
        fuzz_target=None)
    crash = mock.MagicMock(
        crash_type='type',
        crash_state='state',
        security_flag=True,
        file_path='file_path',
        http_flag=True,
        gestures='g1')
    self.group = mock.MagicMock(
        context=context,
        main_crash=crash,
        crashes=[crash],
        one_time_crasher_flag=False,
    )
    self.uworker_input = _create_uworker_input()
    self.uworker_output = uworker_msg_pb2.Output(
        fuzz_task_output=uworker_msg_pb2.FuzzTaskOutput(crash_revision='1'))

  def test_create_testcase_event_emit(self):
    """Test that the create_testcase method emits the expected event."""
    self.mock.store_testcase.side_effect = _store_generic_testcase
    fuzz_task.create_testcase(
        group=self.group,
        uworker_input=self.uworker_input,
        uworker_output=self.uworker_output,
        fully_qualified_fuzzer_name='engine')

    testcase = data_handler.get_testcase_by_id(1)
    self.mock.emit.assert_called_once_with(
        events.TestcaseCreationEvent(
            testcase=testcase,
            creation_origin=events.TestcaseOrigin.FUZZ_TASK,
            uploader=None))


def _store_generic_testcase(*args, **kwargs):  # pylint: disable=unused-argument
  """Store a generic testcase and return its id."""
  testcase = test_utils.create_generic_testcase()
  return testcase.key.id()


def _create_uworker_input(job='job',
                          project_name='some_project',
                          fuzzer_name='engine'):
  uworker_env = {'PROJECT_NAME': project_name}
  return uworker_msg_pb2.Input(
      job_type=job, fuzzer_name=fuzzer_name, uworker_env=uworker_env)


def _get_upload_urls():
  return fuzz_task.UploadUrlCollection([uworker_msg_pb2.BlobUploadUrl()] * 1000)


def _get_upload_urls_from_proto():
  return [uworker_msg_pb2.BlobUploadUrl(key='uuid')] * 1000


@test_utils.with_cloud_emulators('datastore')
class SampleCrashesForReuploadTest(unittest.TestCase):
  """Test testcase creation event is emitted when created during fuzz task."""

  def setUp(self):
    helpers.patch(self, [
        'clusterfuzz._internal.bot.tasks.utasks.fuzz_task._get_sample_rate',
        'clusterfuzz._internal.bot.tasks.utasks.fuzz_task._get_replication_topic',
        'clusterfuzz._internal.bot.tasks.utasks.fuzz_task._publish_to_pubsub',
        'clusterfuzz._internal.system.environment.get_value',
    ])

    # Needed mocks for simulating the resulting crashes of fuzz task.
    self.job_type = 'some_job'
    self.fuzzer_name = 'some_fuzzer'
    self.binary_name = 'binary'
    self.project = 'project'
    self.engine = 'engine'
    self.replication_topic = 'crash_replication'
    self.sample_rate = 100
    self.fuzz_task_id = 'some_task_id'
    self.fuzzed_key = 'fuzzed_key'
    self.arguments = 'some_args'
    self.cli_command = 'cli_command'
    self.http_flag = True
    self.gestures = []

    self.mock._get_sample_rate.return_value = self.sample_rate
    self.mock._get_replication_topic.return_value = self.replication_topic
    self.mock.get_value.return_value = self.fuzz_task_id

    self.fuzz_target = data_types.FuzzTarget(
        engine=self.engine, binary=self.binary_name, project=self.project)

    crash = mock.MagicMock(
        fuzzed_key=self.fuzzed_key,
        arguments=self.arguments,
        application_command_line=self.cli_command,
        http_flag=self.http_flag,
        gestures=self.gestures)

    self.crash_group = mock.MagicMock(crashes=[crash],)

  def test_crashes_are_sampled_to_crash_replication_topic(self):
    """Test that the postprocess_sample_testcases method sends the
      expected message to the crash replication topic."""

    fuzz_task_input = uworker_msg_pb2.FuzzTaskInput()
    fuzz_task_input.fuzz_target.CopyFrom(
        uworker_io.entity_to_protobuf(self.fuzz_target))
    uworker_input = uworker_msg_pb2.Input(  # pylint: disable=no-member
        fuzz_task_input=fuzz_task_input,
        job_type=self.job_type,
        fuzzer_name=self.fuzzer_name,
        uworker_env={},
        setup_input=None,
    )

    fuzz_task_output = mock.MagicMock(crash_groups=[self.crash_group])
    uworker_output = mock.MagicMock(fuzz_task_output=fuzz_task_output)

    fuzz_task.postprocess_sample_testcases(uworker_input, uworker_output)

    expected_messages = [{
        'fuzzed_key': self.fuzzed_key,
        'job': self.job_type,
        'fuzzer': self.fuzzer_name,
        'target_name': self.binary_name,
        'arguments': self.arguments,
        'application_command_line': self.cli_command,
        'gestures': str(self.gestures),
        'http_flag': self.http_flag,
        'original_task_id': self.fuzz_task_id,
    }]

    self.mock._publish_to_pubsub.assert_called_once_with(
        expected_messages, self.replication_topic)
