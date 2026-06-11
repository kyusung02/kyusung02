"""utils.py — safe_filename (path traversal 방지) 검증."""
from utils import safe_filename


def test_traversal_stripped():
    assert safe_filename('../../etc/passwd') == 'passwd'
    assert '..' not in safe_filename('..\\..\\windows')


def test_dots_only_becomes_unnamed():
    assert safe_filename('..') == 'unnamed'
    assert safe_filename('') == 'unnamed'


def test_korean_name_preserved():
    assert safe_filename('삼성전자') == '삼성전자'
    assert safe_filename('KX하이텍') == 'KX하이텍'


def test_no_separators_in_output():
    for raw in ('a/b/c', 'a\\b\\c', 'US_{x}/../y'):
        out = safe_filename(raw)
        assert '/' not in out and '\\' not in out
