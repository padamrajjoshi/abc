"""MongoDB persistence layer for Greece / Schengen Visa Biometric Dossiers.
Connects via pymongo when available and gracefully handles offline/standalone environments.
"""
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urlparse

log = logging.getLogger(__name__)

try:
    import pymongo
    from bson import Binary
    HAVE_PYMONGO = True
except ImportError:
    HAVE_PYMONGO = False
    Binary = None


class MongoDossierStore:
    def __init__(self, uri: Optional[str] = None):
        self.username = (os.getenv('MONGODB_USERNAME') or '').strip()
        self.password = (os.getenv('MONGODB_PASSWORD') or '').strip()
        raw_uri = uri or os.getenv('MONGODB_URI') or os.getenv('MONGO_URI') or ''
        self.uri = self._resolve_uri(raw_uri)
        self.client = None
        self.db = None
        self.dossiers = None
        self.audit = None
        self._connected = False
        self._local_visa_refs: set[str] = set()
        self.init_connection()

    def _resolve_uri(self, raw_uri: str) -> str:
        raw = raw_uri.strip()
        if not raw:
            if os.path.exists('/.dockerenv') or os.getenv('CONTAINER'):
                return 'mongodb://host.docker.internal:27017/biometrics_studio'
            return 'mongodb://127.0.0.1:27017/biometrics_studio'

        # If username and password provided separately and not yet in URI
        if self.username and self.password and '@' not in raw:
            user_enc = quote_plus(self.username)
            pass_enc = quote_plus(self.password)
            if '://' in raw:
                prefix, rest = raw.split('://', 1)
                return f"{prefix}://{user_enc}:{pass_enc}@{rest}"
            else:
                return f"mongodb://{user_enc}:{pass_enc}@{raw}"

        # If running inside Docker container and pointing to localhost/127.0.0.1
        if os.path.exists('/.dockerenv') or os.getenv('CONTAINER'):
            if '://localhost' in raw:
                raw = raw.replace('://localhost', '://host.docker.internal')
            elif '://127.0.0.1' in raw:
                raw = raw.replace('://127.0.0.1', '://host.docker.internal')

        return raw

    def _safe_uri(self) -> str:
        """Mask credentials for display in logs and status API."""
        if '@' in self.uri:
            try:
                prefix, rest = self.uri.split('://', 1)
                creds, host_part = rest.split('@', 1)
                return f"{prefix}://***:***@{host_part}"
            except Exception:
                return 'mongodb://***:***@...'
        return self.uri

    def init_connection(self) -> bool:
        if not HAVE_PYMONGO:
            log.info('pymongo not installed; MongoDB persistence inactive (using local SQLite storage).')
            self._connected = False
            return False
        try:
            client_kwargs = {
                'serverSelectionTimeoutMS': 3000,
                'connectTimeoutMS': 3000
            }
            if self.username and self.password and '@' not in self.uri:
                client_kwargs['username'] = self.username
                client_kwargs['password'] = self.password

            self.client = pymongo.MongoClient(self.uri, **client_kwargs)
            # Test connectivity
            self.client.admin.command('ping')

            # Parse database name
            db_name = 'biometrics_studio'
            try:
                parsed = urlparse(self.uri)
                path = parsed.path.strip('/')
                if path:
                    db_name = path.split('?')[0]
            except Exception:
                db_name = self.uri.rsplit('/', 1)[-1].split('?')[0] or 'biometrics_studio'

            self.db = self.client[db_name]
            self.dossiers = self.db['visa_dossiers']
            self.audit = self.db['audit_logs']

            # Create indices
            self.dossiers.create_index('passport_number')
            self.dossiers.create_index('subject_id')
            self.dossiers.create_index('visa_ref', sparse=True)
            self.dossiers.create_index([('created_at', pymongo.DESCENDING)])
            self._connected = True
            log.info('MongoDB connected successfully: %s', self._safe_uri())
            return True
        except Exception as e:
            log.warning('MongoDB connection unavailable (%s); operating in fallback mode.', e)
            self._connected = False
            return False

    @property
    def is_connected(self) -> bool:
        if not self._connected and HAVE_PYMONGO:
            return self.init_connection()
        return self._connected

    def is_visa_ref_taken(self, visa_ref: str, exclude_passport: str = "") -> bool:
        """Check if a visa reference number is already taken by any stored dossier."""
        v_clean = (visa_ref or '').strip().upper()
        if not v_clean:
            return False
        if v_clean in self._local_visa_refs:
            # If checking with exclude_passport and it's in local cache, let's verify in db
            if not self.is_connected or self.dossiers is None:
                return True
        if not self.is_connected or self.dossiers is None:
            return v_clean in self._local_visa_refs
        try:
            query: Dict[str, Any] = {'visa_ref': v_clean}
            if exclude_passport:
                query['passport_number'] = {'$ne': exclude_passport.strip().upper()}
            return self.dossiers.count_documents(query, limit=1) > 0
        except Exception as e:
            log.warning('Could not check visa_ref uniqueness in MongoDB: %s', e)
            return v_clean in self._local_visa_refs

    def generate_unique_visa_ref(self, prefix: str = "GR-VIS", year: Optional[int] = None) -> str:
        """Generate a cryptographically random, collision-free Visa Reference Number.
        Guaranteed to never repeat any stored reference in the database.
        Format: GR-VIS-YYYY-XXXXXX (6 random digits, giving 900,000 unique values per year).
        """
        year = year or datetime.now().year
        for _ in range(5000):
            rand_code = secrets.randbelow(900000) + 100000
            candidate = f"{prefix}-{year}-{rand_code}"
            if not self.is_visa_ref_taken(candidate):
                self._local_visa_refs.add(candidate)
                return candidate

        # Fallback to 8-character high-entropy alphanumeric token if high volume
        alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
        while True:
            token = "".join(secrets.choice(alphabet) for _ in range(8))
            candidate = f"{prefix}-{year}-{token}"
            if not self.is_visa_ref_taken(candidate):
                self._local_visa_refs.add(candidate)
                return candidate

    def status(self) -> Dict[str, Any]:
        connected = self.is_connected
        stats = {
            'driver': 'pymongo' if HAVE_PYMONGO else 'not_installed',
            'connected': connected,
            'uri': self._safe_uri(),
            'dossier_count': 0
        }
        if connected and self.dossiers is not None:
            try:
                stats['dossier_count'] = self.dossiers.count_documents({})
            except Exception:
                pass
        return stats


    def upsert_dossier(self, data: Dict[str, Any]) -> Dict[str, Any]:
        passport_number = (data.get('passport_number') or '').strip().upper()
        now = datetime.now(timezone.utc).isoformat()
        current_year = datetime.now().year

        # Check existing dossier to preserve already assigned visa_ref
        existing = None
        if self.is_connected and self.dossiers is not None:
            try:
                existing = self.dossiers.find_one({'passport_number': passport_number})
            except Exception:
                existing = None

        candidate_ref = (data.get('visa_ref') or '').strip().upper()

        if existing and existing.get('visa_ref'):
            # If the applicant already has a visa_ref, keep their existing reference
            if not candidate_ref or candidate_ref == existing.get('visa_ref'):
                final_visa_ref = existing['visa_ref']
            elif not self.is_visa_ref_taken(candidate_ref, exclude_passport=passport_number):
                final_visa_ref = candidate_ref
            else:
                final_visa_ref = existing['visa_ref']
        else:
            # Brand new applicant: ensure candidate is non-repeating across entire database
            if candidate_ref and not self.is_visa_ref_taken(candidate_ref, exclude_passport=passport_number):
                final_visa_ref = candidate_ref
            else:
                final_visa_ref = self.generate_unique_visa_ref(year=current_year)

        self._local_visa_refs.add(final_visa_ref)

        doc = {
            'passport_number': passport_number,
            'surname': (data.get('surname') or '').strip().upper(),
            'given_names': (data.get('given_names') or '').strip().upper(),
            'nationality': (data.get('nationality') or 'GRC').strip().upper(),
            'date_of_birth': data.get('date_of_birth') or '',
            'gender': (data.get('gender') or 'M').strip().upper(),
            'passport_expiry': data.get('passport_expiry') or '',
            'visa_ref': final_visa_ref,
            'visa_type': data.get('visa_type') or 'SCHENGEN_C_TOURIST',
            'place_of_birth': data.get('place_of_birth') or '',
            'station_id': data.get('station_id') or 'ATH-CONSULAR-STATION-01',
            'subject_id': data.get('subject_id'),
            'updated_at': now,
        }

        # Handle fingers if passed
        if 'fingers' in data:
            doc['fingers'] = data['fingers']
            doc['finger_count'] = len(data['fingers'])
            doc['status'] = 'ENROLLED_10_PRINT' if len(data['fingers']) >= 10 else 'IN_PROGRESS'

        if self.is_connected and self.dossiers is not None:
            update_payload = {'$set': doc, '$setOnInsert': {'created_at': now}}
            self.dossiers.update_one({'passport_number': passport_number}, update_payload, upsert=True)

        return doc

    def append_fingerprint_group(self, passport_number: str, mode: str,
                                 fingers: List[Dict[str, Any]], simulated: bool = False) -> Dict[str, Any]:
        if not self.is_connected or self.dossiers is None:
            return {'appended': len(fingers)}

        passport_number = passport_number.strip().upper()
        existing = self.dossiers.find_one({'passport_number': passport_number})
        finger_dict = {}
        if existing and 'fingers' in existing:
            for f in existing['fingers']:
                finger_dict[f['finger_name']] = f

        # Map new fingers with quality classification
        for f in fingers:
            nfiq = f.get('nfiq_score', 5)
            rating = 'PASS (High Quality)' if nfiq <= 2 else 'PASS (Marginal)' if nfiq == 3 else 'RECAPTURE (Fail)'
            finger_dict[f['finger_name']] = {
                'finger_name': f['finger_name'],
                'group': mode,
                'nfiq_score': nfiq,
                'status': f.get('status', 'PASS'),
                'quality_rating': rating,
                'feature_count': f.get('feature_count', 0),
                'minutiae_count': f.get('minutiae_count'),
                'box': f.get('box', []),
                'image_base64': f.get('image', ''),
                'captured_at': datetime.now(timezone.utc).isoformat()
            }

        all_fingers = list(finger_dict.values())
        doc_status = 'ENROLLED_10_PRINT' if len(all_fingers) >= 10 else 'IN_PROGRESS'

        self.dossiers.update_one(
            {'passport_number': passport_number},
            {
                '$set': {
                    'fingers': all_fingers,
                    'finger_count': len(all_fingers),
                    'status': doc_status,
                    'simulated': simulated,
                    'updated_at': datetime.now(timezone.utc).isoformat()
                }
            },
            upsert=True
        )
        return {'finger_count': len(all_fingers), 'status': doc_status}

    def list_dossiers(self, limit: int = 500) -> List[Dict[str, Any]]:
        if not self.is_connected or self.dossiers is None:
            return []
        try:
            records = list(self.dossiers.find({}, {'_id': 0}).sort('updated_at', -1).limit(limit))
            return records
        except Exception as e:
            log.error('Failed to query MongoDB dossiers: %s', e)
            return []

    def get_dossier(self, identifier: str) -> Optional[Dict[str, Any]]:
        if not self.is_connected or self.dossiers is None:
            return None
        try:
            id_val = int(identifier) if identifier.isdigit() else -1
            rec = self.dossiers.find_one(
                {'$or': [
                    {'passport_number': identifier.strip().upper()},
                    {'subject_id': id_val}
                ]},
                {'_id': 0}
            )
            return rec
        except Exception:
            return None

    def delete_dossier(self, identifier: str) -> bool:
        if not self.is_connected or self.dossiers is None:
            return False
        try:
            id_val = int(identifier) if identifier.isdigit() else -1
            res = self.dossiers.delete_one({
                '$or': [
                    {'passport_number': identifier.strip().upper()},
                    {'subject_id': id_val}
                ]
            })
            return res.deleted_count > 0
        except Exception as e:
            log.error('Failed to delete dossier: %s', e)
            return False

    def delete_all_dossiers(self) -> int:
        if not self.is_connected or self.dossiers is None:
            return 0
        try:
            res = self.dossiers.delete_many({})
            return res.deleted_count
        except Exception as e:
            log.error('Failed to clear dossiers: %s', e)
            return 0

