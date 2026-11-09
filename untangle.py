#!/usr/bin/env python3
"""
Takeout Untangler: turns a Google Photos Takeout export into a clean,
dated Year/Month photo library.

  python untangle.py <takeout zip or folder> [more zips...] --out "D:\\Photos" [--dry-run]

What it fixes (the usual Takeout mess):
  * Dates: Google strips the taken date from many files and misfiles them.
    The JSON sidecar next to each photo has the real "photoTakenTime"; we
    put it back (EXIF for JPEGs, file modified time for everything).
  * Duplicates: every album is a second copy of the same photo. We keep one
    file per photo in Year/Month, and (optionally) copies in Albums/<name>.
  * Trash: goes to its own _Trash folder, never mixed into your library.
  * Sidecar name quirks: truncated names, "(1)" numbering, "-edited" files.

No upload, no internet, nothing leaves your computer. Reads zips directly,
no need to extract them first. Python 3.9+. Optional: pip install piexif
(for writing EXIF dates into JPEGs; without it only file dates are set).
"""
import argparse, csv, hashlib, io, json, os, re, sys, time, zipfile
from datetime import datetime, timezone

try:
    import piexif
except ImportError:
    piexif = None

MEDIA_EXT = ('jpg', 'jpeg', 'png', 'gif', 'webp', 'heic', 'heif', 'bmp', 'tif', 'tiff', 'dng', 'nef', 'cr2', 'arw',
             'mp4', 'mov', 'm4v', 'avi', 'mkv', '3gp', 'mpg', 'mpeg', 'wmv', 'mp', 'mts')
EXT_RE = re.compile(r'\.(' + '|'.join(MEDIA_EXT) + r')', re.I)
NUM_RE = re.compile(r'\((\d+)\)$')
FNAME_DATE_RES = [
    re.compile(r'(20\d{2}|19\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})'),
    re.compile(r'(20\d{2}|19\d{2})-(\d{2})-(\d{2})[ _T-]?(\d{2})[.:-]?(\d{2})[.:-]?(\d{2})?'),
    re.compile(r'(20\d{2}|19\d{2})(\d{2})(\d{2})'),
]


# ---------- sources: a zip or a folder, same interface ----------
class Entry:
    __slots__ = ('path', 'size', 'read', 'mtime')

    def __init__(self, path, size, read, mtime):
        self.path, self.size, self.read, self.mtime = path, size, read, mtime


def iter_source(src):
    if os.path.isdir(src):
        for root, _, files in os.walk(src):
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, src).replace('\\', '/')
                st = os.stat(p)
                yield Entry(rel, st.st_size, (lambda p=p: open(p, 'rb').read()), st.st_mtime)
    else:
        z = zipfile.ZipFile(src)
        for info in z.infolist():
            if info.is_dir():
                continue
            mt = time.mktime(info.date_time + (0, 0, -1))
            yield Entry(info.filename, info.file_size, (lambda n=info.filename, z=z: z.read(n)), mt)


# ---------- sidecar matching ----------
def sidecar_key(json_name):
    """'IMG_1234.JPG.supplemental-metadata(1).json' -> ('IMG_1234.JPG', 1)
       'IMG_1234.JPG(1).json' -> ('IMG_1234.JPG', 1)
       truncated 'longname.JPG.supplemental-meta.json' -> ('longname.JPG', 0)"""
    base = json_name[:-5] if json_name.lower().endswith('.json') else json_name
    num = 0
    m = NUM_RE.search(base)
    if m:
        num = int(m.group(1))
        base = base[:m.start()]
    em = EXT_RE.search(base)
    if em:
        key = base[:em.end()]
    else:
        key = base
    # drop a '.supplemental-metadata' tail (possibly truncated) that follows the extension
    tail = base[len(key):]
    if tail and not '.supplemental-metadata'.startswith(tail.lower()) and not tail.lower().startswith('.s'):
        key = base  # not a sidecar pattern we know; keep whole thing
    return key, num


def media_key(name):
    """'IMG_1234(1).JPG' -> ('IMG_1234.JPG', 1); 'IMG_1234-edited.JPG' -> ('IMG_1234.JPG', 0)"""
    stem, ext = os.path.splitext(name)
    num = 0
    m = NUM_RE.search(stem)
    if m:
        num = int(m.group(1))
        stem = stem[:m.start()]
    stem = re.sub(r'-(edited|bearbeitet|modifié|editado)$', '', stem, flags=re.I)
    return stem + ext, num


def find_sidecar(folder_jsons, name):
    """folder_jsons: dict (key, num) -> json entry, plus list for prefix matching."""
    mp = folder_jsons['map']
    # 1. the literal name (Samsung bursts really are called 'X(0).jpg')
    exact = mp.get((name, 0))
    if exact:
        return exact
    key, num = media_key(name)
    exact = mp.get((key, num))
    if exact:
        return exact
    if num:
        alt = mp.get((key, 0))
        if alt:
            return alt
    # 2. motion photos: the video half 'X.MP4' / 'X.MP' shares the photo's sidecar 'X.jpg.json' / 'X.MP.jpg.json'
    stem = os.path.splitext(name)[0]
    if 'stems' not in folder_jsons:
        folder_jsons['stems'] = {}
        for (k, n), e in mp.items():
            if n == 0:
                folder_jsons['stems'].setdefault(os.path.splitext(k)[0].lower(), e)
                folder_jsons['stems'].setdefault(k.lower(), e)
    hit = folder_jsons['stems'].get(stem.lower()) or folder_jsons['stems'].get(name.lower())
    if hit:
        return hit
    # truncated sidecar names: json key is a prefix of the media name
    low = key.lower()
    best = None
    for (k, n), e in folder_jsons['map'].items():
        if n == num and len(k) >= 8 and low.startswith(k.lower()) and (best is None or len(k) > len(best[0])):
            best = (k, e)
    return best[1] if best else None


# ---------- dates ----------
def date_from_sidecar(data):
    for k in ('photoTakenTime', 'creationTime'):
        ts = data.get(k, {}).get('timestamp')
        if ts and str(ts).isdigit() and int(ts) > 0:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(), 'sidecar:' + k
    return None, None


def date_from_exif(blob):
    if not piexif:
        return None
    try:
        ex = piexif.load(blob)
        for ifd, tag in (('Exif', piexif.ExifIFD.DateTimeOriginal), ('Exif', piexif.ExifIFD.DateTimeDigitized), ('0th', piexif.ImageIFD.DateTime)):
            v = ex.get(ifd, {}).get(tag)
            if v:
                s = v.decode('ascii', 'ignore').strip()
                if s and not s.startswith('0000'):
                    return datetime.strptime(s[:19], '%Y:%m:%d %H:%M:%S').astimezone()
    except Exception:
        pass
    return None


def date_from_name(name):
    for rx in FNAME_DATE_RES:
        m = rx.search(name)
        if m:
            g = [int(x) if x else 0 for x in m.groups()]
            while len(g) < 6:
                g.append(0)
            try:
                d = datetime(g[0], g[1], g[2], g[3], g[4], g[5])
                if 1990 <= d.year <= datetime.now().year + 1:
                    return d.astimezone()
            except ValueError:
                pass
    return None


def write_exif_date(blob, dt, geo=None):
    """Return a new JPEG blob with DateTimeOriginal set (only if missing)."""
    if not piexif:
        return blob, False
    try:
        ex = piexif.load(blob)
    except Exception:
        return blob, False
    s = dt.strftime('%Y:%m:%d %H:%M:%S').encode()
    changed = False
    exif_ifd = ex.setdefault('Exif', {})
    if not exif_ifd.get(piexif.ExifIFD.DateTimeOriginal):
        exif_ifd[piexif.ExifIFD.DateTimeOriginal] = s
        exif_ifd[piexif.ExifIFD.DateTimeDigitized] = s
        ex.setdefault('0th', {})[piexif.ImageIFD.DateTime] = s
        changed = True
    if geo and geo.get('latitude') and geo.get('longitude') and not ex.get('GPS'):
        def dms(x):
            x = abs(x); d = int(x); m = int((x - d) * 60); s_ = round(((x - d) * 60 - m) * 60 * 10000)
            return ((d, 1), (m, 1), (s_, 10000))
        ex['GPS'] = {
            piexif.GPSIFD.GPSLatitudeRef: b'N' if geo['latitude'] >= 0 else b'S',
            piexif.GPSIFD.GPSLatitude: dms(geo['latitude']),
            piexif.GPSIFD.GPSLongitudeRef: b'E' if geo['longitude'] >= 0 else b'W',
            piexif.GPSIFD.GPSLongitude: dms(geo['longitude']),
        }
        changed = True
    if not changed:
        return blob, False
    try:
        out = io.BytesIO()
        piexif.insert(piexif.dump(ex), blob, out)
        return out.getvalue(), True
    except Exception:
        return blob, False


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description='Turn Google Photos Takeout into a clean Year/Month library.')
    ap.add_argument('sources', nargs='+', help='Takeout zip file(s) or extracted folder(s)')
    ap.add_argument('--out', required=True, help='output folder (created if missing)')
    ap.add_argument('--dry-run', action='store_true', help='only report what would happen, write nothing')
    ap.add_argument('--no-albums', action='store_true', help='do not make Albums/<name> copies')
    ap.add_argument('--skip-trash', action='store_true', help='leave Trash out entirely')
    ap.add_argument('--no-exif', action='store_true', help='do not write dates into JPEG EXIF, only set file times')
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    t0 = time.time()

    # Pass 1: collect everything, group by folder.
    folders = {}  # folder -> {'media': [Entry], 'json': {'map': {(key,num): Entry}}, 'meta': Entry|None}
    total_bytes = 0
    # A folder that holds takeout*.zip files means "all of those zips".
    sources = []
    for src in args.sources:
        if not os.path.exists(src):
            sys.exit(f'not found: {src}')
        if os.path.isdir(src):
            zips = sorted(f for f in os.listdir(src) if f.lower().startswith('takeout') and f.lower().endswith('.zip'))
            if zips:
                sources += [os.path.join(src, f) for f in zips]
                continue
        sources.append(src)
    print('Reading:', ', '.join(os.path.basename(s) for s in sources), flush=True)

    for src in sources:
        for e in iter_source(src):
            parts = e.path.split('/')
            if len(parts) < 2:
                continue
            folder = '/'.join(parts[:-1])
            name = parts[-1]
            f = folders.setdefault(folder, {'media': [], 'json': {'map': {}}, 'meta': None})
            if name.lower().endswith('.json'):
                if name == 'metadata.json':
                    f['meta'] = e
                elif EXT_RE.search(name):
                    f['json']['map'][sidecar_key(name)] = e
                # other json (shared_album_comments etc.) ignored
            elif EXT_RE.fullmatch(os.path.splitext(name)[1] or ''):
                f['media'].append(e)
                total_bytes += e.size
            else:
                f.setdefault('other', []).append(e)

    def folder_kind(folder):
        leaf = folder.split('/')[-1]
        if leaf == 'Trash' or leaf == 'Bin':
            return 'trash', leaf
        if re.match(r'Photos from \d{4}$', leaf):
            return 'year', leaf
        if re.match(r'(Archive|Archiv)$', leaf):
            return 'year', leaf
        return 'album', leaf

    # Pass 2: copy. Year folders first so they become the primary copy.
    order = sorted(folders.items(), key=lambda kv: {'year': 0, 'album': 1, 'trash': 2}[folder_kind(kv[0])[0]])
    seen = {}            # sha1 -> dest path of primary copy
    stats = dict(media=0, placed=0, dup_skipped=0, album_copies=0, trash=0, no_sidecar=0,
                 date_sidecar=0, date_exif=0, date_name=0, date_unknown=0, exif_written=0, other=0)
    rows = []
    bad_chars = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

    def safe(name):
        return bad_chars.sub('_', name).strip().rstrip('.') or 'file'

    def unique_path(dest):
        if not os.path.exists(dest):
            return dest
        stem, ext = os.path.splitext(dest)
        i = 1
        while os.path.exists(f'{stem} ({i}){ext}'):
            i += 1
        return f'{stem} ({i}){ext}'

    def place(blob, dest, dt):
        if args.dry_run:
            return dest
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        dest = unique_path(dest)
        with open(dest, 'wb') as fh:
            fh.write(blob)
        if dt:
            ts = dt.timestamp()
            os.utime(dest, (ts, ts))
        return dest

    n_done = 0
    for folder, f in order:
        kind, leaf = folder_kind(folder)
        if kind == 'trash' and args.skip_trash:
            continue
        album_title = None
        if kind == 'album':
            album_title = leaf
            if f['meta']:
                try:
                    album_title = json.loads(f['meta'].read()).get('title') or leaf
                except Exception:
                    pass
        for e in f['media']:
            stats['media'] += 1
            n_done += 1
            if n_done % 100 == 0:
                print(f'  ... {n_done} files, {time.time()-t0:.0f}s', flush=True)
            name = e.path.split('/')[-1]
            sc = find_sidecar(f['json'], name)
            data = None
            if sc:
                try:
                    data = json.loads(sc.read())
                except Exception:
                    data = None
            else:
                stats['no_sidecar'] += 1
            blob = e.read()
            h = hashlib.sha1(blob).hexdigest()

            dt, src_ = (None, None)
            if data:
                dt, src_ = date_from_sidecar(data)
            is_jpeg = name.lower().endswith(('.jpg', '.jpeg'))
            if not dt and is_jpeg:
                dt = date_from_exif(blob)
                src_ = 'exif' if dt else None
            if not dt:
                dt = date_from_name(name)
                src_ = 'name' if dt else None
            if dt:
                stats['date_' + src_.split(':')[0]] += 1
            else:
                stats['date_unknown'] += 1

            geo = (data or {}).get('geoData') or (data or {}).get('geoDataExif')
            if is_jpeg and dt and not args.no_exif and not args.dry_run:
                blob, wrote = write_exif_date(blob, dt, geo)
                if wrote:
                    stats['exif_written'] += 1

            if kind == 'trash':
                dest_dir = os.path.join(out, '_Trash')
            elif dt:
                dest_dir = os.path.join(out, f'{dt.year:04d}', f'{dt.year:04d}-{dt.month:02d}')
            else:
                dest_dir = os.path.join(out, 'Unknown date')

            if h in seen:
                stats['dup_skipped'] += 1
                primary = seen[h]
                action = 'dup'
            else:
                primary = place(blob, os.path.join(dest_dir, safe(name)), dt)
                seen[h] = primary
                if kind == 'trash':
                    stats['trash'] += 1
                else:
                    stats['placed'] += 1
                action = 'placed'

            if kind == 'album' and not args.no_albums:
                place(blob, os.path.join(out, 'Albums', safe(album_title), safe(name)), dt)
                stats['album_copies'] += 1
                action += '+album'

            rows.append([e.path, action, primary, dt.isoformat() if dt else '', src_ or '', 'yes' if sc else 'no', album_title or ''])

        for e in f.get('other', []):
            stats['other'] += 1
            place(e.read(), os.path.join(out, 'Other', safe(e.path.split('/')[-1])), None)

    if not args.dry_run:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, 'untangle-log.csv'), 'w', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh)
            w.writerow(['source path', 'action', 'placed at', 'date used', 'date from', 'had sidecar', 'album'])
            w.writerows(rows)

    gb = total_bytes / 1e9
    print()
    print('DRY RUN, nothing written.' if args.dry_run else f'Done. Library is in: {out}')
    print(f'  photos+videos seen ........ {stats["media"]}  ({gb:.1f} GB)')
    print(f'  placed in Year/Month ...... {stats["placed"]}')
    print(f'  duplicates skipped ........ {stats["dup_skipped"]}  (same photo in several albums)')
    print(f'  album copies made ......... {stats["album_copies"]}')
    print(f'  trash kept in _Trash ...... {stats["trash"]}')
    print(f'  dates from sidecar ........ {stats["date_sidecar"]}')
    print(f'  dates from EXIF ........... {stats["date_exif"]}')
    print(f'  dates from file name ...... {stats["date_name"]}')
    print(f'  no date found ............. {stats["date_unknown"]}  (-> Unknown date folder)')
    print(f'  files with no sidecar ..... {stats["no_sidecar"]}')
    print(f'  EXIF dates written ........ {stats["exif_written"]}' + ('' if piexif else '  (piexif not installed)'))
    if stats['other']:
        print(f'  other files -> Other ...... {stats["other"]}')
    print(f'  took {time.time()-t0:.0f}s')
    if not args.dry_run:
        print(f'  per-file log: {os.path.join(out, "untangle-log.csv")}')


if __name__ == '__main__':
    main()
