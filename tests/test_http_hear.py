import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
import audio_bridge as ab


def test_parse_challenge():
    h = 'Digest realm="Login to Z", qop="auth", nonce="12345", opaque="xyz"'
    ch = ab._parse_challenge(h)
    assert ch['realm'] == 'Login to Z'
    assert ch['qop'] == 'auth'
    assert ch['nonce'] == '12345'
    assert ch['opaque'] == 'xyz'


def test_digest_header_shape():
    ch = {'realm': 'r', 'nonce': 'n', 'qop': 'auth', 'opaque': 'o'}
    h = ab._digest_header('GET', ab.GETAUDIO_PATH, ch, user='admin', password='pw')
    assert h.startswith('Digest username="admin"')
    assert 'uri="' + ab.GETAUDIO_PATH + '"' in h
    assert 'qop=auth' in h
    assert 'opaque="o"' in h


def test_detect_alaw():
    assert ab._detect_audio_args('Audio/G.711A', b'\x00\x00\x00\x00') == ['-f', 'alaw', '-ar', '8000', '-ac', '1']


def test_detect_aac_sniff():
    assert ab._detect_audio_args('', b'\xff\xf1\x00\x00') == ['-f', 'aac']
