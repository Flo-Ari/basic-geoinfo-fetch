"""An offset-only SQLite R-tree for the original line-oriented GeoJSON caches."""
import json
import math
from pathlib import Path
import sqlite3
from contextlib import closing
from .progress import report, track

def bounds(geometry):
    points=[]
    stack=[geometry.get('coordinates',[])]
    while stack:
        node=stack.pop()
        if not node:
            continue
        if isinstance(node[0],(int,float)):
            if len(node)>=2 and math.isfinite(node[0]) and math.isfinite(node[1]):
                points.append((node[0],node[1]))
        else:
            stack.extend(node)
    if not points:
        return None
    xs,ys=zip(*points)
    return min(xs),min(ys),max(xs),max(ys)

def signature(path):
    stat=path.stat()
    return json.dumps([stat.st_size,stat.st_mtime_ns])

def is_current(index,path):
    if not index.exists():
        return False
    try:
        with closing(sqlite3.connect(index)) as db:
            row=db.execute('SELECT signature FROM metadata').fetchone()
            return row and row[0]==signature(path)
    except sqlite3.DatabaseError:
        return False

def build_index(path,index):
    partial=index.with_name(index.name+'.part')
    partial.unlink(missing_ok=True)
    fingerprint=signature(path)
    print('  首次建立空间索引；之后换范围可直接定位候选要素。',flush=True)
    db=sqlite3.connect(partial)
    try:
        db.executescript('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; CREATE TABLE metadata(signature TEXT); CREATE TABLE offsets(id INTEGER PRIMARY KEY, position INTEGER, length INTEGER); CREATE VIRTUAL TABLE spatial USING rtree(id,minx,maxx,miny,maxy);')
        rows=[]
        boxes=[]
        count=0
        with path.open('rb') as stream:
            report('建立空间索引', 0, path.stat().st_size, '字节', force=True)
            while True:
                offset=stream.tell()
                line=stream.readline()
                if not line:
                    break
                text=line.strip().removesuffix(b',')
                if not text.startswith(b'{') or text.endswith(b'['):
                    continue
                try:
                    feature=json.loads(text)
                except ValueError:
                    continue
                if feature.get('type')!='Feature' or not feature.get('geometry'):
                    continue
                bb=bounds(feature['geometry'])
                if not bb:
                    continue
                count+=1
                rows.append((count,offset,len(line)))
                boxes.append((count,bb[0],bb[2],bb[1],bb[3]))
                if len(rows)==20000:
                    db.executemany('INSERT INTO offsets VALUES(?,?,?)',rows)
                    db.executemany('INSERT INTO spatial VALUES(?,?,?,?,?)',boxes)
                    db.commit()
                    report('建立空间索引', stream.tell(), path.stat().st_size, '字节')
                    rows.clear();boxes.clear()
                if count%1000000==0:
                    print(f'  已索引 {count:,} 条要素...',flush=True)
        if signature(path)!=fingerprint:
            raise ValueError('全国数据在索引期间改变，请重新运行')
        db.executemany('INSERT INTO offsets VALUES(?,?,?)',rows)
        db.executemany('INSERT INTO spatial VALUES(?,?,?,?,?)',boxes)
        db.execute('INSERT INTO metadata VALUES(?)',(fingerprint,))
        db.commit()
    finally:
        db.close()
    partial.replace(index)
    report('建立空间索引', path.stat().st_size, path.stat().st_size, '字节', force=True)
    print(f'  空间索引已就绪：{count:,} 条要素',flush=True)

def indexed_features(geojson_path,bbox):
    path=Path(geojson_path)
    index=path.with_suffix(path.suffix+'.spatial.sqlite')
    if not is_current(index,path):
        build_index(path,index)
    else:
        print('  [空间索引命中] 直接读取目标区域候选要素',flush=True)
    west,south,east,north=bbox
    with closing(sqlite3.connect(index)) as db,path.open('rb') as stream:
        total = db.execute('SELECT COUNT(*) FROM spatial WHERE maxx>=? AND minx<=? AND maxy>=? AND miny<=?',(west,east,south,north)).fetchone()[0]
        cursor=db.execute('SELECT o.position,o.length FROM spatial s JOIN offsets o ON o.id=s.id WHERE s.maxx>=? AND s.minx<=? AND s.maxy>=? AND s.miny<=? ORDER BY o.id',(west,east,south,north))
        for position,length in track(cursor, total):
            stream.seek(position)
            yield json.loads(stream.read(length).strip().removesuffix(b','))
