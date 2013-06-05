"""
This file contains tasks that are designed to perform background operations on the 
running state of a course.



"""

import json
from time import time
from sys import exc_info
from traceback import format_exc

from celery import task, current_task
from celery.utils.log import get_task_logger
from celery.states import SUCCESS, FAILURE

from django.contrib.auth.models import User
from django.db import transaction
from dogapi import dog_stats_api

from xmodule.modulestore.django import modulestore

import mitxmako.middleware as middleware
from track.views import task_track

from courseware.models import StudentModule, CourseTaskLog
from courseware.model_data import ModelDataCache
from courseware.module_render import get_module_for_descriptor_internal


# define different loggers for use within tasks and on client side
TASK_LOG = get_task_logger(__name__)

# define custom task state:
PROGRESS = 'PROGRESS'

# define value to use when no task_id is provided:
UNKNOWN_TASK_ID = 'unknown-task_id'


class UpdateProblemModuleStateError(Exception):
    """
    Error signaling a fatal condition while updating problem modules.

    Used when the current module cannot be processed and no more
    modules should be attempted.
    """
    pass


def _perform_module_state_update(course_id, module_state_key, student_identifier, update_fcn, action_name, filter_fcn,
                                          xmodule_instance_args):
    """
    Performs generic update by visiting StudentModule instances with the update_fcn provided.

    StudentModule instances are those that match the specified `course_id` and `module_state_key`.
    If `student_identifier` is not None, it is used as an additional filter to limit the modules to those belonging
    to that student. If `student_identifier` is None, performs update on modules for all students on the specified problem.

    If a `filter_fcn` is not None, it is applied to the query that has been constructed.  It takes one
    argument, which is the query being filtered, and returns the filtered version of the query.

    The `update_fcn` is called on each StudentModule that passes the resulting filtering.
    It is passed three arguments:  the module_descriptor for the module pointed to by the
    module_state_key, the particular StudentModule to update, and the xmodule_instance_args being
    passed through.  If the value returned by the update function evaluates to a boolean True,
    the update is successful; False indicates the update on the particular student module failed.
    A raised exception indicates a fatal condition -- that no other student modules should be considered.

    If no exceptions are raised, a dict containing the task's result is returned, with the following keys:

          'attempted': number of attempts made
          'updated': number of attempts that "succeeded"
          'total': number of possible subtasks to attempt
          'action_name': user-visible verb to use in status messages.  Should be past-tense.
              Pass-through of input `action_name`.
          'duration_ms': how long the task has (or had) been running.

    Because this is run internal to a task, it does not catch exceptions.  These are allowed to pass up to the
    next level, so that it can set the failure modes and capture the error trace in the CourseTaskLog and the
    result object.

    """
    # get start time for task:
    start_time = time()

    # Hack to get mako templates to work on celery worker server's worker thread.
    # The initialization of Mako templating is usually done when Django is
    # initializing middleware packages as part of processing a server request.
    # When this is run on a celery worker server, no such initialization is
    # called. Using @worker_ready.connect doesn't run in the right container.
    #  So we look for the result: the defining of the lookup paths
    # for templates.
    if 'main' not in middleware.lookup:
        TASK_LOG.debug("Initializing Mako middleware explicitly")
        middleware.MakoMiddleware()

    # find the problem descriptor:
    module_descriptor = modulestore().get_instance(course_id, module_state_key)

    # find the module in question
    modules_to_update = StudentModule.objects.filter(course_id=course_id,
                                                     module_state_key=module_state_key)

    # give the option of rescoring an individual student. If not specified,
    # then rescores all students who have responded to a problem so far
    student = None
    if student_identifier is not None:
        # if an identifier is supplied, then look for the student,
        # and let it throw an exception if none is found.
        if "@" in student_identifier:
            student = User.objects.get(email=student_identifier)
        elif student_identifier is not None:
            student = User.objects.get(username=student_identifier)

    if student is not None:
        modules_to_update = modules_to_update.filter(student_id=student.id)

    if filter_fcn is not None:
        modules_to_update = filter_fcn(modules_to_update)

    # perform the main loop
    num_updated = 0
    num_attempted = 0
    num_total = modules_to_update.count()

    def get_task_progress():
        """Return a dict containing info about current task"""
        current_time = time()
        progress = {'action_name': action_name,
                    'attempted': num_attempted,
                    'updated': num_updated,
                    'total': num_total,
                    'duration_ms': int((current_time - start_time) * 1000),
                    }
        return progress

    task_progress = get_task_progress()
    current_task.update_state(state=PROGRESS, meta=task_progress)
    for module_to_update in modules_to_update:
        num_attempted += 1
        # There is no try here:  if there's an error, we let it throw, and the task will
        # be marked as FAILED, with a stack trace.
        with dog_stats_api.timer('courseware.tasks.module.{0}.time'.format(action_name)):
            if update_fcn(module_descriptor, module_to_update, xmodule_instance_args):
                # If the update_fcn returns true, then it performed some kind of work.
                # Logging of failures is left to the update_fcn itself.
                num_updated += 1

        # update task status:
        task_progress = get_task_progress()
        current_task.update_state(state=PROGRESS, meta=task_progress)

    return task_progress


@transaction.autocommit
def _save_course_task_log_entry(entry):
    """Writes CourseTaskLog entry immediately, ensuring the transaction is committed."""
    entry.save()


def _update_problem_module_state(entry_id, course_id, module_state_key, student_ident, update_fcn, action_name, filter_fcn,
                                 xmodule_instance_args):
    """
    Performs generic update by visiting StudentModule instances with the update_fcn provided.

    The `entry_id` is the primary key for the CourseTaskLog entry representing the task.  This function
    updates the entry on success and failure of the _perform_module_state_update function it
    wraps.  It is setting the entry's value for task_state based on what Celery would set it to once
    the task returns to Celery:  FAILURE if an exception is encountered, and SUCCESS if it returns normally.
    Other arguments are pass-throughs to _perform_module_state_update, and documented there.

    If no exceptions are raised, a dict containing the task's result is returned, with the following keys:

          'attempted': number of attempts made
          'updated': number of attempts that "succeeded"
          'total': number of possible subtasks to attempt
          'action_name': user-visible verb to use in status messages.  Should be past-tense.
              Pass-through of input `action_name`.
          'duration_ms': how long the task has (or had) been running.

    Before returning, this is also JSON-serialized and stored in the task_output column of the CourseTaskLog entry.

    If exceptions were raised internally, they are caught and recorded in the CourseTaskLog entry.
    This is also a JSON-serialized dict, stored in the task_output column, containing the following keys:

           'exception':  type of exception object
           'message': error message from exception object
           'traceback': traceback information (truncated if necessary)

    Once the exception is caught, it is raised again and allowed to pass up to the
    task-running level, so that it can also set the failure modes and capture the error trace in the
    result object that Celery creates.

    """
    task_id = current_task.request.id
    fmt = 'Starting to update problem modules as task "{task_id}": course "{course_id}" problem "{state_key}": nothing {action} yet'
    TASK_LOG.info(fmt.format(task_id=task_id, course_id=course_id, state_key=module_state_key, action=action_name))

    # get the CourseTaskLog to be updated.  If this fails, then let the exception return to Celery.
    # There's no point in catching it here.
    entry = CourseTaskLog.objects.get(pk=entry_id)
    entry.task_id = task_id
    _save_course_task_log_entry(entry)

    # add task_id to xmodule_instance_args, so that it can be output with tracking info:
    if xmodule_instance_args is not None:
        xmodule_instance_args['task_id'] = task_id

    # now that we have an entry we can try to catch failures:
    task_progress = None
    try:
        with dog_stats_api.timer('courseware.tasks.module.{0}.overall_time'.format(action_name)):
            task_progress = _perform_module_state_update(course_id, module_state_key, student_ident, update_fcn,
                                                                  action_name, filter_fcn, xmodule_instance_args)
    except Exception:
        # try to write out the failure to the entry before failing
        exception_type, exception, traceback = exc_info()
        traceback_string = format_exc(traceback) if traceback is not None else ''
        task_progress = {'exception': exception_type.__name__, 'message': str(exception.message)}
        TASK_LOG.warning("background task (%s) failed: %s %s", task_id, exception, traceback_string)
        if traceback is not None:
            task_progress['traceback'] = traceback_string[:700]
        entry.task_output = json.dumps(task_progress)
        entry.task_state = FAILURE
        _save_course_task_log_entry(entry)
        raise

    # if we get here, we assume we've succeeded, so update the CourseTaskLog entry in anticipation:
    entry.task_output = json.dumps(task_progress)
    entry.task_state = SUCCESS
    _save_course_task_log_entry(entry)

    # log and exit, returning task_progress info as task result:
    fmt = 'Finishing task "{task_id}": course "{course_id}" problem "{state_key}": final: {progress}'
    TASK_LOG.info(fmt.format(task_id=task_id, course_id=course_id, state_key=module_state_key, progress=task_progress))
    return task_progress


def _get_task_id_from_xmodule_args(xmodule_instance_args):
    """Gets task_id from `xmodule_instance_args` dict, or returns default value if missing."""
    return xmodule_instance_args.get('task_id', UNKNOWN_TASK_ID) if xmodule_instance_args is not None else UNKNOWN_TASK_ID


def _get_module_instance_for_task(course_id, student, module_descriptor, xmodule_instance_args=None,
                                  grade_bucket_type=None):
    """
    Fetches a StudentModule instance for a given `course_id`, `student` object, and `module_descriptor`.

    `xmodule_instance_args` is used to provide information for creating a track function and an XQueue callback.
    These are passed, along with `grade_bucket_type`, to get_module_for_descriptor_internal, which sidesteps
    the need for a Request object when instantiating an xmodule instance.
    """
    # reconstitute the problem's corresponding XModule:
    model_data_cache = ModelDataCache.cache_for_descriptor_descendents(course_id, student, module_descriptor)

    # get request-related tracking information from args passthrough, and supplement with task-specific
    # information:
    request_info = xmodule_instance_args.get('request_info', {}) if xmodule_instance_args is not None else {}
    task_info = {"student": student.username, "task_id": _get_task_id_from_xmodule_args(xmodule_instance_args)}

    def make_track_function():
        '''
        Make a tracking function that logs what happened.

        For insertion into ModuleSystem, and used by CapaModule, which will
        provide the event_type (as string) and event (as dict) as arguments.
        The request_info and task_info (and page) are provided here.
        '''
        return lambda event_type, event: task_track(request_info, task_info, event_type, event, page='x_module_task')

    xqueue_callback_url_prefix = xmodule_instance_args.get('xqueue_callback_url_prefix', '') \
        if xmodule_instance_args is not None else ''

    return get_module_for_descriptor_internal(student, module_descriptor, model_data_cache, course_id,
                                              make_track_function(), xqueue_callback_url_prefix,
                                              grade_bucket_type=grade_bucket_type)


@transaction.autocommit
def _rescore_problem_module_state(module_descriptor, student_module, xmodule_instance_args=None):
    '''
    Takes an XModule descriptor and a corresponding StudentModule object, and
    performs rescoring on the student's problem submission.

    Throws exceptions if the rescoring is fatal and should be aborted if in a loop.
    In particular, raises UpdateProblemModuleStateError if module fails to instantiate,
    and if the module doesn't support rescoring.

    Returns True if problem was successfully rescored for the given student, and False
    if problem encountered some kind of error in rescoring.
    '''
    # unpack the StudentModule:
    course_id = student_module.course_id
    student = student_module.student
    module_state_key = student_module.module_state_key

    instance = _get_module_instance_for_task(course_id, student, module_descriptor, xmodule_instance_args, grade_bucket_type='rescore')

    if instance is None:
        # Either permissions just changed, or someone is trying to be clever
        # and load something they shouldn't have access to.
        msg = "No module {loc} for student {student}--access denied?".format(loc=module_state_key,
                                                                             student=student)
        TASK_LOG.debug(msg)
        raise UpdateProblemModuleStateError(msg)

    if not hasattr(instance, 'rescore_problem'):
        # if the first instance doesn't have a rescore method, we should
        # probably assume that no other instances will either.
        msg = "Specified problem does not support rescoring."
        raise UpdateProblemModuleStateError(msg)

    result = instance.rescore_problem()
    if 'success' not in result:
        # don't consider these fatal, but false means that the individual call didn't complete:
        TASK_LOG.warning("error processing rescore call for course {course}, problem {loc} and student {student}: "
                         "unexpected response {msg}".format(msg=result, course=course_id, loc=module_state_key, student=student))
        return False
    elif result['success'] not in ['correct', 'incorrect']:
        TASK_LOG.warning("error processing rescore call for course {course}, problem {loc} and student {student}: "
                         "{msg}".format(msg=result['success'], course=course_id, loc=module_state_key, student=student))
        return False
    else:
        TASK_LOG.debug("successfully processed rescore call for course {course}, problem {loc} and student {student}: "
                       "{msg}".format(msg=result['success'], course=course_id, loc=module_state_key, student=student))
        return True


def _filter_module_state_for_done(modules_to_update):
    """Filter to apply for rescoring, to limit module instances to those marked as done"""
    return modules_to_update.filter(state__contains='"done": true')


@task
def rescore_problem(entry_id, course_id, task_input, xmodule_instance_args):
    """Rescores problem in `course_id`.

    `entry_id` is the id value of the CourseTaskLog entry that corresponds to this task.
    `course_id` identifies the course.
    `task_input` should be a dict with the following entries:

      'problem_url': the full URL to the problem to be rescored.  (required)
      'student': the identifier (username or email) of a particular user whose
          problem submission should be rescored.  If not specified, all problem
          submissions will be rescored.

    `xmodule_instance_args` provides information needed by _get_module_instance_for_task()
    to instantiate an xmodule instance.
    """
    action_name = 'rescored'
    update_fcn = _rescore_problem_module_state
    filter_fcn = lambda(modules_to_update): modules_to_update.filter(state__contains='"done": true')
    problem_url = task_input.get('problem_url')
    student_ident = None
    if 'student' in task_input:
        student_ident = task_input['student']
    return _update_problem_module_state(entry_id, course_id, problem_url, student_ident,
                                        update_fcn, action_name, filter_fcn=filter_fcn,
                                        xmodule_instance_args=xmodule_instance_args)


@transaction.autocommit
def _reset_problem_attempts_module_state(_module_descriptor, student_module, xmodule_instance_args=None):
    """
    Resets problem attempts to zero for specified `student_module`.

    Always returns true, indicating success, if it doesn't raise an exception due to database error.
    """
    problem_state = json.loads(student_module.state)
    if 'attempts' in problem_state:
        old_number_of_attempts = problem_state["attempts"]
        if old_number_of_attempts > 0:
            problem_state["attempts"] = 0
            # convert back to json and save
            student_module.state = json.dumps(problem_state)
            student_module.save()
            # get request-related tracking information from args passthrough,
            # and supplement with task-specific information:
            request_info = xmodule_instance_args.get('request_info', {}) if xmodule_instance_args is not None else {}
            task_info = {"student": student_module.student.username, "task_id": _get_task_id_from_xmodule_args(xmodule_instance_args)}
            event_info = {"old_attempts": old_number_of_attempts, "new_attempts": 0}
            task_track(request_info, task_info, 'problem_reset_attempts', event_info, page='x_module_task')

    # consider the reset to be successful, even if no update was performed.  (It's just "optimized".)
    return True


@task
def reset_problem_attempts(entry_id, course_id, task_input, xmodule_instance_args):
    """Resets problem attempts to zero for `problem_url` in `course_id` for all students.

    `entry_id` is the id value of the CourseTaskLog entry that corresponds to this task.
    `course_id` identifies the course.
    `task_input` should be a dict with the following entries:

      'problem_url': the full URL to the problem to be rescored.  (required)

    `xmodule_instance_args` provides information needed by _get_module_instance_for_task()
    to instantiate an xmodule instance.
    """
    action_name = 'reset'
    update_fcn = _reset_problem_attempts_module_state
    problem_url = task_input.get('problem_url')
    return _update_problem_module_state(entry_id, course_id, problem_url, None,
                                        update_fcn, action_name, filter_fcn=None,
                                        xmodule_instance_args=xmodule_instance_args)


@transaction.autocommit
def _delete_problem_module_state(_module_descriptor, student_module, xmodule_instance_args=None):
    """
    Delete the StudentModule entry.

    Always returns true, indicating success, if it doesn't raise an exception due to database error.
    """
    student_module.delete()
    # get request-related tracking information from args passthrough,
    # and supplement with task-specific information:
    request_info = xmodule_instance_args.get('request_info', {}) if xmodule_instance_args is not None else {}
    task_info = {"student": student_module.student.username, "task_id": _get_task_id_from_xmodule_args(xmodule_instance_args)}
    task_track(request_info, task_info, 'problem_delete_state', {}, page='x_module_task')
    return True


@task
def delete_problem_state(entry_id, course_id, task_input, xmodule_instance_args):
    """Deletes problem state entirely for `problem_url` in `course_id` for all students.

    `entry_id` is the id value of the CourseTaskLog entry that corresponds to this task.
    `course_id` identifies the course.
    `task_input` should be a dict with the following entries:

      'problem_url': the full URL to the problem to be rescored.  (required)

    `xmodule_instance_args` provides information needed by _get_module_instance_for_task()
    to instantiate an xmodule instance.
    """
    action_name = 'deleted'
    update_fcn = _delete_problem_module_state
    problem_url = task_input.get('problem_url')
    return _update_problem_module_state(entry_id, course_id, problem_url, None,
                                        update_fcn, action_name, filter_fcn=None,
                                        xmodule_instance_args=xmodule_instance_args)