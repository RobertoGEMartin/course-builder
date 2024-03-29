# Copyright 2014 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Classes and methods to create and manage Certificates.

Course creators will need to customize both the appearance of the certificate,
and also the logic used to determine when it has been earned by a student.
The qualification logic can be customized by:
  * using the designated user interface in course settings
  * editing the course.yaml file
  * adding Python code to custom_criteria.py

The appearance of the certificate can be customized either system-wide, or else
on a course-by-course basis. To customize the certificate appearance
system-wide, edit the file templates/certificate.html in this module.

To make a course-specific certificate, upload a file named "certificate.html"
into the View Templates section of the Dashboard > Assets tab. Images and
resources used by this file should also be uploaded in Dashboard > Assets.
"""

__author__ = [
    'Saifu Angto (saifu@google.com)',
    'John Orr (jorr@google.com)']


import os

from mapreduce import context

import appengine_config
from common import safe_dom
from common import schema_fields
from common import tags
from controllers import sites
from controllers import utils
from models import analytics
from models import courses
from models import custom_modules
from models import data_sources
from models import jobs
from models import models
from modules.analytics import student_aggregate
from modules.certificate import custom_criteria
from modules.dashboard import course_settings
from modules.dashboard import tabs

CERTIFICATE_HANDLER_PATH = 'certificate'
RESOURCES_PATH = '/modules/certificate/resources'


class ShowCertificateHandler(utils.BaseHandler):
    """Handler for student to print course certificate."""

    def get(self):
        """Handles GET requests."""
        student = self.personalize_page_and_get_enrolled()
        if not student:
            return

        if not student_is_qualified(student, self.get_course()):
            self.redirect('/')
            return

        environ = self.app_context.get_environ()

        templates_dir = os.path.join(
            appengine_config.BUNDLE_ROOT, 'modules', 'certificate', 'templates')
        template = self.get_template('certificate.html', [templates_dir])
        self.response.out.write(template.render({
            'student': student,
            'course': environ['course']['title'],
            'google_analytics_id': environ['course'].get('google_analytics_id')
        }))


def _get_score_by_id(score_list, assessment_id):
    for score in score_list:
        if score['id'] == str(assessment_id):
            return score
    return None


def _prepare_custom_criterion(custom, student, course):
    assert hasattr(custom_criteria, custom), ((
        'custom criterion %s is not implemented '
        'as a function in custom_criteria.py.') % custom)
    assert (custom in custom_criteria.registration_table), ((
        'Custom criterion %s is not whitelisted '
        'in the registration_table in custom_criteria.py.') % custom)

    def _check_custom_criterion():
        if not getattr(custom_criteria, custom)(student, course):
            return False
        return True

    return _check_custom_criterion


def _prepare_assessment_criterion(score_list, criterion):
    score = _get_score_by_id(score_list, criterion['assessment_id'])
    assert score is not None, (
        'Invalid assessment id %s.' % criterion['assessment_id'])
    pass_percent = criterion.get('pass_percent', '')
    if pass_percent is not '':
        # Must be machine graded
        assert not score['human_graded'], (
            'If pass_percent is provided, '
            'the assessment must be machine graded.')
        pass_percent = float(pass_percent)
        assert (pass_percent >= 0.0) and (pass_percent <= 100.0), (
            'pass_percent must be between 0 and 100.')
    else:
        # Must be peer graded
        assert score['human_graded'], (
            'If pass_percent is not provided, '
            'the assessment must be human graded.')

    def _check_assessment_criterion():
        if not score['completed']:
            return False
        if pass_percent is not '':
            return score['score'] >= pass_percent
        return True

    return _check_assessment_criterion


def student_is_qualified(student, course):
    """Determines whether the student has met criteria for a certificate.

    Args:
        student: models.models.Student. The student entity to test.
        course: modesl.courses.Course. The course which the student is
            enrolled in.

    Returns:
        True if the student is qualified, False otherwise.
    """
    environ = course.app_context.get_environ()
    score_list = course.get_all_scores(student)

    if not environ.get('certificate_criteria'):
        return False

    criteria_functions = []
    # First validate the correctness of _all_ provided criteria
    for criterion in environ['certificate_criteria']:
        assessment_id = criterion.get('assessment_id', '')
        custom = criterion.get('custom_criteria', '')
        assert (assessment_id is not '') or (custom is not ''), (
            'assessment_id and custom_criteria cannot be both empty.')
        if custom is not '':
            criteria_functions.append(
                _prepare_custom_criterion(custom, student, course))
        elif assessment_id is not '':
            criteria_functions.append(
                _prepare_assessment_criterion(score_list, criterion))
        else:
            assert False, 'Invalid certificate criterion %s.' % criterion

    # All criteria are valid, now do the checking.
    for criterion_function in criteria_functions:
        if not criterion_function():
            return False

    return True


def get_certificate_table_entry(handler, student, course):
    # I18N: Title of section on page showing certificates for course completion.
    title = handler.gettext('Certificate')

    if student_is_qualified(student, course):
        link = safe_dom.A(
            CERTIFICATE_HANDLER_PATH
        ).add_text(
            # I18N: Label on control to navigate to page showing certificate.
            handler.gettext('Click for certificate'))
        return (title, link)
    else:
        return (
            title,
            # I18N: Text indicating student has not yet completed a course.
            handler.gettext(
                'You have not yet met the course requirements for a '
                'certificate of completion.'))


def get_criteria_editor_schema(course):
    criterion_type = schema_fields.FieldRegistry(
        'Criterion',
        extra_schema_dict_values={'className': 'settings-list-item'})

    select_data = [('default', '-- Select requirement --'), (
        '', '-- Custom criterion --')]
    for unit in course.get_assessment_list():
        select_data.append((unit.unit_id, unit.title + (
            ' [Peer Graded]' if course.needs_human_grader(unit) else '')))

    criterion_type.add_property(schema_fields.SchemaField(
        'assessment_id', 'Requirement', 'string', optional=True,
        # The JS will only reveal the following description
        # for peer-graded assessments
        description='When specifying a peer graded assessment as criterion, '
            'the student should complete both the assessment '
            'and the minimum of peer reviews.',
        select_data=select_data,
        extra_schema_dict_values={
            'className': 'inputEx-Field assessment-dropdown'}))

    criterion_type.add_property(schema_fields.SchemaField(
        'pass_percent', 'Passing Percentage', 'string', optional=True,
        extra_schema_dict_values={
            'className': 'pass-percent'}))

    select_data = [('', '-- Select criterion method--')] + [(
        x, x) for x in custom_criteria.registration_table]
    criterion_type.add_property(schema_fields.SchemaField(
        'custom_criteria', 'Custom Criterion', 'string', optional=True,
        select_data=select_data,
        extra_schema_dict_values={
            'className': 'custom-criteria'}))

    is_peer_assessment_table = {}
    for unit in course.get_assessment_list():
        is_peer_assessment_table[unit.unit_id] = (
            True if course.needs_human_grader(unit) else False)

    return schema_fields.FieldArray(
        'certificate_criteria', 'Certificate criteria',
        item_type=criterion_type,
        description='Certificate award criteria. Add the criteria which '
            'students must meet to be awarded a certificate of completion. '
            'In order to receive a certificate, '
            'the student must meet all the criteria.',
        extra_schema_dict_values={
            'is_peer_assessment_table': is_peer_assessment_table,
            'className': 'settings-list',
            'listAddLabel': 'Add a criterion',
            'listRemoveLabel': 'Delete criterion'})


TOTAL_CERTIFICATES = 'total_certificates'
TOTAL_ACTIVE_STUDENTS = 'total_active_students'
TOTAL_STUDENTS = 'total_students'


class CertificatesEarnedGenerator(jobs.AbstractCountingMapReduceJob):

    @staticmethod
    def get_description():
        return 'certificates earned'

    def build_additional_mapper_params(self, app_context):
        return {'course_namespace': app_context.get_namespace_name()}

    @staticmethod
    def entity_class():
        return models.Student

    @staticmethod
    def map(student):
        params = context.get().mapreduce_spec.mapper.params
        ns = params['course_namespace']
        app_context = sites.get_course_index().get_app_context_for_namespace(ns)
        course = courses.Course(None, app_context=app_context)
        if student_is_qualified(student, course):
            yield(TOTAL_CERTIFICATES, 1)
        if student.scores:
            yield(TOTAL_ACTIVE_STUDENTS, 1)
        yield(TOTAL_STUDENTS, 1)


class CertificatesEarnedDataSource(data_sources.SynchronousQuery):

    @staticmethod
    def required_generators():
        return [CertificatesEarnedGenerator]

    @classmethod
    def get_name(cls):
        return 'certificates_earned'

    @classmethod
    def get_title(cls):
        return 'Certificates Earned'

    @classmethod
    def get_schema(cls, unused_app_context, unused_catch_and_log,
                   unused_source_context):
        reg = schema_fields.FieldRegistry(
            'Certificates Earned',
            description='Scalar values aggregated over entire course giving '
            'counts of certificates earned/not-yet-earned.  Only one row will '
            'ever be returned from this data source.')
        reg.add_property(schema_fields.SchemaField(
            TOTAL_STUDENTS, 'Total Students', 'integer',
            description='Total number of students in course'))
        reg.add_property(schema_fields.SchemaField(
            TOTAL_CERTIFICATES, 'Total Certificates', 'integer',
            description='Total number of certificates earned'))
        reg.add_property(schema_fields.SchemaField(
            TOTAL_ACTIVE_STUDENTS, 'Total Active Students', 'integer',
            description='Number of "active" students.  These are students who '
            'have taken at least one assessment.  Note that it is not likely '
            'that a student has achieved a certificate without also being '
            'considered "active".'))
        return reg.get_json_schema_dict()['properties']

    @staticmethod
    def fill_values(app_context, template_values, certificates_earned_job):
        # Set defaults
        template_values.update({
            TOTAL_CERTIFICATES: 0,
            TOTAL_ACTIVE_STUDENTS: 0,
            TOTAL_STUDENTS: 0,
            })
        # Override with actual values from m/r job, if present.
        template_values.update(
            jobs.MapReduceJob.get_results(certificates_earned_job))


def register_analytic():
    data_sources.Registry.register(CertificatesEarnedDataSource)
    name = 'certificates_earned'
    title = 'Certificates Earned'
    certificates_earned = analytics.Visualization(
        name, title, 'certificates_earned.html',
        data_source_classes=[CertificatesEarnedDataSource])
    tabs.Registry.register('analytics', name, title, [certificates_earned])


class CertificateAggregator(
    student_aggregate.AbstractStudentAggregationComponent):

    @classmethod
    def get_name(cls):
        return 'certificate'

    @classmethod
    def get_event_sources_wanted(cls):
        return []

    @classmethod
    def build_static_params(cls, unused_app_context):
        return None

    @classmethod
    def process_event(cls, event, static_params):
        return None

    @classmethod
    def produce_aggregate(cls, course, student, unused_static_params,
                          unused_event_items):
        return {'earned_certificate': student_is_qualified(student, course)}

    @classmethod
    def get_schema(cls):
        return schema_fields.SchemaField(
          'earned_certificate', 'Earned Certificate', 'boolean',
          description='Whether the student has earned a course completion '
          'certificate based on the criteria in place when this fact was '
          'generated.')


custom_module = None


def register_module():
    """Registers this module in the registry."""

    def on_module_enabled():
        register_analytic()
        course_settings.CourseSettingsRESTHandler.REQUIRED_MODULES.append(
            'inputex-list')
        courses.Course.OPTIONS_SCHEMA_PROVIDERS[
            courses.Course.SCHEMA_SECTION_COURSE].append(
                get_criteria_editor_schema)
        course_settings.CourseSettingsHandler.ADDITIONAL_DIRS.append(
            os.path.dirname(__file__))
        course_settings.CourseSettingsHandler.EXTRA_CSS_FILES.append(
            'course_settings.css')
        course_settings.CourseSettingsHandler.EXTRA_JS_FILES.append(
            'course_settings.js')
        utils.StudentProfileHandler.EXTRA_STUDENT_DATA_PROVIDERS.append(
            get_certificate_table_entry)
        student_aggregate.StudentAggregateComponentRegistry.register_component(
            CertificateAggregator)

    def on_module_disabled():
        course_settings.CourseSettingsRESTHandler.REQUIRED_MODULES.remove(
            'inputex-list')
        courses.Course.OPTIONS_SCHEMA_PROVIDERS[
            courses.Course.SCHEMA_SECTION_COURSE].remove(
                get_criteria_editor_schema)
        course_settings.CourseSettingsHandler.ADDITIONAL_DIRS.remove(
            os.path.dirname(__file__))
        course_settings.CourseSettingsHandler.EXTRA_CSS_FILES.remove(
            'course_settings.css')
        course_settings.CourseSettingsHandler.EXTRA_JS_FILES.remove(
            'course_settings.js')
        utils.StudentProfileHandler.EXTRA_STUDENT_DATA_PROVIDERS.remove(
            get_certificate_table_entry)

    global_routes = [
        (os.path.join(RESOURCES_PATH, '.*'), tags.ResourcesHandler)]

    namespaced_routes = [
        ('/' + CERTIFICATE_HANDLER_PATH, ShowCertificateHandler)]

    global custom_module  # pylint: disable=global-statement
    custom_module = custom_modules.Module(
        'Show Certificate',
        'A page to show student certificate.',
        global_routes, namespaced_routes,
        notify_module_disabled=on_module_disabled,
        notify_module_enabled=on_module_enabled)
    return custom_module
