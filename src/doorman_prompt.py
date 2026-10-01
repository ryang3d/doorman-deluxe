#!/usr/bin/env python3
"""Doorman persona + household policy (Phase 2, Task 2.1).

The system prompt is built as a pure function of the recognized-identity context so it
is testable and easy to evolve. Policy rules live here in one place.

Identity context: pass sub_label (e.g. "Ryan") or None for an unrecognized person.

The prompt TEMPLATE text (system / animal / trigger) plus the animal one-liner
pools and the generic fallback line now live in ``prompts_store`` so they are
editable from the web UI. The builders here read those templates and substitute
a fixed set of placeholders via literal ``str.replace``:
  system  -> {household_hint}, {identity}
  animal  -> {animal_label}, {greeting}
  trigger -> {facts}
"""
import json
import random
import prompts_store


# Reference copy of the built-in animal one-liner pools, kept so the module
# remains introspectable and pre-existing tests that read these constants keep
# passing. The live source of truth is ``prompts_store`` (see animal_greeting_line
# / build_doorman_prompt); these are mirrors of the store's defaults, not the
# values the builders read at runtime.
ANIMAL_LINES = {
    'cat': [
        "Oh, a kitty at the door! Here kitty kitty, you're not supposed to be in here.",
        "Scram, kitty cat! Nice to see you but the door's not open for the feline.",
        "Well well, a cat on the front porch. I hope you brought a little greeting for me.",
        "Oh my, a little cat has wandered in! Do you mind me saying hi?",
        "I see a cat at the door! Do you come here often?",
        "A cat at the front door! I can't open for a feline but I can say hello.",
    ],
    'dog': [
        "Well hello there, you big friendly doggy! Who's a good boy?",
        "A dog at the front door! You're such a good boy, come to say hi.",
        "Hey, a dog has stopped by! I hope you brought a nice greeting for me.",
        "Oh, a pup at the door! Do you mind me saying hello to you?",
        "Well, well, a dog has wandered in! I can't open the door but I can say hi.",
        "A nice big doggy at the door! You look like a good friend.",
    ],
}

# Reference fallback for any animal label not in ANIMAL_LINES (store-backed live
# value: prompts_store's 'animal_generic_line' default).
_ANIMAL_LINE_GENERIC = ("Oh, I see a little visitor at the door! "
                        "I can say hi but I can't open for you.")


def _substitute(template, **vals):
    """Replace {placeholder} tokens in `template` with `vals`, literal str.replace."""
    out = template
    for k, v in vals.items():
        out = out.replace('{' + k + '}', str(v))
    return out


def _identity_block(recognized_name):
    """The identity sentence: warm for a known person, guarded for a stranger."""
    if recognized_name:
        return (
            "\nThe visitor is recognized as " + str(recognized_name)
            + ", a household member or known friend. You may greet them by "
            "name and be warm, but still do not volunteer details about "
            "whether anyone else is home."
        )
    return (
        "\nThe visitor is NOT recognized as a household member. Do NOT "
        "reveal whether anyone is home. Keep responses polite but guarded."
    )


def animal_greeting_line(label):
    """Pick ONE natural one-liner from the store-backed pool for `label`."""
    key = {'cat': 'animal_cat_lines', 'dog': 'animal_dog_lines'}.get(label)
    if key:
        lines = prompts_store.lines_of(key)
        if lines:
            return random.choice(lines)
    return prompts_store.get('animal_generic_line')


def build_doorman_prompt(*, recognized_name=None, unknown_ok=True,
                         household_hint=("the residents"), animal_label=None):
    """Return the Gemini Live system prompt for a doorbell interaction.

    recognized_name: Frigate sub_label if a known face was matched (e.g. "Ryan"), else None.
    animal_label: if set ('cat'/'dog'), produce a short, playful, animal-aware prompt
                  instead of the full human visitor prompt. A concrete greeting
                  line is picked from the store pool so it varies per event.
    """
    if animal_label:
        line = animal_greeting_line(animal_label)
        return _substitute(prompts_store.get('animal'),
                           animal_label=animal_label, greeting=line)
    identity = _identity_block(recognized_name)
    return _substitute(prompts_store.get('system'),
                       household_hint=household_hint, identity=identity)


def interaction_trigger_text(*, recognized_name=None, doorbell_pressed=False,
                             label="person", animal=False):
    """Short text to prime the session with what triggered the interaction."""
    if animal:
        return ("A " + str(label) + " was detected at the front door. Say a single, "
                "short, playful one-liner that you can see it, then wrap up. Do "
                "not hold a full conversation - it is a " + str(label) + ", not a person.")
    facts = []
    if doorbell_pressed:
        facts.append("The visitor rang the doorbell.")
    if recognized_name:
        facts.append("This visitor is recognized as " + str(recognized_name) + ".")
    elif label:
        facts.append("A " + str(label) + " was detected at the door.")
    return _substitute(prompts_store.get('trigger'), facts=" ".join(facts)).strip()


def preview_system(template, recognized_name=None):
    """Render the main system prompt from an (optional) draft template so the UI
    can show the effect of an edit before it is saved."""
    identity = _identity_block(recognized_name)
    return _substitute(template, household_hint="the residents", identity=identity)


# ----------------------------------------------------------------------------- tests
if __name__ == "__main__":
    print(build_doorman_prompt(recognized_name="Ryan"))
    print("\n--- unknown ---")
    print(build_doorman_prompt())
    print("\n--- trigger (doorbell, unknown) ---")
    print(interaction_trigger_text(doorbell_pressed=True))
    print("\n--- trigger (recognized) ---")
    print(interaction_trigger_text(recognized_name="Ryan", doorbell_pressed=True))
