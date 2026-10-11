"""Functional acquisition/storage/API tests. No real biometric identity is used."""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.device import SyntheticSlapGenerator
from app.imaging import MODES, decode_png, encode_png
from app.main import app
from app.matcher import AFISDatabase
from app.quality import QualityScorer
from app.segmentation import SlapSegmenter
from app.sourceafis import OpenCVDemoEngine


@pytest.fixture
def database(tmp_path):
    quality = QualityScorer()
    quality.binary = None
    segmenter = SlapSegmenter()
    segmenter.binary = None
    return AFISDatabase(tmp_path/'test.db', segmenter, quality, OpenCVDemoEngine())


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('MATCHER_ENGINE', 'demo')
    monkeypatch.setenv('DEVICE_MODE', 'simulation')
    monkeypatch.setenv('BIOMETRICS_DB', str(tmp_path/'api.db'))
    monkeypatch.delenv('API_TOKEN', raising=False)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize('mode', list(MODES))
def test_segmentation_anatomy(mode, database):
    image = SyntheticSlapGenerator().generate(mode=mode, identity=24)
    assert image.shape == (1500,1600) and image.dtype == np.uint8
    fingers = database.process_slap(encode_png(image), mode)
    assert [f['finger_name'] for f in fingers] == MODES[mode]
    assert len(fingers) == (2 if mode == 'thumbs' else 4)
    assert all(len(f['image_bytes']) > 1000 and f['feature_count'] >= 12 for f in fingers)
    assert all(1 <= f['nfiq_score'] <= 5 and f['estimated'] for f in fingers)
    assert all(f['box'][0] >= 0 and f['box'][1] >= 0 for f in fingers)


def test_no_fabricated_fingers(database):
    with pytest.raises(ValueError, match='Detected 0'):
        database.segmenter.segment(np.full((1500,1600),255,np.uint8))


def test_enroll_append_verify_search_and_persistence(database):
    generator = SyntheticSlapGenerator()
    right = encode_png(generator.generate(identity=101))
    a = database.enroll_subject('Synthetic A',right,simulated=True)
    for mode in ('left4','thumbs'):
        database.enroll_subject('Synthetic A',encode_png(generator.generate(mode=mode,identity=101)),
                                mode, a['subject_id'], True)
    assert database.subjects()[0]['finger_count'] == 10
    database.enroll_subject('Synthetic A',right,subject_id=a['subject_id'],simulated=True)
    assert database.subjects()[0]['finger_count'] == 10
    b = database.enroll_subject('Synthetic B',encode_png(generator.generate(identity=999)),simulated=True)
    match = database.verify_1to1(a['subject_id'],right,simulated=True)
    assert match['is_match'] and match['compared_fingers'] == 4
    assert not database.verify_1to1(b['subject_id'],right,simulated=True)['is_match']
    hits = database.search_1toN(right,simulated=True)
    assert hits[0]['subject_id'] == a['subject_id']
    reopened = AFISDatabase(database.path, database.segmenter, database.quality, database.engine)
    assert reopened.verify_1to1(a['subject_id'],right)['is_match']
    assert not reopened.verify_1to1(a['subject_id'],right,mode='left4')['is_match']
    assert not reopened.verify_1to1(a['subject_id'],right,simulated=False)['is_match']


def test_bad_quality_does_not_write(database, monkeypatch):
    image = encode_png(SyntheticSlapGenerator().generate())
    monkeypatch.setattr(database.quality,'score',lambda img: {
        'nfiq_score':5,'status':'FAIL','quality_engine':'test','estimated':True})
    with pytest.raises(ValueError,match='quality'):
        database.enroll_subject('Rejected',image)
    assert database.subjects() == []


def test_estimated_quality_policy(database):
    image = encode_png(SyntheticSlapGenerator().generate())
    with pytest.raises(ValueError,match='estimated'):
        database.enroll_subject('Restricted',image,allow_estimated=False)
    assert database.subjects() == []


def test_capture_to_search(client):
    capture = client.post('/api/capture',json={'identity':78}).json()
    assert capture['simulated']
    payload = {'capture_id':capture['capture_id'],'mode':'right4'}
    segments = client.post('/api/segment',json=payload)
    assert segments.status_code == 200
    assert len(segments.json()['fingers']) == 4
    enrollment = client.post('/api/enroll',json={**payload,'name':'API synthetic subject'})
    assert enrollment.status_code == 200
    subject = enrollment.json()['subject_id']
    verification = client.post('/api/verify',json={**payload,'subject_id':subject,'kind':'1:1'})
    assert verification.status_code == 200 and verification.json()['is_match']
    search = client.post('/api/verify',json={**payload,'kind':'1:N'})
    assert search.json()['candidates'][0]['subject_id'] == subject
    fid = verification.json()['finger_scores'][0]['fingerprint_id']
    assert client.get(f'/api/fingerprints/{fid}/image').headers['content-type'] == 'image/png'
    assert client.get('/api/subjects').json()['subjects'][0]['finger_count'] == 4
    assert client.get('/').status_code == 200


def test_validation_and_auth(client, monkeypatch):
    assert client.post('/api/segment',json={'image':'invalid'}).status_code == 422
    assert client.post('/api/verify',json={'image':'abc','threshold':0}).status_code == 422
    assert client.post('/api/capture',json={'mode':'invalid'}).status_code == 422
    capture = client.post('/api/capture',json={}).json()
    assert client.post('/api/segment',json={'capture_id':capture['capture_id'],'mode':'thumbs'}).status_code == 422
    assert client.post('/api/capture',json={},headers={'Origin':'https://other.example'}).status_code == 403
    monkeypatch.setenv('API_TOKEN','test-secret')
    assert client.get('/api/subjects').status_code == 401
    assert client.get('/api/subjects',headers={'Authorization':'Bearer test-secret'}).status_code == 200
    assert client.post('/api/capture',content=b'x'*(12*1024*1024+1)).status_code == 413


def test_invalid_probe_subject_and_names(database):
    image = encode_png(SyntheticSlapGenerator().generate())
    with pytest.raises(ValueError):
        database.enroll_subject(' ',image)
    with pytest.raises(ValueError,match='exist'):
        database.verify_1to1(999,image)
    with pytest.raises(ValueError,match='finite'):
        database.search_1toN(image,float('nan'))


def test_real_sourceafis_when_available(tmp_path):
    from app.sourceafis import SourceAFISEngine
    try:
        engine = SourceAFISEngine()
    except (ImportError, RuntimeError):
        pytest.skip('Java/SourceAFIS runtime unavailable; run the Docker integration profile.')
    data = encode_png(SyntheticSlapGenerator().generate())
    database = AFISDatabase(tmp_path/'sourceafis.db',engine=engine)
    result = database.enroll_subject('Official engine test',data,simulated=True)
    assert database.verify_1to1(result['subject_id'],data)['is_match']
