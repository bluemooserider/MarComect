import os, functools, time, sqlite3
from datetime import date, datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash, send_file, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# --- CONFIGURATION ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, 'strategy.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'marcomect_prod_secret_2026')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

db = SQLAlchemy(app)

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

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    first_name = db.Column(db.String(50)); last_name = db.Column(db.String(50))
    email = db.Column(db.String(120), unique=True); role = db.Column(db.String(20), default='user')
    groups = db.relationship('Group', secondary=user_group_assoc, backref=db.backref('users', lazy='dynamic'))
    @property
    def display_name(self): return f"{self.first_name} {self.last_name}" if self.first_name else self.username

class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True); name = db.Column(db.String(50), unique=True, nullable=False)

class MasterCampaign(db.Model):
    id = db.Column(db.Integer, primary_key=True); name = db.Column(db.String(100), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id')); is_completed = db.Column(db.Boolean, default=False)
    owner = db.relationship('User', backref='owned_campaigns')
    sprints = db.relationship('Sprint', backref='campaign', cascade="all, delete-orphan")
    def get_progress(self):
        tasks = [t for s in self.sprints for t in s.tasks]
        return int((sum(1 for t in tasks if t.is_completed) / len(tasks)) * 100) if tasks else 0

class Sprint(db.Model):
    id = db.Column(db.Integer, primary_key=True); name = db.Column(db.String(100), nullable=False)
    campaign_id = db.Column(db.Integer, db.ForeignKey('master_campaign.id'), nullable=False)
    tasks = db.relationship('Task', backref='sprint', cascade="all, delete-orphan")
    def get_progress(self):
        return int((sum(1 for t in self.tasks if t.is_completed) / len(self.tasks)) * 100) if self.tasks else 0

class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True); sprint_id = db.Column(db.Integer, db.ForeignKey('sprint.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False); start_date = db.Column(db.Date, nullable=False); duration_days = db.Column(db.Integer, default=1)
    is_completed = db.Column(db.Boolean, default=False); comments = db.Column(db.Text)
    groups = db.relationship('Group', secondary=task_group_assoc)
    assignees = db.relationship('User', secondary=task_user_assoc, backref='assigned_tasks')
    links = db.relationship('TaskLink', backref='task', cascade="all, delete-orphan")
    files = db.relationship('TaskFile', backref='task', cascade="all, delete-orphan")

class TaskLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    url = db.Column(db.String(500), nullable=False)

class TaskFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    filename = db.Column(db.String(255))
    filepath = db.Column(db.String(500))

# --- AUTO-INITIALIZE FOR RENDER ---
with app.app_context():
    if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)
    db.create_all()
    if not User.query.filter_by(username='admin').first():
        db.session.add(User(username='admin', password_hash=generate_password_hash('admin'), role='admin'))
        db.session.commit()

# --- CONTEXT PROCESSOR ---
@app.context_processor
def inject_notifications():
    if 'user_id' in session:
        user = db.session.get(User, session['user_id'])
        if user and hasattr(user, 'assigned_tasks'):
            pending = [t for t in user.assigned_tasks if not t.is_completed]
            return dict(notification_count=len(pending), my_pending_tasks=pending)
    return dict(notification_count=0, my_pending_tasks=[])

# --- ROUTES ---
@app.route('/')
def index():
    if 'user_id' not in session: return redirect(url_for('login'))
    return render_template('index.html', campaigns=MasterCampaign.query.all(), users=User.query.all())

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = User.query.filter_by(username=request.form['username']).first()
        if u and check_password_hash(u.password_hash, request.form['password']):
            session.clear(); session.update({'user_id': u.id, 'username': u.username, 'role': u.role})
            return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

# --- API ENDPOINTS ---
@app.route('/api/campaign/save', methods=['POST'])
def save_campaign():
    d = request.json; db.session.add(MasterCampaign(name=d['name'], owner_id=int(d['owner_id']))); db.session.commit(); return jsonify({'success': True})

@app.route('/api/task/<int:task_id>')
def get_task(task_id):
    t = db.session.get(Task, task_id)
    return jsonify({
        'id': t.id, 'name': t.name, 'start_date': t.start_date.isoformat(), 'duration_days': t.duration_days, 'comments': t.comments or '',
        'group_ids': [g.id for g in t.groups], 'user_ids': [u.id for u in t.assignees],
        'links': [{'id': l.id, 'url': l.url} for l in t.links], 'files': [{'id': f.id, 'filename': f.filename} for f in t.files]
    })

@app.route('/api/task/save', methods=['POST'])
def save_task():
    d = request.json; t = db.session.get(Task, d['id']) if d.get('id') else Task(sprint_id=d['sprint_id'])
    t.name = d['name']; t.start_date = datetime.strptime(d['start_date'], '%Y-%m-%d').date(); t.duration_days = int(d['duration_days']); t.comments = d.get('comments', '')
    t.groups = Group.query.filter(Group.id.in_([int(x) for x in d.get('group_ids', [])])).all()
    t.assignees = User.query.filter(User.id.in_([int(x) for x in d.get('user_ids', [])])).all()
    if not d.get('id'): db.session.add(t)
    db.session.commit(); return jsonify({'success': True})

@app.route('/api/task/complete', methods=['POST'])
def toggle_task():
    t = db.session.get(Task, request.json['id']); t.is_completed = not t.is_completed; db.session.commit(); return jsonify({'success': True})

@app.route('/api/task/link/add', methods=['POST'])
def add_link():
    d = request.json; db.session.add(TaskLink(task_id=d['task_id'], url=d['url'])); db.session.commit(); return jsonify({'success': True})

@app.route('/api/task/link/delete', methods=['POST'])
def del_link():
    l = db.session.get(TaskLink, request.json['id']); db.session.delete(l); db.session.commit(); return jsonify({'success': True})

@app.route('/api/task/file/upload', methods=['POST'])
def upload_file():
    f = request.files['file']; tid = request.form['task_id']; fn = secure_filename(f.filename); fp = f"{int(time.time())}_{fn}"
    f.save(os.path.join(UPLOAD_FOLDER, fp)); db.session.add(TaskFile(task_id=tid, filename=fn, filepath=fp)); db.session.commit(); return jsonify({'success': True})

@app.route('/task/file/download/<int:file_id>')
def download_file(file_id):
    tf = db.session.get(TaskFile, file_id); return send_from_directory(UPLOAD_FOLDER, tf.filepath, as_attachment=True, download_name=tf.filename)

@app.route('/api/task/file/delete', methods=['POST'])
def del_file():
    tf = db.session.get(TaskFile, request.json['id']); db.session.delete(tf); db.session.commit(); return jsonify({'success': True})

@app.route('/api/gantt_data/<source_type>/<int:source_id>')
def api_gantt_data(source_type, source_id):
    rows = []
    if source_type == "all":
        for c in MasterCampaign.query.all():
            all_t = [t for s in c.sprints for t in s.tasks]
            if all_t:
                s, e = min(t.start_date for t in all_t), max(t.start_date + timedelta(days=t.duration_days) for t in all_t)
                rows.append([f"CAMP_{c.id}", c.name, "Campaign", s.isoformat(), e.isoformat(), None, c.get_progress(), None])
    elif source_type == "campaign":
        c = db.session.get(MasterCampaign, source_id)
        for s in (c.sprints if c else []):
            if s.tasks:
                st, en = min(t.start_date for t in s.tasks), max(t.start_date + timedelta(days=t.duration_days) for t in s.tasks)
                rows.append([f"SPRINT_{s.id}", s.name, "Sprint", st.isoformat(), en.isoformat(), None, s.get_progress(), None])
    elif source_type == "sprint":
        s = db.session.get(Sprint, source_id)
        for t in (s.tasks if s else []):
            rows.append([f"TASK_{t.id}", t.name, "Action", t.start_date.isoformat(), (t.start_date + timedelta(days=t.duration_days)).isoformat(), None, 100 if t.is_completed else 0, None])
    return jsonify({'tasks': rows})

@app.route('/api/sprints/<int:campaign_id>')
def get_sprints(campaign_id):
    c = db.session.get(MasterCampaign, campaign_id); return jsonify([{'id': s.id, 'name': s.name, 'task_count': len(s.tasks), 'progress': s.get_progress()} for s in c.sprints])

@app.route('/api/sprint/save', methods=['POST'])
def save_sprint():
    d = request.json; db.session.add(Sprint(campaign_id=d['campaign_id'], name=d['name'])); db.session.commit(); return jsonify({'success': True})

@app.route('/api/tasks/<int:sprint_id>')
def get_tasks(sprint_id):
    s = db.session.get(Sprint, sprint_id); return jsonify([{'id': t.id, 'name': t.name, 'is_completed': t.is_completed, 'assignee_names': ", ".join([u.display_name for u in t.assignees]) or 'Unassigned'} for t in s.tasks])

@app.route('/api/progress/campaigns')
def get_all_campaign_progress(): return jsonify({c.id: c.get_progress() for c in MasterCampaign.query.all()})

@app.route('/api/users')
def api_get_users(): return jsonify([{'id': u.id, 'display_name': u.display_name, 'email': u.email or u.username, 'group_ids': [g.id for g in u.groups]} for u in User.query.all()])

@app.route('/api/groups')
def api_get_groups(): return jsonify([{'id': g.id, 'name': g.name} for g in Group.query.all()])

@app.route('/admin/backup')
def admin_backup_download(): return send_file(DB_PATH, as_attachment=True, download_name="strategy_backup.db")

if __name__ == '__main__':
    app.run(debug=True)