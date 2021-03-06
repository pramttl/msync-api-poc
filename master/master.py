import os, sys
sys.path.append(os.path.join(os.path.dirname(__file__), "../"))

from pytz import utc
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ProcessPoolExecutor
from apscheduler.job import Job
from dateutil.parser import parse

from flask import Flask, request, session, jsonify, url_for, g, abort
from flask.ext.httpauth import HTTPBasicAuth
#from flask.ext.login import current_user
from models import db

app = Flask(__name__)
app.config.from_object('settings')
db.init_app(app)
auth = HTTPBasicAuth()

import requests
import simplejson as json
from utils.syncing import rsync_call, rsync_call_nonblocking

LOG_FILE = "logfile.txt"
import logging

##################### SCHEDULER CONFIG #########################
# Configuring a persistent job store and instantiating scheduler
# Scheduler starts along with the app (just before)

jobstores = {
    'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')
}

executors = {
    'default': {'type': 'threadpool', 'max_workers': 20},
    'processpool': ProcessPoolExecutor(max_workers=5)
}

job_defaults = {
    'coalesce': False,
    'max_instances': 3
}

############################  MODELS  #############################
from models import User, SlaveNode

####################### UTILITY FUNCTIONS ########################
def sync_project_from_upstream(project, rsync_host, rsync_module, dest, rsync_password,
                               rsync_options):
    """
    Sync's project from upstream and after upstream-master syncing is over,
    instructs each of the slave nodes to sync from the master.

    Note: The same rsync_options are used for upstream-master syncing as for
    the master-slave syncing.
    """
    full_source = '%s@%s::%s' % (project, rsync_host, rsync_module)
    dest = '%s/%s' % (dest, project)

    print('Syncing up %s' % (project,))

    # Blocking rsync call
    rsync_call(full_source, dest, rsync_password,
               rsync_options['basic'],
               rsync_options['defaults'],
               rsync_options['delete'],)

    with app.app_context():
        ftp_hosts = SlaveNode.query.all()

    headers = {'Content-type': 'application/json', 'Accept': 'text/plain'}

    # Tell all ftp hosts to sync from the master
    for node in ftp_hosts:
        slave_api_url = 'http://%s:%s/sync_from_master/' % (node.hostname, node.port)
        data = {
         'project': project,
          #"rsync_module": settings.MASTER_RSYNCD_MODULE + '/' + project, # rsync module
         'rsync_host': app.config['MASTER_HOSTNAME'],
         'rsync_password': app.config['MASTER_RSYNCD_PASSWORD'],
         'rsync_options' : rsync_options,
         'slave_id': node.id,
          }

        # Slave api rsync should be non blocking so that this call returns immediately.
        r = requests.post(slave_api_url, data=json.dumps(data), headers=headers)

        # Analyze respone


@auth.verify_password
def verify_password(username_or_token, password):
    """
    Function useful for authentication.
    Credits: http://blog.miguelgrinberg.com/post/restful-authentication-with-flask
    """
    # first try to authenticate by token
    user = User.verify_auth_token(username_or_token)
    if not user:
        # try to authenticate with username/password
        user = User.query.filter_by(username = username_or_token).first()
        if not user or not user.verify_password(password):
            return False
    g.user = user
    return True


from functools import wraps
def allowed_roles(*roles):
    """
    One user can only have one role. See User model.
    Two roles possible: 'root', 'upstreamuser'
    Usage example: @required_roles('root', 'upstreamuser',)
    """
    def wrapper(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            username = auth.username()
            user = User.query.filter_by(username=username).first()
            if user.role not in roles:
                return "You are not allowed to access this resource"
            return f(*args, **kwargs)
        return wrapped
    return wrapper


######################### VIEWS: AUTH X 2  ##############################

@app.route('/test/', methods = ['GET'])
@allowed_roles('root',)
@auth.login_required
def test_function():
    return jsonify({ 'success': True })


@app.route('/create_api_user/', methods = ['POST',])
@allowed_roles('root',)
@auth.login_required
def new_user():
    """
    Only root users can create new api users.
    """
    username = request.json.get('username')
    password = request.json.get('password')
    # Exists already
    if User.query.filter_by(username=username).first():
        abort(400)
    user = User(username, password)
    db.session.add(user)
    db.session.commit()
    return jsonify({ 'username': user.username })


@app.route('/list_users/', methods = ['GET',])
@allowed_roles('root',)
@auth.login_required
def list_users():
    users = User.query.all()
    users = [{'username': user.username , 'role': user.role} for user in users]
    return json.dumps(users)


@app.route('/get_token/', methods = ['GET',])
@auth.login_required
def get_token():
    """
    Any user should be able to get an access token.
    """
    username = auth.username()
    user = User.query.filter_by(username=username).first()
    token = user.generate_auth_token()
    return json.dumps({'token': token})

######################### VIEWS: API ENDPOINTS ##########################
@app.route('/add_slave/', methods=['POST', ])
@allowed_roles('root',)
@auth.login_required
def add_slave():
    """
    Add a slave node or ftp host to the the FTP setup.
    Only root users can add slave nodes to the cluster.
    """
    obj = request.json
    hostname = obj["hostname"]
    slave_node = SlaveNode.query.filter(SlaveNode.hostname == hostname).first()

    if slave_node:
        details = 'Already added'
        print('Slave already present')
    else:
        ftp_host = SlaveNode(obj['hostname'], obj['port'])
        db.session.add(ftp_host)
        db.session.commit()
        details = 'Added to cluster'
        print('Slave added')

    return jsonify({'method': 'add_slave', 'success': True,
                    'hostname': obj['hostname'],
                    'details': details })


@app.route('/list_slaves/', methods=['GET', ])
@allowed_roles('root',)
@auth.login_required
def list_slaves():
    """
    Returns a list of all slave objects in JSON format.
    """
    ftp_hosts = SlaveNode.query.all()
    ftp_hosts = [obj.to_dict for obj in ftp_hosts]
    return json.dumps(ftp_hosts)


@app.route('/remove_slave/', methods=['POST', ])
@allowed_roles('root',)
@auth.login_required
def remove_slave():
    """
    Remove a slave node from the ftp cluster.
    """
    slave_hostname = request.json['hostname']
    stored_slave = SlaveNode.query.filter(SlaveNode.hostname == slave_hostname).first()
    details = ''

    if stored_slave:
        db.session.delete(stored_slave)
        db.session.commit()
        details = 'Found and deleted'

    return jsonify({'method': 'remove_slave', 'success': True,
                    'details': details })


@app.route('/add_project/', methods=['POST', ])
@allowed_roles('root',)
@auth.login_required
def add_project():
    """
    Schedules an upstream project for syncing (periodic)

    @project: Name of the unix user of the upstream project server
    @rsync_host: IP or hostname of the upstream rsync provider
    @rsync_module: <rsync_module_name>/relative_path_from_there
    """
    #XXX: Anyone can add a project, but we should also store which user
    # added what project.
    project_obj = request.json
    project = project_obj["project"]
    job_id = project_obj.get('id') or project

    job_kwargs = {'project': project,
                  'rsync_host': project_obj['rsync_host'],
                  'rsync_module': project_obj['rsync_module'],
                  'dest': project_obj['dest'],
                  'rsync_password': project_obj['rsync_password'],
                  'rsync_options': {}
                  }
    rsync_options = project_obj.get('rsync_options')

    if rsync_options:
        job_kwargs['rsync_options']['basic'] = rsync_options.get('basic', [])
        job_kwargs['rsync_options']['defaults'] = rsync_options.get('defaults', app.config['RSYNC_DEFAULT_OPTIONS'])
        job_kwargs['rsync_options']['delete'] = rsync_options.get('delete', app.config['RSYNC_DELETE_OPTION'])
    else:
        job_kwargs['rsync_options']['basic'] = []
        job_kwargs['rsync_options']['defaults'] = app.config['RSYNC_DEFAULT_OPTIONS']
        job_kwargs['rsync_options']['delete'] = app.config['RSYNC_DELETE_OPTION']

    # Reading the schedule parameters into a separate dictionary
    schedule_kwargs = project_obj['cron_options']

    # Fuzzy parsing of the schedule time, to accomodate a wide variety of user provided formats.
    # Unix date format is acceptable: Fri Jul 11 03:07:30 IST 2014
    schedule_kwargs['start_date'] = parse(schedule_kwargs['start_date'], fuzzy=True)

    #Todo: Might need a check to make sure all the sub parameters in the cron
    # parameter are acceptable ie. belong to ['start_date', 'minute', 'hour, ...]

    logging.basicConfig()

    # Add the job to the already running scheduler
    print schedule_kwargs
    ct = CronTrigger(**schedule_kwargs)
    job = scheduler.add_job(func=sync_project_from_upstream, id=job_id, trigger=ct, kwargs=job_kwargs)
    #job = Job(id=job_id, func=sync_project_from_upstream, scheduler=scheduler, trigger=ct, args=[], kwargs=job_kwargs)
    print "job_id"
    print job.id

    return jsonify({'method': 'add_project', 'success': True, 'project': project })


@app.route('/list_projects/', methods=['GET', ])
def list_projects():
    """
    List all the upstream projects scheduled for syncing.
    """
    jobs = scheduler.get_jobs()
    projects = []
    for job in jobs:

        schedule_dict = dict(zip(CronTrigger.FIELD_NAMES, job.trigger.fields))
        keys = schedule_dict.keys()
        values = map(str, schedule_dict.values())
        cleaned_schedule_dict = dict(zip(keys, values))
        job_kwargs_copy = job.kwargs.copy()

        # Add cron parameters of the job along with the other parameters.
        for key, value in schedule_dict.items():
            job_kwargs_copy['cron_options'] = cleaned_schedule_dict

        job_kwargs_copy['id'] = job.id
        if not job.next_run_time:
            job_kwargs_copy['enabled'] = False
        else:
            job_kwargs_copy['enabled'] = True

        # The copy stores the basic parameters as well as the schedule parameters.
        projects.append(job_kwargs_copy)
    return json.dumps(projects)


@app.route('/remove_project/', methods=['POST', ])
@allowed_roles('root',)
@auth.login_required
def remove_project():
    """
    Remove an upstream project from the master node.
    """
    #XXX: This endpoint should be available for all users, but they should only
    # be allowed to remove projects they own.
    project_obj = request.json
    job_id = project_obj['id']
    scheduler.remove_job(job_id)

    return jsonify({'method': 'remove_project', 'success': True, 'id': job_id})


@app.route('/disable_project/', methods=['GET', ])
@allowed_roles('root',)
@auth.login_required
def pause_job():
    """
    Pause syncing for a project.
    """
    project_id = request.args.get('id')

    if project_id:
        job = scheduler.get_job(job_id=project_id)
        print dir(job)
        job.pause()
        return jsonify({'method': 'disable_project', 'success': True,})
    else:
        return jsonify({'method': 'disable_project', 'success': False,
                        'note': 'No project id provided'})


@app.route('/enable_project/', methods=['GET', ])
@allowed_roles('root',)
@auth.login_required
def resume_job():
    """
    Resume syncing for a project.
    """
    project_id = request.args.get('id')

    if project_id:
        job = scheduler.get_job(job_id=project_id)
        job.resume()
        return jsonify({'method': 'enable_project', 'success': True,})
    else:
        return jsonify({'method': 'enable_project', 'success': False,
                        'note': 'No project id provided'})


@app.route('/syncup/', methods=['GET', ])
def syncup_project():
    """
    Allows to explicitly initiate syncing for a project.
    """
    project_id = request.args.get('id')

    if project_id:
        job = scheduler.get_job(job_id=project_id)

        rsync_host = job.kwargs['rsync_host']
        rsync_module = job.kwargs['rsync_module']
        project = job.kwargs['project']
        dest = '%s/%s' % (job.kwargs['dest'], project)
        password = job.kwargs['rsync_password']
        rsync_options = job.kwargs['rsync_options']

        full_source = '%s@%s::%s' % (project, rsync_host, rsync_module)
        rsync_call_nonblocking(full_source, dest, password,
                               rsync_options['basic'],
                               rsync_options['defaults'],
                               rsync_options['delete'])

        return jsonify({'method': 'syncup_project', 'success': True,
                        'project_id': projec_id, 'note': 'sync initiated'})
    else:
        return jsonify({'method': 'syncup_project', 'success': False,
                        'note': 'No project id provided'})


@app.route('/update_project/basic/', methods=['POST', ])
@allowed_roles('root',)
@auth.login_required
def update_project_settings():
    """
    Updating basic settings of a project scheduled for syncing.
    - Updating name, rsync_module, rsync_host, dest is allowed.
    - The cron schedule parameters cannot be changed from this endpoint.

    @project: (Required) Orignal name of the project.
    @new_name: (Optional) New name of the project.
    Other parameters are same as in add_project endpoint.
    """
    project_obj = request.json

    # If there is any new project name then use it else use the old project name.
    job_id = project_obj['id']

    # Finding the job in context and updating it.
    jobs = scheduler.get_jobs()
    job_kwargs = {}
    job = scheduler.get_job(job_id)
    job_kwargs['project'] = project_obj.get('project') or job.kwargs['project']
    job_kwargs['rsync_host'] = project_obj.get('rsync_host') or job.kwargs['rsync_host']
    job_kwargs['rsync_module'] = project_obj.get('rsync_module') or job.kwargs['rsync_module']
    job_kwargs['dest'] = project_obj.get('dest') or job.kwargs['dest']
    job_kwargs['rsync_password'] = project_obj.get('rsync_password') or job.kwargs['rsync_password']
    job_kwargs['rsync_options'] = project_obj.get('rsync_options') or job.kwargs['rsync_options']

    if not job_kwargs['rsync_options'].get('basic'):
        job_kwargs['rsync_options']['basic'] = []
    if not job_kwargs['rsync_options'].get('defaults'):
        # If 'defaults' option is not present in new rsync_options then use preset defaults.
        job_kwargs['rsync_options']['defaults'] = app.config['RSYNC_DEFAULT_OPTIONS']
    if not job_kwargs['rsync_options'].get('delete'):
        job_kwargs['rsync_options']['delete'] = app.config['RSYNC_DELETE_OPTION']

    job.modify(kwargs=job_kwargs)

    return jsonify({'method': 'update_project', 'success': True, 'id': job.id})


@app.route('/update_project/schedule/', methods=['POST', ])
def update_project_schedule():
    """
    To update the schedule parameters while updating a project.

    Rule: Specify all the schedule parameters again while updating. The previous
    schedule parameters will be overwritten.

    The only way to update the schedule parameters in Apscheduler v2 is to delete
    the job and add it again with the new parameters. (Apscheduler v3 should have a
    direct way to do this)
    """
    project_obj = request.json

    job_id = project_obj["id"]
    job = scheduler.get_job(job_id)

    # Reading the schedule parameters into a separate dictionary
    schedule_kwargs = {}

    # This is to make sure that default APScheduler values are not overwritten by None
    if project_obj.get('start_date'):
        schedule_kwargs['start_date'] = project_obj.get('start_date')
        schedule_kwargs['start_date'] = parse(schedule_kwargs['start_date'], fuzzy=True)

    if project_obj.get('month'):
        schedule_kwargs['month'] = project_obj.get('month')

    if project_obj.get('day'):
        schedule_kwargs['day'] = project_obj.get('day')

    if project_obj.get('hour'):
        schedule_kwargs['hour'] = project_obj.get('hour')

    if project_obj.get('minute'):
        schedule_kwargs['minute'] = project_obj.get('minute')

    if project_obj.get('day_of_week'):
        schedule_kwargs['day_of_week'] = project_obj.get('day_of_week')

    ct = CronTrigger(**schedule_kwargs)
    job.reschedule(trigger=ct)
    action_status = True

    logging.basicConfig()

    return jsonify({'method': 'update_project', 'success': action_status,
                    'id': job_id})


@app.route('/slave_rsync_complete/', methods=['POST', ])
def slave_rsync_complete():
    """
    When a slave complets syncing of a project it informs the master that it
    has completed syncing.
    """
    details = request.json
    slave_id = details['slave_id']
    project = details['project']
    slave_node = SlaveNode.query.filter_by(id=slave_id).first()

    #XXX: Register that the slave has synced xyz project to the db.
    print('SlaveNode *%s* synced *%s*'%(slave_node.hostname, project))

    return jsonify({'method': 'slave_rsync_complete', 'success': True})


if __name__ == "__main__":
    with app.app_context():
        db.create_all(app=app)
        # Create root user if it does not exist.
        root_user = User.query.filter_by(username=app.config['ROOT_USER']).first()
        if not root_user:
            root_user = User(app.config['ROOT_USER'], app.config['ROOT_PASS'], 'root')
            db.session.add(root_user)
            db.session.commit()

    # Apply configuration to scheduler instance.
    scheduler = BackgroundScheduler()
    scheduler.configure(jobstores=jobstores, executors=executors, job_defaults=job_defaults, timezone=utc)
    scheduler.start()

    app.debug = True
    app.run(port=app.config['MASTER_PORT'],)
