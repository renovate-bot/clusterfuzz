# Copyright 2024 Google LLC
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
"""Module providing utilities for utask users. This module should not depend on
any other module in tasks to prevent circular imports and issues with
appengine."""

from clusterfuzz._internal.protos import uworker_msg_pb2
from clusterfuzz._internal.system import environment


def get_command_from_module(full_module_name: str) -> str:
  module_name = full_module_name.split('.')[-1]
  if not module_name.endswith('_task'):
    raise ValueError(f'{full_module_name} is not a real command')
  return module_name[:-len('_task')]


def is_remotely_executing_utasks() -> bool:
  """Returns True if the utask_main portions of utasks are being remotely
  executed on Google cloud batch."""
  # TODO(metzman): REMOTE_UTASK_EXECUTION should be a config not an env var.
  return (environment.is_production() and
          environment.get_value('REMOTE_UTASK_EXECUTION'))


class UworkerMsgParseError(RuntimeError):
  """Error for parsing UworkerMsgs."""


TESTCASE_BASED_TASKS = {
    'analyze',
    'blame',
    'impact',
    'minimize',
    'progression',
    'regression',
    'symbolize',
    'variant',
}

FUZZER_BASED_TASKS = {
    'corpus_pruning',
    'fuzz',
}


def get_task_execution_event_data(
    task_command: str,
    task_argument: str | uworker_msg_pb2.Input | None,  # pylint: disable=no-member
    job_type: str | None = None) -> dict:
  """Returns a formatted dict with task execution event data."""
  event_data = {}
  event_data['task_job'] = job_type
  if task_command in TESTCASE_BASED_TASKS:
    testcase_id = (
        task_argument.testcase_id
        if isinstance(task_argument, uworker_msg_pb2.Input)  # pylint: disable=no-member
        else task_argument)
    event_data['testcase_id'] = None if testcase_id is None else int(
        testcase_id)
  elif task_command in FUZZER_BASED_TASKS:
    event_data['task_fuzzer'] = (
        task_argument.fuzzer_name
        if isinstance(task_argument, uworker_msg_pb2.Input)  # pylint: disable=no-member
        else task_argument)
  return event_data
