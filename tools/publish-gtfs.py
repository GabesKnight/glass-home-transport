#!/usr/bin/env python3
"""Build compact, route-scoped static GTFS data for GitHub Pages."""
import shutil, argparse, datetime as dt, hashlib, importlib.util, itertools, json, pathlib, sqlite3, tempfile, re
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('gtfs',ROOT/'mac-transport/server.py'); gtfs=importlib.util.module_from_spec(spec);spec.loader.exec_module(gtfs)
def dump(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(obj,separators=(',',':'),ensure_ascii=False),encoding='utf8')
def export(db,out):
    out.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(db); updated=c.execute('SELECT updated FROM meta').fetchone()[0]
    routes=[];stop_index={}; total=0
    for rid,num,name,mode in c.execute("SELECT id,number,name,mode FROM routes WHERE mode IN ('tram','train','bus') ORDER BY id"):
        slug=hashlib.sha256(rid.encode()).hexdigest()[:20]; stops={}; services=set(); patterns={}
        q=c.execute('''SELECT t.id,t.service,a.stop,a.arr,a.dep,a.pick,a.dropoff,s.name FROM trips t JOIN times a ON a.trip=t.id JOIN stops s ON s.id=a.stop WHERE t.route=? ORDER BY t.id,a.seq''',(rid,))
        for tid,group in itertools.groupby(q,lambda x:x[0]):
            rows=list(group); service=rows[0][1]; base=next((x[4] if x[4] is not None else x[3] for x in rows if x[4] is not None or x[3] is not None),None)
            if base is None:continue
            seq=[]; offsets=[]
            for _,_,sid,arr,dep,pick,drop,sname in rows:
                stops[sid]=sname;seq.append(sid);offsets.append([None if arr is None else arr-base,None if dep is None else dep-base,int(pick or 0),int(drop or 0)])
            k=json.dumps([seq,offsets],separators=(',',':')); patterns.setdefault(k,{}).setdefault(service,[]).append(base);services.add(service)
        if not patterns:continue
        cal={s:[[],[]] for s in services}
        for s in services:
            cal[s][0]=[list(x) for x in c.execute('SELECT start,end,days FROM calendar WHERE service=?',(s,))];cal[s][1]=[list(x) for x in c.execute('SELECT date,kind FROM exceptions WHERE service=?',(s,))]
        payload={'id':rid,'number':num,'name':name,'mode':mode,'stops':stops,'calendar':cal,'patterns':[[*json.loads(k),v] for k,v in patterns.items()]}
        p=out/'routes'/f'{slug}.json';dump(p,payload);total+=p.stat().st_size
        routes.append({'id':rid,'number':num,'name':name,'mode':mode,'file':f'routes/{slug}.json'})
        for sid,sname in stops.items():stop_index.setdefault(sid,{'id':sid,'name':sname,'routes':[]})['routes'].append(rid)
    if not routes:raise ValueError('No supported routes')
    dump(out/'catalog.json',{'version':1,'updated':updated,'routes':routes,'stops':list(stop_index.values())});total+= (out/'catalog.json').stat().st_size
    dump(out/'status.json',{'updated':updated,'databaseBytes':total,'refreshing':False,'routes':len(routes),'stops':len(stop_index)})
    (out/'.nojekyll').touch();(out/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Glass Home Transport</title><h1>Glass Home Transport</h1><p>Scheduled Victorian public transport data. <a href="https://opendata.transport.vic.gov.au/dataset/gtfs-schedule">Source: State of Victoria (Department of Transport and Planning), GTFS Schedule</a>. Modified into route-specific timetable files under <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>. Unofficial personal project. <a href="status.json">Dataset status</a></p>',encoding='utf8')
    hub_source=(ROOT/'hub/index.html').read_text(encoding='utf8')
    version=hashlib.sha256(hub_source.encode()).hexdigest()[:20]
    (out/'hub.html').write_text(hub_source.replace('__GLASS_BUILD_VERSION__',version),encoding='utf8')
    dump(out/'hub-version.json',{'version':version})
    for asset in ('remote.html','remote.webmanifest','remote-icon.svg','remote-sw.js'):
        shutil.copyfile(ROOT/'hub'/asset,out/asset)
    if total>900_000_000:raise ValueError(f'Pages data exceeds safe size budget: {total} bytes')
    print(f'Published {len(routes)} routes, {len(stop_index)} stops, {total:,} bytes',flush=True);c.close()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--zip');p.add_argument('--db');p.add_argument('--out',default='public');a=p.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        db=pathlib.Path(a.db) if a.db else pathlib.Path(tmp)/'gtfs.sqlite'
        if not a.db:
            z=pathlib.Path(a.zip) if a.zip else pathlib.Path(tmp)/'gtfs.zip'
            if not a.zip:gtfs.download(z)
            gtfs.import_zip(z,db)
        export(db,pathlib.Path(a.out))
