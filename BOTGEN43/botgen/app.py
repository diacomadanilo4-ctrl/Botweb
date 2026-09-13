import os, io, secrets, string, sqlite3, random
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, jsonify, abort
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'botgen.db')
app = Flask(__name__)
app.secret_key = os.environ.get('BOTGEN_SECRET', 'change-this-development-secret')

GAMES = {
    'roblox': {'name':'Roblox', 'abbr':'R'},
    'call-of-duty': {'name':'Call of Duty', 'abbr':'COD'},
    'mobile-legends': {'name':'Mobile Legends', 'abbr':'ML'},
    'valorant': {'name':'Valorant', 'abbr':'V'},
    'xbox': {'name':'Xbox', 'abbr':'X'},
    'steam': {'name':'Steam', 'abbr':'S'},
    'fortnite': {'name':'Fortnite', 'abbr':'F'},
    'crossfire': {'name':'CrossFire', 'abbr':'CF'},
}
DURATIONS = {'1_day': ('1 Day', 1), '3_days': ('3 Days', 3), '7_days': ('7 Days', 7), 'lifetime': ('Lifetime', None)}

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn

def init_db():
    conn = db()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS keys(id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, duration TEXT NOT NULL, created_at TEXT NOT NULL, redeemed_at TEXT, expires_at TEXT, redeemed_by INTEGER, status TEXT NOT NULL DEFAULT 'Active', FOREIGN KEY(redeemed_by) REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY, game TEXT NOT NULL, line TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, uploaded_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS generations(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, game TEXT NOT NULL, lines_count INTEGER NOT NULL, created_at TEXT NOT NULL, filename TEXT NOT NULL, content TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
    ''')
    owner = conn.execute("SELECT id FROM users WHERE username=?", ('Flux',)).fetchone()
    if not owner:
        pw = os.environ.get('BOTGEN_OWNER_PASSWORD', 'Fluxgen123!')
        conn.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)", ('Flux', generate_password_hash(pw), 'owner', datetime.now(timezone.utc).isoformat()))
    # Seed synthetic demo stock once. These are not real accounts or credentials.
    count = conn.execute('SELECT COUNT(*) c FROM stock').fetchone()['c']
    if count == 0:
        now = datetime.now(timezone.utc).isoformat()
        rows=[]
        for slug in GAMES:
            for i in range(1200):
                token = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(14))
                rows.append((slug, f"DEMO-{GAMES[slug]['abbr']}-{i+1:04d}-{token}", now))
        conn.executemany('INSERT INTO stock(game,line,uploaded_at) VALUES(?,?,?)', rows)
    conn.commit(); conn.close()

@app.before_request
def before(): init_db()

def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if 'user_id' not in session: return redirect(url_for('login', next=request.path))
        return view(*a, **kw)
    return wrapped

def owner_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if session.get('role') != 'owner': abort(403)
        return view(*a, **kw)
    return wrapped

def active_key_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if session.get('role') == 'owner': return view(*a, **kw)
        if not session.get('user_id') or not session.get('key_id'):
            flash('Redeem a valid key before using the generator.', 'error'); return redirect(url_for('home'))
        conn=db(); k=conn.execute('SELECT * FROM keys WHERE id=?', (session['key_id'],)).fetchone(); conn.close()
        if not k or k['status'] != 'Active' or (k['expires_at'] and datetime.fromisoformat(k['expires_at']) <= datetime.now(timezone.utc)):
            session.pop('key_id', None); flash('Your key has expired or is no longer active.', 'error'); return redirect(url_for('home'))
        return view(*a, **kw)
    return wrapped

def key_code():
    alphabet='ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return 'BG-' + '-'.join(''.join(secrets.choice(alphabet) for _ in range(6)) for _ in range(3))

def make_lines(game, n):
    conn=db(); rows=conn.execute('SELECT id,line FROM stock WHERE game=? AND used=0 ORDER BY RANDOM() LIMIT ?', (game,n)).fetchall()
    if len(rows) < n:
        conn.close(); return None, len(rows)
    ids=[r['id'] for r in rows]
    conn.executemany('UPDATE stock SET used=1 WHERE id=?', [(i,) for i in ids])
    conn.commit(); conn.close()
    return [r['line'] for r in rows], n

@app.context_processor
def ctx():
    enabled = False
    if session.get('user_id'):
        if session.get('role') == 'owner': enabled=True
        elif session.get('key_id'):
            conn=db(); k=conn.execute('SELECT * FROM keys WHERE id=?', (session['key_id'],)).fetchone(); conn.close()
            enabled=bool(k and k['status']=='Active' and (not k['expires_at'] or datetime.fromisoformat(k['expires_at']) > datetime.now(timezone.utc)))
    return {'games':GAMES,'durations':DURATIONS,'generator_enabled':enabled}

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        username=request.form.get('username','').strip(); password=request.form.get('password','')
        conn=db(); user=conn.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone(); conn.close()
        if user and check_password_hash(user['password_hash'],password):
            session.clear(); session['user_id']=user['id']; session['username']=user['username']; session['role']=user['role']
            return redirect(url_for('home'))
        flash('Invalid username or password.','error')
    return render_template('login.html')

@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('home'))

@app.route('/redeem', methods=['POST'])
@login_required
def redeem():
    code=request.form.get('key','').strip().upper()
    conn=db(); k=conn.execute('SELECT * FROM keys WHERE key=?',(code,)).fetchone()
    if not k or k['status']!='Active':
        conn.close(); flash('Invalid, used, or inactive key.','error'); return redirect(url_for('home'))
    if k['expires_at'] and datetime.fromisoformat(k['expires_at']) <= datetime.now(timezone.utc):
        conn.execute("UPDATE keys SET status='Expired' WHERE id=?",(k['id'],)); conn.commit(); conn.close(); flash('That key has expired.','error'); return redirect(url_for('home'))
    now=datetime.now(timezone.utc)
    if k['duration']=='lifetime': expires=None
    else:
        days=DURATIONS[k['duration']][1]; expires=(now+timedelta(days=days)).isoformat()
    conn.execute("UPDATE keys SET redeemed_at=?,expires_at=?,redeemed_by=? WHERE id=?",(now.isoformat(),expires,session['user_id'],k['id']))
    conn.commit(); conn.close(); session['key_id']=k['id']; flash('Key accepted! Generator unlocked.','success'); return redirect(url_for('generator'))

@app.route('/generator')
@login_required
@active_key_required
def generator():
    conn=db(); recent=conn.execute('SELECT * FROM generations WHERE user_id=? ORDER BY id DESC LIMIT 5',(session['user_id'],)).fetchall();
    stock={g:conn.execute('SELECT COUNT(*) c FROM stock WHERE game=? AND used=0',(g,)).fetchone()['c'] for g in GAMES}; conn.close()
    return render_template('generator.html', recent=recent, stock=stock)

@app.route('/generate', methods=['POST'])
@login_required
@active_key_required
def generate():
    game=request.form.get('game','')
    try: n=max(1,min(1000,int(request.form.get('lines','100'))))
    except ValueError: n=100
    if game not in GAMES: return jsonify(ok=False,error='Choose a supported game.'),400
    lines,available=make_lines(game,n)
    if lines is None: return jsonify(ok=False,error=f'Only {available} unused lines remain in this database.'),409
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S'); filename=f"BOTGEN_{game}_{n}_{stamp}.txt"
    content='\n'.join(lines)+'\n'
    conn=db(); conn.execute('INSERT INTO generations(user_id,game,lines_count,created_at,filename,content) VALUES(?,?,?,?,?,?)',(session['user_id'],game,n,datetime.now(timezone.utc).isoformat(),filename,content)); conn.commit(); conn.close()
    return jsonify(ok=True, filename=filename, content=content, count=n)

@app.route('/download/<int:generation_id>')
@login_required
def download(generation_id):
    conn=db(); g=conn.execute('SELECT * FROM generations WHERE id=? AND user_id=?',(generation_id,session['user_id'])).fetchone(); conn.close()
    if not g and session.get('role')=='owner':
        conn=db(); g=conn.execute('SELECT * FROM generations WHERE id=?',(generation_id,)).fetchone(); conn.close()
    if not g: abort(404)
    return send_file(io.BytesIO(g['content'].encode()), as_attachment=True, download_name=g['filename'], mimetype='text/plain')

@app.route('/owner')
@login_required
@owner_required
def owner():
    conn=db(); keys=conn.execute('SELECT k.*,u.username FROM keys k LEFT JOIN users u ON u.id=k.redeemed_by ORDER BY k.id DESC LIMIT 100').fetchall(); stock={g:conn.execute('SELECT COUNT(*) c FROM stock WHERE game=? AND used=0',(g,)).fetchone()['c'] for g in GAMES}; gens=conn.execute('SELECT g.*,u.username FROM generations g JOIN users u ON u.id=g.user_id ORDER BY g.id DESC LIMIT 20').fetchall(); conn.close()
    return render_template('owner.html', keys=keys, stock=stock, generations=gens)

@app.route('/owner/create-key', methods=['POST'])
@login_required
@owner_required
def create_key():
    duration=request.form.get('duration'); qty=max(1,min(50,int(request.form.get('quantity','1'))))
    if duration not in DURATIONS: flash('Invalid duration.','error'); return redirect(url_for('owner'))
    conn=db(); made=[]
    for _ in range(qty):
        code=key_code();
        while conn.execute('SELECT 1 FROM keys WHERE key=?',(code,)).fetchone(): code=key_code()
        conn.execute('INSERT INTO keys(key,duration,created_at) VALUES(?,?,?)',(code,duration,datetime.now(timezone.utc).isoformat())); made.append(code)
    conn.commit(); conn.close();
    flash('Created '+str(len(made))+' key(s): '+', '.join(made),'success'); return redirect(url_for('owner'))

@app.route('/owner/revoke/<int:key_id>', methods=['POST'])
@login_required
@owner_required
def revoke(key_id):
    conn=db(); conn.execute("UPDATE keys SET status='Revoked' WHERE id=?",(key_id,)); conn.commit(); conn.close(); return redirect(url_for('owner'))

@app.route('/owner/upload', methods=['POST'])
@login_required
@owner_required
def upload():
    game=request.form.get('game'); f=request.files.get('file')
    if game not in GAMES or not f: flash('Select a game and TXT file.','error'); return redirect(url_for('owner'))
    text=f.read().decode('utf-8','ignore'); lines=[x.strip() for x in text.splitlines() if x.strip()]
    conn=db(); now=datetime.now(timezone.utc).isoformat(); conn.executemany('INSERT INTO stock(game,line,uploaded_at) VALUES(?,?,?)',[(game,x,now) for x in lines]); conn.commit(); conn.close(); flash(f'Uploaded {len(lines)} lines to {GAMES[game]["name"]}.','success'); return redirect(url_for('owner'))

if __name__=='__main__':
    init_db(); app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5000')),debug=True)
