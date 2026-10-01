import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import prompts_store as ps

def _use(d):
    os.environ['DOORMAN_DATA_DIR'] = d

def test_defaults_have_all_seven_keys_and_placeholders(tmp_path):
    _use(str(tmp_path))
    d = ps.defaults()
    assert set(d.keys()) == {'system', 'animal', 'animal_cat_lines',
                             'animal_dog_lines', 'animal_generic_line',
                             'trigger', 'local_notes'}
    assert '{household_hint}' in d['system'] and '{identity}' in d['system']
    assert '{animal_label}' in d['animal'] and '{greeting}' in d['animal']
    assert '{facts}' in d['trigger']
    # animal pools are newline-delimited; each has several greetings
    assert len(d['animal_cat_lines'].splitlines()) >= 5
    assert len(d['animal_dog_lines'].splitlines()) >= 5
    assert d['animal_generic_line'].strip()
    # local_notes is plain text, no tokens required

def test_load_returns_defaults_when_no_file(tmp_path):
    _use(str(tmp_path))
    assert ps.load() == ps.defaults()
    assert ps.get('system') == ps.defaults()['system']

def test_save_writes_file_and_load_returns_saved(tmp_path):
    _use(str(tmp_path))
    ps.save({'system': 'CUSTOM BODY {identity}'})
    p = ps._path()
    assert os.path.exists(p)
    on_disk = json.load(open(p))
    assert on_disk['system'] == 'CUSTOM BODY {identity}'
    assert ps.get('system') == 'CUSTOM BODY {identity}'
    # untouched keys still come from defaults
    assert ps.get('local_notes') == ps.defaults()['local_notes']

def test_save_ignores_unknown_and_non_str(tmp_path):
    _use(str(tmp_path))
    written = ps.save({'nope': 'x', 'system': 'ok', 'animal': 5})
    assert written == ['system']
    assert ps.get('system') == 'ok'
    assert ps.get('animal') == ps.defaults()['animal']

def test_reset_removes_and_falls_back_to_default(tmp_path):
    _use(str(tmp_path))
    ps.save({'trigger': 'EDITED {facts}'})
    assert ps.get('trigger') == 'EDITED {facts}'
    assert ps.reset('trigger') is True
    assert ps.get('trigger') == ps.defaults()['trigger']
    assert ps.reset('trigger') is False   # already at default

def test_bad_file_falls_back_to_defaults(tmp_path):
    _use(str(tmp_path))
    os.makedirs(str(tmp_path), exist_ok=True)
    open(os.path.join(tmp_path, 'prompts.json'), 'w').write('{not json')
    assert ps.load() == ps.defaults()

def test_animal_pool_override_and_lines_of(tmp_path):
    _use(str(tmp_path))
    ps.save({'animal_cat_lines': 'Line one\n\nLine two\n'})
    # lines_of trims blanks -> exactly two greetings
    assert ps.lines_of('animal_cat_lines') == ['Line one', 'Line two']
    # generic line override
    ps.save({'animal_generic_line': 'Hi there, little visitor!'})
    assert ps.get('animal_generic_line') == 'Hi there, little visitor!'
    ps.reset('animal_cat_lines'); ps.reset('animal_generic_line')
