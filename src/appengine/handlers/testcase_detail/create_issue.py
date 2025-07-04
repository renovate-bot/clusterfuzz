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
"""Handler for creating issue."""

from flask import request

from clusterfuzz._internal.issue_management import issue_filer
from clusterfuzz._internal.metrics import events
from handlers import base_handler
from handlers.testcase_detail import show
from libs import handler
from libs import helpers


class Handler(base_handler.Handler):
  """Handler that creates an issue."""

  @staticmethod
  def create_issue(testcase, severity, cc_me):
    """Create an issue."""
    issue_tracker = helpers.get_issue_tracker_for_testcase(testcase)
    user_email = helpers.get_user_email()

    if severity is not None:
      severity = helpers.cast(
          severity, int, 'Invalid value for security severity (%s).' % severity)

    additional_ccs = []
    if cc_me:
      additional_ccs.append(user_email)

    issue_id, _ = issue_filer.file_issue(
        testcase,
        issue_tracker,
        security_severity=severity,
        user_email=user_email,
        additional_ccs=additional_ccs)

    events.emit(
        events.IssueFilingEvent(
            testcase=testcase,
            issue_tracker_project=issue_tracker.project,
            issue_id=str(issue_id) if issue_id else None,
            issue_created=bool(issue_id)))

    if not issue_id:
      raise helpers.EarlyExitError('Unable to create new issue.', 500)

  @handler.post(handler.JSON, handler.JSON)
  @handler.require_csrf_token
  @handler.check_testcase_access
  def post(self, testcase):
    """Create an issue."""
    cc_me = request.get('ccMe')
    severity = request.get('severity')

    self.create_issue(testcase, severity, cc_me)
    return self.render_json(show.get_testcase_detail(testcase))
