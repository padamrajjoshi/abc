"""Unit tests for PassportEye, pytesseract, pdfplumber, and pypdf passport ingestion."""
import pytest
from app.passport import (
    PassportExtractor,
    format_mrz_date,
    icao_check_digit,
    parse_icao_td3_mrz,
    parse_text_fields,
    normalize_date_string,
)


def test_icao_check_digit():
    # Test known passport check digits
    # Passport number "GR9842105"
    assert 0 <= icao_check_digit("GR9842105") <= 9
    # DOB "920615"
    assert 0 <= icao_check_digit("920615") <= 9


def test_format_mrz_date():
    # DOB tests
    assert format_mrz_date("920615", is_expiry=False) == "1992-06-15"
    assert format_mrz_date("040822", is_expiry=False) == "2004-08-22"
    
    # Expiry tests (future dates)
    assert format_mrz_date("320614", is_expiry=True) == "2032-06-14"
    assert format_mrz_date("281130", is_expiry=True) == "2028-11-30"


def test_parse_icao_td3_mrz():
    # Standard 2-line ICAO Doc 9303 TD3 MRZ
    l1 = "P<GRCPAPADOPOULOS<<NIKOLAOS<<<<<<<<<<<<<<<<<"
    # Calculate exact check digits
    num = "GR9842105"
    cd_num = str(icao_check_digit(num))
    dob = "920615"
    cd_dob = str(icao_check_digit(dob))
    exp = "320614"
    cd_exp = str(icao_check_digit(exp))
    l2 = f"{num}{cd_num}GRC{dob}{cd_dob}M{exp}{cd_exp}<<<<<<<<<<<<<<00"

    parsed = parse_icao_td3_mrz([l1, l2])
    assert parsed is not None
    assert parsed['passport_number'] == "GR9842105"
    assert parsed['surname'] == "PAPADOPOULOS"
    assert parsed['given_names'] == "NIKOLAOS"
    assert parsed['nationality'] == "GRC"
    assert parsed['date_of_birth'] == "1992-06-15"
    assert parsed['gender'] == "M"
    assert parsed['passport_expiry'] == "2032-06-14"
    assert parsed['mrz_verified'] is True


def test_parse_text_fields_viz():
    sample_ocr = """
    HELLENIC REPUBLIC
    PASSPORT / PASSEPORT
    PASSPORT NO: GR8491203
    SURNAME: DIMITRIOU
    GIVEN NAMES: ELENI
    NATIONALITY: GRC
    DATE OF BIRTH: 24/08/1988
    SEX: F
    DATE OF EXPIRY: 18/05/2033
    """
    fields = parse_text_fields(sample_ocr)
    assert fields.get('passport_number') == 'GR8491203'
    assert fields.get('surname') == 'DIMITRIOU'
    assert fields.get('given_names') == 'ELENI'
    assert fields.get('gender') == 'F'
    assert fields.get('date_of_birth') == '1988-08-24'
    assert fields.get('passport_expiry') == '2033-05-18'


def test_passport_extractor_init():
    extractor = PassportExtractor()
    assert 'passporteye' in extractor.engines_available
    assert 'pytesseract' in extractor.engines_available
    assert 'pdfplumber' in extractor.engines_available
    assert 'pypdf' in extractor.engines_available


def test_passport_extractor_empty_bytes():
    extractor = PassportExtractor()
    res = extractor.extract_from_bytes(b'not a valid image or pdf', filename='test.jpg')
    assert res['success'] is False
    assert res['passport_number'] == ''
    assert 'diagnostics' in res


def test_parse_mrz_with_header_noise_nepal_passport():
    # User's exact OCR lines from the uploaded Nepal passport image
    lines = [
        'PASSPORTWTTYPEAREAEACOUNTHYCODETFPASSPORTNO<',
        'P<NPLSAH<<SANTOSH<KUMARK<<<<<<<<<<<K<<<<<<<<',
        'PA07500249NPL9703050M320911419017403895<<<12'
    ]
    parsed = parse_icao_td3_mrz(lines)
    assert parsed is not None
    assert parsed['passport_number'] == 'PA0750024'
    assert parsed['surname'] == 'SAH'
    assert parsed['given_names'] == 'SANTOSH KUMAR'
    assert parsed['nationality'] == 'NPL'
    assert parsed['date_of_birth'] == '1997-03-05'
    assert parsed['gender'] == 'M'
    assert parsed['passport_expiry'] == '2032-09-11'
    assert parsed['mrz_verified'] is True
