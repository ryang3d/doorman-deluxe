"""Tests for the pure settings-search helper (src/ui/settings_search.js).

The helper has no DOM dependency so it's loadable under node. We shell out to
`node` and require the file directly — no test deps added to the project.
"""
import json
import os
import subprocess

HERE = os.path.dirname(__file__)
SEARCH_JS = os.path.abspath(os.path.join(HERE, '..', 'src', 'ui', 'settings_search.js'))


def _match(spec, query):
    """Run fieldMatchesQuery(spec, query) in node and return the boolean."""
    expr = ('console.log(JSON.stringify(fieldMatchesQuery(%s, %s)))'
            % (json.dumps(spec), json.dumps(query)))
    code = "const {fieldMatchesQuery}=require(process.argv[1]); %s" % expr
    out = subprocess.run(['node', '-e', code, SEARCH_JS],
                         capture_output=True, text=True, cwd=HERE)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def _f(label, key, help_):
    return {'label': label, 'key': key, 'help': help_}


def test_empty_query_matches_all():
    f = _f('Enable', 'DOORMAN_ENABLE', 'on/off')
    assert _match(f, '') is True
    assert _match(f, '   ') is True


def test_label_match_case_insensitive():
    f = _f('Enable', 'DOORMAN_ENABLE', 'on/off')
    assert _match(f, 'enable') is True
    assert _match(f, 'ENABLE') is True


def test_key_match():
    f = _f('Model path', 'DOORMAN_LLM_MODEL', 'path')
    assert _match(f, 'llm_model') is True


def test_help_match():
    f = _f('X', 'DOORMAN_X', 'wake word sensitivity')
    assert _match(f, 'sensitivity') is True


def test_no_match():
    f = _f('Enable', 'DOORMAN_ENABLE', 'on/off')
    assert _match(f, 'zzz') is False
