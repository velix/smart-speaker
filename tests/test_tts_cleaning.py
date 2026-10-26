"""TTS text cleaning: markdown, emoji, emoticons."""
from voice_assistant.assistant import clean_for_tts


def test_markdown_asterisks():
    assert clean_for_tts("*Hello!* **How are you?**") == "Hello! How are you?"


def test_underscores_and_backticks():
    assert clean_for_tts("I'm _great_ and `fast`") == "I'm great and fast"
    assert clean_for_tts("a_b file_name") == "a b file name"  # stray markers removed


def test_text_emoticons():
    assert clean_for_tts("Sounds good :)") == "Sounds good"
    assert clean_for_tts("Nice :-D ;)") == "Nice"
    assert clean_for_tts("Oops :(") == "Oops"


def test_emoji_removed():
    assert clean_for_tts("I'm happy \U0001F600 \u2764\uFE0F") == "I'm happy"


def test_whitespace_collapsed():
    assert clean_for_tts("hello   world !") == "hello world !"


def test_normal_text_untouched():
    assert clean_for_tts("Hi there! I can help with info, reminders, or questions.") == \
        "Hi there! I can help with info, reminders, or questions."


def test_parentheses_survive():
    # "(", ")" alone must not be eaten by the emoticon pattern
    assert clean_for_tts("Call me (on my phone) later.") == "Call me (on my phone) later."
