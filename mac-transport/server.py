#!/usr/bin/env python3
import re,argparse,csv,io,zipfile,sqlite3,os,json,datetime as dt,urllib.request,urllib.parse,tempfile,threading,time,hmac
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from zoneinfo import ZoneInfo
BASE=Path(__file__).resolve().parent; DB=BASE/'timetable.sqlite'; ZONE=ZoneInfo('Australia/Melbourne'); PORTAL='https://opendata.transport.vic.gov.au'; LOCK=threading.Lock()
def download(target):
    req=urllib.request.Request(PORTAL+'/api/3/action/package_show?id=gtfs-schedule',headers={'User-Agent':'GlassHome/4.1.5'})
    with urllib.request.urlopen(req,timeout=30) as r: resources=json.load(r)['result']['resources']
    urls=[x['url'] for x in resources if x.get('format','').lower()=='zip']
    if not urls: raise ValueError('No ZIP resource found; use --zip with official download')
    url=urllib.parse.urljoin(PORTAL,urls[0]); print('Downloading',url,flush=True)
    with urllib.request.urlopen(url,timeout=90) as r,open(target,'wb') as f:
        n=0
        while True:
            chunk=r.read(1024*1024)
            if not chunk: break
            f.write(chunk); n+=len(chunk)
            if n%(32*1024*1024)==0: print(n//1048576,'MB downloaded',flush=True)
def rows(z,name):
    try:
        with z.open(name+'.txt') as f: yield from csv.DictReader(io.TextIOWrapper(f,encoding='utf-8-sig',newline=''))
    except KeyError: return

def seconds(v):
    try: h,m,s=map(int,v.split(':'));return h*3600+m*60+s
    except (ValueError,AttributeError):return None

def mode(v):
    n=int(v);return 'tram' if n==0 or 900<=n<1000 else 'train' if n in (1,2) or 100<=n<200 or 400<=n<500 else 'bus' if n==3 or 200<=n<300 or 700<=n<800 else ''
def import_zip(file,db=DB):
    tmp=Path(str(db)+'.new');tmp.unlink(missing_ok=True)
    try:
        with sqlite3.connect(tmp) as c,zipfile.ZipFile(file) as outer:
            c.executescript('''PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; CREATE TABLE stops(id TEXT PRIMARY KEY,name TEXT,parent TEXT); CREATE TABLE routes(id TEXT PRIMARY KEY,number TEXT,name TEXT,mode TEXT); CREATE TABLE trips(id TEXT PRIMARY KEY,route TEXT,service TEXT); CREATE TABLE times(trip TEXT,stop TEXT,seq INTEGER,arr INTEGER,dep INTEGER,pick TEXT,dropoff TEXT); CREATE TABLE calendar(service TEXT,start TEXT,end TEXT,days TEXT); CREATE TABLE exceptions(service TEXT,date TEXT,kind TEXT); CREATE TABLE meta(updated TEXT);''')
            names=[n for n in outer.namelist() if n.lower().endswith('google_transit.zip')] or [None]
            for index,name in enumerate(names):
                prefix=str(index)+':'
                def key(v):return prefix+v
                with (zipfile.ZipFile(io.BytesIO(outer.read(name))) if name else zipfile.ZipFile(file)) as z:
                    print('Importing',name or 'GTFS',flush=True)
                    c.executemany('INSERT INTO stops VALUES(?,?,?)',((key(r['stop_id']),r['stop_name'],key(r.get('parent_station',''))) for r in rows(z,'stops')))
                    c.executemany('INSERT INTO routes VALUES(?,?,?,?)',((key(r['route_id']),r.get('route_short_name',''),r.get('route_long_name',''),mode(r['route_type'])) for r in rows(z,'routes')))
                    c.executemany('INSERT INTO trips VALUES(?,?,?)',((key(r['trip_id']),key(r['route_id']),key(r['service_id'])) for r in rows(z,'trips')))
                    c.executemany('INSERT INTO times VALUES(?,?,?,?,?,?,?)',((key(r['trip_id']),key(r['stop_id']),int(r['stop_sequence']),seconds(r.get('arrival_time')),seconds(r.get('departure_time')),r.get('pickup_type','0'),r.get('drop_off_type','0')) for r in rows(z,'stop_times')))
                    c.executemany('INSERT INTO calendar VALUES(?,?,?,?)',((key(r['service_id']),r['start_date'],r['end_date'],''.join(r.get(d,'0') for d in ('monday','tuesday','wednesday','thursday','friday','saturday','sunday'))) for r in rows(z,'calendar')))
                    c.executemany('INSERT INTO exceptions VALUES(?,?,?)',((key(r['service_id']),r['date'],r['exception_type']) for r in rows(z,'calendar_dates')))
            if not c.execute('SELECT 1 FROM times LIMIT 1').fetchone():raise ValueError('Empty timetable')
            c.executescript('CREATE INDEX time_stop ON times(stop); CREATE INDEX time_trip ON times(trip,seq); CREATE INDEX trip_route ON trips(route); CREATE INDEX stop_parent ON stops(parent); CREATE INDEX cal_service ON calendar(service); CREATE INDEX ex_service ON exceptions(service,date);')
            c.execute('INSERT INTO meta VALUES(?)',(dt.datetime.now(dt.timezone.utc).isoformat(),));c.commit()
        os.replace(tmp,db)
    finally:tmp.unlink(missing_ok=True)
def refresh(file=None):
    with LOCK:
        if file:import_zip(file);return
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'gtfs.zip';download(p);import_zip(p)
def connect():
    if not DB.exists():raise ValueError('Mac timetable is not imported yet')
    c=sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;return c

def normalize(v):
    words=re.findall(r'[a-z0-9]+',v.lower())
    aliases={'drive':'dr','road':'rd','street':'st','avenue':'ave','parade':'pde','station':'station'}
    return ' '.join(aliases.get(w,w) for w in words)
def search(c,q,mode='',number=''):
    c.create_function('norm',1,normalize)
    term='%'+normalize(q)+'%'
    if mode=='tram' and number=='86' and normalize(q)=='southern cross': term='%spencer st bourke st%'
    if mode=='tram' and number=='86' and normalize(q)=='clements dr': term='%clements dr%'
    sql='SELECT DISTINCT s.id,s.name FROM stops s WHERE norm(s.name) LIKE ?'
    args=[term]
    if mode or number:
        sql+=" AND EXISTS(SELECT 1 FROM times a JOIN trips t ON t.id=a.trip JOIN routes r ON r.id=t.route WHERE a.stop=s.id AND (?='' OR r.mode=?) AND (?='' OR r.number=?))"
        args.extend([mode,mode,number,number])
    return {'stops':[dict(x) for x in c.execute(sql+' ORDER BY s.name,s.id LIMIT 60',args)],'routes':[dict(x) for x in c.execute("SELECT DISTINCT number,name,id,CASE mode WHEN 'train' THEN 0 WHEN 'tram' THEN 1 ELSE 2 END AS mode FROM routes WHERE (number LIKE ? OR name LIKE ?) AND mode!='' LIMIT 60",('%'+q+'%','%'+q+'%'))]}
def stop_ids(c,b,key):
    name=b.get(key,'').strip(); ident=b.get(key+'Id','')
    if ident:
        if c.execute('SELECT 1 FROM stops WHERE id=? AND name=?',(ident,name)).fetchone():return [ident]
        # IDs are feed scoped; re-resolve names if a refresh changed the prefix.
    n=normalize(name)
    if b.get('mode')=='tram' and b.get('number')=='86':
        if n in ('clements dr','clements dr plenty rd 70','janefield dr','janefield dr plenty rd 70','bundoora') and not ident:
            return [x[0] for x in c.execute("SELECT id FROM stops WHERE name IN ('Clements Dr/Plenty Rd #70','Janefield Dr/Plenty Rd #70')")]
        if n=='southern cross': name='Spencer St/Bourke St #1'
    return [x['id'] for x in search(c,name,b.get('mode',''),b.get('number',''))['stops']]
def pairs(c,b,limit=20):
    o=stop_ids(c,b,'origin');d=stop_ids(c,b,'destination')
    if not o or not d:return []
    sql="""SELECT a.stop AS originId,z.stop AS destinationId,o.name AS origin,s.name AS destination,COUNT(DISTINCT t.id) AS trips FROM times a JOIN trips t ON t.id=a.trip JOIN routes r ON r.id=t.route JOIN times z ON z.trip=a.trip AND z.seq>a.seq JOIN stops o ON o.id=a.stop JOIN stops s ON s.id=z.stop WHERE a.stop IN (%s) AND z.stop IN (%s) AND r.mode=? AND (?='' OR r.number=? OR r.name=?) AND a.pick!='1' AND z.dropoff!='1' AND a.dep IS NOT NULL AND z.arr>=a.dep GROUP BY a.stop,z.stop ORDER BY trips DESC LIMIT ?""" % (','.join('?' for _ in o),','.join('?' for _ in d))
    n=b.get('number','');return [dict(x) for x in c.execute(sql,(*o,*d,b.get('mode',''),n,n,n,limit))]
def reverse_board(c,b):
    # General return candidates share a name or a nonempty parent station; route 86 aliases cover the asymmetric stop 70.
    rev=dict(b,origin=b.get('destination',''),destination=b.get('origin',''),originId='',destinationId='',originLabel=b.get('destinationLabel',''),destinationLabel=b.get('originLabel',''))
    if b.get('mode')=='tram' and b.get('number')=='86' and normalize(rev['destination']) in ('janefield dr plenty rd 70','janefield dr'):rev['destination']='Clements Drive'
    options=pairs(c,rev)
    if not options:raise ValueError('No return journey found; select the opposite-direction stops manually')
    return options
def board(c,b,now=None):
    now=now or dt.datetime.now(ZONE); origin=b.get('origin','').strip();dest=b.get('destination','').strip();number=b.get('number','').strip();m=b.get('mode','')
    if not origin or not dest:raise ValueError('Choose both stops')
    sql='''SELECT a.trip,a.seq AS startseq,z.seq AS endseq,a.dep,z.arr,t.service,r.number,r.name,s.name AS destination FROM stops o JOIN times a ON a.stop=o.id JOIN trips t ON t.id=a.trip JOIN routes r ON r.id=t.route JOIN times z ON z.trip=a.trip AND z.seq>a.seq JOIN stops s ON s.id=z.stop WHERE o.name LIKE ? AND s.name LIKE ? AND r.mode=? AND (?='' OR r.number=? OR r.name=?) AND a.pick!='1' AND z.dropoff!='1' AND a.dep IS NOT NULL AND z.arr>=a.dep'''
    o=stop_ids(c,b,'origin');d=stop_ids(c,b,'destination')
    if not o or not d:raise ValueError('Stop not found; search for an official stop on this route')
    sql=sql.replace('o.name LIKE ?', 'o.id IN ('+','.join('?' for _ in o)+')').replace('s.name LIKE ?', 's.id IN ('+','.join('?' for _ in d)+')')
    candidates=c.execute(sql,(*o,*d,m,number,number,number)).fetchall()
    if not candidates:raise ValueError('No direct service connects these stops on the selected route')
    output=[];seen=set()
    for offset in (-1,0,1,2):
        day=now.date()+dt.timedelta(days=offset);ds=day.strftime('%Y%m%d');active={x[0] for x in c.execute('SELECT service FROM calendar WHERE start<=? AND end>=? AND substr(days,?,1)=\'1\'',(ds,ds,day.weekday()+1))}
        for x in c.execute('SELECT service,kind FROM exceptions WHERE date=?',(ds,)):
            if x[1]=='1':active.add(x[0])
            else:active.discard(x[0])
        base=dt.datetime.combine(day,dt.time(12),ZONE).timestamp()-43200
        for x in candidates:
            dep=base+x['dep'];arr=base+x['arr'];k=(x['trip'],dep)
            if x['service'] not in active or dep<now.timestamp()-60 or k in seen:continue
            seen.add(k);output.append((dep,arr,base,x))
    def iso(v):return dt.datetime.fromtimestamp(v,dt.timezone.utc).isoformat()
    result=[]
    for dep,arr,base,x in sorted(output,key=lambda x:x[0])[:5]:
        timeline=[{'name':s['name'],'time':iso(base+s['arr']) if s['arr'] is not None else None} for s in c.execute('SELECT s.name,t.arr FROM times t JOIN stops s ON s.id=t.stop WHERE trip=? AND seq BETWEEN ? AND ? ORDER BY seq',(x['trip'],x['startseq'],x['endseq']))];result.append({'time':iso(dep),'arrival':iso(arr),'route':x['number'] or x['name'],'destination':x['destination'],'live':False,'status':'Scheduled','timeline':timeline})
    return {'rows':result,'alerts':[]}
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.token):self.send_error(403);return
        u=urllib.parse.urlparse(self.path);q=urllib.parse.parse_qs(u.query)
        try:
            with connect() as c:
                if u.path=='/search':r=search(c,q.get('q',[''])[0][:80],q.get('mode',[''])[0],q.get('number',[''])[0])
                elif u.path=='/validate':r={'pairs':pairs(c,json.loads(q['board'][0]))}
                elif u.path=='/reverse':r={'pairs':reverse_board(c,json.loads(q['board'][0]))}
                elif u.path=='/board':r=board(c,json.loads(q['board'][0]))
                elif u.path=='/status':r={'updated':c.execute('SELECT updated FROM meta').fetchone()[0],'databaseBytes':DB.stat().st_size,'refreshing':LOCK.locked()}
                else:raise ValueError('Unknown endpoint')
            status=200
        except ValueError as e:r={'error':str(e)};status=422
        except Exception as e:print('Request failed:',str(e),flush=True);r={'error':str(e)};status=503
        data=json.dumps(r).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
def loop():
    while True:
        try:
            if not DB.exists() or time.time()-DB.stat().st_mtime>86400:refresh()
        except Exception as e:print('Refresh failed; retaining previous database:',e,flush=True)
        time.sleep(3600)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--zip');p.add_argument('--refresh',action='store_true');p.add_argument('--serve',action='store_true');a=p.parse_args()
    if a.zip or a.refresh:refresh(a.zip)
    if a.serve:
        cfg=json.loads((BASE/'connection.json').read_text());s=ThreadingHTTPServer(('0.0.0.0',8788),Handler);s.token=cfg['token'];threading.Thread(target=loop,daemon=True).start();s.serve_forever()
