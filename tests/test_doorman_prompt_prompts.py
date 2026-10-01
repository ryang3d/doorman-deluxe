import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import prompts_store as ps
import doorman_prompt as dp

def _use(d):
    os.environ['DOORMAN_DATA_DIR'] = d

def test_system_prompt_substitutes_identity_and_household(tmp_path):
    _use(str(tmp_path))
    got = dp.build_doorman_prompt(recognized_name='Ryan')
    assert '{identity}' not in got and '{household_hint}' not in got
    assert "recognized as Ryan" in got
    assert "the residents's home" in got
    unk = dp.build_doorman_prompt()
    assert "NOT recognized" in unk

def test_system_prompt_uses_override_from_store(tmp_path):
    _use(str(tmp_path))
    ps.save({'system': 'HELLO {identity}'})
    got = dp.build_doorman_prompt(recognized_name='Jenny')
    assert got.startswith('HELLO')
    assert 'Jenny' in got

def test_animal_prompt_uses_greeting_and_label(tmp_path):
    _use(str(tmp_path))
    got = dp.build_doorman_prompt(animal_label='cat')
    assert 'cat' in got
    assert '{greeting}' not in got
    assert '{animal_label}' not in got

def test_animal_greeting_line_uses_store_pools(tmp_path):
    _use(str(tmp_path))
    # default cat pool (6 lines) -> any of them
    assert dp.animal_greeting_line('cat') in dp.prompts_store.lines_of('animal_cat_lines')
    # override the cat pool to a single greeting -> that exact line is picked
    ps.save({'animal_cat_lines': 'MY CUSTOM CAT LINE'})
    assert dp.animal_greeting_line('cat') == 'MY CUSTOM CAT LINE'
    # unknown label -> the generic line
    assert dp.animal_greeting_line('hamster') == dp.prompts_store.get('animal_generic_line')
    # generic line is also editable
    ps.save({'animal_generic_line': 'A hamster?'})
    assert dp.animal_greeting_line('hamster') == 'A hamster?'
    ps.reset('animal_cat_lines'); ps.reset('animal_generic_line')

def test_trigger_text_includes_facts(tmp_path):
    _use(str(tmp_path))
    got = dp.interaction_trigger_text(doorbell_pressed=True, recognized_name='Ryan')
    assert '{facts}' not in got
    assert 'The visitor rang the doorbell.' in got
    assert 'recognized as Ryan' in got
    assert 'Greet them briefly' in got

def test_trigger_text_no_facts_no_leading_space(tmp_path):
    _use(str(tmp_path))
    got = dp.interaction_trigger_text()   # no doorbell, no name, label default 'person'
    assert got.startswith('A person was detected at the door.')
    assert 'Greet them briefly' in got
