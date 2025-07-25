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
"""Handler that uploads a testcase"""

# pylint: disable=wrong-import-order
import ast
import datetime
import io
import json
import os

from flask import request
from google.cloud import ndb

from clusterfuzz._internal import fuzzing
from clusterfuzz._internal.base import external_users
from clusterfuzz._internal.base import memoize
from clusterfuzz._internal.base import tasks
from clusterfuzz._internal.base import utils
from clusterfuzz._internal.base.tasks import task_utils
from clusterfuzz._internal.crash_analysis.stack_parsing import stack_analyzer
from clusterfuzz._internal.datastore import data_handler
from clusterfuzz._internal.datastore import data_types
from clusterfuzz._internal.google_cloud_utils import blobs
from clusterfuzz._internal.google_cloud_utils import storage
from clusterfuzz._internal.issue_management import issue_tracker_utils
from clusterfuzz._internal.metrics import events
from clusterfuzz._internal.metrics import monitor
from clusterfuzz._internal.metrics import monitoring_metrics
from clusterfuzz._internal.system import archive
from clusterfuzz._internal.system import environment
from handlers import base_handler
from libs import access
from libs import form
from libs import gcs
from libs import handler
from libs import helpers
from libs.query import datastore_query

MAX_RETRIES = 50
RUN_FILE_PATTERNS = ['run', 'fuzz-', 'index.', 'crash.']
PAGE_SIZE = 20
MORE_LIMIT = 100 - PAGE_SIZE
UPLOAD_URL = '/upload-testcase/upload-oauth'
MEMCACHE_TTL_IN_SECONDS = 60 * 60  # 1 hour.

TRUSTED_AGREEMENT_TEXT = 'This testcase is safe to run'


def _is_uploader_allowed(email):
  """Return bool on whether user is allowed to upload to any job or fuzzer."""
  return external_users.is_upload_allowed_for_user(email)


def _is_trusted_uploader_allowed(email):
  """Return whether or not uploader is allowed and trusted."""
  return access.has_access(
      need_privileged_access=True) and _is_uploader_allowed(email)


def attach_testcases(rows):
  """Attach testcase to each crash."""
  testcases = {}
  for index, row in enumerate(rows):
    testcases[index] = query_testcase(row['testcaseId'])

  for index, row in enumerate(rows):
    testcase = (list(testcases[index]) or [None])[0]
    if testcase:
      testcase = {
          'crashType': testcase.crash_type,
          'crashStateLines': (testcase.crash_state or '').strip().splitlines(),
          'isSecurity': testcase.security_flag,
          'issueNumber': testcase.bug_information,
          'job': testcase.job_type,
          'fuzzerName': testcase.actual_fuzzer_name(),
          'projectName': testcase.project_name,
      }
    row['testcase'] = testcase


def get_result():
  """Get the result."""
  params = dict(request.iterparams())
  page = helpers.cast(request.get('page') or 1, int, "'page' is not an int.")

  query = datastore_query.Query(data_types.TestcaseUploadMetadata)
  query.order('timestamp', is_desc=True)

  if not access.has_access(need_privileged_access=True):
    query.filter('uploader_email', helpers.get_user_email())
    params['permission'] = {'uploaderEmail': helpers.get_user_email()}

  entities, total_pages, total_items, has_more = query.fetch_page(
      page=page, page_size=PAGE_SIZE, projection=None, more_limit=MORE_LIMIT)

  items = []
  for entity in entities:
    items.append({
        'timestamp': utils.utc_datetime_to_timestamp(entity.timestamp),
        'testcaseId': entity.testcase_id,
        'uploaderEmail': entity.uploader_email,
        'filename': entity.filename,
        'bundled': entity.bundled,
        'pathInArchive': entity.path_in_archive,
        'status': entity.status
    })

  attach_testcases(items)

  result = {
      'hasMore': has_more,
      'items': items,
      'page': page,
      'pageSize': PAGE_SIZE,
      'totalItems': total_items,
      'totalPages': total_pages,
  }
  return result, params


def _read_to_bytesio(gcs_path):
  """Return a bytesio representing a GCS object."""
  data = storage.read_data(gcs_path)
  if not data:
    raise helpers.EarlyExitError('Failed to read uploaded archive.', 500)

  return io.BytesIO(data)


def guess_input_file(uploaded_file, filename):
  """Guess the main test case file from an archive."""
  for file_pattern in RUN_FILE_PATTERNS:
    blob_reader = _read_to_bytesio(uploaded_file.gcs_path)
    with archive.open(filename, blob_reader) as reader:
      file_path_input = reader.get_first_file_matching(file_pattern)
      if file_path_input:
        return file_path_input

  return None


def query_testcase(testcase_id):
  """Start a query for an associated testcase."""
  if not testcase_id:
    return []

  return data_types.Testcase.query(data_types.Testcase.key == ndb.Key(
      data_types.Testcase, testcase_id)).iter(
          limit=1,
          projection=[
              'crash_type',
              'crash_state',
              'security_flag',
              'bug_information',
              'job_type',
              'fuzzer_name',
              'overridden_fuzzer_name',
              'project_name',
          ])


def filter_target_names(targets, engine):
  """Filter target names for a fuzzer and remove parent fuzzer prefixes."""
  prefix = engine + '_'
  return [t[len(prefix):] for t in targets if t.startswith(prefix)]


def filter_blackbox_fuzzers(fuzzers):
  """Filter out fuzzers such that only blackbox fuzzers are included."""

  def is_engine_fuzzer(name):
    return any(name.startswith(engine) for engine in fuzzing.ENGINES)

  return [f for f in fuzzers if not is_engine_fuzzer(f)]


@memoize.wrap(memoize.Memcache(MEMCACHE_TTL_IN_SECONDS))
def find_fuzz_target(engine, target_name, job_name):
  """Return fuzz target values given the engine, target name (which may or may
  not be prefixed with project), and job."""
  project_name = data_handler.get_project_name(job_name)
  candidate_name = data_types.fuzz_target_fully_qualified_name(
      engine, project_name, target_name)

  target = data_handler.get_fuzz_target(candidate_name)
  if not target:
    raise helpers.EarlyExitError('Fuzz target does not exist.', 400)

  return target.fully_qualified_name(), target.binary


def _allow_unprivileged_metadata(testcase_metadata):
  """Returns whether or not the provided testcase metadata can be set by an
  unprivileged user."""
  if utils.is_oss_fuzz():
    # Labels in OSS-Fuzz are privileged and control things like disclosure
    # deadlines. Do not let these be editable.
    return False

  # Allow *only* issue labels to be set.
  return len(testcase_metadata) == 1 and 'issue_labels' in testcase_metadata


class Handler(base_handler.Handler):
  """Handler for the testcase uploads page."""

  @handler.get(handler.HTML)
  @handler.oauth
  def get(self):
    """Handles get request."""
    email = helpers.get_user_email()
    if not email:
      raise helpers.AccessDeniedError()

    is_privileged_or_domain_user = access.has_access(
        need_privileged_access=False)
    if is_privileged_or_domain_user or _is_uploader_allowed(email):
      # Privileged, domain and upload users can see all job and fuzzer names.
      allowed_jobs = data_handler.get_all_job_type_names()
      allowed_fuzzers = data_handler.get_all_fuzzer_names_including_children(
          include_parents=True)
    else:
      # Check if this is an external user with access to certain fuzzers/jobs.
      allowed_jobs = external_users.allowed_jobs_for_user(email)
      allowed_fuzzers = external_users.allowed_fuzzers_for_user(
          email, include_from_jobs=True)

      if not allowed_fuzzers and not allowed_jobs:
        raise helpers.AccessDeniedError()

    has_issue_tracker = bool(data_handler.get_issue_tracker_name())

    result, params = get_result()
    return self.render(
        'upload.html', {
            'fieldValues': {
                'blackboxFuzzers': filter_blackbox_fuzzers(allowed_fuzzers),
                'jobs': allowed_jobs,
                'targets': {
                    engine: filter_target_names(allowed_fuzzers, engine)
                    for engine in fuzzing.ENGINES
                },
                'isChromium': utils.is_chromium(),
                'csrfToken': form.generate_csrf_token(),
                'isExternalUser': not is_privileged_or_domain_user,
                'uploadInfo': gcs.prepare_blob_upload()._asdict(),
                'hasIssueTracker': has_issue_tracker,
            },
            'params': params,
            'result': result
        })


class PrepareUploadHandler(base_handler.Handler):
  """Handler that creates an upload URL."""

  @handler.check_user_access(need_privileged_access=False)
  def post(self):
    """Serves the url."""
    return self.render_json({'uploadInfo': gcs.prepare_blob_upload()._asdict()})


class UploadUrlHandlerOAuth(base_handler.Handler):
  """Handler that creates an upload URL (OAuth)."""

  @handler.oauth
  @handler.check_user_access(need_privileged_access=False)
  def post(self):
    """Serves the url."""
    return self.render_json({
        'uploadUrl': request.host_url + UPLOAD_URL,
    })


class JsonHandler(base_handler.Handler):
  """JSON handler for past testcase uploads."""

  @handler.post(handler.JSON, handler.JSON)
  def post(self):
    """Handles a post request."""
    if not helpers.get_user_email():
      raise helpers.AccessDeniedError()

    result, _ = get_result()
    return self.render_json(result)


class UploadHandlerCommon:
  """Handler that uploads the testcase file."""

  def get_upload(self):
    """Get the upload."""
    raise NotImplementedError

  def _handle_upload(self,
                     uploaded_file,
                     job_type,
                     fuzzer_name,
                     target_name,
                     additional_arguments,
                     app_launch_command,
                     gestures,
                     platform_id,
                     http_flag,
                     high_end_job=None,
                     bug_information=None,
                     crash_revision=None,
                     timeout=None,
                     retries=None,
                     bug_summary_update_flag=None,
                     quiet_flag=None,
                     issue_labels=None,
                     stacktrace=None,
                     multiple_testcases=None,
                     trusted_agreement_signed=False,
                     testcase_id=None,
                     testcase_metadata=None) -> str:
    """Holds the logic for actually performing a testcase upload."""
    if testcase_id and not uploaded_file:
      testcase = helpers.get_testcase(testcase_id)
      if not access.can_user_access_testcase(testcase):
        raise helpers.AccessDeniedError()

      # Use minimized testcase for upload (if available).
      if not access.can_user_access_testcase(testcase):
        raise helpers.AccessDeniedError()

      # Use minimized testcase for upload (if available).
      key = (
          testcase.minimized_keys if testcase.minimized_keys and
          testcase.minimized_keys != 'NA' else testcase.fuzzed_keys)

      uploaded_file = blobs.get_blob_info(key)

      # Extract filename part from blob.
      uploaded_file.filename = os.path.basename(
          uploaded_file.filename.replace('\\', os.sep))

    if not job_type:
      raise helpers.EarlyExitError('Missing job name.', 400)

    job = data_types.Job.query(data_types.Job.name == job_type).get()
    if not job:
      raise helpers.EarlyExitError('Invalid job name.', 400)

    job_type_lowercase = job_type.lower()

    for engine in fuzzing.ENGINES:
      if engine.lower() in job_type_lowercase:
        fuzzer_name = engine

    is_engine_job = fuzzer_name and environment.is_engine_fuzzer_job(job_type)

    if not is_engine_job and target_name:
      raise helpers.EarlyExitError(
          'Target name is not applicable to non-engine jobs (AFL, libFuzzer).',
          400)

    if is_engine_job and not target_name:
      raise helpers.EarlyExitError(
          'Missing target name for engine job (AFL, libFuzzer).', 400)

    if (target_name and
        not data_types.Fuzzer.VALID_NAME_REGEX.match(target_name)):
      raise helpers.EarlyExitError('Invalid target name.', 400)

    email = helpers.get_user_email()
    fully_qualified_fuzzer_name = ''
    if is_engine_job and target_name:
      if _is_trusted_uploader_allowed(email) or job.is_external():
        # External jobs don't run and set FuzzTarget entities as part of
        # fuzz_task. Set it here instead.
        # Additionally, record fuzz target here for trusted uploaders
        # to avoid race conditions with newly added fuzz targets.
        fuzz_target = (
            data_handler.record_fuzz_target(fuzzer_name, target_name, job_type))
        fully_qualified_fuzzer_name = fuzz_target.fully_qualified_name()
        target_name = fuzz_target.binary
      else:
        fully_qualified_fuzzer_name, target_name = find_fuzz_target(
            fuzzer_name, target_name, job_type)

    if (not access.has_access(
        need_privileged_access=False,
        job_type=job_type,
        fuzzer_name=(fully_qualified_fuzzer_name or fuzzer_name)) and
        not _is_uploader_allowed(email)):
      helpers.log(f'User {email} does not have access', helpers.VIEW_OPERATION)
      raise helpers.AccessDeniedError()

    # Chrome is the only ClusterFuzz deployment where there are trusted bots
    # running utasks. This check also fails on oss-fuzz because of the way it
    # abuses platform.
    if (not trusted_agreement_signed and utils.is_chromium() and
        task_utils.is_remotely_executing_utasks() and
        ((platform_id and platform_id != 'Linux') or
         job.platform.lower() != 'linux')):
      # Trusted agreement was not signed even though the job has privileges and
      # there are other jobs that don't have privileges.
      raise helpers.EarlyExitError(
          'Sign the trusted job statement or upload to a trusted job.', 400)

    crash_data = None
    if job.is_external():
      if not stacktrace:
        raise helpers.EarlyExitError('Stacktrace required for external jobs.',
                                     400)

      if not crash_revision:
        raise helpers.EarlyExitError('Revision required for external jobs.',
                                     400)

      crash_data = stack_analyzer.get_crash_data(
          stacktrace,
          fuzz_target=target_name,
          symbolize_flag=False,
          already_symbolized=True,
          detect_ooms_and_hangs=True)
    elif stacktrace:
      raise helpers.EarlyExitError(
          'Should not specify stacktrace for non-external jobs.', 400)

    if testcase_metadata:
      try:
        testcase_metadata = json.loads(testcase_metadata)
      except Exception as e:
        raise helpers.EarlyExitError('Invalid metadata JSON.', 400) from e
      if not isinstance(testcase_metadata, dict):
        raise helpers.EarlyExitError('Metadata is not a JSON object.', 400)
    if issue_labels:
      testcase_metadata['issue_labels'] = issue_labels

    try:
      gestures = ast.literal_eval(gestures)
    except Exception as e:
      raise helpers.EarlyExitError('Failed to parse gestures.', 400) from e

    archive_state = 0
    bundled = False
    file_path_input = ''

    # Certain modifications such as app launch command, issue updates are only
    # allowed for privileged users.
    privileged_user = access.has_access(need_privileged_access=True)
    if not privileged_user:
      if bug_information or bug_summary_update_flag:
        raise helpers.EarlyExitError(
            'You are not privileged to update existing issues.', 400)

      need_privileged_access = utils.string_is_true(
          data_handler.get_value_from_job_definition(job_type,
                                                     'PRIVILEGED_ACCESS'))
      if need_privileged_access:
        raise helpers.EarlyExitError(
            'You are not privileged to run this job type.', 400)

      if app_launch_command:
        raise helpers.EarlyExitError(
            'You are not privileged to run arbitrary launch commands.', 400)

      if (testcase_metadata and
          not _allow_unprivileged_metadata(testcase_metadata)):
        raise helpers.EarlyExitError(
            'You are not privileged to set testcase metadata.', 400)

      if additional_arguments:
        raise helpers.EarlyExitError(
            'You are not privileged to add command-line arguments.', 400)

      if gestures:
        raise helpers.EarlyExitError(
            'You are not privileged to run arbitrary gestures.', 400)

    if crash_revision and crash_revision.isdigit():
      crash_revision = int(crash_revision)
    else:
      crash_revision = 0

    if bug_information == '0':  # Auto-recover from this bad input.
      bug_information = None
    if bug_information and not bug_information.isdigit():
      raise helpers.EarlyExitError('Bug is not a number.', 400)

    if not timeout:
      timeout = 0
    elif not timeout.isdigit() or timeout == '0':
      raise helpers.EarlyExitError(
          'Testcase timeout must be a number greater than 0.', 400)
    else:
      timeout = int(timeout)
      if timeout > 120:
        raise helpers.EarlyExitError(
            'Testcase timeout may not be greater than 120 seconds.', 400)

    if retries:
      if retries.isdigit():
        retries = int(retries)
      else:
        retries = None

      if retries is None or retries > MAX_RETRIES:
        raise helpers.EarlyExitError(
            'Testcase retries must be a number less than %d.' % MAX_RETRIES,
            400)
    else:
      retries = None

    job_queue = tasks.queue_for_job(job_type, is_high_end=high_end_job)

    if uploaded_file is not None:
      filename = ''.join([
          x for x in uploaded_file.filename if x not in ' ;/?:@&=+$,{}|<>()\\'
      ])
      key = str(uploaded_file.key())
      if archive.is_archive(filename):
        archive_state = data_types.ArchiveStatus.FUZZED
      if archive_state:
        if multiple_testcases:
          # Create a job to unpack an archive.
          metadata = data_types.BundledArchiveMetadata()
          metadata.blobstore_key = key
          metadata.timeout = timeout
          metadata.job_queue = job_queue
          metadata.job_type = job_type
          metadata.http_flag = http_flag
          metadata.archive_filename = filename
          metadata.uploader_email = email
          metadata.gestures = gestures
          metadata.crash_revision = crash_revision
          metadata.additional_arguments = additional_arguments
          metadata.bug_information = bug_information
          metadata.platform_id = platform_id
          metadata.app_launch_command = app_launch_command
          metadata.fuzzer_name = fuzzer_name
          metadata.overridden_fuzzer_name = fully_qualified_fuzzer_name
          metadata.fuzzer_binary_name = target_name
          metadata.put()

          # Use wait_time=0 to execute the task ASAP, since it is
          # user-facing.
          tasks.add_task(
              'unpack',
              str(metadata.key.id()),
              job_type,
              queue=tasks.queue_for_job(job_type),
              wait_time=0)

          # Create a testcase metadata object to show the user their upload.
          upload_metadata = data_types.TestcaseUploadMetadata()
          upload_metadata.timestamp = datetime.datetime.utcnow()
          upload_metadata.filename = filename
          upload_metadata.blobstore_key = key
          upload_metadata.original_blobstore_key = key
          upload_metadata.status = 'Pending'
          upload_metadata.bundled = True
          upload_metadata.uploader_email = email
          upload_metadata.retries = retries
          upload_metadata.bug_summary_update_flag = bug_summary_update_flag
          upload_metadata.quiet_flag = quiet_flag
          upload_metadata.additional_metadata_string = json.dumps(
              testcase_metadata)
          upload_metadata.bug_information = bug_information
          upload_metadata.put()

          helpers.log('Uploaded multiple testcases.', helpers.VIEW_OPERATION)
          return self.render_json({'multiple': True})

        file_path_input = guess_input_file(uploaded_file, filename)
        if not file_path_input:
          raise helpers.EarlyExitError(
              ("Unable to detect which file to launch. The main file\'s name "
               'must contain either of %s.' % str(RUN_FILE_PATTERNS)), 400)

    else:
      raise helpers.EarlyExitError('Please select a file to upload.', 400)

    testcase_id = data_handler.create_user_uploaded_testcase(
        key,
        key,
        archive_state,
        filename,
        file_path_input,
        timeout,
        job,
        job_queue,
        http_flag,
        gestures,
        additional_arguments,
        bug_information,
        crash_revision,
        email,
        platform_id,
        app_launch_command,
        fuzzer_name,
        fully_qualified_fuzzer_name,
        target_name,
        bundled,
        retries,
        bug_summary_update_flag,
        quiet_flag,
        additional_metadata=testcase_metadata,
        crash_data=crash_data)

    testcase = data_handler.get_testcase_by_id(testcase_id)
    events.emit(
        events.TestcaseCreationEvent(
            testcase=testcase,
            creation_origin=events.TestcaseOrigin.MANUAL_UPLOAD,
            uploader=email))

    if not quiet_flag:
      issue = issue_tracker_utils.get_issue_for_testcase(testcase)
      if issue:
        report_url = data_handler.TESTCASE_REPORT_URL.format(
            domain=data_handler.get_domain(), testcase_id=testcase_id)

        comment = ('ClusterFuzz is analyzing your testcase. '
                   'Developers can follow the progress at %s.' % report_url)
        issue.save(new_comment=comment)

    helpers.log(f'Uploaded testcase {testcase_id}', helpers.VIEW_OPERATION)
    return self.render_json({'id': str(testcase_id)})  # pylint: disable=no-member

  def do_post(self):
    """Upload a testcase."""
    # Set artifical task id env to be used by tracing.
    environment.set_task_id_vars(task_name='upload_testcase')
    testcase_id = request.get('testcaseId')
    uploaded_file = self.get_upload()
    job_type = request.get('job')
    fuzzer_name = request.get('fuzzer')
    target_name = request.get('target')
    multiple_testcases = bool(request.get('multiple'))
    http_flag = bool(request.get('http'))
    high_end_job = bool(request.get('highEnd'))
    bug_information = request.get('issue')
    crash_revision = request.get('revision')
    timeout = request.get('timeout')
    retries = request.get('retries')
    bug_summary_update_flag = bool(request.get('updateIssue'))
    quiet_flag = bool(request.get('quiet'))
    additional_arguments = request.get('args')
    app_launch_command = request.get('cmd')
    platform_id = request.get('platform')
    issue_labels = request.get('issue_labels')
    gestures = request.get('gestures') or '[]'
    stacktrace = request.get('stacktrace')
    trusted_agreement_signed = request.get(
        'trustedAgreement') == TRUSTED_AGREEMENT_TEXT.strip()
    testcase_metadata = request.get('metadata', {})

    return self._handle_upload(
        uploaded_file=uploaded_file,
        testcase_id=testcase_id,
        job_type=job_type,
        fuzzer_name=fuzzer_name,
        target_name=target_name,
        multiple_testcases=multiple_testcases,
        http_flag=http_flag,
        high_end_job=high_end_job,
        bug_information=bug_information,
        crash_revision=crash_revision,
        timeout=timeout,
        retries=retries,
        bug_summary_update_flag=bug_summary_update_flag,
        quiet_flag=quiet_flag,
        additional_arguments=additional_arguments,
        app_launch_command=app_launch_command,
        platform_id=platform_id,
        issue_labels=issue_labels,
        gestures=gestures,
        stacktrace=stacktrace,
        trusted_agreement_signed=trusted_agreement_signed,
        testcase_metadata=testcase_metadata,
    )


class UploadHandler(UploadHandlerCommon, base_handler.GcsUploadHandler):
  """Handler that uploads the testcase file."""

  # pylint: disable=unused-argument
  def before_render_json(self, values, status):
    """Add upload info when the request fails."""
    values['uploadInfo'] = gcs.prepare_blob_upload()._asdict()

  def get_upload(self):
    return base_handler.GcsUploadHandler.get_upload(self)

  @handler.post(handler.FORM, handler.JSON)
  @handler.require_csrf_token
  def post(self):
    return self.do_post()


class NamedBytesIO(io.BytesIO):
  """Named bytesio."""

  def __init__(self, name, value):
    self.name = name
    io.BytesIO.__init__(self, value)


class UploadHandlerOAuth(base_handler.Handler, UploadHandlerCommon):
  """Handler that uploads the testcase file (OAuth)."""

  # pylint: disable=unused-argument
  def before_render_json(self, values, status):
    """Add upload info when the request fails."""
    values['uploadUrl'] = request.host_url + UPLOAD_URL

  def get_upload(self):
    """Get the upload."""
    uploaded_file = request.files.get('file')
    if not uploaded_file:
      raise helpers.EarlyExitError('File upload not found.', 400)

    bytes_io = NamedBytesIO(uploaded_file.filename, uploaded_file.stream.read())
    key = blobs.write_blob(bytes_io)
    return blobs.get_blob_info(key)

  @handler.post(handler.FORM, handler.JSON)
  @handler.oauth
  def post(self, *args):
    return self.do_post()


class CrashReplicationUploadHandler(base_handler.Handler, UploadHandlerCommon):
  """Handler that picks up the pubsub notification."""

  def get_upload(self):
    pass

  @handler.pubsub_push
  def post(self, message):
    """Uploads a crash sampled from fuzz task."""
    with monitor.wrap_with_monitoring():
      message_data = json.loads(message.data.decode('utf-8'))
      helpers.log(f'Message: {type(message)} {message}', helpers.VIEW_OPERATION)

      job = message_data.get('job', None)
      fuzzer = message_data.get('fuzzer', None)
      fuzz_target = message_data.get('target_name', None)
      original_task_id = message_data.get('original_task_id', None)
      helpers.log(f'Uploading testcase from fuzz task id {original_task_id}',
                  helpers.VIEW_OPERATION)
      helpers.log(message.data.decode(), helpers.VIEW_OPERATION)
      uploaded_file_key = message_data['fuzzed_key']
      uploaded_file = blobs.get_blob_info(uploaded_file_key)
      try:
        response = self._handle_upload(
            uploaded_file=uploaded_file,
            job_type=job,
            fuzzer_name=fuzzer,
            target_name=fuzz_target,
            additional_arguments=message_data.get('arguments', None),
            app_launch_command=message_data.get('application_command_line',
                                                None),
            gestures=message_data.get('gestures', '[]'),
            http_flag=message_data.get('http_flag', None),
            platform_id='Linux',
            trusted_agreement_signed=True,
        )
        monitoring_metrics.UPLOAD_TESTCASE_COUNT.increment({
            'fuzzer': fuzzer,
            'job': job,
            'fuzz_target': fuzz_target,
            'success': True,
        })
        return response
      except Exception as e:
        monitoring_metrics.UPLOAD_TESTCASE_COUNT.increment({
            'fuzzer': fuzzer,
            'job': job,
            'fuzz_target': fuzz_target,
            'success': False,
        })
        raise e
