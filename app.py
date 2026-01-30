import os, functools, time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, send_file, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# --- PERSISTENT STORAGE CONFIG ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.environ.get('RENDER_DISK_PATH', os.path.join(BASE_DIR, 'data_storage'))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'strategy.db')
UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'marcomect_stable_2026')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
db = SQLAlchemy(app)


# --- BUSINESS LOGIC HELPERS ---
def add_business_days(from_date, duration_days):
    current_date = from_date
    days_added = 0
    while days_added < duration_days:
        current_date += timedelta(days=1)
        if current_date.weekday() < 5:
            days_added += 1
    return current_date


def next_business_day(from_date):
    current_date = from_date
    while current_date.weekday() >= 5:
        current_date += timedelta(days=1)
    return current_date


# --- MODELS ---
task_group_assoc = db.Table('task_group_assoc',
                            db.Column('task_id', db.Integer, db.ForeignKey('task.id'), primary_key=True),
                            db.Column('group_id', db.Integer, db.ForeignKey('group.id'), primary_key=True))
task_user_assoc = db.Table('task_user_assoc',
                           db.Column('task_id', db.Integer, db.ForeignKey('task.id'), primary_key=True),
                           db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True))
user_group_assoc = db.Table('user_group_assoc',
                            db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
                            db.Column('group_id', db.Integer, db.ForeignKey('group.id'), primary_key=True))
task_dependencies = db.Table('task_dependencies',
                             db.Column('follower_id', db.Integer, db.ForeignKey('task.id'), primary_key=True),
                             db.Column('predecessor_id', db.Integer, db.ForeignKey('task.id'), primary_key=True))


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    first_name = db.Column(db.String(50));
    last_name = db.Column(db.String(50))
    email = db.Column(db.String(120), unique=True);
    role = db.Column(db.String(20), default='user')
    groups = db.relationship('Group', secondary=user_group_assoc, backref=db.backref('users', lazy='dynamic'))

    @property
    def display_name(self): return f"{self.first_name} {self.last_name}" if (
                self.first_name and self.last_name) else self.username


class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True);
    name = db.Column(db.String(50), unique=True, nullable=False)


class MasterCampaign(db.Model):
    id = db.Column(db.Integer, primary_key=True);
    name = db.Column(db.String(100), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    sprints = db.relationship('Sprint', backref='campaign', cascade="all, delete-orphan")

    def get_progress(self):
        tasks = [t for s in self.sprints for t in s.tasks]
        return int((sum(1 for t in tasks if t.is_completed) / len(tasks)) * 100) if tasks else 0


class Sprint(db.Model):
    id = db.Column(db.Integer, primary_key=True);
    name = db.Column(db.String(100), nullable=False)
    campaign_id = db.Column(db.Integer, db.ForeignKey('master_campaign.id'), nullable=False)
    tasks = db.relationship('Task', backref='sprint', cascade="all, delete-orphan")

    def get_progress(self):
        return int((sum(1 for t in self.tasks if t.is_completed) / len(self.tasks)) * 100) if self.tasks else 0

    def get_date_range(self):
        if not self.tasks: return "No Dates Set"
        start_dates = [t.start_date for t in self.tasks if t.start_date]
        end_dates = [t.end_date for t in self.tasks if t.start_date]
        if not start_dates: return "No Dates Set"
        return f"{min(start_dates).strftime('%b %d')} - {max(end_dates).strftime('%b %d')}"


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True);
    sprint_id = db.Column(db.Integer, db.ForeignKey('sprint.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    start_date = db.Column(db.Date, nullable=True)
    duration_days = db.Column(db.Integer, default=1)
    is_completed = db.Column(db.Boolean, default=False);
    comments = db.Column(db.Text)
    groups = db.relationship('Group', secondary=task_group_assoc)
    assignees = db.relationship('User', secondary=task_user_assoc, backref='assigned_tasks')
    links = db.relationship('TaskLink', backref='task', cascade="all, delete-orphan")
    files = db.relationship('TaskFile', backref='task', cascade="all, delete-orphan")
    predecessors = db.relationship('Task', secondary=task_dependencies,
                                   primaryjoin=(id == task_dependencies.c.follower_id),
                                   secondaryjoin=(id == task_dependencies.c.predecessor_id),
                                   backref=db.backref('followers', lazy='dynamic'), lazy='dynamic')

    @property
    def end_date(self):
        if not self.start_date: return None
        return add_business_days(self.start_date, self.duration_days)


class TaskLink(db.Model):
    id = db.Column(db.Integer, primary_key=True);
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False);
    url = db.Column(db.String(500), nullable=False)


class TaskFile(db.Model):
    id = db.Column(db.Integer, primary_key=True);
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False);
    filename = db.Column(db.String(255));
    filepath = db.Column(db.String(500))


# --- BOOTSTRAP ---
with app.app_context():
    db.create_all()
    if not User.query.filter_by(username='admin').first():
        db.session.add(User(username='admin', password_hash=generate_password_hash('admin'), role='admin'))
        db.session.commit()


@app.context_processor
def inject_globals():
    if 'user_id' in session:
        u = db.session.get(User, session['user_id'])
        if u and hasattr(u, 'assigned_tasks'):
            pending = [t for t in u.assigned_tasks if not t.is_completed]
            return dict(notification_count=len(pending), my_pending_tasks=pending)
    return dict(notification_count=0, my_pending_tasks=[])


def admin_only(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if session.get('role') != 'admin': return jsonify({'success': False}), 403
        return f(*args, **kwargs)

    return wrapper


# --- CORE ROUTES ---
@app.route('/')
def home():
    if 'user_id' not in session: return redirect(url_for('login'))
    return render_template('index.html', campaigns=MasterCampaign.query.order_by(MasterCampaign.id.desc()).all(),
                           users=User.query.all())


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = User.query.filter_by(username=request.form['username']).first()
        if u and check_password_hash(u.password_hash, request.form['password']):
            session.update({'user_id': u.id, 'username': u.username, 'role': u.role});
            return redirect(url_for('home'))
    return render_template('login.html')


@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))


# --- API ---
@app.route('/api/campaign/save', methods=['POST'])
def save_camp():
    d = request.json
    if d.get('id'):
        c = db.session.get(MasterCampaign, d['id'])
        if c:
            c.name = d['name']
            c.owner_id = int(d['owner_id'])
    else:
        db.session.add(MasterCampaign(name=d['name'], owner_id=int(d['owner_id'])))
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/campaign/delete', methods=['POST'])
@admin_only
def delete_camp():
    c = db.session.get(MasterCampaign, request.json['id'])
    if c: db.session.delete(c); db.session.commit(); return jsonify({'success': True})
    return jsonify({'success': False}), 404


@app.route('/api/campaign/clone', methods=['POST'])
def clone_camp():
    if 'user_id' not in session: return jsonify({'success': False, 'message': 'Auth error'}), 403
    old_id = request.json['id']
    old_c = db.session.get(MasterCampaign, old_id)
    if not old_c: return jsonify({'success': False}), 404

    try:
        new_c = MasterCampaign(name=f"Copy of {old_c.name}", owner_id=session['user_id'])
        db.session.add(new_c)
        db.session.flush()

        for old_s in old_c.sprints:
            new_s = Sprint(name=old_s.name, campaign_id=new_c.id)
            db.session.add(new_s)
            db.session.flush()

            for old_t in old_s.tasks:
                new_t = Task(
                    sprint_id=new_s.id,
                    name=old_t.name,
                    start_date=None,
                    duration_days=old_t.duration_days,
                    comments=old_t.comments,
                    is_completed=False
                )
                db.session.add(new_t)
                for link in old_t.links:
                    db.session.add(TaskLink(task=new_t, url=link.url))
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/api/sprints/<int:cid>')
def fetch_sprints(cid):
    c = db.session.get(MasterCampaign, cid)
    data = []
    if c:
        for s in c.sprints:
            has_unassigned = any(len(t.assignees) == 0 for t in s.tasks)
            data.append({
                'id': s.id,
                'name': s.name,
                'progress': s.get_progress(),
                'date_range': s.get_date_range(),
                'has_unassigned': has_unassigned
            })
    return jsonify(data)


@app.route('/api/sprint/save', methods=['POST'])
def save_spr(): d = request.json; db.session.add(
    Sprint(campaign_id=d['campaign_id'], name=d['name'])); db.session.commit(); return jsonify({'success': True})


@app.route('/api/tasks/<int:sid>')
def fetch_tasks(sid):
    s = db.session.get(Sprint, sid)
    return jsonify([{
        'id': t.id,
        'name': t.name,
        'is_completed': t.is_completed,
        'assignee_names': ", ".join([u.display_name for u in t.assignees]) or 'Unassigned',
        'is_assigned': len(t.assignees) > 0
    } for t in (s.tasks if s else [])])


@app.route('/api/task/<int:tid>')
def fetch_single_task(tid):
    t = db.session.get(Task, tid)
    return jsonify({
        'id': t.id, 'name': t.name,
        'start_date': t.start_date.isoformat() if t.start_date else '',
        'duration_days': t.duration_days, 'comments': t.comments or '',
        'group_ids': [g.id for g in t.groups], 'user_ids': [u.id for u in t.assignees],
        'predecessor_ids': [p.id for p in t.predecessors],
        'links': [{'id': l.id, 'url': l.url} for l in t.links],
        'files': [{'id': f.id, 'filename': f.filename} for f in t.files]
    })


@app.route('/api/task/delete', methods=['POST'])
def delete_task():
    t = db.session.get(Task, request.json['id'])
    if t:
        db.session.delete(t)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': False}), 404


@app.route('/api/task/save', methods=['POST'])
def save_tk():
    d = request.json
    t = db.session.get(Task, d['id']) if d.get('id') else Task(sprint_id=d['sprint_id'])
    t.name = d['name'];
    t.duration_days = int(d['duration_days']);
    t.comments = d.get('comments', '')

    if 'predecessor_ids' in d:
        pred_ids = [int(x) for x in d['predecessor_ids']]
        if t.id and t.id in pred_ids: pred_ids.remove(t.id)
        preds = Task.query.filter(Task.id.in_(pred_ids)).all()
        t.predecessors = preds
        if len(preds) > 0:
            valid_ends = [p.end_date for p in preds if p.end_date]
            if valid_ends: t.start_date = next_business_day(max(valid_ends))
        else:
            try:
                t.start_date = datetime.strptime(d['start_date'], '%Y-%m-%d').date()
            except:
                t.start_date = None
    else:
        try:
            t.start_date = datetime.strptime(d['start_date'], '%Y-%m-%d').date()
        except:
            t.start_date = None

    t.groups = Group.query.filter(Group.id.in_([int(x) for x in d.get('group_ids', [])])).all()
    t.assignees = User.query.filter(User.id.in_([int(x) for x in d.get('user_ids', [])])).all()
    if not d.get('id'): db.session.add(t)
    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/task/complete', methods=['POST'])
def toggle_tk():
    t = db.session.get(Task, request.json['id']);
    t.is_completed = not t.is_completed;
    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/user/change_password', methods=['POST'])
def change_password():
    d = request.json;
    u = db.session.get(User, session['user_id'])
    if not check_password_hash(u.password_hash, d['current']): return jsonify(
        {'success': False, 'message': 'Wrong password'}), 401
    u.password_hash = generate_password_hash(d['new']);
    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/users')
def get_users(): return jsonify([{'id': u.id, 'display_name': u.display_name, 'email': u.email or u.username,
                                  'role': u.role, 'first_name': u.first_name, 'last_name': u.last_name,
                                  'group_ids': [g.id for g in u.groups]} for u in User.query.all()])


@app.route('/api/user/save', methods=['POST'])
@admin_only
def admin_save_user():
    d = request.json;
    u = db.session.get(User, d['id']) if d.get('id') else User()
    u.email = d['email'];
    u.username = d['email'];
    u.first_name = d.get('first_name');
    u.last_name = d.get('last_name');
    u.role = d.get('role', 'user')
    if d.get('password'): u.password_hash = generate_password_hash(d['password'])
    if 'group_ids' in d: u.groups = Group.query.filter(Group.id.in_([int(x) for x in d['group_ids']])).all()
    if not d.get('id'): db.session.add(u)
    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/groups')
def get_groups(): return jsonify([{'id': g.id, 'name': g.name} for g in Group.query.all()])


@app.route('/api/group/save', methods=['POST'])
@admin_only
def admin_save_group(): d = request.json; db.session.add(Group(name=d['name'])); db.session.commit(); return jsonify(
    {'success': True})


@app.route('/api/group/members/save', methods=['POST'])
@admin_only
def save_group_members():
    d = request.json;
    gid = int(d['group_id']);
    new_uids = set([int(x) for x in d['user_ids']])
    g = db.session.get(Group, gid)
    if not g: return jsonify({'success': False}), 404
    all_users = User.query.all()
    for u in all_users:
        is_in_group = g in u.groups;
        should_be_in = u.id in new_uids
        if should_be_in and not is_in_group:
            u.groups.append(g)
        elif not should_be_in and is_in_group:
            u.groups.remove(g)
    db.session.commit();
    return jsonify({'success': True})


@app.route('/admin/backup')
def admin_backup_download(): return send_file(DB_PATH, as_attachment=True, download_name="backup.db")


@app.route('/api/task/link/add', methods=['POST'])
def add_lk(): d = request.json; db.session.add(
    TaskLink(task_id=d['task_id'], url=d['url'])); db.session.commit(); return jsonify({'success': True})


@app.route('/api/task/file/upload', methods=['POST'])
def up_fl():
    f = request.files['file'];
    tid = request.form['task_id'];
    fn = secure_filename(f.filename);
    fp = f"{int(time.time())}_{fn}"
    f.save(os.path.join(UPLOAD_FOLDER, fp));
    db.session.add(TaskFile(task_id=tid, filename=fn, filepath=fp));
    db.session.commit();
    return jsonify({'success': True})


@app.route('/task/file/dl/<int:fid>')
def dl_fl(fid): tf = db.session.get(TaskFile, fid); return send_from_directory(UPLOAD_FOLDER, tf.filepath,
                                                                               as_attachment=True,
                                                                               download_name=tf.filename)


@app.route('/api/progress/camps')
def camp_prog(): return jsonify({c.id: c.get_progress() for c in MasterCampaign.query.all()})


# --- GANTT CHART DATA (FIXED AGGREGATION) ---
@app.route('/api/gantt_data/<string:dtype>/<int:oid>')
def get_gantt_data(dtype, oid):
    rows = []

    def fmt(d):
        return d.isoformat() if d else None

    if dtype == 'all':
        for c in MasterCampaign.query.all():
            # NEW: Aggregate actual dates
            all_starts = []
            all_ends = []
            for s in c.sprints:
                for t in s.tasks:
                    if t.start_date:
                        all_starts.append(t.start_date)
                        all_ends.append(t.end_date)

            if all_starts:
                start = min(all_starts)
                end = max(all_ends)
                rows.append([f'CAMP_{c.id}', c.name, 'Strategy', fmt(start), fmt(end), None, c.get_progress(), None])
            else:
                # Placeholder for empty campaigns
                s = datetime.today().date()
                rows.append([f'CAMP_{c.id}', c.name, 'Strategy', fmt(s), fmt(add_business_days(s, 1)), None, 0, None])

    elif dtype == 'campaign':
        c = db.session.get(MasterCampaign, oid)
        if c:
            for s in c.sprints:
                # NEW: Aggregate actual dates for phases
                s_starts = [t.start_date for t in s.tasks if t.start_date]
                s_ends = [t.end_date for t in s.tasks if t.start_date]

                if s_starts:
                    start = min(s_starts)
                    end = max(s_ends)
                    rows.append([f'SPRINT_{s.id}', s.name, 'Phase', fmt(start), fmt(end), None, s.get_progress(), None])
                else:
                    s = datetime.today().date()
                    rows.append(
                        [f'SPRINT_{s.id}', s.name, 'Phase', fmt(s), fmt(add_business_days(s, 1)), None, 0, None])

    elif dtype == 'sprint':
        s = db.session.get(Sprint, oid)
        if s:
            for t in s.tasks:
                if t.start_date:
                    dep_str = ",".join([f"TASK_{p.id}" for p in t.predecessors]) if t.predecessors.count() > 0 else None
                    rows.append([f'TASK_{t.id}', t.name, t.assignees[0].display_name if t.assignees else 'Unassigned',
                                 fmt(t.start_date), t.end_date.isoformat(), None, 100 if t.is_completed else 0,
                                 dep_str])

    return jsonify({'tasks': rows})


if __name__ == '__main__':
    app.run(debug=True)