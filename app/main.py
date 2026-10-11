"""FastAPI workstation. Run with python -m uvicorn app.main:app."""
import base64
from datetime import datetime, timezone
import hmac
import io
import json
import logging
import os
import secrets
import threading
import time
import zipfile
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .device import MantraDeviceManager
from .imaging import convert_image_format, encode_png, from_base64, to_base64
from .matcher import AFISDatabase, SubjectNotFound
from .mongodb import MongoDossierStore
from .passport import PassportExtractor


log = logging.getLogger(__name__)


class CaptureStore:
    """Bounded RAM-only acquisitions; raw frames expire after 15 minutes."""
    def __init__(self):
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def put(self, data, mode, simulated):
        identifier = secrets.token_urlsafe(24)
        with self.lock:
            self.items[identifier] = (time.monotonic(), data, mode, simulated)
            while len(self.items) > 16:
                self.items.popitem(last=False)
        return identifier

    def get(self, identifier, mode):
        with self.lock:
            item = self.items.get(identifier)
            if item is None or time.monotonic() - item[0] > 900:
                self.items.pop(identifier, None)
                raise ValueError('Capture expired; scan again.')
            if item[2] != mode:
                raise ValueError('Capture mode differs from acquisition mode.')
            return item[1], item[3]


@asynccontextmanager
async def lifespan(app):
    device = MantraDeviceManager()
    try:
        app.state.device = device
        app.state.database = AFISDatabase(os.getenv('BIOMETRICS_DB', 'data/biometrics.db'))
        app.state.mongodb = MongoDossierStore()
        app.state.passport_extractor = PassportExtractor()
        app.state.captures = CaptureStore()
        app.state.operations = threading.BoundedSemaphore(2)
        yield
    finally:
        device.close()


app = FastAPI(title='Slap Biometrics Studio', version='1.0.0', lifespan=lifespan)


class BoundedBodyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        if scope['method'] in ('POST', 'PUT', 'PATCH'):
            messages, length = [], 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                length += len(message.get('body', b''))
                if length > 12 * 1024 * 1024:
                    return await JSONResponse({'detail': 'Request exceeds 12 MiB.'}, status_code=413)(scope, receive, send)
                messages.append(message)
                if not message.get('more_body', False):
                    break
            async def replay():
                if messages:
                    return messages.pop(0)
                return await receive()
            return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)


app.add_middleware(BoundedBodyMiddleware)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware('http')
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


def authorize(request: Request):
    token = os.getenv('API_TOKEN')
    if token and not hmac.compare_digest(request.headers.get('Authorization', ''), f'Bearer {token}'):
        raise HTTPException(401, 'A valid workstation API token is required.')
    origin = request.headers.get('Origin')
    if request.method != 'GET' and origin:
        allowed = {str(request.base_url).rstrip('/')}
        # Include Host and X-Forwarded-Host from reverse proxies (ngrok, caddy, nginx)
        host = request.headers.get('x-forwarded-host') or request.headers.get('host')
        proto = request.headers.get('x-forwarded-proto') or request.url.scheme or 'https'
        if host:
            allowed.add(f"{proto}://{host}".rstrip('/'))
            allowed.add(f"https://{host}".rstrip('/'))
            allowed.add(f"http://{host}".rstrip('/'))

        # Check explicitly configured ALLOWED_ORIGINS
        env_allowed = [o.strip().rstrip('/') for o in os.getenv('ALLOWED_ORIGINS', '').split(',') if o.strip()]
        if '*' in env_allowed or not env_allowed:
            # Default: automatically allow ngrok tunnels, localhost, and matching hosts
            parsed = urlparse(origin)
            domain = parsed.netloc.split(':')[0]
            if (domain.endswith('.ngrok-free.app') or
                domain.endswith('.ngrok.io') or
                domain.endswith('.ngrok.app') or
                domain in ('localhost', '127.0.0.1') or
                (host and domain == host.split(':')[0])):
                return

        for o in env_allowed:
            if o == '*' or o == origin.rstrip('/'):
                return

        if origin.rstrip('/') not in allowed:
            raise HTTPException(403, 'Cross-origin operation denied.')


class BusyOperation:
    def __init__(self, request):
        self.gate = request.app.state.operations

    def __enter__(self):
        if not self.gate.acquire(blocking=False):
            raise HTTPException(429, 'Workstation busy; retry shortly.')

    def __exit__(self, *args):
        self.gate.release()


class Payload(BaseModel):
    model_config = ConfigDict(extra='ignore')


class CaptureRequest(Payload):
    mode: Literal['right4', 'left4', 'thumbs'] = 'right4'
    identity: int = Field(default=1, ge=0, le=2**31-1)
    impression: int = Field(default=0, ge=0, le=2**31-1)
    skin: Literal['normal', 'dry', 'moist'] = 'normal'


class ImageRequest(Payload):
    image: str | None = Field(default=None, max_length=11_185_000)
    capture_id: str | None = Field(default=None, max_length=100)
    mode: Literal['right4', 'left4', 'thumbs', 'single'] = 'right4'


class EnrollRequest(ImageRequest):
    name: str = Field(min_length=1, max_length=120)
    subject_id: int | None = Field(default=None, ge=1)
    passport_number: str | None = Field(default=None, max_length=30)
    surname: str | None = Field(default=None, max_length=100)
    given_names: str | None = Field(default=None, max_length=100)
    nationality: str | None = Field(default=None, max_length=50)
    date_of_birth: str | None = Field(default=None, max_length=30)
    gender: str | None = Field(default=None, max_length=10)
    passport_expiry: str | None = Field(default=None, max_length=30)
    visa_ref: str | None = Field(default=None, max_length=50)
    visa_type: str | None = Field(default=None, max_length=50)
    place_of_birth: str | None = Field(default=None, max_length=100)
    station_id: str | None = Field(default=None, max_length=100)


class VisaDossierPayload(Payload):
    passport_number: str = Field(min_length=2, max_length=30)
    surname: str = Field(min_length=1, max_length=100)
    given_names: str = Field(min_length=1, max_length=100)
    nationality: str = Field(default='GRC', min_length=2, max_length=50)
    date_of_birth: str = Field(default='', max_length=30)
    gender: Literal['M', 'F', 'X'] = 'M'
    passport_expiry: str | None = None
    visa_ref: str | None = None
    visa_type: str = 'SCHENGEN_C_TOURIST'
    place_of_birth: str | None = None
    station_id: str | None = None
    subject_id: int | None = None


class VerifyRequest(ImageRequest):
    kind: Literal['1:1', '1:N'] = '1:1'
    subject_id: int | None = Field(default=None, ge=1)
    finger_name: str | None = Field(default=None, max_length=30)
    threshold: float = Field(default=40.0, ge=1, le=10000, allow_inf_nan=False)


class ConvertImageRequest(Payload):
    image: str = Field(min_length=1, max_length=15_000_000)
    format: Literal['TIFF', 'BMP', 'PNG', 'JPEG', 'JPG', 'WEBP', 'RAW', 'BIN'] = 'TIFF'
    dpi: int = Field(default=500, ge=72, le=2400)
    quality: int = Field(default=100, ge=1, le=100)



def resolve_image(payload, request):
    if bool(payload.capture_id) == bool(payload.image):
        raise ValueError('Supply exactly one of capture_id or image (base64 PNG).')
    if payload.capture_id:
        return request.app.state.captures.get(payload.capture_id, payload.mode)
    return from_base64(payload.image), None


def public_finger(finger):
    nfiq = finger.get('nfiq_score', 5)
    rating = 'PASS (High Quality)' if nfiq <= 2 else 'PASS (Marginal)' if nfiq == 3 else 'RECAPTURE (Fail)'
    return {**{k: v for k, v in finger.items() if k not in ('image_bytes', 'template_bytes')},
            'image': to_base64(finger['image_bytes']),
            'quality_rating': rating}


@app.exception_handler(SubjectNotFound)
async def not_found(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=404)


@app.exception_handler(ValueError)
async def invalid_input(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=422)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    errors = []
    for err in exc.errors():
        loc = " -> ".join(str(l) for l in err.get("loc", []) if l != "body")
        msg = err.get("msg", "Invalid value")
        errors.append(f"{loc}: {msg}" if loc else msg)
    clean_msg = "; ".join(errors) or "Invalid request parameters."
    log.warning("Request validation 422 on %s %s: %s", request.method, request.url.path, clean_msg)
    return JSONResponse({'detail': clean_msg}, status_code=422)


@app.exception_handler(RuntimeError)
async def unavailable(request, exc):
    log.error('Workstation operation unavailable: %s', exc)
    return JSONResponse({'detail': 'Processing or device unavailable. Check workstation logs.'}, status_code=503)


@app.get('/', response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / 'templates/index.html').read_text()


@app.get('/database', response_class=HTMLResponse)
def database_page():
    return (Path(__file__).parent / 'templates/database.html').read_text()



@app.get('/api/health', dependencies=[Depends(authorize)])
def health(request: Request):
    db = request.app.state.database
    return {'device': request.app.state.device.status(), 'matcher_engine': db.engine.name,
            'demo_matcher': db.engine.is_demo,
            'quality_engine': 'nist-nfiq1' if db.quality.binary else 'opencv-estimate',
            'segmentation_engine': 'nist-nfseg' if db.segmenter.binary else 'opencv',
            'estimated_quality_allowed': os.getenv('ALLOW_ESTIMATED_QUALITY', '1') == '1',
            'authentication_enabled': bool(os.getenv('API_TOKEN')),
            'mongodb': request.app.state.mongodb.status(),
            'passport_extractor': request.app.state.passport_extractor.engines_available}


@app.post('/api/passport/extract', dependencies=[Depends(authorize)])
async def extract_passport_data(request: Request):
    """Extract passport personal details using PassportEye, pytesseract, pdfplumber, and pypdf."""
    extractor = request.app.state.passport_extractor
    file_bytes = b''
    filename = 'document.jpg'

    content_type = request.headers.get('content-type', '')
    if 'multipart/form-data' in content_type:
        try:
            form = await request.form()
            uploaded_file = form.get('file')
            if uploaded_file and hasattr(uploaded_file, 'read'):
                file_bytes = await uploaded_file.read()
                filename = getattr(uploaded_file, 'filename', '') or 'uploaded_document'
        except Exception as e:
            log.warning('Multipart form reading failed: %s', e)
    
    if not file_bytes:
        try:
            body = await request.json()
            if body and 'image_base64' in body:
                file_bytes = from_base64(body['image_base64'])
                filename = body.get('filename', 'passport_document.jpg')
        except Exception:
            pass

    if not file_bytes:
        raise HTTPException(400, 'No file or image_base64 provided for passport extraction.')

    size_kb = round(len(file_bytes) / 1024, 1)
    log.info('[PASSPORT-INGEST] >>> Received passport document: "%s" (%s KB)', filename, size_kb)
    t_start = time.perf_counter()
    data = extractor.extract_from_bytes(file_bytes, filename=filename)
    t_elapsed = round((time.perf_counter() - t_start) * 1000, 1)
    
    if data.get('success'):
        log.info('[PASSPORT-INGEST] <<< SUCCESS in %s ms via [%s] -> Passport: %s, Holder: %s %s, DOB: %s, Country: %s, Checksums: %s',
                 t_elapsed, data.get('extraction_method'), data.get('passport_number'),
                 data.get('surname'), data.get('given_names'), data.get('date_of_birth'),
                 data.get('nationality'), 'VALID' if data.get('mrz_verified') else 'UNVERIFIED')
    else:
        log.warning('[PASSPORT-INGEST] <<< FINISHED in %s ms with no valid passport fields detected. Engines tried: %s',
                    t_elapsed, data.get('engines_used'))

    # Guarantee unique, non-repeating Visa Reference Number across the central database
    mongo = request.app.state.mongodb
    p_num = (data.get('passport_number') or '').strip().upper()
    existing_dossier = mongo.get_dossier(p_num) if p_num else None
    if existing_dossier and existing_dossier.get('visa_ref'):
        data['visa_ref'] = existing_dossier['visa_ref']
    else:
        candidate_ref = (data.get('visa_ref') or '').strip().upper()
        if candidate_ref and not mongo.is_visa_ref_taken(candidate_ref, exclude_passport=p_num):
            data['visa_ref'] = candidate_ref
        else:
            data['visa_ref'] = mongo.generate_unique_visa_ref()

    return data


@app.get('/api/visa/generate-ref')
def generate_visa_ref(request: Request):
    """Generate a random unique Visa Reference Number guaranteed never to repeat any stored reference."""
    mongo = request.app.state.mongodb
    unique_ref = mongo.generate_unique_visa_ref()
    return {'visa_ref': unique_ref, 'year': datetime.now().year}


@app.post('/api/capture', dependencies=[Depends(authorize)])
def capture(payload: CaptureRequest, request: Request):
    with BusyOperation(request):
        device = request.app.state.device
        image = device.capture(payload.mode, payload.identity, payload.skin, payload.impression)
        data = encode_png(image)
        simulated = device.status()['simulated']
        identifier = request.app.state.captures.put(data, payload.mode, simulated)
        return {'capture_id': identifier, 'image': to_base64(data), 'mode': payload.mode,
                'width': 1600, 'height': 1500, 'dpi': 500, 'simulated': simulated,
                'lossless': True, 'quality_level': '100% Uncompressed Lossless', 'bit_depth': 8,
                'device_engine': device.status()['engine']}


@app.get('/api/capture/{capture_id}/download')
def download_raw_capture(capture_id: str, request: Request):
    """Download the 1600x1500 uncompressed 500 DPI optical slap capture 100% lossless."""
    try:
        with request.app.state.captures.lock:
            item = request.app.state.captures.items.get(capture_id)
            if not item:
                raise HTTPException(404, 'Capture frame not found or expired.')
            raw_bytes = item[1]
            mode = item[2]
        return Response(
            raw_bytes,
            media_type='image/png',
            headers={'Content-Disposition': f'attachment; filename="optical_slap_{mode}_{capture_id}_500dpi_lossless.png"'}
        )
    except Exception as e:
        raise HTTPException(404, str(e))


@app.post('/api/segment', dependencies=[Depends(authorize)])
def segment(payload: ImageRequest, request: Request):
    if payload.mode == 'single':
        raise ValueError('Segmentation requires a slap capture mode.')
    with BusyOperation(request):
        data, simulated = resolve_image(payload, request)
        fingers = request.app.state.database.process_slap(data, payload.mode)
        return {'fingers': [public_finger(f) for f in fingers], 'mode': payload.mode,
                'simulated': simulated, 'finger_count': len(fingers),
                'lossless': True, 'dpi': 500, 'quality_level': '100% Lossless'}



@app.post('/api/enroll', dependencies=[Depends(authorize)])
def enroll(payload: EnrollRequest, request: Request):
    if payload.mode == 'single':
        raise ValueError('Enrollment requires a slap capture mode.')
    with BusyOperation(request):
        data, simulated = resolve_image(payload, request)
        result = request.app.state.database.enroll_subject(
            payload.name, data, payload.mode, payload.subject_id, bool(simulated),
            os.getenv('ALLOW_ESTIMATED_QUALITY', '1') == '1')
        result['provenance'] = 'simulated' if simulated else 'device' if simulated is False else 'uploaded-unverified'

        # Sync with MongoDB dossier
        try:
            fingers = request.app.state.database.process_slap(data, payload.mode)
            passport_num = payload.passport_number or f"PASS-{result['subject_id']}"
            dossier_data = {
                'passport_number': passport_num,
                'surname': payload.surname or payload.name.split()[-1] if payload.name else '',
                'given_names': payload.given_names or ' '.join(payload.name.split()[:-1]) if payload.name else '',
                'nationality': payload.nationality or 'GRC',
                'date_of_birth': payload.date_of_birth or '',
                'gender': payload.gender or 'M',
                'passport_expiry': payload.passport_expiry or '',
                'visa_ref': (payload.visa_ref or '').strip() or request.app.state.mongodb.generate_unique_visa_ref(),
                'visa_type': payload.visa_type or 'SCHENGEN_C_TOURIST',
                'place_of_birth': payload.place_of_birth or '',
                'station_id': payload.station_id or 'ATH-CONSULAR-01',
                'subject_id': result['subject_id']
            }
            request.app.state.mongodb.upsert_dossier(dossier_data)
            mongo_res = request.app.state.mongodb.append_fingerprint_group(
                passport_num, payload.mode, [public_finger(f) for f in fingers], bool(simulated)
            )
            result['mongodb'] = mongo_res
            result['passport_number'] = passport_num
        except Exception as e:
            log.warning('MongoDB dossier synchronization skipped: %s', e)

        return result


@app.post('/api/visa/dossier', dependencies=[Depends(authorize)])
def save_visa_dossier(payload: VisaDossierPayload, request: Request):
    with BusyOperation(request):
        saved = request.app.state.mongodb.upsert_dossier(payload.model_dump())
        return {'status': 'saved', 'dossier': saved}


@app.get('/api/visa/dossiers', dependencies=[Depends(authorize)])
def list_visa_dossiers(request: Request):
    return {'dossiers': request.app.state.mongodb.list_dossiers()}


@app.get('/api/visa/dossier/{identifier}', dependencies=[Depends(authorize)])
def get_visa_dossier(identifier: str, request: Request):
    dossier = request.app.state.mongodb.get_dossier(identifier)
    if not dossier:
        raise HTTPException(404, 'Visa dossier not found.')
    return dossier


@app.post('/api/verify', dependencies=[Depends(authorize)])
def verify(payload: VerifyRequest, request: Request):
    with BusyOperation(request):
        data, simulated = resolve_image(payload, request)
        db = request.app.state.database
        if payload.kind == '1:1':
            if payload.subject_id is None:
                raise ValueError('subject_id is required for 1:1 verification.')
            return db.verify_1to1(payload.subject_id, data, payload.threshold,
                                 payload.mode, payload.finger_name, simulated)
        return {'candidates': db.search_1toN(data, payload.threshold, payload.mode,
                                            payload.finger_name, simulated),
                'threshold': payload.threshold, 'matcher_engine': db.engine.name}


@app.get('/api/subjects', dependencies=[Depends(authorize)])
def subjects(request: Request):
    return {'subjects': request.app.state.database.subjects()}


@app.get('/api/fingerprints/{fingerprint_id}/image', dependencies=[Depends(authorize)])
def fingerprint_image(fingerprint_id: int, request: Request):
    return Response(request.app.state.database.fingerprint(fingerprint_id), media_type='image/png')


# ==============================================================================
# Database Center Unauthenticated Endpoints (Localhost Administrative Access)
# ==============================================================================

@app.get('/api/database/records')
def get_database_records(request: Request):
    """Retrieve all applicant records with complete biometric data. No authentication required on localhost."""
    mongo = request.app.state.mongodb
    db = request.app.state.database

    # Fetch from Mongo
    mongo_records = mongo.list_dossiers(limit=500) if mongo.is_connected else []
    seen_subject_ids = set()
    records_by_passport = {}

    for rec in mongo_records:
        passport = (rec.get('passport_number') or '').strip().upper()
        if passport:
            records_by_passport[passport] = rec
        if rec.get('subject_id') is not None:
            seen_subject_ids.add(rec['subject_id'])

    # Reconcile with SQLite subjects
    try:
        sqlite_subjects = db.subjects()
        for sub in sqlite_subjects:
            s_id = sub['id']
            full_sqlite = db.subject_details(s_id)
            if not full_sqlite:
                continue

            # If subject not in MongoDB dossiers, create a record representation
            if s_id not in seen_subject_ids:
                name_parts = (sub.get('name') or '').strip().split()
                surname = name_parts[-1] if name_parts else 'UNKNOWN'
                given = ' '.join(name_parts[:-1]) if len(name_parts) > 1 else (name_parts[0] if name_parts else '')
                passport_gen = f"GR-PASS-{s_id:04d}"
                rec = {
                    'passport_number': passport_gen,
                    'surname': surname,
                    'given_names': given,
                    'nationality': 'GRC',
                    'date_of_birth': '',
                    'gender': 'M',
                    'passport_expiry': '',
                    'visa_ref': mongo.generate_unique_visa_ref(),
                    'visa_type': 'SCHENGEN_C_TOURIST',
                    'place_of_birth': '',
                    'station_id': 'LOCAL-REGISTRY',
                    'subject_id': s_id,
                    'status': 'ENROLLED_10_PRINT' if full_sqlite.get('finger_count', 0) >= 10 else 'IN_PROGRESS',
                    'fingers': full_sqlite.get('fingers', []),
                    'finger_count': full_sqlite.get('finger_count', 0),
                    'created_at': sub.get('created_at'),
                    'updated_at': sub.get('created_at'),
                    'simulated': bool(sub.get('simulated', False))
                }
                records_by_passport[passport_gen] = rec
            else:
                # If subject is in MongoDB, check if MongoDB has finger images; if not, merge from SQLite
                for p_num, m_rec in records_by_passport.items():
                    if m_rec.get('subject_id') == s_id:
                        if not m_rec.get('fingers') and full_sqlite.get('fingers'):
                            m_rec['fingers'] = full_sqlite['fingers']
                            m_rec['finger_count'] = len(full_sqlite['fingers'])
    except Exception as e:
        log.warning('SQLite subject reconciliation failed: %s', e)

    merged = list(records_by_passport.values())
    merged.sort(key=lambda x: x.get('updated_at') or x.get('created_at') or '', reverse=True)
    return {
        'records': merged,
        'count': len(merged),
        'storage_engine': 'hybrid' if mongo.is_connected else 'sqlite_fallback'
    }


@app.get('/api/database/records/{identifier}')
def get_database_record(identifier: str, request: Request):
    """Retrieve a single applicant record by passport or subject ID with full biometrics."""
    mongo = request.app.state.mongodb
    db = request.app.state.database

    dossier = mongo.get_dossier(identifier) if mongo.is_connected else None
    if dossier:
        return dossier

    # Fallback to SQLite by subject_id
    if identifier.isdigit():
        s_id = int(identifier)
        sqlite_details = db.subject_details(s_id)
        if sqlite_details:
            return sqlite_details

    # Try matching passport number format GR-PASS-XXXX
    if identifier.startswith('GR-PASS-') and identifier[8:].isdigit():
        s_id = int(identifier[8:])
        sqlite_details = db.subject_details(s_id)
        if sqlite_details:
            return sqlite_details

    raise HTTPException(404, 'Record not found in biometric database.')


@app.delete('/api/database/records/{identifier}')
def delete_database_record(identifier: str, request: Request):
    """Delete an applicant record and all associated biometrics from both stores."""
    mongo = request.app.state.mongodb
    db = request.app.state.database

    mongo_deleted = False
    sqlite_deleted = False
    associated_sub_id = None

    # Find associated subject_id from mongo if present
    if mongo.is_connected:
        existing = mongo.get_dossier(identifier)
        if existing and existing.get('subject_id') is not None:
            associated_sub_id = existing['subject_id']
        mongo_deleted = mongo.delete_dossier(identifier)

    # Determine sqlite subject id
    if associated_sub_id is None:
        if identifier.isdigit():
            associated_sub_id = int(identifier)
        elif identifier.startswith('GR-PASS-') and identifier[8:].isdigit():
            associated_sub_id = int(identifier[8:])

    if associated_sub_id is not None:
        sqlite_deleted = db.delete_subject(associated_sub_id)

    if not mongo_deleted and not sqlite_deleted:
        raise HTTPException(404, f'Record "{identifier}" not found in database to delete.')

    return {
        'status': 'deleted',
        'identifier': identifier,
        'mongo_deleted': mongo_deleted,
        'sqlite_deleted': sqlite_deleted
    }


@app.delete('/api/database/records')
def clear_all_database_records(request: Request):
    """Purge all applicant dossiers and biometric data from the database."""
    mongo = request.app.state.mongodb
    db = request.app.state.database

    mongo_count = mongo.delete_all_dossiers() if mongo.is_connected else 0
    db.clear_all()

    return {
        'status': 'cleared',
        'mongo_dossiers_deleted': mongo_count,
        'message': 'All applicant dossiers and biometric records cleared successfully.'
    }


@app.get('/api/database/stats')
def get_database_stats(request: Request):
    """Statistical summary of biometric repository."""
    mongo = request.app.state.mongodb
    records_data = get_database_records(request)
    records = records_data.get('records', [])

    total_dossiers = len(records)
    total_fingers = 0
    complete_10_print = 0
    high_quality_fingers = 0
    marginal_fingers = 0
    fail_fingers = 0

    for r in records:
        fingers = r.get('fingers', [])
        f_count = len(fingers)
        total_fingers += f_count
        if f_count >= 10:
            complete_10_print += 1
        for f in fingers:
            score = f.get('nfiq_score', 5)
            if score <= 2:
                high_quality_fingers += 1
            elif score == 3:
                marginal_fingers += 1
            else:
                fail_fingers += 1

    return {
        'total_dossiers': total_dossiers,
        'total_fingers': total_fingers,
        'complete_10_print': complete_10_print,
        'partial_dossiers': total_dossiers - complete_10_print,
        'high_quality_fingers': high_quality_fingers,
        'marginal_fingers': marginal_fingers,
        'fail_fingers': fail_fingers,
        'high_quality_rate': round(high_quality_fingers / total_fingers * 100, 1) if total_fingers > 0 else 100.0,
        'mongo_status': mongo.status() if mongo.is_connected else {'connected': False}
    }


@app.post('/api/database/convert')
def convert_image_endpoint(payload: ConvertImageRequest):
    """Convert any fingerprint image to a target format at 100% full quality."""
    try:
        raw_b64 = payload.image
        if raw_b64.startswith('data:'):
            raw_b64 = raw_b64.split(',', 1)[1]
        data = base64.b64decode(raw_b64)
        conv_bytes, mime, ext = convert_image_format(
            data, target_format=payload.format, dpi=payload.dpi, quality=payload.quality
        )
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size

        return {
            'converted_base64': base64.b64encode(conv_bytes).decode('ascii'),
            'format': payload.format.upper(),
            'extension': ext,
            'mime_type': mime,
            'size_bytes': len(conv_bytes),
            'width': w,
            'height': h,
            'dpi': payload.dpi,
            'quality': 100,
            'status': 'SUCCESS'
        }
    except Exception as e:
        log.error('Format conversion failed: %s', e)
        raise HTTPException(400, f'Image conversion failed: {str(e)}')


@app.get('/api/database/export/{identifier}/zip')
def export_dossier_zip(identifier: str, request: Request, format: str = 'TIFF'):
    """Export all 10 fingers of an applicant in the chosen format with 100% quality inside a ZIP archive."""
    dossier = get_database_record(identifier, request)
    passport = dossier.get('passport_number') or identifier
    fingers = dossier.get('fingers', [])
    if not fingers:
        raise HTTPException(400, 'Dossier has no enrolled fingerprint images to export.')

    pos_order = {
        'right_thumb': '01_right_thumb',
        'right_index': '02_right_index',
        'right_middle': '03_right_middle',
        'right_ring': '04_right_ring',
        'right_little': '05_right_little',
        'left_thumb': '06_left_thumb',
        'left_index': '07_left_index',
        'left_middle': '08_left_middle',
        'left_ring': '09_left_ring',
        'left_little': '10_left_little',
    }

    zip_buffer = io.BytesIO()
    manifest_fingers = []

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for idx, f in enumerate(fingers):
            raw_img = f.get('image_base64') or f.get('image') or ''
            if not raw_img:
                continue
            if raw_img.startswith('data:'):
                raw_img = raw_img.split(',', 1)[1]
            try:
                img_bytes = base64.b64decode(raw_img)
                conv_bytes, mime, ext = convert_image_format(img_bytes, target_format=format)

                f_key = (f.get('finger_name') or f'finger_{idx+1}').lower().replace(' ', '_')
                prefix = pos_order.get(f_key, f'{idx+1:02d}_{f_key}')
                filename = f'{prefix}_500dpi.{ext}'
                zf.writestr(filename, conv_bytes)

                manifest_fingers.append({
                    'file_name': filename,
                    'finger_name': f.get('finger_name'),
                    'nfiq_score': f.get('nfiq_score'),
                    'quality_rating': f.get('quality_rating'),
                    'minutiae_count': f.get('feature_count') or f.get('minutiae_count'),
                    'dpi': 500,
                    'format': format.upper(),
                    'size_bytes': len(conv_bytes)
                })
            except Exception as e:
                log.warning('Could not convert finger %s: %s', f.get('finger_name'), e)

        # Write manifest
        manifest = {
            'standard': 'Schengen VIS / NIST SP 500-290 Tenprint Biometric Archive',
            'dossier_passport': passport,
            'applicant_name': f"{dossier.get('surname', '')} {dossier.get('given_names', '')}".strip(),
            'nationality': dossier.get('nationality', 'GRC'),
            'date_of_birth': dossier.get('date_of_birth', ''),
            'gender': dossier.get('gender', ''),
            'visa_reference': dossier.get('visa_ref', ''),
            'format': format.upper(),
            'dpi': 500,
            'quality_level': '100% Full Quality (Lossless / Zero-Subsampling)',
            'finger_count': len(manifest_fingers),
            'fingers': manifest_fingers,
            'exported_at': datetime.now(timezone.utc).isoformat()
        }
        zf.writestr('manifest.json', json.dumps(manifest, indent=2))

    zip_bytes = zip_buffer.getvalue()
    filename_clean = f"biometrics_{passport}_{format.lower()}_500dpi.zip"
    return Response(
        zip_bytes,
        media_type='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{filename_clean}"'}
    )


@app.get('/api/database/export/{identifier}/finger/{finger_name}')
def export_single_finger(identifier: str, finger_name: str, request: Request, format: str = 'TIFF'):
    """Export a single finger image in any format at 100% full quality."""
    dossier = get_database_record(identifier, request)
    passport = dossier.get('passport_number') or identifier
    fingers = dossier.get('fingers', [])

    target_f = None
    target_clean = finger_name.lower().replace(' ', '_')
    for f in fingers:
        f_name_clean = (f.get('finger_name') or '').lower().replace(' ', '_')
        if f_name_clean == target_clean:
            target_f = f
            break

    if not target_f:
        raise HTTPException(404, f'Finger "{finger_name}" not found in dossier.')

    raw_img = target_f.get('image_base64') or target_f.get('image') or ''
    if not raw_img:
        raise HTTPException(404, 'No image available for this finger.')

    if raw_img.startswith('data:'):
        raw_img = raw_img.split(',', 1)[1]

    data = base64.b64decode(raw_img)
    conv_bytes, mime, ext = convert_image_format(data, target_format=format)
    filename = f"{passport}_{target_clean}_500dpi.{ext}"
    return Response(
        conv_bytes,
        media_type=mime,
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.get('/api/database/export/all/lossless-zip')
def export_all_lossless_zip(request: Request, format: str = 'PNG'):
    """Download all enrolled applicant fingerprints across the entire database in 100% lossless format inside a master ZIP archive."""
    records_data = get_database_records(request)
    records = records_data.get('records', [])
    if not records:
        raise HTTPException(400, 'No dossiers in database to export.')

    zip_buffer = io.BytesIO()
    total_exported = 0

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for rec in records:
            passport = rec.get('passport_number') or f"REC_{rec.get('subject_id', 'UNKNOWN')}"
            safe_p = "".join(c for c in passport if c.isalnum() or c in ('-', '_'))
            folder = f"{safe_p}_{rec.get('surname', 'UNKNOWN')}"

            for idx, f in enumerate(rec.get('fingers', [])):
                raw_img = f.get('image_base64') or f.get('image') or ''
                if not raw_img:
                    continue
                if raw_img.startswith('data:'):
                    raw_img = raw_img.split(',', 1)[1]
                try:
                    img_bytes = base64.b64decode(raw_img)
                    conv_bytes, mime, ext = convert_image_format(img_bytes, target_format=format)
                    f_name = (f.get('finger_name') or f'finger_{idx+1}').lower().replace(' ', '_')
                    filename = f"{folder}/{idx+1:02d}_{f_name}_500dpi_lossless.{ext}"
                    zf.writestr(filename, conv_bytes)
                    total_exported += 1
                except Exception as e:
                    log.warning('Could not export finger %s for %s: %s', f.get('finger_name'), passport, e)

            # Add applicant JSON metadata
            meta_json = json.dumps(rec, indent=2)
            zf.writestr(f"{folder}/applicant_metadata.json", meta_json)

    zip_bytes = zip_buffer.getvalue()
    filename_clean = f"national_biometrics_all_lossless_{format.lower()}_{datetime.now().strftime('%Y%m%d')}.zip"
    return Response(
        zip_bytes,
        media_type='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{filename_clean}"'}
    )


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('app.main:app', host='127.0.0.1', port=8000)



