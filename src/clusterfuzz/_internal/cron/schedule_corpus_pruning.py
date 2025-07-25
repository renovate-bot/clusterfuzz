# Copyright 2023 Google LLC
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
"""Schedule corpus pruning tasks."""

import time

from clusterfuzz._internal.base import tasks
from clusterfuzz._internal.base import utils
from clusterfuzz._internal.datastore import data_types
from clusterfuzz._internal.datastore import fuzz_target_utils
from clusterfuzz._internal.datastore import ndb_utils
from clusterfuzz._internal.metrics import logs


def get_tasks_to_schedule():
  """Return (task_target, job_name, queue_name) arguments to schedule a task."""
  for job in ndb_utils.get_all_from_query(data_types.Job.query()):
    if not utils.string_is_true(job.get_environment().get('CORPUS_PRUNE')):
      continue

    queue_name = tasks.queue_for_job(job.name)
    for target_job in fuzz_target_utils.get_fuzz_target_jobs(job=job.name):
      task_target = target_job.fuzz_target_name
      yield (task_target, job.name, queue_name)


def main():
  """Schedule corpus pruning tasks."""
  for task_target, job_name, queue_name in get_tasks_to_schedule():
    logs.info(f'Adding corpus pruning task {task_target}.')
    # Do this to avoid starving fuzzing. Don't rely on ETA since the fuzz task
    # scheduler looks at the queue.
    time.sleep(.25)
    tasks.add_task(
        'corpus_pruning', task_target, job_name, queue=queue_name, wait_time=60)

  logs.info('Schedule corpus pruning task succeeded.')
  return True
