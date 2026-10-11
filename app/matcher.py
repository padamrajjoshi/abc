"""Transactional SQLite registry with anatomically constrained matching."""
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .imaging import decode_png, validate_mode
from .quality import QualityScorer
from .segmentation import SlapSegmenter
from .sourceafis import create_engine


class SubjectNotFound(ValueError):
    pass


class AFISDatabase:
    def __init__(self, path='biometrics.db', segmenter=None, quality=None, engine=None):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.segmenter = segmenter or SlapSegmenter()
        self.quality = quality or QualityScorer()
        self.engine = engine or create_engine()
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS subjects (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    created_at TEXT NOT NULL, simulated INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS fingerprints (
                    id INTEGER PRIMARY KEY,
                    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                    finger_name TEXT NOT NULL, nfiq_score INTEGER NOT NULL CHECK(nfiq_score BETWEEN 1 AND 5),
                    image_png BLOB NOT NULL, template_bytes BLOB NOT NULL,
                    template_engine TEXT NOT NULL, quality_engine TEXT NOT NULL,
                    UNIQUE(subject_id, finger_name));
                CREATE INDEX IF NOT EXISTS fingerprints_subject ON fingerprints(subject_id);
                CREATE INDEX IF NOT EXISTS fingerprints_position ON fingerprints(finger_name);
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def process_slap(self, slap_image_bytes, mode='right4'):
        image = decode_png(slap_image_bytes)
        if image.shape != (1500, 1600):
            raise ValueError('Slap image must be exactly 1600 × 1500 pixels at 500 DPI.')
        fingers = self.segmenter.segment(image, mode)
        for finger in fingers:
            finger.update(self.quality.score(decode_png(finger['image_bytes'])))
            template, features = self.engine.extract(finger['image_bytes'])
            finger['template_bytes'] = template
            finger.update(features)
            finger['matcher_engine'] = self.engine.name
        return fingers

    def enroll_subject(self, name, slap_image_bytes, mode='right4', subject_id=None,
                       simulated=False, allow_estimated=True):
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError('Subject name must contain 1–120 characters.')
        fingers = self.process_slap(slap_image_bytes, mode)
        if any(f['status'] == 'FAIL' for f in fingers):
            raise ValueError('Enrollment rejected: one or more fingers failed quality (score > 3).')
        if not allow_estimated and any(f['estimated'] for f in fingers):
            raise ValueError('Enrollment requires measured NFIQ; estimated scores are disabled.')
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as db:
            if subject_id is None:
                subject_id = db.execute('INSERT INTO subjects(name,created_at,simulated) VALUES(?,?,?)',
                                        (name, now, int(simulated))).lastrowid
            else:
                subject = db.execute('SELECT * FROM subjects WHERE id=?', (subject_id,)).fetchone()
                if subject is None:
                    raise SubjectNotFound('Subject does not exist.')
                if subject['name'] != name or bool(subject['simulated']) != simulated:
                    raise ValueError('Existing subject name or simulation provenance differs.')
            for finger in fingers:
                db.execute('''INSERT INTO fingerprints(subject_id,finger_name,nfiq_score,image_png,
                              template_bytes,template_engine,quality_engine) VALUES(?,?,?,?,?,?,?)
                              ON CONFLICT(subject_id,finger_name) DO UPDATE SET
                              nfiq_score=excluded.nfiq_score,image_png=excluded.image_png,
                              template_bytes=excluded.template_bytes,template_engine=excluded.template_engine,
                              quality_engine=excluded.quality_engine''',
                           (subject_id, finger['finger_name'], finger['nfiq_score'], finger['image_bytes'],
                            finger['template_bytes'], self.engine.name, finger['quality_engine']))
        return {'subject_id': subject_id, 'name': name, 'enrolled_fingers': len(fingers),
                'mode': mode, 'matcher_engine': self.engine.name, 'simulated': simulated}

    def subjects(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute('''SELECT s.*,count(f.id) AS finger_count,
                    group_concat(f.finger_name, ', ') AS fingers FROM subjects s
                    LEFT JOIN fingerprints f ON f.subject_id=s.id GROUP BY s.id ORDER BY s.id DESC''')]

    def fingerprint(self, fingerprint_id):
        with self.connection() as db:
            row = db.execute('SELECT image_png FROM fingerprints WHERE id=?', (fingerprint_id,)).fetchone()
        if row is None:
            raise SubjectNotFound('Fingerprint does not exist.')
        return bytes(row['image_png'])

    def delete_subject(self, subject_id: int):
        with self.connection() as db:
            cur = db.execute('DELETE FROM subjects WHERE id=?', (subject_id,))
            return cur.rowcount > 0

    def clear_all(self):
        with self.connection() as db:
            db.execute('DELETE FROM fingerprints')
            db.execute('DELETE FROM subjects')
            return True

    def subject_details(self, subject_id: int):
        with self.connection() as db:
            s_row = db.execute('SELECT * FROM subjects WHERE id=?', (subject_id,)).fetchone()
            if not s_row:
                return None
            subject = dict(s_row)
            f_rows = db.execute(
                'SELECT id, finger_name, nfiq_score, image_png, quality_engine FROM fingerprints WHERE subject_id=? ORDER BY id ASC',
                (subject_id,)
            ).fetchall()
            from .imaging import to_base64
            subject['fingers'] = []
            for f in f_rows:
                nfiq = f['nfiq_score']
                rating = 'PASS (High Quality)' if nfiq <= 2 else 'PASS (Marginal)' if nfiq == 3 else 'RECAPTURE (Fail)'
                subject['fingers'].append({
                    'id': f['id'],
                    'finger_name': f['finger_name'],
                    'nfiq_score': nfiq,
                    'quality_rating': rating,
                    'status': 'PASS' if nfiq <= 3 else 'FAIL',
                    'quality_engine': f['quality_engine'],
                    'image_base64': to_base64(bytes(f['image_png']))
                })
            subject['finger_count'] = len(subject['fingers'])
            return subject


    def _probe(self, data, mode, finger_name):
        if mode == 'single':
            valid_names = set(sum([validate_mode(m) for m in ('right4', 'left4', 'thumbs')], []))
            if finger_name not in valid_names:
                raise ValueError('Select an anatomical finger_name for a single probe.')
            image = decode_png(data)
            quality = self.quality.score(image)
            if quality['status'] == 'FAIL':
                raise ValueError('Probe failed quality; recapture.')
            template, _ = self.engine.extract(data)
            return [(finger_name, self.engine.prepare(template))]
        fingers = self.process_slap(data, mode)
        if any(f['status'] == 'FAIL' for f in fingers):
            raise ValueError('Probe contains a failed quality finger; recapture.')
        return [(f['finger_name'], self.engine.prepare(f['template_bytes'])) for f in fingers]

    def _search(self, data, threshold, mode, finger_name, subject_id=None, simulated=None):
        if not math.isfinite(threshold) or not 1 <= threshold <= 10000:
            raise ValueError('Threshold must be finite and between 1 and 10000.')
        probes = self._probe(data, mode, finger_name)
        with self.connection() as db:
            if subject_id is not None and db.execute('SELECT id FROM subjects WHERE id=?', (subject_id,)).fetchone() is None:
                raise SubjectNotFound('Subject does not exist.')
            query = '''SELECT f.*,s.name,s.simulated FROM fingerprints f
                       JOIN subjects s ON s.id=f.subject_id WHERE 1=1'''
            params = []
            if subject_id is not None:
                query += ' AND s.id=?'
                params.append(subject_id)
            if simulated is not None:
                query += ' AND s.simulated=?'
                params.append(int(simulated))
            rows = list(db.execute(query, params))
        candidates = {}
        for row in rows:
            for position, matcher in probes:
                if row['finger_name'] != position:
                    continue
                # Stored PNG enables lazy re-extraction after engine upgrades.
                template = bytes(row['template_bytes'])
                if row['template_engine'] != self.engine.name:
                    template, _ = self.engine.extract(bytes(row['image_png']))
                score = self.engine.match(matcher, template)
                candidate = candidates.setdefault(row['subject_id'], {
                    'subject_id': row['subject_id'], 'name': row['name'],
                    'simulated': bool(row['simulated']), 'finger_scores': []})
                candidate['finger_scores'].append({'finger_name': position, 'score': round(score, 3),
                                                   'fingerprint_id': row['id']})
        ranked = []
        for candidate in candidates.values():
            scores = candidate['finger_scores']
            candidate.update(score=round(min(s['score'] for s in scores), 3),
                             threshold=threshold, matcher_engine=self.engine.name,
                             score_calibrated=False, demo_matcher=self.engine.is_demo,
                             compared_fingers=len(scores), required_fingers=len(probes))
            # Require every requested anatomical position; conservative minimum fusion.
            candidate['is_match'] = len(scores) == len(probes) and candidate['score'] >= threshold
            candidate['aggregation'] = 'minimum score across all requested fingers'
            ranked.append(candidate)
        return sorted(ranked, key=lambda c: (-c['score'], c['subject_id']))

    def verify_1to1(self, subject_id, probe_image_bytes, threshold=40.0,
                   mode='right4', finger_name=None, simulated=None):
        ranked = self._search(probe_image_bytes, threshold, mode, finger_name, subject_id, simulated)
        if ranked:
            return ranked[0]
        return {'subject_id': subject_id, 'score': 0.0, 'is_match': False,
                'threshold': threshold, 'matcher_engine': self.engine.name,
                'compared_fingers': 0, 'reason': 'No compatible enrolled finger positions or capture provenance.'}

    def search_1toN(self, probe_image_bytes, threshold=40.0, mode='right4',
                   finger_name=None, simulated=None):
        return [c for c in self._search(probe_image_bytes, threshold, mode, finger_name,
                                       simulated=simulated) if c['is_match']]
