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

# pylint: disable=protected-access
"""Google issue tracker implementation."""

import datetime
import enum
import functools
import re
from typing import List
from typing import Optional
from typing import Sequence
from typing import Tuple
import urllib.parse

from google.auth import exceptions
import googleapiclient.http

from clusterfuzz._internal.issue_management import issue_tracker
from clusterfuzz._internal.issue_management.google_issue_tracker import client
from clusterfuzz._internal.metrics import logs

_NUM_RETRIES = 3

# TODO: Make these configuration settings instead of hardcoded values in code.
# These custom fields use repeated enums.
_CHROMIUM_OS_CUSTOM_FIELD_ID = '1223084'
_CHROMIUM_COMPONENT_TAGS_CUSTOM_FIELD_ID = '1222907'
_CHROMIUM_RELEASE_BLOCK_CUSTOM_FIELD_ID = '1223086'

_OSS_FUZZ_REPORTED_CUSTOM_FIELD_ID = '1349561'
_OSS_FUZZ_PROJECT_CUSTOM_FIELD_ID = '1349507'

_SEVERITY_LABEL_PREFIX = 'Security_Severity-'

_DEFAULT_SEVERITY = 'S4'
_DEFAULT_PRIORITY = 'P4'


class IssueAccessLevel(str, enum.Enum):
  LIMIT_NONE = 'LIMIT_NONE'
  LIMIT_VIEW = 'LIMIT_VIEW'
  LIMIT_APPEND = 'LIMIT_APPEND'
  LIMIT_VIEW_TRUSTED = 'LIMIT_VIEW_TRUSTED'


def _get_access_limit_from_labels(labels: issue_tracker.LabelStore):
  """ Extracts the access limit label from labels and returns the
  corresponding IssueAccessLevel or None."""
  limit_view_label = _extract_label(labels, 'LIMIT_')
  if not limit_view_label:
    return None
  try:
    return IssueAccessLevel('LIMIT_' + limit_view_label)
  except:
    logs.warning('Trying to set issue access level to incorrect value:'
                 f'LIMIT_{limit_view_label}. Ignoring.')
    return None


def retry_on_invalid_gaia_accounts(func):
  """Decorator to retry saving an issue, skipping CCs
    without a GAIA account."""

  @functools.wraps(func)
  def wrapper(self, *args, **kwargs):
    try:
      return func(self, *args, **kwargs)
    except Exception as e:
      # Try to handle the case where a 400 buganizer response is
      # received due to a non gaia email.
      email_regex = r'[\w\.\-\+]+@[\w\.-]+'
      emails_to_skip = re.findall(email_regex, str(e))
      return func(self, *args, **kwargs, skip_emails=emails_to_skip)

  return wrapper


class IssueTrackerError(Exception):
  """Base issue tracker error."""


class IssueTrackerNotFoundError(IssueTrackerError):
  """Not found error."""


class IssueTrackerPermissionError(IssueTrackerError):
  """Permission error."""


def _extract_all_labels(labels: issue_tracker.LabelStore,
                        prefix: str) -> List[str]:
  """Extract all label values."""
  results = []
  labels_to_remove = []
  for label in labels:
    if not label.startswith(prefix):
      continue
    results.append(label[len(prefix):])
    labels_to_remove.append(label)
  for label in labels_to_remove:
    labels.remove(label)
  return results


def _sanitize_oses(oses: Sequence[str]) -> List[str]:
  """Sanitizes the given OS custom field values."""
  result = []
  for os in oses:
    # Skip empty OS values. Workaround for https://crbug.com/366955327.
    if not os:
      continue

    # The OS custom field no longer has the 'Chrome' value.
    # It was replaced by 'ChromeOS'.
    if os == 'Chrome':
      os = 'ChromeOS'

    result.append(os)

  return result


def _extract_label(labels: Sequence[str], prefix: str) -> Optional[str]:
  """Extract a label value."""
  for label in labels:
    if not label.startswith(prefix):
      continue
    result = label[len(prefix):]
    labels.remove(label)
    return result
  return None


def _get_labels(labels: Sequence[str], prefix: str) -> List[str]:
  """Return the values of all labels with the given prefix."""
  results = []
  for label in labels:
    if not label.startswith(prefix):
      continue
    results.append(label[len(prefix):])
  return results


def _get_oss_fuzz_reported_value(labels: Sequence[str]) -> Optional[dict]:
  """Return OSS-Fuzz Reported custom field value."""
  added_reported = _extract_label(labels, 'Reported-')
  if not added_reported:
    return None

  try:
    year, month, day = _parse_date_label(added_reported)
  except ValueError:
    logs.warning(f'Invalid date format for Reported-{added_reported}')
    return None

  return {
      'year': year,
      'month': month,
      'day': day,
  }


def _get_severity_from_labels(labels: Sequence[str]) -> Optional[str]:
  """Return the value of the first severity label, if any."""
  # Ignore case to match `issue_tracker.LabelStore.remove_by_prefix()`.
  values = _get_labels((l.lower() for l in labels),
                       _SEVERITY_LABEL_PREFIX.lower())
  if not values:
    return None

  if len(values) > 1:
    extra = ','.join(values[1:])
    logs.error(
        f'google_issue_tracker: ignoring additional severity labels: [{extra}]')

  value = values[0]
  severity = _get_severity_from_label_value(value)
  logs.info(
      f'google_issue_tracker: severity label = {value}, field = {severity}')
  return severity


def _get_severity_from_label_value(value):
  """Convert a severity label value into a Google issue tracker severity."""
  value = value.lower()
  if value == 'critical':
    return 'S0'
  if value == 'high':
    return 'S1'
  if value == 'medium':
    return 'S2'
  if value == 'low':
    return 'S3'
  # Default case.
  return _DEFAULT_SEVERITY


def _parse_date_label(date_value: str) -> Tuple[int, int, int]:
  """Parse a YYYY-MM-DD string into date components."""
  year, month, day = date_value.split('-')
  return int(year), int(month), int(day)


class Issue(issue_tracker.Issue):
  """Issue tracker issue."""

  def __init__(self, data, is_new, tracker):
    self._data = data
    self._is_new = is_new
    self._issue_tracker = tracker
    ccs = data['issueState'].get('ccs', [])
    self._ccs = issue_tracker.LabelStore(
        [user['emailAddress'] for user in ccs if 'emailAddress' in user])
    collaborators = data['issueState'].get('collaborators', [])
    self._collaborators = issue_tracker.LabelStore([
        user['emailAddress'] for user in collaborators if 'emailAddress' in user
    ])
    labels = [
        str(hotlist_id)
        for hotlist_id in data['issueState'].get('hotlistIds', [])
    ]
    self._labels = issue_tracker.LabelStore(labels)

    component_tags = self._get_component_tags()
    self._component_tags = issue_tracker.LabelStore(component_tags)

    self._body = None
    self._changed = set()
    self._issue_access_limit = IssueAccessLevel.LIMIT_NONE

  def _get_component_tags(self):
    """Returns the value of the Component Tags custom field."""
    custom_fields = self._data['issueState'].get('customFields', [])
    for cf in custom_fields:
      if cf.get('customFieldId') == _CHROMIUM_COMPONENT_TAGS_CUSTOM_FIELD_ID:
        enum_values = cf.get('repeatedEnumValue')
        if enum_values:
          return enum_values.get('values') or []
    return []

  def _get_component_paths(self, component_tags):
    """Converts component IDs from component tags into component paths.

    Eg:  component_id=1456567 will be translated into
         "Blink>JavaScript>Compiler>Sparkplug".
    """
    component_paths = set()
    for ct in component_tags:
      if ct.isnumeric():
        component_path = self._issue_tracker._get_relative_component_path(ct)
        if not component_path:
          logs.warning('google_issue_tracker: Component ID %s did not return '
                       'a component path' % ct)
          continue
        component_paths.add(component_path)
      else:
        # The component tag is already a component path.
        component_paths.add(ct)
    return sorted(component_paths)

  def _filter_custom_field_enum_values(self, custom_field_id, values):
    """Filters out invalid enum values from the provided values."""
    filtered_values = []
    for cf in self._data.get('customFields', []):
      if cf['customFieldId'] == custom_field_id:
        allowed_values = cf['enumValues']
        for v in values:
          if v in allowed_values:
            filtered_values.append(v)
          else:
            logs.info('google_issue_tracker: Value %s for CustomFieldId %s was '
                      'not in allowed values' % (v, custom_field_id))
        break
    return filtered_values

  def _filter_labels(self):
    """Filters out and logs labels that are not hotlist IDs."""
    logs.info(
        'google_issue_tracker: Labels before filtering: %s' % list(self.labels))
    labels_to_remove = []
    for label in self.labels:
      if not label.isnumeric():
        logs.warning('google_issue_tracker: Label %s was not a hotlist ID. '
                     'Removing it.' % label)
        labels_to_remove.append(label)
    for remove_label in labels_to_remove:
      self.labels.remove(remove_label)
    logs.info(
        'google_issue_tracker: Labels after filtering: %s' % list(self.labels))

  def _reset_tracking(self):
    """Resets diff tracking."""
    self._changed.clear()
    self._ccs.reset_tracking()
    self._collaborators.reset_tracking()
    self._labels.reset_tracking()
    self._component_tags.reset_tracking()

  def apply_extension_fields(self, extension_fields):
    """Applies _ext_ prefixed extension fields."""
    if extension_fields.get('_ext_collaborators'):
      logs.info('google_issue_tracker: In apply_extension_fields for '
                'collaborators: %s' % extension_fields['_ext_collaborators'])
      self._changed.add('_ext_collaborators')
      for collaborator in extension_fields['_ext_collaborators']:
        self._collaborators.add(collaborator)

    if extension_fields.get('_ext_issue_access_limit'):
      logs.info('google_issue_tracker: In apply_extension_fields for IAL: %s' %
                extension_fields['_ext_issue_access_limit'])
      self._changed.add('_issue_access_limit')
      self._issue_access_limit = extension_fields['_ext_issue_access_limit']

  @property
  def issue_tracker(self):
    """The issue tracker for this issue."""
    return self._issue_tracker

  @property
  def id(self):
    """The issue identifier."""
    return int(self._data['issueId'])

  @property
  def title(self):
    """The issue title."""
    return self._data['issueState'].get('title')

  @title.setter
  def title(self, new_title):
    self._changed.add('title')
    self._data['issueState']['title'] = new_title

  @property
  def reporter(self):
    """The issue reporter."""
    reporter = self._data['issueState'].get('reporter')
    if not reporter:
      return None
    return reporter['emailAddress']

  @reporter.setter
  def reporter(self, new_reporter):
    self._changed.add('reporter')
    self._data['issueState']['reporter'] = _make_user(new_reporter)

  @property
  def merged_into(self):
    """The issue that this is merged into."""
    return self._data['issueState'].get('canonicalIssueId')

  @property
  def closed_time(self):
    """When the issue was closed."""
    resolved_time = self._data.get('resolvedTime')
    if not resolved_time:
      return None
    return _parse_datetime(resolved_time)

  @property
  def is_open(self):
    """Whether the issue is open."""
    return self.status in ['NEW', 'ASSIGNED', 'ACCEPTED']

  @property
  def status(self):
    """The issue status."""
    return self._data['issueState']['status']

  @status.setter
  def status(self, new_status):
    self._changed.add('status')
    self._data['issueState']['status'] = new_status

  @property
  def body(self):
    """The issue body."""
    if self._body is not None:
      return self._body
    if not self._is_new:
      self._body = ''
      result = self.issue_tracker._execute(self.issue_tracker.client.issues()
                                           .issueUpdates().list(
                                               issueId=str(self.id),
                                               pageSize=1,
                                               sortBy='ASC'))
      logs.info('google_issue_tracker: is_new: %s' % result)
      if 'issueUpdates' not in result:
        return self._body
      if len(result['issueUpdates']) < 1:
        return self._body
      issue_update = result['issueUpdates'][0]
      if issue_update['commentNumber'] != 1:
        return self._body
      self._body = result['issueUpdates'][0]['issueComment']['comment']
    return self._body

  @body.setter
  def body(self, new_body):
    self._body = new_body

  @property
  def assignee(self):
    """The issue assignee."""
    assignee = self._data['issueState'].get('assignee')
    if not assignee:
      return None
    return assignee['emailAddress']

  @assignee.setter
  def assignee(self, new_assignee):
    self._changed.add('assignee')
    self._data['issueState']['assignee'] = _make_user(new_assignee)

  @property
  def created_time(self):
    """The time at which this issue was created."""
    return self._data['createdTime']

  @property
  def ccs(self):
    """The issue CC list."""
    return self._ccs

  @property
  def labels(self):
    """The issue labels list."""
    return self._labels

  @property
  def component_id(self):
    """The issue's component ID."""
    return self._data['issueState']['componentId']

  @component_id.setter
  def component_id(self, component_id):
    """Setter for component_id."""
    self._changed.add('component_id')
    self._data['issueState']['componentId'] = component_id

  @property
  def components(self):
    """The issue's component tags."""
    return self._component_tags

  @property
  def actions(self):
    """Gets the issue actions."""
    page_token = None
    while True:
      result = self.issue_tracker._execute(self.issue_tracker.client.issues()
                                           .issueUpdates().list(
                                               issueId=str(self.id),
                                               pageToken=page_token,
                                               sortBy='ASC'))
      for issue_update in result['issueUpdates']:
        yield Action(issue_update)
      page_token = result.get('nextPageToken')
      if not page_token:
        break

  @property
  def _os_custom_field_values(self) -> List[str]:
    """OS custom field values."""
    custom_fields = self._data['issueState'].get('customFields', [])
    for cf in custom_fields:
      if cf.get('customFieldId') == _CHROMIUM_OS_CUSTOM_FIELD_ID:
        enum_values = cf.get('repeatedEnumValue')
        if enum_values:
          return enum_values.get('values') or []
    return []

  @property
  def _releaseblock_custom_field_values(self):
    """ReleaseBlock custom field values."""
    custom_fields = self._data['issueState'].get('customFields', [])
    for cf in custom_fields:
      if cf.get('customFieldId') == _CHROMIUM_RELEASE_BLOCK_CUSTOM_FIELD_ID:
        enum_values = cf.get('repeatedEnumValue')
        if enum_values:
          return enum_values.get('values') or []
    return []

  @property
  def _foundin_versions(self):
    """FoundIn versions."""
    foundin_versions = self._data['issueState'].get('foundInVersions')
    if not foundin_versions:
      return []
    return foundin_versions

  @property
  def _verifier(self):
    """The issue verifier."""
    verifier = self._data['issueState'].get('verifier')
    if not verifier:
      return None
    return verifier['emailAddress']

  @_verifier.setter
  def _verifier(self, new_verifier):
    self._changed.add('_verifier')
    self._data['issueState']['verifier'] = _make_user(new_verifier)

  def _add_update_single(self,
                         update_body,
                         added,
                         removed,
                         field_name,
                         api_field_name,
                         modifier=None):
    """Prepares a single field update."""
    if field_name not in self._changed:
      return
    new_value = getattr(self, field_name)
    if new_value:
      if modifier:
        new_value = modifier(new_value)
      added.append(api_field_name)
      update_body['add'][api_field_name] = new_value
    else:
      removed.append(api_field_name)

  def _set_severity(self, severity: str):
    self._data['issueState']['severity'] = severity

  def _add_update_collection(self,
                             update_body,
                             added,
                             removed,
                             field_name,
                             api_field_name,
                             modifier=None):
    """Prepares a collection field update."""
    collection = getattr(self, field_name)
    collection_added = list(collection.added)
    if collection_added:
      added.append(api_field_name)
      if modifier:
        collection_added = modifier(collection_added)
      update_body['add'][api_field_name] = collection_added
    collection_removed = list(collection.removed)
    if collection_removed:
      removed.append(api_field_name)
      if modifier:
        collection_removed = modifier(collection_removed)
      update_body['remove'][api_field_name] = collection_removed

  def _update_issue(self, new_comment=None, notify=True):
    """Updates an existing issue."""
    update_body = {
        'add': {},
        'addMask': '',
        'remove': {},
        'removeMask': '',
    }
    if 'status' in self._changed and self.status == 'VERIFIED':
      # Always set verifier, as the caller is doing the verification.
      self._verifier = client.user()
      # VERIFIED also requires assignee to be set, so set it in case it's not
      # set.
      if not self.assignee:
        self.assignee = client.user()
    # Add updates.
    added = []
    removed = []
    self._add_update_single(update_body, added, removed, 'status', 'status')
    self._add_update_single(update_body, added, removed, 'assignee', 'assignee',
                            _make_user)
    self._add_update_single(update_body, added, removed, '_verifier',
                            'verifier', _make_user)
    self._add_update_single(update_body, added, removed, 'reporter', 'reporter',
                            _make_user)
    self._add_update_single(update_body, added, removed, 'title', 'title')
    self._add_update_collection(update_body, added, removed, 'ccs', 'ccs',
                                _make_users)
    self._add_update_collection(update_body, added, removed, '_collaborators',
                                'collaborators', _make_users)
    self._add_update_single(update_body, added, removed, '_issue_access_limit',
                            'access_limit')

    # Custom fields are modified by providing the complete value of the
    # customFieldId.
    custom_field_entries = []

    # Special case OS custom field.
    added_oses = _get_labels(self.labels.added, 'OS-')
    if added_oses:
      oses = set(self._os_custom_field_values)
      new_oses = oses.union(_sanitize_oses(added_oses))
      if oses != new_oses:
        custom_field_entries.append({
            'customFieldId': _CHROMIUM_OS_CUSTOM_FIELD_ID,
            'repeatedEnumValue': {
                'values': list(sorted(new_oses)),
            }
        })
    # Remove all OS labels or they will be attempted to be added as
    # hotlist IDs.
    self.labels.remove_by_prefix('OS-')

    # Special case ReleaseBlock custom field.
    added_releaseblocks = _get_labels(self.labels.added, 'ReleaseBlock-')
    if added_releaseblocks:
      releaseblocks = self._releaseblock_custom_field_values
      releaseblocks.extend(added_releaseblocks)
      custom_field_entries.append({
          'customFieldId': _CHROMIUM_RELEASE_BLOCK_CUSTOM_FIELD_ID,
          'repeatedEnumValue': {
              'values': releaseblocks,
          }
      })
    # Remove all ReleaseBlock labels or they will be attempted to be added as
    # hotlist IDs.
    self.labels.remove_by_prefix('ReleaseBlock-')

    # Special case: OSS-Fuzz "Reported" custom field.
    added_reported = _get_oss_fuzz_reported_value(self.labels)
    if added_reported:
      custom_field_entries.append({
          'customFieldId': _OSS_FUZZ_REPORTED_CUSTOM_FIELD_ID,
          'dateValue': added_reported,
      })

    # Special case: OSS-Fuzz "Project" custom field.
    added_project = _extract_label(self.labels, 'Proj-')
    if added_project:
      # Assume there is only one.
      custom_field_entries.append({
          'customFieldId': _OSS_FUZZ_PROJECT_CUSTOM_FIELD_ID,
          'textValue': added_project
      })

    # Special case Component Tags custom field.
    if self.components.added:
      component_paths = self._get_component_paths(self.components)
      values = self._filter_custom_field_enum_values(
          _CHROMIUM_COMPONENT_TAGS_CUSTOM_FIELD_ID, component_paths)
      if values:
        # Validation check: Do not update component tags if they are the same
        # as on the issue.
        existing_tags = self._get_component_tags()
        if not set(values).issubset(set(existing_tags)):
          logs.info('google_issue_tracker: Going to add these components to '
                    'component tags: %s' % values)
          custom_field_entries.append({
              'customFieldId': _CHROMIUM_COMPONENT_TAGS_CUSTOM_FIELD_ID,
              'repeatedEnumValue': {
                  'values': values,
              }
          })

    if custom_field_entries:
      added.append('customFields')
      update_body['add']['customFields'] = custom_field_entries

    # Special case FoundIn versions.
    added_foundins = _get_labels(self.labels.added, 'FoundIn-')
    if added_foundins:
      foundins = self._foundin_versions
      foundins.extend(added_foundins)
      added.append('foundInVersions')
      update_body['add']['foundInVersions'] = foundins
    # Remove FoundIn labels or they will be attempted to be added as
    # hotlist IDs.
    self.labels.remove_by_prefix('FoundIn-')

    severity = _get_severity_from_labels(self.labels.added)
    if severity is not None:
      self._set_severity(severity)
      added.append('severity')
      update_body['add']['severity'] = severity
      self.labels.remove_by_prefix(_SEVERITY_LABEL_PREFIX)

    update_body['addMask'] = ','.join(added)
    update_body['removeMask'] = ','.join(removed)
    if notify:
      update_body['significanceOverride'] = 'MAJOR'
    else:
      update_body['significanceOverride'] = 'SILENT'
    if new_comment:
      update_body['issueComment'] = {
          'comment': new_comment,
      }
    result = self._data
    if 'component_id' in self._changed:
      # This is a special case here, because changing the component id requires
      # a move operation. However, we still treat that as an update for
      # simplicity.
      move_body = {
          'componentId': self.component_id,
          'significanceOverride': 'MAJOR' if notify else 'SILENT'
      }
      result = self.issue_tracker._execute(
          self.issue_tracker.client.issues().move(
              issueId=str(self.id), body=move_body))
      logs.info('google_issue_tracker: move result: %s' % result)
    if added or removed or new_comment:
      logs.info('google_issue_tracker: modify update_body: %s' % update_body)
      result = self.issue_tracker._execute(
          self.issue_tracker.client.issues().modify(
              issueId=str(self.id), body=update_body))
      logs.info('google_issue_tracker: modify result: %s' % result)
      # Do not use results from modify call, it could contain obfuscated emails
      # See crbug/323736910. Do a seperate get issue call.
      result = self.issue_tracker._execute(
          self.issue_tracker.client.issues().get(issueId=str(self.id)))
      logs.info('google_issue_tracker: get after modify result: %s' % result)

    # Make sure self.labels contains only hotlist IDs.
    self._filter_labels()

    # Special case: hotlists.
    # TODO(ochang): Investigate batching.
    added_hotlists = self.labels.added
    removed_hotlists = self.labels.removed
    logs.info('google_issue_tracker: added_hotlists: %s' % added_hotlists)
    logs.info('google_issue_tracker: removed_hotlists: %s' % removed_hotlists)
    for hotlist in added_hotlists:
      self.issue_tracker._execute(
          self.issue_tracker.client.hotlists().createEntries(
              hotlistId=str(hotlist),
              body={'hotlistEntry': {
                  'issueId': str(self.id),
              }},
          ))
    for hotlist in removed_hotlists:
      self.issue_tracker._execute(self.issue_tracker.client.hotlists()
                                  .entries().delete(
                                      hotlistId=str(hotlist),
                                      issueId=str(self.id)))
    return result

  @retry_on_invalid_gaia_accounts
  def save(self, new_comment=None, notify=True, skip_emails=[]):  # pylint: disable=dangerous-default-value
    """Saves the issue."""
    logs.info(f'Skipping supposed non gaia emails emails: {skip_emails}.')
    if self._is_new:
      logs.info('google_issue_tracker: Creating new issue..')
      priority = _extract_label(self.labels, 'Pri-') or _DEFAULT_PRIORITY
      issue_type = _extract_label(self.labels, 'Type-') or 'BUG'
      self._data['issueState']['type'] = issue_type
      self._data['issueState']['priority'] = priority

      custom_field_entries = []
      oses = _sanitize_oses(_extract_all_labels(self.labels, 'OS-'))
      if oses:
        oses.sort()
        custom_field_entries.append({
            'customFieldId': _CHROMIUM_OS_CUSTOM_FIELD_ID,
            'repeatedEnumValue': {
                'values': oses,
            },
        })
      releaseblocks = _extract_all_labels(self.labels, 'ReleaseBlock-')
      if releaseblocks:
        custom_field_entries.append({
            'customFieldId': _CHROMIUM_RELEASE_BLOCK_CUSTOM_FIELD_ID,
            'repeatedEnumValue': {
                'values': releaseblocks
            },
        })

      # Special case: OSS-Fuzz "Reported" custom field.
      added_reported = _get_oss_fuzz_reported_value(self.labels)
      if added_reported:
        custom_field_entries.append({
            'customFieldId': _OSS_FUZZ_REPORTED_CUSTOM_FIELD_ID,
            'dateValue': added_reported,
        })

      # Special case: OSS-Fuzz "Project" custom field.
      added_project = _extract_label(self.labels, 'Proj-')
      if added_project:
        # Assume there is only one.
        custom_field_entries.append({
            'customFieldId': _OSS_FUZZ_PROJECT_CUSTOM_FIELD_ID,
            'textValue': added_project
        })

      if list(self.components):
        component_paths = self._get_component_paths(self.components)
        logs.info(
            'google_issue_tracker: In save. Going to add these components to '
            'component tags: %s' % component_paths)
        custom_field_entries.append({
            'customFieldId': _CHROMIUM_COMPONENT_TAGS_CUSTOM_FIELD_ID,
            'repeatedEnumValue': {
                'values': component_paths
            },
        })
      if custom_field_entries:
        self._data['issueState']['customFields'] = custom_field_entries

      foundin_values = _extract_all_labels(self.labels, 'FoundIn-')
      if foundin_values:
        self._data['issueState']['foundInVersions'] = foundin_values

      severity = _get_severity_from_labels(self.labels) or _DEFAULT_SEVERITY
      self._set_severity(severity)
      self.labels.remove_by_prefix(_SEVERITY_LABEL_PREFIX)

      # Check for an access_limit label before filtering
      access_limit_from_labels = _get_access_limit_from_labels(self.labels)
      # Make sure self.labels contains only hotlist IDs.
      self._filter_labels()

      if self.component_id:
        self._data['issueState']['componentId'] = int(self.component_id)
      # TODO(vitorguidi): delete this hack once we have a solution for GAIA.
      # Skip CCd users w/o a GAIA account
      ccs = list(set(self._ccs) - set(skip_emails))
      if ccs:
        self._data['issueState']['ccs'] = _make_users(ccs)
      collaborators = list(self._collaborators)
      if collaborators:
        self._data['issueState']['collaborators'] = _make_users(collaborators)
      access_limit = self._issue_access_limit
      if access_limit:
        self._data['issueState']['accessLimit'] = {'accessLevel': access_limit}
      if (access_limit == IssueAccessLevel.LIMIT_NONE and
          access_limit_from_labels):
        self._data['issueState']['accessLimit'] = {
            'accessLevel': access_limit_from_labels.value
        }

      self._data['issueState']['hotlistIds'] = [
          int(label) for label in self.labels
      ]
      if self._body is not None:
        self._data['issueComment'] = {
            'comment': self._body,
        }
      result = self.issue_tracker._execute(
          self.issue_tracker.client.issues().create(
              body=self._data, templateOptions_applyTemplate=True))
      logs.info('google_issue_tracker: result of create: %s' % result)
      self._is_new = False
    else:
      logs.info('google_issue_tracker: Updating issue..')
      # TODO(vitorguidi): remove this once we have a fix for GAIA.
      # Remove all CCd users w/o GAIA accounts
      for email in skip_emails:
        self.ccs.remove(email)
      result = self._update_issue(new_comment=new_comment, notify=notify)
    self._reset_tracking()
    self._data = result
    logs.info('google_issue_tracker: self._data: %s' % self._data)


class Action(issue_tracker.Action):
  """Issue tracker action."""

  # FieldUpdates give us the integer value for statuses, so we need to keep this
  # mapping.
  INT_TO_STATUS = {
      1: 'NEW',
      2: 'ASSIGNED',
      3: 'ACCEPTED',
      4: 'FIXED',
      5: 'VERIFIED',
      6: 'NOT_REPRODUCIBLE',
      7: 'INTENDED_BEHAVIOR',
      8: 'OBSOLETE',
      9: 'INFEASIBLE',
      10: 'DUPLICATE',
  }

  def __init__(self, data):
    self._data = data

  def _get_actual_value(self, value):
    """Gets the actual value of a field update google.protobuf.Any value."""
    if value is None:
      return None
    if 'emailAddress' in value:
      return value['emailAddress']
    if 'value' in value:
      return value['value']
    raise IssueTrackerError('Unknown value type: ' + value['type'])

  def _get_actual_values(self, values):
    """Gets the actual values of field update values."""
    if values is None:
      return None
    return [self._get_actual_value(value) for value in values]

  def _get_field_update(self, field_name):
    """Gets the FieldUpdate for a field name."""
    if 'fieldUpdates' not in self._data:
      return None
    return next(
        (update for update in self._data['fieldUpdates']
         if update['field'] == field_name),
        None,
    )

  def _get_field_update_single(self, field_name):
    """Gets a single field update."""
    update = self._get_field_update(field_name)
    if not update:
      return None, None
    single_value_update = update.get('singleValueUpdate')
    if not single_value_update:
      return None, None
    return (
        self._get_actual_value(single_value_update.get('oldValue')),
        self._get_actual_value(single_value_update.get('newValue')),
    )

  def _get_field_update_changes(self, field_name):
    """Gets a collection field update."""
    update = self._get_field_update(field_name)
    if not update:
      return None, None
    collection_update = update.get('collectionUpdate')
    if not collection_update:
      return None, None
    return (
        self._get_actual_values(collection_update.get('removedValues')),
        self._get_actual_values(collection_update.get('addedValues')),
    )

  @property
  def author(self):
    """The author of the action."""
    return self._data['author']['emailAddress']

  @property
  def comment(self):
    """Represents a comment."""
    if 'issueComment' not in self._data:
      return None
    return self._data['issueComment']['comment']

  @property
  def title(self):
    """The new issue title."""
    return self._get_field_update_single('title')[1]

  @property
  def status(self):
    """The new issue status."""
    return self.INT_TO_STATUS.get(self._get_field_update_single('status')[1])

  @property
  def assignee(self):
    """The new issue assignee."""
    assignee = self._get_field_update_single('assignee')[1]
    if not assignee:
      return None
    return assignee

  @property
  def ccs(self):
    """The issue CC change list."""
    removed, added = self._get_field_update_changes('ccs')
    change_list = issue_tracker.ChangeList()
    if added:
      change_list.added.extend(added)
    if removed:
      change_list.removed.extend(removed)
    return change_list

  @property
  def labels(self):
    """The issue labels change list."""
    # We need to use the snake_case version of the field here, as that's the
    # string value the backend actually uses.
    removed, added = self._get_field_update_changes('hotlist_ids')
    change_list = issue_tracker.ChangeList()
    if added:
      change_list.added.extend(added)
    if removed:
      change_list.removed.extend(removed)
    return change_list

  @property
  def components(self):
    # Component tags will be handled by _update_issue and save.
    return issue_tracker.ChangeList()


class IssueTracker(issue_tracker.IssueTracker):
  """Google issue tracker implementation."""

  def __init__(self, project, http_client, config):
    self._project = project
    self._client = http_client
    self._default_component_id = config['default_component_id']
    self._type = config['type'] if hasattr(config, 'type') else None
    self._url = config['url']

  @property
  def client(self):
    """HTTP Client."""
    if self._client is None:
      self._client = client.build()
    return self._client

  def _execute(self, request):
    """Executes a request."""
    http = None
    for _ in range(2):
      try:
        return request.execute(num_retries=_NUM_RETRIES, http=http)
      except exceptions.RefreshError:
        # Rebuild client and retry request.
        http = client.build_http()
        self._client = client.build('issuetracker', http=http)
        return request.execute(num_retries=_NUM_RETRIES, http=http)
      except client.HttpError as e:
        if e.resp.status == 404:
          raise IssueTrackerNotFoundError(str(e))
        if e.resp.status == 403:
          raise IssueTrackerPermissionError(str(e))
        raise IssueTrackerError(str(e))

  @property
  def project(self):
    """Gets the project name of this issue tracker."""
    return self._project

  @property
  def type(self):
    """The type of the tracker - e.g. monorail, google-issue-tracker, etc."""
    return self._type

  def new_issue(self):
    """Creates an unsaved new issue."""
    data = {
        'issueState': {
            'componentId': self._default_component_id,
            'ccs': [],
            'collaborators': [],
            'hotlistIds': [],
            'accessLimit': {
                'accessLevel': IssueAccessLevel.LIMIT_NONE
            },
        }
    }
    return Issue(data, True, self)

  def _get_relative_component_path(self, component_id):
    """Gets the component path relative to the default component path.

    For component_id=1456567 (Sparkplug) and
    default_component_id=1363614 (Chromium).
    This method will return "Blink>JavaScript>Compiler>Sparkplug" and not
    "Chromium Public Trackers>Chromium>Blink>JavaScript>Compiler/Sparkplug"

    This matches the allowed values format of the Chromium component tags
    custom field.
    """
    try:
      component = self._execute(
          self.client.components().get(componentId=str(component_id)))
    except IssueTrackerNotFoundError:
      return None
    except IssueTrackerError:
      logs.error('Failed to retrieve component.', component_id=component_id)
      return None

    if component['componentId'] == str(self._default_component_id):
      return None
    if component.get('parentComponentId'):
      parent_component_id = component['parentComponentId']
      component_name = component.get('name', '')
      if parent_component_id == str(self._default_component_id):
        return component_name
      return self._get_relative_component_path(
          parent_component_id) + ">" + component_name
    return None

  def get_issue(self, issue_id):
    """Gets the issue with the given ID."""
    try:
      issue = self._execute(self.client.issues().get(issueId=str(issue_id)))
      logs.info('google_issue_tracker: get_issue. issue: %s' % issue)
      return Issue(issue, False, self)
    except IssueTrackerNotFoundError:
      return None
    except IssueTrackerError:
      logs.error('Failed to retrieve issue.', issue_id=issue_id)
      return None

  def get_description(self, issue_id):
    """Gets the content of the description for the issue with the given ID."""
    try:
      comments = self._execute(
          self.client.issues().comments().list(issueId=str(issue_id)))
      logs.info('google_issue_tracker: get_description comments: %s' % comments)
      for comment in comments['issueComments']:
        if comment['commentNumber'] == 1:
          return comment['comment']
      return None
    except IssueTrackerNotFoundError:
      return None
    except IssueTrackerError:
      logs.error('Failed to retrieve issue description.', issue_id=issue_id)
      return None

  def get_attachment_metadata(self, issue_id):
    """Gets the attachment metadata of an issue with the given ID."""
    try:
      attachment_metadata = self._execute(
          self.client.issues().attachments().list(issueId=str(issue_id)))
      logs.info('google_issue_tracker: get_attachment_metadata: %s' %
                attachment_metadata)
      return attachment_metadata['attachments']
    except IssueTrackerNotFoundError:
      return None
    except IssueTrackerError:
      logs.error(
          'Failed to retrieve issue attachment metadata.', issue_id=issue_id)
      return None

  def get_attachment(self, resource_name):
    """Gets the attachment for the given resource name."""
    try:
      http_request = self.client.media().download(resourceName=resource_name)
      http_request.uri = http_request.uri.replace('alt=json', 'alt=media')
      http_request.postproc = googleapiclient.http.HttpRequest.null_postproc
      attachment = self._execute(http_request)
      logs.info('google_issue_tracker: get_attachment attachment downloaded')
      return attachment
    except IssueTrackerNotFoundError:
      return None
    except IssueTrackerError:
      logs.error(
          'Failed to retrieve attachment for resource name: %s' % resource_name)
      return None

  def find_issues(self, keywords=None, only_open=None):
    """Finds issues."""
    return self.find_issues_with_filters(keywords=keywords, only_open=only_open)

  def find_issues_with_filters(self,
                               keywords=None,
                               query_filters=None,
                               only_open=None):
    """Finds issues with additional query filters."""
    page_token = None
    while True:
      issues = self._execute(self.client.issues().list(
          query=_get_query(
              string_keywords=keywords,
              query_filters=query_filters,
              only_open=only_open),
          pageToken=page_token))
      if "issues" not in issues:
        return
      for issue in issues['issues']:
        logs.info('google_issue_tracker: find_issues. issue: %s' % issue)
        yield Issue(issue, False, self)
      page_token = issues.get('nextPageToken')
      if not page_token:
        break

  def find_issues_url(self, keywords=None, only_open=None):
    """Finds issues (web URL)."""
    return self.find_issues_url_with_filters(
        keywords=keywords, only_open=only_open)

  def find_issues_url_with_filters(self,
                                   keywords=None,
                                   query_filters=None,
                                   only_open=None):
    """Finds issues (web URL) with additional query filters."""
    return (self._url + '?' + urllib.parse.urlencode({
        'q':
            _get_query(
                string_keywords=keywords,
                query_filters=query_filters,
                only_open=only_open),
    }))

  def issue_url(self, issue_id):
    """Returns the issue URL with the given ID."""
    return self._url + '/' + str(issue_id)

  @property
  def label_type(self):
    """Label type."""
    return 'hotlist'

  def label_text(self, label):
    """Text for a label (with label type)."""
    return 'hotlistid:' + str(label)


def _make_user(email):
  """Makes a User."""
  return {
      'emailAddress': email,
  }


def _make_users(emails):
  """Makes Users."""
  return [_make_user(email) for email in emails]


def _parse_datetime(date_string):
  """Parses a datetime."""
  datetime_obj, _, microseconds_string = date_string.rstrip('Z').partition('.')
  datetime_obj = datetime.datetime.strptime(datetime_obj, '%Y-%m-%dT%H:%M:%S')
  if microseconds_string:
    microseconds = int(microseconds_string, 10)
    return datetime_obj + datetime.timedelta(microseconds=microseconds)
  return datetime_obj


def _get_query(string_keywords, only_open, query_filters):
  """Gets a search query."""
  query = ' '.join('"{}"'.format(keyword) for keyword in string_keywords)
  if query_filters:
    query += ' '
    query += ' '.join('{}'.format(keyword) for keyword in query_filters)
  if only_open:
    query += ' status:open'
  return query


# Uncomment for local testing. Will need access to a service account for these
# steps to work. List of steps taken (for posterity)-
# 1. gcloud iam service-accounts keys create --iam-account=${service_account} \
#    --key-file-type=json /tmp/sa-key
# 2. pipenv shell
# 3. GOOGLE_APPLICATION_CREDENTIALS=/tmp/sa-key PYTHONPATH=$PYTHONPATH:src/ \
#    python src/clusterfuzz/_internal/issue_management/google_issue_tracker/\
#    issue_tracker.py

# if __name__ == '__main__':
#   it = IssueTracker('chromium', None, {'default_component_id': 1363614})
#
#   # Test _get_component_paths.
#   issue = it.new_issue()
#   issue.components.add('1456407')  # 'Blink'
#   issue.components.add('1456567')  # 'Blink>JavaScript>Compiler>Sparkplug'
#   issue.components.add('1363614')  # 'Chromium'
#   issue.components.add('OS>Software>Enterprise>Policies')
#   issue.components.add('Blink>JavaScript>Compiler>Sparkplug')  # Adding again
#   issue.components.add('Blink>JavaScript>Compiler>Sparkplug')  # Adding again2
#   component_paths = issue._get_component_paths(issue._component_tags)
#   print(component_paths)
#
#   # Test issue creation.
#   issue = it.new_issue()
#   issue.title = 'test issue'
#   issue.assignee = 'rmistry@google.com'
#   issue.status = 'ASSIGNED'
#   issue.labels.add('OS-Linux')
#   issue.labels.add('OS-Android')
#   issue.labels.add('OS-Chrome')
#   issue.labels.add('FoundIn-123')
#   issue.labels.add('FoundIn-789')
#   issue.labels.add('ReleaseBlock-Dev')
#   issue.labels.add('ReleaseBlock-Beta')
#   issue.labels.add('UNKNOWN-LABEL')  # Should be filtered out
#   issue.components.add('1456407')  # 'Blink'
#   issue.components.add('1456567')  # 'Blink>JavaScript>Compiler>Sparkplug'
#   issue.components.add('1363614')  # 'Chromium'
#   issue.components.add('OS>Software>Enterprise>Policies')
#   issue.components.add('Blink>JavaScript>Compiler>Sparkplug')
#   issue.apply_extension_fields({
#       '_ext_collaborators': [
#           'rmistry@google.com',
#           'skia-npm-audit-mirror@skia-public.iam.gserviceaccount.com'
#       ],
#       '_ext_issue_access_limit':
#           IssueAccessLevel.LIMIT_VIEW_TRUSTED,
#   })
#   issue.save(new_comment='testing')
#
#   # Test issue query.
#   queried_issue = it.get_issue(323696390)
#   print(queried_issue._data)
#   print(queried_issue.assignee)
#   queried_issue.labels.add('OS-ChromeOS')
#   queried_issue.labels.add('OS-Chrome')
#   queried_issue.labels.add('OS-Android')
#   queried_issue.labels.add('FoundIn-456')
#   queried_issue.labels.add('FoundIn-6')
#   queried_issue.labels.add('ReleaseBlock-Beta')
#   queried_issue.labels.add('ReleaseBlock-Dev')
#   queried_issue.labels.add('UNKNOWN-LABEL')  # Should be filtered out
#   # 'Blink>JavaScript>Compiler>Sparkplug'
#   queried_issue.components.add('1456567')
#   queried_issue.components.add('OS>Software>Enterprise>ChromeApps')
#   queried_issue.components.add('OS>Systems>Network>General')
#   queried_issue.components.add('asdfasdfasdf')  # Should be filtered out
#   queried_issue.components.add(123123123)  # Should be filtered out
#   queried_issue._update_issue()
